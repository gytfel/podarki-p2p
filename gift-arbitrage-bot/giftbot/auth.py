"""Получение данных входа (initData) мини-приложений через ваш Telegram-аккаунт.

Площадки узнают пользователя по initData — той же строке, которую Telegram передаёт
мини-приложению при открытии. Её срок жизни ограничен, поэтому бот запрашивает её заново
по расписанию и при ошибке авторизации.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .markets.base import AuthError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebApp:
    bot: str  # юзернейм бота площадки
    short_name: str  # короткое имя мини-приложения из ссылки t.me/<bot>/<short_name>
    prefix: str  # что площадка ожидает перед initData


WEBAPPS = {
    "portals": WebApp(bot="portals", short_name="market", prefix="tma "),
    "tonnel": WebApp(bot="Tonnel_Network_bot", short_name="gifts", prefix=""),
}


class AuthSetupError(Exception):
    pass


def extract_init_data(url: str) -> str:
    """Достаёт tgWebAppData из ссылки, которую Telegram выдаёт для мини-приложения."""
    parts = urlsplit(url)
    for chunk in (parts.fragment, parts.query):
        params = parse_qs(chunk, keep_blank_values=True)
        if params.get("tgWebAppData"):
            return params["tgWebAppData"][0]
    raise ValueError("в ссылке мини-приложения нет tgWebAppData")


def normalize_override(market: str, value: str) -> str:
    prefix = WEBAPPS[market].prefix
    if prefix and not value.startswith(prefix):
        return prefix + value
    return value


class AuthProvider:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._overrides = {m: normalize_override(m, v) for m, v in cfg.auth_overrides.items()}
        self._client = None

    async def start(self, markets: tuple[str, ...]) -> None:
        if all(m in self._overrides for m in markets):
            return
        self._cfg.require("api_id", "api_hash")
        from telethon import TelegramClient

        self._client = TelegramClient(str(self._cfg.session_path), self._cfg.api_id, self._cfg.api_hash)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._client.disconnect()
            raise AuthSetupError("Telegram-аккаунт не авторизован. Сначала выполните: python -m giftbot login")

    async def get(self, market: str) -> str:
        if market in self._overrides:
            return self._overrides[market]
        if self._client is None:
            raise AuthError(market, "клиент Telegram не запущен")
        app = WEBAPPS[market]
        try:
            init_data = await self._request(app)
        except Exception as exc:
            raise AuthError(
                market,
                f"не удалось открыть мини-приложение @{app.bot}: {type(exc).__name__}: {exc}. "
                f"Откройте @{app.bot} в Telegram вручную хотя бы раз.",
            ) from exc
        log.info("%s: данные входа обновлены", market)
        return app.prefix + init_data

    async def _request(self, app: WebApp) -> str:
        from telethon.tl.functions.messages import RequestAppWebViewRequest
        from telethon.tl.types import InputBotAppShortName, InputUser

        peer = await self._client.get_input_entity(app.bot)
        bot = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
        result = await self._client(
            RequestAppWebViewRequest(
                peer=peer,
                app=InputBotAppShortName(bot_id=bot, short_name=app.short_name),
                platform="android",
            )
        )
        return extract_init_data(result.url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
