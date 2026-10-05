"""Approve reviewed seed candidates as family mappings, with reviewer and rationale."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from sec_xbrl_warehouse.domain.canonical import ConceptMapping
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper


@dataclass(frozen=True)
class SeedApproval:
    metric: str
    namespace: str
    concept_name: str
    created: bool
    # True when an identical approval (same reviewer and rationale) already existed.
    already_approved: bool = False
    # Dry runs: True when the real run would create a new mapping version.
    would_create: bool = False


class SeedApprovalRepository(Protocol):
    async def approve_mapping_once(
        self,
        *,
        metric: str,
        namespace: str,
        concept_name: str,
        explanation: str,
        created_by: str,
    ) -> tuple[ConceptMapping, bool]: ...

    async def identical_approval_exists(
        self,
        *,
        metric: str,
        namespace: str,
        concept_name: str,
        explanation: str,
        created_by: str,
    ) -> bool: ...


async def approve_seed_candidates(
    mapper: CanonicalMapper,
    repository: SeedApprovalRepository,
    *,
    reviewer: str,
    rationale: str,
    metrics: Collection[str] | None = None,
    dry_run: bool = False,
) -> list[SeedApproval]:
    """Approve each selected seed candidate as a family mapping (`namespace="us-gaap"`).

    Seeds stay candidates until a person runs this with a reviewer and a
    rationale; both are recorded on every mapping. Already-approved mappings
    aren't versioned again (`created=False`). `dry_run` writes nothing and sets
    `would_create` on each item a real run with the same reviewer and rationale
    would create. `metrics` limits the run to those codes.
    """
    if not reviewer.strip() or not rationale.strip():
        raise ValueError("A reviewer and a rationale are required to approve seeds")
    if metrics is not None:
        unknown = set(metrics) - set(mapper.metrics)
        if unknown:
            raise ValueError(f"Unknown canonical metrics: {sorted(unknown)}")
    selected = sorted(
        (
            (metric, family, concept)
            for (family, concept), codes in mapper.candidate_rules.items()
            for metric in codes
            if metrics is None or metric in metrics
        ),
        key=lambda item: (
            item[0],
            item[1],
            mapper.candidate_priorities.get((item[1], item[2]), 999),
            item[2],
        ),
    )
    results: list[SeedApproval] = []
    for metric, family, concept in selected:
        if dry_run:
            exists = await repository.identical_approval_exists(
                metric=metric,
                namespace=family,
                concept_name=concept,
                explanation=rationale,
                created_by=reviewer,
            )
            results.append(
                SeedApproval(
                    metric,
                    family,
                    concept,
                    created=False,
                    already_approved=exists,
                    would_create=not exists,
                )
            )
            continue
        _, created = await repository.approve_mapping_once(
            metric=metric,
            namespace=family,
            concept_name=concept,
            explanation=rationale,
            created_by=reviewer,
        )
        results.append(
            SeedApproval(metric, family, concept, created=created, already_approved=not created)
        )
    return results
