"""Запуск: python -m giftbot <команда>

  login   — войти в Telegram-аккаунт (один раз)
  chatid  — узнать свой chat_id для ADMIN_CHAT_ID
  check   — проверить бота и обе площадки одним запросом
  run     — запустить сканер
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import re
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth import AuthProvider, AuthSetupError, connect
from .config import Config, ConfigError, load_config
from .markets import HttpClient, MarketAdapter, MarketError, PortalsAdapter, RateLimiter, TonnelAdapter
from .notifier import TelegramBot
from .scanner import Scanner
from .store import Store
from .trader import Trader


def setup_logging(log_dir: Path, verbose: bool, console_level: int) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_dir / "giftbot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(console_level)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    for noisy in ("telethon", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_adapters(cfg: Config) -> dict[str, MarketAdapter]:
    adapters: dict[str, MarketAdapter] = {}
    for market in cfg.markets:
        limiter = RateLimiter(cfg.request_interval, jitter=cfg.request_interval / 2)
        http = HttpClient(market, limiter, proxy=cfg.proxy)
        if market == "portals":
            adapters[market] = PortalsAdapter(http, api_url=cfg.portals_api_url)
        elif market == "tonnel":
            adapters[market] = TonnelAdapter(
                http,
                page_url=cfg.tonnel_page_url,
                stats_url=cfg.tonnel_stats_url,
                balance_url=cfg.tonnel_balance_url,
                trade_url=cfg.tonnel_trade_url,
            )
    return adapters


async def cmd_login(cfg: Config) -> int:
    cfg.require("api_id", "api_hash")
    from telethon import TelegramClient

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash, receive_updates=False)
    first_login = False
    try:
        await connect(client)
        if not await client.is_user_authorized():
            first_login = True
            print("Вход в ваш Telegram-аккаунт: номер телефона, затем код из Telegram")
            print("и пароль, если включена двухэтапная проверка.")
            await client.start(
                phone=ask_phone,
                code_callback=lambda: input("Код из Telegram: "),
                password=lambda: getpass.getpass("Пароль двухэтапной проверки: "),
            )
        me = await client.get_me()
        if me.bot:  # бот не может открывать мини-приложения площадок
            await client.log_out()
            print("\n❌ Это вход ботом, а нужен ваш аккаунт. Запустите вход ещё раз и введите номер телефона.")
            return 1
    except Exception as exc:  # неверный номер или код, нет связи с Telegram, неверные API_ID/API_HASH
        print(f"\n❌ Вход не выполнен: {type(exc).__name__}: {exc}")
        await client.disconnect()
        return 1
    print(f"\n✅ Вход выполнен: {me.first_name}" + (f" (@{me.username})." if me.username else "."))
    print(f"Сессия сохранена в {cfg.session_path}.session — никому её не передавайте.")
    await client.disconnect()
    if first_login:
        print("\nОткройте с этого аккаунта мини-приложения @portals и @Tonnel_Network_bot хотя бы по разу.")
        try:
            input("Когда откроете, нажмите Enter… ")
        except EOFError:
            pass
    return 0


PHONE_RE = re.compile(r"\+?[\d\s()-]{7,20}")


def ask_phone() -> str:
    """Номер телефона для входа. Telethon принял бы сюда и токен бота — такой ввод не пропускаем."""
    while True:
        value = input("Номер телефона (+7…): ").strip()
        if PHONE_RE.fullmatch(value):
            return value
        print("Нужен номер телефона вашего Telegram-аккаунта, например +79991234567 (не токен бота).")


async def cmd_chatid(cfg: Config) -> int:
    cfg.require("bot_token")
    bot = TelegramBot(cfg.bot_token, None, proxy=cfg.proxy)
    print("Напишите своему боту в Telegram любое сообщение, например /start. Жду 2 минуты…")
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            try:
                updates = await bot.fetch_updates(timeout=20)
            except Exception as exc:
                print(f"❌ Telegram: {exc}. Проверьте BOT_TOKEN и что сервер видит api.telegram.org")
                return 1
            for update in updates:
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id") is not None:
                    print(f"\nВаш chat_id: {chat['id']}")
                    print("Впишите это число в ADMIN_CHAT_ID в файле .env")
                    return 0
    finally:
        await bot.close()
    print("Сообщений не пришло. Проверьте BOT_TOKEN и напишите боту ещё раз.")
    return 1


async def cmd_check(cfg: Config) -> int:
    cfg.require("bot_token", "admin_chat_id")
    ok = True
    bot = TelegramBot(cfg.bot_token, cfg.admin_chat_id, proxy=cfg.proxy)
    try:
        me = await bot.get_me()
        print(f"✅ Telegram-бот: @{me['username']}")
        if await bot.send("🔧 Проверка связи: сканер видит ваш чат."):
            print("✅ Тестовое сообщение отправлено")
        else:
            ok = False
            print("❌ Сообщение не ушло: проверьте ADMIN_CHAT_ID и что вы нажали /start у бота")
    except Exception as exc:
        ok = False
        print(f"❌ Telegram-бот: {exc}")
    finally:
        await bot.close()

    auth = AuthProvider(cfg)
    adapters = build_adapters(cfg)
    try:
        await auth.start(cfg.markets)
        for market, adapter in adapters.items():
            print(f"\n— {adapter.title} —")
            try:
                adapter.set_auth(await auth.get(market))
                print("✅ Вход получен")
                await adapter.refresh()
                print("✅ Коллекции и флоры загружены")
                items = await adapter.latest(10)
                print(f"✅ Последних лотов получено: {len(items)}")
                for item in items[:3]:
                    print(f"   {item.title()} — {item.price:.2f} TON")
                sample = next((i for i in items if i.key), None)
                if sample:
                    floor = await adapter.model_floor(sample.key)
                    cheapest = await adapter.cheapest(sample.key, cfg.verify_depth)
                    floor_text = f"{floor.price:.2f} TON" if floor else "нет данных"
                    print(f"✅ {sample.collection} — {sample.model}: флор {floor_text}, дешёвых лотов: {len(cheapest or [])}")
                if cfg.autobuy.enabled and market in cfg.autobuy.markets:
                    problem = adapter.trading_problem()
                    if problem:
                        ok = False
                        print(f"❌ Торговля: {problem}")
                    else:
                        info = await adapter.balance()
                        print(f"✅ Баланс: {info.available:.2f} TON")
                        if info.auto_withdraw:
                            ok = False
                            print("❌ Включён вывод купленного сразу в Telegram — выключите в приложении площадки")
                        gifts = await adapter.owned()
                        print(f"✅ Ваш инвентарь читается: подарков {len(gifts)}")
            except MarketError as error:
                ok = False
                print(f"❌ {error.human()}")
    finally:
        for adapter in adapters.values():
            await adapter.close()
        await auth.close()

    print("\nВсё в порядке, можно запускать: python -m giftbot run" if ok else "\nЕсть ошибки — см. выше и data/logs.")
    return 0 if ok else 1


async def cmd_run(cfg: Config) -> int:
    cfg.require("bot_token", "admin_chat_id")
    store = Store(cfg.db_path)
    bot = TelegramBot(cfg.bot_token, cfg.admin_chat_id, proxy=cfg.proxy)
    auth = AuthProvider(cfg)
    adapters = build_adapters(cfg)
    scanner = Scanner(cfg, adapters, auth, bot, store)
    scanner.trader = Trader(cfg, scanner)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, scanner.stop)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: остановка через Ctrl+C
    try:
        await auth.start(cfg.markets)
        await scanner.run()
    finally:
        for adapter in adapters.values():
            await adapter.close()
        await auth.close()
        await bot.close()
        store.close()
    return 0


COMMANDS = {"login": cmd_login, "chatid": cmd_chatid, "check": cmd_check, "run": cmd_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m giftbot", description="Сканер выгодных лотов Telegram-подарков")
    parser.add_argument("command", choices=list(COMMANDS), help="login | chatid | check | run")
    parser.add_argument("--env", default=".env", help="путь к файлу настроек (по умолчанию .env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.env)
    except ConfigError as exc:
        print(f"❌ Ошибка в настройках: {exc}")
        return 2
    setup_logging(cfg.log_dir, args.verbose, logging.INFO if args.command == "run" else logging.WARNING)
    try:
        return asyncio.run(COMMANDS[args.command](cfg))
    except (ConfigError, AuthSetupError) as exc:
        print(f"❌ {exc}")
        return 2
    except KeyboardInterrupt:
        return 130  # прерывание — не успех: install.sh не должен идти дальше


if __name__ == "__main__":
    sys.exit(main())
