from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import clickhouse_connect
import pytest
from sqlalchemy.engine import make_url

from sec_xbrl_warehouse.config import Settings

ROOT = Path(__file__).resolve().parents[2]


async def _postgres_command(dsn: str, statement: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def postgres_settings() -> Settings:
    if os.getenv("RUN_XBRL_INTEGRATION") != "1":
        pytest.skip("Set RUN_XBRL_INTEGRATION=1 after starting Docker services")
    base = Settings(env="test")
    database = f"xbrl_test_{uuid4().hex[:16]}"
    url = make_url(base.postgres_dsn)
    admin_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    test_dsn = url.set(database=database).render_as_string(hide_password=False)
    asyncio.run(_postgres_command(admin_dsn, f'CREATE DATABASE "{database}"'))
    environment = {
        **os.environ,
        "XBRL_ENV": "test",
        "XBRL_POSTGRES_DSN": test_dsn,
        "PYTHONPATH": str(ROOT / "src"),
    }
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        yield base.model_copy(update={"postgres_dsn": test_dsn})
    finally:
        asyncio.run(
            _postgres_command(admin_dsn, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        )


@pytest.fixture(scope="session")
def clickhouse_settings() -> Settings:
    if os.getenv("RUN_XBRL_INTEGRATION") != "1":
        pytest.skip("Set RUN_XBRL_INTEGRATION=1 after starting Docker services")
    base = Settings(env="test")
    database = f"xbrl_test_{uuid4().hex[:16]}"
    client = clickhouse_connect.get_client(
        host=base.clickhouse_host,
        port=base.clickhouse_port,
        username=base.clickhouse_username,
        password=base.clickhouse_password,
        database=base.clickhouse_database,
        secure=base.clickhouse_secure,
    )
    client.command(f"CREATE DATABASE {database}")
    try:
        yield base.model_copy(update={"clickhouse_database": database})
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        client.close()
