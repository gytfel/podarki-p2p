"""Автопокупка: покупает выгодные лоты строго в рамках лимитов и выставляет их на продажу.

Правила безопасности:
- Ничего не покупается, пока AUTOBUY_MODE не live и вы не отправили /autobuy on.
  После каждого перезапуска автопокупка снова выключена.
- Любая непонятная ситуация (сбой площадки, неподтверждённая покупка, внутренняя ошибка)
  выключает автопокупку. Включить обратно можно только командой.
- Запрос на покупку никогда не повторяется: при сбое связи бот проверяет инвентарь,
  а не платит второй раз.
- Покупки идут строго по одной, с паузой между ними.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
from collections import defaultdict
from typing import TYPE_CHECKING

from .config import Config
from .evaluator import Deal
from .markets.base import AuthError, BuyRejected, MarketAdapter, MarketError
from .models import Listing, OwnedGift
from .store import OPEN_STATUSES, SPENT_STATUSES, STATUS_NAMES, Position
from .ui import Button, Reply

if TYPE_CHECKING:
    from .scanner import Scanner

log = logging.getLogger(__name__)

DAY = 86400.0


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def ton(value: float) -> str:
    return f"{value:.2f}"


def ceil_cents(value: float) -> float:
    return math.ceil(value * 100 - 1e-9) / 100


class Trader:
    def __init__(
        self,
        cfg: Config,
        scanner: Scanner,
        *,
        sleep=asyncio.sleep,
        confirm_attempts: int = 4,
        confirm_delay: float = 3.0,
    ):
        self.cfg = cfg
        self.ab = cfg.autobuy
        self.scanner = scanner
        self.armed = False
        self.disarm_reason: str | None = None
        self._last_buy = -math.inf
        self._lock = asyncio.Lock()
        self._notices: dict[str, float] = {}
        self._sleep = sleep
        self._confirm_attempts = confirm_attempts
        self._confirm_delay = confirm_delay

    # ------------------------------------------------------------ вспомогательное

    @property
    def store(self):
        return self.scanner.store

    def _adapter(self, market: str) -> MarketAdapter:
        return self.scanner.adapters[market]

    def _title(self, market: str) -> str:
        return self._adapter(market).title

    async def notify(self, text: str, buttons: tuple[tuple[Button, ...], ...] = ()) -> None:
        await self.scanner.bot.send(text, buttons=buttons)

    async def notify_once(self, key: str, text: str, every: float = 3600.0) -> None:
        """Одно и то же предупреждение — не чаще раза в час."""
        now = self.scanner.clock()
        if now - self._notices.get(key, -math.inf) >= every:
            self._notices[key] = now
            await self.notify(text)

    def usage(self) -> tuple[float, int, int]:
        """Потрачено за сутки, покупок за сутки, непроданных позиций."""
        statuses = SPENT_STATUSES + (("dry",) if self.ab.mode == "dry" else ())
        spent, count = self.store.spent_since(DAY, statuses)
        return spent, count, len(self.store.positions(OPEN_STATUSES))

    def disarm(self, reason: str) -> bool:
        """Выключает автопокупку. True — если она была включена."""
        if not self.armed:
            return False
        self.armed = False
        self.disarm_reason = reason
        log.warning("Автопокупка выключена: %s", reason)
        return True

    async def _trade(self, market: str, func, *args):
        """Торговый запрос. 401/403 значит «запрос не принят» — тогда можно обновить вход и повторить."""
        try:
            return await func(*args)
        except AuthError:
            if not await self.scanner.refresh_auth(market):
                raise
            return await func(*args)

    # ------------------------------------------------------------ решение о покупке

    async def consider(self, deal: Deal, anomaly: bool) -> int | None:
        """Вызывается сканером для каждой найденной сделки. Возвращает номер позиции, если купили."""
        if not self.ab.enabled or not self.armed:
            return None
        reason = self._static_reason(deal, anomaly)
        if reason:
            log.info("Автопокупка пропустила %s: %s", deal.listing.title(), reason)
            return None
        try:
            async with self._lock:
                if not self.armed:  # могли выключить, пока ждали очереди
                    return None
                reason = await self._limits_reason(deal)
                if reason:
                    log.info("Автопокупка пропустила %s: %s", deal.listing.title(), reason)
                    return None
                return await self._buy(deal)
        except Exception as exc:
            log.exception("Внутренняя ошибка автопокупки")
            self.disarm("внутренняя ошибка")
            await self.notify(
                f"⚠️ <b>Внутренняя ошибка автопокупки</b>: {esc(type(exc).__name__)}: {esc(exc)}\n"
                "Автопокупка выключена. Проверьте /positions и лог, затем /autobuy on."
            )
            return None

    def _static_reason(self, deal: Deal, anomaly: bool) -> str | None:
        item = deal.listing
        if anomaly:
            return "подозрительно низкая цена — только вручную"
        if item.market not in self.ab.markets:
            return f"покупки на {self._title(item.market)} не разрешены (AUTOBUY_MARKETS)"
        if deal.cross and not self.ab.cross:
            return "покупки под перепродажу на другой площадке выключены (AUTOBUY_CROSS)"
        if deal.cross and item.locked_until:
            return "подарок нельзя сразу вывести"
        if deal.profit < self.ab.min_profit_ton or deal.profit_pct < self.ab.min_profit_pct:
            return f"прибыль {deal.profit:.2f} TON ниже порога автопокупки"
        if deal.cost > self.ab.max_price:
            return f"цена {deal.cost:.2f} TON выше лимита на одну покупку"
        if item.number is None:
            return "у лота нет номера — покупку нельзя будет проверить"
        for market in {item.market, deal.sell_market}:
            health = self.scanner.health[market]
            if health.tripped or health.consecutive:
                return f"{self._title(market)} работает с ошибками"
        return None

    async def _limits_reason(self, deal: Deal) -> str | None:
        if self.scanner.clock() - self._last_buy < self.ab.cooldown:
            return "пауза между покупками"
        spent, count, open_count = self.usage()
        if count >= self.ab.max_per_day:
            await self.notify_once(
                "max_per_day",
                f"🛑 Автопокупка: достигнут лимит {self.ab.max_per_day} покупок за сутки. Новые — когда окно освободится.",
            )
            return "лимит покупок за сутки"
        if spent + deal.cost > self.ab.daily_limit:
            await self.notify_once(
                "daily_limit",
                f"🛑 Автопокупка: за сутки потрачено {ton(spent)} из {ton(self.ab.daily_limit)} TON — "
                f"лот за {ton(deal.cost)} не помещается в лимит.",
            )
            return "дневной лимит трат"
        if open_count >= self.ab.max_open:
            await self.notify_once(
                "max_open",
                f"🛑 Автопокупка: уже {open_count} непроданных подарков (лимит {self.ab.max_open}). "
                "Новые покупки — после продажи. Список: /positions",
            )
            return "лимит непроданных подарков"

        market = deal.listing.market
        title = self._title(market)
        try:
            info = await self.scanner.call(market, self._adapter(market).balance)
        except MarketError as error:
            return f"не удалось проверить баланс: {error}"
        if info.auto_withdraw:
            await self.notify_once(
                f"withdraw:{market}",
                f"⚠️ На {title} включён вывод купленных подарков сразу в Telegram — бот не сможет их выставить. "
                "Выключите эту настройку в приложении площадки.",
            )
            return "включён автовывод купленного"
        if info.available - deal.cost < self.ab.reserve:
            await self.notify_once(
                f"balance:{market}",
                f"💸 Автопокупка: на {title} {ton(info.available)} TON — не хватает на лот за {ton(deal.cost)} "
                f"с запасом {ton(self.ab.reserve)} TON.",
            )
            return "мало средств"
        return None

    # ------------------------------------------------------------ покупка

    async def _buy(self, deal: Deal) -> int | None:
        item = deal.listing
        market = item.market
        title = self._title(market)
        name = esc(item.title())
        self._last_buy = self.scanner.clock()

        if self.ab.mode == "dry":
            position_id = self.store.open_position(deal, "dry")
            await self.notify(
                f"🧪 <b>Тест #{position_id}:</b> купил бы {name} на {title} за {ton(deal.cost)} TON, "
                f"ожидаемая прибыль {deal.profit:+.2f} TON. Реальной покупки не было."
            )
            return position_id

        position_id = self.store.open_position(deal, "pending")
        log.warning("ПОКУПКА #%d: %s на %s за %.2f TON", position_id, item.title(), market, item.price)
        failure: str | None = None
        try:
            await self._trade(market, self._adapter(market).buy, item)
        except (BuyRejected, AuthError) as error:
            self.store.update_position(position_id, status="failed", note=str(error)[:500])
            await self.notify(
                f"↩️ Не купилось #{position_id}: {name} — {esc(error)}.\n"
                "Скорее всего, лот уже купили или изменилась цена. Деньги не списаны."
            )
            return None
        except Exception as error:  # сеть, формат ответа: неизвестно, прошла ли покупка
            failure = f"{type(error).__name__}: {error}"
            log.error("Покупка #%d: сбой во время запроса: %s", position_id, failure)

        owned = await self._confirm_owned(market, item)
        if owned is None:
            self.store.update_position(position_id, status="unknown", note=(failure or "нет в инвентаре")[:500])
            self.disarm(f"не удалось подтвердить покупку #{position_id}")
            what = (
                f"сбой во время покупки ({esc(failure)}) — неизвестно, прошла ли она"
                if failure
                else "площадка ответила «успешно», но подарка нет в инвентаре"
            )
            await self.notify(
                f"⚠️ <b>Покупка #{position_id}: {what}</b>\n{name} за {ton(deal.cost)} TON на {title}.\n\n"
                "Проверьте инвентарь и баланс вручную. Автопокупка выключена — включить: /autobuy on.\n"
                f"Бот продолжит искать подарок в инвентаре. Если разобрались сами: /forget {position_id}",
                buttons=((("📦 Позиции", "+/positions"), ("🤖 Автопокупка", "+/autobuy")),),
            )
            return position_id

        self.store.update_position(position_id, status="bought", owned_id=owned.owned_id)
        lines = [f"🛒 <b>Куплено #{position_id}</b>: {name}", f"На {title} за {ton(deal.cost)} TON с комиссией."]
        if failure:
            lines.append("Во время покупки был сбой связи, но подарок в инвентаре.")
        if deal.cross:
            lines.append(
                f"Перенесите подарок на {self._title(deal.sell_market)} и выставьте примерно за "
                f"{ton(deal.resale_price)} TON — ожидаемая прибыль {deal.profit:+.2f} TON. "
                f"Когда подарок уйдёт из инвентаря {title}, позиция закроется сама."
            )
        elif self.ab.autolist:
            lines.append(await self._list(position_id, deal, owned))
        else:
            lines.append(f"Автовыставление выключено: выставьте вручную примерно за {ton(deal.resale_price)} TON.")
        await self.notify("\n".join(lines))
        return position_id

    async def _confirm_owned(self, market: str, item: Listing) -> OwnedGift | None:
        adapter = self._adapter(market)
        for attempt in range(self._confirm_attempts):
            if attempt:
                await self._sleep(self._confirm_delay)
            try:
                gifts = await self.scanner.call(market, adapter.owned)
            except MarketError:
                continue
            for gift in gifts:
                if gift.matches(item.collection, item.number, item.listing_id):
                    return gift
        return None

    async def _list(self, position_id: int, deal: Deal, owned: OwnedGift) -> str:
        """Выставляет купленное: чуть дешевле самого дешёвого конкурента, но не ниже цены без убытка."""
        market = deal.sell_market
        adapter = self._adapter(market)
        fees = self.cfg.fees[market]
        th = self.cfg.thresholds
        try:
            items = await self.scanner.call(market, adapter.cheapest, deal.listing.key, self.cfg.verify_depth)
        except MarketError:
            items = None
        competitors = [i for i in items or [] if not owned.matches(i.collection, i.number, i.listing_id)]
        ref = competitors[0].price if competitors else deal.ref_price
        target = ref * (1 - th.undercut)
        no_loss = (deal.cost + th.min_profit_ton) / (1 - fees.sell)
        price = ceil_cents(max(target, no_loss))
        try:
            await self._trade(market, adapter.list_for_sale, owned, price)
        except Exception as error:
            log.warning("Позиция #%d: не удалось выставить: %s", position_id, error)
            return f"⚠️ Выставить не получилось ({esc(error)}). Выставьте вручную примерно за {ton(price)} TON."
        proceeds = price * (1 - fees.sell)
        profit = proceeds - deal.cost
        self.store.update_position(position_id, status="listed", list_price=price, expected_profit=profit)
        text = f"Выставлено за {ton(price)} TON → на руки {ton(proceeds)}, прибыль {profit:+.2f} TON."
        if price > ceil_cents(target) + 0.005:
            text += (
                f"\nРынок просел (самый дешёвый сейчас {ton(ref)}), поэтому цена выставлена без убытка — "
                "продаваться может дольше."
            )
        return text

    # ------------------------------------------------------------ сопровождение позиций

    async def recover(self) -> None:
        """При запуске: покупки, прерванные перезапуском, переводим в «неизвестно» и проверяем инвентарь."""
        interrupted = self.store.positions(("pending",))
        for position in interrupted:
            self.store.update_position(position.id, status="unknown", note="бот перезапустился во время покупки")
        if interrupted:
            ids = ", ".join(f"#{p.id}" for p in interrupted)
            await self.notify(
                f"⚠️ Бот перезапустился во время покупки {ids}. Проверяю инвентарь; "
                "если подарок найдётся — сообщу. Список: /positions"
            )

    async def check_positions(self) -> None:
        """Проверяет инвентарь: продались ли выставленные подарки, нашлись ли «неизвестные» покупки."""
        by_market: dict[str, list[Position]] = defaultdict(list)
        for position in self.store.positions(("bought", "listed", "unknown")):
            by_market[position.buy_market].append(position)
        for market, positions in by_market.items():
            if market not in self.scanner.adapters or self.scanner.health[market].tripped:
                continue
            try:
                gifts = await self.scanner.call(market, self._adapter(market).owned)
            except MarketError:
                continue
            if not getattr(gifts, "complete", True):
                await self.notify_once(
                    f"inventory:{market}",
                    f"⚠️ На {self._title(market)} в инвентаре слишком много подарков, бот читает не все. "
                    "Продажи могут не определяться: проверяйте /positions и закрывайте вручную через /forget номер.",
                    every=DAY,
                )
            for position in positions:
                await self._check_position(position, gifts)

    async def _check_position(self, position: Position, gifts: list[OwnedGift]) -> None:
        found = next((g for g in gifts if g.matches(position.collection, position.number, position.owned_id)), None)
        name = esc(position.title())
        if position.status == "unknown":
            if found:
                self.store.update_position(position.id, status="bought", owned_id=found.owned_id, misses=0)
                await self.notify(
                    f"✅ Покупка #{position.id} подтвердилась: {name} в инвентаре. Выставьте его вручную."
                )
            return
        if found:
            if position.misses:
                self.store.update_position(position.id, misses=0)
            return
        if not getattr(gifts, "complete", True):
            return  # инвентарь прочитан не целиком: отсутствие подарка не значит, что он продан
        if position.misses < 1:  # ждём второй проверки, чтобы не ошибиться из-за сбоя выдачи
            self.store.update_position(position.id, misses=position.misses + 1)
            return
        title = self._title(position.buy_market)
        if position.status == "listed":
            self.store.update_position(position.id, status="sold")
            await self.notify(
                f"💰 <b>Продано #{position.id}</b>: {name}\n"
                f"Выставляли за {ton(position.list_price or 0)} TON, прибыль {position.expected_profit:+.2f} TON.\n"
                "(Если вы сами сняли или вывели подарок — прибыль не получена.)"
            )
        else:
            self.store.update_position(position.id, status="closed", note="подарок ушёл из инвентаря")
            await self.notify(f"📦 Позиция #{position.id} закрыта: {name} больше нет в инвентаре {title}.")

    # ------------------------------------------------------------ команды

    async def arm(self) -> str:
        if not self.ab.enabled:
            return (
                "Автопокупка выключена в настройках: AUTOBUY_MODE=off.\n"
                "Поставьте dry (тест без покупок) или live и перезапустите бота."
            )
        problems: list[str] = []
        balances: list[str] = []
        for market in self.ab.markets:
            adapter = self._adapter(market)
            if self.scanner.health[market].tripped:
                problems.append(f"{adapter.title}: площадка остановлена — сначала /resume")
                continue
            problem = adapter.trading_problem()
            if problem:
                problems.append(f"{adapter.title}: {esc(problem)}")
                continue
            try:
                info = await self.scanner.call(market, adapter.balance)
            except MarketError as error:
                problems.append(f"{adapter.title}: не удалось проверить баланс ({esc(error.kind)})")
                continue
            if info.auto_withdraw:
                problems.append(f"{adapter.title}: включён вывод купленного в Telegram — выключите в приложении")
            balances.append(f"{adapter.title}: {ton(info.available)} TON")
        if problems:
            return "Автопокупка не включена:\n" + "\n".join(f"• {p}" for p in problems)
        self.armed = True
        self.disarm_reason = None
        return self.status_text() + "\n\nБаланс: " + ", ".join(balances)

    def status_text(self) -> str:
        spent, count, open_count = self.usage()
        if not self.ab.enabled:
            state = "⏹ выключена в настройках (AUTOBUY_MODE=off)"
        elif self.armed:
            state = "✅ включена"
        else:
            state = "⏸ выключена" + (f" ({esc(self.disarm_reason)})" if self.disarm_reason else "") + " — /autobuy on"
        mode = {"live": "реальные покупки", "dry": "тест, без покупок", "off": "выключено"}[self.ab.mode]
        return "\n".join(
            [
                f"🤖 <b>Автопокупка</b>: {state}",
                f"Режим: {self.ab.mode} ({mode})",
                f"Площадки: {', '.join(self._title(m) for m in self.ab.markets) or '—'}; "
                f"между площадками: {'да' if self.ab.cross else 'нет'}",
                f"За 24 ч: потрачено {ton(spent)} из {ton(self.ab.daily_limit)} TON, "
                f"покупок {count} из {self.ab.max_per_day}",
                f"Непроданных подарков: {open_count} из {self.ab.max_open}",
                f"Порог: прибыль от {ton(self.ab.min_profit_ton)} TON и {self.ab.min_profit_pct * 100:.0f}%, "
                f"лот до {ton(self.ab.max_price)} TON",
            ]
        )

    def positions_text(self) -> str:
        positions = self.store.positions(OPEN_STATUSES)
        if not positions:
            return "Открытых позиций нет."
        lines = ["<b>Открытые позиции</b>"]
        for p in positions:
            line = f"#{p.id} {esc(p.title())} · {self._title(p.buy_market)} · {STATUS_NAMES[p.status]}"
            line += f" · куплено за {ton(p.cost)}"
            if p.list_price:
                line += f", выставлено за {ton(p.list_price)}"
            lines.append(line)
        lines.append("\nЗакрыть позицию, если разобрались вручную: кнопка ниже или /forget номер")
        return "\n".join(lines)

    # ------------------------------------------------------------ панели с кнопками

    def panel(self, note: str = "") -> Reply:
        """Состояние автопокупки с кнопками. note — строка о только что сделанном."""
        text = self.status_text()
        if not self.ab.enabled:
            buttons: tuple[tuple[Button, ...], ...] = ((("🔄 Обновить", "/autobuy"),),)
        else:
            toggle = ("⏹ Выключить", "/autobuy off") if self.armed else ("✅ Включить", "/autobuy ask")
            buttons = ((toggle, ("🔄 Обновить", "/autobuy")), (("📦 Позиции", "+/positions"),))
        return Reply(f"{note}\n\n{text}" if note else text, buttons)

    def confirm_arm(self) -> Reply:
        """Реальные покупки включаются только после второго нажатия."""
        return Reply(
            "⚠️ <b>Включить автопокупку в режиме live?</b>\n"
            f"Бот будет сам покупать подарки за реальные TON: до {ton(self.ab.max_price)} TON за лот, "
            f"до {ton(self.ab.daily_limit)} TON и {self.ab.max_per_day} покупок за сутки, "
            f"не больше {self.ab.max_open} непроданных.",
            ((("✅ Да, включить", "/autobuy on"), ("Отмена", "/autobuy")),),
        )

    def positions_panel(self, note: str = "") -> Reply:
        """Открытые позиции, у каждой кнопка «Закрыть»."""
        rows: list[tuple[Button, ...]] = [
            ((f"🗑 Закрыть #{p.id}", f"/forget ask {p.id}"),) for p in self.store.positions(OPEN_STATUSES)[:20]
        ]
        rows.append((("🔄 Обновить", "/positions"),))
        text = self.positions_text()
        return Reply(f"{note}\n\n{text}" if note else text, tuple(rows))

    def _open_position(self, raw: str) -> tuple[Position | None, str]:
        """Открытая позиция по номеру из команды или причина, почему её нет."""
        raw = raw.lstrip("#")
        if not raw.isdigit():
            return None, "Укажите номер позиции: /forget 12"
        position = self.store.get_position(int(raw))
        if position is None:
            return None, f"Позиция #{raw} не найдена."
        if position.status not in OPEN_STATUSES:
            return None, f"Позиция #{raw} уже закрыта ({STATUS_NAMES.get(position.status, position.status)})."
        return position, ""

    async def on_command(self, command: str, args: list[str]) -> str | Reply | None:
        if command == "/autobuy":
            sub = args[0].lower() if args else ""
            if sub in ("on", "вкл"):
                return Reply(await self.arm(), self.panel().buttons)
            if sub in ("off", "выкл"):
                self.disarm("выключена командой")
                return self.panel("⏹ Автопокупка выключена.")
            if sub == "ask":  # кнопка «Включить»: в live сначала спрашиваем подтверждение
                if self.ab.mode == "live" and not self.armed:
                    return self.confirm_arm()
                if self.ab.mode == "dry" and not self.armed:
                    return Reply(await self.arm(), self.panel().buttons)
            return self.panel()
        if command == "/positions":
            return self.positions_panel()
        if command == "/forget":
            ask = bool(args) and args[0].lower() == "ask"  # кнопка «Закрыть»: сначала подтверждение
            rest = args[1:] if ask else args
            position, problem = self._open_position(rest[0] if rest else "")
            if position is None:
                return self.positions_panel(problem)
            if ask:
                return Reply(
                    f"Закрыть позицию #{position.id} — {esc(position.title())}?\n"
                    "Бот перестанет следить за этим подарком. С самим подарком на площадке ничего не произойдёт.",
                    ((("✅ Да, закрыть", f"/forget {position.id}"), ("Отмена", "/positions")),),
                )
            self.store.update_position(position.id, status="closed", note="закрыта вручную")
            return self.positions_panel(f"Позиция #{position.id} закрыта.")
        return None
