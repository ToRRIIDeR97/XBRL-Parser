"""One SEC-active process at a time, enforced with a PostgreSQL advisory lock.

The SEC rate limiter is per process. `SecEgressLock` makes sure only one process
sends SEC traffic: it takes a session-level `pg_try_advisory_lock` on a dedicated
connection and keeps that connection open while it holds the lock. PostgreSQL
releases the lock when the connection ends, so a crashed holder never blocks
the next process. No table is involved.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import asyncpg
from sqlalchemy.engine import make_url

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import SecAccessError

LOCK_NAME = "xbrl:sec-egress"


class SecEgressBusyError(SecAccessError):
    """Another process holds the SEC egress lock."""


class EgressLock(Protocol):
    async def acquire(self) -> None: ...

    async def release(self) -> None: ...


class SecEgressLock:
    """Session-level PostgreSQL advisory lock guarding SEC egress for one process."""

    def __init__(
        self,
        settings: Settings,
        *,
        connect_timeout_seconds: float = 10.0,
        check_interval_seconds: float | None = None,
    ) -> None:
        url = make_url(settings.postgres_dsn).set(drivername="postgresql")
        self._dsn = url.render_as_string(hide_password=False)
        self._timeout = connect_timeout_seconds
        self._check_interval = (
            check_interval_seconds
            if check_interval_seconds is not None
            else settings.sec_egress_check_seconds
        )
        self._connection: Any = None
        self._checked_at = 0.0
        self._guard = asyncio.Lock()

    @property
    def held(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> None:
        """Take the lock without waiting; raise `SecEgressBusyError` if another holds it.

        Acquiring again while held is a no-op.
        """
        async with self._guard:
            if self._connection is not None:
                return
            connection = await asyncpg.connect(self._dsn, timeout=self._timeout)
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", LOCK_NAME
                )
            except BaseException:
                await connection.close()
                raise
            if not locked:
                await connection.close()
                raise SecEgressBusyError(
                    "another process holds the SEC egress lock; only one process may "
                    "send SEC requests at a time"
                )
            self._connection = connection
            self._checked_at = time.monotonic()

    async def verify(self) -> None:
        """Confirm the held lock is still ours, at most every `check_interval` seconds.

        A session lock lives exactly as long as its connection. If the connection
        dropped silently, PostgreSQL has released the lock and another process may
        hold it now, so the lock is re-acquired (raising `SecEgressBusyError` if
        that fails) before more SEC requests are sent.
        """
        if self._connection is None or time.monotonic() - self._checked_at < self._check_interval:
            return
        async with self._guard:
            connection = self._connection
            if connection is None:
                return
            try:
                held = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND granted)",
                    timeout=self._timeout,
                )
            except Exception:
                held = False
            if held:
                self._checked_at = time.monotonic()
                return
            self._connection = None
            connection.terminate()
        await self.acquire()

    async def release(self) -> None:
        """Unlock and close the dedicated connection. Safe to call when not held."""
        async with self._guard:
            connection, self._connection = self._connection, None
            if connection is None:
                return
            try:
                await connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", LOCK_NAME
                )
            finally:
                await connection.close()

    async def abandon(self) -> None:
        """Drop the session without unlocking, as a crashed holder would (tests)."""
        async with self._guard:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.terminate()
