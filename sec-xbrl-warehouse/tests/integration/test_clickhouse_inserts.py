import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("clickhouse_connect")
pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


@pytest.fixture(scope="module")
def warehouse(clickhouse_settings: Settings) -> ClickHouseWarehouse:
    instance = ClickHouseWarehouse(clickhouse_settings)
    instance.apply_ddl(Path("sql/clickhouse"))
    yield instance
    instance.close()


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


def test_xbrl_publication_and_canonical_views_are_replay_safe(
    warehouse: ClickHouseWarehouse, clickhouse_settings: Settings
) -> None:
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    fixture = Path(__file__).parents[1] / "fixtures/xbrl/simple/simple.xml"
    bundle = ArelleExtractor(clickhouse_settings).extract(fixture, filing)
    counts = warehouse.insert_xbrl_bundle(bundle)
    assert counts["facts"] == 3
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_xbrl_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 0
    )
    warehouse.publish_xbrl_bundle(bundle, counts)

    revenue = next(fact for fact in bundle.facts if fact.concept_name == "Revenue")
    canonical = CanonicalFact(
        canonical_fact_id="c" * 64,
        raw_fact_id=revenue.fact_id,
        accession=filing.accession,
        cik=filing.cik,
        filed_date=filing.filed_date,
        form=filing.form,
        canonical_metric="revenue",
        normalized_value_decimal=Decimal("123456789"),
        normalized_unit="USD",
        period_kind="duration",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dimensions_json="[]",
        mapping_id=UUID("11111111-1111-1111-1111-111111111111"),
        mapping_version=1,
        mapping_confidence=1,
        mapping_explanation="integration mapping",
        extraction_revision=1,
    )
    assert warehouse.insert_canonical_facts((canonical,)) == 1

    # Replays write physical rows but preserve one logical latest row.
    warehouse.insert_xbrl_bundle(bundle)
    warehouse.publish_xbrl_bundle(bundle, counts)
    warehouse.insert_canonical_facts((canonical,))
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_xbrl_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 3
    )
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_canonical_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 1
    )
