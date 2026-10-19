"""Tonnel (@Tonnel_Network_bot). Неофициальное API мини-приложения.

Эндпоинты взяты из открытой библиотеки tonnelmp. Цена в ответах Tonnel — без комиссии
покупателя: при покупке площадка добавляет её сверху (см. TONNEL_BUY_FEE_PCT).
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote

from ..models import BalanceInfo, Inventory, Key, Listing, ModelFloor, OwnedGift, normalize, strip_rarity
from .base import (
    AuthError,
    BuyRejected,
    HttpClient,
    HttpError,
    MarketAdapter,
    MarketError,
    SchemaError,
    SkipItem,
    as_int_or_none,
    as_price,
    as_str,
    only_key,
    parse_items,
    pick,
)

log = logging.getLogger(__name__)

DEFAULT_PAGE_URL = "https://gifts2.tonnel.network/api/pageGifts"
DEFAULT_STATS_URL = "https://gifts2.tonnel.network/api/filterStats"  # туда же ходит веб-приложение Tonnel
DEFAULT_BALANCE_URL = "https://gifts2.tonnel.network/api/balance/info"
DEFAULT_TRADE_URL = "https://gifts.coffin.meme/api/"  # торговый сервер Tonnel
MAX_LIMIT = 30  # больше Tonnel за раз не отдаёт
OWNED_MAX_PAGES = 5

HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
}

SORT_LATEST = '{"message_post_time":-1,"gift_id":-1}'
SORT_PRICE_ASC = '{"price":1,"gift_id":-1}'


@dataclass(frozen=True)
class _Floor:
    collection: str  # как пишет Tonnel: «Toy Bear»
    model: str  # как пишет Tonnel: «Wizard (1.5%)»
    price: float
    count: int | None


class TonnelAdapter(MarketAdapter):
    name = "tonnel"
    title = "Tonnel"

    def __init__(
        self,
        http: HttpClient,
        *,
        page_url: str = DEFAULT_PAGE_URL,
        stats_url: str = DEFAULT_STATS_URL,
        balance_url: str = DEFAULT_BALANCE_URL,
        trade_url: str = DEFAULT_TRADE_URL,
        token_factory: Callable[[], tuple[str, str]] | None = None,
    ):
        super().__init__(http)
        self.page_url = page_url
        self.stats_url = stats_url
        self.balance_url = balance_url
        self.trade_url = trade_url if trade_url.endswith("/") else trade_url + "/"
        self._token_factory = token_factory
        self._floors: dict[Key, _Floor] = {}
        self._collections: dict[str, str] = {}  # нормализованное имя -> как пишет Tonnel
        # все модели, в том числе без лотов: ключ -> (коллекция, модель) как пишет Tonnel
        self._models: dict[Key, tuple[str, str]] = {}

    # ------------------------------------------------------------ запросы

    @staticmethod
    def _base_filter() -> dict[str, Any]:
        # Те же условия, что у витрины Tonnel: в продаже, не куплен, за TON, не предмаркет
        return {
            "price": {"$exists": True},
            "buyer": {"$exists": False},
            "asset": "TON",
            "refunded": {"$ne": True},
            "export_at": {"$exists": True},
        }

    async def _raw_page(self, flt: dict[str, Any], sort: str, limit: int, page: int = 1) -> Any:
        payload = {
            "filter": json.dumps(flt),
            "limit": min(limit, MAX_LIMIT),
            "page": page,
            "sort": sort,
            "ref": 0,
            "price_range": 0,
            "user_auth": self.auth,
        }
        return await self.http.request("POST", self.page_url, headers=HEADERS, json_body=payload)

    async def _page(self, flt: dict[str, Any], sort: str, limit: int, what: str) -> list[Listing]:
        data = await self._raw_page(flt, sort, limit)
        return parse_items(self.name, data, self._parse_listing, what)

    async def latest(self, limit: int) -> list[Listing]:
        return await self._page(self._base_filter(), SORT_LATEST, limit, "последние лоты")

    async def refresh(self) -> None:
        data = await self.http.request("POST", self.stats_url, headers=HEADERS, json_body={"authData": self.auth})
        if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("data"), dict):
            message = data.get("message") if isinstance(data, dict) else None
            raise SchemaError(self.name, f"filterStats: неожиданный ответ ({message or 'нет data'})")
        floors: dict[Key, _Floor] = {}
        collections: dict[str, str] = {}
        models: dict[Key, tuple[str, str]] = {}
        bad = 0
        for raw_key, value in data["data"].items():
            collection, sep, model = str(raw_key).partition("_")
            if not sep or not isinstance(value, dict):
                bad += 1
                continue
            collections[normalize(collection)] = collection
            key = (normalize(collection), normalize(model))
            models[key] = (collection, model)
            if value.get("floorPrice") is None:
                continue  # нет лотов этой модели
            try:
                price = as_price(self.name, value["floorPrice"], "floorPrice")
            except SchemaError:
                bad += 1
                continue
            floors[key] = _Floor(collection, model, price, as_int_or_none(value.get("howMany")))
        if data["data"] and (not floors or bad * 2 > len(data["data"])):
            raise SchemaError(self.name, f"filterStats: не разобрано {bad} из {len(data['data'])} записей")
        self._floors = floors
        self._collections = collections
        self._models = models
        log.info("Tonnel: загружено флоров моделей: %d", len(floors))

    async def model_floor(self, key: Key) -> ModelFloor | None:
        floor = self._floors.get(key)
        return ModelFloor(price=floor.price, count=floor.count) if floor else None

    async def cheapest(self, key: Key, limit: int) -> list[Listing] | None:
        if key[0] not in self._collections:
            return None
        names = self._models.get(key)
        if names is None:
            return None  # модели не было при последнем обновлении кэша — запросить нечем
        # Запрашиваем вживую, даже если по кэшу лотов нет: кэш обновляется раз в FLOORS_REFRESH_SEC
        flt = self._base_filter()
        collection, model = names
        flt["gift_name"] = collection
        flt["model"] = model_filter(model)
        items = await self._page(flt, SORT_PRICE_ASC, limit, "дешёвые лоты модели")
        return only_key(self.name, items, key)

    # ------------------------------------------------------------ торговля

    def _token(self) -> tuple[str, str]:
        """Метка времени и подпись, которых торговый сервер Tonnel ждёт в каждом запросе."""
        if self._token_factory is None:
            try:
                from tonnelmp.wtf import generate_wtf
            except ImportError as exc:
                raise MarketError(self.name, "для торговли на Tonnel установите пакет: pip install tonnelmp==1.2") from exc
            self._token_factory = generate_wtf
        return self._token_factory()

    def trading_problem(self) -> str | None:
        try:
            self._token()
        except MarketError as exc:
            return str(exc)
        try:
            tonnel_user_id(self.name, self.auth)
        except AuthError as exc:
            return str(exc)
        return None

    async def _trade(self, path: str, payload: dict[str, Any]) -> dict:
        timestamp, signature = self._token()
        body = {"authData": self.auth, **payload, "timestamp": timestamp, "wtf": signature}
        try:
            data = await self.http.request("POST", self.trade_url + path, headers=HEADERS, json_body=body)
        except HttpError as exc:
            raise BuyRejected(self.name, str(exc)) from exc
        if not isinstance(data, dict):
            raise SchemaError(self.name, f"{path}: ожидался объект, пришло {type(data).__name__}")
        if data.get("status") == "success":
            return data
        if data.get("status") == "error":
            raise BuyRejected(self.name, str(data.get("message") or data))
        raise SchemaError(self.name, f"{path}: неожиданный ответ {str(data)[:200]}")

    async def balance(self) -> BalanceInfo:
        data = await self.http.request("POST", self.balance_url, headers=HEADERS, json_body={"authData": self.auth})
        if not isinstance(data, dict) or data.get("status") != "success" or "balance" not in data:
            message = data.get("message") if isinstance(data, dict) else None
            raise SchemaError(self.name, f"balance/info: неожиданный ответ ({message or 'нет balance'})")
        try:
            available = float(data["balance"])
        except (TypeError, ValueError):
            raise SchemaError(self.name, f"balance: ожидалось число, пришло {data['balance']!r}") from None
        return BalanceInfo(available=max(0.0, available), auto_withdraw=bool(data.get("transferGift")))

    async def buy(self, listing: Listing) -> None:
        # Цена — как в ленте, без комиссии: Tonnel сам добавит её при списании
        await self._trade(f"buyGift/{listing.listing_id}", {"asset": "TON", "price": float(listing.price)})

    async def owned(self) -> Inventory:
        user_id = tonnel_user_id(self.name, self.auth)
        queries = [
            # выставленные на продажу
            ({"seller": user_id, "buyer": {"$exists": False}, "price": {"$exists": True}}, SORT_LATEST, True),
            # лежат в инвентаре без цены
            (
                {"seller": user_id, "buyer": {"$exists": False}, "refunded": {"$ne": True}, "price": {"$exists": False}},
                '{"gift_num":1,"gift_id":-1}',
                False,
            ),
        ]
        gifts: list[OwnedGift] = []
        complete = True
        for flt, sort, listed in queries:
            for page in range(1, OWNED_MAX_PAGES + 1):
                data = await self._raw_page(flt, sort, MAX_LIMIT, page)
                gifts += parse_items(self.name, data, lambda i: self._parse_owned(i, listed), "ваши подарки")
                if not isinstance(data, list) or len(data) < MAX_LIMIT:
                    break
            else:
                complete = False
                log.warning("Tonnel: в инвентаре больше %d подарков, прочитаны не все", MAX_LIMIT * OWNED_MAX_PAGES)
        return Inventory(gifts, complete)

    async def list_for_sale(self, gift: OwnedGift, price: float) -> None:
        await self._trade("listForSale", {"gift_id": int(gift.owned_id), "price": round(price, 2), "asset": "TON"})

    def _parse_owned(self, item: Any, listed: bool) -> OwnedGift:
        if not isinstance(item, dict):
            raise SchemaError(self.name, "подарок: ожидался объект")
        gift_id = as_int_or_none(item.get("gift_id"))
        if gift_id is None:
            raise SchemaError(self.name, f"gift_id: ожидалось число, пришло {item.get('gift_id')!r}")
        if gift_id < 0:
            raise SkipItem()
        model = item.get("model")
        return OwnedGift(
            market=self.name,
            owned_id=str(gift_id),
            collection=as_str(self.name, pick(item, "name", "gift_name"), "name"),
            model=strip_rarity(model) if isinstance(model, str) and model else None,
            number=as_int_or_none(item.get("gift_num")),
            listed_price=as_price(self.name, item.get("price")) if listed and item.get("price") is not None else None,
        )

    # ------------------------------------------------------------ разбор

    def _parse_listing(self, item: Any) -> Listing:
        if not isinstance(item, dict):
            raise SchemaError(self.name, "лот: ожидался объект")
        gift_id = as_int_or_none(item.get("gift_id"))
        if gift_id is None:
            raise SchemaError(self.name, f"gift_id: ожидалось число, пришло {item.get('gift_id')!r}")
        if gift_id < 0 or item.get("bundleData"):
            raise SkipItem()  # набор из нескольких подарков
        if item.get("auction") or item.get("status") == "auction":
            raise SkipItem()
        if item.get("asset") not in (None, "TON"):
            raise SkipItem()
        model = item.get("model")
        return Listing(
            market=self.name,
            listing_id=str(gift_id),
            collection=as_str(self.name, pick(item, "name", "gift_name"), "name"),
            model=strip_rarity(model) if isinstance(model, str) and model else None,
            price=as_price(self.name, item.get("price")),
            number=as_int_or_none(item.get("gift_num")),
            backdrop=strip_rarity(item["backdrop"]) if isinstance(item.get("backdrop"), str) else None,
            symbol=strip_rarity(item["symbol"]) if isinstance(item.get("symbol"), str) else None,
        )


def model_filter(model: str) -> Any:
    """Фильтр по модели для pageGifts. В лотах модель с редкостью («Ghost (1.5%)»), а в filterStats её может
    не быть («Ghost») — тогда ищем по началу названия, как tonnelmp."""
    if strip_rarity(model) != model:
        return model
    return {"$regex": f"^{re.escape(model)}( \\(|$)"}


def tonnel_user_id(market: str, init_data: str) -> int:
    """Telegram ID из initData: Tonnel ищет ваши подарки по нему."""
    try:
        raw = parse_qs(init_data).get("user", [""])[0]
        try:
            user = json.loads(raw)
        except ValueError:
            user = json.loads(unquote(raw))
        return int(user["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthError(market, "в данных входа Tonnel нет ID пользователя") from exc
