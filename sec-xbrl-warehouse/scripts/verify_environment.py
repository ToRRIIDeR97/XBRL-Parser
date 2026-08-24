from __future__ import annotations

import importlib.metadata
import platform
import shutil
import sys
from pathlib import Path

REQUIRED_COMMANDS = ("docker",)
PACKAGES = (
    "arelle-release",
    "edgartools",
    "clickhouse-connect",
    "pyarrow",
    "sqlalchemy",
    "alembic",
    "fastapi",
)


def main() -> int:
    failures = 0
    print(f"Python: {sys.version.split()[0]} ({platform.platform()})")
    if sys.version_info < (3, 12):  # noqa: UP036 - runtime guard for host installs
        print("FAIL: Python 3.12 or later is required")
        failures += 1
    for command in REQUIRED_COMMANDS:
        path = shutil.which(command)
        print(f"{command}: {path or 'MISSING'}")
        failures += int(path is None)
    for package in PACKAGES:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "MISSING"
            failures += 1
        print(f"{package}: {version}")
    for path in (Path("config"), Path("sql/clickhouse"), Path("migrations")):
        exists = path.exists()
        print(f"{path}: {'OK' if exists else 'MISSING'}")
        failures += int(not exists)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
