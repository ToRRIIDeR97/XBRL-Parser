from __future__ import annotations

import asyncio
import time

from sec_xbrl_warehouse.observability.metrics import SEC_THROTTLE_SECONDS


class AsyncRateLimiter:
    """Process-local leaky-bucket limiter with evenly spaced requests.

    Use one shared instance per process for all SEC traffic. This deliberately
    avoids bursts even when the configured average rate would technically allow
    them.
    """

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        started = time.monotonic()
        try:
            async with self._lock:
                now = time.monotonic()
                wait_seconds = max(0.0, self._next_allowed - now)
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                    now = time.monotonic()
                self._next_allowed = max(now, self._next_allowed) + self._interval
        finally:
            SEC_THROTTLE_SECONDS.observe(time.monotonic() - started)

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None
