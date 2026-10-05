"""Worker-visible acceptance tests for 06-canonical-metrics-expansion."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper

CONFIG = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "config"

REQUIRED_METRICS = {
    # income statement
    "revenue", "cost_of_revenue", "gross_profit", "research_and_development",
    "selling_general_administrative", "operating_income", "interest_expense",
    "income_tax_expense", "net_income", "basic_eps", "diluted_eps",
    # shares
    "basic_shares", "diluted_shares", "shares_outstanding",
    # cash flow
    "operating_cash_flow", "capital_expenditures", "free_cash_flow",
    "depreciation_amortization", "share_based_compensation", "share_repurchases",
    "dividends_paid", "acquisitions", "debt_issued", "debt_repaid", "income_taxes_paid",
    # balance sheet
    "cash_and_equivalents", "short_term_investments", "accounts_receivable", "inventory",
    "total_current_assets", "goodwill", "total_assets", "accounts_payable",
    "deferred_revenue", "total_current_liabilities", "current_debt", "long_term_debt",
    "operating_lease_liabilities", "total_liabilities", "stockholders_equity",
    # per share
    "dividends_per_share",
    # banks and lenders
    "net_interest_income", "deposits", "loans_net", "allowance_for_credit_losses",
    "provision_for_credit_losses",
}


class _NoRepository:
    async def approved_mappings_for_concept(self, *args, **kwargs):
        return ()

    async def create_reviews(self, filing, reviews):
        return {}


def _mapper() -> CanonicalMapper:
    return CanonicalMapper.load_rules(
        CONFIG / "canonical_metrics.yml", CONFIG / "concept_seed.yml", _NoRepository()
    )


# AC-1
def test_ac1_metric_catalogue_covers_research_needs() -> None:
    mapper = _mapper()
    missing = REQUIRED_METRICS - set(mapper.metrics)
    assert not missing, sorted(missing)


# AC-1
def test_ac1_every_reported_metric_has_a_us_gaap_candidate() -> None:
    mapper = _mapper()
    covered = {
        metric
        for (hint, _concept), metrics in mapper.candidate_rules.items()
        if hint == "us-gaap"
        for metric in metrics
    }
    reported = {code for code, metric in mapper.metrics.items() if not metric.formula}
    assert not (reported - covered), sorted(reported - covered)


# AC-3
@pytest.mark.integration
def test_ac3_family_mapping_applies_to_every_us_gaap_year(postgres_settings) -> None:
    from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository

    async def run() -> tuple[int, int, int]:
        repository = PostgresMappingRepository(postgres_settings)
        try:
            await repository.seed_metrics(tuple(_mapper().metrics.values()))
            await repository.approve_mapping(
                metric="revenue",
                namespace="us-gaap",
                concept_name="Revenues",
                explanation="Standard US-GAAP total revenues",
                created_by="acceptance-test",
            )
            found = []
            for namespace in ("http://fasb.org/us-gaap/2019", "http://fasb.org/us-gaap/2025"):
                found.append(
                    len(
                        await repository.approved_mappings_for_concept(
                            namespace, "Revenues", None, 320193, date(2026, 1, 30)
                        )
                    )
                )
            extension = await repository.approved_mappings_for_concept(
                "http://www.apple.com/20250927", "Revenues", None, 320193, date(2026, 1, 30)
            )
            return found[0], found[1], len(extension)
        finally:
            await repository.close()

    assert asyncio.run(run()) == (1, 1, 0)


# AC-4
@pytest.mark.integration
def test_ac4_bulk_seed_approval_is_explicit_and_idempotent(postgres_settings) -> None:
    from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository
    from sec_xbrl_warehouse.mapping.seed_approval import approve_seed_candidates

    async def run():
        repository = PostgresMappingRepository(postgres_settings)
        try:
            mapper = CanonicalMapper.load_rules(
                CONFIG / "canonical_metrics.yml", CONFIG / "concept_seed.yml", repository
            )
            await repository.seed_metrics(tuple(mapper.metrics.values()))
            dry = await approve_seed_candidates(
                mapper, repository, reviewer="analyst", rationale="seed review", metrics={"revenue"}, dry_run=True
            )
            first = await approve_seed_candidates(
                mapper, repository, reviewer="analyst", rationale="seed review", metrics={"revenue"}
            )
            second = await approve_seed_candidates(
                mapper, repository, reviewer="analyst", rationale="seed review", metrics={"revenue"}
            )
            live = await repository.approved_mappings_for_concept(
                "http://fasb.org/us-gaap/2024", "Revenues", None, 1, date(2025, 2, 1)
            )
            return dry, first, second, live
        finally:
            await repository.close()

    dry, first, second, live = asyncio.run(run())
    assert dry and not any(item.created for item in dry)
    assert first and all(item.created for item in first)
    assert {item.metric for item in first} == {"revenue"}
    assert second and not any(item.created for item in second)
    assert [mapping.canonical_metric for mapping in live] == ["revenue"]
