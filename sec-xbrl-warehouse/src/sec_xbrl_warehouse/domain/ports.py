from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import ArtifactState, JobType, SecTable
from sec_xbrl_warehouse.domain.models import (
    Company,
    DatasetArtifact,
    FilingPackageManifest,
    FilingRef,
    JobRecord,
    QueueSnapshot,
    SourceArtifactRef,
    StoredObject,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle


@runtime_checkable
class ObjectStore(Protocol):
    def ensure_bucket(self) -> None: ...

    def exists(self, key: str) -> bool: ...

    def put_file(
        self,
        source: Path,
        key: str,
        *,
        expected_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    def get_file(
        self, key: str, destination: Path, *, max_bytes: int | None = None
    ) -> StoredObject: ...

    def put_bytes(
        self,
        data: bytes,
        key: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class ControlRepository(Protocol):
    async def get_source_artifact(self, artifact_id: UUID) -> SourceArtifactRef: ...

    async def get_checkpoint(
        self, job_id: UUID, stage: str, key: str = "default"
    ) -> dict[str, Any] | None: ...

    async def save_checkpoint(
        self, job_id: UUID, stage: str, payload: dict[str, Any], key: str = "default"
    ) -> None: ...

    async def upsert_source_artifact(
        self,
        artifact: DatasetArtifact,
        *,
        checksum_sha256: str | None = None,
        object_key: str | None = None,
    ) -> UUID: ...

    async def mark_artifact_state(
        self,
        artifact_id: UUID,
        state: ArtifactState,
        *,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    async def enqueue_job(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 100,
        max_attempts: int = 5,
    ) -> UUID: ...

    async def list_jobs(
        self, *, state: str | None = None, job_type: str | None = None, limit: int = 100
    ) -> list[JobRecord]: ...

    async def retry_job(self, job_id: UUID) -> JobRecord: ...

    async def queue_snapshot(self) -> QueueSnapshot: ...

    async def enqueue_job_if_absent(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 100,
        max_attempts: int = 5,
    ) -> UUID | None: ...

    async def published_filing_revisions(
        self, accessions: Sequence[str] | None = None
    ) -> list[tuple[str, int]]: ...

    async def upsert_company(self, company: Company) -> None: ...

    async def get_company(self, cik: int) -> Company | None: ...

    async def find_company_by_ticker(self, ticker: str) -> Company | None: ...

    async def known_company_ciks(self, ciks: Sequence[int]) -> set[int]: ...

    async def extraction_checkpoint(
        self, accession: str, revision: int
    ) -> dict[str, Any] | None: ...

    async def claim_job(
        self,
        worker_id: str,
        lock_timeout_seconds: int,
        job_types: Sequence[JobType] | None = None,
    ) -> JobRecord | None: ...

    async def complete_job(
        self,
        job_id: UUID,
        result: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> None: ...

    async def fail_job(
        self,
        job_id: UUID,
        error: str,
        retryable: bool,
        *,
        worker_id: str | None = None,
        attempt: int | None = None,
        retry_at: datetime | None = None,
    ) -> None: ...

    async def upsert_filing(self, filing: FilingRef, *, keep_derived: bool = False) -> None:
        """Insert or update a filing row.

        With `keep_derived`, stored `fiscal_year`, `fiscal_period`, and
        `report_date` are not replaced by NULL (discovery metadata lacks them).
        """
        ...

    async def update_filing_package(
        self, manifest: FilingPackageManifest, revision: int | None = None
    ) -> None: ...

    async def record_validation_issues(self, issues: Sequence[ValidationIssue]) -> None: ...


@runtime_checkable
class Warehouse(Protocol):
    def ping(self) -> bool: ...

    def insert_sec_batch(self, table: SecTable, batch: Any) -> int: ...

    def insert_xbrl_bundle(self, bundle: XbrlExtractionBundle) -> dict[str, int]: ...

    def publish_xbrl_bundle(self, bundle: XbrlExtractionBundle, counts: dict[str, int]) -> None: ...

    def insert_canonical_facts(self, facts: Sequence[CanonicalFact]) -> int: ...

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class XbrlExtractor(Protocol):
    def extract(
        self,
        entrypoint: Path,
        filing: FilingRef,
        *,
        extraction_revision: int = 1,
    ) -> XbrlExtractionBundle: ...


@runtime_checkable
class FilingDiscovery(Protocol):
    async def discover(self, *args: Any, **kwargs: Any) -> Sequence[FilingRef]: ...


@runtime_checkable
class FilingPackageStore(Protocol):
    async def download_package(self, filing: FilingRef) -> FilingPackageManifest: ...

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path: ...

    def archive_package(
        self, manifest: FilingPackageManifest, package_directory: Path
    ) -> FilingPackageManifest: ...

    def delete_source_files(self, manifest: FilingPackageManifest) -> None: ...


@runtime_checkable
class BatchWriter(Protocol):
    def write_batches(
        self,
        batches: Iterable[Any],
        *,
        destination_prefix: str,
        partition_values: dict[str, str],
    ) -> tuple[StoredObject, ...]: ...
