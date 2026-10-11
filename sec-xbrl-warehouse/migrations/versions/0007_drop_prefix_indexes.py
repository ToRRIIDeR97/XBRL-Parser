"""Drop indexes that are a leading prefix of another index.

`ix_validation_accession` (accession) is covered by
`ix_validation_accession_revision` (accession, extraction_revision), and
`ix_source_artifacts_dataset_period` (dataset, period) by the unique constraint
`uq_source_artifact_period_revision` (dataset, period, revision). Lookups keep using
the wider index; each write maintains one index fewer.

Revision ID: 0007_drop_prefix_indexes
Revises: 0006_filing_manifest_pointer
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_drop_prefix_indexes"
down_revision: str | None = "0006_filing_manifest_pointer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_validation_accession", table_name="validation_issues")
    op.drop_index("ix_source_artifacts_dataset_period", table_name="source_artifacts")


def downgrade() -> None:
    op.create_index("ix_source_artifacts_dataset_period", "source_artifacts", ["dataset", "period"])
    op.create_index("ix_validation_accession", "validation_issues", ["accession"])
