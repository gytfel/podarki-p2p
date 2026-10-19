"""Сканер: опрашивает площадки, ищет выгодные лоты и присылает уведомления.

Порядок проверки нового лота:
1. Цена в заданном диапазоне и известна модель подарка.
2. Быстрая оценка по кэшу флоров: есть ли смысл проверять дальше.
3. Проверка вживую: запрос самых дешёвых лотов этой модели на площадке продажи.
4. Для межплощадочной сделки — проверка, что лот ещё не купили.
5. Уведомление. Покупает бот только при включённой автопокупке (giftbot/trader.py).
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from .config import Config
from .evaluator import Deal, evaluate, is_anomaly, passes
from .guard import MarketHealth, hint_for
from .markets.base import AuthError, MarketAdapter, MarketError
from .models import Listing, ModelFloor
from .store import Store
from .ui import Button, Reply

log = logging.getLogger(__name__)

HELP = (
    "<b>Команды</b> — кнопками в меню под полем ввода или текстом:\n"
    "📊 /status — состояние площадок и число найденных сделок\n"
    "⏸ /pause — поставить сканирование на паузу\n"
    "▶️ /resume — продолжить и снова включить остановленные площадки\n"
    "⚙️ /settings — текущие пороги и комиссии\n\n"
    "<b>Автопокупка</b>\n"
    "🤖 /autobuy — состояние, лимиты, кнопки «Включить» и «Выключить»\n"
    "/autobuy on — включить, /autobuy off — выключить\n"
    "📦 /positions — купленные и ещё не проданные подарки, кнопка «Закрыть» у каждого\n"
    "/forget номер — закрыть позицию, если разобрались вручную"
)


class Notifier(Protocol):
    async def send(
        self, text: str, chat_id: int | None = None, *, buttons: Sequence[Sequence[Button]] = (), menu: bool = False
    ) -> bool: ...

    async def poll_commands(self, handler: Callable[[str, list[str]], Awaitable[str | Reply | None]]) -> None: ...


class AuthSource(Protocol):
    async def get(self, market: str) -> str: ...


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def ton(value: float) -> str:
    return f"{value:.2f}"


def ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин"
    return f"{seconds / 3600:.1f} ч"


def listing_gone(listing: Listing, cheapest: list[Listing], depth: int) -> bool:
    """По выдаче «самые дешёвые лоты модели» понимаем, что лот уже купили или сняли."""
    if any(item.listing_id == listing.listing_id for item in cheapest):
        return False
    if len(cheapest) < depth:
        return True  # выдача полная, а лота в ней нет
    return listing.price < cheapest[-1].price  # лот дешевле последнего в выдаче, но его нет


class Scanner:
    def __init__(
        self,
        cfg: Config,
        adapters: dict[str, MarketAdapter],
        auth: AuthSource,
        bot: Notifier,
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.adapters = adapters
        self.auth = auth
        self.bot = bot
        self.store = store
        self.clock = clock
        self.health = {m: MarketHealth(cfg.max_errors) for m in adapters}
        self.new_listings = {m: 0 for m in adapters}
        self.paused = False
        self.started_at = clock()
        self.trader = None  # автопокупка (giftbot.trader.Trader), подключается в __main__
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ вызовы площадок

    async def call(self, market: str, func: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """Вызов API площадки: при ошибке входа обновляет его, считает ошибки, включает аварийный стоп."""
        health = self.health[market]
        try:
            try:
                result = await func(*args)
            except AuthError as first:
                log.info("%s: %s — обновляю вход и повторяю запрос", market, first)
                if not await self.refresh_auth(market):
                    raise
                result = await func(*args)
        except MarketError as error:
            log.warning("%s: %s", market, error.human())
            if health.fail(error, self.clock()):
                await self._notify_trip(market, error)
            raise
        health.ok(self.clock())
        return result

    async def refresh_auth(self, market: str) -> bool:
        try:
            self.adapters[market].set_auth(await self.auth.get(market))
            return True
        except Exception as exc:
            log.warning("%s: не удалось обновить вход: %s", market, exc)
            return False

    # ------------------------------------------------------------ опрос

    async def poll_once(self, market: str) -> None:
        if self.paused:
            return
        if self.health[market].tripped:
            await self._probe(market)
            return
        adapter = self.adapters[market]
        try:
            listings = await self.call(market, adapter.latest, self.cfg.latest_limit)
        except MarketError:
            return
        new = [item for item in listings if self.store.is_new(item)]
        self.new_listings[market] += len(new)
        for listing in new:
            if self._stop.is_set() or self.paused or self.health[market].tripped:
                break
            try:
                await self.handle_listing(listing)
            except MarketError:
                pass  # уже учтено в call()
            except Exception:
                log.exception("Ошибка при разборе лота %s", listing)

    async def _probe(self, market: str) -> None:
        """Пробный запрос к выключенной площадке: не ожила ли."""
        health = self.health[market]
        now = self.clock()
        # после каждой неудачной пробы ждём вдвое дольше (5, 10, 20, 40 мин…, не больше часа):
        # частые пробы продлевают блокировку Cloudflare
        interval = min(self.cfg.probe_interval * 2**health.failed_probes, max(self.cfg.probe_interval, 3600.0))
        if health.waiting_resume or now - health.last_probe < interval:
            return
        health.last_probe = now
        adapter = self.adapters[market]
        try:
            if health.last_error is None or isinstance(health.last_error, AuthError):
                await self.refresh_auth(market)  # вход обновляем, только если площадка жаловалась на него
            await adapter.latest(1)
            await adapter.refresh()
        except MarketError as error:
            health.last_error = error
            health.failed_probes += 1
            log.info("%s: пробный запрос не прошёл: %s", market, error.human())
            return
        if self.cfg.auto_resume:
            health.reset()
            health.ok(now)
            await self.bot.send(f"✅ <b>{adapter.title}</b> снова отвечает, сканирование возобновлено.")
        else:
            health.waiting_resume = True
            await self.bot.send(
                f"✅ <b>{adapter.title}</b> снова отвечает. Отправьте /resume, чтобы продолжить.",
                buttons=((("▶️ Продолжить", "+/resume"),),),
            )

    # ------------------------------------------------------------ оценка лота

    async def handle_listing(self, listing: Listing) -> Deal | None:
        cfg, th = self.cfg, self.cfg.thresholds
        if not cfg.min_buy_price <= listing.price <= cfg.max_buy_price:
            return None
        key = listing.key
        if key is None:
            return None

        # 1. Быстрая оценка по кэшу флоров (пороги ослаблены вдвое: кэш мог устареть)
        candidates: list[Deal] = []
        for market, adapter in self.adapters.items():
            if self.health[market].tripped:
                continue
            try:
                floor = await self.call(market, adapter.model_floor, key)
            except MarketError:
                continue
            if floor is None:
                continue
            estimate = evaluate(listing, market, floor.price, floor.count, cfg.fees, th)
            if (
                passes(estimate, th, slack=0.5, check_liquidity=False)
                or is_anomaly(listing.price, floor.price, th)
                or self._is_own_floor(listing, market, floor)
            ):
                candidates.append(estimate)

        # 2. Проверка вживую по самым дешёвым лотам модели
        candidates.sort(key=lambda d: d.profit, reverse=True)
        for estimate in candidates[:2]:
            if self.health[estimate.sell_market].tripped:
                continue
            deal = await self._verify(listing, estimate.sell_market)
            if deal is None:
                continue
            anomaly = is_anomaly(listing.price, deal.ref_price, th)
            if not anomaly and not passes(deal, th):
                log.debug("%s: после проверки невыгодно (%.2f TON)", listing.title(), deal.profit)
                continue
            if deal.cross and await self._still_listed(listing) is False:
                log.info("%s: лот уже купили или сняли", listing.title())
                return None
            await self._report(deal, anomaly)
            if self.trader is not None:
                await self.trader.consider(deal, anomaly)
            return deal
        return None

    def _is_own_floor(self, listing: Listing, market: str, floor: ModelFloor) -> bool:
        """Флор своей площадки и есть этот лот: кэш обновился уже после его появления (Portals грузит
        модели при первом лоте коллекции). Цену следующего конкурента по кэшу не узнать — нужна проверка вживую."""
        if market != listing.market or abs(floor.price - listing.price) > 0.005:
            return False
        # площадка сообщила, сколько лотов модели в продаже: без лота-кандидата конкурентов слишком мало
        return floor.count is None or floor.count > self.cfg.thresholds.min_ref_listings

    async def _cheapest(self, market: str, listing: Listing) -> list[Listing] | None:
        try:
            return await self.call(market, self.adapters[market].cheapest, listing.key, self.cfg.verify_depth)
        except MarketError:
            return None

    async def _verify(self, listing: Listing, market: str) -> Deal | None:
        items = await self._cheapest(market, listing)
        if items is None:
            return None
        if market == listing.market and listing_gone(listing, items, self.cfg.verify_depth):
            log.info("%s: лот уже купили или сняли", listing.title())
            return None
        others = [i for i in items if not (i.market == listing.market and i.listing_id == listing.listing_id)]
        if not others:
            return None  # конкурентов нет — цену перепродажи не на что опереть
        return evaluate(listing, market, others[0].price, len(others), self.cfg.fees, self.cfg.thresholds)

    async def _still_listed(self, listing: Listing) -> bool | None:
        if self.health[listing.market].tripped:
            return None
        items = await self._cheapest(listing.market, listing)
        if items is None:
            return None
        return not listing_gone(listing, items, self.cfg.verify_depth)

    # ------------------------------------------------------------ уведомления

    async def _report(self, deal: Deal, anomaly: bool) -> None:
        self.store.add_deal(deal, anomaly)
        log.info(
            "%s: %s %s -> %s, цена %.2f, прибыль %.2f TON (%.1f%%)",
            "АНОМАЛИЯ" if anomaly else "СДЕЛКА",
            deal.listing.title(),
            deal.listing.market,
            deal.sell_market,
            deal.listing.price,
            deal.profit,
            deal.profit_pct * 100,
        )
        await self.bot.send(self.format_deal(deal, anomaly))

    def format_deal(self, deal: Deal, anomaly: bool) -> str:
        item = deal.listing
        buy_title = self.adapters[item.market].title
        sell_title = self.adapters[deal.sell_market].title
        route = f"{buy_title} → {sell_title}" if deal.cross else f"внутри {buy_title}"
        head = "⚠️ <b>Подозрительно дешёвый лот</b>" if anomaly else "💎 <b>Выгодный лот</b>"

        name = f"<b>{esc(item.collection)}</b>"
        if item.model:
            name += f" — {esc(item.model)}"
        if item.number is not None:
            name += f" · #{item.number}"
        lines = [f"{head} · {route}", name]
        extras = [f"фон: {esc(item.backdrop)}"] if item.backdrop else []
        if item.symbol:
            extras.append(f"узор: {esc(item.symbol)}")
        if extras:
            joined = ", ".join(extras)
            lines.append(joined[0].upper() + joined[1:])

        fee_note = f" (с комиссией {ton(deal.cost)})" if abs(deal.cost - item.price) > 0.005 else ""
        lines += [
            "",
            f"Купить на {buy_title}: {ton(item.price)} TON{fee_note}",
            f"Выставить на {sell_title}: ~{ton(deal.resale_price)} TON "
            f"(самый дешёвый «{esc(item.model)}» сейчас {ton(deal.ref_price)}, "
            f"проверено лотов этой модели: {deal.ref_count})",
            f"На руки после комиссии: {ton(deal.proceeds)} TON",
        ]
        if deal.transfer_cost:
            lines.append(f"Перенос между площадками: −{ton(deal.transfer_cost)} TON")
        lines += ["", f"<b>Прибыль: {deal.profit:+.2f} TON ({deal.profit_pct * 100:+.1f}%)</b>"]

        notes = [
            "Цена перепродажи — по выставленным лотам этой модели, а не по реальным продажам: "
            "проверьте в приложении, по чём её покупают."
        ]
        if anomaly:
            notes.append(
                f"⚠️ Цена в {deal.ref_price / item.price:.1f} раза ниже рынка. Это может быть ошибка данных "
                "или подделка — проверьте лот вручную."
            )
        if item.locked_until:
            notes.append(f"🔒 Вывести подарок можно только после {item.locked_until:%d.%m %H:%M} UTC.")
        if deal.cross:
            notes.append("Перед покупкой проверьте, что подарок можно сразу вывести с площадки.")
        if self.trader is not None and self.trader.armed:
            notes.append("Автопокупка включена: если лот подходит под её лимиты, бот купит его сам.")
        else:
            notes.append("Лот могли уже купить: бот только сообщает, решение за вами.")
        return "\n".join(lines) + "\n\n" + "\n".join(notes)

    async def _notify_trip(self, market: str, error: MarketError) -> None:
        title = self.adapters[market].title
        resume = (
            "Сканирование продолжится само, когда площадка снова ответит."
            if self.cfg.auto_resume
            else "Когда площадка снова ответит, бот напишет — продолжить можно командой /resume."
        )
        autobuy = ""
        buttons: list[tuple[Button, ...]] = [(("▶️ Включить сейчас", "+/resume"), ("📊 Статус", "+/status"))]
        if self.trader is not None and self.trader.disarm(f"{title}: сканирование остановлено"):
            autobuy = "\n\n🛑 Автопокупка выключена. Включить снова: /autobuy on"
            buttons.append((("🤖 Автопокупка", "+/autobuy"),))
        await self.bot.send(
            f"⛔ <b>{title}: сканирование остановлено</b>\n"
            f"Причина: {esc(error.kind)} — {esc(error)}\n"
            f"Ошибок подряд: {self.health[market].consecutive}\n\n"
            f"{esc(hint_for(error))}\n"
            f"Бот проверяет площадку раз в {ago(self.cfg.probe_interval)}. {resume}{autobuy}",
            buttons=tuple(buttons),
        )

    # ------------------------------------------------------------ команды

    async def on_command(self, command: str, args: list[str]) -> str | Reply | None:
        if command in ("/start", "/help"):
            return HELP
        if command == "/status":
            return self.status_panel()
        if command == "/pause":
            self.paused = True
            return self.status_panel("⏸ Сканирование на паузе.")
        if command == "/resume":
            self.paused = False
            revived = [self.adapters[m].title for m, h in self.health.items() if h.tripped]
            for health in self.health.values():
                health.reset()
            text = "▶️ Сканирование продолжено."
            if revived:
                text += " Снова включены: " + ", ".join(revived) + "."
            return self.status_panel(text)
        if command == "/settings":
            return self.settings_text()
        if self.trader is not None:
            reply = await self.trader.on_command(command, args)
            if reply is not None:
                return reply
        return "Неизвестная команда. Список команд: /help"

    def status_panel(self, note: str = "") -> Reply:
        """Статус с кнопками «Обновить» и «Пауза»/«Продолжить». note — строка о только что сделанном."""
        stopped = self.paused or any(h.tripped for h in self.health.values())
        refresh: Button = ("🔄 Обновить", "/status")
        # «Пауза» и «Продолжить» на разных местах: повторное нажатие попадёт в «Обновить», а не отменит действие
        first_row = (("▶️ Продолжить", "/resume"), refresh) if stopped else (refresh, ("⏸ Пауза", "/pause"))
        rows: list[tuple[Button, ...]] = [first_row]
        if self.trader is not None and self.cfg.autobuy.enabled:
            rows.append((("🤖 Автопокупка", "+/autobuy"), ("📦 Позиции", "+/positions")))
        text = self.status_text()
        return Reply(f"{note}\n\n{text}" if note else text, tuple(rows))

    def status_text(self) -> str:
        now = self.clock()
        lines = ["⏸ <b>На паузе</b>" if self.paused else "▶️ <b>Работает</b>", ""]
        for market, adapter in self.adapters.items():
            health = self.health[market]
            if health.tripped:
                state = "⛔ остановлена"
                if health.last_error:
                    state += f" ({esc(health.last_error.kind)})"
                if health.waiting_resume:
                    state += ", снова отвечает — /resume"
            elif health.consecutive:
                state = f"⚠️ ошибок подряд: {health.consecutive}"
            else:
                state = "✅ в порядке"
            last = f"ответ {ago(now - health.last_ok)} назад" if health.last_ok else "ещё не отвечала"
            lines.append(f"<b>{adapter.title}</b>: {state} · {last} · новых лотов: {self.new_listings[market]}")
        lines += [
            "",
            f"Сделок за 24 ч: {self.store.deals_since(86400)}",
            f"Работает {ago(now - self.started_at)}",
        ]
        if self.trader is not None and self.cfg.autobuy.enabled:
            lines += ["", self.trader.status_text()]
        return "\n".join(lines)

    def settings_text(self) -> str:
        th = self.cfg.thresholds
        lines = [
            "<b>Пороги</b>",
            f"Цена лота: {ton(self.cfg.min_buy_price)}–{ton(self.cfg.max_buy_price)} TON",
            f"Прибыль: от {ton(th.min_profit_ton)} TON и от {th.min_profit_pct * 100:.1f}%",
            f"Выставлять дешевле самого дешёвого на {th.undercut * 100:.1f}%",
            f"Минимум конкурентов для оценки цены: {th.min_ref_listings}",
            f"Перенос между площадками: {ton(th.transfer_cost_ton)} TON",
            f"Подозрительно: дешевле {th.anomaly_ratio * 100:.0f}% от рынка",
            "",
            "<b>Комиссии</b>",
        ]
        for market, adapter in self.adapters.items():
            fees = self.cfg.fees[market]
            lines.append(f"{adapter.title}: покупка +{fees.buy * 100:.1f}%, продажа −{fees.sell * 100:.1f}%")
        return "\n".join(lines)

    # ------------------------------------------------------------ запуск

    async def start(self) -> None:
        problems: list[str] = []
        for market, adapter in self.adapters.items():
            if not await self.refresh_auth(market):
                problems.append(f"{adapter.title}: не удалось получить вход, подробности в логе")
        for market, adapter in self.adapters.items():
            try:
                await self.call(market, adapter.refresh)
            except MarketError as error:
                problems.append(f"{adapter.title}: не загрузились флоры ({esc(error.kind)})")
        # Уже выставленные лоты считаем увиденными, чтобы не завалить уведомлениями при старте
        for market, adapter in self.adapters.items():
            try:
                self.store.mark_seen(await self.call(market, adapter.latest, self.cfg.latest_limit))
            except MarketError:
                pass
        th = self.cfg.thresholds
        text = (
            "✅ <b>Сканер запущен</b>\n"
            f"Площадки: {', '.join(a.title for a in self.adapters.values())}\n"
            f"Ищу прибыль от {ton(th.min_profit_ton)} TON и {th.min_profit_pct * 100:.0f}%, "
            f"цена лота {ton(self.cfg.min_buy_price)}–{ton(self.cfg.max_buy_price)} TON.\n"
            "Команды — кнопками в меню под полем ввода, /help — список"
        )
        if self.trader is not None and self.cfg.autobuy.enabled:
            mode = "ТЕСТОВЫЙ режим (dry)" if self.cfg.autobuy.mode == "dry" else "режим live — РЕАЛЬНЫЕ покупки"
            text += f"\n\n🤖 Автопокупка: {mode}. Сейчас выключена, включить: /autobuy on"
        if problems:
            text += "\n\n⚠️ " + "\n⚠️ ".join(problems)
        await self.bot.send(text, menu=True)
        if self.trader is not None:
            await self.trader.recover()

    async def run(self) -> None:
        await self.start()
        tasks = [asyncio.create_task(self._poll_loop(m), name=f"poll:{m}") for m in self.adapters]
        tasks += [
            asyncio.create_task(self._periodic(self.cfg.floors_refresh, self.refresh_floors), name="floors"),
            asyncio.create_task(self._periodic(self.cfg.auth_refresh, self.refresh_all_auth), name="auth"),
            asyncio.create_task(self._periodic(3600, self._housekeeping), name="housekeeping"),
            asyncio.create_task(self.bot.poll_commands(self.on_command), name="commands"),
        ]
        if self.trader is not None and self.cfg.autobuy.enabled:
            tasks.append(
                asyncio.create_task(
                    self._periodic(self.cfg.autobuy.positions_check, self.trader.check_positions), name="positions"
                )
            )
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.bot.send("⏹ Сканер остановлен.")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def _poll_loop(self, market: str) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once(market)
            except Exception:
                log.exception("Сбой в цикле опроса %s", market)
            await self._sleep(self.cfg.poll_interval_for(market) + random.uniform(0, self.cfg.poll_jitter))

    async def _periodic(self, interval: float, func: Callable[[], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            await self._sleep(interval)
            if self._stop.is_set():
                break
            try:
                await func()
            except Exception:
                log.exception("Сбой периодической задачи %s", getattr(func, "__name__", func))

    async def refresh_floors(self) -> None:
        for market, adapter in self.adapters.items():
            if self.health[market].tripped or self.paused:
                continue
            try:
                await self.call(market, adapter.refresh)
            except MarketError:
                pass

    async def refresh_all_auth(self) -> None:
        for market in self.adapters:
            if not self.health[market].tripped:
                await self.refresh_auth(market)

    async def _housekeeping(self) -> None:
        self.store.prune()
