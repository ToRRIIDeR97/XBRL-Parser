from datetime import UTC, date, datetime
from decimal import Decimal

from sec_xbrl_warehouse.normalization.series import MetricObservation, build_series
from sec_xbrl_warehouse.query.series import series_versions

D = Decimal


def _obs(
    metric, start, end, value, accession, accepted, fy=None, fp=None, filing_end=None, **extra
):
    return MetricObservation(
        cik=1,
        metric=metric,
        concept_qname=extra.pop("concept", f"us-gaap:{metric}"),
        accession=accession,
        form="10-Q",
        accepted_at=datetime.fromisoformat(accepted).replace(tzinfo=UTC),
        fiscal_year=fy,
        fiscal_period=fp,
        filing_period_end=date.fromisoformat(filing_end) if filing_end else None,
        period_start=date.fromisoformat(start) if start else None,
        period_end=date.fromisoformat(end) if end else None,
        period_instant=None,
        value=D(value),
        unit=extra.pop("unit", "USD"),
        **extra,
    )


def _quarters(points):
    return [(p.period_end, p.value, p.derivation) for p in points if p.period_type == "quarter"]


def test_reported_quarter_wins_over_ytd_difference_for_the_same_period() -> None:
    observations = [
        _obs("revenue", "2025-01-01", "2025-03-31", 10, "q1", "2025-04-20T00:00:00"),
        _obs("revenue", "2025-04-01", "2025-06-30", 12, "q2", "2025-07-20T00:00:00"),
        _obs("revenue", "2025-01-01", "2025-06-30", 23, "q2", "2025-07-20T00:00:00"),
    ]
    assert _quarters(build_series(observations)) == [
        (date(2025, 3, 31), D(10), "reported"),
        (date(2025, 6, 30), D(12), "reported"),
    ]


def test_ttm_needs_four_contiguous_quarters() -> None:
    gap = [
        _obs("revenue", "2024-01-01", "2024-03-31", 1, "a", "2024-04-20T00:00:00"),
        _obs("revenue", "2024-04-01", "2024-06-30", 2, "b", "2024-07-20T00:00:00"),
        _obs("revenue", "2024-07-01", "2024-09-30", 3, "c", "2024-10-20T00:00:00"),
        _obs("revenue", "2025-01-01", "2025-03-31", 4, "d", "2025-04-20T00:00:00"),
    ]
    assert [p for p in build_series(gap) if p.period_type == "ttm"] == []
    contiguous = [
        *gap[:3],
        _obs("revenue", "2024-10-01", "2024-12-31", 5, "e", "2025-02-01T00:00:00"),
    ]
    (ttm,) = [p for p in build_series(contiguous) if p.period_type == "ttm"]
    assert (ttm.period_start, ttm.period_end, ttm.value) == (
        date(2024, 1, 1),
        date(2024, 12, 31),
        D(11),
    )
    assert ttm.source_accessions == ("a", "b", "c", "e")
    assert ttm.available_at == datetime(2025, 2, 1, tzinfo=UTC)


def test_mixed_units_are_never_combined() -> None:
    observations = [
        _obs("revenue", "2025-01-01", "2025-03-31", 10, "q1", "2025-04-20T00:00:00"),
        _obs("revenue", "2025-01-01", "2025-06-30", 30, "q2", "2025-07-20T00:00:00", unit="EUR"),
    ]
    assert _quarters(build_series(observations)) == [(date(2025, 3, 31), D(10), "reported")]


def test_as_reported_derives_from_first_reported_values() -> None:
    observations = [
        _obs("revenue", "2025-01-01", "2025-03-31", 10, "q1", "2025-04-20T00:00:00"),
        _obs("revenue", "2025-01-01", "2025-03-31", 11, "q2", "2025-07-20T00:00:00"),
        _obs("revenue", "2025-01-01", "2025-06-30", 25, "q2", "2025-07-20T00:00:00"),
    ]
    latest = dict((end, value) for end, value, _ in _quarters(build_series(observations)))
    first = dict(
        (end, value) for end, value, _ in _quarters(build_series(observations, view="as_reported"))
    )
    assert latest[date(2025, 6, 30)] == D(14) and first[date(2025, 6, 30)] == D(15)


def test_formulas_apply_to_ttm_and_respect_custom_definitions() -> None:
    observations = [
        _obs("a", "2025-01-01", "2025-03-31", 10, "q1", "2025-04-20T00:00:00"),
        _obs("b", "2025-01-01", "2025-03-31", 4, "q1", "2025-04-20T00:00:00"),
    ]
    points = build_series(observations, formulas={"ratio": "(a - b) / b * 2"})
    (ratio,) = [p for p in points if p.metric == "ratio"]
    assert ratio.value == D(3) and ratio.derivation == "formula"
    assert ratio.source_accessions == ("q1",)


def test_versions_record_restatements_with_their_known_from_time() -> None:
    original = _obs("revenue", "2024-01-01", "2024-12-31", 100, "fy24", "2025-02-01T00:00:00")
    restated = _obs("revenue", "2024-01-01", "2024-12-31", 105, "fy25", "2026-02-01T00:00:00")
    rows = [
        (view, known_from, point.value)
        for view, known_from, point in series_versions([original, restated])
        if point.period_type == "annual"
    ]
    assert rows == [
        ("as_reported", datetime(2025, 2, 1, tzinfo=UTC), D(100)),
        ("latest", datetime(2025, 2, 1, tzinfo=UTC), D(100)),
        ("latest", datetime(2026, 2, 1, tzinfo=UTC), D(105)),
    ]
