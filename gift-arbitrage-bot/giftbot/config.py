"""Настройки из файла .env (образец — .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .evaluator import Fees, Thresholds

KNOWN_MARKETS = ("portals", "tonnel")

ENV_NAMES = {
    "bot_token": "BOT_TOKEN",
    "admin_chat_id": "ADMIN_CHAT_ID",
    "api_id": "API_ID",
    "api_hash": "API_HASH",
}


class ConfigError(Exception):
    pass


def _get(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            raise ConfigError(f"{name}: ожидается число, указано «{raw}»") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"{name}: {value} вне допустимого диапазона [{lo}; {hi}]")
    return value


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    value = _float(name, default, lo, hi)
    if value != int(value):
        raise ConfigError(f"{name}: ожидается целое число")
    return int(value)


def _optional_int(name: str) -> int | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидается целое число, указано «{raw}»") from None


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on", "да"):
        return True
    if raw.lower() in ("0", "false", "no", "off", "нет"):
        return False
    raise ConfigError(f"{name}: ожидается true или false, указано «{raw}»")


def _pct(name: str, default: float, hi: float = 100.0) -> float:
    """Проценты в .env -> доли в коде."""
    return _float(name, default, 0.0, hi) / 100.0


AUTOBUY_MODES = ("off", "dry", "live")


@dataclass(frozen=True)
class AutobuyConfig:
    mode: str  # off — выключена, dry — только «купил бы», live — реальные покупки
    markets: tuple[str, ...]  # где разрешено покупать
    cross: bool  # покупать ли под перепродажу на другой площадке
    min_profit_ton: float
    min_profit_pct: float  # доля
    max_price: float  # максимум за одну покупку, TON (с комиссией)
    daily_limit: float  # сколько можно потратить за 24 часа, TON
    max_per_day: int
    max_open: int  # сколько купленных и ещё не проданных подарков может висеть одновременно
    reserve: float  # сколько TON всегда оставлять на балансе
    cooldown: float  # пауза между покупками, секунд
    autolist: bool  # сразу выставлять купленное на продажу
    positions_check: float  # как часто проверять, продались ли подарки, секунд

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class Config:
    bot_token: str | None
    admin_chat_id: int | None
    api_id: int | None
    api_hash: str | None
    data_dir: Path
    markets: tuple[str, ...]
    fees: dict[str, Fees]
    thresholds: Thresholds
    min_buy_price: float
    max_buy_price: float
    poll_interval: float
    poll_jitter: float
    request_interval: float
    floors_refresh: float
    auth_refresh: float
    max_errors: int
    probe_interval: float
    auto_resume: bool
    latest_limit: int
    verify_depth: int
    proxy: str | None
    auth_overrides: dict[str, str]
    portals_api_url: str
    tonnel_page_url: str
    tonnel_stats_url: str
    tonnel_balance_url: str
    tonnel_trade_url: str
    autobuy: AutobuyConfig

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telegram"  # Telethon допишет .session

    @property
    def db_path(self) -> Path:
        return self.data_dir / "giftbot.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def require(self, *fields: str) -> None:
        missing = [ENV_NAMES.get(f, f.upper()) for f in fields if getattr(self, f) in (None, "")]
        if missing:
            raise ConfigError("Заполните в .env: " + ", ".join(missing))


def load_config(env_file: str | Path = ".env") -> Config:
    from dotenv import load_dotenv

    load_dotenv(env_file, override=False)

    markets = tuple(m.strip().lower() for m in (_get("MARKETS") or "portals,tonnel").split(",") if m.strip())
    unknown = [m for m in markets if m not in KNOWN_MARKETS]
    if unknown or not markets:
        raise ConfigError(f"MARKETS: неизвестные площадки {unknown}. Доступны: {', '.join(KNOWN_MARKETS)}")

    fees = {
        # Portals: комиссия уже в цене лота, продавец получает цену минус комиссия
        "portals": Fees(buy=_pct("PORTALS_BUY_FEE_PCT", 0, 50), sell=_pct("PORTALS_SELL_FEE_PCT", 5, 50)),
        # Tonnel: в API цена без комиссии, покупатель доплачивает её сверху
        "tonnel": Fees(buy=_pct("TONNEL_BUY_FEE_PCT", 10, 50), sell=_pct("TONNEL_SELL_FEE_PCT", 0, 50)),
    }

    min_ref = _int("MIN_REF_LISTINGS", 3, 1, 20)
    verify_depth = _int("VERIFY_DEPTH", 6, 2, 30)
    if verify_depth < min_ref + 1:
        raise ConfigError("VERIFY_DEPTH должен быть хотя бы на 1 больше MIN_REF_LISTINGS")

    thresholds = Thresholds(
        min_profit_ton=_float("MIN_PROFIT_TON", 0.5, 0),
        min_profit_pct=_pct("MIN_PROFIT_PCT", 5, 1000),
        undercut=_pct("UNDERCUT_PCT", 1, 50),
        min_ref_listings=min_ref,
        anomaly_ratio=_pct("ANOMALY_RATIO_PCT", 25, 100),
        transfer_cost_ton=_float("TRANSFER_COST_TON", 0.3, 0),
    )

    min_buy = _float("MIN_BUY_PRICE_TON", 1, 0)
    max_buy = _float("MAX_BUY_PRICE_TON", 50, 0)
    if max_buy <= min_buy:
        raise ConfigError("MAX_BUY_PRICE_TON должен быть больше MIN_BUY_PRICE_TON")

    autobuy = _load_autobuy(markets)

    overrides = {}
    for market in KNOWN_MARKETS:
        value = _get(f"{market.upper()}_AUTH_DATA")
        if value:
            overrides[market] = value

    return Config(
        bot_token=_get("BOT_TOKEN"),
        admin_chat_id=_optional_int("ADMIN_CHAT_ID"),
        api_id=_optional_int("API_ID"),
        api_hash=_get("API_HASH"),
        data_dir=Path(_get("DATA_DIR") or "data"),
        markets=markets,
        fees=fees,
        thresholds=thresholds,
        min_buy_price=min_buy,
        max_buy_price=max_buy,
        poll_interval=_float("POLL_INTERVAL_SEC", 8, 2),
        poll_jitter=_float("POLL_JITTER_SEC", 4, 0),
        request_interval=_float("MIN_REQUEST_INTERVAL_SEC", 1.5, 0.3),
        floors_refresh=_float("FLOORS_REFRESH_SEC", 180, 30),
        auth_refresh=_float("AUTH_REFRESH_SEC", 1800, 60),
        max_errors=_int("MAX_CONSECUTIVE_ERRORS", 5, 1, 100),
        probe_interval=_float("PROBE_INTERVAL_SEC", 300, 30),
        auto_resume=_bool("AUTO_RESUME", True),
        latest_limit=_int("LATEST_LIMIT", 30, 5, 30),
        verify_depth=verify_depth,
        proxy=_get("PROXY"),
        auth_overrides=overrides,
        portals_api_url=_get("PORTALS_API_URL") or "https://portal-market.com/api/",
        tonnel_page_url=_get("TONNEL_PAGE_URL") or "https://gifts2.tonnel.network/api/pageGifts",
        tonnel_stats_url=_get("TONNEL_STATS_URL") or "https://gifts3.tonnel.network/api/filterStats",
        tonnel_balance_url=_get("TONNEL_BALANCE_URL") or "https://gifts2.tonnel.network/api/balance/info",
        tonnel_trade_url=_get("TONNEL_TRADE_URL") or "https://gifts.coffin.meme/api/",
        autobuy=autobuy,
    )


def _load_autobuy(markets: tuple[str, ...]) -> AutobuyConfig:
    mode = (_get("AUTOBUY_MODE") or "off").lower()
    if mode not in AUTOBUY_MODES:
        raise ConfigError(f"AUTOBUY_MODE: ожидается off, dry или live, указано «{mode}»")
    buy_markets = tuple(
        m.strip().lower() for m in (_get("AUTOBUY_MARKETS") or "portals").split(",") if m.strip()
    )
    outside = [m for m in buy_markets if m not in markets]
    if outside and mode != "off":
        raise ConfigError(f"AUTOBUY_MARKETS: площадки {outside} не включены в MARKETS")
    # Автопокупка выключена: площадки вне MARKETS не мешают запуску сканера
    buy_markets = tuple(m for m in buy_markets if m in markets)
    if mode != "off" and not buy_markets:
        raise ConfigError("AUTOBUY_MARKETS: укажите хотя бы одну площадку из MARKETS")
    max_price = _float("AUTOBUY_MAX_PRICE_TON", 10, 0)
    daily_limit = _float("AUTOBUY_DAILY_LIMIT_TON", 30, 0)
    if mode != "off" and max_price > daily_limit:
        raise ConfigError("AUTOBUY_MAX_PRICE_TON не может быть больше AUTOBUY_DAILY_LIMIT_TON")
    return AutobuyConfig(
        mode=mode,
        markets=buy_markets,
        cross=_bool("AUTOBUY_CROSS", False),
        min_profit_ton=_float("AUTOBUY_MIN_PROFIT_TON", 1, 0),
        min_profit_pct=_pct("AUTOBUY_MIN_PROFIT_PCT", 10, 1000),
        max_price=max_price,
        daily_limit=daily_limit,
        max_per_day=_int("AUTOBUY_MAX_PER_DAY", 5, 1, 1000),
        max_open=_int("AUTOBUY_MAX_OPEN", 3, 1, 1000),
        reserve=_float("AUTOBUY_RESERVE_TON", 0.5, 0),
        cooldown=_float("AUTOBUY_COOLDOWN_SEC", 60, 0),
        autolist=_bool("AUTOLIST", True),
        positions_check=_float("POSITIONS_CHECK_SEC", 300, 60),
    )
