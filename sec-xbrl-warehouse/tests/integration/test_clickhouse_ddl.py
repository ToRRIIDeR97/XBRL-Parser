import os
from pathlib import Path

import pytest

pytest.importorskip("clickhouse_connect")
pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

pytestmark = pytest.mark.integration


def test_clickhouse_ddl_is_idempotent() -> None:
    if os.getenv("RUN_XBRL_INTEGRATION") != "1":
        pytest.skip("Set RUN_XBRL_INTEGRATION=1 after starting Docker services")
    warehouse = ClickHouseWarehouse(Settings(env="test"))
    first = warehouse.apply_ddl(Path("sql/clickhouse"))
    second = warehouse.apply_ddl(Path("sql/clickhouse"))
    assert first == second
    assert warehouse.ping()
