"""Source-neutral filing keys and entity keys.

Every `accession` column widens to 128 characters so it can hold a filing key
(`<source>-<native id>`, domain/sources.py) as well as an SEC accession. The
`entity_keys` table maps a non-SEC `(scheme, identifier)` to an entity key allocated
from `entity_key_seq`, which starts at 10^10, above every SEC CIK. Widening a VARCHAR
needs no table rewrite.

The downgrade is refused while a non-SEC filing key or entity key exists: shrinking
the columns would truncate keys, and dropping `entity_keys` would let a later upgrade
hand out the same numbers again.

Revision ID: 0008_filing_entity_keys
Revises: 0007_drop_prefix_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_filing_entity_keys"
down_revision: str | None = "0007_drop_prefix_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCESSION_COLUMNS = (
    ("filings", False),
    ("validation_issues", True),
    ("mapping_reviews", False),
    ("mapping_review_observations", False),
)
ENTITY_KEY_BASE = 10_000_000_000


def upgrade() -> None:
    for table, nullable in ACCESSION_COLUMNS:
        op.alter_column(
            table,
            "accession",
            type_=sa.String(128),
            existing_type=sa.String(20),
            existing_nullable=nullable,
        )
    op.execute(
        f"CREATE SEQUENCE entity_key_seq AS BIGINT START {ENTITY_KEY_BASE} "
        f"MINVALUE {ENTITY_KEY_BASE} NO CYCLE"
    )
    op.create_table(
        "entity_keys",
        sa.Column(
            "entity_key",
            sa.BigInteger(),
            server_default=sa.text("nextval('entity_key_seq')"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("scheme", sa.Text(), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("scheme", "identifier", name="uq_entity_keys_scheme_identifier"),
        sa.CheckConstraint(f"entity_key >= {ENTITY_KEY_BASE}", name="ck_entity_keys_range"),
    )
    op.execute("ALTER SEQUENCE entity_key_seq OWNED BY entity_keys.entity_key")


def downgrade() -> None:
    connection = op.get_bind()
    checks = [
        f"SELECT count(*) FROM {table} WHERE accession !~ '^[0-9]{{10}}-[0-9]{{2}}-[0-9]{{6}}$'"
        for table, _ in ACCESSION_COLUMNS
    ]
    checks.append(f"SELECT count(*) FROM filings WHERE cik >= {ENTITY_KEY_BASE}")
    checks.append("SELECT count(*) FROM entity_keys")
    found = sum(int(connection.execute(sa.text(check)).scalar_one()) for check in checks)
    if found:
        raise RuntimeError(
            f"Refusing to downgrade 0008: {found} rows hold non-SEC filing or entity keys"
        )
    op.drop_table("entity_keys")  # drops the owned sequence too
    for table, nullable in reversed(ACCESSION_COLUMNS):
        op.alter_column(
            table,
            "accession",
            type_=sa.String(20),
            existing_type=sa.String(128),
            existing_nullable=nullable,
        )
