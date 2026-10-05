"""Worker-visible acceptance tests for 08-job-queue-hardening."""

from __future__ import annotations

import asyncio
import importlib

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from clickhouse_connect.driver.exceptions import OperationalError

from sec_xbrl_warehouse.exceptions import WarehouseWriteError
from sec_xbrl_warehouse.worker import is_retryable_exception

sql = importlib.import_module("conftest").sql


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


# AC-1
@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("Error HTTPConnectionPool(host='clickhouse', port=8123): connection refused"),
        EndpointConnectionError(endpoint_url="http://minio:9000"),
        _client_error("SlowDown"),
        _client_error("ServiceUnavailable"),
        _client_error("InternalError"),
    ],
)
def test_ac1_transient_storage_errors_are_retryable(exc: Exception) -> None:
    assert is_retryable_exception(exc)


# AC-1: wrapped in the warehouse's own error type, the cause still decides.
def test_ac1_wrapped_transient_cause_is_retryable() -> None:
    try:
        try:
            raise _client_error("SlowDown")
        except ClientError as inner:
            raise WarehouseWriteError("insert failed") from inner
    except WarehouseWriteError as outer:
        assert is_retryable_exception(outer)


async def _enqueue(control, key: str, max_attempts: int = 5):
    return await control.enqueue_job(
        job_type="ingest_filing", idempotency_key=key, payload={"k": key}, max_attempts=max_attempts
    )


# AC-2
@pytest.mark.integration
def test_ac2_dead_job_can_be_listed_and_retried(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    async def run():
        control = PostgresControlRepository(postgres_settings)
        try:
            job_id = await _enqueue(control, "dead-1", max_attempts=1)
            claimed = await control.claim_job("w1", 900)
            await control.fail_job(claimed.id, "boom", retryable=False, worker_id="w1", attempt=claimed.attempts)
            dead = await control.list_jobs(state="dead")
            await control.retry_job(job_id)
            again = await control.claim_job("w2", 900)
            return job_id, dead, again
        finally:
            await control.close()

    job_id, dead, again = asyncio.run(run())
    assert [job.id for job in dead] == [job_id]
    assert again is not None and again.id == job_id and again.attempts == 1


# AC-3
@pytest.mark.integration
def test_ac3_exhausted_job_is_dead_lettered_with_reason_and_next_job_claimed(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    async def run():
        control = PostgresControlRepository(postgres_settings)
        try:
            exhausted = await _enqueue(control, "exhausted", max_attempts=2)
            await sql(
                postgres_settings,
                "UPDATE ingestion_jobs SET state='running', attempts=2, "
                "locked_at=now() - interval '2 hours', locked_by='gone' WHERE id=$1",
                exhausted,
            )
            ready = await _enqueue(control, "ready")
            await sql(postgres_settings, "UPDATE ingestion_jobs SET created_at=now() + interval '1 minute' WHERE id=$1", ready)
            claimed = await control.claim_job("w1", 900)
            rows = await sql(postgres_settings, "SELECT state, last_error FROM ingestion_jobs WHERE id=$1", exhausted)
            return ready, claimed, rows[0]
        finally:
            await control.close()

    ready, claimed, row = asyncio.run(run())
    assert claimed is not None and claimed.id == ready
    assert row["state"] == "dead"
    assert row["last_error"] and "attempt" in row["last_error"].lower()
