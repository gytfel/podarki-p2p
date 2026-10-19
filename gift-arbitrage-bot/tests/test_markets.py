import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import FakeHttp
from giftbot.markets.base import SchemaError
from giftbot.markets.portals import PortalsAdapter
from giftbot.markets.tonnel import TonnelAdapter

KEY = ("toy bear", "wizard")


def run(coro):
    return asyncio.run(coro)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# ------------------------------------------------------------------ Portals

COLLECTIONS = {
    "collections": [
        {"id": "c1", "name": "Toy Bear", "short_name": "toybear", "floor_price": "9.5"},
        {"id": "c2", "name": "Durov's Cap", "short_name": "durovscap", "floor_price": None},
    ]
}
FILTERS = {"floor_prices": {"toybear": {"models": {"Wizard": "12.5", "Pilot": "10"}, "backdrops": {}, "symbols": {}}}}


def portals_nft(nid, price, model="Wizard", name="Toy Bear", number=7):
    return {
        "id": nid,
        "name": name,
        "collection_id": "c1",
        "external_collection_number": number,
        "price": str(price),
        "status": "listed",
        "attributes": [
            {"type": "model", "value": model, "rarity_per_mille": 15},
            {"type": "backdrop", "value": "Black", "rarity_per_mille": 20},
            {"type": "symbol", "value": "Star", "rarity_per_mille": 5},
        ],
    }


def portals(routes):
    http = FakeHttp(routes)
    adapter = PortalsAdapter(http)
    adapter.set_auth("tma test")
    return adapter, http


def test_portals_latest_parses_listing_and_sends_auth():
    adapter, http = portals([("GET", "nfts/search", {"results": [portals_nft("n1", 10.5)]})])
    [item] = run(adapter.latest(20))
    assert (item.market, item.listing_id, item.collection, item.model) == ("portals", "n1", "Toy Bear", "Wizard")
    assert item.price == 10.5 and item.number == 7 and item.backdrop == "Black"
    params = query(http.calls[0][1])
    assert params["sort_by"] == "listed_at desc"
    assert params["premarket_status"] == "without_premarket"


def test_portals_floors_and_cheapest():
    adapter, http = portals(
        [
            ("GET", "collections/filters", FILTERS),
            ("GET", "collections?", COLLECTIONS),
            ("GET", "nfts/search", {"results": [portals_nft("n2", 13), portals_nft("n1", 12.5)]}),
        ]
    )
    run(adapter.refresh())
    floor = run(adapter.model_floor(KEY))
    assert floor.price == 12.5
    run(adapter.model_floor(KEY))
    assert sum("collections/filters" in c[1] for c in http.calls) == 1  # модели кэшируются

    items = run(adapter.cheapest(KEY, 6))
    assert [i.listing_id for i in items] == ["n1", "n2"]  # отсортировано по цене
    params = query(http.calls[-1][1])
    assert params["collection_ids"] == "c1"
    assert params["filter_by_models"] == "Wizard"
    assert params["sort_by"] == "price asc"

    assert run(adapter.cheapest(("unknown", "x"), 6)) is None
    assert run(adapter.cheapest(("toy bear", "nonexistent"), 6)) is None


def test_portals_model_without_listings_is_still_checked_live():
    filters = {"floor_prices": {"toybear": {"models": {"Wizard": "12.5", "Ghost": "0"}}}}
    adapter, http = portals(
        [
            ("GET", "collections/filters", filters),
            ("GET", "collections?", COLLECTIONS),
            ("GET", "nfts/search", {"results": [portals_nft("n9", 30, model="Ghost")]}),
        ]
    )
    run(adapter.refresh())
    assert run(adapter.model_floor(("toy bear", "ghost"))) is None  # по кэшу лотов нет…
    [item] = run(adapter.cheapest(("toy bear", "ghost"), 6))  # …но площадку спрашиваем вживую
    assert item.listing_id == "n9"
    assert query(http.calls[-1][1])["filter_by_models"] == "Ghost"


def test_portals_broken_model_filter_is_detected():
    adapter, _ = portals(
        [
            ("GET", "collections/filters", FILTERS),
            ("GET", "collections?", COLLECTIONS),
            ("GET", "nfts/search", {"results": [portals_nft("n1", 5, model="Pilot"), portals_nft("n2", 6, "Pilot")]}),
        ]
    )
    run(adapter.refresh())
    with pytest.raises(SchemaError, match="фильтр по модели"):
        run(adapter.cheapest(KEY, 6))


def test_portals_changed_format_raises_schema_error():
    adapter, _ = portals([("GET", "nfts/search", {"items": []})])
    with pytest.raises(SchemaError):
        run(adapter.latest(20))

    bad = [{"id": "x", "price": "abc", "name": "Toy Bear"}] * 3 + [portals_nft("n1", 10)]
    adapter, _ = portals([("GET", "nfts/search", {"results": bad})])
    with pytest.raises(SchemaError, match="не разобрано 3 из 4"):
        run(adapter.latest(20))


def test_portals_tolerates_single_bad_record():
    records = [portals_nft("n1", 10), portals_nft("n2", 11), portals_nft("n3", 12), {"id": "x", "price": "0"}]
    adapter, _ = portals([("GET", "nfts/search", {"results": records})])
    assert len(run(adapter.latest(20))) == 3


# ------------------------------------------------------------------ Tonnel

STATS = {
    "status": "success",
    "data": {
        "Toy Bear_Wizard (1.5%)": {"floorPrice": 11, "howMany": 7},
        "Toy Bear_Pilot (2%)": {"floorPrice": None, "howMany": 0},
        "Durov's Cap_Royal (0.5%)": {"floorPrice": 450.5, "howMany": 2},
    },
}


def tonnel_gift(gid, price, model="Wizard (1.5%)", **extra):
    gift = {
        "gift_id": gid,
        "gift_num": 555,
        "name": "Toy Bear",
        "model": model,
        "backdrop": "Black (2%)",
        "symbol": "Star (0.5%)",
        "price": price,
        "asset": "TON",
    }
    gift.update(extra)
    return gift


def tonnel(routes):
    http = FakeHttp(routes)
    adapter = TonnelAdapter(http)
    adapter.set_auth("init-data")
    return adapter, http


def test_tonnel_latest_skips_bundles_and_other_assets():
    feed = [tonnel_gift(101, 10.5), tonnel_gift(-5, 3), tonnel_gift(102, 1, asset="USDT")]
    adapter, http = tonnel([("POST", "pageGifts", feed)])
    [item] = run(adapter.latest(50))
    assert (item.listing_id, item.model, item.backdrop, item.number) == ("101", "Wizard", "Black", 555)
    payload = http.calls[0][2]
    assert payload["limit"] == 30  # потолок Tonnel
    assert payload["user_auth"] == "init-data"
    assert json.loads(payload["filter"])["asset"] == "TON"


def test_tonnel_floors_and_cheapest():
    def page(url, body):
        if json.loads(body["filter"])["model"] == "Pilot (2%)":
            return [tonnel_gift(201, 9, model="Pilot (2%)")]  # выставили после обновления кэша
        return [tonnel_gift(103, 12), tonnel_gift(101, 11)]

    adapter, http = tonnel([("POST", "filterStats", STATS), ("POST", "pageGifts", page)])
    run(adapter.refresh())
    floor = run(adapter.model_floor(KEY))
    assert (floor.price, floor.count) == (11, 7)
    assert run(adapter.model_floor(("toy bear", "pilot"))) is None  # по кэшу лотов нет

    items = run(adapter.cheapest(KEY, 6))
    assert [i.listing_id for i in items] == ["101", "103"]
    flt = json.loads(http.calls[-1][2]["filter"])
    assert flt["gift_name"] == "Toy Bear" and flt["model"] == "Wizard (1.5%)"
    assert run(adapter.cheapest(("unknown", "x"), 6)) is None
    assert run(adapter.cheapest(("toy bear", "no such model"), 6)) is None
    # кэш говорит «лотов нет», но cheapest всё равно спрашивает площадку
    assert [i.listing_id for i in run(adapter.cheapest(("toy bear", "pilot"), 6))] == ["201"]


def test_tonnel_stats_error_is_schema_error():
    adapter, _ = tonnel([("POST", "filterStats", {"status": "error", "message": "bad auth"})])
    with pytest.raises(SchemaError, match="bad auth"):
        run(adapter.refresh())


# ------------------------------------------------------------------ торговля

from giftbot.markets.base import BuyRejected, HttpError  # noqa: E402
from giftbot.markets.tonnel import tonnel_user_id  # noqa: E402
from giftbot.models import Listing, OwnedGift  # noqa: E402

INIT_DATA = "query_id=AA&user=%7B%22id%22%3A777%2C%22first_name%22%3A%22Vlad%22%7D&auth_date=1&hash=abc"


def test_portals_buy_payload_and_rejection():
    adapter, http = portals([("POST", "nfts", None)])
    run(adapter.buy(Listing("portals", "n1", "Toy Bear", "Wizard", 12.1 + 0.2, 7)))
    method, url, body = http.calls[0]
    assert url.endswith("/api/nfts")
    assert body == {"nft_details": [{"id": "n1", "price": "12.3"}]}

    adapter, _ = portals([("POST", "nfts", HttpError("portals", "HTTP 400: nft not listed", 400))])
    with pytest.raises(BuyRejected, match="nft not listed"):
        run(adapter.buy(Listing("portals", "n1", "Toy Bear", "Wizard", 12.3, 7)))


def test_portals_balance_owned_and_listing():
    owned_listed = {"nfts": [portals_nft("n1", 15)]}
    owned_unlisted = {"nfts": [{**portals_nft("n2", 1), "price": None, "status": "unlisted"}]}
    adapter, http = portals(
        [
            ("GET", "users/wallets", {"balance": "20.5", "frozen_funds": "0.5"}),
            ("GET", "status=listed", owned_listed),
            ("GET", "status=unlisted", owned_unlisted),
            ("POST", "bulk-list", {"successful_nfts": ["n2"], "failed_nfts": []}),
        ]
    )
    assert run(adapter.balance()).available == 20.0
    gifts = run(adapter.owned())
    assert [(g.owned_id, g.listed_price, g.number) for g in gifts] == [("n1", 15.0, 7), ("n2", None, 7)]
    assert gifts.complete
    run(adapter.list_for_sale(gifts[1], 13.25))
    assert http.calls[-1][2] == {"nft_prices": [{"nft_id": "n2", "price": "13.25"}]}

    adapter, _ = portals([("POST", "bulk-list", {"successful_nfts": [], "failed_nfts": [{"id": "n2"}]})])
    with pytest.raises(BuyRejected):
        run(adapter.list_for_sale(gifts[1], 13.25))


def test_portals_owned_marks_truncated_inventory():
    full_page = {"nfts": [portals_nft(f"n{i}", 15, number=i) for i in range(50)]}
    adapter, _ = portals([("GET", "status=listed", full_page), ("GET", "status=unlisted", {})])
    gifts = run(adapter.owned())
    assert len(gifts) == 250 and not gifts.complete  # упёрлись в лимит страниц


def tonnel_trader(routes):
    http = FakeHttp(routes)
    adapter = TonnelAdapter(http, token_factory=lambda: ("1700000000", "signed"))
    adapter.set_auth(INIT_DATA)
    return adapter, http


def test_tonnel_buy_list_and_errors():
    adapter, http = tonnel_trader([("POST", "buyGift/101", {"status": "success"}), ("POST", "listForSale", {"status": "success"})])
    run(adapter.buy(Listing("tonnel", "101", "Toy Bear", "Wizard", 10.5, 555)))
    body = http.calls[0][2]
    assert body["price"] == 10.5 and body["asset"] == "TON"  # цена без комиссии, как в ленте
    assert body["authData"] == INIT_DATA and body["wtf"] == "signed" and body["timestamp"] == "1700000000"
    run(adapter.list_for_sale(OwnedGift("tonnel", "202", "Toy Bear", "Wizard", 555, None), 13.456))
    assert http.calls[1][2]["gift_id"] == 202 and http.calls[1][2]["price"] == 13.46

    adapter, _ = tonnel_trader([("POST", "buyGift", {"status": "error", "message": "Gift already sold"})])
    with pytest.raises(BuyRejected, match="already sold"):
        run(adapter.buy(Listing("tonnel", "101", "Toy Bear", "Wizard", 10.5, 555)))

    adapter, _ = tonnel_trader([("POST", "buyGift", {"ok": True})])
    with pytest.raises(SchemaError):
        run(adapter.buy(Listing("tonnel", "101", "Toy Bear", "Wizard", 10.5, 555)))


def test_tonnel_balance_and_owned():
    def page(url, body):
        flt = json.loads(body["filter"])
        assert flt["seller"] == 777
        if flt["price"] == {"$exists": True}:
            return [tonnel_gift(301, 14.0)]
        return [{**tonnel_gift(302, None), "price": None}]

    adapter, _ = tonnel_trader(
        [
            ("POST", "balance/info", {"status": "success", "balance": 42.5, "transferGift": True}),
            ("POST", "pageGifts", page),
        ]
    )
    info = run(adapter.balance())
    assert info.available == 42.5 and info.auto_withdraw
    gifts = run(adapter.owned())
    assert [(g.owned_id, g.listed_price) for g in gifts] == [("301", 14.0), ("302", None)]
    assert adapter.trading_problem() is None


def test_tonnel_user_id_from_init_data():
    assert tonnel_user_id("tonnel", INIT_DATA) == 777
    with pytest.raises(Exception, match="ID пользователя"):
        tonnel_user_id("tonnel", "query_id=1")


# ------------------------------------------------------------------ Portals: новый формат collections/filters

from giftbot.markets.portals import parse_filter_models  # noqa: E402

NEW_FILTERS = {
    "collections": {
        "toybear": {
            "models": [
                {"name": "Wizard", "floor_price": "12.5", "rarity_per_mille": 15, "count": 7, "photo_url": ""},
                {"name": "Pilot (2%)", "floor_price": None, "rarity_per_mille": 20, "count": 0, "photo_url": ""},
            ],
            "symbols": [],
            "backdrops": [],
        }
    },
    "collection_photo_url": {"toybear": ""},
    "collection_floor_price": {"toybear": "9.5"},
}


def test_portals_new_filters_format_is_parsed():
    models = parse_filter_models("portals", NEW_FILTERS, "toybear")
    assert models == {"wizard": ("Wizard", 12.5), "pilot": ("Pilot", None)}
    # старый формат по-прежнему понимаем
    assert parse_filter_models("portals", FILTERS, "toybear")["wizard"] == ("Wizard", 12.5)
    assert parse_filter_models("portals", NEW_FILTERS, "unknown") == {}


def test_portals_unknown_model_fields_are_named_in_the_error():
    data = {"collections": {"toybear": {"models": [{"label": "Wizard", "cost": "1"}] * 3}}}
    with pytest.raises(SchemaError, match="cost, label"):
        parse_filter_models("portals", data, "toybear")
    with pytest.raises(SchemaError, match="поля ответа: something"):
        parse_filter_models("portals", {"something": {}}, "toybear")


def test_portals_adapter_reads_new_filters_format():
    adapter, _ = portals(
        [
            ("GET", "collections/filters", NEW_FILTERS),
            ("GET", "collections?", COLLECTIONS),
            ("GET", "nfts/search", {"results": [portals_nft("n1", 12.5)]}),
        ]
    )
    run(adapter.refresh())
    assert run(adapter.model_floor(KEY)).price == 12.5
    assert [i.listing_id for i in run(adapter.cheapest(KEY, 6))] == ["n1"]


def test_portals_thin_collection_keeps_models_without_listings():
    # у модели без лотов Portals не присылает floor_price вовсе
    data = {"collections": {"toybear": {"models": [
        {"name": "Wizard", "floor_price": "12.5", "supply": 100},
        {"name": "Pilot", "supply": 50},
        {"name": "Ghost", "supply": 40},
        {"name": "Witch", "supply": 30},
    ]}}}
    models = parse_filter_models("portals", data, "toybear")
    assert models == {"wizard": ("Wizard", 12.5), "pilot": ("Pilot", None), "ghost": ("Ghost", None),
                      "witch": ("Witch", None)}


def test_portals_buy_refused_with_http_200_is_a_rejection():
    refusal = {"total_purchased": 0, "total_failed": 1,
               "purchase_results": [{"status": "failed", "reason": "INSUFFICIENT_BALANCE"}]}
    adapter, _ = portals([("POST", "nfts", refusal)])
    with pytest.raises(BuyRejected, match="INSUFFICIENT_BALANCE"):
        run(adapter.buy(Listing("portals", "n1", "Toy Bear", "Wizard", 12.3, 7)))
    adapter, _ = portals([("POST", "nfts", {"total_purchased": 1, "total_failed": 0, "purchase_results": []})])
    run(adapter.buy(Listing("portals", "n1", "Toy Bear", "Wizard", 12.3, 7)))  # успех — без ошибки


def test_tonnel_model_without_rarity_in_stats_is_searched_by_prefix():
    from giftbot.markets.tonnel import model_filter

    assert model_filter("Wizard (1.5%)") == "Wizard (1.5%)"
    regex = model_filter("Ghost")["$regex"]
    import re as _re
    assert _re.match(regex, "Ghost (1.5%)") and _re.match(regex, "Ghost") and not _re.match(regex, "Ghostly (2%)")
    stats = {"status": "success", "data": {"Toy Bear_Wizard": {"floorPrice": 11, "howMany": 7}}}
    adapter, http = tonnel([("POST", "filterStats", stats), ("POST", "pageGifts", [tonnel_gift(101, 11)])])
    run(adapter.refresh())
    [item] = run(adapter.cheapest(KEY, 6))  # лот с «Wizard (1.5%)» найден по «Wizard»
    assert item.listing_id == "101"
    assert json.loads(http.calls[-1][2]["filter"])["model"] == {"$regex": "^Wizard( \\(|$)"}


def test_portals_collection_without_listings_is_not_a_format_change():
    unpriced = {"collections": {"durovscap": {"models": [{"name": "Royal", "supply": 3}, {"name": "Black", "supply": 2}]}}}
    # у коллекции нет лотов — модели без цены нормальны
    assert parse_filter_models("portals", unpriced, "durovscap", expect_prices=False) == {
        "royal": ("Royal", None), "black": ("Black", None)}
    # у коллекции есть лоты, а цены нет ни у одной модели — значит, сменился формат
    with pytest.raises(SchemaError, match="ни у одной модели"):
        parse_filter_models("portals", unpriced, "durovscap")
    adapter, _ = portals([("GET", "collections/filters", unpriced), ("GET", "collections?", COLLECTIONS)])
    run(adapter.refresh())  # в COLLECTIONS у Durov's Cap floor_price = None
    assert run(adapter.model_floor(("durov's cap", "royal"))) is None
