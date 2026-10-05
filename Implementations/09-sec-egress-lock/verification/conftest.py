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
