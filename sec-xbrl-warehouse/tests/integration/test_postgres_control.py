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
    JobType,
    Severity,
)
from sec_xbrl_warehouse.domain.models import (
    DatasetArtifact,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.exceptions import StateTransitionError
from sec_xbrl_warehouse.infrastructure.storage.postgres import (
    PostgresControlRepository,
    checkpoint_lease,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


@pytest.fixture(autouse=True)
async def _clean_isolated_database(postgres_settings: Settings) -> None:
    """Keep tests independent inside the session-scoped disposable database."""
    repo = PostgresControlRepository(postgres_settings)
    try:
        async with repo.sessions.begin() as session:
            await session.execute(
                text(
                    "TRUNCATE validation_issues, job_checkpoints, ingestion_jobs, "
                    "mapping_reviews, concept_mappings, canonical_metrics, filings, "
                    "source_artifacts RESTART IDENTITY CASCADE"
                )
            )
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


async def test_upsert_source_artifact_is_idempotent_on_url_and_checksum(
    postgres_settings: Settings,
) -> None:
    repo = PostgresControlRepository(postgres_settings)
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


async def test_artifact_state_transitions_reject_impossible_regressions(
    postgres_settings: Settings,
) -> None:
    repo = PostgresControlRepository(postgres_settings)
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


async def test_enqueue_job_same_idempotency_key_returns_same_job(
    postgres_settings: Settings,
) -> None:
    repo = PostgresControlRepository(postgres_settings)
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


async def test_two_concurrent_workers_never_claim_same_job(
    postgres_settings: Settings,
) -> None:
    repo = PostgresControlRepository(postgres_settings)
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


async def test_stale_lock_is_reclaimable(postgres_settings: Settings) -> None:
    repo = PostgresControlRepository(postgres_settings)
    try:
        key = f"stale-{uuid4()}"
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=key, priority=1, payload={"x": 1}
        )
        worker_id = "reused-worker"
        first = await repo.claim_job(worker_id, lock_timeout_seconds=900)
        assert first is not None
        assert first.state is JobState.RUNNING
        with checkpoint_lease(first):
            await repo.save_checkpoint(first.id, "load", {"rows": 5})

        # A second worker cannot grab it while fresh...
        gone = await repo.claim_job("live-worker", lock_timeout_seconds=900)
        assert gone is None

        # ...but after the lock timeout, the same job is reclaimable.
        async with repo.sessions.begin() as session:
            await session.execute(
                text("UPDATE ingestion_jobs SET locked_at = :stale WHERE id = :job_id"),
                {"stale": datetime.now(UTC) - timedelta(seconds=1800), "job_id": first.id},
            )
        reclaimed = await repo.claim_job(worker_id, lock_timeout_seconds=900)
        assert reclaimed is not None and reclaimed.id == first.id
        assert reclaimed.attempts == 2
        with checkpoint_lease(first):
            with pytest.raises(StateTransitionError):
                await repo.get_checkpoint(first.id, "load")
            with pytest.raises(StateTransitionError):
                await repo.save_checkpoint(first.id, "load", {"rows": 6})
        with checkpoint_lease(reclaimed):
            assert await repo.get_checkpoint(reclaimed.id, "load") == {"rows": 5}
            await repo.save_checkpoint(reclaimed.id, "load", {"rows": 10})
            assert await repo.get_checkpoint(reclaimed.id, "load") == {"rows": 10}
        with pytest.raises(StateTransitionError):
            await repo.complete_job(first.id, worker_id=worker_id, attempt=first.attempts)
        with pytest.raises(StateTransitionError):
            await repo.heartbeat_job(first.id, worker_id, first.attempts)
    finally:
        await repo.close()


async def test_claim_job_respects_job_type_filter(postgres_settings: Settings) -> None:
    repo = PostgresControlRepository(postgres_settings)
    try:
        held_id = await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=f"held-{uuid4()}", priority=-1, payload={}
        )
        scoped_id = await repo.enqueue_job(
            job_type="ingest_filing", idempotency_key=f"scoped-{uuid4()}", priority=-1, payload={}
        )
        record = await repo.claim_job("w1", 900, [JobType.INGEST_FILING])
        assert record is not None and record.id == scoped_id
        held = await repo.claim_job("w2", 900)
        assert held is not None and held.id == held_id
    finally:
        await repo.close()


async def test_fail_job_retry_then_dead_letter(postgres_settings: Settings) -> None:
    repo = PostgresControlRepository(postgres_settings)
    try:
        key = f"retry-{uuid4()}"
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=key, max_attempts=2, payload={"x": 1}
        )
        record = await repo.claim_job("w1", lock_timeout_seconds=900)
        assert record is not None
        await repo.fail_job(
            record.id,
            "transient timeout",
            retryable=True,
            worker_id="w1",
            attempt=record.attempts,
        )
        # Backoff puts available_at in the future; reset it to reclaim immediately.
        async with repo.sessions.begin() as session:
            await session.execute(
                text("UPDATE ingestion_jobs SET available_at = now() WHERE id = :job_id"),
                {"job_id": record.id},
            )
        retry = await repo.claim_job("w2", lock_timeout_seconds=900)
        assert retry is not None and retry.attempts == 2
        await repo.fail_job(
            retry.id,
            "failed again",
            retryable=True,
            worker_id="w2",
            attempt=retry.attempts,
        )
        # Exhausted attempts -> dead, not claimable.
        dead = await repo.claim_job("w3", lock_timeout_seconds=900)
        assert dead is None
    finally:
        await repo.close()


async def test_complete_job_only_from_running(postgres_settings: Settings) -> None:
    repo = PostgresControlRepository(postgres_settings)
    try:
        key = f"complete-{uuid4()}"
        await repo.enqueue_job(job_type="load_bulk", idempotency_key=key, payload={"x": 1})
        record = await repo.claim_job("w1", lock_timeout_seconds=900)
        assert record is not None
        with pytest.raises(StateTransitionError):
            await repo.get_checkpoint(record.id, "load")
        with pytest.raises(StateTransitionError):
            await repo.save_checkpoint(record.id, "load", {"rows": 5})
        with checkpoint_lease(record):
            await repo.save_checkpoint(record.id, "load", {"rows": 5})
            await repo.save_checkpoint(record.id, "load", {"rows": 10})
            assert await repo.get_checkpoint(record.id, "load") == {"rows": 10}
        await repo.complete_job(
            record.id,
            {"rows": 10},
            worker_id="w1",
            attempt=record.attempts,
        )
        with pytest.raises(StateTransitionError):
            await repo.complete_job(record.id)
    finally:
        await repo.close()


async def test_upsert_filing_and_validation_issues(postgres_settings: Settings) -> None:
    repo = PostgresControlRepository(postgres_settings)
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
            extraction_revision=3,
        )
        await repo.record_validation_issues([issue])
        from sqlalchemy import select

        from sec_xbrl_warehouse.infrastructure.storage.postgres_models import ValidationIssueRow

        async with repo.sessions() as session:
            revisions = (
                await session.scalars(
                    select(ValidationIssueRow.extraction_revision).where(
                        ValidationIssueRow.accession == accession
                    )
                )
            ).all()
        assert revisions == [3]
    finally:
        await repo.close()


async def test_discovery_upsert_keeps_stored_fiscal_fields_over_nulls(
    postgres_settings: Settings,
) -> None:
    from sqlalchemy import select

    from sec_xbrl_warehouse.infrastructure.storage.postgres_models import FilingRow

    repo = PostgresControlRepository(postgres_settings)
    try:
        accession = "0000000001-26-000002"
        enriched = _filing(accession).model_copy(
            update={
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "report_date": datetime(2025, 12, 31).date(),
            }
        )
        await repo.upsert_filing(enriched)
        await repo.upsert_filing(
            _filing(accession).model_copy(update={"form": "10-K/A"}), keep_derived=True
        )
        async with repo.sessions() as session:
            row = (
                await session.execute(select(FilingRow).where(FilingRow.accession == accession))
            ).scalar_one()
        assert (row.fiscal_year, row.fiscal_period, row.report_date) == (
            2025,
            "FY",
            datetime(2025, 12, 31).date(),
        )
        assert row.form == "10-K/A"
        await repo.upsert_filing(enriched.model_copy(update={"fiscal_period": "Q4"}))
        async with repo.sessions() as session:
            row = (
                await session.execute(select(FilingRow).where(FilingRow.accession == accession))
            ).scalar_one()
        assert row.fiscal_period == "Q4"
        # A later extraction that derives NULL (conflicting dei values) clears it.
        await repo.upsert_filing(enriched.model_copy(update={"fiscal_year": None}))
        async with repo.sessions() as session:
            row = (
                await session.execute(select(FilingRow).where(FilingRow.accession == accession))
            ).scalar_one()
        assert row.fiscal_year is None
    finally:
        await repo.close()


async def test_deferred_retry_preserves_server_deadline_and_fence(postgres_settings):
    repo = PostgresControlRepository(postgres_settings)
    try:
        await repo.enqueue_job(
            job_type="load_bulk", idempotency_key=f"deferred-{uuid4()}", payload={}
        )
        job = await repo.claim_job("owner", lock_timeout_seconds=900)
        deadline = datetime.now(UTC) + timedelta(days=10)
        with pytest.raises(StateTransitionError):
            await repo.fail_job(
                job.id, "deferred", True, worker_id="stale", attempt=job.attempts, retry_at=deadline
            )
        await repo.fail_job(
            job.id, "deferred", True, worker_id="owner", attempt=job.attempts, retry_at=deadline
        )
        assert await repo.claim_job("next", lock_timeout_seconds=900) is None
        async with repo.sessions() as session:
            saved = await session.scalar(
                text("SELECT available_at FROM ingestion_jobs WHERE id = :id"), {"id": job.id}
            )
        assert saved >= deadline
    finally:
        await repo.close()


async def test_extraction_checkpoint_finds_completed_ingest(postgres_settings: Settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import checkpoint_lease

    repo = PostgresControlRepository(postgres_settings)
    accession = "0000000001-26-000777"
    try:
        await repo.enqueue_job(
            job_type="ingest_filing",
            idempotency_key=f"filing:{accession}:3",
            priority=0,
            payload={"filing": {"accession": accession}, "extraction_revision": 3},
        )
        job = await repo.claim_job("remap-worker", 900, [JobType.INGEST_FILING])
        assert job is not None and job.payload["filing"]["accession"] == accession
        pointer = {"object_key": "silver/x.json", "sha256": "a" * 64}
        with checkpoint_lease(job):
            await repo.save_checkpoint(job.id, "extract", pointer)
        assert await repo.extraction_checkpoint(accession, 3) is None  # not complete yet
        await repo.complete_job(
            job.id, {"status": "complete"}, worker_id="remap-worker", attempt=job.attempts
        )
        assert await repo.extraction_checkpoint(accession, 3) == pointer
        assert await repo.extraction_checkpoint(accession, 2) is None
        assert await repo.enqueue_job_if_absent(
            job_type="map_canonical", idempotency_key="remap-x", payload={}
        )
        assert (
            await repo.enqueue_job_if_absent(
                job_type="map_canonical", idempotency_key="remap-x", payload={}
            )
            is None
        )
    finally:
        await repo.close()


async def test_company_round_trips_every_field(postgres_settings: Settings) -> None:
    from datetime import date

    from sec_xbrl_warehouse.domain.models import Company, FormerName

    company = Company(
        cik=987654,
        name="Round Trip Corp",
        entity_type="operating",
        sic=3571,
        sic_description="Electronic Computers",
        tickers=("RTC", "RTC-W"),
        exchanges=("Nasdaq", "Nasdaq"),
        fiscal_year_end="0630",
        state_of_incorporation="DE",
        filer_category="Large accelerated filer",
        former_names=(FormerName(name="OLD RTC", valid_from=date(2001, 1, 2), valid_to=None),),
        source_checksum="c" * 64,
        fetched_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )
    repo = PostgresControlRepository(postgres_settings)
    try:
        await repo.upsert_company(company)
        assert await repo.get_company(987654) == company
        assert await repo.find_company_by_ticker("rtc-w") == company
        assert await repo.known_company_ciks([987654, 1]) == {987654}
        assert await repo.known_company_ciks([]) == set()
    finally:
        await repo.close()


async def test_ticker_move_removes_matching_exchange(postgres_settings: Settings) -> None:
    from sec_xbrl_warehouse.domain.models import Company

    repo = PostgresControlRepository(postgres_settings)
    try:
        await repo.upsert_company(
            Company(
                cik=555001,
                name="Holder A",
                tickers=("AAA", "MOVE", "aaa"),
                exchanges=("NYSE", "Nasdaq", "NYSE"),
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        stored = await repo.get_company(555001)
        assert stored is not None
        assert (stored.tickers, stored.exchanges) == (("AAA", "MOVE"), ("NYSE", "Nasdaq"))
        await repo.upsert_company(
            Company(
                cik=555002,
                name="Holder B",
                tickers=("MOVE",),
                exchanges=("CboeBZX",),
                fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        old = await repo.get_company(555001)
        assert old is not None and (old.tickers, old.exchanges) == (("AAA",), ("NYSE",))
        stale = await repo.stale_company_ciks(
            [555001, 555002, 999], datetime(2026, 6, 1, tzinfo=UTC)
        )
        assert stale == {555001}
    finally:
        await repo.close()
