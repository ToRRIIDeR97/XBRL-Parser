from __future__ import annotations

from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sec_xbrl_warehouse.domain.enums import (
    ArtifactState,
    DatasetKind,
    JobState,
    JobType,
    Severity,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetArtifact(DomainModel):
    dataset: DatasetKind
    period: str
    source_url: str
    filename: str
    published_label: str
    expected_size_bytes: int | None = None
    checksum_sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            not value
            or value in {".", ".."}
            or any(char in value for char in "/\\:\x00")
            or any(ord(char) < 32 for char in value)
            or value.endswith((".", " "))
            or PureWindowsPath(value).is_reserved()
        ):
            raise ValueError("Artifact filename must be a safe single filename")
        return value


class StoredObject(DomainModel):
    bucket: str
    key: str
    size_bytes: int
    checksum_sha256: str
    etag: str | None = None
    local_path: Path | None = None


class SourceArtifactRef(DomainModel):
    id: UUID
    dataset: DatasetKind
    period: str
    source_url: str
    object_key: str | None = None
    checksum_sha256: str | None = None
    revision: int = 1
    state: ArtifactState = ArtifactState.DISCOVERED
    created_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class FilingRef(DomainModel):
    accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    cik: int = Field(ge=0)
    company_name: str
    form: str
    filed_date: date
    accepted_at: datetime | None = None
    report_date: date | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    primary_document: str | None = None
    instance_document: str | None = None
    archive_directory_url: str
    primary_document_url: str | None = None
    is_amendment: bool = False
    is_inline_xbrl: bool | None = None


class FormerName(DomainModel):
    name: str
    valid_from: date | None = None
    valid_to: date | None = None


class Company(DomainModel):
    """Issuer metadata from the SEC submissions JSON (no contact data is kept)."""

    cik: int = Field(ge=0)
    name: str
    entity_type: str | None = None
    sic: int | None = None
    sic_description: str | None = None
    tickers: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    fiscal_year_end: str | None = Field(
        default=None, pattern=r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$"
    )
    state_of_incorporation: str | None = None
    filer_category: str | None = None
    former_names: tuple[FormerName, ...] = ()
    source_checksum: str | None = None
    fetched_at: datetime | None = None


class PackageMember(DomainModel):
    name: str
    source_url: str
    size_bytes: int | None = None
    content_type: str | None = None
    checksum_sha256: str | None = None
    is_primary_document: bool = False
    is_xbrl_entrypoint: bool = False
    # Listed archive (`<accession>-xbrl.zip`) this member was expanded from;
    # None for files fetched directly from the filing directory.
    source_archive: str | None = None


class FilingPackageManifest(DomainModel):
    filing: FilingRef
    members: tuple[PackageMember, ...]
    acquired_at: datetime
    manifest_object_key: str | None = None
    manifest_checksum_sha256: str | None = None
    package_object_key: str | None = None
    package_checksum_sha256: str | None = None


class BatchStats(DomainModel):
    table: str
    batch_number: int
    rows: int
    bytes_read: int | None = None
    bytes_written: int | None = None
    elapsed_seconds: float


class LoadStats(DomainModel):
    artifact_id: UUID
    source_period: str
    rows_by_table: dict[str, int]
    parquet_objects: tuple[str, ...] = ()
    elapsed_seconds: float
    validation_issue_count: int = 0


class ValidationIssue(DomainModel):
    source: str
    code: str
    severity: Severity
    message: str
    accession: str | None = None
    artifact_id: UUID | None = None
    table: str | None = None
    source_document: str | None = None
    line: int | None = None
    column: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    # Filing extraction revision that produced the issue; None for bulk loads.
    extraction_revision: int | None = None


class JobRecord(DomainModel):
    id: UUID
    job_type: JobType
    state: JobState
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    priority: int
    available_at: datetime
    locked_at: datetime | None = None
    locked_by: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class QueueSnapshot(DomainModel):
    """Job counts by `(job_type, state)` and the age of the oldest claimable job."""

    counts: dict[tuple[str, str], int]
    oldest_ready_age_seconds: float = 0.0


class BackfillPlan(DomainModel):
    generated_at: datetime
    dataset: DatasetKind
    from_year: int
    to_year: int
    artifacts: tuple[DatasetArtifact, ...]
    catalog_url: str
    plan_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_version: int = 1
