"""Telegram-бот для уведомлений и команд. Слушается только владельца (ADMIN_CHAT_ID).

Команды можно напечатать, нажать в меню под полем ввода или кнопкой под сообщением (см. ui.py).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

import httpx

from .ui import COMMANDS, MENU, Button, Reply

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, list[str]], Awaitable["str | Reply | None"]]

STALE_QUERY_ERRORS = ("query is too old", "query ID is invalid")
STALE_PRESS_TEXT = (
    "⌛ Кнопку нажали, пока бот был недоступен, — это нажатие не выполнено. "
    "Откройте нужное меню заново и нажмите ещё раз."
)


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.split()
    return parts[0].split("@")[0].lower(), parts[1:]


def inline_markup(buttons: Sequence[Sequence[Button]]) -> dict:
    """Кнопки под сообщением. Пустой список убирает кнопки при редактировании."""
    return {"inline_keyboard": [[{"text": label, "callback_data": command} for label, command in row] for row in buttons]}


class TelegramBot:
    def __init__(
        self,
        token: str,
        admin_chat_id: int | None,
        *,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
        send_interval: float = 1.0,
        menu: Sequence[Sequence[Button]] = MENU,
        commands: Sequence[tuple[str, str]] = COMMANDS,
    ):
        self.admin_chat_id = admin_chat_id
        self._send_interval = send_interval
        self._base = f"https://api.telegram.org/bot{token}/"
        self._client = client or httpx.AsyncClient(timeout=40.0, proxy=proxy)
        self._send_lock = asyncio.Lock()
        self._offset = 0
        # меню под полем ввода: надпись кнопки приходит обычным текстом, переводим её в команду
        self._menu_markup = (
            {"keyboard": [[{"text": label} for label, _ in row] for row in menu], "resize_keyboard": True, "is_persistent": True}
            if menu
            else None
        )
        self._menu_commands = {label: command for row in menu for label, command in row}
        self._commands = commands

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(self._base + method, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method}: {data.get('description', resp.status_code)}")
        return data

    async def get_me(self) -> dict:
        return (await self._call("getMe"))["result"]

    async def send(
        self,
        text: str,
        chat_id: int | None = None,
        *,
        buttons: Sequence[Sequence[Button]] = (),
        menu: bool = False,
    ) -> bool:
        """Отправляет сообщение. Никогда не бросает исключений — уведомление не должно ронять сканер.

        buttons — кнопки под сообщением, menu — показать меню под полем ввода (одно из двух).
        """
        chat_id = chat_id or self.admin_chat_id
        if chat_id is None:
            log.warning("ADMIN_CHAT_ID не задан, сообщение не отправлено: %s", text[:200])
            return False
        extra: dict = {}
        if buttons:
            extra["reply_markup"] = inline_markup(buttons)
        elif menu and self._menu_markup:
            extra["reply_markup"] = self._menu_markup
        async with self._send_lock:  # не больше ~1 сообщения в секунду в один чат
            for attempt in range(2):
                try:
                    await self._call(
                        "sendMessage",
                        chat_id=chat_id,
                        text=text[:4096],
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        **extra,
                    )
                    await asyncio.sleep(self._send_interval)
                    return True
                except Exception as exc:
                    log.warning("Не удалось отправить сообщение в Telegram (попытка %d): %s", attempt + 1, exc)
                    await asyncio.sleep(self._send_interval * 3)
        return False

    async def send_reply(self, reply: str | Reply, chat_id: int | None = None) -> bool:
        """Ответ на команду. К ответу без своих кнопок прикладываем меню, чтобы оно всегда было под рукой."""
        if isinstance(reply, Reply):
            return await self.send(reply.text, chat_id, buttons=reply.buttons, menu=not reply.buttons)
        return await self.send(reply, chat_id, menu=True)

    async def edit(self, chat_id: int, message_id: int, reply: str | Reply) -> bool:
        """Заменяет сообщение с кнопкой новым ответом. False — заменить не вышло, нужно прислать новое."""
        text, buttons = (reply.text, reply.buttons) if isinstance(reply, Reply) else (reply, ())
        async with self._send_lock:
            try:
                await self._call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[:4096],
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=inline_markup(buttons),
                )
            except Exception as exc:
                if "message is not modified" in str(exc):
                    return True  # нажали «Обновить», а ничего не изменилось
                log.warning("Не удалось обновить сообщение в Telegram: %s", exc)
                return False
            await asyncio.sleep(self._send_interval)
            return True

    async def answer_callback(self, callback_id: str | None, text: str | None = None) -> bool:
        """Подтверждает нажатие кнопки: иначе в Telegram на ней долго крутятся «часики».

        False — Telegram считает нажатие устаревшим (его сделали давно, например пока бот был выключен).
        """
        if not callback_id:
            return True
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        try:
            await self._call("answerCallbackQuery", **params)
        except Exception as exc:
            log.debug("Не удалось ответить на нажатие кнопки: %s", exc)
            return not any(sign in str(exc) for sign in STALE_QUERY_ERRORS)
        return True

    async def setup_commands(self) -> None:
        """Меню «/» в Telegram со списком команд."""
        if not self._commands:
            return
        try:
            await self._call(
                "setMyCommands", commands=[{"command": name, "description": text} for name, text in self._commands]
            )
        except Exception as exc:
            log.warning("Не удалось задать меню команд в Telegram: %s", exc)

    async def fetch_updates(self, timeout: int = 30) -> list[dict]:
        data = await self._call(
            "getUpdates", offset=self._offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def handle_update(self, update: dict, handler: CommandHandler) -> None:
        if update.get("callback_query"):
            await self._handle_callback(update["callback_query"], handler)
            return
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        text = self._menu_commands.get(text, text)  # кнопка меню — это её команда
        if chat_id is None or not text.startswith("/"):
            return
        if self.admin_chat_id is None:
            await self.send(f"Ваш chat_id: <code>{chat_id}</code>\nВпишите его в ADMIN_CHAT_ID в .env.", chat_id)
            return
        if chat_id != self.admin_chat_id:
            log.warning("Команда из чужого чата %s проигнорирована", chat_id)
            return
        command, args = parse_command(text)
        reply = await handler(command, args)
        if reply:
            await self.send_reply(reply, chat_id)

    async def _handle_callback(self, query: dict, handler: CommandHandler) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = query.get("data") or ""
        if self.admin_chat_id is None or chat_id != self.admin_chat_id:
            log.warning("Нажатие кнопки из чужого чата %s проигнорировано", chat_id)
            await self.answer_callback(query.get("id"))
            return
        as_new = data.startswith("+")  # «+» — ответ отдельным сообщением, не трогая сообщение с кнопкой
        line = data[1:] if as_new else data
        if not line.startswith("/"):
            await self.answer_callback(query.get("id"), "Кнопка устарела")
            return
        if not await self.answer_callback(query.get("id")):
            # старое нажатие не выполняем: со временем обстановка могла измениться (это может быть «Да, включить»)
            log.warning("Устаревшее нажатие кнопки %r пропущено", data)
            await self.send(STALE_PRESS_TEXT, chat_id)
            return
        command, args = parse_command(line)
        reply = await handler(command, args)
        if not reply:
            return
        message_id = message.get("message_id")
        if as_new or message_id is None or not await self.edit(chat_id, message_id, reply):
            await self.send_reply(reply, chat_id)

    async def _handle_safely(self, update: dict, handler: CommandHandler) -> None:
        """Сбой одной команды не должен терять остальные из той же пачки."""
        try:
            await self.handle_update(update, handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Сбой при обработке команды из Telegram")

    async def skip_stale_presses(self, handler: CommandHandler) -> None:
        """При запуске: нажатия кнопок, накопившиеся, пока бот был выключен, не выполняем.
        Напечатанные команды выполняем как обычно."""
        stale = 0
        try:
            while updates := await self.fetch_updates(timeout=0):
                for update in updates:
                    query = update.get("callback_query")
                    if query is None:
                        await self._handle_safely(update, handler)
                        continue
                    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
                    await self.answer_callback(query.get("id"))
                    if chat_id == self.admin_chat_id:
                        stale += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Не удалось разобрать накопившиеся команды Telegram: %s", exc)
        if stale:
            log.warning("Пропущено нажатий кнопок, сделанных до запуска: %d", stale)
            await self.send(STALE_PRESS_TEXT)

    async def poll_commands(self, handler: CommandHandler) -> None:
        await self.setup_commands()
        await self.skip_stale_presses(handler)
        while True:
            try:
                for update in await self.fetch_updates():
                    await self._handle_safely(update, handler)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Ошибка получения команд Telegram: %s", exc)
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self._client.aclose()
