"""Tag validation issues with the extraction revision that raised them.

Revision ID: 0002_validation_issue_revision
Revises: 0001_control_plane
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_validation_issue_revision"
down_revision: str | None = "0001_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows stay NULL: their revision was never recorded.
    op.add_column(
        "validation_issues", sa.Column("extraction_revision", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_validation_accession_revision",
        "validation_issues",
        ["accession", "extraction_revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_validation_accession_revision", table_name="validation_issues")
    op.drop_column("validation_issues", "extraction_revision")
