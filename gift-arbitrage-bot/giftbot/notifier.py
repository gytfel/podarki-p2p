"""Telegram-бот для уведомлений и команд. Слушается только владельца (ADMIN_CHAT_ID)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, list[str]], Awaitable[str | None]]


class TelegramBot:
    def __init__(
        self,
        token: str,
        admin_chat_id: int | None,
        *,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
        send_interval: float = 1.0,
    ):
        self.admin_chat_id = admin_chat_id
        self._send_interval = send_interval
        self._base = f"https://api.telegram.org/bot{token}/"
        self._client = client or httpx.AsyncClient(timeout=40.0, proxy=proxy)
        self._send_lock = asyncio.Lock()
        self._offset = 0

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(self._base + method, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method}: {data.get('description', resp.status_code)}")
        return data

    async def get_me(self) -> dict:
        return (await self._call("getMe"))["result"]

    async def send(self, text: str, chat_id: int | None = None) -> bool:
        """Отправляет сообщение. Никогда не бросает исключений — уведомление не должно ронять сканер."""
        chat_id = chat_id or self.admin_chat_id
        if chat_id is None:
            log.warning("ADMIN_CHAT_ID не задан, сообщение не отправлено: %s", text[:200])
            return False
        async with self._send_lock:  # не больше ~1 сообщения в секунду в один чат
            for attempt in range(2):
                try:
                    await self._call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=text[:4096],
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    await asyncio.sleep(self._send_interval)
                    return True
                except Exception as exc:
                    log.warning("Не удалось отправить сообщение в Telegram (попытка %d): %s", attempt + 1, exc)
                    await asyncio.sleep(self._send_interval * 3)
        return False

    async def fetch_updates(self, timeout: int = 30) -> list[dict]:
        data = await self._call("getUpdates", offset=self._offset, timeout=timeout, allowed_updates=["message"])
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def handle_update(self, update: dict, handler: CommandHandler) -> None:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text.startswith("/"):
            return
        if self.admin_chat_id is None:
            await self.send(f"Ваш chat_id: <code>{chat_id}</code>\nВпишите его в ADMIN_CHAT_ID в .env.", chat_id)
            return
        if chat_id != self.admin_chat_id:
            log.warning("Команда из чужого чата %s проигнорирована", chat_id)
            return
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        reply = await handler(command, parts[1:])
        if reply:
            await self.send(reply, chat_id)

    async def poll_commands(self, handler: CommandHandler) -> None:
        while True:
            try:
                for update in await self.fetch_updates():
                    await self.handle_update(update, handler)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Ошибка получения команд Telegram: %s", exc)
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self._client.aclose()
