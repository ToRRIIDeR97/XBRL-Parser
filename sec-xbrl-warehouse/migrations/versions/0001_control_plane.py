"""Create the PostgreSQL control-plane schema.

Revision ID: 0001_control_plane
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_control_plane"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dataset", sa.String(32), nullable=False),
        sa.Column("period", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("published_label", sa.Text(), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(32), nullable=False, server_default="discovered"),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("source_url", "revision", name="uq_source_artifact_url_revision"),
        sa.UniqueConstraint(
            "dataset", "period", "revision", name="uq_source_artifact_period_revision"
        ),
    )
    op.create_index("ix_source_artifacts_state", "source_artifacts", ["state"])
    op.create_index("ix_source_artifacts_dataset_period", "source_artifacts", ["dataset", "period"])

    op.create_table(
        "filings",
        sa.Column("accession", sa.String(20), primary_key=True),
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("form", sa.String(32), nullable=False),
        sa.Column("filed_date", sa.Date(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("fiscal_period", sa.String(16), nullable=True),
        sa.Column("primary_document", sa.Text(), nullable=True),
        sa.Column("instance_document", sa.Text(), nullable=True),
        sa.Column("archive_directory_url", sa.Text(), nullable=False),
        sa.Column("primary_document_url", sa.Text(), nullable=True),
        sa.Column("is_amendment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_inline_xbrl", sa.Boolean(), nullable=True),
        sa.Column("package_object_key", sa.Text(), nullable=True),
        sa.Column("package_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("latest_extraction_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_filings_cik_filed_date", "filings", ["cik", "filed_date"])
    op.create_index("ix_filings_form_filed_date", "filings", ["form", "filed_date"])

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_claim", "ingestion_jobs", ["state", "available_at", "priority"])
    op.create_index("ix_jobs_locked", "ingestion_jobs", ["locked_at"])

    op.create_table(
        "job_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("checkpoint_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("job_id", "stage", "checkpoint_key", name="uq_job_checkpoint"),
    )

    op.create_table(
        "validation_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "accession",
            sa.String(20),
            sa.ForeignKey("filings.accession", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("table_name", sa.String(32), nullable=True),
        sa.Column("source_document", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("column_number", sa.Integer(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_validation_accession", "validation_issues", ["accession"])
    op.create_index("ix_validation_artifact", "validation_issues", ["artifact_id"])
    op.create_index("ix_validation_code_severity", "validation_issues", ["code", "severity"])

    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_type", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("software_version", sa.String(64), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "canonical_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("statement", sa.String(64), nullable=False),
        sa.Column("period_type", sa.String(32), nullable=False),
        sa.Column("unit_family", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("formula", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "concept_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "metric_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_metrics.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("concept_name", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("cik", sa.BigInteger(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="1.0"),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text(), nullable=False, server_default="system"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "metric_id",
            "namespace",
            "concept_name",
            "taxonomy_version",
            "cik",
            "mapping_version",
            name="uq_concept_mapping_version",
        ),
    )
    op.create_index(
        "ix_mapping_lookup",
        "concept_mappings",
        ["namespace", "concept_name", "taxonomy_version", "cik", "status"],
    )

    op.create_table(
        "mapping_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("accession", sa.String(20), nullable=False),
        sa.Column("fact_id", sa.String(64), nullable=False),
        sa.Column("concept_qname", sa.Text(), nullable=False),
        sa.Column(
            "candidate_metric_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("accession", "fact_id", name="uq_mapping_review_fact"),
    )


def downgrade() -> None:
    for table in (
        "mapping_reviews",
        "concept_mappings",
        "canonical_metrics",
        "ingestion_runs",
        "validation_issues",
        "job_checkpoints",
        "ingestion_jobs",
        "filings",
        "source_artifacts",
    ):
        op.drop_table(table)
