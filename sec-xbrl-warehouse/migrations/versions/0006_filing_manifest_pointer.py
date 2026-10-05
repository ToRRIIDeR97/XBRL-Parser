"""Point each filing at its stored package manifest so parse jobs can find it.

Revision ID: 0006_filing_manifest_pointer
Revises: 0005_job_deferral
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_filing_manifest_pointer"
down_revision: str | None = "0005_job_deferral"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filings", sa.Column("manifest_object_key", sa.Text(), nullable=True))
    op.add_column(
        "filings", sa.Column("manifest_checksum_sha256", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("filings", "manifest_checksum_sha256")
    op.drop_column("filings", "manifest_object_key")
