from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol
from uuid import UUID

import yaml

from sec_xbrl_warehouse.domain.canonical import CanonicalFact, CanonicalMetric, ConceptMapping
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact
from sec_xbrl_warehouse.normalization.dimensions import dimensions_to_json
from sec_xbrl_warehouse.normalization.facts import normalize_numeric_value, normalize_unit


class MappingRepository(Protocol):
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


class CanonicalMapper:
    """Map raw facts without destroying or mutating the as-filed layer."""

    def __init__(
        self,
        metrics: dict[str, CanonicalMetric],
        candidate_rules: dict[tuple[str, str], tuple[str, ...]],
        repository: MappingRepository,
    ) -> None:
        self.metrics = metrics
        self.candidate_rules = candidate_rules
        self.repository = repository

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
        candidates: dict[tuple[str, str], list[str]] = {}
        for item in seed_payload.get("mappings", []):
            code = str(item["canonical_metric"])
            if code not in metrics:
                raise ValueError(f"Seed mapping references unknown metric: {code}")
            key = (str(item.get("namespace_hint") or ""), str(item["concept"]))
            candidates.setdefault(key, []).append(code)
        return cls(
            metrics=metrics,
            candidate_rules={key: tuple(value) for key, value in candidates.items()},
            repository=repository,
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
        if len(approved) == 1:
            mapping = approved[0]
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
        )

    async def map_batch(
        self,
        facts: Iterable[XbrlFact],
        filing: FilingRef,
        *,
        extraction_revision: int,
    ) -> tuple[CanonicalFact, ...]:
        mapped: list[CanonicalFact] = []
        for fact in facts:
            decision = await self.map_fact(fact, filing)
            if decision.mapping is None:
                await self.enqueue_review(decision, fact, filing)
                continue
            mapped.append(
                self.to_canonical_fact(
                    decision,
                    fact,
                    filing,
                    extraction_revision=extraction_revision,
                )
            )
        return tuple(mapped)

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
    lowered = namespace.lower()
    for value in ("us-gaap", "ifrs-full", "dei", "srt"):
        if value in lowered:
            return value
    return "extension"
