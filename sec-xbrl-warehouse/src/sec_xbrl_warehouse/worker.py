from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.models import JobRecord
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SchemaDriftError
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.logging import get_logger

logger = get_logger(__name__)
JobHandler = Callable[[JobRecord], Awaitable[dict[str, Any] | None]]


class Worker:
    """PostgreSQL-backed worker with graceful shutdown and stage handlers."""

    def __init__(
        self,
        settings: Settings,
        control: PostgresControlRepository,
        handlers: dict[JobType, JobHandler],
    ) -> None:
        self.settings = settings
        self.control = control
        self.handlers = handlers
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal_name, self.stop)
        while not self._stopping.is_set():
            job = await self.control.claim_job(
                self.settings.worker_id,
                self.settings.worker_lock_timeout_seconds,
            )
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    continue
                continue
            await self._execute(job)

    async def _execute(self, job: JobRecord) -> None:
        handler = self.handlers.get(job.job_type)
        if handler is None:
            await self.control.fail_job(
                job.id,
                f"No handler registered for {job.job_type.value}",
                retryable=False,
            )
            return
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            result = await handler(job)
            await self.control.complete_job(job.id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("job_failed", job_id=str(job.id), job_type=job.job_type.value)
            retryable = is_retryable_exception(exc)
            await self.control.fail_job(job.id, str(exc), retryable=retryable)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job: JobRecord) -> None:
        interval = max(5.0, self.settings.worker_lock_timeout_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self.control.heartbeat_job(job.id, self.settings.worker_id)


def build_handlers(
    *,
    settings: Settings | None = None,
    services: dict[str, Any] | None = None,
) -> dict[JobType, JobHandler]:
    """Construct concrete handlers and their dependencies.

    Each handler is thin: validate the versioned payload, call one application
    service, and return a small checkpoint/result object. Pipeline logic stays in
    the application services.
    """
    handlers: dict[JobType, JobHandler] = {}
    if services is None:
        return handlers
    orchestrator = services.get("orchestrator")
    ingestor = services.get("ingestor")
    mapper = services.get("mapper")

    if orchestrator is not None:

        async def _download_bulk(job: JobRecord) -> dict[str, Any] | None:
            result: dict[str, Any] | None = await orchestrator.run_artifact_job(job.payload)
            return result

        async def _reconcile(job: JobRecord) -> dict[str, Any] | None:
            artifact_id = str(job.payload.get("artifact_id") or "")
            expected = job.payload.get("expected_rows")
            result: dict[str, Any] | None = await orchestrator.reconcile_artifact(
                artifact_id, expected_rows=expected if isinstance(expected, dict) else None
            )
            return result

        handlers[JobType.DOWNLOAD_BULK] = _download_bulk
        handlers[JobType.RECONCILE_ARTIFACT] = _reconcile

    if ingestor is not None:

        async def _ingest_filing(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.domain.models import FilingRef

            filing = FilingRef.model_validate(job.payload["filing"])
            revision = int(job.payload.get("extraction_revision", 1))
            result: dict[str, Any] | None = await ingestor.ingest(
                filing, extraction_revision=revision
            )
            return result

        handlers[JobType.INGEST_FILING] = _ingest_filing

    if mapper is not None:

        async def _map_canonical(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.domain.models import FilingRef
            from sec_xbrl_warehouse.domain.xbrl import XbrlFact

            filing = FilingRef.model_validate(job.payload["filing"])
            facts = tuple(XbrlFact.model_validate(item) for item in job.payload.get("facts", []))
            canonical = await mapper.map_batch(
                facts,
                filing,
                extraction_revision=int(job.payload.get("extraction_revision", 1)),
            )
            return {"mapped": len(canonical), "unmapped": len(facts) - len(canonical)}

        handlers[JobType.MAP_CANONICAL] = _map_canonical
    return handlers


def is_retryable_exception(exc: Exception) -> bool:
    """Classify network/timeout/temporary DB failures explicitly."""
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.HTTPStatusError,
        ),
    ):
        return True
    if isinstance(exc, (ArtifactIntegrityError, SchemaDriftError)):
        return False
    # PostgreSQL transient error classes surface through SQLAlchemy.
    module = type(exc).__module__
    if module.startswith("sqlalchemy"):
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("timeout", "connection", "locked", "deadlock", "serialization")
        )
    return False
