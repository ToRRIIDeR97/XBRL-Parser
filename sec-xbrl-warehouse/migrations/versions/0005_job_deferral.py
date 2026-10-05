"""Record when a job was first deferred on a busy SEC egress lock.

Revision ID: 0005_job_deferral
Revises: 0004_companies
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_job_deferral"
down_revision: str | None = "0004_companies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("deferred_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "deferred_since")
