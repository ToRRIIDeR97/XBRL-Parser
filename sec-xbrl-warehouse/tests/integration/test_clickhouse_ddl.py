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
        upgraded.client.command(
            f"""CREATE TABLE {database}.sec_submissions
                (adsh String, _source_revision UInt32, _ingested_at DateTime64(3, 'UTC'))
                ENGINE = ReplacingMergeTree(_source_revision) ORDER BY adsh"""
        )
        upgraded.client.command(
            f"INSERT INTO {database}.sec_submissions VALUES ('legacy', 1, now64(3))"
        )
        assert upgraded.schema_drift(Path("sql/clickhouse")) == ("sec_submissions",)
        backups = upgraded.upgrade_ddl(Path("sql/clickhouse"))
        assert backups.keys() == {"sec_submissions"}
        assert upgraded.query(f"SELECT count() AS n FROM {database}.sec_submissions")[0]["n"] == 1
        assert (
            upgraded.query(
                "SELECT count() AS n FROM system.tables "
                "WHERE database = %(database)s AND name = %(table)s",
                {"database": database, "table": backups["sec_submissions"]},
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
