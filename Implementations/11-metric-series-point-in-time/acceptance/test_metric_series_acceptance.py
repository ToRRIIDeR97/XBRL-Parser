"""Worker-visible acceptance tests for 11-metric-series-point-in-time."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

D = Decimal
CIK = 320193


def _obs(metric, start, end, value, accession, accepted, fy, fp, filing_end, *, instant=None, concept=None):
    from sec_xbrl_warehouse.normalization.series import MetricObservation

    return MetricObservation(
        cik=CIK,
        metric=metric,
        concept_qname=concept or f"us-gaap:{metric}",
        accession=accession,
        form="10-K" if fp == "FY" else "10-Q",
        accepted_at=datetime.fromisoformat(accepted).replace(tzinfo=UTC),
        fiscal_year=fy,
        fiscal_period=fp,
        filing_period_end=date.fromisoformat(filing_end),
        period_start=date.fromisoformat(start) if start else None,
        period_end=date.fromisoformat(end) if end else None,
        period_instant=date.fromisoformat(instant) if instant else None,
        value=D(value),
        unit="USD",
    )


def _fy2025_cash_flow():
    """Apple-like fiscal year: 10-Qs report year-to-date cash flows only."""
    return [
        _obs("operating_cash_flow", "2024-09-29", "2024-12-28", 1243, "q1", "2025-01-31T06:00:00", 2025, "Q1", "2024-12-28"),
        _obs("operating_cash_flow", "2024-09-29", "2025-03-29", 2197, "q2", "2025-05-02T06:00:00", 2025, "Q2", "2025-03-29"),
        _obs("operating_cash_flow", "2024-09-29", "2025-06-28", 3137, "q3", "2025-08-01T06:00:00", 2025, "Q3", "2025-06-28"),
        _obs("operating_cash_flow", "2024-09-29", "2025-09-27", 4162, "fy", "2025-10-31T06:00:00", 2025, "FY", "2025-09-27"),
    ]


# AC-1
def test_ac1_discrete_quarters_are_derived_from_year_to_date_values() -> None:
    from sec_xbrl_warehouse.normalization.series import build_series

    points = build_series(_fy2025_cash_flow())
    quarters = sorted(
        (point.fiscal_quarter, point.value, point.derivation)
        for point in points
        if point.period_type == "quarter"
    )
    assert quarters == [
        (1, D(1243), "reported"),
        (2, D(954), "ytd_difference"),
        (3, D(940), "ytd_difference"),
        (4, D(1025), "annual_minus_nine_months"),
    ]
    annual = [point for point in points if point.period_type == "annual"]
    assert [(p.fiscal_year, p.value) for p in annual] == [(2025, D(4162))]


# AC-1
def test_ac1_ttm_at_fiscal_year_end_equals_annual() -> None:
    from sec_xbrl_warehouse.normalization.series import build_series

    ttm = [point for point in build_series(_fy2025_cash_flow()) if point.period_type == "ttm"]
    assert [(point.period_end, point.value, point.derivation) for point in ttm] == [
        (date(2025, 9, 27), D(4162), "ttm_sum")
    ]


# AC-2
def test_ac2_point_in_time_and_restated_views() -> None:
    from sec_xbrl_warehouse.normalization.series import build_series

    original = _obs("revenue", "2023-10-01", "2024-09-28", 3910, "fy24", "2024-11-01T06:00:00", 2024, "FY", "2024-09-28")
    restated = _obs("revenue", "2023-10-01", "2024-09-28", 3950, "fy25", "2025-10-31T06:00:00", 2025, "FY", "2025-09-27")

    def annual(**kwargs):
        return [p.value for p in build_series([original, restated], **kwargs) if p.period_type == "annual"]

    assert annual(view="latest") == [D(3950)]
    assert annual(view="as_reported") == [D(3910)]
    assert annual(view="latest", as_of=datetime(2025, 1, 1, tzinfo=UTC)) == [D(3910)]
    assert annual(view="latest", as_of=datetime(2024, 10, 1, tzinfo=UTC)) == []


# AC-3
def test_ac3_fact_rows_carry_filing_acceptance_time() -> None:
    from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import facts_table

    accepted = datetime(2025, 10, 31, 6, 1, 26, tzinfo=UTC)
    filing = FilingRef(
        accession="0000320193-25-000079", cik=CIK, company_name="Apple Inc.", form="10-K",
        filed_date=date(2025, 10, 31), accepted_at=accepted,
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
    )
    fact = XbrlFact(
        fact_id="f" * 64, concept_namespace="http://fasb.org/us-gaap/2025", concept_name="Revenues",
        concept_qname="us-gaap:Revenues", context_id="c", context_hash="0" * 64,
        value_kind=FactValueKind.NUMERIC, raw_value="1", value_decimal=D(1),
        period_kind=PeriodKind.DURATION, period_start=date(2024, 9, 29), period_end=date(2025, 9, 27),
        entity_scheme="http://www.sec.gov/CIK", entity_identifier="0000320193",
        dimension_hash="0" * 64, duplicate_set_hash="0" * 64, source_document="aapl.htm",
    )
    bundle = XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="aapl.htm", document_type="inline", taxonomy_namespaces=(),
            source_documents=("aapl.htm",), software_name="arelle", software_version="2.44.4",
        ),
        contexts=(), concepts=(), facts=(fact,), relationships=(), footnotes=(), validation_issues=(),
    )
    table = facts_table(bundle)
    assert "accepted_at" in table.column_names
    assert table.column("accepted_at").to_pylist() == [accepted]
