"""Portals (@portals). Неофициальное API мини-приложения.

Эндпоинты взяты из открытой библиотеки aportalsmp. Если Portals что-то поменяет,
править нужно здесь: адреса, названия параметров и полей ответа.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from ..models import BalanceInfo, Inventory, Key, Listing, ModelFloor, OwnedGift, normalize, strip_rarity
from .base import (
    BuyRejected,
    HttpClient,
    HttpError,
    MarketAdapter,
    SchemaError,
    SkipItem,
    as_int_or_none,
    as_price,
    as_str,
    format_price,
    only_key,
    parse_dt,
    parse_items,
)

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://portal-market.com/api/"
PAGE_SIZE = 100
MAX_PAGES = 10
OWNED_PAGE = 50
OWNED_MAX_PAGES = 5

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://portal-market.com",
    "Referer": "https://portal-market.com/",
}


@dataclass(frozen=True)
class _Collection:
    id: str
    name: str
    short_name: str
    floor: float | None


def short_name(name: str) -> str:
    return name.replace(" ", "").replace("'", "").replace("’", "").replace("-", "").lower()


class PortalsAdapter(MarketAdapter):
    name = "portals"
    title = "Portals"

    def __init__(
        self,
        http: HttpClient,
        *,
        api_url: str = DEFAULT_API_URL,
        models_ttl: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(http)
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self.models_ttl = models_ttl
        self._clock = clock
        self._collections: dict[str, _Collection] = {}  # нормализованное имя -> коллекция
        self._names_by_id: dict[str, str] = {}
        # нормализованное имя коллекции -> (когда загружено, {модель: (как пишет Portals, флор или None)})
        self._models: dict[str, tuple[float, dict[str, tuple[str, float | None]]]] = {}

    # ------------------------------------------------------------ запросы

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = self.api_url + path + ("?" + urlencode(params, safe=",") if params else "")
        headers = {**HEADERS, "Authorization": self.auth}
        return await self.http.request("GET", url, headers=headers)

    @staticmethod
    def _listing_params(limit: int, sort_by: str) -> dict[str, Any]:
        return {
            "offset": 0,
            "limit": limit,
            "sort_by": sort_by,
            "status": "listed",
            "exclude_bundled": "true",
            # предмаркет нельзя вывести с площадки — для арбитража не годится
            "premarket_status": "without_premarket",
        }

    async def latest(self, limit: int) -> list[Listing]:
        data = await self._get("nfts/search", self._listing_params(limit, "listed_at desc"))
        return parse_items(self.name, _results(self.name, data), self._parse_listing, "последние лоты")

    async def refresh(self) -> None:
        collections: dict[str, _Collection] = {}
        offset = 0
        for _ in range(MAX_PAGES):
            data = await self._get("collections", {"offset": offset, "limit": PAGE_SIZE})
            if not isinstance(data, dict) or not isinstance(data.get("collections"), list):
                raise SchemaError(self.name, "collections: нет списка collections в ответе")
            items = data["collections"]
            added = 0
            for coll in parse_items(self.name, items, self._parse_collection, "коллекции"):
                key = normalize(coll.name)
                if key not in collections:
                    collections[key] = coll
                    added += 1
            # если API игнорирует offset, вторая страница повторит первую — тогда стоп
            if len(items) < PAGE_SIZE or added == 0:
                break
            offset += PAGE_SIZE
        if not collections:
            raise SchemaError(self.name, "список коллекций пуст")
        self._collections = collections
        self._names_by_id = {c.id: c.name for c in collections.values()}
        log.info("Portals: загружено коллекций: %d", len(collections))

    async def model_floor(self, key: Key) -> ModelFloor | None:
        coll = self._collections.get(key[0])
        if coll is None:
            return None
        model = (await self._models_for(coll)).get(key[1])
        return ModelFloor(price=model[1]) if model and model[1] is not None else None

    async def cheapest(self, key: Key, limit: int) -> list[Listing] | None:
        coll = self._collections.get(key[0])
        if coll is None:
            return None
        model = (await self._models_for(coll)).get(key[1])
        if model is None:
            return None  # модели нет в кэше — запросить нечем
        # Запрашиваем вживую, даже если по кэшу лотов нет: кэш моделей живёт models_ttl секунд
        params = self._listing_params(limit, "price asc")
        params["collection_ids"] = coll.id
        params["filter_by_models"] = model[0]
        data = await self._get("nfts/search", params)
        items = parse_items(self.name, _results(self.name, data), self._parse_listing, "дешёвые лоты модели")
        return only_key(self.name, items, key)

    # ------------------------------------------------------------ торговля

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        headers = {**HEADERS, "Authorization": self.auth, "Content-Type": "application/json"}
        return await self.http.request("POST", self.api_url + path, headers=headers, json_body=payload, allow_empty=True)

    async def balance(self) -> BalanceInfo:
        data = await self._get("users/wallets/", {})
        if not isinstance(data, dict) or "balance" not in data:
            raise SchemaError(self.name, "users/wallets: нет поля balance в ответе")
        total = _amount(self.name, data.get("balance"), "balance")
        frozen = _amount(self.name, data.get("frozen_funds"), "frozen_funds")
        return BalanceInfo(available=max(0.0, total - frozen))

    async def buy(self, listing: Listing) -> None:
        payload = {"nft_details": [{"id": listing.listing_id, "price": format_price(listing.price)}]}
        try:
            data = await self._post("nfts", payload)
        except HttpError as exc:  # 4xx: площадка отказала, покупка не прошла
            raise BuyRejected(self.name, str(exc)) from exc
        # Отказ может прийти и с кодом 200: {"total_purchased": 0, "purchase_results": [{"reason": ...}]}
        if isinstance(data, dict) and "total_purchased" in data and not data.get("total_purchased"):
            results = data.get("purchase_results") or []
            reasons = [str(r["reason"]) for r in results if isinstance(r, dict) and r.get("reason")]
            raise BuyRejected(self.name, f"покупка не прошла: {', '.join(reasons) or 'причина не указана'}")

    async def owned(self) -> Inventory:
        gifts: list[OwnedGift] = []
        complete = True
        for status in ("listed", "unlisted"):
            for page in range(OWNED_MAX_PAGES):
                params = {"offset": page * OWNED_PAGE, "limit": OWNED_PAGE, "status": status}
                data = await self._get("nfts/owned", params)
                if data == {}:
                    break  # пустой инвентарь
                if not isinstance(data, dict) or not isinstance(data.get("nfts"), list):
                    raise SchemaError(self.name, "nfts/owned: нет списка nfts в ответе")
                listed = status == "listed"
                gifts += parse_items(self.name, data["nfts"], lambda i: self._parse_owned(i, listed), "ваши подарки")
                if len(data["nfts"]) < OWNED_PAGE:
                    break
            else:
                complete = False
                log.warning("Portals: в инвентаре больше %d подарков (%s), прочитаны не все", OWNED_PAGE * OWNED_MAX_PAGES, status)
        return Inventory(gifts, complete)

    async def list_for_sale(self, gift: OwnedGift, price: float) -> None:
        payload = {"nft_prices": [{"nft_id": gift.owned_id, "price": format_price(price)}]}
        try:
            data = await self._post("nfts/bulk-list", payload)
        except HttpError as exc:
            raise BuyRejected(self.name, str(exc)) from exc
        if isinstance(data, dict) and data.get("failed_nfts"):
            raise BuyRejected(self.name, f"площадка не выставила подарок: {data['failed_nfts']}")

    def _parse_owned(self, item: Any, listed: bool) -> OwnedGift:
        if not isinstance(item, dict):
            raise SchemaError(self.name, "подарок: ожидался объект")
        model = None
        for attr in item.get("attributes") or []:
            if isinstance(attr, dict) and attr.get("type") == "model" and attr.get("value"):
                model = strip_rarity(str(attr["value"]))
        name = item.get("name") or self._names_by_id.get(str(item.get("collection_id")))
        return OwnedGift(
            market=self.name,
            owned_id=as_str(self.name, item.get("id"), "id"),
            collection=as_str(self.name, name, "name"),
            model=model,
            number=as_int_or_none(item.get("external_collection_number")),
            listed_price=as_price(self.name, item.get("price")) if listed else None,
        )

    # ------------------------------------------------------------ кэш моделей

    async def _models_for(self, coll: _Collection) -> dict[str, tuple[str, float | None]]:
        cached = self._models.get(normalize(coll.name))
        if cached and self._clock() - cached[0] < self.models_ttl:
            return cached[1]
        data = await self._get("collections/filters", {"short_names": coll.short_name})
        models = parse_filter_models(self.name, data, coll.short_name, expect_prices=coll.floor is not None)
        self._models[normalize(coll.name)] = (self._clock(), models)
        return models

    # ------------------------------------------------------------ разбор

    def _parse_collection(self, item: Any) -> _Collection:
        if not isinstance(item, dict):
            raise SchemaError(self.name, "коллекция: ожидался объект")
        name = as_str(self.name, item.get("name"), "name")
        floor_raw = item.get("floor_price")
        try:
            floor = as_price(self.name, floor_raw) if floor_raw is not None else None
        except SchemaError:
            floor = None
        return _Collection(
            id=as_str(self.name, item.get("id"), "id"),
            name=name,
            short_name=str(item.get("short_name") or short_name(name)),
            floor=floor,
        )

    def _parse_listing(self, item: Any) -> Listing:
        if not isinstance(item, dict):
            raise SchemaError(self.name, "лот: ожидался объект")
        name = item.get("name") or self._names_by_id.get(str(item.get("collection_id")))
        model = backdrop = symbol = None
        attributes = item.get("attributes") or []
        if not isinstance(attributes, list):
            raise SchemaError(self.name, "attributes: ожидался список")
        for attr in attributes:
            if not isinstance(attr, dict):
                continue
            value = attr.get("value")
            if attr.get("type") == "model":
                model = strip_rarity(str(value)) if value else None
            elif attr.get("type") == "backdrop":
                backdrop = value
            elif attr.get("type") == "symbol":
                symbol = value
        if item.get("status") not in (None, "listed"):
            raise SkipItem()
        locked = parse_dt(item.get("unlocks_at"))
        if locked and locked <= datetime.now(timezone.utc):
            locked = None
        return Listing(
            market=self.name,
            listing_id=as_str(self.name, item.get("id"), "id"),
            collection=as_str(self.name, name, "name"),
            model=model,
            price=as_price(self.name, item.get("price")),
            number=as_int_or_none(item.get("external_collection_number")),
            backdrop=backdrop,
            symbol=symbol,
            locked_until=locked,
        )


# Поля модели в новом формате collections/filters. Точные названия Portals не публикует —
# берём первое найденное; если не нашлось ни одного, ошибка перечислит реальные поля объекта.
MODEL_NAME_KEYS = ("name", "value", "model", "title")
MODEL_PRICE_KEYS = ("floor_price", "floor", "min_price", "floorPrice", "price")


def parse_filter_models(
    market: str, data: Any, short_name: str, *, expect_prices: bool = True
) -> dict[str, tuple[str, float | None]]:
    """Модели коллекции из ответа collections/filters: нормализованное имя -> (как пишет Portals, флор или None).

    Старый формат (как в aportalsmp 1.3): {"floor_prices": {"<short_name>": {"models": {"Wizard": "12.5"}}}}.
    Новый формат: {"collections": {"<short_name>": {"models": [{...}, ...], ...}}, "collection_floor_price": ...}.
    expect_prices=False — у коллекции сейчас нет лотов, и модели без цены там нормальны.
    """
    if not isinstance(data, dict):
        raise SchemaError(market, f"collections/filters: ожидался объект, пришло {type(data).__name__}")
    block = next((data[k] for k in ("collections", "floor_prices") if isinstance(data.get(k), dict)), None)
    if block is None:
        raise SchemaError(market, f"collections/filters: нет списка коллекций (поля ответа: {', '.join(list(data)[:10])})")
    entry = block.get(short_name)
    if entry is None:
        return {}  # о коллекции Portals ничего не сообщил
    raw_models = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(raw_models, dict):
        pairs = list(raw_models.items())
    elif isinstance(raw_models, list):
        # У модели без лотов поля с ценой нет вовсе. Формат сменился, только если у коллекции есть лоты,
        # а цены нет ни у одной модели
        if expect_prices and raw_models and not any(isinstance(i, dict) and any(k in i for k in MODEL_PRICE_KEYS) for i in raw_models):
            first = raw_models[0]
            fields = ", ".join(sorted(map(str, first))) if isinstance(first, dict) else type(first).__name__
            raise SchemaError(market, f"модели: не нашёл цену ни у одной модели (поля: {fields})")
        pairs = parse_items(market, raw_models, lambda item: _model_pair(market, item), "модели коллекции")
    else:
        raise SchemaError(market, "collections/filters: нет списка моделей")
    models: dict[str, tuple[str, float | None]] = {}
    for raw_name, raw_price in pairs:
        try:
            price = as_price(market, raw_price, f"флор модели {raw_name}")
        except SchemaError:
            price = None  # у модели нет лотов, но имя нужно для запроса вживую
        models[normalize(raw_name)] = (strip_rarity(raw_name), price)
    return models


def _model_pair(market: str, item: Any) -> tuple[str, Any]:
    if not isinstance(item, dict):
        raise SchemaError(market, f"модель: ожидался объект, пришло {type(item).__name__}")
    fields = ", ".join(sorted(map(str, item)))
    name = next((item[k] for k in MODEL_NAME_KEYS if isinstance(item.get(k), str) and item[k].strip()), None)
    if name is None:
        raise SchemaError(market, f"модель: не нашёл название среди полей {fields}")
    price_key = next((k for k in MODEL_PRICE_KEYS if k in item), None)
    return name, item[price_key] if price_key else None  # нет цены — у модели сейчас нет лотов


def _amount(market: str, value: Any, what: str) -> float:
    if value in (None, ""):
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise SchemaError(market, f"{what}: ожидалось число, пришло {value!r}") from None
    if amount < 0 or amount != amount:
        raise SchemaError(market, f"{what}: недопустимое значение {value!r}")
    return amount


def _results(market: str, data: Any) -> Any:
    if not isinstance(data, dict) or "results" not in data:
        raise SchemaError(market, "nfts/search: нет поля results в ответе")
    return data["results"]
