from datetime import date
from decimal import Decimal

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import (
    classify_period,
    coerce_typed_value,
    relationship_type_from_arcrole,
    semantic_hash,
)


def test_semantic_hash_is_key_order_independent() -> None:
    assert semantic_hash({"a": 1, "b": 2}) == semantic_hash({"b": 2, "a": 1})


def test_numeric_coercion_uses_decimal() -> None:
    kind, value = coerce_typed_value("0.1", is_nil=False, is_numeric=True, base_xsd_type=None)
    assert kind is FactValueKind.NUMERIC
    assert value == Decimal("0.1")


def test_period_and_relationship_classification() -> None:
    assert classify_period(start=None, end=None, instant=date(2025, 1, 1)) is PeriodKind.INSTANT
    assert (
        relationship_type_from_arcrole("http://www.xbrl.org/2003/arcrole/summation-item")
        is RelationshipType.CALCULATION
    )
