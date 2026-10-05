"""Worker-visible acceptance tests for 15-fetch-parse-split."""

from __future__ import annotations

import asyncio

from support15 import Control, Extractor, NoNetworkStore, Warehouse, Writer, filing, job, manifest, wait_until

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor
from sec_xbrl_warehouse.worker import Worker, build_handlers


# AC-1
def test_ac1_fetch_job_stores_package_and_enqueues_parse() -> None:
    class _Ingestor:
        async def fetch(self, filing, *, job_id=None):
            return manifest()

    control = Control()
    handlers = build_handlers(services={"ingestor": _Ingestor(), "control": control})
    payload = {"filing": filing().model_dump(mode="json"), "extraction_revision": 3}
    asyncio.run(handlers[JobType.FETCH_FILING](job(JobType.FETCH_FILING, payload)))

    (parse,) = control.jobs
    assert parse["job_type"] == JobType.PARSE_FILING.value
    assert parse["payload"] == {"accession": filing().accession, "extraction_revision": 3}
    assert parse["idempotency_key"] == f"parse:{filing().accession}:3"


# AC-2
def test_ac2_parse_uses_the_stored_package_and_never_downloads() -> None:
    control = Control()
    control.packages[filing().accession] = manifest()
    extractor = Extractor()
    ingestor = IncrementalFilingIngestor(
        control, NoNetworkStore(), extractor, Writer(), Warehouse(), settings=Settings(env="test")
    )
    result = asyncio.run(ingestor.parse(filing().accession, extraction_revision=2))
    assert result["status"] == "complete"
    assert result["extraction_revision"] == 2
    assert extractor.calls == 1


# AC-3
def test_ac3_worker_runs_up_to_concurrency_jobs_at_once() -> None:
    class _Control:
        def __init__(self) -> None:
            self.queue = [job(JobType.PARSE_FILING, {"accession": filing().accession}) for _ in range(4)]
            self.completed: list = []

        async def claim_job(self, worker_id, lock_timeout_seconds, job_types=None):
            return self.queue.pop(0) if self.queue else None

        async def heartbeat_job(self, job_id, worker_id, attempt=None):
            return None

        async def complete_job(self, job_id, result=None, *, worker_id=None, attempt=None):
            self.completed.append(job_id)

        async def fail_job(self, *args, **kwargs):
            raise AssertionError("no job should fail")

    state = {"running": 0, "peak": 0}

    async def handler(record):
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.05)
        state["running"] -= 1
        return {}

    control = _Control()
    settings = Settings(env="test", worker_poll_seconds=0.01, worker_lock_timeout_seconds=60, worker_concurrency=2)
    worker = Worker(settings, control, {JobType.PARSE_FILING: handler})

    async def run() -> None:
        task = asyncio.create_task(worker.run())
        await wait_until(lambda: len(control.completed) == 4)
        worker.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert state["peak"] == 2
