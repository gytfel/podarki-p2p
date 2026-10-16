"""Общая часть адаптеров площадок: ошибки, лимит запросов, HTTP-клиент, проверка ответов."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, TypeVar

from ..models import BalanceInfo, Key, Listing, ModelFloor, OwnedGift

log = logging.getLogger(__name__)
T = TypeVar("T")


# ---------------------------------------------------------------- ошибки


class MarketError(Exception):
    kind = "ошибка площадки"

    def __init__(self, market: str, message: str):
        super().__init__(message)
        self.market = market

    def human(self) -> str:
        return f"{self.kind}: {self}"


class TransportError(MarketError):
    kind = "площадка или сеть недоступны"


class AuthError(MarketError):
    kind = "ошибка авторизации"


class RateLimited(MarketError):
    kind = "слишком частые запросы"

    def __init__(self, market: str, message: str, retry_after: float | None = None):
        super().__init__(market, message)
        self.retry_after = retry_after


class HttpError(MarketError):
    kind = "неожиданный ответ HTTP"

    def __init__(self, market: str, message: str, status: int):
        super().__init__(market, message)
        self.status = status


class SchemaError(MarketError):
    """Ответ пришёл, но его формат не тот, что ожидает бот. Данным доверять нельзя."""

    kind = "формат ответа изменился"


class BuyRejected(MarketError):
    """Площадка отказала в сделке (лот уже купили, цена изменилась, мало средств). Деньги не списаны."""

    kind = "сделка отклонена"


class SkipItem(Exception):
    """Запись корректная, но нам не подходит (аукцион, набор, не TON). Не считается ошибкой."""


# ---------------------------------------------------------------- лимит запросов


class RateLimiter:
    """Не чаще одного запроса в min_interval секунд (плюс случайная пауза)."""

    def __init__(
        self,
        min_interval: float,
        jitter: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.min_interval = min_interval
        self.jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_allowed - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_allowed = self._clock() + self.min_interval + random.uniform(0, self.jitter)

    def penalize(self, seconds: float) -> None:
        """После ответа 429 не трогаем площадку указанное время."""
        self._next_allowed = max(self._next_allowed, self._clock() + seconds)


# ---------------------------------------------------------------- HTTP


def _snippet(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


class HttpClient:
    """Запросы через curl_cffi с отпечатком браузера (иначе Cloudflare отдаёт 403)."""

    def __init__(
        self,
        market: str,
        limiter: RateLimiter,
        *,
        proxy: str | None = None,
        impersonate: str = "chrome",
        timeout: float = 15.0,
    ):
        self.market = market
        self.limiter = limiter
        self._proxy = proxy
        self._impersonate = impersonate
        self._timeout = timeout
        self._session = None

    async def _get_session(self):
        if self._session is None:
            from curl_cffi.requests import AsyncSession

            proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
            self._session = AsyncSession(impersonate=self._impersonate, timeout=self._timeout, proxies=proxies)
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        allow_empty: bool = False,
    ) -> Any:
        """allow_empty — для торговых запросов: пустой ответ (204) считается успехом."""
        await self.limiter.wait()
        session = await self._get_session()
        started = time.monotonic()
        try:
            resp = await session.request(method, url, headers=headers, json=json_body)
        except Exception as exc:  # curl_cffi бросает свои типы ошибок
            raise TransportError(self.market, f"нет ответа: {type(exc).__name__}: {exc}") from exc

        status = resp.status_code
        log.debug("%s %s %s -> %s за %.2f с", self.market, method, url.split("?")[0], status, time.monotonic() - started)

        if status == 429:
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            self.limiter.penalize(retry_after or 30.0)
            raise RateLimited(self.market, f"HTTP 429, пауза {retry_after or 30:.0f} с", retry_after)
        if status in (401, 403):
            raise AuthError(
                self.market,
                f"HTTP {status}: вход истёк или запрос заблокирован (Cloudflare). Ответ: {_snippet(resp.text)}",
            )
        if status >= 500:
            raise TransportError(self.market, f"HTTP {status} на стороне площадки")
        if not 200 <= status < 300:
            raise HttpError(self.market, f"HTTP {status}: {_snippet(resp.text)}", status)
        if allow_empty and not resp.content.strip():
            return None
        try:
            return resp.json()
        except Exception as exc:
            raise SchemaError(self.market, f"ответ не JSON: {_snippet(resp.text)}") from exc

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(1.0, float(value))
    except ValueError:
        return None


# ---------------------------------------------------------------- разбор ответов


def as_price(market: str, value: Any, what: str = "price") -> float:
    if isinstance(value, bool):
        raise SchemaError(market, f"поле {what}: ожидалось число, пришло {value!r}")
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise SchemaError(market, f"поле {what}: ожидалось число, пришло {value!r}") from None
    if not math.isfinite(price) or price <= 0:
        raise SchemaError(market, f"поле {what}: недопустимая цена {value!r}")
    return price


def as_str(market: str, value: Any, what: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(market, f"поле {what}: ожидалась непустая строка, пришло {value!r}")
    return value.strip()


def as_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_items(market: str, items: Any, parse_one: Callable[[Any], T], what: str) -> list[T]:
    """Разбирает список записей. Если не разобралась бóльшая часть — формат изменился."""
    if not isinstance(items, list):
        raise SchemaError(market, f"{what}: ожидался список, пришло {type(items).__name__}")
    result: list[T] = []
    bad: list[str] = []
    checked = 0
    for item in items:
        try:
            result.append(parse_one(item))
            checked += 1
        except SkipItem:
            continue
        except (SchemaError, KeyError, TypeError, ValueError, AttributeError) as exc:
            checked += 1
            bad.append(str(exc))
    if bad:
        log.warning("%s: %s — не разобрано %d из %d записей, пример: %s", market, what, len(bad), checked, bad[0])
    if checked and len(bad) * 2 > checked:
        raise SchemaError(market, f"{what}: не разобрано {len(bad)} из {checked} записей ({bad[0]})")
    return result


def pick(item: dict, *names: str) -> Any:
    for name in names:
        if item.get(name) is not None:
            return item[name]
    return None


# ---------------------------------------------------------------- интерфейс площадки


class MarketAdapter(ABC):
    name: str = ""
    title: str = ""

    def __init__(self, http: HttpClient):
        self.http = http
        self.auth = ""

    def set_auth(self, auth: str) -> None:
        self.auth = auth

    @abstractmethod
    async def latest(self, limit: int) -> list[Listing]:
        """Последние выставленные лоты."""

    @abstractmethod
    async def refresh(self) -> None:
        """Обновить кэш коллекций и флоров."""

    @abstractmethod
    async def model_floor(self, key: Key) -> ModelFloor | None:
        """Флор модели из кэша. None — модели нет на площадке."""

    @abstractmethod
    async def cheapest(self, key: Key, limit: int) -> list[Listing] | None:
        """Самые дешёвые лоты модели прямо сейчас (запрос вживую). None — коллекция или модель площадке неизвестна."""

    # --- торговля (для автопокупки) ---

    def trading_problem(self) -> str | None:
        """Почему торговля на площадке невозможна прямо сейчас (None — всё готово)."""
        return None

    async def balance(self) -> BalanceInfo:
        raise NotImplementedError(f"{self.title}: торговля не поддерживается")

    async def buy(self, listing: Listing) -> None:
        """Купить лот ровно по цене listing.price. BuyRejected — площадка отказала, деньги не списаны."""
        raise NotImplementedError(f"{self.title}: торговля не поддерживается")

    async def owned(self) -> list[OwnedGift]:
        """Все ваши подарки на площадке: выставленные и нет (models.Inventory, если выдача могла обрезаться)."""
        raise NotImplementedError(f"{self.title}: торговля не поддерживается")

    async def list_for_sale(self, gift: OwnedGift, price: float) -> None:
        raise NotImplementedError(f"{self.title}: торговля не поддерживается")

    async def close(self) -> None:
        await self.http.close()


def format_price(price: float) -> str:
    """12.300000001 -> «12.3»: цена для торговых запросов без хвостов float."""
    return f"{price:.9f}".rstrip("0").rstrip(".")


def only_key(market: str, listings: Iterable[Listing], key: Key) -> list[Listing]:
    """Оставляет лоты нужной модели. Если площадка вернула чужие модели — фильтр в API сломался."""
    listings = list(listings)
    matched = [item for item in listings if item.key == key]
    if listings and (len(listings) - len(matched)) * 2 > len(listings):
        raise SchemaError(market, "фильтр по модели не сработал: площадка вернула лоты других моделей")
    return sorted(matched, key=lambda item: item.price)
