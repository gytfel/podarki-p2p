import asyncio
import json

import httpx
import pytest

from giftbot.auth import extract_init_data, normalize_override
from giftbot.config import ConfigError, load_config
from giftbot.notifier import TelegramBot


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ Telegram-бот


def make_bot(admin, fail=False):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("sendMessage"):
            sent.append(body)
            if fail:
                return httpx.Response(400, json={"ok": False, "description": "chat not found"})
        return httpx.Response(200, json={"ok": True, "result": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramBot("123:abc", admin, client=client, send_interval=0), sent


def update(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_commands_from_strangers_are_ignored():
    bot, sent = make_bot(admin=42)
    calls = []

    async def handler(cmd, args):
        calls.append(cmd)
        return "ok"

    run(bot.handle_update(update(999, "/pause"), handler))
    assert calls == [] and sent == []

    run(bot.handle_update(update(42, "/status@my_bot extra"), handler))
    assert calls == ["/status"]
    assert sent[0]["chat_id"] == 42 and sent[0]["text"] == "ok"


def test_without_admin_bot_tells_chat_id():
    bot, sent = make_bot(admin=None)

    async def handler(cmd, args):  # pragma: no cover
        raise AssertionError

    run(bot.handle_update(update(777, "/start"), handler))
    assert "777" in sent[0]["text"]


def test_send_failure_does_not_raise():
    bot, sent = make_bot(admin=42, fail=True)
    assert run(bot.send("hello")) is False
    assert len(sent) == 2  # одна повторная попытка


# ------------------------------------------------------------------ авторизация


def test_extract_init_data_from_webapp_url():
    url = (
        "https://portal-market.com/#tgWebAppData=query_id%3DAA%26user%3D%257B%2522id%2522%253A1%257D"
        "%26auth_date%3D1%26hash%3Dabc&tgWebAppVersion=8.0&tgWebAppPlatform=android"
    )
    assert extract_init_data(url) == "query_id=AA&user=%7B%22id%22%3A1%7D&auth_date=1&hash=abc"
    with pytest.raises(ValueError):
        extract_init_data("https://example.com/#nothing=1")


def test_manual_auth_gets_portals_prefix():
    assert normalize_override("portals", "query_id=1") == "tma query_id=1"
    assert normalize_override("portals", "tma query_id=1") == "tma query_id=1"
    assert normalize_override("tonnel", "query_id=1") == "query_id=1"


# ------------------------------------------------------------------ настройки


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in ["MARKETS", "MIN_PROFIT_PCT", "TONNEL_BUY_FEE_PCT", "VERIFY_DEPTH", "MIN_REF_LISTINGS",
                 "PORTALS_AUTH_DATA", "AUTO_RESUME", "MAX_BUY_PRICE_TON", "ADMIN_CHAT_ID", "AUTOBUY_MODE",
                 "AUTOBUY_MARKETS", "AUTOBUY_MIN_PROFIT_PCT", "AUTOBUY_MAX_PRICE_TON", "AUTOBUY_DAILY_LIMIT_TON"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch, tmp_path / "missing.env"


def test_config_defaults_and_percent_conversion(env):
    monkeypatch, path = env
    monkeypatch.setenv("MIN_PROFIT_PCT", "7,5")
    monkeypatch.setenv("TONNEL_BUY_FEE_PCT", "6")
    monkeypatch.setenv("PORTALS_AUTH_DATA", "query_id=1")
    monkeypatch.setenv("AUTO_RESUME", "нет")
    cfg = load_config(path)
    assert cfg.markets == ("portals", "tonnel")
    assert cfg.thresholds.min_profit_pct == pytest.approx(0.075)
    assert cfg.fees["tonnel"].buy == pytest.approx(0.06)
    assert cfg.auth_overrides == {"portals": "query_id=1"}
    assert cfg.auto_resume is False


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("MARKETS", "portals,fragment", "неизвестные площадки"),
        ("MIN_PROFIT_PCT", "много", "ожидается число"),
        ("VERIFY_DEPTH", "3", "VERIFY_DEPTH"),
        ("MAX_BUY_PRICE_TON", "0.5", "MAX_BUY_PRICE_TON"),
        ("ADMIN_CHAT_ID", "me", "целое число"),
    ],
)
def test_config_errors_are_readable(env, name, value, message):
    monkeypatch, path = env
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_autobuy_config(env):
    monkeypatch, path = env
    for name in ("AUTOBUY_MODE", "AUTOBUY_MARKETS", "AUTOBUY_MAX_PRICE_TON", "AUTOBUY_DAILY_LIMIT_TON"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(path)
    assert cfg.autobuy.mode == "off" and not cfg.autobuy.enabled
    assert cfg.autobuy.markets == ("portals",)  # Tonnel — только если включить явно

    monkeypatch.setenv("AUTOBUY_MODE", "LIVE")
    monkeypatch.setenv("AUTOBUY_MARKETS", "portals,tonnel")
    monkeypatch.setenv("AUTOBUY_MIN_PROFIT_PCT", "12")
    cfg = load_config(path)
    assert cfg.autobuy.enabled and cfg.autobuy.markets == ("portals", "tonnel")
    assert cfg.autobuy.min_profit_pct == pytest.approx(0.12)

    monkeypatch.setenv("AUTOBUY_MODE", "yes")
    with pytest.raises(ConfigError, match="off, dry или live"):
        load_config(path)

    monkeypatch.setenv("AUTOBUY_MODE", "live")
    monkeypatch.setenv("MARKETS", "portals")
    with pytest.raises(ConfigError, match="не включены в MARKETS"):
        load_config(path)

    monkeypatch.setenv("MARKETS", "portals,tonnel")
    monkeypatch.setenv("AUTOBUY_MAX_PRICE_TON", "40")
    with pytest.raises(ConfigError, match="AUTOBUY_MAX_PRICE_TON"):
        load_config(path)


def test_scanner_on_tonnel_only_starts_with_autobuy_off(env):
    monkeypatch, path = env
    monkeypatch.setenv("MARKETS", "tonnel")  # AUTOBUY_MARKETS по умолчанию portals
    cfg = load_config(path)
    assert cfg.markets == ("tonnel",)
    assert cfg.autobuy.markets == ()
