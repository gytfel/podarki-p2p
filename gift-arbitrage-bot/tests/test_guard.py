import asyncio

from giftbot.guard import MarketHealth, hint_for
from giftbot.markets.base import AuthError, RateLimiter, SchemaError, TransportError


def test_trips_after_consecutive_errors_and_success_resets_counter():
    health = MarketHealth(max_errors=3)
    assert not health.fail(TransportError("tonnel", "timeout"), 1)
    health.ok(2)
    assert health.consecutive == 0
    assert not health.fail(TransportError("tonnel", "timeout"), 3)
    assert not health.fail(TransportError("tonnel", "timeout"), 4)
    assert health.fail(TransportError("tonnel", "timeout"), 5)  # третья подряд — стоп
    assert health.tripped
    assert not health.fail(TransportError("tonnel", "timeout"), 6)  # повторно не сообщаем
    health.reset()
    assert not health.tripped and health.consecutive == 0


def test_schema_error_trips_immediately():
    health = MarketHealth(max_errors=5)
    assert health.fail(SchemaError("portals", "нет поля results"), 1)
    assert health.tripped


def test_hints_name_the_market_file():
    assert "giftbot/markets/portals.py" in hint_for(SchemaError("portals", "x"))
    assert "login" in hint_for(AuthError("tonnel", "x"))


def test_rate_limiter_spaces_requests_and_respects_penalty():
    now = [100.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=fake_sleep)

    async def scenario():
        await limiter.wait()
        await limiter.wait()
        limiter.penalize(30)
        await limiter.wait()

    asyncio.run(scenario())
    assert slept == [2.0, 30.0]
