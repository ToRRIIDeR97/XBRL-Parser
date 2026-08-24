import asyncio

from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.models import JobRecord
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SchemaDriftError
from sec_xbrl_warehouse.worker import build_handlers, is_retryable_exception


def test_is_retryable_classifies_network_and_schema_errors() -> None:
    assert is_retryable_exception(TimeoutError("t")) is True
    assert is_retryable_exception(ConnectionError("c")) is True
    assert is_retryable_exception(ArtifactIntegrityError("bad zip")) is False
    assert is_retryable_exception(SchemaDriftError("header drift")) is False
    assert is_retryable_exception(ValueError("plain")) is False


def test_build_handlers_without_services_returns_empty() -> None:
    assert build_handlers() == {}


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.reconciled = []

    async def run_artifact_job(self, payload):
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
    result = asyncio.run(
        handlers[JobType.DOWNLOAD_BULK](
            _job(JobType.DOWNLOAD_BULK, {"artifact": {"dataset": "notes"}})
        )
    )
    assert result == {"artifact_id": "A"}
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

        async def complete_job(self, job_id, result=None):
            self.completed.append((job_id, result))

        async def fail_job(self, job_id, error, retryable):
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


def test_worker_fails_job_on_handler_error() -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.worker import Worker

    class _Control:
        async def heartbeat_job(self, job_id, worker_id):
            return None

        async def complete_job(self, job_id, result=None):
            raise AssertionError("should not complete")

        async def fail_job(self, job_id, error, retryable):
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
