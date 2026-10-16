"""Сценарии автопокупки на площадках-заглушках."""
import asyncio

import pytest

from conftest import FakeAdapter, FakeAuth, FakeBot, lot, make_autobuy, make_config
from giftbot.markets.base import BuyRejected, SchemaError, TransportError
from giftbot.models import ModelFloor, OwnedGift
from giftbot.scanner import Scanner
from giftbot.store import Store
from giftbot.trader import Trader

KEY = ("toy bear", "wizard")


def run(coro):
    return asyncio.run(coro)


class World:
    def __init__(self, store, **ab):
        self.now = [1000.0]
        self.portals = FakeAdapter("portals", "Portals")
        self.tonnel = FakeAdapter("tonnel", "Tonnel")
        self.bot = FakeBot()
        self.store = store
        cfg = make_config(autobuy=make_autobuy(**ab))
        self.scanner = Scanner(
            cfg, {"portals": self.portals, "tonnel": self.tonnel}, FakeAuth(), self.bot, store,
            clock=lambda: self.now[0],
        )

        async def no_sleep(_):
            pass

        self.trader = Trader(cfg, self.scanner, sleep=no_sleep, confirm_attempts=2, confirm_delay=0)
        self.scanner.trader = self.trader

    def arm(self) -> str:
        return run(self.scanner.on_command("/autobuy", ["on"]))

    def portals_snipe(self, lid="a", price=8.0, number=101):
        """Лот на Portals заметно дешевле трёх конкурентов той же модели."""
        self.portals.feed = [lot("portals", lid, price, number=number)]
        self.portals.floors[KEY] = ModelFloor(12.0)
        book = [lot("portals", lid, price, number=number)]
        book += [lot("portals", f"{lid}-c{i}", p, number=900 + i) for i, p in enumerate([12.0, 12.5, 13.0])]
        self.portals.books[KEY] = book

    def poll(self, market="portals"):
        run(self.scanner.poll_once(market))

    def text(self) -> str:
        return "\n---\n".join(self.bot.sent)


@pytest.fixture
def world(store):
    return World(store)


def test_nothing_is_bought_until_armed(world):
    world.portals_snipe()
    world.poll()
    assert "buy" not in world.portals.calls
    assert "Выгодный лот" in world.text()


def test_full_purchase_and_listing(world):
    reply = world.arm()
    assert "Автопокупка</b>: ✅ включена" in reply and "Portals: 100.00 TON" in reply

    world.portals_snipe()
    world.poll()

    assert world.portals.calls.count("buy") == 1
    assert world.portals.listed == [("own-a", 11.88)]  # на 1% дешевле конкурента за 12
    message = world.bot.sent[-1]
    assert "Куплено #1" in message and "Выставлено за 11.88 TON" in message
    [position] = world.store.positions(["listed"])
    assert position.list_price == 11.88 and position.expected_profit == pytest.approx(11.88 * 0.95 - 8)


def test_mode_off_cannot_be_armed(store):
    world = World(store, mode="off")
    assert "AUTOBUY_MODE=off" in world.arm()
    world.portals_snipe()
    world.poll()
    assert "buy" not in world.portals.calls


def test_dry_mode_simulates_and_counts_limits(store):
    world = World(store, mode="dry", max_per_day=1)
    world.arm()
    world.portals_snipe("a")
    world.poll()
    assert "buy" not in world.portals.calls
    assert "Тест #1" in world.bot.sent[-1]
    world.portals_snipe("b", number=102)
    world.poll()
    assert "лимит 1 покупок за сутки" in world.bot.sent[-1]


def test_rejected_purchase_keeps_autobuy_on(world):
    world.arm()
    world.portals.buy_errors = [BuyRejected("portals", "HTTP 400: already sold")]
    world.portals_snipe()
    world.poll()
    assert "Не купилось #1" in world.bot.sent[-1] and "Деньги не списаны" in world.bot.sent[-1]
    assert world.store.positions(["failed"])
    assert world.trader.armed


def test_network_failure_is_never_retried_and_checked_in_inventory(world):
    world.arm()
    world.portals.buy_errors = [TransportError("portals", "timeout")]
    world.portals_snipe()
    world.poll()
    assert world.portals.calls.count("buy") == 1  # никаких повторных покупок
    assert "был сбой связи, но подарок в инвентаре" in world.bot.sent[-1]
    assert world.store.positions(["listed"])
    assert world.trader.armed


def test_unconfirmed_purchase_disarms(world):
    world.arm()
    world.portals.buy_errors = [TransportError("portals", "timeout")]
    world.portals.buy_lands = False
    world.portals_snipe()
    world.poll()
    assert world.portals.calls.count("buy") == 1
    assert "неизвестно, прошла ли она" in world.bot.sent[-1]
    assert world.store.positions(["unknown"])
    assert not world.trader.armed

    world.portals_snipe("b", number=102)  # дальше ничего не покупаем
    world.poll()
    assert world.portals.calls.count("buy") == 1


def test_success_without_gift_in_inventory_disarms(world):
    world.arm()
    world.portals.buy_lands = False
    world.portals_snipe()
    world.poll()
    assert "подарка нет в инвентаре" in world.bot.sent[-1]
    assert not world.trader.armed


@pytest.mark.parametrize(
    "limits,setup,expected",
    [
        ({"max_price": 7}, None, None),
        ({"daily_limit": 10}, "second", "потрачено 8.00 из 10.00 TON"),
        ({"max_open": 1}, "second", "уже 1 непроданных подарков"),
        ({"max_per_day": 1}, "second", "лимит 1 покупок за сутки"),
        ({"reserve": 95}, None, "не хватает на лот"),
        ({"min_profit_ton": 5}, None, None),
    ],
)
def test_limits_block_purchases(store, limits, setup, expected):
    world = World(store, **limits)
    world.arm()
    world.portals_snipe("a")
    world.poll()
    if setup == "second":
        assert world.portals.calls.count("buy") == 1
        world.portals_snipe("b", number=102)
        world.poll()
        assert world.portals.calls.count("buy") == 1
    else:
        assert "buy" not in world.portals.calls
    if expected:
        assert expected in world.bot.sent[-1]


def test_cooldown_between_purchases(store):
    world = World(store, cooldown=60)
    world.arm()
    world.portals_snipe("a")
    world.poll()
    world.portals_snipe("b", number=102)
    world.poll()
    assert world.portals.calls.count("buy") == 1
    world.now[0] += 61
    world.portals_snipe("c", number=103)
    world.poll()
    assert world.portals.calls.count("buy") == 2


def test_auto_withdraw_setting_blocks_arming(world):
    world.tonnel.auto_withdraw = True
    reply = world.arm()
    assert "Автопокупка не включена" in reply and "вывод купленного" in reply
    assert not world.trader.armed


def test_suspicious_price_is_never_bought(world):
    world.arm()
    world.portals_snipe(price=2.0)
    world.poll()
    assert "Подозрительно дешёвый лот" in world.bot.sent[-1]
    assert "buy" not in world.portals.calls


def test_cross_market_buy_only_when_allowed(store):
    def setup(world):
        world.tonnel.feed = [lot("tonnel", "101", 10.0, number=555)]
        world.portals.floors[KEY] = ModelFloor(16.0)
        world.portals.books[KEY] = [lot("portals", f"p{i}", p, number=i) for i, p in enumerate([16, 16.5, 17, 18])]
        world.tonnel.books[KEY] = [lot("tonnel", "101", 10.0, number=555)]

    world = World(store)
    world.arm()
    setup(world)
    world.poll("tonnel")
    assert "buy" not in world.tonnel.calls

    world = World(Store(":memory:"), cross=True)
    world.arm()
    setup(world)
    world.poll("tonnel")
    assert world.tonnel.calls.count("buy") == 1
    assert "Перенесите подарок на Portals" in world.bot.sent[-1]
    assert world.tonnel.listed == []  # между площадками бот сам не выставляет


def test_market_failure_disarms_autobuy(world):
    world.arm()
    world.portals.errors = [SchemaError("portals", "нет поля results")]
    world.poll()
    assert not world.trader.armed
    assert "Автопокупка выключена" in world.bot.sent[-1]
    assert "Portals: площадка остановлена" in world.arm()


def test_listing_failure_asks_to_list_manually(world):
    world.arm()
    world.portals.list_errors = [BuyRejected("portals", "limited gift")]
    world.portals_snipe()
    world.poll()
    assert "Выставить не получилось" in world.bot.sent[-1]
    assert world.store.positions(["bought"])


def test_falling_market_lists_without_loss(world):
    world.arm()
    world.portals_snipe()
    original_buy = world.portals.buy

    async def buy_then_market_drops(listing):
        await original_buy(listing)
        world.portals.books[KEY] = [lot("portals", "cheap", 8.5, number=999)] * 3

    world.portals.buy = buy_then_market_drops
    world.poll()
    # без убытка: (8.00 + 0.50 мин. прибыли) / 0.95 = 8.947… → 8.95
    assert world.portals.listed == [("own-a", 8.95)]
    assert "Рынок просел" in world.bot.sent[-1]


def test_sale_detected_after_two_checks(world):
    world.arm()
    world.portals_snipe()
    world.poll()
    world.portals.inventory = []  # подарок купили
    run(world.trader.check_positions())
    assert "Продано" not in world.text()
    run(world.trader.check_positions())
    assert "Продано #1" in world.bot.sent[-1]
    assert world.store.positions(["sold"])


def test_unknown_purchase_confirmed_later(world):
    world.arm()
    world.portals.buy_lands = False
    world.portals_snipe()
    world.poll()
    world.portals.inventory = [OwnedGift("portals", "late", "Toy Bear", "Wizard", 101, None)]
    run(world.trader.check_positions())
    assert "Покупка #1 подтвердилась" in world.bot.sent[-1]
    assert world.store.positions(["bought"])


def test_interrupted_purchase_is_recovered(world):
    from giftbot.evaluator import evaluate

    cfg = world.scanner.cfg
    deal = evaluate(lot("portals", "a", 8.0, number=101), "portals", 12.0, 3, cfg.fees, cfg.thresholds)
    world.store.open_position(deal, "pending")
    run(world.trader.recover())
    assert world.store.positions(["unknown"])
    assert "перезапустился во время покупки #1" in world.bot.sent[-1]


def test_positions_and_forget_commands(world):
    world.arm()
    world.portals_snipe()
    world.poll()
    text = run(world.scanner.on_command("/positions", []))
    assert "#1 Toy Bear — Wizard #101" in text and "выставлено за 11.88" in text
    assert "Позиция #1 закрыта" in run(world.scanner.on_command("/forget", ["#1"]))
    assert "уже закрыта" in run(world.scanner.on_command("/forget", ["1"]))
    assert "Открытых позиций нет" in run(world.scanner.on_command("/positions", []))
    assert "Автопокупка выключена" in run(world.scanner.on_command("/autobuy", ["off"]))
    assert "выключена командой" in run(world.scanner.on_command("/autobuy", []))
