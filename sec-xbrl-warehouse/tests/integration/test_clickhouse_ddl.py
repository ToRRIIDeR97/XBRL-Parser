from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("clickhouse_connect")
pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

pytestmark = pytest.mark.integration


def test_clickhouse_ddl_is_idempotent(clickhouse_settings: Settings) -> None:
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        first = warehouse.apply_ddl(Path("sql/clickhouse"))
        second = warehouse.apply_ddl(Path("sql/clickhouse"))
        assert first == second
        assert warehouse.ping()
    finally:
        warehouse.close()


def test_clickhouse_upgrade_preserves_rows_and_old_table(clickhouse_settings: Settings) -> None:
    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_upgrade_{uuid4().hex[:12]}"
    admin.client.command(f"CREATE DATABASE {database}")
    upgraded: ClickHouseWarehouse | None = None
    try:
        upgraded = ClickHouseWarehouse(
            clickhouse_settings.model_copy(update={"clickhouse_database": database})
        )
        definition = upgraded._table_definitions(Path("sql/clickhouse"))["sec_dimensions"][0]
        upgraded.client.command(definition.replace("dimh String", "dimh FixedString(32)"))
        upgraded.client.command(
            f"INSERT INTO {database}.sec_dimensions (dimh, segments, segt, _artifact_id, _source_revision, _row_hash) "
            "VALUES ('0123456789abcdef0123456789abcdef', 'preserved', 1, '11111111-1111-1111-1111-111111111111', 1, 'hash')"
        )
        assert upgraded.schema_drift(Path("sql/clickhouse")) == ("sec_dimensions",)
        before = upgraded.query(
            f"SELECT dimh, segments, segt, _artifact_id, _source_revision FROM {database}.sec_dimensions"
        )
        before[0]["dimh"] = before[0]["dimh"].decode()
        backups = upgraded.upgrade_ddl(Path("sql/clickhouse"))
        assert (
            upgraded.query(
                f"SELECT dimh, segments, segt, _artifact_id, _source_revision FROM {database}.sec_dimensions"
            )
            == before
        )
        assert upgraded.schema_drift(Path("sql/clickhouse")) == ()
        assert backups.keys() == {"sec_dimensions"}
        assert upgraded.query(f"SELECT count() AS n FROM {database}.sec_dimensions")[0]["n"] == 1
        assert (
            upgraded.query(
                "SELECT count() AS n FROM system.tables "
                "WHERE database = %(database)s AND name = %(table)s",
                {"database": database, "table": backups["sec_dimensions"]},
            )[0]["n"]
            == 1
        )
    finally:
        if upgraded is not None:
            upgraded.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()


def test_shared_client_concurrency_and_query_budgets(clickhouse_settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from clickhouse_connect.driver.exceptions import DatabaseError

    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        barrier = Barrier(2)

        def query():
            barrier.wait(timeout=5)
            return warehouse.query("SELECT sleep(0.05) AS value")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(query) for _ in range(2)]
            assert all(future.result() == [{"value": 0}] for future in futures)
    finally:
        warehouse.close()
    reader = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"api_query_max_rows": 10000}), read_only=True
    )
    try:
        assert reader.query("SELECT 1 AS value") == [{"value": 1}]
        with pytest.raises(DatabaseError):
            reader.query("CREATE TABLE forbidden (value UInt8) ENGINE = Memory")
        with pytest.raises(DatabaseError):
            reader.query("SELECT sum(number) FROM numbers(10001)")
    finally:
        reader.close()


def test_nullable_upgrade_preserves_populated_values(clickhouse_settings):
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_nullable_{uuid4().hex[:12]}"
    warehouse.client.command(f"CREATE DATABASE {database}")
    warehouse.database = database
    try:
        ddl = warehouse._table_definitions(Path("sql/clickhouse"))["sec_tags"][0]
        warehouse.client.command(ddl.replace("doc Nullable(String)", "doc String"))
        warehouse.client.command(
            f"INSERT INTO {database}.sec_tags (tag, version, doc) VALUES ('Revenue', '2026', 'original text')"
        )
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ("sec_tags",)
        warehouse.upgrade_ddl(Path("sql/clickhouse"))
        assert warehouse.query(f"SELECT doc FROM {database}.sec_tags") == [{"doc": "original text"}]
        warehouse.client.command(
            f"INSERT INTO {database}.sec_tags (tag, version, doc) VALUES ('Assets', '2026', NULL)"
        )
        assert warehouse.query(f"SELECT countIf(doc IS NULL) AS n FROM {database}.sec_tags") == [
            {"n": 1}
        ]
    finally:
        warehouse.client.command(f"DROP DATABASE {database} SYNC")
        warehouse.close()


def test_cal_signed_upgrade_preserves_unsigned_values(clickhouse_settings):
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_cal_{uuid4().hex[:12]}"
    warehouse.client.command(f"CREATE DATABASE {database}")
    warehouse.database = database
    try:
        ddl = warehouse._table_definitions(Path("sql/clickhouse"))["sec_calculations"][0]
        warehouse.client.command(ddl.replace("negative Int16", "negative UInt8"))
        warehouse.client.command(
            f"INSERT INTO {database}.sec_calculations (arc, negative) VALUES (1, 0), (2, 1), (3, 255)"
        )
        backups = warehouse.upgrade_ddl(Path("sql/clickhouse"))
        assert backups.keys() == {"sec_calculations"}
        for table in ("sec_calculations", backups["sec_calculations"]):
            assert warehouse.query(f"SELECT negative FROM {database}.{table} ORDER BY arc") == [
                {"negative": 0},
                {"negative": 1},
                {"negative": 255},
            ]
        warehouse.client.command(
            f"INSERT INTO {database}.sec_calculations (arc, negative) VALUES (4, -1)"
        )
        assert warehouse.query(f"SELECT negative FROM {database}.sec_calculations WHERE arc=4") == [
            {"negative": -1}
        ]
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ()
        assert warehouse.upgrade_ddl(Path("sql/clickhouse")) == {}
    finally:
        warehouse.client.command(f"DROP DATABASE {database} SYNC")
        warehouse.close()


def test_text_upgrade_keeps_distinct_source_rows_and_deduplicates_replay(clickhouse_settings):
    from sec_xbrl_warehouse.validation.data_quality import _uniqueness_checks

    warehouse = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_txt_{uuid4().hex[:12]}"
    warehouse.client.command(f"CREATE DATABASE {database}")
    warehouse.close()
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    aid = "11111111-1111-1111-1111-111111111111"
    try:
        ddl = warehouse._table_definitions(Path("sql/clickhouse"))["sec_text_facts"][0]
        warehouse.client.command(ddl.replace("_artifact_id, _row_hash)", "_artifact_id)"))
        warehouse.client.command(
            f"INSERT INTO {database}.sec_text_facts (_artifact_id,context,value,_row_hash) VALUES ('{aid}','context-a','first','hash-a')"
        )
        backups = warehouse.upgrade_ddl(Path("sql/clickhouse"))
        assert backups.keys() == {"sec_text_facts"}
        for _ in range(2):
            warehouse.client.command(
                f"INSERT INTO {database}.sec_text_facts (_artifact_id,context,value,_row_hash) VALUES ('{aid}','context-a','first','hash-a'), ('{aid}','context-b','second','hash-b'), ('{aid}','context-b','third','hash-c')"
            )
        rows = warehouse.query(
            f"SELECT context,value FROM {database}.sec_text_facts FINAL ORDER BY value"
        )
        assert rows == [
            {"context": "context-a", "value": "first"},
            {"context": "context-b", "value": "second"},
            {"context": "context-b", "value": "third"},
        ]
        checks = _uniqueness_checks("sec_text_facts", aid, warehouse)
        assert len(checks) == 1 and checks[0].passed and checks[0].observed == 3
        assert warehouse.query(
            f"SELECT count() AS n FROM {database}.{backups['sec_text_facts']} FINAL"
        ) == [{"n": 1}]
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ()
    finally:
        warehouse.client.command(f"DROP DATABASE {database} SYNC")
        warehouse.close()


@pytest.mark.parametrize("table_name", ["sec_numeric_facts", "sec_text_facts"])
def test_bulk_dates_preserve_full_year_range_and_replay(clickhouse_settings, table_name):
    from datetime import date

    import pyarrow as pa

    from sec_xbrl_warehouse.domain.enums import SecTable
    from sec_xbrl_warehouse.ingestion.bulk_loader import enrich_sec_batch
    from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader

    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_dates_{uuid4().hex[:12]}"
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    try:
        ddl = warehouse._table_definitions(Path("sql/clickhouse"))[table_name][0]
        warehouse.client.command(ddl.replace("ddate String", "ddate Date"))
        warehouse.client.command(
            f"INSERT INTO {table_name} (ddate) SETTINGS optimize_on_insert=0 "
            "VALUES ('2025-12-31'), ('2025-12-31')"
        )
        backups = warehouse.upgrade_ddl(Path("sql/clickhouse"))
        assert warehouse.query(f"SELECT ddate FROM {table_name}") == [{"ddate": "2025-12-31"}] * 2
        assert (
            warehouse.query(f"SELECT ddate FROM {backups[table_name]}")
            == [{"ddate": date(2025, 12, 31)}] * 2
        )
        table = SecTable.NUM if table_name == "sec_numeric_facts" else SecTable.TXT
        raw = next(SecTsvBatchReader().iter_batches(Path("tests/fixtures/fsnds-mini.zip"), table))
        rows = [{**raw.to_pylist()[0], "ddate": f"{year:04d}1231"} for year in (205, 2204, 9999)]
        batch = enrich_sec_batch(
            table,
            pa.RecordBatch.from_pylist(rows, schema=raw.schema),
            artifact_id="22222222-2222-2222-2222-222222222222",
            source_period="2026-05",
            source_revision=1,
            sub_lookup={
                rows[0]["adsh"]: {
                    "cik": 1,
                    "filed": date(2026, 5, 1),
                    "form": "10-Q",
                    "fy": 2026,
                    "fp": "Q1",
                }
            },
        )
        assert batch.schema.field("ddate").type == pa.date32()
        for _ in range(2):
            assert warehouse.insert_sec_batch(table, batch) == 3
        assert warehouse.query(
            f"SELECT ddate FROM {table_name} FINAL WHERE _artifact_id='22222222-2222-2222-2222-222222222222' ORDER BY ddate"
        ) == [{"ddate": "0205-12-31"}, {"ddate": "2204-12-31"}, {"ddate": "9999-12-31"}]
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ()
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE {database} SYNC")
        admin.close()
