import asyncio
from datetime import date

import httpx
import pytest

from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.models import FilingRef, JobRecord
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    SchemaDriftError,
    StateTransitionError,
)
from sec_xbrl_warehouse.infrastructure.storage.postgres import current_checkpoint_lease
from sec_xbrl_warehouse.worker import build_handlers, is_retryable_exception


def test_is_retryable_classifies_network_and_schema_errors() -> None:
    assert is_retryable_exception(TimeoutError("t")) is True
    assert is_retryable_exception(ConnectionError("c")) is True
    assert is_retryable_exception(ArtifactIntegrityError("bad zip")) is False
    assert is_retryable_exception(SchemaDriftError("header drift")) is False
    assert is_retryable_exception(ValueError("plain")) is False


def test_build_handlers_requires_runtime_services() -> None:
    with pytest.raises(ValueError, match="services"):
        build_handlers()


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.reconciled = []

    async def download_artifact_job(self, payload, *, job_id=None):
        return {"artifact_id": "A", "job_id": str(job_id)}

    async def run_artifact_job(self, payload, *, job_id=None):
        return {"artifact_id": "A"}

    async def reconcile_artifact(self, artifact_id, *, expected_rows=None):
        self.reconciled.append((artifact_id, expected_rows))
        return {"artifact_id": artifact_id}


def _job(job_type: JobType, payload=None) -> JobRecord:
    from datetime import UTC, datetime
    from uuid import uuid4

    return JobRecord(
        id=uuid4(),
        job_type=job_type,
        state="pending",
        idempotency_key=f"k-{job_type.value}",
        payload=payload or {},
        attempts=0,
        max_attempts=5,
        priority=100,
        available_at=datetime.now(UTC),
    )


def test_build_handlers_wires_orchestrator() -> None:
    orchestrator = _FakeOrchestrator()
    handlers = build_handlers(services={"orchestrator": orchestrator})
    assert set(handlers) >= {JobType.DOWNLOAD_BULK, JobType.RECONCILE_ARTIFACT}
    job = _job(JobType.DOWNLOAD_BULK, {"artifact": {"dataset": "notes"}})
    result = asyncio.run(handlers[JobType.DOWNLOAD_BULK](job))
    assert result == {"artifact_id": "A", "job_id": str(job.id)}
    asyncio.run(
        handlers[JobType.RECONCILE_ARTIFACT](
            _job(JobType.RECONCILE_ARTIFACT, {"artifact_id": "X", "expected_rows": {"sub": 1}})
        )
    )
    assert orchestrator.reconciled == [("X", {"sub": 1})]


def test_worker_executes_and_completes_job() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        def __init__(self) -> None:
            self.completed = []
            self.failed = []

        async def heartbeat_job(self, job_id, worker_id):
            return None

        async def complete_job(self, job_id, result=None, *, worker_id=None, attempt=None):
            self.completed.append((job_id, result))

        async def fail_job(self, job_id, error, retryable, *, worker_id=None, attempt=None):
            self.failed.append((job_id, error, retryable))

    control = _Control()
    settings = Settings(
        env="test",
        worker_poll_seconds=0.01,
        worker_lock_timeout_seconds=60,
    )

    async def handler(job):
        return {"ok": True}

    worker = Worker(settings, control, {JobType.DOWNLOAD_BULK: handler})
    job = _job(JobType.DOWNLOAD_BULK)
    asyncio.run(worker._execute(job))
    assert control.completed and control.completed[0][0] == job.id
    assert not control.failed


def test_worker_propagates_checkpoint_lease_to_handler_task() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        async def complete_job(self, *args, **kwargs):
            return None

    seen = []

    async def handler(job):
        seen.append(current_checkpoint_lease())
        return None

    job = _job(JobType.DOWNLOAD_BULK)
    worker = Worker(Settings(env="test"), _Control(), {JobType.DOWNLOAD_BULK: handler})
    asyncio.run(worker._execute(job))
    assert seen == [job]
    assert current_checkpoint_lease() is None


def test_worker_fails_job_on_handler_error() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        async def heartbeat_job(self, job_id, worker_id):
            return None

        async def complete_job(self, job_id, result=None, *, worker_id=None, attempt=None):
            raise AssertionError("should not complete")

        async def fail_job(self, job_id, error, retryable, *, worker_id=None, attempt=None):
            self.captured = (job_id, error, retryable)

    control = _Control()

    async def handler(job):
        raise SchemaDriftError("boom")

    worker = Worker(
        Settings(env="test", worker_poll_seconds=0.01, worker_lock_timeout_seconds=60),
        control,
        {JobType.DOWNLOAD_BULK: handler},
    )
    job = _job(JobType.DOWNLOAD_BULK)
    asyncio.run(worker._execute(job))
    assert control.captured[2] is False  # SchemaDrift is not retryable


def test_worker_cancels_handler_when_lease_is_lost() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        async def complete_job(self, *args, **kwargs):
            raise AssertionError("lost worker must not complete")

        async def fail_job(self, *args, **kwargs):
            raise AssertionError("lost worker must not fail the reclaimed job")

    cancelled = False

    async def handler(job):
        nonlocal cancelled
        try:
            await asyncio.Future()
        finally:
            cancelled = True

    worker = Worker(Settings(env="test"), _Control(), {JobType.DOWNLOAD_BULK: handler})  # type: ignore[arg-type]

    async def lost(job):
        raise StateTransitionError("reclaimed")

    worker._heartbeat = lost  # type: ignore[method-assign]
    asyncio.run(worker._execute(_job(JobType.DOWNLOAD_BULK)))
    assert cancelled


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example",
        form="10-K",
        filed_date=date(2026, 1, 2),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def test_build_handlers_wires_incremental_mapping_and_discovery() -> None:
    class _Ingestor:
        async def ingest(self, filing, *, extraction_revision, job_id):
            return {"accession": filing.accession, "revision": extraction_revision}

        async def reconcile(self, accession):
            return {"accession": accession}

    class _Mapper:
        async def map_batches(self, facts, filing, *, extraction_revision, batch_bytes):
            self.received = (tuple(facts), filing, extraction_revision)
            yield ()

    class _Warehouse:
        def insert_canonical_facts(self, facts):
            self.facts = facts
            return len(facts)

    class _Discovery:
        async def discover_range(self, filed_from, filed_to, forms):
            self.received = (filed_from, filed_to, forms)
            return (_filing(),)

    class _Control:
        def __init__(self):
            self.jobs = []

        async def enqueue_job(self, **kwargs):
            self.jobs.append(kwargs)

    mapper = _Mapper()
    warehouse = _Warehouse()
    discovery = _Discovery()
    control = _Control()
    handlers = build_handlers(
        services={
            "ingestor": _Ingestor(),
            "mapper": mapper,
            "warehouse": warehouse,
            "discovery": discovery,
            "forms": ["10-K"],
            "control": control,
        }
    )
    filing_payload = _filing().model_dump(mode="json")

    ingested = asyncio.run(
        handlers[JobType.INGEST_FILING](
            _job(JobType.INGEST_FILING, {"filing": filing_payload, "extraction_revision": 2})
        )
    )
    assert ingested == {"accession": _filing().accession, "revision": 2}
    reconciled = asyncio.run(
        handlers[JobType.RECONCILE_FILING](
            _job(JobType.RECONCILE_FILING, {"accession": _filing().accession})
        )
    )
    assert reconciled == {"accession": _filing().accession}
    mapped = asyncio.run(
        handlers[JobType.MAP_CANONICAL](
            _job(JobType.MAP_CANONICAL, {"filing": filing_payload, "facts": []})
        )
    )
    assert mapped == {"mapped": 0, "unmapped": 0}
    discovered = asyncio.run(
        handlers[JobType.DISCOVER_FILINGS](
            _job(
                JobType.DISCOVER_FILINGS,
                {"filed_from": "2026-01-01", "filed_to": "2026-01-02"},
            )
        )
    )
    assert discovered == {"filings_enqueued": 1}
    assert control.jobs[0]["idempotency_key"] == "filing:0000000001-26-000001:1"


def test_worker_handles_missing_handler_and_stops_while_idle() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        def __init__(self) -> None:
            self.failed = []

        async def claim_job(self, worker_id, lock_timeout_seconds, job_types=None):
            return None

        async def fail_job(self, *args, **kwargs):
            self.failed.append((args, kwargs))

    control = _Control()
    worker = Worker(
        Settings(env="test", worker_poll_seconds=0.01),
        control,
        {},  # type: ignore[arg-type]
    )
    asyncio.run(worker._execute(_job(JobType.DOWNLOAD_BULK)))
    assert control.failed

    async def run_until_stopped() -> None:
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0)
        worker.stop()
        await task

    asyncio.run(run_until_stopped())


def test_retryable_exception_unwraps_causes_and_database_errors() -> None:
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = httpx.ReadTimeout("timeout")
    assert is_retryable_exception(wrapped)

    class DatabaseError(Exception):
        pass

    DatabaseError.__module__ = "sqlalchemy.exc"
    assert is_retryable_exception(DatabaseError("deadlock detected"))
    assert not is_retryable_exception(DatabaseError("invalid statement"))


def test_worker_claims_only_requested_job_types() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    requested = []

    class _Control:
        async def claim_job(self, worker_id, lock_timeout_seconds, job_types=None):
            requested.append(job_types)
            worker.stop()
            return None

    worker = Worker(
        Settings(env="test", worker_poll_seconds=0.01),
        _Control(),  # type: ignore[arg-type]
        {},
        [JobType.INGEST_FILING],
    )
    asyncio.run(worker.run())
    assert requested == [(JobType.INGEST_FILING,)]
