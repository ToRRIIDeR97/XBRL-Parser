from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import ArtifactState, JobState, JobType
from sec_xbrl_warehouse.domain.models import (
    Company,
    DatasetArtifact,
    FilingPackageManifest,
    FilingRef,
    FormerName,
    JobRecord,
    QueueSnapshot,
    SourceArtifactRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.exceptions import StateTransitionError
from sec_xbrl_warehouse.infrastructure.storage.postgres_models import (
    CompanyRow,
    FilingRow,
    IngestionJobRow,
    JobCheckpointRow,
    SourceArtifactRow,
    ValidationIssueRow,
)
from sec_xbrl_warehouse.observability.metrics import JOB_DEAD_LETTERS, JOB_TRANSITIONS

_ALLOWED_ARTIFACT_TRANSITIONS: dict[ArtifactState, set[ArtifactState]] = {
    ArtifactState.DISCOVERED: {ArtifactState.DOWNLOADING, ArtifactState.FAILED},
    ArtifactState.DOWNLOADING: {ArtifactState.ACQUIRED, ArtifactState.FAILED},
    ArtifactState.ACQUIRED: {ArtifactState.VALIDATING, ArtifactState.FAILED},
    ArtifactState.VALIDATING: {
        ArtifactState.LOADING,
        ArtifactState.QUARANTINED,
        ArtifactState.FAILED,
    },
    ArtifactState.LOADING: {
        ArtifactState.RECONCILING,
        ArtifactState.QUARANTINED,
        ArtifactState.FAILED,
    },
    ArtifactState.RECONCILING: {
        ArtifactState.COMPLETE,
        ArtifactState.QUARANTINED,
        ArtifactState.FAILED,
    },
    ArtifactState.FAILED: {ArtifactState.DOWNLOADING, ArtifactState.VALIDATING},
    ArtifactState.QUARANTINED: {ArtifactState.VALIDATING},
    ArtifactState.COMPLETE: set(),
}

_CHECKPOINT_LEASE: ContextVar[JobRecord | None] = ContextVar("xbrl_checkpoint_lease", default=None)


@contextmanager
def checkpoint_lease(job: JobRecord) -> Iterator[None]:
    token = _CHECKPOINT_LEASE.set(job)
    try:
        yield
    finally:
        _CHECKPOINT_LEASE.reset(token)


def current_checkpoint_lease() -> JobRecord | None:
    return _CHECKPOINT_LEASE.get()


class PostgresControlRepository:
    """PostgreSQL implementation of artifact, filing, job, and issue state.

    Transactions stay deliberately narrow. Job claims use SKIP LOCKED and every
    completion, failure, checkpoint, and heartbeat is fenced by worker and attempt.
    """

    def __init__(self, settings: Settings, engine: AsyncEngine | None = None) -> None:
        self.settings = settings
        self.engine = engine or create_async_engine(
            settings.postgres_dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def get_source_artifact(self, artifact_id: UUID) -> SourceArtifactRef:
        async with self.sessions() as session:
            row = await session.get(SourceArtifactRow, artifact_id)
            if row is None:
                raise KeyError(f"Unknown source artifact: {artifact_id}")
            return SourceArtifactRef.model_validate(
                {name: getattr(row, name) for name in SourceArtifactRef.model_fields}
            )

    async def get_checkpoint(
        self, job_id: UUID, stage: str, key: str = "default"
    ) -> dict[str, Any] | None:
        lease = self._require_checkpoint_lease(job_id)
        async with self.sessions.begin() as session:
            await self._lock_checkpoint_job(session, lease)
            return await session.scalar(
                select(JobCheckpointRow.payload).where(
                    JobCheckpointRow.job_id == job_id,
                    JobCheckpointRow.stage == stage,
                    JobCheckpointRow.checkpoint_key == key,
                )
            )

    async def save_checkpoint(
        self, job_id: UUID, stage: str, payload: dict[str, Any], key: str = "default"
    ) -> None:
        lease = self._require_checkpoint_lease(job_id)
        statement = (
            pg_insert(JobCheckpointRow)
            .values(job_id=job_id, stage=stage, checkpoint_key=key, payload=payload)
            .on_conflict_do_update(constraint="uq_job_checkpoint", set_={"payload": payload})
        )
        async with self.sessions.begin() as session:
            await self._lock_checkpoint_job(session, lease)
            await session.execute(statement)

    @staticmethod
    def _require_checkpoint_lease(job_id: UUID) -> JobRecord:
        lease = current_checkpoint_lease()
        if lease is None or lease.id != job_id or lease.locked_by is None:
            raise StateTransitionError(
                f"Checkpoint access requires an active lease for job {job_id}"
            )
        return lease

    @staticmethod
    async def _lock_checkpoint_job(session: AsyncSession, lease: JobRecord) -> None:
        row = await session.scalar(
            select(IngestionJobRow)
            .where(
                IngestionJobRow.id == lease.id,
                IngestionJobRow.state == JobState.RUNNING.value,
                IngestionJobRow.locked_by == lease.locked_by,
                IngestionJobRow.attempts == lease.attempts,
            )
            .with_for_update()
        )
        if row is None:
            raise StateTransitionError(f"Checkpoint lease is no longer valid for job {lease.id}")

    async def upsert_source_artifact(
        self,
        artifact: DatasetArtifact,
        *,
        checksum_sha256: str | None = None,
        object_key: str | None = None,
    ) -> UUID:
        """Return an existing identical artifact or create a new source revision.

        Implementation contract:
        1. If `source_url` plus a non-null checksum already exists, return it.
        2. Otherwise lock rows for `(dataset, period)`, allocate max(revision)+1,
           and insert the artifact atomically.
        3. A republished SEC period with different bytes must never overwrite an
           earlier revision.
        """
        async with self.sessions.begin() as session:
            # Row locks cannot protect the first revision of a previously unseen period.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"artifact:{artifact.dataset.value}:{artifact.period}"},
            )
            checksum_sha256 = checksum_sha256 or artifact.checksum_sha256
            if checksum_sha256:
                existing = await session.scalar(
                    select(SourceArtifactRow.id).where(
                        SourceArtifactRow.source_url == artifact.source_url,
                        SourceArtifactRow.checksum_sha256 == checksum_sha256,
                    )
                )
                if existing is not None:
                    return existing

            revision = (
                await session.scalar(
                    select(func.coalesce(func.max(SourceArtifactRow.revision), 0)).where(
                        SourceArtifactRow.dataset == artifact.dataset.value,
                        SourceArtifactRow.period == artifact.period,
                    )
                )
                or 0
            ) + 1
            row = SourceArtifactRow(
                dataset=artifact.dataset.value,
                period=artifact.period,
                source_url=artifact.source_url,
                filename=artifact.filename,
                published_label=artifact.published_label,
                expected_size_bytes=artifact.expected_size_bytes,
                checksum_sha256=checksum_sha256 or artifact.checksum_sha256,
                etag=artifact.etag,
                last_modified=artifact.last_modified,
                object_key=object_key,
                revision=revision,
                state=ArtifactState.ACQUIRED.value
                if object_key
                else ArtifactState.DISCOVERED.value,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def mark_artifact_state(
        self,
        artifact_id: UUID,
        state: ArtifactState,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(SourceArtifactRow)
                .where(SourceArtifactRow.id == artifact_id)
                .with_for_update()
            )
            if row is None:
                raise KeyError(f"Unknown source artifact: {artifact_id}")
            current = ArtifactState(row.state)
            if state != current and state not in _ALLOWED_ARTIFACT_TRANSITIONS[current]:
                raise StateTransitionError(f"Invalid artifact transition: {current} -> {state}")
            row.state = state.value
            if details:
                row.details = {**row.details, **details}

    async def enqueue_job(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 100,
        max_attempts: int = 5,
    ) -> UUID:
        values = {
            "job_type": JobType(job_type).value,
            "state": JobState.PENDING.value,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "priority": priority,
            "max_attempts": max_attempts,
        }
        statement = (
            pg_insert(IngestionJobRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[IngestionJobRow.idempotency_key],
                set_={"updated_at": func.now()},
            )
            .returning(IngestionJobRow.id)
        )
        async with self.sessions.begin() as session:
            job_id = await session.scalar(statement)
            if job_id is None:
                raise RuntimeError("PostgreSQL did not return a job ID")
            return job_id

    async def enqueue_job_if_absent(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 100,
        max_attempts: int = 5,
    ) -> UUID | None:
        """Insert a pending job; return None if the idempotency key already exists."""
        statement = (
            pg_insert(IngestionJobRow)
            .values(
                job_type=JobType(job_type).value,
                state=JobState.PENDING.value,
                idempotency_key=idempotency_key,
                payload=payload,
                priority=priority,
                max_attempts=max_attempts,
            )
            .on_conflict_do_nothing(index_elements=[IngestionJobRow.idempotency_key])
            .returning(IngestionJobRow.id)
        )
        async with self.sessions.begin() as session:
            job_id: UUID | None = await session.scalar(statement)
            return job_id

    async def published_filing_revisions(
        self, accessions: Sequence[str] | None = None
    ) -> list[tuple[str, int]]:
        """`(accession, latest_extraction_revision)` for filings with a published extraction."""
        statement = (
            select(FilingRow.accession, FilingRow.latest_extraction_revision)
            .where(FilingRow.latest_extraction_revision > 0)
            .order_by(FilingRow.accession)
        )
        if accessions is not None:
            statement = statement.where(FilingRow.accession.in_(list(accessions)))
        async with self.sessions() as session:
            return [(str(row[0]), int(row[1])) for row in (await session.execute(statement)).all()]

    async def upsert_company(self, company: Company) -> None:
        """Insert or update a company; tickers it lists are removed from other companies.

        `tickers` and `exchanges` are parallel lists (SEC submissions order). A
        ticker belongs to at most one company at a time, so the newest upsert that
        lists it wins, and the previous holder loses the ticker together with the
        exchange at the same position. Both changes commit in one transaction.
        """
        pairs = _ticker_pairs(company.tickers, company.exchanges)
        tickers = [ticker for ticker, _ in pairs]
        values = {
            "cik": company.cik,
            "name": company.name,
            "entity_type": company.entity_type,
            "sic": company.sic,
            "sic_description": company.sic_description,
            "tickers": tickers,
            "exchanges": [exchange for _, exchange in pairs]
            if _aligned(company)
            else list(company.exchanges),
            "fiscal_year_end": company.fiscal_year_end,
            "state_of_incorporation": company.state_of_incorporation,
            "filer_category": company.filer_category,
            "former_names": [item.model_dump(mode="json") for item in company.former_names],
            "source_checksum": company.source_checksum,
            "fetched_at": company.fetched_at,
        }
        async with self.sessions.begin() as session:
            # Serialize ticker moves so concurrent upserts can't both keep a ticker.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended('companies:tickers', 0))")
            )
            if tickers:
                holders = (
                    await session.scalars(
                        select(CompanyRow)
                        .where(CompanyRow.cik != company.cik, CompanyRow.tickers.overlap(tickers))
                        .with_for_update()
                    )
                ).all()
                moved = set(tickers)
                for holder in holders:
                    aligned = len(holder.exchanges or []) == len(holder.tickers or [])
                    keep = [
                        index
                        for index, ticker in enumerate(holder.tickers or [])
                        if ticker not in moved
                    ]
                    holder.exchanges = (
                        [holder.exchanges[index] for index in keep]
                        if aligned
                        else list(holder.exchanges or [])
                    )
                    holder.tickers = [holder.tickers[index] for index in keep]
                    holder.updated_at = func.now()
                await session.flush()
            statement = pg_insert(CompanyRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[CompanyRow.cik],
                    set_={
                        **{key: value for key, value in values.items() if key != "cik"},
                        "updated_at": func.now(),
                    },
                )
            )

    async def stale_company_ciks(self, ciks: Sequence[int], fetched_before: datetime) -> set[int]:
        """Stored CIKs whose submissions were fetched before `fetched_before` (or never)."""
        if not ciks:
            return set()
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CompanyRow.cik).where(
                    CompanyRow.cik.in_(list(ciks)),
                    or_(CompanyRow.fetched_at.is_(None), CompanyRow.fetched_at < fetched_before),
                )
            )
            return {int(cik) for cik in rows}

    async def get_company(self, cik: int) -> Company | None:
        async with self.sessions() as session:
            row = await session.get(CompanyRow, cik)
            return _company_from_row(row) if row is not None else None

    async def find_company_by_ticker(self, ticker: str) -> Company | None:
        """Case-insensitive ticker lookup; None when no company lists it."""
        normalized = ticker.strip().upper()
        if not normalized:
            return None
        async with self.sessions() as session:
            row = await session.scalar(
                select(CompanyRow).where(CompanyRow.tickers.contains([normalized])).limit(1)
            )
            return _company_from_row(row) if row is not None else None

    async def known_company_ciks(self, ciks: Sequence[int]) -> set[int]:
        if not ciks:
            return set()
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CompanyRow.cik).where(CompanyRow.cik.in_(list(ciks)))
            )
            return {int(cik) for cik in rows}

    async def extraction_checkpoint(self, accession: str, revision: int) -> dict[str, Any] | None:
        """The saved extraction (`object_key`, `sha256`) of a completed filing ingestion."""
        statement = (
            select(JobCheckpointRow.payload)
            .join(IngestionJobRow, IngestionJobRow.id == JobCheckpointRow.job_id)
            .where(
                IngestionJobRow.job_type == JobType.INGEST_FILING.value,
                IngestionJobRow.state == JobState.COMPLETE.value,
                IngestionJobRow.payload["filing"]["accession"].astext == accession,
                IngestionJobRow.payload["extraction_revision"].astext == str(revision),
                JobCheckpointRow.stage == "extract",
            )
            .order_by(IngestionJobRow.updated_at.desc())
            .limit(1)
        )
        async with self.sessions() as session:
            payload = await session.scalar(statement)
        return dict(payload) if payload else None

    async def claim_job(
        self,
        worker_id: str,
        lock_timeout_seconds: int,
        job_types: Sequence[JobType] | None = None,
    ) -> JobRecord | None:
        """Atomically claim one ready or stale job using `SKIP LOCKED`.

        When `job_types` is given, jobs of other types are neither claimed nor
        dead-lettered, so a scoped worker leaves held work untouched.

        Acceptance tests must run two concurrent transactions and prove that the
        same UUID cannot be returned twice. Stale `running` jobs are reclaimable
        after `lock_timeout_seconds`.
        """
        # Compare freshness against database time: `available_at` is populated by
        # PostgreSQL's clock, so a host-computed "now" can disagree after a
        # container restart or in high-latency deployments.
        stale_before = func.now() - timedelta(seconds=lock_timeout_seconds)
        type_filter = (
            IngestionJobRow.job_type.in_([job_type.value for job_type in job_types])
            if job_types
            else true()
        )
        async with self.sessions.begin() as session:
            # A selected job whose attempts are exhausted (its lease expired on the
            # last attempt) is dead-lettered with a reason, and the search goes on,
            # so one crashed job doesn't idle the worker for a poll interval.
            for _ in range(_CLAIM_SCAN_LIMIT):
                row = await session.scalar(
                    select(IngestionJobRow)
                    .where(
                        type_filter,
                        or_(
                            and_(
                                IngestionJobRow.state.in_(
                                    [JobState.PENDING.value, JobState.RETRY.value]
                                ),
                                IngestionJobRow.available_at <= func.now(),
                            ),
                            and_(
                                IngestionJobRow.state == JobState.RUNNING.value,
                                IngestionJobRow.locked_at < stale_before,
                            ),
                        ),
                    )
                    .order_by(IngestionJobRow.priority.asc(), IngestionJobRow.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                if row.attempts < row.max_attempts:
                    break
                previous = f" Last error: {row.last_error}" if row.last_error else ""
                row.state = JobState.DEAD.value
                row.last_error = (
                    f"Dead-lettered at claim: all {row.max_attempts} attempts used "
                    f"(lease expired after attempt {row.attempts}).{previous}"
                )[:16_000]
                row.locked_at = None
                row.locked_by = None
                row.heartbeat_at = None
                await session.flush()
                JOB_TRANSITIONS.labels(state="dead", job_type=row.job_type).inc()
                JOB_DEAD_LETTERS.labels(job_type=row.job_type, reason="attempts_exhausted").inc()
            else:
                return None
            lock_now = datetime.now(UTC)
            row.state = JobState.RUNNING.value
            row.attempts += 1
            # Store database time in the row (flushed at commit); the returned
            # record is built from real datetimes so SQL expressions never
            # escape this method.
            row.locked_at = func.now()
            row.heartbeat_at = func.now()
            row.locked_by = worker_id
            return JobRecord(
                id=row.id,
                job_type=JobType(row.job_type),
                state=JobState(row.state),
                idempotency_key=row.idempotency_key,
                payload=row.payload,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                priority=row.priority,
                available_at=row.available_at,
                locked_at=lock_now,
                locked_by=worker_id,
            )

    async def list_jobs(
        self,
        *,
        state: str | None = None,
        job_type: str | None = None,
        limit: int = 100,
    ) -> list[JobRecord]:
        """Jobs filtered by state and type, oldest first."""
        if not 1 <= limit <= 10_000:
            raise ValueError("Job list limit must be between 1 and 10000")
        statement = select(IngestionJobRow)
        if state is not None:
            statement = statement.where(IngestionJobRow.state == JobState(state).value)
        if job_type is not None:
            statement = statement.where(IngestionJobRow.job_type == JobType(job_type).value)
        statement = statement.order_by(
            IngestionJobRow.created_at.asc(), IngestionJobRow.id.asc()
        ).limit(limit)
        async with self.sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [_job_record(row) for row in rows]

    async def retry_job(self, job_id: UUID) -> JobRecord:
        """Revive a dead job: state `retry`, attempts reset, claimable now.

        The previous error stays in `last_error`. Only dead jobs can be retried.
        """
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(IngestionJobRow).where(IngestionJobRow.id == job_id).with_for_update()
            )
            if row is None:
                raise KeyError(job_id)
            if row.state != JobState.DEAD.value:
                raise StateTransitionError(
                    f"Only dead jobs can be retried; {job_id} is {row.state}"
                )
            row.state = JobState.RETRY.value
            row.attempts = 0
            row.deferred_since = None
            row.available_at = func.now()
            row.locked_at = None
            row.locked_by = None
            row.heartbeat_at = None
            if row.last_error:
                row.last_error = f"Retried manually after: {row.last_error}"[:16_000]
            await session.flush()
            await session.refresh(row)
            return _job_record(row)

    async def queue_snapshot(self) -> QueueSnapshot:
        """Counts by `(job_type, state)` and the oldest ready job's age, in database time."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(IngestionJobRow.job_type, IngestionJobRow.state, func.count()).group_by(
                        IngestionJobRow.job_type, IngestionJobRow.state
                    )
                )
            ).all()
            oldest = await session.scalar(
                select(
                    func.coalesce(
                        func.max(
                            func.extract(
                                "epoch",
                                func.now()
                                - func.greatest(
                                    IngestionJobRow.available_at, IngestionJobRow.created_at
                                ),
                            )
                        ),
                        0,
                    )
                ).where(
                    IngestionJobRow.state.in_([JobState.PENDING.value, JobState.RETRY.value]),
                    IngestionJobRow.available_at <= func.now(),
                )
            )
        return QueueSnapshot(
            counts={(str(job_type), str(state)): int(count) for job_type, state, count in rows},
            oldest_ready_age_seconds=max(0.0, float(oldest or 0)),
        )

    async def heartbeat_job(self, job_id: UUID, worker_id: str, attempt: int | None = None) -> None:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(IngestionJobRow)
                .where(
                    IngestionJobRow.id == job_id,
                    IngestionJobRow.state == JobState.RUNNING.value,
                    IngestionJobRow.locked_by == worker_id,
                    *([IngestionJobRow.attempts == attempt] if attempt is not None else []),
                )
                .values(heartbeat_at=func.now(), locked_at=func.now())
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise StateTransitionError(f"Worker {worker_id} no longer owns job {job_id}")

    async def complete_job(
        self,
        job_id: UUID,
        result: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            changed = await session.execute(
                update(IngestionJobRow)
                .where(
                    IngestionJobRow.id == job_id,
                    IngestionJobRow.state == JobState.RUNNING.value,
                    *([IngestionJobRow.locked_by == worker_id] if worker_id is not None else []),
                    *([IngestionJobRow.attempts == attempt] if attempt is not None else []),
                )
                .values(
                    state=JobState.COMPLETE.value,
                    result=result,
                    completed_at=func.now(),
                    locked_at=None,
                    locked_by=None,
                    heartbeat_at=None,
                    last_error=None,
                    deferred_since=None,
                )
            )
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise StateTransitionError(f"Cannot complete non-running job {job_id}")

    async def fail_job(
        self,
        job_id: UUID,
        error: str,
        retryable: bool,
        *,
        worker_id: str | None = None,
        attempt: int | None = None,
        retry_at: datetime | None = None,
        defer_seconds: float | None = None,
        max_defer_seconds: float | None = None,
    ) -> JobState:
        """Record a failure: retry with backoff, or dead when out of attempts/permanent.

        `defer_seconds` marks a deferral, not a failure (for example the SEC egress
        lock is busy): the job returns to `retry` after that delay and the attempt
        isn't counted. Deferral is capped: once the job has been deferred for
        `max_defer_seconds` since its first deferral (database time), it goes
        `dead` with that reason. Returns the job's new state.
        """
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(IngestionJobRow).where(IngestionJobRow.id == job_id).with_for_update()
            )
            if row is None:
                raise KeyError(job_id)
            if (
                row.state != JobState.RUNNING.value
                or (worker_id is not None and row.locked_by != worker_id)
                or (attempt is not None and row.attempts != attempt)
            ):
                raise StateTransitionError(f"Cannot fail a job no longer owned: {job_id}")
            if defer_seconds is not None:
                database_now = await session.scalar(select(func.now()))
                if database_now is None:
                    raise RuntimeError("PostgreSQL did not return now()")
                first_deferred = row.deferred_since or database_now
                row.deferred_since = first_deferred
                deferred_for = (database_now - first_deferred).total_seconds()
                row.locked_at = None
                row.locked_by = None
                row.heartbeat_at = None
                if max_defer_seconds is not None and deferred_for >= max_defer_seconds:
                    row.state = JobState.DEAD.value
                    row.last_error = (
                        f"Deferred for {deferred_for:.0f}s (limit {max_defer_seconds:.0f}s) "
                        f"waiting for the SEC egress lock: {error}"
                    )[:16_000]
                    JOB_DEAD_LETTERS.labels(
                        job_type=row.job_type, reason="egress_busy_timeout"
                    ).inc()
                    return JobState.DEAD
                row.state = JobState.RETRY.value
                row.attempts = max(0, row.attempts - 1)
                row.available_at = func.now() + timedelta(seconds=max(0.0, defer_seconds))
                row.last_error = error[:16_000]
                return JobState.RETRY
            row.deferred_since = None
            can_retry = retryable and row.attempts < row.max_attempts
            row.state = JobState.RETRY.value if can_retry else JobState.DEAD.value
            # Database time plus jittered exponential backoff, capped at one hour,
            # so a skewed worker clock can't push retries out (or pull them in).
            available_at: Any = func.now() + timedelta(seconds=retry_delay_seconds(row.attempts))
            if retry_at is not None:
                available_at = func.greatest(available_at, retry_at)
            row.available_at = available_at
            if not can_retry:
                reason = "permanent_error" if not retryable else "retries_exhausted"
                JOB_DEAD_LETTERS.labels(job_type=row.job_type, reason=reason).inc()
            row.last_error = error[:16_000]
            row.locked_at = None
            row.locked_by = None
            row.heartbeat_at = None
            return JobState(row.state)

    async def upsert_filing(self, filing: FilingRef, *, keep_derived: bool = False) -> None:
        values = {
            "accession": filing.accession,
            "cik": filing.cik,
            "company_name": filing.company_name,
            "form": filing.form,
            "filed_date": filing.filed_date,
            "accepted_at": filing.accepted_at,
            "report_date": filing.report_date,
            "fiscal_year": filing.fiscal_year,
            "fiscal_period": filing.fiscal_period,
            "primary_document": filing.primary_document,
            "instance_document": filing.instance_document,
            "archive_directory_url": filing.archive_directory_url,
            "primary_document_url": filing.primary_document_url,
            "is_amendment": filing.is_amendment,
            "is_inline_xbrl": filing.is_inline_xbrl,
        }
        async with self.sessions.begin() as session:
            await session.execute(filing_upsert_statement(values, keep_derived=keep_derived))

    async def record_validation_issues(self, issues: Sequence[ValidationIssue]) -> None:
        if not issues:
            return
        rows = [
            {
                "source": issue.source,
                "code": issue.code,
                "severity": issue.severity.value,
                "message": issue.message,
                "accession": issue.accession,
                "artifact_id": issue.artifact_id,
                "table_name": issue.table,
                "source_document": issue.source_document,
                "line": issue.line,
                "column_number": issue.column,
                "details": issue.details,
                "extraction_revision": issue.extraction_revision,
            }
            for issue in issues
        ]
        async with self.sessions.begin() as session:
            await session.execute(ValidationIssueRow.__table__.insert(), rows)  # type: ignore[attr-defined]

    async def update_filing_package(
        self, manifest: FilingPackageManifest, revision: int | None = None
    ) -> None:
        values: dict[str, Any] = {
            "package_object_key": manifest.package_object_key,
            "package_checksum_sha256": manifest.package_checksum_sha256,
        }
        if revision is not None:
            values["latest_extraction_revision"] = func.greatest(
                FilingRow.latest_extraction_revision, revision
            )
        async with self.sessions.begin() as session:
            await session.execute(
                update(FilingRow)
                .where(FilingRow.accession == manifest.filing.accession)
                .values(**values)
            )

    @staticmethod
    def _to_job_record(row: IngestionJobRow) -> JobRecord:
        return JobRecord(
            id=row.id,
            job_type=JobType(row.job_type),
            state=JobState(row.state),
            idempotency_key=row.idempotency_key,
            payload=row.payload,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            priority=row.priority,
            available_at=row.available_at,
            locked_at=row.locked_at,
            locked_by=row.locked_by,
        )


# Filing-level fields derived from the filing's own dei facts. Discovery usually
# lacks them, so the discovery upsert at the start of a re-ingestion must not
# erase values an earlier extraction stored. The post-extraction upsert writes
# them as derived, including NULL for conflicting or invalid dei values.
_DERIVED_FILING_FIELDS = ("report_date", "fiscal_year", "fiscal_period")


def filing_upsert_statement(values: dict[str, Any], *, keep_derived: bool = False) -> Any:
    """Insert or update a filing row; `keep_derived` keeps stored fiscal fields over NULLs."""
    statement = pg_insert(FilingRow).values(**values)
    updates: dict[str, Any] = {key: value for key, value in values.items() if key != "accession"}
    if keep_derived:
        for key in _DERIVED_FILING_FIELDS:
            updates[key] = func.coalesce(statement.excluded[key], getattr(FilingRow, key))
    return statement.on_conflict_do_update(index_elements=[FilingRow.accession], set_=updates)


def _company_from_row(row: CompanyRow) -> Company:
    return Company(
        cik=int(row.cik),
        name=row.name,
        entity_type=row.entity_type,
        sic=row.sic,
        sic_description=row.sic_description,
        tickers=tuple(row.tickers or ()),
        exchanges=tuple(row.exchanges or ()),
        fiscal_year_end=row.fiscal_year_end,
        state_of_incorporation=row.state_of_incorporation,
        filer_category=row.filer_category,
        former_names=tuple(FormerName.model_validate(item) for item in row.former_names or ()),
        source_checksum=row.source_checksum,
        fetched_at=row.fetched_at,
    )


_CLAIM_SCAN_LIMIT = 100
MAX_RETRY_DELAY_SECONDS = 3600.0


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff (2, 4, 8, ... seconds) with 50-100% jitter, at most one hour."""
    base = min(MAX_RETRY_DELAY_SECONDS, 2.0 ** max(attempts, 1))
    return base * (0.5 + random.random() * 0.5)


def _job_record(row: IngestionJobRow) -> JobRecord:
    return JobRecord(
        id=row.id,
        job_type=JobType(row.job_type),
        state=JobState(row.state),
        idempotency_key=row.idempotency_key,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        priority=row.priority,
        available_at=row.available_at,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _aligned(company: Company) -> bool:
    return len(company.exchanges) == len(company.tickers)


def _ticker_pairs(tickers: Sequence[str], exchanges: Sequence[str]) -> list[tuple[str, str]]:
    """Upper-cased, de-duplicated tickers with the exchange at the same position."""
    aligned = len(exchanges) == len(tickers)
    pairs: dict[str, str] = {}
    for index, ticker in enumerate(tickers):
        pairs.setdefault(ticker.upper(), exchanges[index] if aligned else "")
    return list(pairs.items())
