"""Worker-visible acceptance tests for 09-sec-egress-lock."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient


class _FakeLock:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> None:
        from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError

        if self.busy:
            raise SecEgressBusyError("another process holds the SEC egress lock")
        self.acquired += 1

    async def release(self) -> None:
        self.released += 1


def _client(lock, calls: list[str]) -> SecHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(env="test", sec_max_requests_per_second=10)
    return SecHttpClient(settings, transport, egress_lock=lock)


# AC-2
async def test_ac2_lock_taken_once_before_first_request_and_released_on_exit() -> None:
    lock, calls = _FakeLock(), []
    async with _client(lock, calls) as http:
        assert lock.acquired == 0
        await http.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
        await http.get_json("https://data.sec.gov/submissions/CIK0000789019.json")
    assert lock.acquired == 1
    assert lock.released == 1
    assert len(calls) == 2


# AC-2
async def test_ac2_busy_lock_blocks_request_before_it_is_sent() -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError

    lock, calls = _FakeLock(busy=True), []
    async with _client(lock, calls) as http:
        with pytest.raises(SecEgressBusyError):
            await http.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
    assert calls == []


# AC-1
@pytest.mark.integration
def test_ac1_second_process_is_refused_until_first_releases(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError, SecEgressLock

    async def run() -> list[str]:
        first, second = SecEgressLock(postgres_settings), SecEgressLock(postgres_settings)
        events = []
        await first.acquire()
        events.append("first")
        try:
            await second.acquire()
            events.append("second-unexpected")
        except SecEgressBusyError:
            events.append("second-busy")
        await first.release()
        await second.acquire()
        events.append("second")
        await second.release()
        return events

    assert asyncio.run(run()) == ["first", "second-busy", "second"]
