"""HTTP-клиент на настоящем локальном сервере: как коды ответов превращаются в ошибки."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from giftbot.markets.base import AuthError, HttpClient, HttpError, RateLimited, RateLimiter, SchemaError, TransportError

ROUTES = {
    "/ok": (200, {}, b'{"results": []}'),
    "/auth": (401, {}, b'{"message": "expired"}'),
    "/cf": (403, {}, b"<html>Just a moment...</html>"),
    "/slow-down": (429, {"Retry-After": "12"}, b""),
    "/boom": (502, {}, b"bad gateway"),
    "/teapot": (418, {}, b"teapot"),
    "/html": (200, {}, b"<html>maintenance</html>"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        status, headers, body = ROUTES[self.path]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def fetch(base, path, limiter=None):
    async def go():
        client = HttpClient("test", limiter or RateLimiter(0))
        try:
            return await client.request("GET", base + path)
        finally:
            await client.close()

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_ok_json(server):
    assert fetch(server, "/ok") == {"results": []}


@pytest.mark.parametrize(
    "path,error",
    [("/auth", AuthError), ("/cf", AuthError), ("/boom", TransportError), ("/teapot", HttpError), ("/html", SchemaError)],
)
def test_status_mapping(server, path, error):
    with pytest.raises(error):
        fetch(server, path)


def test_429_pauses_requests(server):
    limiter = RateLimiter(0)
    with pytest.raises(RateLimited) as info:
        fetch(server, "/slow-down", limiter)
    assert info.value.retry_after == 12
    assert limiter._next_allowed - limiter._clock() > 11


def test_unreachable_host_is_transport_error():
    with pytest.raises(TransportError):
        fetch("http://127.0.0.1:9", "/ok")
