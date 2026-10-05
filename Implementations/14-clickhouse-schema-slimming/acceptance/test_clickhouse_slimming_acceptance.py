"""Worker-visible acceptance tests for 14-clickhouse-schema-slimming."""

from __future__ import annotations

from pathlib import Path

import pytest
from support14 import EXT, GAAP, bundle, concept, fact

from sec_xbrl_warehouse.domain.enums import FactValueKind


# AC-1
def test_ac1_filing_concepts_keep_only_extensions_and_taxonomy_concepts_are_separate() -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import concepts_table, taxonomy_concepts_table

    data = bundle(concepts=[concept(GAAP, "Revenues"), concept(GAAP, "Assets"), concept(EXT, "ServicesNetSales")])
    assert concepts_table(data).column("concept_name").to_pylist() == ["ServicesNetSales"]
    assert sorted(taxonomy_concepts_table(data).column("concept_name").to_pylist()) == ["Assets", "Revenues"]


# AC-3
def test_ac3_raw_value_is_kept_only_when_it_adds_information() -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import facts_table

    text = fact("Description", raw="Plain text", value=None, text="Plain text", kind=FactValueKind.TEXT)
    scaled = fact("Revenues", raw="1,234")
    table = facts_table(bundle(facts=[text, scaled]))
    raw = dict(zip(table.column("concept_name").to_pylist(), table.column("raw_value").to_pylist()))
    assert raw == {"Description": None, "Revenues": "1,234"}


# AC-2 / AC-4
@pytest.mark.integration
def test_ac2_hashes_are_stored_binary_but_read_back_as_hex(clickhouse_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    data = bundle(facts=[fact("Revenues")], concepts=[concept(GAAP, "Revenues")])
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        counts = warehouse.insert_xbrl_bundle(data)
        warehouse.publish_xbrl_bundle(data, counts)
        types = {
            row["name"]: row["type"]
            for row in warehouse.query(
                "SELECT name, type FROM system.columns WHERE database = %(db)s AND table = 'xbrl_facts'",
                {"db": clickhouse_settings.clickhouse_database},
            )
        }
        rows = warehouse.query("SELECT fact_id FROM v_xbrl_facts_latest WHERE accession = %(a)s", {"a": data.filing.accession})
    finally:
        warehouse.close()
    assert types["fact_id"] == "FixedString(32)"
    assert types.get("_row_hash", "FixedString(32)") in {"FixedString(32)", "UInt64"}
    assert [row["fact_id"] for row in rows] == [data.facts[0].fact_id]
