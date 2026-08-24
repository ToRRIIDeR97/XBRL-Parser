from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sec_xbrl_warehouse.domain.enums import PeriodKind


@dataclass(frozen=True, slots=True)
class NormalizedPeriod:
    kind: PeriodKind
    start: date | None
    end: date | None
    instant: date | None
    duration_days: int | None
    quarter_count_hint: int | None
    classification: str


def normalize_period(
    *,
    kind: PeriodKind,
    start: date | None = None,
    end: date | None = None,
    instant: date | None = None,
) -> NormalizedPeriod:
    """Validate a raw XBRL period and add non-destructive query hints.

    Calendar-day classifications are only hints. Fiscal calendars, 52/53-week
    years, transition reports, and stub periods make exact labels filing-specific.
    The source dates remain authoritative.
    """
    if kind is PeriodKind.INSTANT:
        if instant is None or start is not None or end is not None:
            raise ValueError("Instant period requires only `instant`")
        return NormalizedPeriod(kind, None, None, instant, None, 0, "instant")
    if kind is PeriodKind.FOREVER:
        if any(value is not None for value in (start, end, instant)):
            raise ValueError("Forever period cannot contain dates")
        return NormalizedPeriod(kind, None, None, None, None, None, "forever")
    if start is None or end is None or instant is not None:
        raise ValueError("Duration period requires start/end and no instant")
    if start > end:
        raise ValueError("Duration start cannot be after end")
    days = (end - start).days + 1
    quarter_hint = infer_quarter_count(days) or 0
    classification = {
        1: "quarter_or_stub",
        2: "half_year_or_two_quarters",
        3: "year_to_date_three_quarters",
        4: "annual",
    }.get(quarter_hint, "nonstandard_duration")
    return NormalizedPeriod(kind, start, end, None, days, quarter_hint, classification)


def infer_quarter_count(duration_days: int) -> int | None:
    """Return a loose quarter-count hint using SEC-style duration bands."""
    if 70 <= duration_days <= 110:
        return 1
    if 150 <= duration_days <= 210:
        return 2
    if 240 <= duration_days <= 300:
        return 3
    if 330 <= duration_days <= 400:
        return 4
    return None


def duration_days(start: date, end: date) -> int:
    if start > end:
        raise ValueError("start cannot be after end")
    return (end - start).days + 1
