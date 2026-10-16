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
    MappingReviewObservationRow,
    MappingReviewRow,
)
from sec_xbrl_warehouse.mapping.canonical_mapper import ReviewRequest
from sec_xbrl_warehouse.mapping.namespaces import namespace_family


class PostgresMappingRepository:
    """PostgreSQL adapter for approved concept mappings and review queue."""

    def __init__(self, settings: Settings, engine: AsyncEngine | None = None) -> None:
        self.settings = settings
        self.engine = engine or create_async_engine(
            settings.postgres_dsn.get_secret_value(),
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
        """Return the most specific approved mappings valid on the filed date.

        A mapping's `namespace` is either an exact namespace URI or a family token
        (`us-gaap`, `ifrs-full`, `dei`, `srt`) that matches every standard
        namespace URI of that family; extension namespaces never match a family.
        Specificity order: CIK, then taxonomy version, then exact namespace URI,
        then family. Within equal specificity, the highest mapping version wins
        (append-only history means the latest is authoritative).
        """
        key = (namespace, concept_name, taxonomy_version)
        return (await self.approved_mappings_for_concepts([key], cik, filed_date))[key]

    async def approved_mappings_for_concepts(
        self,
        keys: Sequence[tuple[str, str, str | None]],
        cik: int,
        filed_date: date,
    ) -> dict[tuple[str, str, str | None], Sequence[ConceptMapping]]:
        """`approved_mappings_for_concept` for many `(namespace, name, version)` keys.

        One query per chunk of keys instead of one per concept; each key gets the
        same result the single-concept lookup returns.
        """
        result: dict[tuple[str, str, str | None], Sequence[ConceptMapping]] = {}
        unique = list(dict.fromkeys(keys))
        for start in range(0, len(unique), _MAPPING_KEY_CHUNK):
            chunk = unique[start : start + _MAPPING_KEY_CHUNK]
            namespaces = {
                candidate
                for namespace, _, _ in chunk
                for candidate in (namespace, namespace_family(namespace))
                if candidate is not None
            }
            versions = {version for _, _, version in chunk if version is not None}
            async with self.sessions() as session:
                rows = (
                    await session.execute(
                        select(ConceptMappingRow, CanonicalMetricRow.code)
                        .join(
                            CanonicalMetricRow,
                            ConceptMappingRow.metric_id == CanonicalMetricRow.id,
                        )
                        .where(
                            ConceptMappingRow.namespace.in_(sorted(namespaces)),
                            ConceptMappingRow.concept_name.in_(sorted({k[1] for k in chunk})),
                            ConceptMappingRow.status == "approved",
                            ConceptMappingRow.cik.is_(None) | (ConceptMappingRow.cik == cik),
                            ConceptMappingRow.taxonomy_version.is_(None)
                            | ConceptMappingRow.taxonomy_version.in_(sorted(versions)),
                            ConceptMappingRow.valid_from.is_(None)
                            | (ConceptMappingRow.valid_from <= filed_date),
                            ConceptMappingRow.valid_to.is_(None)
                            | (ConceptMappingRow.valid_to >= filed_date),
                        )
                    )
                ).all()
            by_name: dict[str, list[tuple[ConceptMappingRow, str]]] = {}
            for row, metric_code in rows:
                by_name.setdefault(row.concept_name, []).append((row, metric_code))
            for key in chunk:
                namespace, concept_name, taxonomy_version = key
                family = namespace_family(namespace)
                result[key] = _most_specific(
                    [
                        (row, code)
                        for row, code in by_name.get(concept_name, ())
                        if row.namespace in (namespace, family)
                        and row.taxonomy_version in (None, taxonomy_version)
                    ],
                    namespace,
                )
        return result

    async def seed_metrics(self, metrics: Sequence[CanonicalMetric]) -> int:
        async with self.sessions.begin() as session:
            for metric in metrics:
                # `additive` is series configuration read from the YAML, not stored.
                values = metric.model_dump(exclude={"additive"})
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
        """Approve a mapping; repeating an identical approval returns the existing version."""
        mapping, _ = await self.approve_mapping_once(
            metric=metric,
            namespace=namespace,
            concept_name=concept_name,
            explanation=explanation,
            created_by=created_by,
            taxonomy_version=taxonomy_version,
            cik=cik,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        return mapping

    async def identical_approval_exists(
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
    ) -> bool:
        """Whether `approve_mapping_once` with these arguments would return `created=False`."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(ConceptMappingRow)
                .join(CanonicalMetricRow, ConceptMappingRow.metric_id == CanonicalMetricRow.id)
                .where(
                    CanonicalMetricRow.code == metric,
                    ConceptMappingRow.namespace == namespace,
                    ConceptMappingRow.concept_name == concept_name,
                    ConceptMappingRow.cik.is_not_distinct_from(cik),
                    ConceptMappingRow.taxonomy_version.is_not_distinct_from(taxonomy_version),
                )
                .order_by(ConceptMappingRow.mapping_version.desc())
                .limit(1)
            )
        return _is_identical_approval(
            row,
            explanation=explanation,
            created_by=created_by,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    async def approve_mapping_once(
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
    ) -> tuple[ConceptMapping, bool]:
        """Return `(mapping, created)`.

        Mappings are append-only and versioned. If the latest version for this
        metric, namespace, concept, taxonomy version, and CIK is already approved
        with the same validity window, rationale, and reviewer, it is returned with
        `created=False`. A different reviewer or rationale records a new version.
        """
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
            current = await session.scalar(
                select(ConceptMappingRow)
                .where(
                    ConceptMappingRow.metric_id == metric_id,
                    ConceptMappingRow.namespace == namespace,
                    ConceptMappingRow.concept_name == concept_name,
                    ConceptMappingRow.cik.is_not_distinct_from(cik),
                    ConceptMappingRow.taxonomy_version.is_not_distinct_from(taxonomy_version),
                )
                .order_by(ConceptMappingRow.mapping_version.desc())
                .limit(1)
            )
            if _is_identical_approval(
                current,
                explanation=explanation,
                created_by=created_by,
                valid_from=valid_from,
                valid_to=valid_to,
            ):
                assert current is not None
                return _to_concept_mapping(current, metric), False
            version = (current.mapping_version if current is not None else 0) + 1
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
            return _to_concept_mapping(row, metric), True

    async def list_reviews(self, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        """Open reviews, one per concept, with cumulative evidence."""
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
                    "concept_namespace": row.concept_namespace,
                    "concept_name": row.concept_name,
                    "concept": row.concept_qname,
                    "fact_count": row.evidence.get("fact_count"),
                    "filing_count": row.evidence.get("filing_count"),
                    "latest_accession": row.evidence.get("latest_accession", row.accession),
                    "fact_id": row.fact_id,
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
            namespace = str(evidence.get("namespace") or "")
            # A family mapping (`us-gaap`) resolves reviews of any standard year namespace.
            if mapping.namespace not in {namespace, namespace_family(namespace)} or (
                evidence.get("concept_name") != mapping.concept_name
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
        """Open or update one review per concept and record this filing's observation.

        A concept with an open review gets its evidence replaced by this filing's
        decision; per-filing counts live in `mapping_review_observations`, keyed
        by accession, so replaying a filing doesn't inflate the cumulative
        `fact_count`/`filing_count`. Returns the review ID for every request's fact.
        """
        merged: dict[tuple[str, str], tuple[XbrlFact, Sequence[str], dict[str, object], int]] = {}
        samples: dict[tuple[str, str], list[str]] = {}
        fact_concepts: dict[str, tuple[str, str]] = {}
        for fact, candidates, evidence in reviews:
            concept = (fact.concept_namespace, fact.concept_name)
            fact_concepts[fact.fact_id] = concept
            count = _evidence_count(evidence)
            fact_samples = _evidence_samples(evidence, fact.fact_id)
            if concept in merged:
                first, first_candidates, first_evidence, total = merged[concept]
                merged[concept] = (first, first_candidates, first_evidence, total + count)
                samples[concept] = (samples[concept] + fact_samples)[:5]
            else:
                merged[concept] = (fact, candidates, evidence, count)
                samples[concept] = fact_samples[:5]

        review_ids: dict[tuple[str, str], UUID] = {}
        # Concept order fixes the row-lock order, so concurrent filings sharing
        # concepts can't deadlock on the upsert.
        items = sorted(merged.items())
        for offset in range(0, len(items), 500):
            chunk = items[offset : offset + 500]
            rows = [
                {
                    "accession": filing.accession,
                    "fact_id": fact.fact_id,
                    "concept_namespace": concept[0],
                    "concept_name": concept[1],
                    "concept_qname": fact.concept_qname,
                    "candidate_metric_codes": list(candidates),
                    "evidence": {
                        **evidence,
                        "namespace": fact.concept_namespace,
                        "concept_name": fact.concept_name,
                        "taxonomy_version": fact.taxonomy_version,
                        "cik": filing.cik,
                        "latest_accession": filing.accession,
                        "sample_fact_ids": samples[concept],
                    },
                    "state": "open",
                }
                for concept, (fact, candidates, evidence, _) in chunk
            ]
            async with self.sessions.begin() as session:
                statement = pg_insert(MappingReviewRow).values(rows)
                upserted = await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            MappingReviewRow.concept_namespace,
                            MappingReviewRow.concept_name,
                        ],
                        index_where=MappingReviewRow.state == "open",
                        set_={
                            "accession": statement.excluded.accession,
                            "fact_id": statement.excluded.fact_id,
                            "concept_qname": statement.excluded.concept_qname,
                            "candidate_metric_codes": statement.excluded.candidate_metric_codes,
                            "evidence": statement.excluded.evidence,
                        },
                    ).returning(
                        MappingReviewRow.concept_namespace,
                        MappingReviewRow.concept_name,
                        MappingReviewRow.id,
                    )
                )
                found = {(namespace, name): rid for namespace, name, rid in upserted.all()}
                if found.keys() != {concept for concept, _ in chunk}:
                    raise RuntimeError("PostgreSQL did not return all review IDs")
                observation = pg_insert(MappingReviewObservationRow).values(
                    [
                        {
                            "review_id": found[concept],
                            "accession": filing.accession,
                            "fact_count": count,
                            "sample_fact_ids": samples[concept],
                        }
                        for concept, (_, _, _, count) in chunk
                    ]
                )
                await session.execute(
                    observation.on_conflict_do_update(
                        index_elements=[
                            MappingReviewObservationRow.review_id,
                            MappingReviewObservationRow.accession,
                        ],
                        set_={
                            "fact_count": observation.excluded.fact_count,
                            "sample_fact_ids": observation.excluded.sample_fact_ids,
                            "observed_at": func.now(),
                        },
                    )
                )
                await session.execute(
                    text(
                        """
                        UPDATE mapping_reviews AS review
                        SET evidence = review.evidence || jsonb_build_object(
                            'fact_count', totals.fact_count,
                            'filing_count', totals.filing_count
                        )
                        FROM (
                            SELECT review_id, sum(fact_count) AS fact_count,
                                   count(*) AS filing_count
                            FROM mapping_review_observations
                            WHERE review_id = ANY(:ids)
                            GROUP BY review_id
                        ) AS totals
                        WHERE review.id = totals.review_id
                        """
                    ),
                    {"ids": list(found.values())},
                )
                review_ids.update(found)
        return {fact_id: review_ids[concept] for fact_id, concept in fact_concepts.items()}


def _evidence_count(evidence: dict[str, object]) -> int:
    value = evidence.get("fact_count", 1)
    return value if isinstance(value, int) and value > 0 else 1


def _evidence_samples(evidence: dict[str, object], fact_id: str) -> list[str]:
    value = evidence.get("sample_fact_ids")
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    return [fact_id]


def _is_identical_approval(
    row: ConceptMappingRow | None,
    *,
    explanation: str,
    created_by: str,
    valid_from: date | None,
    valid_to: date | None,
) -> bool:
    return (
        row is not None
        and row.status == "approved"
        and row.valid_from == valid_from
        and row.valid_to == valid_to
        and row.rationale == explanation
        and row.created_by == created_by
    )


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


# Keys per approved-mapping query; keeps bound parameters well under asyncpg's limit.
_MAPPING_KEY_CHUNK = 2000


def _most_specific(
    rows: Sequence[tuple[ConceptMappingRow, str]], namespace: str
) -> tuple[ConceptMapping, ...]:
    """The latest version of each mapping, keeping only the most specific ones."""
    if not rows:
        return ()
    latest: dict[tuple[object, ...], tuple[ConceptMappingRow, str]] = {}
    for row, metric_code in rows:
        key = (metric_code, row.namespace, row.cik, row.taxonomy_version)
        if key not in latest or latest[key][0].mapping_version < row.mapping_version:
            latest[key] = (row, metric_code)

    def specificity(row: ConceptMappingRow) -> tuple[bool, bool, bool]:
        return (
            row.cik is not None,
            row.taxonomy_version is not None,
            row.namespace == namespace,
        )

    best = max(specificity(row) for row, _ in latest.values())
    return tuple(
        _to_concept_mapping(row, metric_code)
        for row, metric_code in latest.values()
        if specificity(row) == best
    )
