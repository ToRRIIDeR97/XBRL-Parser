import os
import sys
from pathlib import Path

WAREHOUSE = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse"
for path in (WAREHOUSE / "src", WAREHOUSE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
os.environ.setdefault("XBRL_ENV", "test")
os.environ.setdefault("XBRL_SEC_USER_AGENT", "Test Suite test@example.com")

# Reuse the repository's integration fixtures: each creates and drops its own
# temporary PostgreSQL database / ClickHouse database and skips unless
# RUN_XBRL_INTEGRATION=1.
from tests.integration.conftest import clickhouse_settings, postgres_settings  # noqa: E402,F401


import asyncpg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


async def sql(settings, statement: str, *args):
    """Run raw SQL against the temporary test database (test setup only)."""
    url = make_url(settings.postgres_dsn).set(drivername="postgresql")
    connection = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        return await connection.fetch(statement, *args)
    finally:
        await connection.close()
