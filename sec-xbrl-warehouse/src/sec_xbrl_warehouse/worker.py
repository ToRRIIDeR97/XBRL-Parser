from __future__ import annotations

import asyncio
import contextlib
import re
import signal
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from typing import Any

import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.models import JobRecord
from sec_xbrl_warehouse.domain.xbrl import CURRENT_EXTRACTION_REVISION
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    DiskReserveError,
    SchemaDriftError,
    SecRetryDeferred,
    StateTransitionError,
)
from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError
from sec_xbrl_warehouse.infrastructure.storage.postgres import (
    PostgresControlRepository,
    checkpoint_lease,
)
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.observability.metrics import JOB_TRANSITIONS, JOBS, STAGE_LATENCY

logger = get_logger(__name__)
JobHandler = Callable[[JobRecord], Coroutine[Any, Any, dict[str, Any] | None]]


@contextlib.asynccontextmanager
async def runtime_services(
    settings: Settings, control: PostgresControlRepository
) -> AsyncIterator[dict[str, Any]]:
    """Own the network clients used by every worker stage."""
    from sec_xbrl_warehouse.config_files import load_yaml
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressLock
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
    from sec_xbrl_warehouse.infrastructure.storage.object_store import create_object_store
    from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor
    from sec_xbrl_warehouse.ingestion.bulk_downloader import BulkArtifactDownloader
    from sec_xbrl_warehouse.ingestion.bulk_loader import BulkArtifactLoader
    from sec_xbrl_warehouse.ingestion.filing_discovery import (
        SecDailyIndexDiscovery,
        SecSubmissionsDiscovery,
    )
    from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader
    from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor
    from sec_xbrl_warehouse.ingestion.orchestrator import IngestionOrchestrator
    from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader
    from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
    from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository

    settings.ensure_directories()
    sources = load_yaml(settings.config_dir / "sources.yml")["sec"]
    store = create_object_store(settings)
    await asyncio.to_thread(store.ensure_bucket)
    warehouse = await asyncio.to_thread(ClickHouseWarehouse, settings)
    try:
        async with SecHttpClient(settings, egress_lock=SecEgressLock(settings)) as http:
            repository = PostgresMappingRepository(settings, engine=control.engine)
            mapper = CanonicalMapper.load_rules(
                settings.config_dir / "canonical_metrics.yml",
                settings.config_dir / "concept_seed.yml",
                repository,
            )
            await repository.seed_metrics(tuple(mapper.metrics.values()))
            writer = ParquetBatchWriter(settings, store)
            loader = BulkArtifactLoader(
                SecTsvBatchReader(settings.arrow_batch_rows, settings.arrow_batch_bytes),
                writer,
                warehouse,
                control,
                store,
                settings=settings,
            )
            yield {
                "control": control,
                "warehouse": warehouse,
                "object_store": store,
                "mapper": mapper,
                "forms": sources["default_forms"],
                "discovery": SecDailyIndexDiscovery(
                    http, sources["daily_index_base"], sources["archive_base"], object_store=store
                ),
                "submissions": SecSubmissionsDiscovery(http, sources["data_api_base"]),
                "http": http,
                "data_api_base": sources["data_api_base"],
                "orchestrator": IngestionOrchestrator(
                    control,
                    downloader=BulkArtifactDownloader(settings, http, store),
                    loader=loader,
                    object_store=store,
                ),
                "ingestor": IncrementalFilingIngestor(
                    control,
                    FilingPackageDownloader(settings, http, store),
                    ArelleExtractor(settings),
                    writer,
                    warehouse,
                    settings=settings,
                    object_store=store,
                    mapper=mapper,
                    ensure_sec_egress=http.ensure_egress,
                ),
            }
    finally:
        await asyncio.to_thread(warehouse.close)


class Worker:
    """PostgreSQL-backed worker with graceful shutdown and stage handlers."""

    def __init__(
        self,
        settings: Settings,
        control: PostgresControlRepository,
        handlers: dict[JobType, JobHandler],
        job_types: Sequence[JobType] | None = None,
    ) -> None:
        self.settings = settings
        self.control = control
        self.handlers = handlers
        self.job_types = tuple(job_types) if job_types else None
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal_name, self.stop)
        while not self._stopping.is_set():
            try:
                await asyncio.to_thread(self.settings.require_disk_reserve)
            except DiskReserveError:
                logger.warning("ingestion_paused_low_disk")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.worker_poll_seconds
                    )
                continue
            job = await self.control.claim_job(
                self.settings.worker_id,
                self.settings.worker_lock_timeout_seconds,
                self.job_types,
            )
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    continue
                continue
            await self._execute(job)

    async def _execute(self, job: JobRecord) -> None:
        handler = self.handlers.get(job.job_type)
        if handler is None:
            await self.control.fail_job(
                job.id,
                f"No handler registered for {job.job_type.value}",
                retryable=False,
                worker_id=self.settings.worker_id,
                attempt=job.attempts,
            )
            JOB_TRANSITIONS.labels(state="dead", job_type=job.job_type.value).inc()
            return
        heartbeat = asyncio.create_task(self._heartbeat(job))
        with checkpoint_lease(job):
            execution = asyncio.create_task(handler(job))
        JOBS.labels(job_type=job.job_type.value, state="running").inc()
        try:
            with STAGE_LATENCY.labels(stage="job", pipeline=job.job_type.value).time():
                done, _ = await asyncio.wait(
                    (execution, heartbeat), return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    await heartbeat
                result = await execution
            await self.control.complete_job(
                job.id, result, worker_id=self.settings.worker_id, attempt=job.attempts
            )
            JOB_TRANSITIONS.labels(state="complete", job_type=job.job_type.value).inc()
        except StateTransitionError:
            logger.warning("job_lease_lost", job_id=str(job.id))
            JOB_TRANSITIONS.labels(state="lease_lost", job_type=job.job_type.value).inc()
        except asyncio.CancelledError:
            raise
        except SecEgressBusyError as exc:
            # Another process is sending SEC traffic: defer without spending an attempt.
            logger.warning("sec_egress_busy", job_id=str(job.id), job_type=job.job_type.value)
            try:
                await self.control.fail_job(
                    job.id,
                    str(exc),
                    retryable=True,
                    defer_seconds=self.settings.sec_egress_busy_retry_seconds,
                    worker_id=self.settings.worker_id,
                    attempt=job.attempts,
                )
            except StateTransitionError:
                JOB_TRANSITIONS.labels(state="lease_lost", job_type=job.job_type.value).inc()
            else:
                JOB_TRANSITIONS.labels(state="deferred", job_type=job.job_type.value).inc()
        except Exception as exc:
            logger.exception("job_failed", job_id=str(job.id), job_type=job.job_type.value)
            retryable = is_retryable_exception(exc)
            deferral: dict[str, Any] = (
                {"retry_at": exc.retry_at} if isinstance(exc, SecRetryDeferred) else {}
            )
            try:
                await self.control.fail_job(
                    job.id,
                    str(exc),
                    retryable=retryable,
                    **deferral,
                    worker_id=self.settings.worker_id,
                    attempt=job.attempts,
                )
            except StateTransitionError:
                logger.warning("job_lease_lost", job_id=str(job.id))
                JOB_TRANSITIONS.labels(state="lease_lost", job_type=job.job_type.value).inc()
            else:
                state = "retry" if retryable and job.attempts < job.max_attempts else "dead"
                JOB_TRANSITIONS.labels(state=state, job_type=job.job_type.value).inc()
        finally:
            heartbeat.cancel()
            execution.cancel()
            await asyncio.gather(heartbeat, execution, return_exceptions=True)
            JOBS.labels(job_type=job.job_type.value, state="running").dec()

    async def _heartbeat(self, job: JobRecord) -> None:
        interval = max(5.0, self.settings.worker_lock_timeout_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self.control.heartbeat_job(job.id, self.settings.worker_id, job.attempts)


def build_handlers(
    *,
    settings: Settings | None = None,
    services: dict[str, Any] | None = None,
) -> dict[JobType, JobHandler]:
    """Construct concrete handlers and their dependencies.

    Each handler is thin: validate the versioned payload, call one application
    service, and return a small checkpoint/result object. Pipeline logic stays in
    the application services.
    """
    handlers: dict[JobType, JobHandler] = {}
    if services is None:
        raise ValueError("Worker services must be constructed before registering handlers")
    orchestrator = services.get("orchestrator")
    ingestor = services.get("ingestor")
    mapper = services.get("mapper")

    if orchestrator is not None:

        async def _download_bulk(job: JobRecord) -> dict[str, Any] | None:
            result: dict[str, Any] | None = await orchestrator.download_artifact_job(
                job.payload, job_id=job.id
            )
            return result

        async def _load_bulk(job: JobRecord) -> dict[str, Any] | None:
            result: dict[str, Any] | None = await orchestrator.run_artifact_job(
                job.payload, job_id=job.id
            )
            return result

        async def _reconcile(job: JobRecord) -> dict[str, Any] | None:
            artifact_id = str(job.payload.get("artifact_id") or "")
            expected = job.payload.get("expected_rows")
            result: dict[str, Any] | None = await orchestrator.reconcile_artifact(
                artifact_id, expected_rows=expected if isinstance(expected, dict) else None
            )
            return result

        handlers[JobType.DOWNLOAD_BULK] = _download_bulk
        handlers[JobType.LOAD_BULK] = _load_bulk
        handlers[JobType.RECONCILE_ARTIFACT] = _reconcile

    if ingestor is not None:

        async def _ingest_filing(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.domain.models import FilingRef

            filing = FilingRef.model_validate(job.payload["filing"])
            revision = int(job.payload.get("extraction_revision", 1))
            result: dict[str, Any] | None = await ingestor.ingest(
                filing, extraction_revision=revision, job_id=job.id
            )
            return result

        handlers[JobType.INGEST_FILING] = _ingest_filing

        async def _reconcile_filing(job: JobRecord) -> dict[str, Any] | None:
            result: dict[str, Any] = await ingestor.reconcile(str(job.payload["accession"]))
            return result

        handlers[JobType.RECONCILE_FILING] = _reconcile_filing

    if mapper is not None:

        async def _map_canonical(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.mapping.remap import load_published_extraction

            accession = str(job.payload["accession"])
            revision = int(job.payload["extraction_revision"])
            run_settings = settings or Settings()
            bundle = await load_published_extraction(
                services["control"], services["object_store"], run_settings, accession, revision
            )
            mapped = 0
            async for canonical in mapper.map_batches(
                bundle.facts,
                bundle.filing,
                extraction_revision=revision,
                batch_bytes=run_settings.arrow_batch_bytes,
            ):
                await asyncio.to_thread(services["warehouse"].insert_canonical_facts, canonical)
                mapped += len(canonical)
            return {
                "accession": accession,
                "extraction_revision": revision,
                "mapped": mapped,
                "unmapped": len(bundle.facts) - mapped,
            }

        handlers[JobType.MAP_CANONICAL] = _map_canonical
    if services.get("discovery") is not None:

        async def _discover(job: JobRecord) -> dict[str, Any] | None:

            from sec_xbrl_warehouse.ingestion.filing_discovery import enrich_companies

            forms = job.payload.get("forms", services["forms"])
            filed_from = _payload_date(job.payload.get("filed_from"))
            filed_to = _payload_date(job.payload.get("filed_to"))
            result: dict[str, Any] = {}
            if job.payload.get("cik") is not None:
                # One issuer from its submissions JSON: the same request yields the company.
                company, filings = await services["submissions"].discover_with_company(
                    int(job.payload["cik"]), forms=forms, filed_from=filed_from, filed_to=filed_to
                )
                await services["control"].upsert_company(company)
                result["companies_stored"] = 1
            else:
                if filed_from is None or filed_to is None:
                    raise ValueError("Daily-index discovery needs filed_from and filed_to")
                filings = await services["discovery"].discover_range(filed_from, filed_to, forms)
                if services.get("http") is not None:
                    # Daily indexes carry no company data: fetch submissions once per new CIK.
                    result["companies_stored"] = await enrich_companies(
                        services["control"],
                        services["http"],
                        ciks=[filing.cik for filing in filings],
                        as_of=filed_to,
                        data_api_base=services.get("data_api_base", "https://data.sec.gov"),
                        max_age_days=(settings or Settings()).company_refresh_days,
                    )
            for filing in filings:
                await services["control"].enqueue_job(
                    job_type=JobType.INGEST_FILING.value,
                    idempotency_key=f"filing:{filing.accession}:{CURRENT_EXTRACTION_REVISION}",
                    payload={
                        "filing": filing.model_dump(mode="json"),
                        "extraction_revision": CURRENT_EXTRACTION_REVISION,
                    },
                )
            return {"filings_enqueued": len(filings), **result}

        handlers[JobType.DISCOVER_FILINGS] = _discover
    return handlers


def _payload_date(value: Any) -> Any:
    from datetime import date

    return date.fromisoformat(str(value)) if value else None


# ClickHouse server error codes that clear on their own (load, merges, restarts).
# 164 READONLY is a permission/settings error and stays permanent.
TRANSIENT_CLICKHOUSE_CODES = frozenset(
    {
        3,  # UNEXPECTED_END_OF_FILE (connection dropped mid-read)
        32,  # ATTEMPT_TO_READ_AFTER_EOF (connection dropped mid-read)
        159,  # TIMEOUT_EXCEEDED
        202,  # TOO_MANY_SIMULTANEOUS_QUERIES
        203,  # NO_FREE_CONNECTION
        209,  # SOCKET_TIMEOUT
        210,  # NETWORK_ERROR
        236,  # ABORTED
        241,  # MEMORY_LIMIT_EXCEEDED
        242,  # TABLE_IS_READ_ONLY (replica temporarily read-only)
        244,  # UNEXPECTED_ZOOKEEPER_ERROR
        252,  # TOO_MANY_PARTS
        285,  # TOO_FEW_LIVE_REPLICAS
        319,  # UNKNOWN_STATUS_OF_INSERT
        394,  # QUERY_WAS_CANCELLED
        425,  # SYSTEM_ERROR
        999,  # KEEPER_EXCEPTION
    }
)
# S3/MinIO error codes worth retrying; others (AccessDenied, NoSuchBucket) are permanent.
TRANSIENT_S3_CODES = frozenset(
    {
        "SlowDown",
        "ServiceUnavailable",
        "InternalError",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "Throttling",
        "ThrottlingException",
        "503",
        "500",
    }
)
RETRYABLE_HTTP_STATUSES = frozenset({403, 408, 429})
_CLICKHOUSE_CODE = re.compile(r"\bCode:\s*(\d+)")


def is_retryable_exception(exc: BaseException) -> bool:
    """Classify network, timeout, and temporary storage failures as retryable.

    Wrapped errors (for example a `WarehouseWriteError` raised from a ClickHouse
    or S3 error) are classified by their `__cause__`. Integrity, schema drift,
    ClickHouse syntax/missing-table, and S3 permission/bucket errors stay permanent.
    """
    if isinstance(exc, (ArtifactIntegrityError, SchemaDriftError)):
        return False
    if isinstance(
        exc,
        (
            SecEgressBusyError,
            DiskReserveError,
            SecRetryDeferred,
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.TransportError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        # SEC throttling answers 403; 408/429/5xx are transient; other 4xx aren't.
        status = exc.response.status_code
        return status in RETRYABLE_HTTP_STATUSES or status >= 500
    if _is_transient_storage_error(exc):
        return True
    if exc.__cause__ is not None:
        return is_retryable_exception(exc.__cause__)
    # PostgreSQL transient error classes surface through SQLAlchemy.
    module = type(exc).__module__
    if module.startswith("sqlalchemy"):
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("timeout", "connection", "locked", "deadlock", "serialization")
        )
    return False


def _is_transient_storage_error(exc: BaseException) -> bool:
    module = type(exc).__module__
    if module.startswith("clickhouse_connect"):
        from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

        if isinstance(exc, OperationalError):
            return True  # connection refused/reset, HTTP timeouts
        if isinstance(exc, DatabaseError):
            match = _CLICKHOUSE_CODE.search(str(exc))
            return match is not None and int(match.group(1)) in TRANSIENT_CLICKHOUSE_CODES
        return False
    if module.startswith(("botocore", "boto3")):
        from botocore.exceptions import (
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        if isinstance(
            exc,
            (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError, ConnectTimeoutError),
        ):
            return True
        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            return str(error.get("Code")) in TRANSIENT_S3_CODES or status in {500, 503}
    return False
