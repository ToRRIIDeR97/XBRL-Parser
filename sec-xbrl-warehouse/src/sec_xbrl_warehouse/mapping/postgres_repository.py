from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import ConceptMapping
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact
from sec_xbrl_warehouse.infrastructure.storage.postgres_models import (
    CanonicalMetricRow,
    ConceptMappingRow,
    MappingReviewRow,
)


class PostgresMappingRepository:
    """PostgreSQL adapter for approved concept mappings and review queue."""

    def __init__(self, settings: Settings, engine: AsyncEngine | None = None) -> None:
        self.settings = settings
        self.engine = engine or create_async_engine(
            settings.postgres_dsn,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def approved_mappings_for_concept(
        self,
        namespace: str,
        concept_name: str,
        taxonomy_version: str | None,
        cik: int,
        filed_date: date,
    ) -> Sequence[ConceptMapping]:
        """Return approved mappings valid on the filed date, most specific first.

        Specificity order: exact CIK match, then taxonomy-version match, then
        global namespace match. Within equal specificity, the highest mapping
        version wins (append-only history means the latest is authoritative).
        """
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ConceptMappingRow, CanonicalMetricRow.code)
                    .join(
                        CanonicalMetricRow,
                        ConceptMappingRow.metric_id == CanonicalMetricRow.id,
                    )
                    .where(
                        ConceptMappingRow.namespace == namespace,
                        ConceptMappingRow.concept_name == concept_name,
                        ConceptMappingRow.status == "approved",
                        ConceptMappingRow.valid_from.is_(None)
                        | (ConceptMappingRow.valid_from <= filed_date),
                        ConceptMappingRow.valid_to.is_(None)
                        | (ConceptMappingRow.valid_to >= filed_date),
                    )
                )
            ).all()
        if not rows:
            return ()

        scored: list[tuple[int, int, int, ConceptMapping]] = []
        for row, metric_code in rows:
            exact_cik = row.cik == cik
            exact_version = row.taxonomy_version == taxonomy_version
            score = (2 if exact_cik else 0) + (2 if exact_version else 0)
            scored.append(
                (
                    score,
                    row.mapping_version,
                    -row.mapping_version,
                    _to_concept_mapping(row, metric_code),
                )
            )
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[3] for item in scored)

    async def create_review(
        self,
        *,
        filing: FilingRef,
        fact: XbrlFact,
        candidates: Sequence[str],
        evidence: dict[str, object],
    ) -> UUID:
        """Create an open review candidate for an unmapped fact."""
        async with self.sessions.begin() as session:
            row = MappingReviewRow(
                accession=filing.accession,
                fact_id=fact.fact_id,
                concept_qname=fact.concept_qname,
                candidate_metric_codes=list(candidates),
                evidence=dict(evidence),
                state="open",
            )
            session.add(row)
            await session.flush()
            return row.id


def _to_concept_mapping(row: ConceptMappingRow, metric_code: str) -> ConceptMapping:
    return ConceptMapping(
        id=row.id,
        namespace=row.namespace,
        concept_name=row.concept_name,
        taxonomy_version=row.taxonomy_version,
        canonical_metric=metric_code,
        mapping_type="exact" if row.cik is not None else "namespace",
        confidence=float(row.confidence),
        approved=row.status == "approved",
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        explanation=row.rationale,
        version=row.mapping_version,
    )
