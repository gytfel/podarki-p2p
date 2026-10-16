"""Общие структуры данных: лот, флор модели и нормализация названий."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# «Wizard (1.5%)» -> «Wizard». Tonnel пишет редкость в скобках, Portals — нет.
_RARITY_RE = re.compile(r"\s*\(\s*[\d.,]+\s*%\s*\)\s*$")
_SPACES_RE = re.compile(r"\s+")

Key = tuple[str, str]  # (коллекция, модель) в нормализованном виде


def strip_rarity(name: str) -> str:
    return _RARITY_RE.sub("", name).strip()


def normalize(name: str) -> str:
    """Приводит название к виду, одинаковому на всех площадках."""
    name = strip_rarity(name)
    name = name.replace("’", "'").replace("`", "'")
    name = _SPACES_RE.sub(" ", name)
    return name.strip().casefold()


def make_key(collection: str | None, model: str | None) -> Key | None:
    if not collection or not model:
        return None
    return normalize(collection), normalize(model)


@dataclass(frozen=True)
class Listing:
    """Подарок, выставленный на продажу."""

    market: str  # "portals" | "tonnel"
    listing_id: str  # ID лота на площадке
    collection: str  # название коллекции, например «Toy Bear»
    model: str | None  # модель без процента редкости
    price: float  # цена в TON так, как её отдаёт API площадки
    number: int | None = None  # номер подарка в Telegram
    backdrop: str | None = None
    symbol: str | None = None
    locked_until: datetime | None = None  # до какого времени лот нельзя вывести

    @property
    def key(self) -> Key | None:
        return make_key(self.collection, self.model)

    def title(self) -> str:
        text = self.collection
        if self.model:
            text += f" — {self.model}"
        if self.number is not None:
            text += f" #{self.number}"
        return text


@dataclass(frozen=True)
class ModelFloor:
    """Минимальная цена модели на площадке (из кэша)."""

    price: float
    count: int | None = None  # сколько лотов модели в продаже, если площадка это сообщает


@dataclass(frozen=True)
class BalanceInfo:
    available: float  # сколько TON можно потратить прямо сейчас
    auto_withdraw: bool = False  # площадка сразу выводит купленное в Telegram (Tonnel)


@dataclass(frozen=True)
class OwnedGift:
    """Подарок в вашем инвентаре на площадке."""

    market: str
    owned_id: str  # ID для выставления на продажу
    collection: str
    model: str | None
    number: int | None
    listed_price: float | None  # None — не выставлен

    def matches(self, collection: str, number: int | None, owned_id: str | None = None) -> bool:
        if owned_id and self.owned_id == owned_id:
            return True
        return number is not None and self.number == number and normalize(self.collection) == normalize(collection)
