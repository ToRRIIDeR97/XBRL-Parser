"""Standardized metric series with point-in-time views.

Pure functions that turn canonical observations (one per reported value) into
discrete quarters, fiscal years, trailing twelve months, and instants:

- Observations are selected per `(metric, period)` *before* any derivation:
  consolidated only (`dimensions_json == "[]"`), lowest `priority` within a filing,
  then the newest (`latest`) or oldest (`as_reported`) filing; `as_of` drops
  observations accepted after the cutoff first.
- Quarters are reported 3-month values, or differences of consecutive
  year-to-date values that share a `period_start`; Q4 = year - nine months.
- TTM is the sum of four contiguous discrete quarters.
- A derived point needs every input; nothing is guessed from calendars.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Literal

View = Literal["latest", "as_reported"]
PeriodType = Literal["quarter", "annual", "ttm", "instant"]
Derivation = Literal["reported", "ytd_difference", "annual_minus_nine_months", "ttm_sum", "formula"]

# Inclusive duration lengths in days (end - start), wide enough for 52/53-week years.
QUARTER_DAYS = range(80, 101)
SIX_MONTH_DAYS = range(170, 191)
NINE_MONTH_DAYS = range(260, 286)
YEAR_DAYS = range(350, 381)

# Formula metrics from config/canonical_metrics.yml; callers may pass their own.
DEFAULT_FORMULAS: Mapping[str, str] = {
    "free_cash_flow": "operating_cash_flow - capital_expenditures",
}


@dataclass(frozen=True, slots=True)
class MetricObservation:
    cik: int
    metric: str
    concept_qname: str
    accession: str
    form: str
    accepted_at: datetime | None
    fiscal_year: int | None
    fiscal_period: str | None
    filing_period_end: date | None
    period_start: date | None
    period_end: date | None
    period_instant: date | None
    value: Decimal
    unit: str | None
    priority: int = 1
    dimensions_json: str = "[]"


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    cik: int
    metric: str
    period_type: PeriodType
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_start: date | None
    period_end: date
    value: Decimal
    unit: str | None
    derivation: Derivation
    concept_qname: str | None
    source_accessions: tuple[str, ...]
    available_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Value:
    """A selected value for one period, with the inputs it came from."""

    start: date | None
    end: date
    value: Decimal
    unit: str | None
    concept: str | None
    accessions: frozenset[str]
    available_at: datetime | None
    derivation: Derivation = "reported"
    sources: tuple[str, ...] = field(default=())


def build_series(
    observations: Iterable[MetricObservation],
    *,
    view: View = "latest",
    as_of: datetime | None = None,
    formulas: Mapping[str, str] | None = None,
) -> list[SeriesPoint]:
    """Build discrete quarter, annual, TTM, and instant points for every metric."""
    if view not in ("latest", "as_reported"):
        raise ValueError(f"Unknown series view: {view}")
    cutoff = _utc(as_of) if as_of is not None else None
    usable = [
        item
        for item in observations
        if _is_consolidated(item.dimensions_json) and _known_by(item, cutoff)
    ]
    labels = _fiscal_labels(usable)
    by_metric: dict[str, list[MetricObservation]] = defaultdict(list)
    for item in usable:
        by_metric[item.metric].append(item)

    points: list[SeriesPoint] = []
    for metric, items in sorted(by_metric.items()):
        points.extend(_metric_points(metric, _select(items, view), labels))
    points.extend(_formula_points(points, formulas if formulas is not None else DEFAULT_FORMULAS))
    return sorted(
        points,
        key=lambda point: (
            point.metric,
            point.period_type,
            point.period_end,
            point.period_start or date.min,
        ),
    )


# --- selection --------------------------------------------------------------


def _select(
    items: Sequence[MetricObservation], view: View
) -> dict[tuple[date | None, date], MetricObservation]:
    """One observation per period: best priority within a filing, then by filing time."""
    best_in_filing: dict[tuple[tuple[date | None, date], str], MetricObservation] = {}
    for item in items:
        period = _period_key(item)
        if period is None:
            continue
        key = (period, item.accession)
        current = best_in_filing.get(key)
        if current is None or (item.priority, item.concept_qname) < (
            current.priority,
            current.concept_qname,
        ):
            best_in_filing[key] = item
    selected: dict[tuple[date | None, date], MetricObservation] = {}
    for (period, _), item in best_in_filing.items():
        current = selected.get(period)
        if current is None:
            selected[period] = item
            continue
        newer = (_accepted(item) or _EPOCH, item.accession) > (
            _accepted(current) or _EPOCH,
            current.accession,
        )
        if newer == (view == "latest"):
            selected[period] = item
    return selected


def _period_key(item: MetricObservation) -> tuple[date | None, date] | None:
    if item.period_instant is not None:
        return (None, item.period_instant)
    if item.period_start is not None and item.period_end is not None:
        return (item.period_start, item.period_end)
    return None


# --- derivation -------------------------------------------------------------


def _metric_points(
    metric: str,
    selected: Mapping[tuple[date | None, date], MetricObservation],
    labels: Mapping[date, tuple[int | None, str | None]],
) -> list[SeriesPoint]:
    cik = next(iter(selected.values())).cik if selected else 0
    values = {period: _from_observation(item) for period, item in selected.items()}
    points: list[SeriesPoint] = []
    durations: dict[tuple[date, date], _Value] = {}
    for (start, end), value in values.items():
        if start is None:
            points.append(_point(cik, metric, "instant", value, labels, quarter_from_label=True))
        else:
            durations[(start, end)] = value
    quarters: dict[date, _Value] = {}
    for (start, end), value in durations.items():
        days = (end - start).days
        if days in QUARTER_DAYS:
            quarters[end] = value
        elif days in YEAR_DAYS:
            points.append(_point(cik, metric, "annual", value, labels))

    # Year-to-date differences: consecutive cumulative values sharing a start date.
    by_start: dict[date, list[_Value]] = defaultdict(list)
    for (start, _), value in durations.items():
        by_start[start].append(value)
    for start, cumulative in by_start.items():
        ordered = sorted(cumulative, key=lambda value: value.end)
        for previous, current in pairwise(ordered):
            kind = _ytd_step(start, previous.end, current.end)
            if kind is None or current.end in quarters or previous.unit != current.unit:
                continue
            quarters[current.end] = _Value(
                start=previous.end + timedelta(days=1),
                end=current.end,
                value=current.value - previous.value,
                unit=current.unit,
                concept=current.concept,
                accessions=previous.accessions | current.accessions,
                available_at=_latest(previous.available_at, current.available_at),
                derivation=kind,
            )

    ordered_quarters = sorted(quarters.values(), key=lambda value: value.end)
    for quarter in ordered_quarters:
        points.append(_point(cik, metric, "quarter", quarter, labels, quarter_from_label=True))
    for index in range(3, len(ordered_quarters)):
        window = ordered_quarters[index - 3 : index + 1]
        if not _contiguous(window) or len({item.unit for item in window}) != 1:
            continue
        last = window[-1]
        ttm = _Value(
            start=window[0].start,
            end=last.end,
            value=sum((item.value for item in window), Decimal(0)),
            unit=last.unit,
            concept=last.concept,
            accessions=frozenset().union(*(item.accessions for item in window)),
            available_at=_latest(*(item.available_at for item in window)),
            derivation="ttm_sum",
        )
        points.append(_point(cik, metric, "ttm", ttm, labels, quarter_from_label=True))
    return points


def _ytd_step(start: date, previous_end: date, current_end: date) -> Derivation | None:
    """Which derivation a consecutive pair of cumulative periods allows, if any."""
    previous = (previous_end - start).days
    current = (current_end - start).days
    if previous in QUARTER_DAYS and current in SIX_MONTH_DAYS:
        return "ytd_difference"
    if previous in SIX_MONTH_DAYS and current in NINE_MONTH_DAYS:
        return "ytd_difference"
    if previous in NINE_MONTH_DAYS and current in YEAR_DAYS:
        return "annual_minus_nine_months"
    return None


def _contiguous(window: Sequence[_Value]) -> bool:
    return all(
        current.start is not None and current.start == previous.end + timedelta(days=1)
        for previous, current in pairwise(window)
    )


def _formula_points(
    points: Sequence[SeriesPoint], formulas: Mapping[str, str]
) -> list[SeriesPoint]:
    """Per-period formula values where every operand has a point with the same period."""
    index: dict[tuple[str, PeriodType, date | None, date], SeriesPoint] = {
        (point.metric, point.period_type, point.period_start, point.period_end): point
        for point in points
    }
    periods = {(point.period_type, point.period_start, point.period_end) for point in points}
    derived: list[SeriesPoint] = []
    for metric, expression in sorted(formulas.items()):
        tokens = _tokenize(expression)
        operands = sorted({token for token in tokens if token.isidentifier()})
        for period_type, start, end in sorted(
            periods, key=lambda item: (item[0], item[2], item[1] or date.min)
        ):
            inputs = [index.get((operand, period_type, start, end)) for operand in operands]
            if any(item is None for item in inputs):
                continue
            present = [item for item in inputs if item is not None]
            if len({item.unit for item in present}) != 1:
                continue
            value = _evaluate(tokens, {item.metric: item.value for item in present})
            if value is None:
                continue
            anchor = present[0]
            derived.append(
                SeriesPoint(
                    cik=anchor.cik,
                    metric=metric,
                    period_type=period_type,
                    fiscal_year=anchor.fiscal_year,
                    fiscal_quarter=anchor.fiscal_quarter,
                    period_start=start,
                    period_end=end,
                    value=value,
                    unit=anchor.unit,
                    derivation="formula",
                    concept_qname=None,
                    source_accessions=tuple(
                        sorted(
                            {accession for item in present for accession in item.source_accessions}
                        )
                    ),
                    available_at=_latest(*(item.available_at for item in present)),
                )
            )
    return derived


# --- helpers ----------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TOKEN = re.compile(r"\s*(?:([A-Za-z_][A-Za-z0-9_]*)|(\d+(?:\.\d+)?)|(.))")


def _from_observation(item: MetricObservation) -> _Value:
    period = _period_key(item)
    assert period is not None
    return _Value(
        start=period[0],
        end=period[1],
        value=item.value,
        unit=item.unit,
        concept=item.concept_qname,
        accessions=frozenset({item.accession}),
        available_at=_accepted(item),
    )


def _point(
    cik: int,
    metric: str,
    period_type: PeriodType,
    value: _Value,
    labels: Mapping[date, tuple[int | None, str | None]],
    *,
    quarter_from_label: bool = False,
) -> SeriesPoint:
    fiscal_year, fiscal_period = labels.get(value.end, (None, None))
    return SeriesPoint(
        cik=cik,
        metric=metric,
        period_type=period_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=_quarter(fiscal_period) if quarter_from_label else None,
        period_start=value.start,
        period_end=value.end,
        value=value.value,
        unit=value.unit,
        derivation=value.derivation,
        concept_qname=value.concept,
        source_accessions=tuple(sorted(value.accessions)),
        available_at=value.available_at,
    )


def _fiscal_labels(items: Iterable[MetricObservation]) -> dict[date, tuple[int | None, str | None]]:
    """Fiscal year/period of the filing whose period end is each date (latest filing wins)."""
    labels: dict[date, tuple[tuple[datetime, str], int | None, str | None]] = {}
    for item in items:
        if item.filing_period_end is None or (
            item.fiscal_year is None and item.fiscal_period is None
        ):
            continue
        rank = (_accepted(item) or _EPOCH, item.accession)
        current = labels.get(item.filing_period_end)
        if current is None or rank > current[0]:
            labels[item.filing_period_end] = (rank, item.fiscal_year, item.fiscal_period)
    return {end: (year, period) for end, (_, year, period) in labels.items()}


def _quarter(fiscal_period: str | None) -> int | None:
    if fiscal_period is None:
        return None
    period = fiscal_period.strip().upper()
    if period in {"Q1", "Q2", "Q3", "Q4"}:
        return int(period[1])
    if period == "FY":
        return 4
    return None


def _known_by(item: MetricObservation, cutoff: datetime | None) -> bool:
    """With a cutoff, only observations accepted at or before it (unknown times are dropped)."""
    if cutoff is None:
        return True
    accepted = _accepted(item)
    return accepted is not None and accepted <= cutoff


def _accepted(item: MetricObservation) -> datetime | None:
    return _utc(item.accepted_at) if item.accepted_at is not None else None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _is_consolidated(dimensions_json: str | None) -> bool:
    return (dimensions_json or "[]").strip() in {"[]", ""}


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    for name, number, symbol in _TOKEN.findall(expression):
        token = name or number or symbol
        if token.strip():
            if symbol and symbol not in "+-*/()":
                raise ValueError(f"Unsupported formula token {symbol!r} in {expression!r}")
            tokens.append(token)
    return tokens


def _evaluate(tokens: Sequence[str], values: Mapping[str, Decimal]) -> Decimal | None:
    """Evaluate `+ - * / ( )` over metric values with standard precedence."""
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def factor() -> Decimal | None:
        token = take()
        if token == "(":
            result = expression()
            take()  # ")"
            return result
        if token == "-":
            operand = factor()
            return None if operand is None else -operand
        if token.isidentifier():
            return values.get(token)
        return Decimal(token)

    def term() -> Decimal | None:
        result = factor()
        while peek() in ("*", "/"):
            operator = take()
            right = factor()
            if result is None or right is None:
                return None
            if operator == "/" and right == 0:
                return None
            result = result * right if operator == "*" else result / right
        return result

    def expression() -> Decimal | None:
        result = term()
        while peek() in ("+", "-"):
            operator = take()
            right = term()
            if result is None or right is None:
                return None
            result = result + right if operator == "+" else result - right
        return result

    return expression()
