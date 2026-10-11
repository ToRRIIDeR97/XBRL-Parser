from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SourceArtifactRow(TimestampMixin, Base):
    __tablename__ = "source_artifacts"
    __table_args__ = (
        UniqueConstraint("source_url", "revision", name="uq_source_artifact_url_revision"),
        UniqueConstraint(
            "dataset", "period", "revision", name="uq_source_artifact_period_revision"
        ),
        Index("ix_source_artifacts_state", "state"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    published_label: Mapped[str] = mapped_column(Text, nullable=False)
    expected_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    object_key: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="discovered", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class FilingRow(TimestampMixin, Base):
    __tablename__ = "filings"
    __table_args__ = (
        Index("ix_filings_cik_filed_date", "cik", "filed_date"),
        Index("ix_filings_form_filed_date", "form", "filed_date"),
    )

    accession: Mapped[str] = mapped_column(String(128), primary_key=True)
    cik: Mapped[int] = mapped_column(BigInteger, nullable=False)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    form: Mapped[str] = mapped_column(String(32), nullable=False)
    filed_date: Mapped[date] = mapped_column(Date, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_date: Mapped[date | None] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column(SmallInteger)
    fiscal_period: Mapped[str | None] = mapped_column(String(16))
    primary_document: Mapped[str | None] = mapped_column(Text)
    instance_document: Mapped[str | None] = mapped_column(Text)
    archive_directory_url: Mapped[str] = mapped_column(Text, nullable=False)
    primary_document_url: Mapped[str | None] = mapped_column(Text)
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_inline_xbrl: Mapped[bool | None] = mapped_column(Boolean)
    package_object_key: Mapped[str | None] = mapped_column(Text)
    package_checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    # The stored package manifest (`manifests/...`) that parse jobs load.
    manifest_object_key: Mapped[str | None] = mapped_column(Text)
    manifest_checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    latest_extraction_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class IngestionJobRow(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "state", "available_at", "priority"),
        Index("ix_jobs_locked", "locked_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # First deferral on a busy SEC egress lock since the job last ran; caps deferral.
    deferred_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobCheckpointRow(Base):
    __tablename__ = "job_checkpoints"
    __table_args__ = (
        UniqueConstraint("job_id", "stage", "checkpoint_key", name="uq_job_checkpoint"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ValidationIssueRow(Base):
    __tablename__ = "validation_issues"
    __table_args__ = (
        Index("ix_validation_artifact", "artifact_id"),
        Index("ix_validation_code_severity", "code", "severity"),
        Index("ix_validation_accession_revision", "accession", "extraction_revision"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    accession: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("filings.accession", ondelete="SET NULL")
    )
    artifact_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_artifacts.id", ondelete="SET NULL")
    )
    table_name: Mapped[str | None] = mapped_column(String(32))
    source_document: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    column_number: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    extraction_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestionRunRow(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    software_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CanonicalMetricRow(TimestampMixin, Base):
    __tablename__ = "canonical_metrics"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(String(64), nullable=False)
    period_type: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_family: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    formula: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ConceptMappingRow(Base):
    __tablename__ = "concept_mappings"
    __table_args__ = (
        UniqueConstraint(
            "metric_id",
            "namespace",
            "concept_name",
            "taxonomy_version",
            "cik",
            "mapping_version",
            name="uq_concept_mapping_version",
        ),
        Index(
            "ix_mapping_lookup",
            "namespace",
            "concept_name",
            "taxonomy_version",
            "cik",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    metric_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("canonical_metrics.id", ondelete="RESTRICT"),
        nullable=False,
    )
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    concept_name: Mapped[str] = mapped_column(Text, nullable=False)
    taxonomy_version: Mapped[str | None] = mapped_column(Text)
    cik: Mapped[int | None] = mapped_column(BigInteger)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MappingReviewRow(Base):
    """One review per concept while open; `accession`/`fact_id` are the latest sample."""

    __tablename__ = "mapping_reviews"
    __table_args__ = (
        Index(
            "uq_mapping_review_open_concept",
            "concept_namespace",
            "concept_name",
            unique=True,
            postgresql_where=text("state = 'open'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    accession: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    concept_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    concept_name: Mapped[str] = mapped_column(Text, nullable=False)
    concept_qname: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_metric_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MappingReviewObservationRow(Base):
    """Unmapped facts of a reviewed concept in one filing (idempotent per accession)."""

    __tablename__ = "mapping_review_observations"

    review_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("mapping_reviews.id", ondelete="CASCADE"),
        primary_key=True,
    )
    accession: Mapped[str] = mapped_column(String(128), primary_key=True)
    fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_fact_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EntityKeyRow(Base):
    """Entity key allocated to a non-SEC `(scheme, identifier)`; never reused (0008)."""

    __tablename__ = "entity_keys"
    __table_args__ = (
        UniqueConstraint("scheme", "identifier", name="uq_entity_keys_scheme_identifier"),
        CheckConstraint("entity_key >= 10000000000", name="ck_entity_keys_range"),
    )

    entity_key: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        server_default=text("nextval('entity_key_seq')"),
    )
    scheme: Mapped[str] = mapped_column(Text, nullable=False)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CompanyRow(TimestampMixin, Base):
    """Issuer metadata from SEC submissions; tickers are upper-cased and unique per holder."""

    __tablename__ = "companies"
    __table_args__ = (Index("ix_companies_tickers", "tickers", postgresql_using="gin"),)

    cik: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    sic: Mapped[int | None] = mapped_column(Integer)
    sic_description: Mapped[str | None] = mapped_column(Text)
    tickers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}", default=list
    )
    exchanges: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}", default=list
    )
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))
    state_of_incorporation: Mapped[str | None] = mapped_column(Text)
    filer_category: Mapped[str | None] = mapped_column(Text)
    former_names: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )
    source_checksum: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
