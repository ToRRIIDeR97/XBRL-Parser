from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol
from uuid import UUID

import yaml

from sec_xbrl_warehouse.domain.canonical import CanonicalFact, CanonicalMetric, ConceptMapping
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact
from sec_xbrl_warehouse.mapping.namespaces import FAMILY_TOKENS, namespace_family
from sec_xbrl_warehouse.normalization.dimensions import dimensions_to_json
from sec_xbrl_warehouse.normalization.facts import (
    MONETARY_MEASURES,
    normalize_numeric_value,
    normalize_unit,
)

ReviewRequest = tuple[XbrlFact, Sequence[str], dict[str, object]]


class MappingRepository(Protocol):
    async def create_reviews(
        self, filing: FilingRef, reviews: Sequence[ReviewRequest]
    ) -> dict[str, UUID]: ...

    async def approved_mappings_for_concept(
        self,
        namespace: str,
        concept_name: str,
        taxonomy_version: str | None,
        cik: int,
        filed_date: date,
    ) -> Sequence[ConceptMapping]: ...

    async def create_review(
        self,
        *,
        filing: FilingRef,
        fact: XbrlFact,
        candidates: Sequence[str],
        evidence: dict[str, object],
    ) -> UUID: ...


@dataclass(frozen=True, slots=True)
class MappingDecision:
    mapping: ConceptMapping | None
    candidates: tuple[str, ...]
    explanation: str
    evidence: dict[str, object]


_MAX_REVIEW_SAMPLES = 5


class _ConceptReview:
    """Unmapped facts of one concept in one filing."""

    def __init__(self, fact: XbrlFact, decision: MappingDecision) -> None:
        self.fact = fact
        self.decision = decision
        self.fact_count = 1
        self.sample_fact_ids = [fact.fact_id]

    def add(self, fact: XbrlFact) -> None:
        self.fact_count += 1
        if len(self.sample_fact_ids) < _MAX_REVIEW_SAMPLES:
            self.sample_fact_ids.append(fact.fact_id)


class CanonicalMapper:
    """Map raw facts without destroying or mutating the as-filed layer."""

    def __init__(
        self,
        metrics: dict[str, CanonicalMetric],
        candidate_rules: dict[tuple[str, str], tuple[str, ...]],
        repository: MappingRepository,
        candidate_priorities: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self.metrics = metrics
        self.candidate_rules = candidate_rules
        self.repository = repository
        # (namespace family, concept) -> priority within its metric and family (1 first).
        self.candidate_priorities = candidate_priorities or {}

    @classmethod
    def load_rules(
        cls,
        metrics_path: Path,
        seed_path: Path,
        repository: MappingRepository,
    ) -> CanonicalMapper:
        """Load metric definitions and unapproved seed candidates from YAML."""
        metrics_payload = yaml.safe_load(metrics_path.read_text(encoding="utf-8")) or {}
        seed_payload = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
        metrics: dict[str, CanonicalMetric] = {}
        for item in metrics_payload.get("metrics", []):
            metric = CanonicalMetric.model_validate(item)
            if metric.code in metrics:
                raise ValueError(f"Duplicate canonical metric code: {metric.code}")
            metrics[metric.code] = metric
        for metric in metrics.values():
            if metric.formula and metric.statement != "derived":
                raise ValueError(f"Formula metric must use statement 'derived': {metric.code}")
        candidates: dict[tuple[str, str], list[str]] = {}
        priorities: dict[tuple[str, str], int] = {}
        for item in seed_payload.get("mappings", []):
            code = str(item["canonical_metric"])
            if code not in metrics:
                raise ValueError(f"Seed mapping references unknown metric: {code}")
            if metrics[code].formula:
                raise ValueError(f"Formula metric cannot have concept candidates: {code}")
            key = (str(item.get("namespace_hint") or ""), str(item["concept"]))
            if key[0] not in FAMILY_TOKENS:
                raise ValueError(f"Seed namespace hint must be one of {sorted(FAMILY_TOKENS)}")
            candidates.setdefault(key, []).append(code)
            if "priority" in item:
                priorities[key] = int(item["priority"])
        return cls(
            metrics=metrics,
            candidate_rules={key: tuple(value) for key, value in candidates.items()},
            repository=repository,
            candidate_priorities=priorities,
        )

    async def map_fact(self, fact: XbrlFact, filing: FilingRef) -> MappingDecision:
        """Apply mapping precedence and return a fully explainable decision.

        1. Query approved concept+taxonomy+CIK mappings via the repository.
        2. Resolve multiple matches by explicit specificity/version rules; never
           choose arbitrarily.
        3. Seed mappings remain candidates until reviewed.
        4. Return no mapping and enqueue review when ambiguous/unmapped.
        """
        approved = await self.repository.approved_mappings_for_concept(
            fact.concept_namespace,
            fact.concept_name,
            fact.taxonomy_version,
            filing.cik,
            filing.filed_date,
        )
        return self._decide(fact, approved)

    def _decide(self, fact: XbrlFact, approved: Sequence[ConceptMapping]) -> MappingDecision:
        if len(approved) == 1:
            mapping = approved[0]
            metric = self.metrics[mapping.canonical_metric]
            incompatibility = _incompatibility(metric, fact)
            if incompatibility:
                return MappingDecision(
                    mapping=None,
                    candidates=(mapping.canonical_metric,),
                    explanation=f"Approved concept mapping is incompatible: {incompatibility}.",
                    evidence={
                        "mapping_id": str(mapping.id),
                        "incompatibility": incompatibility,
                    },
                )
            return MappingDecision(
                mapping=mapping,
                candidates=(mapping.canonical_metric,),
                explanation=mapping.explanation,
                evidence={"precedence": "approved_repository_mapping"},
            )
        if len(approved) > 1:
            return MappingDecision(
                mapping=None,
                candidates=tuple(sorted({item.canonical_metric for item in approved})),
                explanation="Multiple approved mappings matched; manual conflict resolution required.",
                evidence={"mapping_ids": [str(item.id) for item in approved]},
            )
        hint = _namespace_hint(fact.concept_namespace)
        candidates = self.candidate_rules.get((hint, fact.concept_name), ())
        return MappingDecision(
            mapping=None,
            candidates=candidates,
            explanation=(
                "Candidate seed rule matched but is not approved."
                if candidates
                else "No approved or candidate mapping matched."
            ),
            evidence={
                "namespace": fact.concept_namespace,
                "concept": fact.concept_name,
                "taxonomy_version": fact.taxonomy_version,
                "period_kind": fact.period_kind.value,
                "unit": normalize_unit(fact),
                "dimension_hash": fact.dimension_hash,
            },
        )

    def to_canonical_fact(
        self,
        decision: MappingDecision,
        fact: XbrlFact,
        filing: FilingRef,
        *,
        extraction_revision: int,
        sign_multiplier: int = 1,
    ) -> CanonicalFact:
        if decision.mapping is None:
            raise ValueError("Cannot create CanonicalFact without an approved mapping")
        mapping = decision.mapping
        payload = {
            "raw_fact_id": fact.fact_id,
            "mapping_id": str(mapping.id),
            "mapping_version": mapping.version,
            "extraction_revision": extraction_revision,
        }
        canonical_fact_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return CanonicalFact(
            canonical_fact_id=canonical_fact_id,
            raw_fact_id=fact.fact_id,
            accession=filing.accession,
            cik=filing.cik,
            filed_date=filing.filed_date,
            form=filing.form,
            canonical_metric=mapping.canonical_metric,
            normalized_value_decimal=normalize_numeric_value(fact, sign_multiplier=sign_multiplier),
            normalized_value_text=fact.value_text,
            normalized_unit=normalize_unit(fact),
            sign_multiplier=sign_multiplier,
            period_kind=fact.period_kind.value,
            period_start=fact.period_start,
            period_end=fact.period_end,
            period_instant=fact.period_instant,
            dimensions_json=dimensions_to_json(fact.dimensions),
            mapping_id=mapping.id,
            mapping_version=mapping.version,
            mapping_confidence=mapping.confidence,
            mapping_explanation=decision.explanation,
            extraction_revision=extraction_revision,
            accepted_at=filing.accepted_at,
            concept_namespace=fact.concept_namespace,
            concept_qname=fact.concept_qname,
            fiscal_year=filing.fiscal_year,
            fiscal_period=filing.fiscal_period,
            report_date=filing.report_date,
        )

    async def map_batch(
        self,
        facts: Iterable[XbrlFact],
        filing: FilingRef,
        *,
        extraction_revision: int,
    ) -> tuple[CanonicalFact, ...]:
        return tuple(
            [
                fact
                async for batch in self.map_batches(
                    facts, filing, extraction_revision=extraction_revision
                )
                for fact in batch
            ]
        )

    async def map_batches(
        self,
        facts: Iterable[XbrlFact],
        filing: FilingRef,
        *,
        extraction_revision: int,
        batch_rows: int = 500,
        batch_bytes: int = 32 * 1024**2,
    ) -> AsyncIterator[tuple[CanonicalFact, ...]]:
        if batch_rows < 1:
            raise ValueError("Mapping batch size must be positive")
        approved: dict[tuple[str, str, str | None], Sequence[ConceptMapping]] = {}
        mapped: list[CanonicalFact] = []
        # One review request per concept per filing: the first unmapped fact's
        # decision plus a count and sample fact IDs, sent after the last batch.
        reviews: dict[tuple[str, str], _ConceptReview] = {}
        count = size = 0
        for fact in facts:
            record_size = len(fact.model_dump_json().encode("utf-8")) + 512
            if record_size > batch_bytes:
                raise ValueError("Single mapping record exceeds byte budget")
            if count and size + record_size > batch_bytes:
                yield tuple(mapped)
                mapped, count, size = [], 0, 0
            size += record_size
            key = (fact.concept_namespace, fact.concept_name, fact.taxonomy_version)
            if key not in approved:
                approved[key] = await self.repository.approved_mappings_for_concept(
                    *key, filing.cik, filing.filed_date
                )
            decision = self._decide(fact, approved[key])
            if decision.mapping is None:
                concept = (fact.concept_namespace, fact.concept_name)
                review = reviews.get(concept)
                if review is None:
                    reviews[concept] = _ConceptReview(fact, decision)
                else:
                    review.add(fact)
            else:
                mapped.append(
                    self.to_canonical_fact(
                        decision, fact, filing, extraction_revision=extraction_revision
                    )
                )
            count += 1
            if count == batch_rows:
                yield tuple(mapped)
                mapped, count, size = [], 0, 0
        if count:
            yield tuple(mapped)
        if reviews:
            requests: list[ReviewRequest] = [
                (
                    review.fact,
                    review.decision.candidates,
                    {
                        **self.explain_mapping(review.decision),
                        "fact_count": review.fact_count,
                        "sample_fact_ids": review.sample_fact_ids,
                    },
                )
                for review in reviews.values()
            ]
            await self.repository.create_reviews(filing, requests)

    def explain_mapping(self, decision: MappingDecision) -> dict[str, object]:
        return {
            "mapped": decision.mapping is not None,
            "mapping_id": str(decision.mapping.id) if decision.mapping else None,
            "metric": decision.mapping.canonical_metric if decision.mapping else None,
            "candidates": list(decision.candidates),
            "explanation": decision.explanation,
            "evidence": decision.evidence,
        }

    async def enqueue_review(
        self,
        decision: MappingDecision,
        fact: XbrlFact,
        filing: FilingRef,
    ) -> UUID:
        return await self.repository.create_review(
            filing=filing,
            fact=fact,
            candidates=decision.candidates,
            evidence=self.explain_mapping(decision),
        )


def _namespace_hint(namespace: str) -> str:
    return namespace_family(namespace) or "extension"


def _incompatibility(metric: CanonicalMetric, fact: XbrlFact) -> str | None:
    if metric.period_type != "derived" and fact.period_kind.value != metric.period_type:
        return f"expected {metric.period_type} period, got {fact.period_kind.value}"
    unit = normalize_unit(fact) or ""
    if metric.unit_family == "monetary" and (
        fact.unit is None
        or len(fact.unit.numerator_measures) != 1
        or fact.unit.denominator_measures
        or not (
            fact.unit.numerator_measures[0] in MONETARY_MEASURES
            or "iso4217" in fact.unit.numerator_measures[0].lower()
        )
    ):
        return f"expected one monetary measure, got {unit or 'no unit'}"
    if metric.unit_family == "shares" and "share" not in unit.lower():
        return f"expected shares, got {unit or 'no unit'}"
    if metric.unit_family == "monetary_per_share" and (
        "/" not in unit or "share" not in unit.lower()
    ):
        return f"expected monetary per share, got {unit or 'no unit'}"
    return None
