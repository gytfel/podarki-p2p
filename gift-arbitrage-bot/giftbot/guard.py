"""Аварийный стоп: считает ошибки площадки и выключает её при подозрительном поведении."""
from __future__ import annotations

from dataclasses import dataclass, field

from .markets.base import AuthError, Blocked, MarketError, RateLimited, SchemaError

HINTS = {
    Blocked: "Cloudflare площадки не пускает IP вашего сервера — обычно так блокируют IP дата-центров, "
    "повторы только продлевают блокировку. Выходы: прокси с «домашним» (резидентским) IP только для этой "
    "площадки ({market_upper}_PROXY в .env), другой VPS или работа без неё (MARKETS). "
    "Отчёт о блокировке: bash install.sh diag.",
    SchemaError: "Похоже, площадка обновила API. Посмотрите лог data/logs/giftbot.log и поправьте разбор "
    "в giftbot/markets/{market}.py.",
    AuthError: "Обновите вход: python -m giftbot login (на сервере — снова запустите install.sh), "
    "либо данные авторизации в .env. "
    "Если ошибка 403 повторяется — запросы может блокировать Cloudflare.",
    RateLimited: "Площадка просит реже: увеличьте {market_upper}_POLL_INTERVAL_SEC (или общий POLL_INTERVAL_SEC) "
    "и MIN_REQUEST_INTERVAL_SEC.",
}


def hint_for(error: MarketError) -> str:
    for cls, text in HINTS.items():
        if isinstance(error, cls):
            return text.format(market=error.market, market_upper=error.market.upper())
    return "Площадка или сеть недоступны. Обычно это проходит само."


@dataclass
class MarketHealth:
    max_errors: int
    consecutive: int = 0
    tripped: bool = False
    tripped_at: float | None = None
    last_ok: float | None = None
    last_error: MarketError | None = None
    last_probe: float = 0.0
    waiting_resume: bool = False  # площадка снова отвечает, ждём /resume
    failed_probes: int = 0  # неудачных пробных запросов подряд: пробуем всё реже
    total_errors: int = field(default=0)

    def ok(self, now: float) -> None:
        self.consecutive = 0
        self.last_ok = now

    def fail(self, error: MarketError, now: float) -> bool:
        """Учитывает ошибку. Возвращает True, если площадка только что выключена."""
        self.consecutive += 1
        self.total_errors += 1
        self.last_error = error
        # Изменившемуся формату ответа доверять нельзя ни секунды, а блокировку повторы только продлевают — стоп сразу
        limit_reached = self.consecutive >= self.max_errors or isinstance(error, (SchemaError, Blocked))
        if not self.tripped and limit_reached:
            self.tripped = True
            self.tripped_at = now
            self.last_probe = now
            return True
        return False

    def reset(self) -> None:
        self.consecutive = 0
        self.tripped = False
        self.tripped_at = None
        self.waiting_resume = False
        self.failed_probes = 0
