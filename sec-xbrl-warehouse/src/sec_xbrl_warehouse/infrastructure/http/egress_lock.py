"""One egress process per source, enforced with PostgreSQL advisory locks.

Rate limiters are per process. `SourceEgressLock` makes sure only one process
sends a given source's traffic: it takes a session-level `pg_try_advisory_lock`
on `xbrl:<source>-egress` over a dedicated connection and keeps that connection
open while it holds the lock. PostgreSQL releases the lock when the connection
ends, so a crashed holder never blocks the next process. Different sources use
different lock names and never block each other. No table is involved.

`SecEgressLock` is the `sec` lock (`xbrl:sec-egress`, unchanged) and raises
`SecEgressBusyError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import asyncpg
from sqlalchemy.engine import make_url

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.sources import SEC_SOURCE, is_source_code
from sec_xbrl_warehouse.exceptions import SecAccessError, SourceAccessError

LOCK_NAME = "xbrl:sec-egress"


def lock_name(source: str) -> str:
    """The advisory lock name for one source's egress (`xbrl:<source>-egress`)."""
    if not is_source_code(source):
        raise ValueError(f"Invalid source code: {source!r}")
    return f"xbrl:{source}-egress"


class EgressBusyError(SourceAccessError):
    """Another process holds this source's egress lock."""


class SecEgressBusyError(EgressBusyError, SecAccessError):
    """Another process holds the SEC egress lock."""


class EgressLock(Protocol):
    async def acquire(self) -> None: ...

    async def release(self) -> None: ...


class SourceEgressLock:
    """Session-level PostgreSQL advisory lock guarding one source's egress."""

    def __init__(
        self,
        settings: Settings,
        *,
        source: str,
        connect_timeout_seconds: float = 10.0,
        check_interval_seconds: float | None = None,
    ) -> None:
        self.source = source
        self.name = lock_name(source)
        url = make_url(settings.postgres_dsn.get_secret_value()).set(drivername="postgresql")
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
        """Take the lock without waiting; raise `EgressBusyError` if another holds it.

        Acquiring again while held is a no-op.
        """
        async with self._guard:
            if self._connection is not None:
                return
            connection = await asyncpg.connect(self._dsn, timeout=self._timeout)
            try:
                locked = await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", self.name
                )
            except BaseException:
                await connection.close()
                raise
            if not locked:
                await connection.close()
                raise self._busy()
            self._connection = connection
            self._checked_at = time.monotonic()

    async def verify(self) -> None:
        """Confirm the held lock is still ours, at most every `check_interval` seconds.

        A session lock lives exactly as long as its connection. If the connection
        dropped silently, PostgreSQL has released the lock and another process may
        hold it now, so the lock is re-acquired (raising `SecEgressBusyError` if
        that fails) before more requests are sent.
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
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", self.name
                )
            finally:
                await connection.close()

    def _busy(self) -> EgressBusyError:
        # Any lock on the `sec` source raises the SEC error, whichever class built it.
        if self.source == SEC_SOURCE:
            return SecEgressBusyError(
                "another process holds the SEC egress lock; only one process may "
                "send SEC requests at a time"
            )
        return EgressBusyError(
            f"another process holds the {self.source} egress lock; only one process may "
            f"send {self.source} requests at a time"
        )

    async def abandon(self) -> None:
        """Drop the session without unlocking, as a crashed holder would (tests)."""
        async with self._guard:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.terminate()


class SecEgressLock(SourceEgressLock):
    """The SEC egress lock (`xbrl:sec-egress`); a busy lock raises `SecEgressBusyError`."""

    def __init__(
        self,
        settings: Settings,
        *,
        connect_timeout_seconds: float = 10.0,
        check_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(
            settings,
            source=SEC_SOURCE,
            connect_timeout_seconds=connect_timeout_seconds,
            check_interval_seconds=check_interval_seconds,
        )


class LazyEgress:
    """Holds an egress lock from first use until `close` (no HTTP client needed).

    Parse-only workers in online Arelle mode use it: Arelle may resolve taxonomies
    from SEC hosts, and that traffic must count as this process's SEC egress.
    """

    def __init__(self, lock: EgressLock) -> None:
        self.lock = lock
        self._held = False
        self._guard = asyncio.Lock()

    async def ensure(self) -> None:
        if self._held:
            verify = getattr(self.lock, "verify", None)
            if verify is not None:
                await verify()
            return
        async with self._guard:
            if not self._held:
                await self.lock.acquire()
                self._held = True

    async def close(self) -> None:
        if self._held:
            self._held = False
            await self.lock.release()
