from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalMetric, ConceptMapping
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact
from sec_xbrl_warehouse.infrastructure.storage.postgres_models import (
    CanonicalMetricRow,
    ConceptMappingRow,
    MappingReviewRow,
)
from sec_xbrl_warehouse.mapping.canonical_mapper import ReviewRequest


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
                        ConceptMappingRow.cik.is_(None) | (ConceptMappingRow.cik == cik),
                        ConceptMappingRow.taxonomy_version.is_(None)
                        | (ConceptMappingRow.taxonomy_version == taxonomy_version),
                        ConceptMappingRow.valid_from.is_(None)
                        | (ConceptMappingRow.valid_from <= filed_date),
                        ConceptMappingRow.valid_to.is_(None)
                        | (ConceptMappingRow.valid_to >= filed_date),
                    )
                )
            ).all()
        if not rows:
            return ()

        latest: dict[tuple[object, ...], tuple[ConceptMappingRow, str]] = {}
        for row, metric_code in rows:
            key = (metric_code, row.cik, row.taxonomy_version)
            if key not in latest or latest[key][0].mapping_version < row.mapping_version:
                latest[key] = (row, metric_code)
        best = max(
            (row.cik is not None, row.taxonomy_version is not None) for row, _ in latest.values()
        )
        return tuple(
            _to_concept_mapping(row, metric_code)
            for row, metric_code in latest.values()
            if (row.cik is not None, row.taxonomy_version is not None) == best
        )

    async def seed_metrics(self, metrics: Sequence[CanonicalMetric]) -> int:
        async with self.sessions.begin() as session:
            for metric in metrics:
                values = metric.model_dump()
                statement = pg_insert(CanonicalMetricRow).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[CanonicalMetricRow.code],
                        set_={key: value for key, value in values.items() if key != "code"},
                    )
                )
        return len(metrics)

    async def approve_mapping(
        self,
        *,
        metric: str,
        namespace: str,
        concept_name: str,
        explanation: str,
        created_by: str,
        taxonomy_version: str | None = None,
        cik: int | None = None,
        valid_from: date | None = None,
        valid_to: date | None = None,
    ) -> ConceptMapping:
        if not all(
            (
                metric.strip(),
                namespace.strip(),
                concept_name.strip(),
                explanation.strip(),
                created_by.strip(),
            )
        ):
            raise ValueError("Metric, concept, namespace, rationale, and reviewer are required")
        if valid_from and valid_to and valid_from > valid_to:
            raise ValueError("Mapping validity dates are reversed")
        async with self.sessions.begin() as session:
            metric_id = await session.scalar(
                select(CanonicalMetricRow.id).where(CanonicalMetricRow.code == metric)
            )
            if metric_id is None:
                raise ValueError(
                    f"Unknown canonical metric: {metric}; import metric definitions first"
                )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"mapping:{metric}:{namespace}:{concept_name}:{taxonomy_version}:{cik}"},
            )
            version = (
                await session.scalar(
                    select(func.max(ConceptMappingRow.mapping_version)).where(
                        ConceptMappingRow.metric_id == metric_id,
                        ConceptMappingRow.namespace == namespace,
                        ConceptMappingRow.concept_name == concept_name,
                        ConceptMappingRow.cik == cik,
                        ConceptMappingRow.taxonomy_version == taxonomy_version,
                    )
                )
                or 0
            ) + 1
            row = ConceptMappingRow(
                metric_id=metric_id,
                namespace=namespace,
                concept_name=concept_name,
                taxonomy_version=taxonomy_version,
                cik=cik,
                valid_from=valid_from,
                valid_to=valid_to,
                status="approved",
                confidence=1,
                rationale=explanation,
                mapping_version=version,
                created_by=created_by,
            )
            session.add(row)
            await session.flush()
            return _to_concept_mapping(row, metric)

    async def list_reviews(self, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        if not 1 <= limit <= 10000 or offset < 0:
            raise ValueError("Invalid review pagination")
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(MappingReviewRow)
                    .where(MappingReviewRow.state == "open")
                    .order_by(MappingReviewRow.created_at, MappingReviewRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [
                {
                    "id": str(row.id),
                    "accession": row.accession,
                    "fact_id": row.fact_id,
                    "concept": row.concept_qname,
                    "candidates": row.candidate_metric_codes,
                    "evidence": row.evidence,
                }
                for row in rows
            ]

    async def resolve_review(self, review_id: UUID, mapping: ConceptMapping) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(MappingReviewRow, review_id, with_for_update=True)
            if row is None:
                raise KeyError(review_id)
            evidence = row.evidence
            if (
                evidence.get("namespace") != mapping.namespace
                or evidence.get("concept_name") != mapping.concept_name
            ):
                raise ValueError("The approved mapping does not match the review concept")
            await session.execute(
                update(MappingReviewRow)
                .where(MappingReviewRow.id == review_id)
                .values(
                    state="resolved",
                    resolution={"mapping_id": str(mapping.id), "version": mapping.version},
                    resolved_at=func.now(),
                )
            )

    async def create_review(
        self,
        *,
        filing: FilingRef,
        fact: XbrlFact,
        candidates: Sequence[str],
        evidence: dict[str, object],
    ) -> UUID:
        return (await self.create_reviews(filing, [(fact, candidates, evidence)]))[fact.fact_id]

    async def create_reviews(
        self, filing: FilingRef, reviews: Sequence[ReviewRequest]
    ) -> dict[str, UUID]:
        result: dict[str, UUID] = {}
        for offset in range(0, len(reviews), 500):
            rows = {
                fact.fact_id: {
                    "accession": filing.accession,
                    "fact_id": fact.fact_id,
                    "concept_qname": fact.concept_qname,
                    "candidate_metric_codes": list(candidates),
                    "evidence": {
                        **evidence,
                        "namespace": fact.concept_namespace,
                        "concept_name": fact.concept_name,
                        "taxonomy_version": fact.taxonomy_version,
                        "cik": filing.cik,
                    },
                    "state": "open",
                }
                for fact, candidates, evidence in reviews[offset : offset + 500]
            }
            async with self.sessions.begin() as session:
                inserted = await session.execute(
                    pg_insert(MappingReviewRow)
                    .values(list(rows.values()))
                    .on_conflict_do_nothing(constraint="uq_mapping_review_fact")
                    .returning(MappingReviewRow.fact_id, MappingReviewRow.id)
                )
                found = {fact_id: review_id for fact_id, review_id in inserted.all()}
                missing = rows.keys() - found.keys()
                if missing:
                    existing = await session.execute(
                        select(MappingReviewRow.fact_id, MappingReviewRow.id).where(
                            MappingReviewRow.accession == filing.accession,
                            MappingReviewRow.fact_id.in_(missing),
                        )
                    )
                    found.update({fact_id: review_id for fact_id, review_id in existing.all()})
                if found.keys() != rows.keys():
                    raise RuntimeError("PostgreSQL did not return all review IDs")
                result.update(found)
        return result


def _to_concept_mapping(row: ConceptMappingRow, metric_code: str) -> ConceptMapping:
    return ConceptMapping(
        id=row.id,
        namespace=row.namespace,
        concept_name=row.concept_name,
        taxonomy_version=row.taxonomy_version,
        cik=row.cik,
        canonical_metric=metric_code,
        mapping_type="filing_specific"
        if row.cik is not None
        else "exact"
        if row.taxonomy_version
        else "namespace",
        confidence=float(row.confidence),
        approved=row.status == "approved",
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        explanation=row.rationale,
        version=row.mapping_version,
    )
