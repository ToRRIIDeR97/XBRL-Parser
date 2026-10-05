import asyncio
from pathlib import Path

import pytest

from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
from sec_xbrl_warehouse.mapping.seed_approval import approve_seed_candidates

CONFIG = Path(__file__).resolve().parents[2] / "config"


class _Repository:
    """Approvals keyed by (metric, namespace, concept, rationale, reviewer)."""

    def __init__(self) -> None:
        self.approved: set[tuple[str, ...]] = set()

    async def approved_mappings_for_concept(self, *args, **kwargs):
        return ()

    async def create_reviews(self, filing, reviews):
        return {}

    async def identical_approval_exists(
        self, *, metric, namespace, concept_name, explanation, created_by
    ) -> bool:
        return (metric, namespace, concept_name, explanation, created_by) in self.approved

    async def approve_mapping_once(
        self, *, metric, namespace, concept_name, explanation, created_by
    ):
        key = (metric, namespace, concept_name, explanation, created_by)
        created = key not in self.approved
        self.approved.add(key)
        return None, created


def _mapper(repository: _Repository) -> CanonicalMapper:
    return CanonicalMapper.load_rules(
        CONFIG / "canonical_metrics.yml", CONFIG / "concept_seed.yml", repository
    )


def test_dry_run_reports_what_a_real_run_would_create() -> None:
    repository = _Repository()
    mapper = _mapper(repository)

    async def run(**options):
        return await approve_seed_candidates(
            mapper, repository, reviewer="analyst", rationale="seed review", **options
        )

    asyncio.run(run(metrics={"revenue"}))  # approve revenue only
    dry = asyncio.run(run(metrics={"revenue", "net_income"}, dry_run=True))
    assert not any(item.created for item in dry)
    assert {item.metric for item in dry if item.would_create} == {"net_income"}
    assert all(item.already_approved for item in dry if item.metric == "revenue")
    real = asyncio.run(run(metrics={"revenue", "net_income"}))
    assert [item.created for item in real] == [item.would_create for item in dry]


def test_seed_approval_requires_reviewer_and_known_metrics() -> None:
    repository = _Repository()
    mapper = _mapper(repository)
    with pytest.raises(ValueError, match="reviewer"):
        asyncio.run(approve_seed_candidates(mapper, repository, reviewer=" ", rationale="x"))
    with pytest.raises(ValueError, match="Unknown"):
        asyncio.run(
            approve_seed_candidates(
                mapper, repository, reviewer="a", rationale="b", metrics={"nope"}
            )
        )


def test_seed_candidates_exist_in_current_us_gaap_names() -> None:
    # Regression: a candidate named a concept that no US-GAAP taxonomy defines.
    seeds = (CONFIG / "concept_seed.yml").read_text()
    assert "FinancingReceivableCreditLossExpenseReversal\n" not in seeds
    assert "FinancingReceivableExcludingAccruedInterestCreditLossExpenseReversal" in seeds
