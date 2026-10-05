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


def _apple_shares_and_eps():
    s = "2024-09-29"
    rows = []
    for metric, values in (
        ("diluted_shares", (15_150_865, 15_106_000, 15_056_133, 15_004_697)),
        ("diluted_eps", ("2.40", "4.05", "5.62", "6.08")),
        ("revenue", (124_300, 219_659, 313_695, 416_161)),
    ):
        for (end, accession, accepted, fp), value in zip(
            (
                ("2024-12-28", "q1", "2025-01-31T06:00:00", "Q1"),
                ("2025-03-29", "q2", "2025-05-02T06:00:00", "Q2"),
                ("2025-06-28", "q3", "2025-08-01T06:00:00", "Q3"),
                ("2025-09-27", "fy", "2025-10-31T06:00:00", "FY"),
            ),
            values,
            strict=True,
        ):
            rows.append(_obs(metric, s, end, value, accession, accepted, 2025, fp, end))
    return rows


def test_non_additive_metrics_are_never_differenced_or_summed() -> None:
    points = build_series(_apple_shares_and_eps())
    for metric in ("diluted_shares", "diluted_eps"):
        mine = [p for p in points if p.metric == metric]
        assert {p.derivation for p in mine} == {"reported"}
        assert sorted((p.period_type, p.fiscal_quarter) for p in mine) == [
            ("annual", None),
            ("quarter", 1),
        ]
        assert all(p.value > 0 for p in mine)
    revenue = [p for p in points if p.metric == "revenue"]
    assert {p.derivation for p in revenue} >= {
        "ytd_difference",
        "annual_minus_nine_months",
        "ttm_sum",
    }


def test_catalogue_marks_shares_and_eps_non_additive() -> None:
    from pathlib import Path

    from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
    from sec_xbrl_warehouse.normalization.series import DEFAULT_NON_ADDITIVE
    from sec_xbrl_warehouse.query.series import non_additive_from_metrics

    config = Path(__file__).resolve().parents[2] / "config"
    mapper = CanonicalMapper.load_rules(
        config / "canonical_metrics.yml",
        config / "concept_seed.yml",
        None,  # type: ignore[arg-type]
    )
    flagged = non_additive_from_metrics(mapper.metrics)
    assert flagged == DEFAULT_NON_ADDITIVE
    assert "dividends_per_share" not in flagged
    # An explicit empty set restores additive treatment (callers own the catalogue).
    points = build_series(_apple_shares_and_eps(), non_additive=())
    assert any(p.metric == "diluted_shares" and p.derivation == "ttm_sum" for p in points)


def _discrete_year(**units):
    """Apple-like FY2025: three reported 3-month quarters and the fiscal year."""
    unit = units.get("q2_unit", "USD")
    return [
        _obs("revenue", "2024-09-29", "2024-12-28", 124300, "q1", "2025-01-31T00:00:00"),
        _obs("revenue", "2024-12-29", "2025-03-29", 95359, "q2", "2025-05-02T00:00:00", unit=unit),
        _obs("revenue", "2025-03-30", "2025-06-28", 94036, "q3", "2025-08-01T00:00:00"),
        _obs("revenue", "2024-09-29", "2025-09-27", 416161, "fy", "2025-10-31T00:00:00"),
    ]


def test_q4_falls_back_to_annual_minus_three_contiguous_quarters() -> None:
    points = build_series(_discrete_year())
    q4 = next(p for p in points if p.period_type == "quarter" and p.period_end == date(2025, 9, 27))
    assert (q4.period_start, q4.value, q4.derivation) == (
        date(2025, 6, 29),
        D(102466),
        "annual_minus_quarters",
    )
    assert q4.source_accessions == ("fy", "q1", "q2", "q3")
    assert q4.available_at == datetime(2025, 10, 31, tzinfo=UTC)
    (ttm,) = [p for p in points if p.period_type == "ttm"]
    assert ttm.value == D(416161)


def test_nine_month_value_takes_precedence_over_the_quarter_fallback() -> None:
    observations = [
        *_discrete_year(),
        _obs("revenue", "2024-09-29", "2025-06-28", 313000, "q3", "2025-08-01T00:00:00"),
    ]
    q4 = next(
        p
        for p in build_series(observations)
        if p.period_type == "quarter" and p.period_end == date(2025, 9, 27)
    )
    assert (q4.value, q4.derivation) == (D(103161), "annual_minus_nine_months")


def test_quarter_fallback_needs_contiguous_quarters_one_unit_and_additivity() -> None:
    def q4s(observations, **kwargs):
        return [
            p
            for p in build_series(observations, **kwargs)
            if p.period_type == "quarter" and p.period_end == date(2025, 9, 27)
        ]

    gap = [item for item in _discrete_year() if item.accession != "q2"]
    assert q4s(gap) == []
    assert q4s(_discrete_year(q2_unit="EUR")) == []
    assert q4s(_discrete_year(), non_additive={"revenue"}) == []
