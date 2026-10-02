from pathlib import Path

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor

pytestmark = pytest.mark.integration


async def test_reconciliation_uses_text_values_and_rejects_oversized_results(clickhouse_settings):
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        accession = "0000000009-26-000001"
        value = "A filing text block " * 1000
        warehouse.client.command(
            """INSERT INTO sec_text_facts (adsh, tag, version, ddate, qtrs, lang, value)
               VALUES (%(accession)s, 'Disclosure', %(accession)s, '2025-12-31', 0, 'en', %(value)s)""",
            parameters={"accession": accession, "value": value},
        )
        captured = []

        class RecordingWarehouse:
            def query(self, sql, parameters=None):
                rows = warehouse.query(sql, parameters)
                captured.append(rows)
                return rows

        ingestor = IncrementalFilingIngestor(
            None, None, None, None, RecordingWarehouse(), settings=clickhouse_settings
        )
        result = await ingestor.reconcile(accession)
        assert result["bulk_count"] == 1
        assert captured[1][0]["value_raw"] == value
        ingestor.settings = clickhouse_settings.model_copy(update={"reconciliation_max_bytes": 100})
        with pytest.raises(DatabaseError):
            await ingestor.reconcile(accession)
    finally:
        warehouse.close()
