from __future__ import annotations

from decimal import Decimal

from sec_xbrl_warehouse.domain.enums import FactValueKind
from sec_xbrl_warehouse.domain.xbrl import XbrlFact

MONETARY_MEASURES = {
    "iso4217:USD": "USD",
    "iso4217:EUR": "EUR",
    "iso4217:GBP": "GBP",
    "iso4217:JPY": "JPY",
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "JPY": "JPY",
}


def normalize_numeric_value(fact: XbrlFact, *, sign_multiplier: int = 1) -> Decimal | None:
    """Apply only an explicitly justified sign convention to a numeric fact."""
    if sign_multiplier not in {-1, 0, 1}:
        raise ValueError("sign_multiplier must be -1, 0, or 1")
    if fact.value_kind is not FactValueKind.NUMERIC or fact.value_decimal is None:
        return None
    return fact.value_decimal * Decimal(sign_multiplier)


def normalize_unit(fact: XbrlFact) -> str | None:
    """Return a query-friendly unit while preserving the raw unit on the fact."""
    if fact.unit is None:
        return None
    numerators = fact.unit.numerator_measures
    denominators = fact.unit.denominator_measures
    if len(numerators) == 1 and not denominators:
        return MONETARY_MEASURES.get(numerators[0], numerators[0])
    numerator = "*".join(sorted(numerators)) or "1"
    denominator = "*".join(sorted(denominators))
    return f"{numerator}/{denominator}" if denominator else numerator


def fact_semantic_key(fact: XbrlFact) -> tuple[object, ...]:
    """Key suitable for bulk-vs-Arelle fact reconciliation."""
    return (
        fact.concept_namespace,
        fact.concept_name,
        fact.taxonomy_version,
        fact.period_kind.value,
        fact.period_start,
        fact.period_end,
        fact.period_instant,
        normalize_unit(fact),
        fact.dimension_hash,
        fact.language,
    )
