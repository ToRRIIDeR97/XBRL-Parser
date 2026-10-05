from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import FilingRef, ValidationIssue
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle, XbrlFact

_DEI_NAMESPACE = re.compile(r"^http://xbrl\.sec\.gov/dei/\d{4}(?:-\d{2}-\d{2})?$")
_GYEAR = re.compile(r"^(\d{4})(?:Z|[+-]\d{2}:\d{2})?$")
FISCAL_PERIODS = frozenset(
    {"FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "M9", "T1", "T2", "T3", "M8", "CY"}
)
_YEAR = "DocumentFiscalYearFocus"
_PERIOD = "DocumentFiscalPeriodFocus"
_END = "DocumentPeriodEndDate"


def derive_fiscal_focus(
    bundle: XbrlExtractionBundle,
) -> tuple[FilingRef, tuple[ValidationIssue, ...]]:
    """Fill filing fiscal fields from the bundle's non-dimensional dei facts.

    Returns the enriched `FilingRef` and the diagnostics. Raw facts are not
    changed. A dei value replaces a different discovery value (with a
    warning); conflicting or invalid dei values leave the field NULL. Without
    a dei fact for a field, the discovery value passes through. `report_date`
    is filled only when discovery supplied none.
    """
    filing = bundle.filing
    issues: list[ValidationIssue] = []
    values: dict[str, list[XbrlFact]] = {_YEAR: [], _PERIOD: [], _END: []}
    for fact in bundle.facts:
        if (
            fact.concept_name in values
            and not fact.dimensions
            and not fact.is_nil
            and _DEI_NAMESPACE.match(fact.concept_namespace)
        ):
            values[fact.concept_name].append(fact)

    updates: dict[str, Any] = {}
    year = _resolve(values[_YEAR], _parse_year, filing, "fiscal_year", issues)
    if year is not _ABSENT:
        updates["fiscal_year"] = year
    period = _resolve(values[_PERIOD], _parse_period, filing, "fiscal_period", issues)
    if period is not _ABSENT:
        updates["fiscal_period"] = period
    if filing.report_date is None:
        end = _resolve(values[_END], _parse_date, filing, "report_date", issues)
        if end is not _ABSENT:
            updates["report_date"] = end

    mismatches = {
        field: {"discovery": _json(getattr(filing, field)), "dei": _json(value)}
        for field, value in updates.items()
        if value is not None and getattr(filing, field) not in (None, value)
    }
    if mismatches:
        issues.append(
            _issue(
                filing,
                "warehouse:fiscalFocusMismatch",
                Severity.WARNING,
                "dei fiscal focus differs from discovery metadata; dei values were used",
                mismatches,
            )
        )
    return filing.model_copy(update=updates), tuple(issues)


def _json(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


class _Absent:
    pass


_ABSENT = _Absent()


def _resolve(
    facts: list[XbrlFact],
    parse: Callable[[str], Any],
    filing: FilingRef,
    field: str,
    issues: list[ValidationIssue],
) -> Any:
    """Return the single dei value, None on conflict/invalid, or `_ABSENT`."""
    if not facts:
        return _ABSENT
    # Invalid lexical values are keyed by their text so that two different
    # invalid values still count as a conflict.
    keys: dict[object, str] = {}
    for fact in facts:
        lexical = _lexical(fact)
        value = parse(lexical)
        keys.setdefault(value if value is not None else ("invalid", lexical), lexical)
    if len(keys) > 1:
        issues.append(
            _issue(
                filing,
                "warehouse:fiscalFocusConflict",
                Severity.ERROR,
                f"Conflicting non-dimensional dei values for {field}",
                {"field": field, "values": sorted(keys.values())},
            )
        )
        return None
    key, lexical = next(iter(keys.items()))
    if isinstance(key, tuple):
        issues.append(
            _issue(
                filing,
                "warehouse:fiscalFocusInvalid",
                Severity.WARNING,
                f"Invalid dei value for {field}: {lexical!r}",
                {"field": field, "value": lexical},
            )
        )
        return None
    return key


def _lexical(fact: XbrlFact) -> str:
    if fact.value_date is not None:
        return fact.value_date.isoformat()
    return (fact.value_text if fact.value_text is not None else fact.raw_value or "").strip()


def _parse_year(lexical: str) -> int | None:
    match = _GYEAR.match(lexical)
    return int(match.group(1)) if match else None


def _parse_period(lexical: str) -> str | None:
    value = lexical.upper()
    return value if value in FISCAL_PERIODS else None


def _parse_date(lexical: str) -> date | None:
    try:
        return date.fromisoformat(lexical)
    except ValueError:
        return None


def _issue(
    filing: FilingRef,
    code: str,
    severity: Severity,
    message: str,
    details: dict[str, Any],
) -> ValidationIssue:
    return ValidationIssue(
        source="warehouse",
        code=code,
        severity=severity,
        message=message,
        accession=filing.accession,
        details=details,
    )
