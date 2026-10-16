import asyncio

import pytest

from conftest import FakeAdapter, FakeAuth, FakeBot, lot, make_config
from giftbot.markets.base import AuthError, SchemaError, TransportError
from giftbot.models import ModelFloor
from giftbot.scanner import Scanner, listing_gone

KEY = ("toy bear", "wizard")


def run(coro):
    return asyncio.run(coro)


class World:
    def __init__(self, store, **cfg):
        self.now = [1000.0]
        self.portals = FakeAdapter("portals", "Portals")
        self.tonnel = FakeAdapter("tonnel", "Tonnel")
        self.bot = FakeBot()
        self.auth = FakeAuth()
        self.store = store
        self.scanner = Scanner(
            make_config(**cfg),
            {"portals": self.portals, "tonnel": self.tonnel},
            self.auth,
            self.bot,
            store,
            clock=lambda: self.now[0],
        )

    def portals_book(self, *prices, ids=None):
        ids = ids or [f"p{i}" for i in range(len(prices))]
        self.portals.books[KEY] = [lot("portals", i, p) for i, p in zip(ids, prices)]


@pytest.fixture
def world(store):
    return World(store)


def test_cross_market_deal_is_reported(world):
    world.tonnel.feed = [lot("tonnel", "101", 10.0, number=555)]
    world.portals.floors[KEY] = ModelFloor(14.0)
    world.portals_book(14.0, 14.2, 15.0, 16.0)
    world.tonnel.books[KEY] = [lot("tonnel", "101", 10.0), lot("tonnel", "102", 13.0)]

    run(world.scanner.poll_once("tonnel"))

    [message] = world.bot.sent
    assert "Выгодный лот" in message and "Tonnel → Portals" in message
    assert "#555" in message
    assert "+1.87 TON" in message
    assert world.store.deals_since(3600) == 1

    run(world.scanner.poll_once("tonnel"))  # тот же лот второй раз не присылаем
    assert len(world.bot.sent) == 1


def test_cross_market_deal_skipped_when_listing_already_sold(world):
    world.tonnel.feed = [lot("tonnel", "101", 10.0)]
    world.portals.floors[KEY] = ModelFloor(14.0)
    world.portals_book(14.0, 14.2, 15.0, 16.0)
    world.tonnel.books[KEY] = [lot("tonnel", "102", 13.0)]  # нашего лота уже нет

    run(world.scanner.poll_once("tonnel"))
    assert world.bot.sent == []


def test_cross_deal_for_model_that_had_no_listings_at_cache_refresh(store):
    from conftest import FakeHttp
    from giftbot.markets.tonnel import TonnelAdapter

    stats = {"status": "success", "data": {"Toy Bear_Wizard (1.5%)": {"floorPrice": None, "howMany": 0},
                                           "Toy Bear_Pilot (2%)": {"floorPrice": 5, "howMany": 9}}}
    gift = {"gift_id": 101, "gift_num": 555, "name": "Toy Bear", "model": "Wizard (1.5%)", "price": 10.0,
            "asset": "TON"}
    tonnel = TonnelAdapter(FakeHttp([("POST", "filterStats", stats), ("POST", "pageGifts", [gift])]))
    run(tonnel.refresh())
    world = World(store)
    world.scanner.adapters["tonnel"] = tonnel
    world.portals.floors[KEY] = ModelFloor(16.0)
    world.portals_book(16.0, 16.5, 17.0, 18.0)

    run(world.scanner.poll_once("tonnel"))

    [message] = world.bot.sent  # лот проверен вживую и всё ещё в продаже
    assert "Tonnel → Portals" in message


def test_same_market_snipe(world):
    world.portals.feed = [lot("portals", "a", 8.0)]
    world.portals.floors[KEY] = ModelFloor(12.0)
    world.portals_book(8.0, 12.0, 12.5, 13.0, ids=["a", "b", "c", "d"])

    run(world.scanner.poll_once("portals"))

    [message] = world.bot.sent
    assert "внутри Portals" in message
    assert "Перенос" not in message
    assert "самый дешёвый сейчас 12.00, лотов: 3" in message


def test_same_market_snipe_when_cached_floor_is_the_listing_itself(world):
    world.portals.feed = [lot("portals", "a", 8.0)]
    world.portals.floors[KEY] = ModelFloor(8.0)  # кэш загрузили уже после появления лота
    world.portals_book(8.0, 12.0, 12.5, 13.0, ids=["a", "b", "c", "d"])

    run(world.scanner.poll_once("portals"))

    [message] = world.bot.sent
    assert "внутри Portals" in message
    assert "самый дешёвый сейчас 12.00, лотов: 3" in message


def test_own_floor_with_too_few_listings_is_not_checked_live(world):
    world.tonnel.feed = [lot("tonnel", "101", 8.0)]
    world.tonnel.floors[KEY] = ModelFloor(8.0, count=3)  # сам лот + 2 конкурента — меньше MIN_REF_LISTINGS

    run(world.scanner.poll_once("tonnel"))
    assert "cheapest" not in world.tonnel.calls
    assert world.bot.sent == []


def test_too_few_competitors_is_not_a_deal(world):
    world.portals.feed = [lot("portals", "a", 8.0)]
    world.portals.floors[KEY] = ModelFloor(12.0)
    world.portals_book(8.0, 12.0, 12.5, ids=["a", "b", "c"])

    run(world.scanner.poll_once("portals"))
    assert world.bot.sent == []


def test_stale_floor_is_rechecked_live(world):
    world.portals.feed = [lot("portals", "a", 8.0)]
    world.portals.floors[KEY] = ModelFloor(12.0)  # в кэше выгодно…
    world.portals_book(8.0, 8.2, 8.3, 8.5, ids=["a", "b", "c", "d"])  # …а вживую нет

    run(world.scanner.poll_once("portals"))
    assert world.bot.sent == []


def test_suspiciously_cheap_listing_is_flagged(world):
    world.tonnel.feed = [lot("tonnel", "101", 2.0)]
    world.portals.floors[KEY] = ModelFloor(14.0)
    world.portals_book(14.0, 14.2, 15.0, 16.0)
    world.tonnel.books[KEY] = [lot("tonnel", "101", 2.0)]

    run(world.scanner.poll_once("tonnel"))
    [message] = world.bot.sent
    assert "Подозрительно дешёвый лот" in message
    assert "проверьте лот вручную" in message
    assert world.store.deals_since(3600) == 0  # в статистику сделок не идёт


def test_price_outside_range_is_ignored(world):
    world.tonnel.feed = [lot("tonnel", "101", 80.0)]
    world.portals.floors[KEY] = ModelFloor(140.0)
    run(world.scanner.poll_once("tonnel"))
    assert "model_floor" not in world.portals.calls


def test_schema_error_stops_market_at_once(world):
    world.portals.errors = [SchemaError("portals", "нет поля results")]
    run(world.scanner.poll_once("portals"))

    assert world.scanner.health["portals"].tripped
    [alert] = world.bot.sent
    assert "Portals: сканирование остановлено" in alert
    assert "формат ответа изменился" in alert

    calls = len(world.portals.calls)
    run(world.scanner.poll_once("portals"))  # до пробного запроса площадку не трогаем
    assert len(world.portals.calls) == calls


def test_transport_errors_trip_after_limit(world):
    world.tonnel.errors = [TransportError("tonnel", "timeout")] * 3
    for _ in range(3):
        run(world.scanner.poll_once("tonnel"))
    assert world.scanner.health["tonnel"].tripped
    assert len(world.bot.sent) == 1


def test_auth_error_refreshes_and_retries(world):
    world.portals.errors = [AuthError("portals", "HTTP 401")]
    run(world.scanner.poll_once("portals"))
    assert world.portals.calls == ["latest", "latest"]
    assert world.portals.auth_seen[-1] == "portals-auth-1"
    assert world.scanner.health["portals"].consecutive == 0
    assert world.bot.sent == []


def test_probe_revives_market(world):
    world.portals.errors = [SchemaError("portals", "x")]
    run(world.scanner.poll_once("portals"))
    world.now[0] += 301
    run(world.scanner.poll_once("portals"))
    assert not world.scanner.health["portals"].tripped
    assert "снова отвечает, сканирование возобновлено" in world.bot.sent[-1]


def test_probe_waits_for_resume_when_auto_resume_off(store):
    world = World(store, auto_resume=False)
    world.portals.errors = [SchemaError("portals", "x")]
    run(world.scanner.poll_once("portals"))
    world.now[0] += 301
    run(world.scanner.poll_once("portals"))
    assert world.scanner.health["portals"].tripped
    assert "/resume" in world.bot.sent[-1]
    reply = run(world.scanner.on_command("/resume", []))
    assert "Снова включены: Portals" in reply
    assert not world.scanner.health["portals"].tripped


def test_pause_and_status_commands(world):
    assert "паузе" in run(world.scanner.on_command("/pause", []))
    run(world.scanner.poll_once("portals"))
    assert world.portals.calls == []
    assert "На паузе" in world.scanner.status_text()
    run(world.scanner.on_command("/resume", []))
    run(world.scanner.poll_once("portals"))
    assert world.portals.calls == ["latest"]
    assert "Неизвестная команда" in run(world.scanner.on_command("/buy", []))
    assert "Tonnel: покупка +10.0%" in run(world.scanner.on_command("/settings", []))


def test_names_are_escaped_in_messages(world):
    world.portals.feed = [lot("portals", "a", 8.0, collection="<b>Bad</b>")]
    key = ("<b>bad</b>", "wizard")
    world.portals.floors[key] = ModelFloor(12.0)
    world.portals.books[key] = [lot("portals", i, p, collection="<b>Bad</b>") for i, p in
                                [("a", 8.0), ("b", 12.0), ("c", 12.5), ("d", 13.0)]]
    run(world.scanner.poll_once("portals"))
    assert "&lt;b&gt;Bad&lt;/b&gt;" in world.bot.sent[0]


def test_start_marks_existing_listings_as_seen(world):
    world.tonnel.feed = [lot("tonnel", "101", 10.0)]
    world.portals.floors[KEY] = ModelFloor(14.0)
    world.portals_book(14.0, 14.2, 15.0, 16.0)
    world.tonnel.books[KEY] = [lot("tonnel", "101", 10.0)]
    run(world.scanner.start())
    assert "Сканер запущен" in world.bot.sent[0]
    run(world.scanner.poll_once("tonnel"))
    assert len(world.bot.sent) == 1  # старые лоты не присылаем


def test_listing_gone():
    me = lot("tonnel", "1", 10.0)
    assert not listing_gone(me, [me], 6)
    assert listing_gone(me, [lot("tonnel", "2", 11.0)], 6)  # выдача неполная, лота нет
    full = [lot("tonnel", str(i), 9.0 + i) for i in range(2, 8)]
    assert listing_gone(me, full, 6)  # дешевле последнего в выдаче, но лота нет
    pricey = [lot("tonnel", str(i), 5.0) for i in range(2, 8)]
    assert not listing_gone(me, pricey, 6)  # не помещается в выдачу — неизвестно


def test_run_loop_starts_polls_and_stops_cleanly(store):
    world = World(store, poll_interval=0.01)
    world.scanner.clock = __import__("time").time
    world.tonnel.floors[KEY] = ModelFloor(10.0)

    async def scenario():
        task = asyncio.create_task(world.scanner.run())
        await asyncio.sleep(0.05)
        world.tonnel.feed = [lot("tonnel", "101", 10.0)]
        world.portals.floors[KEY] = ModelFloor(14.0)
        world.portals_book(14.0, 14.2, 15.0, 16.0)
        world.tonnel.books[KEY] = [lot("tonnel", "101", 10.0)]
        await asyncio.sleep(0.1)
        world.scanner.stop()
        await asyncio.wait_for(task, 2)

    run(scenario())
    assert "Сканер запущен" in world.bot.sent[0]
    assert any("Выгодный лот" in m for m in world.bot.sent)
    assert world.bot.sent[-1] == "⏹ Сканер остановлен."
    assert world.tonnel.calls.count("latest") >= 3
