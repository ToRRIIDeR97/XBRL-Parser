"""Make the mapping review identity the concept instead of the fact.

Open reviews are collapsed to one per (concept_namespace, concept_name): the
earliest open review is kept and the others are marked `superseded` (not
deleted), with `resolution.superseded_by` naming the kept review. Per-filing
counts move to `mapping_review_observations`. Resolved reviews are unchanged.

Downgrade reopens the superseded reviews, drops the observation table and the
concept columns, and restores the per-fact unique constraint. It fails if new
reviews created after the upgrade repeat an `(accession, fact_id)` pair.

Revision ID: 0003_mapping_review_per_concept
Revises: 0002_validation_issue_revision
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_mapping_review_per_concept"
down_revision: str | None = "0002_validation_issue_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mapping_reviews", sa.Column("concept_namespace", sa.Text(), nullable=True))
    op.add_column("mapping_reviews", sa.Column("concept_name", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE mapping_reviews
        SET concept_namespace = coalesce(evidence->>'namespace', ''),
            concept_name = coalesce(evidence->>'concept_name', concept_qname)
        """
    )
    op.alter_column("mapping_reviews", "concept_namespace", nullable=False)
    op.alter_column("mapping_reviews", "concept_name", nullable=False)

    op.create_table(
        "mapping_review_observations",
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mapping_reviews.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("accession", sa.String(20), primary_key=True),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("sample_fact_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # Kept review per open concept: the earliest one, so existing IDs stay stable.
    op.execute(
        """
        CREATE TEMPORARY TABLE review_keepers AS
        SELECT id AS review_id, keeper_id, latest_id
        FROM (
            SELECT id,
                   first_value(id) OVER concept_reviews AS keeper_id,
                   last_value(id) OVER concept_reviews AS latest_id
            FROM mapping_reviews
            WHERE state = 'open'
            WINDOW concept_reviews AS (
                PARTITION BY concept_namespace, concept_name
                ORDER BY created_at, id
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            )
        ) ranked
        """
    )
    op.execute(
        """
        INSERT INTO mapping_review_observations (review_id, accession, fact_count, sample_fact_ids)
        SELECT keepers.keeper_id,
               review.accession,
               count(*),
               (array_to_json((array_agg(review.fact_id ORDER BY review.created_at, review.id))[1:5]))::jsonb
        FROM mapping_reviews AS review
        JOIN review_keepers AS keepers ON keepers.review_id = review.id
        GROUP BY keepers.keeper_id, review.accession
        """
    )
    op.execute(
        """
        UPDATE mapping_reviews AS review
        SET state = 'superseded',
            resolution = jsonb_build_object('superseded_by', keepers.keeper_id::text),
            resolved_at = now()
        FROM review_keepers AS keepers
        WHERE keepers.review_id = review.id AND keepers.keeper_id <> review.id
        """
    )
    op.execute(
        """
        UPDATE mapping_reviews AS review
        SET evidence = review.evidence || jsonb_build_object(
                'fact_count', totals.fact_count,
                'filing_count', totals.filing_count,
                'latest_accession', totals.latest_accession,
                'sample_fact_ids', totals.sample_fact_ids
            )
        FROM (
            SELECT review_id,
                   (SELECT latest.accession FROM review_keepers AS keepers
                    JOIN mapping_reviews AS latest ON latest.id = keepers.latest_id
                    WHERE keepers.review_id = obs.review_id) AS latest_accession,
                   sum(fact_count) AS fact_count,
                   count(*) AS filing_count,
                   (SELECT sample_fact_ids FROM mapping_review_observations AS first
                    WHERE first.review_id = obs.review_id
                    ORDER BY first.accession LIMIT 1) AS sample_fact_ids
            FROM mapping_review_observations AS obs
            GROUP BY review_id
        ) AS totals
        WHERE review.id = totals.review_id
        """
    )
    op.execute("DROP TABLE review_keepers")
    op.drop_constraint("uq_mapping_review_fact", "mapping_reviews", type_="unique")
    op.create_index(
        "uq_mapping_review_open_concept",
        "mapping_reviews",
        ["concept_namespace", "concept_name"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_mapping_review_open_concept", table_name="mapping_reviews")
    op.execute(
        """
        UPDATE mapping_reviews
        SET state = 'open', resolution = NULL, resolved_at = NULL
        WHERE state = 'superseded' AND resolution ? 'superseded_by'
        """
    )
    op.drop_table("mapping_review_observations")
    op.drop_column("mapping_reviews", "concept_name")
    op.drop_column("mapping_reviews", "concept_namespace")
    op.create_unique_constraint(
        "uq_mapping_review_fact", "mapping_reviews", ["accession", "fact_id"]
    )
