import os
from pathlib import Path

import pytest

pytest.importorskip("clickhouse_connect")
pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


@pytest.fixture(scope="module")
def warehouse() -> ClickHouseWarehouse:
    settings = Settings(env="test")
    yield ClickHouseWarehouse(settings)


def test_sec_bulk_tables_load_and_dedupe(warehouse: ClickHouseWarehouse) -> None:
    """Load a typed batch per SEC table; second load must not change logical counts."""
    fixture = Path(__file__).parents[1] / "fixtures/fsnds-mini.zip"
    from sec_xbrl_warehouse.ingestion.bulk_loader import enrich_sec_batch
    from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader

    reader = SecTsvBatchReader(batch_rows=1)
    artifact_id = "11111111-1111-1111-1111-111111111111"
    period = "2025Q4"
    revision = 1

    for table in SecTable:
        raw = next(reader.iter_batches(fixture, table))
        enriched = enrich_sec_batch(
            table,
            raw,
            artifact_id=artifact_id,
            source_period=period,
            source_revision=revision,
        )
        first = warehouse.insert_sec_batch(table, enriched)
        assert first == enriched.num_rows, table
        # Re-load same logical rows -> ReplacingMergeTree collapses duplicates.
        again = warehouse.insert_sec_batch(table, enriched)
        assert again == enriched.num_rows, table

    # Verify deduplicated logical count equals source row count (1 per table).
    # ReplacingMergeTree merges are asynchronous; use argMax to get the logical
    # latest-revision state without depending on background merges.
    for table in SecTable:
        target = {
            SecTable.SUB: "sec_submissions",
            SecTable.TAG: "sec_tags",
            SecTable.DIM: "sec_dimensions",
            SecTable.NUM: "sec_numeric_facts",
            SecTable.TXT: "sec_text_facts",
            SecTable.REN: "sec_renderings",
            SecTable.PRE: "sec_presentations",
            SecTable.CAL: "sec_calculations",
        }[table]
        rows = warehouse.query(
            f"""SELECT count() AS n FROM (
                    SELECT _row_hash, argMax(_source_revision, _source_revision) AS rev
                    FROM {target}
                    WHERE _artifact_id = %(aid)s
                    GROUP BY _row_hash
                )""",
            {"aid": artifact_id},
        )
        assert rows[0]["n"] == 1, table
