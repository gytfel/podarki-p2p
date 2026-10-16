"""Общие заглушки для тестов: настройки, HTTP, площадка, бот, авторизация."""
from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from giftbot.config import AutobuyConfig, Config  # noqa: E402
from giftbot.evaluator import Fees, Thresholds  # noqa: E402
from giftbot.markets.base import MarketAdapter  # noqa: E402
from giftbot.markets.base import BuyRejected  # noqa: E402
from giftbot.models import BalanceInfo, Key, Listing, ModelFloor, OwnedGift  # noqa: E402
from giftbot.store import Store  # noqa: E402


def make_autobuy(**overrides: Any) -> AutobuyConfig:
    ab = AutobuyConfig(
        mode="live",
        markets=("portals", "tonnel"),
        cross=False,
        min_profit_ton=1.0,
        min_profit_pct=0.10,
        max_price=20.0,
        daily_limit=30.0,
        max_per_day=5,
        max_open=3,
        reserve=0.5,
        cooldown=0.0,
        autolist=True,
        positions_check=300,
    )
    return replace(ab, **overrides)


def make_config(**overrides: Any) -> Config:
    cfg = Config(
        bot_token="token",
        admin_chat_id=42,
        api_id=1,
        api_hash="hash",
        data_dir=Path("data"),
        markets=("portals", "tonnel"),
        fees={"portals": Fees(buy=0.0, sell=0.05), "tonnel": Fees(buy=0.10, sell=0.0)},
        thresholds=Thresholds(
            min_profit_ton=0.5,
            min_profit_pct=0.05,
            undercut=0.01,
            min_ref_listings=3,
            anomaly_ratio=0.25,
            transfer_cost_ton=0.3,
        ),
        min_buy_price=1.0,
        max_buy_price=50.0,
        poll_interval=8,
        poll_jitter=0,
        request_interval=1,
        floors_refresh=180,
        auth_refresh=1800,
        max_errors=3,
        probe_interval=300,
        auto_resume=True,
        latest_limit=30,
        verify_depth=6,
        proxy=None,
        auth_overrides={},
        portals_api_url="https://portal-market.com/api/",
        tonnel_page_url="https://gifts2.tonnel.network/api/pageGifts",
        tonnel_stats_url="https://gifts3.tonnel.network/api/filterStats",
        tonnel_balance_url="https://gifts2.tonnel.network/api/balance/info",
        tonnel_trade_url="https://gifts.coffin.meme/api/",
        autobuy=make_autobuy(mode="off"),
    )
    return replace(cfg, **overrides)


def lot(market: str, lid: str, price: float, collection="Toy Bear", model="Wizard", number=None) -> Listing:
    return Listing(market=market, listing_id=lid, collection=collection, model=model, price=price, number=number)


class FakeHttp:
    """Отвечает заранее заданными данными по подстроке адреса."""

    def __init__(self, routes: list[tuple[str, str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    async def request(self, method: str, url: str, *, headers=None, json_body=None, allow_empty=False) -> Any:
        self.calls.append((method, url, json_body))
        for route_method, fragment, response in self.routes:
            if route_method == method and fragment in url:
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(url, json_body)
                return copy.deepcopy(response)
        raise AssertionError(f"неожиданный запрос {method} {url}")

    async def close(self) -> None:
        pass


class FakeAdapter(MarketAdapter):
    """Площадка в памяти. Ошибки можно подкладывать в очередь errors."""

    def __init__(self, name: str, title: str):
        super().__init__(http=None)  # type: ignore[arg-type]
        self.name = name
        self.title = title
        self.feed: list[Listing] = []
        self.floors: dict[Key, ModelFloor] = {}
        self.books: dict[Key, list[Listing]] = {}
        self.errors: list[Exception] = []
        self.calls: list[str] = []
        self.auth_seen: list[str] = []
        # торговля
        self.wallet = 100.0
        self.auto_withdraw = False
        self.inventory: list[OwnedGift] = []
        self.buy_errors: list[Exception] = []
        self.list_errors: list[Exception] = []
        self.buy_lands = True  # попадает ли купленное в инвентарь
        self.bought: list[Listing] = []
        self.listed: list[tuple[str, float]] = []

    def _maybe_fail(self, method: str) -> None:
        self.calls.append(method)
        self.auth_seen.append(self.auth)
        if self.errors:
            raise self.errors.pop(0)

    async def latest(self, limit: int) -> list[Listing]:
        self._maybe_fail("latest")
        return list(self.feed[:limit])

    async def refresh(self) -> None:
        self._maybe_fail("refresh")

    async def model_floor(self, key: Key) -> ModelFloor | None:
        self._maybe_fail("model_floor")
        return self.floors.get(key)

    async def cheapest(self, key: Key, limit: int) -> list[Listing] | None:
        self._maybe_fail("cheapest")
        if key not in self.books:
            return None
        return sorted(self.books[key], key=lambda i: i.price)[:limit]

    async def balance(self) -> BalanceInfo:
        self._maybe_fail("balance")
        return BalanceInfo(available=self.wallet, auto_withdraw=self.auto_withdraw)

    async def buy(self, listing: Listing) -> None:
        self.calls.append("buy")
        self.bought.append(listing)
        if self.buy_errors:
            error = self.buy_errors.pop(0)
            landed = not isinstance(error, BuyRejected) and self.buy_lands
            if landed:
                self._land(listing)
            raise error
        if self.buy_lands:
            self._land(listing)

    def _land(self, listing: Listing) -> None:
        self.inventory.append(
            OwnedGift(self.name, f"own-{listing.listing_id}", listing.collection, listing.model, listing.number, None)
        )
        if self.books.get(listing.key):
            self.books[listing.key] = [i for i in self.books[listing.key] if i.listing_id != listing.listing_id]

    async def owned(self) -> list[OwnedGift]:
        self._maybe_fail("owned")
        return list(self.inventory)

    async def list_for_sale(self, gift: OwnedGift, price: float) -> None:
        self.calls.append("list")
        if self.list_errors:
            raise self.list_errors.pop(0)
        self.listed.append((gift.owned_id, price))

    async def close(self) -> None:
        pass


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, chat_id: int | None = None) -> bool:
        self.sent.append(text)
        return True

    async def poll_commands(self, handler) -> None:  # pragma: no cover
        pass


class FakeAuth:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.count = 0

    async def get(self, market: str) -> str:
        if self.fail:
            raise RuntimeError("нет сети")
        self.count += 1
        return f"{market}-auth-{self.count}"


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()
