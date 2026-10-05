"""Independent verification tests for 08-job-queue-hardening."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from clickhouse_connect.driver.exceptions import DatabaseError

from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SchemaDriftError
from sec_xbrl_warehouse.worker import is_retryable_exception

sql = importlib.import_module("conftest").sql
OPS = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "ops"


# AC-1: permanent storage failures stay permanent.
@pytest.mark.parametrize(
    "exc",
    [
        ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject"),
        ClientError({"Error": {"Code": "NoSuchBucket", "Message": "no"}}, "PutObject"),
        DatabaseError("Code: 62. DB::Exception: Syntax error: failed at position 1"),
        DatabaseError("Code: 60. DB::Exception: Table xbrl.missing does not exist"),
        ArtifactIntegrityError("checksum"),
        SchemaDriftError("header"),
    ],
)
def test_ac1_permanent_errors_are_not_retried(exc: Exception) -> None:
    assert not is_retryable_exception(exc)


# AC-1: ClickHouse server-side transient codes are retryable.
@pytest.mark.parametrize(
    "message",
    [
        "Code: 252. DB::Exception: Too many parts (300). (TOO_MANY_PARTS)",
        "Code: 241. DB::Exception: Memory limit (total) exceeded. (MEMORY_LIMIT_EXCEEDED)",
        "Code: 159. DB::Exception: Timeout exceeded. (TIMEOUT_EXCEEDED)",
    ],
)
def test_ac1_clickhouse_transient_codes_are_retryable(message: str) -> None:
    assert is_retryable_exception(DatabaseError(message))


async def _control(settings):
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    return PostgresControlRepository(settings)


# AC-2: only dead jobs can be revived; others are refused.
@pytest.mark.integration
def test_ac2_retry_refuses_non_dead_jobs(postgres_settings) -> None:
    from sec_xbrl_warehouse.exceptions import StateTransitionError

    async def run():
        control = await _control(postgres_settings)
        try:
            job_id = await control.enqueue_job(job_type="ingest_filing", idempotency_key="live", payload={})
            with pytest.raises(StateTransitionError):
                await control.retry_job(job_id)
            listed = await control.list_jobs(state="pending", job_type="ingest_filing")
            return job_id, listed
        finally:
            await control.close()

    job_id, listed = asyncio.run(run())
    assert [job.id for job in listed] == [job_id]


# AC-4: backoff uses database time, not the worker host's clock.
@pytest.mark.integration
def test_ac4_backoff_ignores_skewed_host_clock(postgres_settings, monkeypatch) -> None:
    import sec_xbrl_warehouse.infrastructure.storage.postgres as postgres_module

    class _Skewed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)

    monkeypatch.setattr(postgres_module, "datetime", _Skewed)

    async def run():
        control = await _control(postgres_settings)
        try:
            await control.enqueue_job(job_type="ingest_filing", idempotency_key="skew", payload={})
            job = await control.claim_job("w1", 900)
            await control.fail_job(job.id, "transient", retryable=True, worker_id="w1", attempt=job.attempts)
            rows = await sql(postgres_settings, "SELECT available_at - now() AS delay FROM ingestion_jobs WHERE id=$1", job.id)
            return rows[0]["delay"]
        finally:
            await control.close()

    delay = asyncio.run(run())
    assert timedelta(0) <= delay <= timedelta(hours=1)


# AC-4: retries are jittered and capped.
@pytest.mark.integration
def test_ac4_backoff_is_jittered_within_bounds(postgres_settings) -> None:
    async def run():
        control = await _control(postgres_settings)
        try:
            ids = []
            for index in range(12):
                await control.enqueue_job(job_type="ingest_filing", idempotency_key=f"j{index}", payload={})
            for _ in range(12):
                job = await control.claim_job("w1", 900)
                await control.fail_job(job.id, "transient", retryable=True, worker_id="w1", attempt=job.attempts)
                ids.append(job.id)
            rows = await sql(
                postgres_settings,
                "SELECT extract(epoch FROM available_at - updated_at) AS seconds FROM ingestion_jobs WHERE id = ANY($1::uuid[])",
                ids,
            )
            return [float(row["seconds"]) for row in rows]
        finally:
            await control.close()

    seconds = asyncio.run(run())
    assert len({round(value, 3) for value in seconds}) > 1, "expected jitter"
    assert all(0 < value <= 3600 for value in seconds)


# AC-5: a queue snapshot exposes backlog by state and the oldest ready job's age.
@pytest.mark.integration
def test_ac5_queue_snapshot_reports_backlog(postgres_settings) -> None:
    async def run():
        control = await _control(postgres_settings)
        try:
            for index in range(3):
                await control.enqueue_job(job_type="load_bulk", idempotency_key=f"b{index}", payload={})
            await sql(postgres_settings, "UPDATE ingestion_jobs SET created_at = now() - interval '2 hours', available_at = now() - interval '2 hours'")
            return await control.queue_snapshot()
        finally:
            await control.close()

    snapshot = asyncio.run(run())
    assert snapshot.counts[("load_bulk", "pending")] == 3
    assert snapshot.oldest_ready_age_seconds >= 7000


# AC-5: alert rules cover dead jobs and a stalled queue.
def test_ac5_alert_rules_cover_queue_health() -> None:
    from sec_xbrl_warehouse.config_files import load_yaml

    rules = [rule for group in load_yaml(OPS / "alerts.yml")["groups"] for rule in group["rules"]]
    expressions = " ".join(str(rule.get("expr", "")) for rule in rules)
    assert "dead" in expressions
    assert "xbrl_queue_oldest_ready_age_seconds" in expressions
