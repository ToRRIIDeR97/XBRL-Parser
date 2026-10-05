"""Independent verification tests for 09-sec-egress-lock."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient


# AC-1: a crashed holder (connection gone without release) frees the lock.
@pytest.mark.integration
def test_ac1_lock_is_freed_when_holder_connection_dies(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressLock

    async def run() -> bool:
        holder, waiter = SecEgressLock(postgres_settings), SecEgressLock(postgres_settings)
        await holder.acquire()
        await holder.abandon()  # drop the session without unlocking, as a crash would
        await waiter.acquire()
        await waiter.release()
        return True

    assert asyncio.run(run())


# AC-1: acquiring twice in one holder is idempotent and one release frees it for others.
@pytest.mark.integration
def test_ac1_reacquire_is_idempotent(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressLock

    async def run() -> bool:
        holder, other = SecEgressLock(postgres_settings), SecEgressLock(postgres_settings)
        await holder.acquire()
        await holder.acquire()
        await holder.release()
        await other.acquire()
        await other.release()
        return True

    assert asyncio.run(run())


# AC-3: end to end, the second client is refused before any SEC request is sent.
@pytest.mark.integration
def test_ac3_second_client_sends_nothing(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError, SecEgressLock

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    settings = Settings(env="test")

    async def run() -> tuple[int, bool]:
        first = SecHttpClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)), egress_lock=SecEgressLock(postgres_settings))
        second = SecHttpClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)), egress_lock=SecEgressLock(postgres_settings))
        async with first, second:
            await first.get_json("https://www.sec.gov/files/company_tickers.json")
            sent_before = len(calls)
            refused = False
            try:
                await second.get_json("https://www.sec.gov/files/company_tickers.json")
            except SecEgressBusyError:
                refused = True
            return len(calls) - sent_before, refused

    sent, refused = asyncio.run(run())
    assert refused and sent == 0


# AC-4: clients that never contact the SEC never take the lock (non-SEC workers can coexist).
async def test_ac4_unused_client_never_acquires() -> None:
    class _Lock:
        acquired = 0
        released = 0

        async def acquire(self) -> None:
            self.acquired += 1

        async def release(self) -> None:
            self.released += 1

    lock = _Lock()
    async with SecHttpClient(Settings(env="test"), httpx.AsyncClient(), egress_lock=lock):
        pass
    assert lock.acquired == 0 and lock.released == 0


# AC-5: a rejected URL (non-SEC host) does not take the lock either.
async def test_ac5_invalid_url_is_rejected_before_locking() -> None:
    from sec_xbrl_warehouse.exceptions import SecAccessError

    class _Lock:
        acquired = 0

        async def acquire(self) -> None:
            self.acquired += 1

        async def release(self) -> None:
            pass

    lock = _Lock()
    async with SecHttpClient(Settings(env="test"), httpx.AsyncClient(), egress_lock=lock) as http:
        with pytest.raises(SecAccessError):
            await http.get_json("https://example.com/not-sec.json")
    assert lock.acquired == 0
