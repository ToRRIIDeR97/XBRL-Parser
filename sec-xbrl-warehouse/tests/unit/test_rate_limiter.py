import time

import pytest

from sec_xbrl_warehouse.infrastructure.http.rate_limiter import AsyncRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_spaces_requests() -> None:
    limiter = AsyncRateLimiter(20)
    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - start >= 0.04
