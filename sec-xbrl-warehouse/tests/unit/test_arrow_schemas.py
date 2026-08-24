from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.exceptions import WarehouseWriteError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    canonical_fact_schema,
    require_exact_schema,
    sec_source_schema,
    sec_warehouse_schema,
    xbrl_warehouse_schema,
)


def test_all_declared_arrow_contracts_can_be_built() -> None:
    for table in SecTable:
        assert sec_source_schema(table).names
        assert sec_warehouse_schema(table).names

    for entity in ("contexts", "concepts", "facts", "relationships", "footnotes"):
        assert xbrl_warehouse_schema(entity).names

    assert canonical_fact_schema().names


def test_exact_schema_accepts_identical_schema() -> None:
    schema = sec_warehouse_schema(SecTable.NUM)
    require_exact_schema(schema, schema, name="sec_numeric_facts")


def test_exact_schema_rejects_reordered_fields() -> None:
    expected = pa.schema(
        [
            pa.field("first", pa.string(), nullable=False),
            pa.field("second", pa.int64(), nullable=True),
        ]
    )
    actual = pa.schema(list(reversed(expected)))

    with pytest.raises(WarehouseWriteError, match="column mismatch"):
        require_exact_schema(actual, expected, name="test")


def test_exact_schema_rejects_missing_field() -> None:
    expected = pa.schema(
        [
            pa.field("first", pa.string(), nullable=False),
            pa.field("second", pa.int64(), nullable=True),
        ]
    )
    actual = pa.schema([expected.field("first")])

    with pytest.raises(WarehouseWriteError, match="column mismatch"):
        require_exact_schema(actual, expected, name="test")


def test_exact_schema_rejects_type_or_nullability_change() -> None:
    expected = pa.schema([pa.field("value", pa.int64(), nullable=False)])
    actual = pa.schema([pa.field("value", pa.string(), nullable=True)])

    with pytest.raises(WarehouseWriteError, match="type/nullability mismatch"):
        require_exact_schema(actual, expected, name="test")
