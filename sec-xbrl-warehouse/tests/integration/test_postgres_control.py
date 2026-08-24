import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import (
    ArtifactState,
    DatasetKind,
    JobState,
    Severity,
)
from sec_xbrl_warehouse.domain.models import (
    DatasetArtifact,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.exceptions import StateTransitionError
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

pytestmark = pytest.mark.integration

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_XBRL_INTEGRATION") != "1",
    reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
)


@pytest.fixture(autouse=True)
async def _cleanup_jobs() -> None:
    """Remove leftover jobs so concurrency tests observe only their own queue."""
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        async with repo.sessions.begin() as session:
            await session.execute(text("DELETE FROM ingestion_jobs"))
    finally:
        await repo.close()


def _artifact(period: str = "2024Q1", url_suffix: str = "") -> DatasetArtifact:
    return DatasetArtifact(
        dataset=DatasetKind.NOTES,
        period=period,
        source_url=f"https://www.sec.gov/archives/notes-{url_suffix}{period}.zip",
        filename=f"notes-{period}.zip",
        published_label=f"Notes {period}",
    )


def _filing(accession: str = "0000000001-26-000001") -> FilingRef:
    return FilingRef(
        accession=accession,
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=datetime(2026, 1, 2).date(),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


async def test_upsert_source_artifact_is_idempotent_on_url_and_checksum() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        artifact = _artifact()
        first = await repo.upsert_source_artifact(artifact, checksum_sha256="a" * 64)
        second = await repo.upsert_source_artifact(artifact, checksum_sha256="a" * 64)
        assert first == second

        # Republished bytes become a new revision, never an overwrite.
        revision_two = await repo.upsert_source_artifact(artifact, checksum_sha256="b" * 64)
        assert revision_two != first
    finally:
        await repo.close()


async def test_artifact_state_transitions_reject_impossible_regressions() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        artifact_id = await repo.upsert_source_artifact(_artifact())
        await repo.mark_artifact_state(artifact_id, ArtifactState.DOWNLOADING)
        with pytest.raises(StateTransitionError):
            await repo.mark_artifact_state(artifact_id, ArtifactState.COMPLETE)
        await repo.mark_artifact_state(artifact_id, ArtifactState.ACQUIRED)
        await repo.mark_artifact_state(artifact_id, ArtifactState.VALIDATING)
        await repo.mark_artifact_state(artifact_id, ArtifactState.LOADING)
        await repo.mark_artifact_state(artifact_id, ArtifactState.RECONCILING)
        await repo.mark_artifact_state(artifact_id, ArtifactState.COMPLETE)
        # COMPLETE is terminal.
        with pytest.raises(StateTransitionError):
            await repo.mark_artifact_state(artifact_id, ArtifactState.FAILED)
    finally:
        await repo.close()


async def test_enqueue_job_same_idempotency_key_returns_same_job() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        key = f"k-{uuid4()}"
        first = await repo.enqueue_job(
            job_type="download_bulk", idempotency_key=key, payload={"x": 1}
        )
        second = await repo.enqueue_job(
            job_type="download_bulk", idempotency_key=f"k2-{uuid4()}", payload={"x": 1}
        )
        assert first != second
        # Same key returns same job id.
        first_again = await repo.enqueue_job(
            job_type="download_bulk", idempotency_key=key, payload={"x": 1}
        )
        assert first_again == first
    finally:
        await repo.close()


async def test_two_concurrent_workers_never_claim_same_job() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        key = f"concurrent-{uuid4()}"
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=key, priority=1, payload={"x": 1}
        )

        async def claim(worker_id: str) -> str | None:
            record = await repo.claim_job(worker_id, lock_timeout_seconds=900)
            return str(record.id) if record else None

        results = await asyncio.gather(claim("w1"), claim("w2"))
        claimed = [item for item in results if item is not None]
        assert len(claimed) == 1
    finally:
        await repo.close()


async def test_stale_lock_is_reclaimable() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        key = f"stale-{uuid4()}"
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=key, priority=1, payload={"x": 1}
        )
        first = await repo.claim_job("dead-worker", lock_timeout_seconds=900)
        assert first is not None
        assert first.state is JobState.RUNNING

        # A second worker cannot grab it while fresh...
        gone = await repo.claim_job("live-worker", lock_timeout_seconds=900)
        assert gone is None

        # ...but after the lock timeout, the same job is reclaimable.
        async with repo.sessions.begin() as session:
            await session.execute(
                text("UPDATE ingestion_jobs SET locked_at = :stale WHERE id = :job_id"),
                {"stale": datetime.now(UTC) - timedelta(seconds=1800), "job_id": first.id},
            )
        reclaimed = await repo.claim_job("live-worker", lock_timeout_seconds=900)
        assert reclaimed is not None and reclaimed.id == first.id
        assert reclaimed.attempts == 2
    finally:
        await repo.close()


async def test_fail_job_retry_then_dead_letter() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        key = f"retry-{uuid4()}"
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=key, max_attempts=2, payload={"x": 1}
        )
        record = await repo.claim_job("w1", lock_timeout_seconds=900)
        assert record is not None
        await repo.fail_job(record.id, "transient timeout", retryable=True)
        # Backoff puts available_at in the future; reset it to reclaim immediately.
        async with repo.sessions.begin() as session:
            await session.execute(
                text("UPDATE ingestion_jobs SET available_at = now() WHERE id = :job_id"),
                {"job_id": record.id},
            )
        retry = await repo.claim_job("w2", lock_timeout_seconds=900)
        assert retry is not None and retry.attempts == 2
        await repo.fail_job(retry.id, "failed again", retryable=True)
        # Exhausted attempts -> dead, not claimable.
        dead = await repo.claim_job("w3", lock_timeout_seconds=900)
        assert dead is None
    finally:
        await repo.close()


async def test_complete_job_only_from_running() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        key = f"complete-{uuid4()}"
        await repo.enqueue_job(job_type="load_bulk", idempotency_key=key, payload={"x": 1})
        record = await repo.claim_job("w1", lock_timeout_seconds=900)
        assert record is not None
        await repo.complete_job(record.id, {"rows": 10})
        with pytest.raises(StateTransitionError):
            await repo.complete_job(record.id)
    finally:
        await repo.close()


async def test_upsert_filing_and_validation_issues() -> None:
    repo = PostgresControlRepository(Settings(env="test"))
    try:
        accession = "0000000001-26-000001"
        await repo.upsert_filing(_filing(accession))
        await repo.upsert_filing(_filing(accession))  # idempotent
        issue = ValidationIssue(
            source="arelle",
            code="ARELLE_TEST",
            severity=Severity.WARNING,
            message="test message",
            accession=accession,
            line=12,
        )
        await repo.record_validation_issues([issue])
    finally:
        await repo.close()
