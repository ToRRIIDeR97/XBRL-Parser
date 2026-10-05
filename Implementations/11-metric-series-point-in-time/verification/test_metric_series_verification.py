"""Independent verification tests for 11-metric-series-point-in-time."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

D = Decimal
CIK = 789019


def _obs(metric, start, end, value, accession, accepted, fy, fp, filing_end, *, instant=None,
         concept=None, priority=1, dimensions="[]"):
    from sec_xbrl_warehouse.normalization.series import MetricObservation

    return MetricObservation(
        cik=CIK, metric=metric, concept_qname=concept or f"us-gaap:{metric}", accession=accession,
        form="10-K" if fp == "FY" else "10-Q",
        accepted_at=datetime.fromisoformat(accepted).replace(tzinfo=UTC),
        fiscal_year=fy, fiscal_period=fp, filing_period_end=date.fromisoformat(filing_end),
        period_start=date.fromisoformat(start) if start else None,
        period_end=date.fromisoformat(end) if end else None,
        period_instant=date.fromisoformat(instant) if instant else None,
        value=D(value), unit="USD", priority=priority, dimensions_json=dimensions,
    )


def _points(observations, **kwargs):
    from sec_xbrl_warehouse.normalization.series import build_series

    return build_series(observations, **kwargs)


# AC-1: without the nine-month value, Q4 is not fabricated and no TTM is produced.
def test_ac1_missing_input_means_no_derived_point() -> None:
    observations = [
        _obs("revenue", "2025-07-01", "2025-09-30", 100, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30"),
        _obs("revenue", "2025-07-01", "2026-06-30", 480, "fy", "2026-07-30T00:00:00", 2026, "FY", "2026-06-30"),
    ]
    points = _points(observations)
    assert {(p.period_type, p.fiscal_quarter) for p in points} == {("quarter", 1), ("annual", None)}


# AC-1: 52/53-week calendars (91-day quarters, 371-day years) are recognised.
def test_ac1_fifty_three_week_year() -> None:
    observations = [
        _obs("revenue", "2022-09-25", "2022-12-31", 1172, "q1", "2023-02-03T00:00:00", 2023, "Q1", "2022-12-31"),
        _obs("revenue", "2022-09-25", "2023-09-30", 3833, "fy", "2023-11-03T00:00:00", 2023, "FY", "2023-09-30"),
    ]
    kinds = sorted(p.period_type for p in _points(observations))
    assert kinds == ["annual", "quarter"]


# AC-1: instants become instant points; TTM is never computed for them.
def test_ac1_instant_metrics_are_points_without_ttm() -> None:
    observations = [
        _obs("total_assets", None, None, 500, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30", instant="2025-09-30"),
        _obs("total_assets", None, None, 520, "q2", "2026-01-20T00:00:00", 2026, "Q2", "2025-12-31", instant="2025-12-31"),
    ]
    points = _points(observations)
    assert {p.period_type for p in points} == {"instant"}
    assert sorted((p.period_end, p.fiscal_quarter) for p in points) == [(date(2025, 9, 30), 1), (date(2025, 12, 31), 2)]


# AC-4: when two concepts map to one metric for the same period, the higher-priority concept wins.
def test_ac4_concept_priority_decides_and_is_recorded() -> None:
    observations = [
        _obs("revenue", "2025-07-01", "2025-09-30", 105, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30",
             concept="us-gaap:Revenues", priority=2),
        _obs("revenue", "2025-07-01", "2025-09-30", 100, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30",
             concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", priority=1),
    ]
    (point,) = _points(observations)
    assert point.value == D(100)
    assert point.concept_qname == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


# AC-5: dimensional observations never enter the consolidated series.
def test_ac5_dimensional_observations_are_excluded() -> None:
    observations = [
        _obs("revenue", "2025-07-01", "2025-09-30", 100, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30"),
        _obs("revenue", "2025-07-01", "2025-09-30", 40, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30",
             dimensions='[{"axis":"us-gaap:StatementBusinessSegmentsAxis","member":"x:CloudMember"}]'),
    ]
    assert [p.value for p in _points(observations)] == [D(100)]


# AC-6: formula metrics are derived per period only when every operand exists.
def test_ac6_free_cash_flow_requires_both_operands() -> None:
    observations = [
        _obs("operating_cash_flow", "2025-07-01", "2025-09-30", 420, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30"),
        _obs("capital_expenditures", "2025-07-01", "2025-09-30", 90, "q1", "2025-10-20T00:00:00", 2026, "Q1", "2025-09-30"),
        _obs("operating_cash_flow", "2025-10-01", "2025-12-31", 400, "q2", "2026-01-20T00:00:00", 2026, "Q2", "2025-12-31"),
    ]
    fcf = [(p.fiscal_quarter, p.value, p.derivation) for p in _points(observations) if p.metric == "free_cash_flow"]
    assert fcf == [(1, D(330), "formula")]


# AC-2: as-of cutoffs apply to derived points too, and availability is the latest input time.
def test_ac2_derived_points_respect_cutoff_and_availability() -> None:
    observations = [
        _obs("operating_cash_flow", "2024-09-29", "2025-06-28", 3137, "q3", "2025-08-01T06:00:00", 2025, "Q3", "2025-06-28"),
        _obs("operating_cash_flow", "2024-09-29", "2025-09-27", 4162, "fy", "2025-10-31T06:00:00", 2025, "FY", "2025-09-27"),
    ]
    before = [p for p in _points(observations, as_of=datetime(2025, 10, 1, tzinfo=UTC)) if p.fiscal_quarter == 4]
    after = [p for p in _points(observations) if p.fiscal_quarter == 4]
    assert before == []
    assert after[0].available_at == datetime(2025, 10, 31, 6, tzinfo=UTC)
    assert set(after[0].source_accessions) == {"q3", "fy"}


# AC-3: canonical facts carry the filing acceptance time too.
def test_ac3_canonical_facts_carry_acceptance_time() -> None:
    from sec_xbrl_warehouse.domain.canonical import CanonicalMetric, ConceptMapping
    from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlFact, XbrlUnit
    from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper, MappingDecision

    accepted = datetime(2026, 1, 20, 21, 5, tzinfo=UTC)
    filing = FilingRef(
        accession="0000789019-26-000010", cik=CIK, company_name="Verify", form="10-Q",
        filed_date=date(2026, 1, 20), accepted_at=accepted,
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/789019/1",
    )
    mapping = ConceptMapping(
        id=UUID(int=1), namespace="us-gaap", concept_name="Revenues", canonical_metric="revenue",
        mapping_type="exact", confidence=1.0, approved=True, explanation="test", version=1,
    )
    fact = XbrlFact(
        fact_id="a" * 64, concept_namespace="http://fasb.org/us-gaap/2025", concept_name="Revenues",
        concept_qname="us-gaap:Revenues", context_id="c", context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC, raw_value="1", value_decimal=D(1),
        period_kind=PeriodKind.DURATION, period_start=date(2025, 10, 1), period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK", entity_identifier="0000789019",
        dimension_hash="0" * 64, duplicate_set_hash="0" * 64, source_document="x.htm",
    )
    metric = CanonicalMetric(code="revenue", name="Revenue", statement="income_statement",
                             period_type="duration", unit_family="monetary")

    class _Repo:
        async def approved_mappings_for_concept(self, *args):
            return (mapping,)

    mapper = CanonicalMapper({"revenue": metric}, {}, _Repo())
    decision = MappingDecision(mapping=mapping, candidates=("revenue",), explanation="test", evidence={})
    canonical = mapper.to_canonical_fact(decision, fact, filing, extraction_revision=1)
    assert canonical.accepted_at == accepted


# AC-7: series are rebuilt into ClickHouse and served by the query service with as-of support.
@pytest.mark.integration
def test_ac7_series_rebuild_and_query(clickhouse_settings) -> None:
    from sec_xbrl_warehouse.domain.canonical import CanonicalFact
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
    from sec_xbrl_warehouse.query.series import rebuild_metric_series
    from sec_xbrl_warehouse.query.service import QueryService

    def canonical(accession, accepted, start, end, value, revision=1):
        return CanonicalFact(
            canonical_fact_id=(accession + "x" * 64)[:64], raw_fact_id=(accession + "r" * 64)[:64],
            accession=accession, cik=CIK, filed_date=accepted.date(), accepted_at=accepted, form="10-K",
            canonical_metric="revenue", normalized_value_decimal=D(value), normalized_unit="USD",
            period_kind="duration", period_start=start, period_end=end, dimensions_json="[]",
            mapping_id=UUID(int=7), mapping_version=1, mapping_confidence=1.0,
            mapping_explanation="test", extraction_revision=revision,
        )

    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        warehouse.insert_canonical_facts([
            canonical("0000789019-24-000001", datetime(2024, 7, 30, tzinfo=UTC), date(2023, 7, 1), date(2024, 6, 30), 2451),
            canonical("0000789019-25-000001", datetime(2025, 7, 30, tzinfo=UTC), date(2023, 7, 1), date(2024, 6, 30), 2460),
        ])
        rebuild_metric_series(warehouse, CIK)
        service = QueryService(warehouse)
        latest = service.metric_series(cik=CIK, metrics=["revenue"], view="latest")
        early = service.metric_series(cik=CIK, metrics=["revenue"], view="latest", as_of=datetime(2025, 1, 1, tzinfo=UTC))
    finally:
        warehouse.close()
    assert [D(str(row["value"])) for row in latest if row["period_type"] == "annual"] == [D(2460)]
    assert [D(str(row["value"])) for row in early if row["period_type"] == "annual"] == [D(2451)]
