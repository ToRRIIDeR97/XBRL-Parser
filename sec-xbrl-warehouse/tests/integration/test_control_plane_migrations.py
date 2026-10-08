"""Upgrade and downgrade the control-plane migrations on a scratch database."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from sec_xbrl_warehouse.config import Settings

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


def _alembic(dsn: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "XBRL_ENV": "test",
            "XBRL_POSTGRES_DSN": dsn,
            "PYTHONPATH": str(ROOT / "src"),
        },
        check=True,
        capture_output=True,
        text=True,
    )


async def _run(dsn: str, statement: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(dsn)
    try:
        return list(await connection.fetch(statement, *args))
    finally:
        await connection.close()


def _review(accession: str, fact_id: str, namespace: str, concept: str, state: str = "open"):
    evidence = {"namespace": namespace, "concept_name": concept, "reason": "seed"}
    return (uuid4(), accession, fact_id, f"x:{concept}", "[]", json.dumps(evidence), state)


def test_review_and_issue_migrations_upgrade_and_downgrade() -> None:
    base = Settings(env="test")
    database = f"xbrl_migration_{uuid4().hex[:12]}"
    url = make_url(base.postgres_dsn.get_secret_value())
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    plain = url.set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )
    dsn = url.set(database=database).render_as_string(hide_password=False)
    asyncio.run(_run(admin, f'CREATE DATABASE "{database}"'))
    try:
        _alembic(dsn, "upgrade", "0001_control_plane")
        ns = "http://fasb.org/us-gaap/2025"
        rows = [
            _review("0000000001-26-000001", "f1", ns, "Revenues"),
            _review("0000000001-26-000001", "f2", ns, "Revenues"),
            _review("0000000001-26-000002", "f3", ns, "Revenues"),
            _review("0000000001-26-000002", "f4", "http://example.com/2025", "Revenues"),
            _review("0000000001-26-000002", "f5", ns, "Assets", state="resolved"),
        ]

        async def seed() -> None:
            connection = await asyncpg.connect(plain)
            try:
                for index, row in enumerate(rows):
                    await connection.execute(
                        "INSERT INTO mapping_reviews (id, accession, fact_id, concept_qname, "
                        "candidate_metric_codes, evidence, state, created_at) VALUES "
                        "($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, now() + make_interval(secs => $8))",
                        *row,
                        index,
                    )
            finally:
                await connection.close()

        asyncio.run(seed())
        _alembic(dsn, "upgrade", "head")
        states = asyncio.run(
            _run(plain, "SELECT state, count(*) AS n FROM mapping_reviews GROUP BY state")
        )
        assert {row["state"]: row["n"] for row in states} == {
            "open": 2,
            "superseded": 2,
            "resolved": 1,
        }
        (kept,) = asyncio.run(
            _run(
                plain,
                "SELECT id, evidence FROM mapping_reviews WHERE state = 'open' "
                "AND concept_namespace = $1",
                ns,
            )
        )
        evidence = json.loads(kept["evidence"])
        assert kept["id"] == rows[0][0]
        assert (evidence["fact_count"], evidence["filing_count"]) == (3, 2)
        assert evidence["latest_accession"] == "0000000001-26-000002"
        observations = asyncio.run(
            _run(plain, "SELECT sum(fact_count) AS n FROM mapping_review_observations")
        )
        assert observations[0]["n"] == 4
        columns = asyncio.run(
            _run(
                plain,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'validation_issues' AND column_name = 'extraction_revision'",
            )
        )
        assert len(columns) == 1

        tables = asyncio.run(_run(plain, "SELECT to_regclass('companies') AS name"))
        assert tables[0]["name"] == "companies"
        _alembic(dsn, "downgrade", "0003_mapping_review_per_concept")
        tables = asyncio.run(_run(plain, "SELECT to_regclass('companies') AS name"))
        assert tables[0]["name"] is None
        _alembic(dsn, "downgrade", "0002_validation_issue_revision")
        states = asyncio.run(
            _run(plain, "SELECT state, count(*) AS n FROM mapping_reviews GROUP BY state")
        )
        assert {row["state"]: row["n"] for row in states} == {"open": 4, "resolved": 1}
        _alembic(dsn, "downgrade", "0001_control_plane")
        columns = asyncio.run(
            _run(
                plain,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'validation_issues' AND column_name = 'extraction_revision'",
            )
        )
        assert columns == []
        _alembic(dsn, "upgrade", "head")
    finally:
        asyncio.run(_run(admin, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
