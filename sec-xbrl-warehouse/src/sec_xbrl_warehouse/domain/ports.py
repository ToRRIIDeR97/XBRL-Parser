from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import ArtifactState, SecTable
from sec_xbrl_warehouse.domain.models import (
    DatasetArtifact,
    FilingPackageManifest,
    FilingRef,
    JobRecord,
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

    def get_file(self, key: str, destination: Path) -> StoredObject: ...

    def put_bytes(
        self,
        data: bytes,
        key: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...


@runtime_checkable
class ControlRepository(Protocol):
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

    async def claim_job(self, worker_id: str, lock_timeout_seconds: int) -> JobRecord | None: ...

    async def complete_job(self, job_id: UUID, result: dict[str, Any] | None = None) -> None: ...

    async def fail_job(self, job_id: UUID, error: str, retryable: bool) -> None: ...

    async def upsert_filing(self, filing: FilingRef) -> None: ...

    async def record_validation_issues(self, issues: Sequence[ValidationIssue]) -> None: ...


@runtime_checkable
class Warehouse(Protocol):
    def ping(self) -> bool: ...

    def insert_sec_batch(self, table: SecTable, batch: Any) -> int: ...

    def insert_xbrl_bundle(self, bundle: XbrlExtractionBundle) -> dict[str, int]: ...

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


@runtime_checkable
class BatchWriter(Protocol):
    def write_batches(
        self,
        batches: Iterable[Any],
        *,
        destination_prefix: str,
        partition_values: dict[str, str],
    ) -> tuple[StoredObject, ...]: ...
