"""Add the companies table from SEC submissions data.

Revision ID: 0004_companies
Revises: 0003_mapping_review_per_concept
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_companies"
down_revision: str | None = "0003_mapping_review_per_concept"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("cik", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("sic", sa.Integer(), nullable=True),
        sa.Column("sic_description", sa.Text(), nullable=True),
        sa.Column("tickers", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("exchanges", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("fiscal_year_end", sa.String(4), nullable=True),
        sa.Column("state_of_incorporation", sa.Text(), nullable=True),
        sa.Column("filer_category", sa.Text(), nullable=True),
        sa.Column(
            "former_names",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("source_checksum", sa.String(64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Ticker lookups are case-insensitive; tickers are stored upper-cased.
    op.create_index("ix_companies_tickers", "companies", ["tickers"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_companies_tickers", table_name="companies")
    op.drop_table("companies")
