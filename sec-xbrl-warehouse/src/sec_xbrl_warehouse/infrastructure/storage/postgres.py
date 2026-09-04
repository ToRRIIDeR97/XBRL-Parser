from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import ArtifactState, JobState, JobType
from sec_xbrl_warehouse.domain.models import (
    DatasetArtifact,
    FilingPackageManifest,
    FilingRef,
    JobRecord,
    SourceArtifactRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.exceptions import StateTransitionError
from sec_xbrl_warehouse.infrastructure.storage.postgres_models import (
    FilingRow,
    IngestionJobRow,
    JobCheckpointRow,
    SourceArtifactRow,
    ValidationIssueRow,
)

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
        async with self.sessions() as session:
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
        statement = (
            pg_insert(JobCheckpointRow)
            .values(job_id=job_id, stage=stage, checkpoint_key=key, payload=payload)
            .on_conflict_do_update(constraint="uq_job_checkpoint", set_={"payload": payload})
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)

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

    async def claim_job(self, worker_id: str, lock_timeout_seconds: int) -> JobRecord | None:
        """Atomically claim one ready or stale job using `SKIP LOCKED`.

        Acceptance tests must run two concurrent transactions and prove that the
        same UUID cannot be returned twice. Stale `running` jobs are reclaimable
        after `lock_timeout_seconds`.
        """
        # Compare freshness against database time: `available_at` is populated by
        # PostgreSQL's clock, so a host-computed "now" can disagree after a
        # container restart or in high-latency deployments.
        stale_before = func.now() - timedelta(seconds=lock_timeout_seconds)
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(IngestionJobRow)
                .where(
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
                    )
                )
                .order_by(IngestionJobRow.priority.asc(), IngestionJobRow.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            if row.attempts >= row.max_attempts:
                row.state = JobState.DEAD.value
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
    ) -> None:
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
            can_retry = retryable and row.attempts < row.max_attempts
            row.state = JobState.RETRY.value if can_retry else JobState.DEAD.value
            row.available_at = datetime.now(UTC) + timedelta(
                seconds=min(3600, 2 ** max(row.attempts, 1))
            )
            row.last_error = error[:16_000]
            row.locked_at = None
            row.locked_by = None
            row.heartbeat_at = None

    async def upsert_filing(self, filing: FilingRef) -> None:
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
        statement = pg_insert(FilingRow).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[FilingRow.accession],
            set_={key: value for key, value in values.items() if key != "accession"},
        )
        async with self.sessions.begin() as session:
            await session.execute(statement)

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
