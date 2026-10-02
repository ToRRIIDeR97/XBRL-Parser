from pathlib import Path
from types import SimpleNamespace

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    ClickHouseWarehouse,
    _ddl_columns,
    _preserving_type,
)


def test_column_drift_includes_type_nullability_and_defaults():
    warehouse = ClickHouseWarehouse(Settings(), client=SimpleNamespace())
    definitions = warehouse._table_definitions(Path(__file__).parents[2] / "sql/clickhouse")
    statement, key = definitions["sec_dimensions"]
    expected = _ddl_columns(statement)
    warehouse.client.query = lambda *a, **k: SimpleNamespace(result_rows=[("sec_dimensions", key)])
    warehouse._column_contract = lambda name: dict(expected)
    assert warehouse.schema_drift(Path(__file__).parents[2] / "sql/clickhouse") == ()
    for replacement in [
        ("fixedstring(32)", "", ""),
        ("nullable(string)", "", ""),
        ("string", "DEFAULT", "'x'"),
    ]:
        warehouse._column_contract = lambda name, replacement=replacement: {
            **expected,
            "dimh": replacement,
        }
        assert warehouse.schema_drift(Path(__file__).parents[2] / "sql/clickhouse") == (
            "sec_dimensions",
        )
    assert _preserving_type("FixedString(32)", "Nullable(String)")
    assert _preserving_type("LowCardinality(String)", "LowCardinality(Nullable(String))")
    assert not _preserving_type("Nullable(String)", "String")
    assert not _preserving_type("UInt64", "UInt8")
