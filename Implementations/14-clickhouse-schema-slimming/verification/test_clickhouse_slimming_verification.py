"""Independent verification tests for 14-clickhouse-schema-slimming."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from support14v import EXT, GAAP, bundle, concept, fact

from sec_xbrl_warehouse.domain.enums import FactValueKind


# AC-3 boundaries: numeric raw text equal to the canonical value is dropped; formatted text is kept.
@pytest.mark.parametrize(
    ("raw", "value", "kept"),
    [("1000", Decimal(1000), False), ("1,000", Decimal(1000), True), ("(5)", Decimal(-5), True), ("1.50", Decimal("1.50"), False)],
)
def test_ac3_numeric_raw_value_rules(raw: str, value: Decimal, kept: bool) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import facts_table

    table = facts_table(bundle(facts=[fact("Revenues", raw=raw, value=value)]))
    assert (table.column("raw_value").to_pylist()[0] is not None) is kept


# AC-3: nil facts and text that differs from the stored text keep their raw value.
def test_ac3_text_that_differs_is_kept() -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import facts_table

    html = fact("PolicyTextBlock", raw="<p>Policy</p>", value=None, text="Policy", kind=FactValueKind.TEXT)
    table = facts_table(bundle(facts=[html]))
    assert table.column("raw_value").to_pylist() == ["<p>Policy</p>"]


# AC-4: standard concepts are stored once across filings; the filing view still lists what each filing uses.
@pytest.mark.integration
def test_ac4_taxonomy_concepts_dedupe_across_filings(clickhouse_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    first = bundle("0000000001-26-000001", facts=[fact("Revenues")], concepts=[concept(GAAP, "Revenues"), concept(GAAP, "Assets"), concept(EXT, "ServicesNetSales")])
    second = bundle("0000000001-26-000002", facts=[fact("Revenues")], concepts=[concept(GAAP, "Revenues"), concept(GAAP, "Assets")])
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        for data in (first, second):
            counts = warehouse.insert_xbrl_bundle(data)
            warehouse.publish_xbrl_bundle(data, counts)
        stored = warehouse.query("SELECT count() AS n FROM taxonomy_concepts FINAL")[0]["n"]
        used = warehouse.query(
            "SELECT concept_qname FROM v_filing_concepts_latest WHERE accession = %(a)s ORDER BY concept_qname",
            {"a": first.filing.accession},
        )
    finally:
        warehouse.close()
    assert int(stored) == 2
    assert [row["concept_qname"] for row in used] == ["ex:ServicesNetSales", "us-gaap:Revenues"]
