"""Диагностика: что площадки отвечают с этого сервера.

Запуск: python -m giftbot diag (на сервере: bash install.sh diag). Печатает короткий отчёт и сохраняет его
в data/diag.txt. Ключей, данных входа и полного IP-адреса в отчёте нет — им можно делиться.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlencode

from .auth import AuthProvider
from .config import Config
from .markets.base import SchemaError
from .markets.portals import HEADERS as PORTALS_HEADERS
from .markets.portals import parse_filter_models
from .markets.tonnel import HEADERS as TONNEL_HEADERS
from .markets.tonnel import SORT_LATEST

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
IMPERSONATE = ("chrome", "chrome124", "chrome110", "safari")
# заголовки настоящего Chrome, как в tonnelmp: на случай, если Cloudflare смотрит на них
BROWSER_HEADERS = {
    "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36",
}
CF_CODES = {
    "1006": "IP-адрес сервера заблокирован",
    "1007": "IP-адрес сервера заблокирован",
    "1008": "IP-адрес сервера заблокирован",
    "1009": "заблокирована страна, в которой находится сервер",
    "1010": "заблокирован «отпечаток» браузера",
    "1012": "доступ запрещён",
    "1015": "слишком частые запросы",
    "1020": "сработало правило защиты сайта",
}


def shape(value: Any, depth: int = 0) -> str:
    """Структура JSON без лишних подробностей: ключи, типы, короткие примеры значений."""
    pad = "  " * depth
    if isinstance(value, dict):
        if depth >= 5:
            return f"{{…{len(value)} ключей}}"
        items = list(value.items())
        lines = [f"{{{len(value)} ключей"]
        for key, item in items[:8]:
            lines.append(f"{pad}  {key!r}: {shape(item, depth + 1)}")
        if len(items) > 8:
            lines.append(f"{pad}  … ещё {len(items) - 8}: {', '.join(repr(k) for k, _ in items[8:20])}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        return f"[{len(value)} шт., первый: {shape(value[0], depth + 1)}]" if value else "[]"
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "…"


def cloudflare_verdict(status: int, text: str, headers: Any) -> str:
    if 200 <= status < 300:
        return "ok"
    low = text.lower()
    code = re.search(r"error(?:\s+code)?:?\s*(?:<[^>]+>\s*)*(\d{4})", text, re.I)
    ray = re.search(r"ray id:?\s*(?:<[^>]+>\s*)*([0-9a-f]{16})", text, re.I)
    if "just a moment" in low or (headers.get("cf-mitigated") or "") == "challenge":
        verdict = "Cloudflare: проверка браузера (JS-challenge)"
    elif "you have been blocked" in low or "attention required" in low:
        verdict = "Cloudflare: доступ заблокирован"
    elif "cloudflare" in low and ("access denied" in low or "error" in low):
        verdict = "Cloudflare: доступ запрещён"
    elif re.match(r"\s*error code:\s*1\d{3}", text, re.I):
        verdict = "Cloudflare: доступ запрещён"
    else:
        return f"не Cloudflare: {' '.join(text.split())[:150]}"
    if code:
        verdict += f", код {code.group(1)} — {CF_CODES.get(code.group(1), 'см. справку Cloudflare')}"
    country = re.search(r"\(([A-Z]{2})\)", text) if code and code.group(1) == "1009" else None
    if country:
        verdict += f" ({country.group(1)})"
    if ray:
        verdict += f", Ray ID {ray.group(1)}"
    return verdict


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)


async def fetch(method: str, url: str, *, impersonate: str = "chrome", proxy: str | None = None, **kwargs):
    """Сырой запрос: (код, заголовки, текст) или текст ошибки соединения."""
    from curl_cffi.requests import AsyncSession

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        async with AsyncSession(impersonate=impersonate, timeout=20, proxies=proxies) as session:
            resp = await session.request(method, url, **kwargs)
            return resp.status_code, resp.headers, resp.text
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"


async def server_location(report: Report, proxy: str | None) -> None:
    status, _, text = await fetch("GET", TRACE_URL, proxy=proxy)
    if status != 200:
        report.add(f"Сервер: не удалось определить ({text[:100]})")
        return
    info = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    ip = info.get("ip", "?")
    masked = ".".join(ip.split(".")[:2] + ["x", "x"]) if ip.count(".") == 3 else ip[:9] + "…"
    via = " (через PROXY)" if proxy else ""
    report.add(f"Сервер{via}: страна {info.get('loc', '?')}, IP {masked}, узел Cloudflare {info.get('colo', '?')}")


async def diag_portals(report: Report, cfg: Config, auth: str) -> None:
    report.add("\n— Portals —")
    base = cfg.portals_api_url if cfg.portals_api_url.endswith("/") else cfg.portals_api_url + "/"
    headers = {**PORTALS_HEADERS, "Authorization": auth}

    async def get(path: str, params: dict) -> tuple[int | None, Any, str]:
        url = base + path + ("?" + urlencode(params, safe=",") if params else "")
        status, _, text = await fetch("GET", url, headers=headers, proxy=cfg.proxy_for("portals"))
        try:
            return status, json.loads(text), text
        except ValueError:
            return status, None, text

    status, data, text = await get("collections", {"offset": 0, "limit": 3})
    collections = data.get("collections") if isinstance(data, dict) else None
    if status != 200 or not collections:
        report.add(f"collections: HTTP {status}, {' '.join(text.split())[:200]}")
        return
    first = collections[0]
    name, short, cid = first.get("name"), first.get("short_name"), first.get("id")
    report.add(f"collections: ok, пример: {name!r} short_name={short!r}")
    for path, params in (
        ("collections/filters", {"short_names": short}),
        ("collections/filters", {"collection_ids": cid}),
        ("collections/floors", {}),
    ):
        await asyncio.sleep(1.5)
        status, data, text = await get(path, params)
        label = f"{path}?{urlencode(params)}" if params else path
        report.add(f"\n{label}: HTTP {status}")
        report.add(shape(data) if data is not None else " ".join(text.split())[:300])
        if "short_names" in params and data is not None:
            report.add(f"Разбор ботом: {check_models(data, short)}")


def check_models(data: Any, short_name: str) -> str:
    """Как бот разберёт модели из этого ответа — то же, что делает сканер."""
    try:
        models = parse_filter_models("portals", data, short_name)
    except SchemaError as exc:
        return f"❌ {exc}"
    priced = [(name, price) for name, price in models.values() if price is not None]
    if not models:
        return "❌ моделей не найдено"
    sample = f", пример: {priced[0][0]} — {priced[0][1]:.2f} TON" if priced else ""
    return f"✅ моделей {len(models)}, с ценой {len(priced)}{sample}"


async def diag_tonnel(report: Report, cfg: Config, auth: str) -> None:
    report.add("\n— Tonnel —")
    page = {
        "filter": json.dumps({"price": {"$exists": True}, "buyer": {"$exists": False}, "asset": "TON"}),
        "limit": 1,
        "page": 1,
        "sort": SORT_LATEST,
        "ref": 0,
        "price_range": 0,
        "user_auth": auth,
    }
    stats = {"authData": auth}
    # второй адрес флоров для сравнения: gifts2 <-> gifts3
    other = re.sub(r"//gifts(\d)\.", lambda m: "//gifts" + ("3" if m.group(1) == "2" else "2") + ".", cfg.tonnel_stats_url)
    targets = [
        ("pageGifts", cfg.tonnel_page_url, page),
        ("filterStats", cfg.tonnel_stats_url, stats),
    ]
    if other != cfg.tonnel_stats_url:
        host = re.search(r"//([^/]+)", other)
        targets.append((f"filterStats ({host.group(1) if host else other})", other, stats))
    variants = [(imp, TONNEL_HEADERS) for imp in IMPERSONATE] + [("chrome110", {**TONNEL_HEADERS, **BROWSER_HEADERS})]
    blocked = False
    for name, url, payload in targets:
        if blocked:
            report.add(f"{name}: не проверяю — IP уже заблокирован, лишние запросы только продлевают блокировку")
            continue
        for imp, headers in variants:
            await asyncio.sleep(1.0)
            status, resp_headers, text = await fetch(
                "POST", url, impersonate=imp, headers=headers, json=payload, proxy=cfg.proxy_for("tonnel")
            )
            label = f"{name} · {imp}" + (" + заголовки браузера" if headers is not TONNEL_HEADERS else "")
            if status is None:
                report.add(f"{label}: нет ответа ({text[:120]})")
                continue
            verdict = cloudflare_verdict(status, text, resp_headers)
            report.add(f"{label}: HTTP {status} — {verdict}")
            if 200 <= status < 300:
                try:
                    report.add(shape(json.loads(text)))
                except ValueError:
                    report.add(" ".join(text.split())[:200])
                break  # этот адрес отвечает — остальные варианты не нужны
            if "JS-challenge" not in verdict:
                # другой «отпечаток браузера» помогает только против проверки браузера, не против блокировки
                blocked = verdict.startswith("Cloudflare")
                break


async def run_diag(cfg: Config) -> int:
    report = Report()
    report.add("Диагностика площадок (ключи и данные входа в отчёт не попадают)")
    await server_location(report, cfg.proxy)
    if cfg.proxy_for("tonnel") != cfg.proxy:
        await server_location(report, cfg.proxy_for("tonnel"))  # куда ведёт TONNEL_PROXY
    provider = AuthProvider(cfg)
    try:
        await provider.start(cfg.markets)
        for market, runner in (("portals", diag_portals), ("tonnel", diag_tonnel)):
            if market not in cfg.markets:
                continue
            try:
                auth = await provider.get(market)
            except Exception as exc:
                report.add(f"\n— {market} —\nВход не получен: {exc}")
                continue
            await runner(report, cfg, auth)
    finally:
        await provider.close()
    path = cfg.data_dir / "diag.txt"
    path.write_text("\n".join(report.lines) + "\n", encoding="utf-8")
    print(f"\nОтчёт сохранён: {path}")
    return 0
