"""Расчёт выгоды сделки. Чистые функции без сети — их легко проверить тестами."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Listing


@dataclass(frozen=True)
class Fees:
    """Комиссии площадки в долях (0.05 = 5%)."""

    buy: float  # сколько покупатель доплачивает сверху к цене лота
    sell: float  # сколько площадка удерживает с продавца


@dataclass(frozen=True)
class Thresholds:
    min_profit_ton: float  # минимальная прибыль в TON
    min_profit_pct: float  # минимальная прибыль в долях от затрат (0.05 = 5%)
    undercut: float  # насколько дешевле самого дешёвого конкурента выставлять (0.01 = 1%)
    min_ref_listings: int  # сколько конкурирующих лотов должно быть, чтобы доверять цене
    anomaly_ratio: float  # цена ниже ref * ratio считается подозрительной
    transfer_cost_ton: float  # расходы на перенос подарка между площадками


@dataclass(frozen=True)
class Deal:
    listing: Listing
    sell_market: str
    ref_price: float  # самый дешёвый конкурент на площадке продажи
    ref_count: int | None  # сколько конкурентов видно (для оценки ликвидности)
    resale_price: float  # по какой цене выставлять
    cost: float  # сколько заплатим с комиссией
    proceeds: float  # сколько получим на руки после продажи
    transfer_cost: float

    @property
    def profit(self) -> float:
        return self.proceeds - self.cost - self.transfer_cost

    @property
    def profit_pct(self) -> float:
        return self.profit / self.cost if self.cost > 0 else 0.0

    @property
    def cross(self) -> bool:
        return self.sell_market != self.listing.market


def evaluate(
    listing: Listing,
    sell_market: str,
    ref_price: float,
    ref_count: int | None,
    fees: dict[str, Fees],
    th: Thresholds,
) -> Deal:
    buy_fees = fees[listing.market]
    sell_fees = fees[sell_market]
    cost = listing.price * (1 + buy_fees.buy)
    resale = ref_price * (1 - th.undercut)
    proceeds = resale * (1 - sell_fees.sell)
    transfer = th.transfer_cost_ton if sell_market != listing.market else 0.0
    return Deal(
        listing=listing,
        sell_market=sell_market,
        ref_price=ref_price,
        ref_count=ref_count,
        resale_price=resale,
        cost=cost,
        proceeds=proceeds,
        transfer_cost=transfer,
    )


def passes(deal: Deal, th: Thresholds, *, slack: float = 1.0, check_liquidity: bool = True) -> bool:
    """Проходит ли сделка пороги. slack < 1 ослабляет пороги для предварительного отбора."""
    if deal.profit < th.min_profit_ton * slack:
        return False
    if deal.profit_pct < th.min_profit_pct * slack:
        return False
    if check_liquidity and (deal.ref_count or 0) < th.min_ref_listings:
        return False
    return True


def is_anomaly(price: float, ref_price: float, th: Thresholds) -> bool:
    """Слишком низкая цена: ошибка данных, подмена или чья-то опечатка. Только ручная проверка."""
    return ref_price > 0 and price < ref_price * th.anomaly_ratio
