from __future__ import annotations

import asyncio
import contextlib
import re
import signal
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from typing import Any

import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobState, JobType
from sec_xbrl_warehouse.domain.models import JobRecord
from sec_xbrl_warehouse.domain.sources import SEC_SOURCE
from sec_xbrl_warehouse.domain.xbrl import CURRENT_EXTRACTION_REVISION
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    ConfigurationError,
    DiskReserveError,
    SchemaDriftError,
    SourceRateLimited,
    SourceRetryDeferred,
    StateTransitionError,
    UnsupportedSourceError,
)
from sec_xbrl_warehouse.infrastructure.http.egress_lock import (
    EgressBusyError,
    SecEgressBusyError,
)
from sec_xbrl_warehouse.infrastructure.storage.postgres import (
    PostgresControlRepository,
    checkpoint_lease,
)
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.observability.metrics import JOB_TRANSITIONS, JOBS, STAGE_LATENCY

logger = get_logger(__name__)
JobHandler = Callable[[JobRecord], Coroutine[Any, Any, dict[str, Any] | None]]


# Job types that send a filing source's traffic (its client and egress lock).
SOURCE_JOB_TYPES = frozenset(
    {
        JobType.FETCH_FILING,
        JobType.INGEST_FILING,
        JobType.DISCOVER_FILINGS,
    }
)
# Job types that send SEC traffic: the filing jobs plus the SEC-only bulk download.
SEC_JOB_TYPES = SOURCE_JOB_TYPES | {JobType.DOWNLOAD_BULK}
# Job types whose completion queues a `rebuild_series` job for the company.
SERIES_SOURCE_JOB_TYPES = frozenset(
    {JobType.INGEST_FILING, JobType.PARSE_FILING, JobType.MAP_CANONICAL}
)
# Job types that run Arelle (online Arelle may resolve taxonomies from SEC hosts).
EXTRACTION_JOB_TYPES = frozenset({JobType.INGEST_FILING, JobType.PARSE_FILING})


def requires_network(job_types: Sequence[JobType] | None, source: str) -> bool:
    """Whether a worker claiming `job_types` (None: every type) needs `source`'s client."""
    network = SEC_JOB_TYPES if source == SEC_SOURCE else SOURCE_JOB_TYPES
    return job_types is None or bool(network.intersection(job_types))


def requires_sec(job_types: Sequence[JobType] | None) -> bool:
    """Whether a worker claiming `job_types` (None: every type) needs an SEC client."""
    return requires_network(job_types, SEC_SOURCE)


@contextlib.asynccontextmanager
async def runtime_services(
    settings: Settings,
    control: PostgresControlRepository,
    job_types: Sequence[JobType] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Own the network clients used by the worker's job types.

    The source registry holds one adapter per `XBRL_ENABLED_SOURCES` entry. A
    source's client (and its egress lock, taken on the first request) is built only
    when the worker's job types need it (`requires_network`); the SEC client also
    serves the SEC-only bulk download. A worker without the SEC client that may run
    Arelle in online mode (`XBRL_ARELLE_OFFLINE=false`) still takes the SEC egress
    lock lazily, before its first extraction; with offline Arelle it never touches it.
    Inbox sources (`nse`, `mca`, `acra`) read operator files from `XBRL_INBOX_DIR` and
    get no client or lock.
    """
    from sec_xbrl_warehouse.config_files import load_yaml
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import (
        LazyEgress,
        SecEgressLock,
        SourceEgressLock,
    )
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
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
    from sec_xbrl_warehouse.sources.base import SourceAdapter
    from sec_xbrl_warehouse.sources.dart import DART_SOURCE, DartSourceAdapter
    from sec_xbrl_warehouse.sources.edinet import EDINET_SOURCE, EdinetSourceAdapter
    from sec_xbrl_warehouse.sources.esef import ESEF_SOURCE, EsefSourceAdapter
    from sec_xbrl_warehouse.sources.inbox import INBOX_PROFILES, InboxSourceAdapter
    from sec_xbrl_warehouse.sources.keyed import required_key
    from sec_xbrl_warehouse.sources.registry import SourceRegistry, source_policies
    from sec_xbrl_warehouse.sources.sec import SecSourceAdapter

    # Each source plan adds its adapter (with a `SourceHttpClient` and
    # `SourceEgressLock` when `requires_network`) here. Inbox sources (operator files
    # on local disk) have no policy, client, or lock.
    policies = source_policies(settings)
    unimplemented = [
        code
        for code in settings.enabled_sources
        if code not in policies and code not in INBOX_PROFILES
    ]
    if unimplemented:
        raise ConfigurationError(f"No adapter is implemented for enabled sources {unimplemented}")
    # EDINET and DART need the operator's own API key; a worker that would send their
    # requests refuses to start without it (parse-only workers never send any).
    keyed: tuple[tuple[str, Any, Any, str], ...] = (
        (EDINET_SOURCE, EdinetSourceAdapter, settings.edinet_api_key, "EDINET"),
        (DART_SOURCE, DartSourceAdapter, settings.dart_api_key, "DART"),
    )
    for code, _, api_key, name in keyed:
        if code in settings.enabled_sources and requires_network(job_types, code):
            required_key(api_key, name, f"XBRL_{name}_API_KEY")
    settings.ensure_directories()
    sources = load_yaml(settings.config_dir / "sources.yml")["sec"]
    store = create_object_store(settings)
    await asyncio.to_thread(store.ensure_bucket)
    control.object_store = store  # parse jobs load stored manifests
    warehouse = await asyncio.to_thread(ClickHouseWarehouse, settings)
    extractor = ArelleExtractor(settings)
    lazy_egress: LazyEgress | None = None
    try:
        async with contextlib.AsyncExitStack() as stack:
            http: SecHttpClient | None = None
            ensure_sec_egress = None
            if SEC_SOURCE in settings.enabled_sources and requires_sec(job_types):
                http = await stack.enter_async_context(
                    SecHttpClient(settings, egress_lock=SecEgressLock(settings))
                )
                ensure_sec_egress = http.ensure_egress
            elif not settings.arelle_offline and (
                job_types is None or EXTRACTION_JOB_TYPES.intersection(job_types)
            ):
                lazy_egress = LazyEgress(SecEgressLock(settings))
                ensure_sec_egress = lazy_egress.ensure
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
            # Without `http` this only materializes and archives stored packages.
            packages = FilingPackageDownloader(settings, http, store)
            adapters: list[SourceAdapter] = []
            if SEC_SOURCE in settings.enabled_sources:
                adapters.append(
                    SecSourceAdapter(
                        packages,
                        submissions=(
                            SecSubmissionsDiscovery(http, sources["data_api_base"])
                            if http is not None
                            else None
                        ),
                        daily_index=(
                            SecDailyIndexDiscovery(
                                http,
                                sources["daily_index_base"],
                                sources["archive_base"],
                                object_store=store,
                            )
                            if http is not None
                            else None
                        ),
                        http=http,
                        companies=_MirroredCompanies(control, warehouse),
                        data_api_base=sources["data_api_base"],
                        company_refresh_days=settings.company_refresh_days,
                    )
                )
            if ESEF_SOURCE in settings.enabled_sources:
                esef_http: SourceHttpClient | None = None
                if requires_network(job_types, ESEF_SOURCE):
                    # filings.xbrl.org gets the same operator identity as the SEC.
                    settings.assert_sec_identity()
                    esef_http = await stack.enter_async_context(
                        SourceHttpClient(
                            settings,
                            policies[ESEF_SOURCE],
                            egress_lock=SourceEgressLock(settings, source=ESEF_SOURCE),
                        )
                    )
                adapters.append(EsefSourceAdapter(settings, esef_http, store, control))
            for code, adapter_type, _, _ in keyed:
                if code not in settings.enabled_sources:
                    continue
                keyed_http: SourceHttpClient | None = None
                if requires_network(job_types, code):
                    # The regulators get the same operator identity as the SEC.
                    settings.assert_sec_identity()
                    keyed_http = await stack.enter_async_context(
                        SourceHttpClient(
                            settings,
                            policies[code],
                            egress_lock=SourceEgressLock(settings, source=code),
                        )
                    )
                adapters.append(adapter_type(settings, keyed_http, store, control))
            for code, profile in INBOX_PROFILES.items():
                if code in settings.enabled_sources:
                    adapters.append(InboxSourceAdapter(settings, store, control, profile))
            registry = SourceRegistry(adapters, packages=packages)
            services: dict[str, Any] = {
                "control": control,
                "warehouse": warehouse,
                "object_store": store,
                "mapper": mapper,
                "sources": registry,
                "forms": sources["default_forms"],
                "data_api_base": sources["data_api_base"],
                "orchestrator": IngestionOrchestrator(
                    control,
                    downloader=(
                        BulkArtifactDownloader(settings, http, store) if http is not None else None
                    ),
                    loader=loader,
                    object_store=store,
                ),
                "ingestor": IncrementalFilingIngestor(
                    control,
                    registry,
                    extractor,
                    writer,
                    warehouse,
                    settings=settings,
                    object_store=store,
                    mapper=mapper,
                    ensure_sec_egress=ensure_sec_egress,
                    fiscal_focus=registry.fiscal_focus,
                ),
            }
            yield services
    finally:
        if lazy_egress is not None:
            await lazy_egress.close()
        await asyncio.to_thread(extractor.close)
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
        if self.job_types and SERIES_SOURCE_JOB_TYPES.intersection(self.job_types):
            # Jobs that queue a series rebuild bring their follow-up job type along,
            # so `--job-type parse_filing` workers also run the rebuilds.
            self.job_types = tuple(dict.fromkeys((*self.job_types, JobType.REBUILD_SERIES)))
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal_name, self.stop)
        # Up to `worker_concurrency` jobs run at once, each with its own heartbeat,
        # lease, and checkpoint ownership; a job is claimed only when a slot is free.
        running: set[asyncio.Task[None]] = set()
        try:
            while not self._stopping.is_set():
                if len(running) >= self.settings.worker_concurrency:
                    await self._wait(running)
                    continue
                try:
                    await asyncio.to_thread(self.settings.require_disk_reserve)
                except DiskReserveError:
                    logger.warning("ingestion_paused_low_disk")
                    await self._wait(running, self.settings.worker_poll_seconds)
                    continue
                job = await self.control.claim_job(
                    self.settings.worker_id,
                    self.settings.worker_lock_timeout_seconds,
                    self.job_types,
                )
                if job is None:
                    await self._wait(running, self.settings.worker_poll_seconds)
                    continue
                task = asyncio.create_task(self._execute(job))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            # Graceful stop: running jobs finish (or checkpoint) before the worker exits.
            if running:
                await asyncio.gather(*running, return_exceptions=True)

    async def _wait(
        self, running: set[asyncio.Task[None]], poll_seconds: float | None = None
    ) -> None:
        """Until a running job finishes, the worker is stopped, or `poll_seconds` pass."""
        stopping = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait(
                {stopping, *running}, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

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
        except EgressBusyError as exc:
            # Another process is sending this source's traffic: defer without spending
            # an attempt.
            logger.warning(
                "sec_egress_busy" if isinstance(exc, SecEgressBusyError) else "egress_busy",
                job_id=str(job.id),
                job_type=job.job_type.value,
            )
            try:
                deferred_state = await self.control.fail_job(
                    job.id,
                    str(exc),
                    retryable=True,
                    defer_seconds=self.settings.sec_egress_busy_retry_seconds,
                    max_defer_seconds=self.settings.sec_egress_max_defer_seconds,
                    worker_id=self.settings.worker_id,
                    attempt=job.attempts,
                )
            except StateTransitionError:
                JOB_TRANSITIONS.labels(state="lease_lost", job_type=job.job_type.value).inc()
            else:
                outcome = "dead" if deferred_state == JobState.DEAD else "deferred"
                JOB_TRANSITIONS.labels(state=outcome, job_type=job.job_type.value).inc()
        except Exception as exc:
            logger.exception("job_failed", job_id=str(job.id), job_type=job.job_type.value)
            retryable = is_retryable_exception(exc)
            deferral: dict[str, Any] = (
                {"retry_at": exc.retry_at} if isinstance(exc, SourceRetryDeferred) else {}
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
            if result is not None and result.get("status") == "complete":
                result = {**result, "series": await _queue_series(services, settings, filing.cik)}
            return result

        handlers[JobType.INGEST_FILING] = _ingest_filing

        async def _fetch_filing(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.domain.models import FilingRef

            filing = FilingRef.model_validate(job.payload["filing"])
            revision = int(job.payload.get("extraction_revision", CURRENT_EXTRACTION_REVISION))
            manifest = await ingestor.fetch(filing, job_id=job.id)
            parse_id = await services["control"].enqueue_job(
                job_type=JobType.PARSE_FILING.value,
                idempotency_key=f"parse:{filing.accession}:{revision}",
                payload={"accession": filing.accession, "extraction_revision": revision},
            )
            return {
                "accession": filing.accession,
                "package_object_key": manifest.package_object_key,
                "parse_job_id": str(parse_id) if parse_id is not None else None,
            }

        handlers[JobType.FETCH_FILING] = _fetch_filing

        async def _parse_filing(job: JobRecord) -> dict[str, Any] | None:
            accession = str(job.payload["accession"])
            revision = int(job.payload.get("extraction_revision", CURRENT_EXTRACTION_REVISION))
            result: dict[str, Any] | None = await ingestor.parse(
                accession, extraction_revision=revision, job_id=job.id
            )
            if result is not None and result.get("status") == "complete":
                cik = result.get("cik") or await _filing_cik(services, accession)
                if cik is not None:
                    result = {
                        **result,
                        "series": await _queue_series(services, settings, int(cik)),
                    }
            return result

        handlers[JobType.PARSE_FILING] = _parse_filing

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
            registry = services.get("sources")
            bundle = await load_published_extraction(
                services["control"],
                services["object_store"],
                run_settings,
                accession,
                revision,
                # Same per-source fiscal focus as parse (SEC: dei facts).
                fiscal_focus=registry.fiscal_focus if registry is not None else None,
            )
            mapped = 0
            async for canonical in mapper.map_batches(
                bundle.facts,
                bundle.filing,  # fiscal fields come from the extraction's dei facts
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
                "series": await _queue_series(services, settings, bundle.filing.cik),
            }

        handlers[JobType.MAP_CANONICAL] = _map_canonical

    async def _rebuild_series_job(job: JobRecord) -> dict[str, Any] | None:
        cik = int(job.payload["cik"])
        result = await _rebuild_series(services, cik)
        if result["status"] == "failed":
            # A queued rebuild is retried like any job (the inline one was not).
            raise RuntimeError(f"Series rebuild failed for CIK {cik}: {result['error']}")
        return {"cik": cik, **result}

    handlers[JobType.REBUILD_SERIES] = _rebuild_series_job
    if services.get("sources") is not None or services.get("discovery") is not None:

        async def _discover(job: JobRecord) -> dict[str, Any] | None:
            from sec_xbrl_warehouse.domain.sources import filing_source, is_source_code
            from sec_xbrl_warehouse.sources.base import DiscoveryRequest

            # Only a missing key means SEC; an empty or malformed value never falls back.
            source = job.payload.get("source", SEC_SOURCE)
            if not is_source_code(source):
                raise UnsupportedSourceError(f"Invalid discovery source: {source!r}")
            # An unknown or disabled source raises `UnsupportedSourceError` (never retried).
            adapter = _discovery_adapter(services, settings, source)
            forms = job.payload.get("forms")
            if forms is None:
                # The configured default forms are SEC form types.
                forms = services.get("forms", ()) if source == SEC_SOURCE else ()
            entity = job.payload.get("entity")
            if entity is None and job.payload.get("cik") is not None:
                entity = int(job.payload["cik"])  # legacy SEC payload
            found = await adapter.discover(
                DiscoveryRequest(
                    filed_from=_payload_date(job.payload.get("filed_from")),
                    filed_to=_payload_date(job.payload.get("filed_to")),
                    entity=str(entity) if entity is not None else None,
                    forms=tuple(str(form) for form in forms),
                )
            )
            foreign = [
                filing.accession
                for filing in found.filings
                if filing_source(filing.accession) != source
            ]
            if foreign:
                raise ValueError(f"{source} discovery returned other sources' filings: {foreign}")
            filings = found.filings
            result: dict[str, Any] = {}
            if found.companies:
                companies = _MirroredCompanies(services["control"], services.get("warehouse"))
                for company in found.companies:
                    await companies.upsert_company(company)
            if found.companies or found.stored_companies is not None:
                result["companies_stored"] = len(found.companies) + (found.stored_companies or 0)
            for filing in filings:
                await services["control"].enqueue_job(
                    job_type=JobType.FETCH_FILING.value,
                    idempotency_key=f"fetch:{filing.accession}:{CURRENT_EXTRACTION_REVISION}",
                    payload={
                        "filing": filing.model_dump(mode="json"),
                        "extraction_revision": CURRENT_EXTRACTION_REVISION,
                    },
                )
            return {"filings_enqueued": len(filings), **result}

        handlers[JobType.DISCOVER_FILINGS] = _discover
    return handlers


def _discovery_adapter(services: dict[str, Any], settings: Settings | None, source: str) -> Any:
    """The source's adapter from the registry, or an SEC adapter over legacy services.

    Services without a `sources` registry (older callers and test doubles) carry
    `discovery`, `submissions`, and `http` directly; they serve SEC discovery only.
    """
    registry = services.get("sources")
    if registry is not None:
        return registry.adapter(source)
    if source != SEC_SOURCE:
        raise UnsupportedSourceError(f"Source {source!r} is not enabled here (enabled: sec)")
    from sec_xbrl_warehouse.sources.sec import SecSourceAdapter

    http = services.get("http")
    return SecSourceAdapter(
        submissions=services.get("submissions"),
        daily_index=services.get("discovery"),
        http=http,
        companies=(
            _MirroredCompanies(services["control"], services.get("warehouse"))
            if services.get("control") is not None
            else None
        ),
        data_api_base=services.get("data_api_base", "https://data.sec.gov"),
        company_refresh_days=(
            (settings or Settings()).company_refresh_days if http is not None else None
        ),
    )


class _MirroredCompanies:
    """Company writes go to PostgreSQL and, when available, the ClickHouse mirror."""

    def __init__(self, control: Any, warehouse: Any) -> None:
        self.control = control
        self.warehouse = warehouse

    async def known_company_ciks(self, ciks: Any) -> Any:
        return await self.control.known_company_ciks(ciks)

    async def stale_company_ciks(self, ciks: Any, fetched_before: Any) -> Any:
        return await self.control.stale_company_ciks(ciks, fetched_before)

    async def upsert_company(self, company: Any) -> None:
        await self.control.upsert_company(company)
        if self.warehouse is not None and hasattr(self.warehouse, "upsert_company_mirror"):
            await asyncio.to_thread(self.warehouse.upsert_company_mirror, company)


async def _queue_series(
    services: dict[str, Any], settings: Settings | None, cik: int
) -> dict[str, Any]:
    """Queue the company's coalesced `rebuild_series` job, or rebuild inline.

    Control repositories without `enqueue_series_rebuild` (test doubles) rebuild
    inline as before.
    """
    enqueue = getattr(services.get("control"), "enqueue_series_rebuild", None)
    if enqueue is None:
        return await _rebuild_series(services, cik)
    delay = (settings or Settings()).series_rebuild_delay_seconds
    try:
        job_id = await enqueue(cik, delay)
    except Exception as exc:
        logger.exception("series_rebuild_enqueue_failed", cik=cik)
        return {"status": "failed", "error": str(exc)[:2000]}
    return {"status": "queued", "job_id": str(job_id) if job_id is not None else None}


async def _rebuild_series(services: dict[str, Any], cik: int) -> dict[str, Any]:
    """Rebuild a company's metric series after new canonical facts.

    Derived data: a failure is logged and reported in the job result instead of
    failing (and re-running) the ingest. `series rebuild --cik` repairs it.
    """
    warehouse = services.get("warehouse")
    mapper = services.get("mapper")
    if warehouse is None or not hasattr(warehouse, "insert_metric_series"):
        return {"status": "skipped"}
    from sec_xbrl_warehouse.query.series import (
        formulas_from_metrics,
        non_additive_from_metrics,
        rebuild_metric_series,
    )

    try:
        written = await asyncio.to_thread(
            rebuild_metric_series,
            warehouse,
            cik,
            formulas=formulas_from_metrics(mapper.metrics) if mapper is not None else None,
            non_additive=non_additive_from_metrics(mapper.metrics) if mapper is not None else None,
            priorities=getattr(mapper, "candidate_priorities", None),
        )
    except Exception as exc:
        logger.exception("series_rebuild_failed", cik=cik)
        return {"status": "failed", "error": str(exc)[:2000]}
    return {"status": "rebuilt", "rows": written}


async def _filing_cik(services: dict[str, Any], accession: str) -> int | None:
    """The filing's CIK for the series rebuild (from its stored manifest)."""
    getter = getattr(services.get("control"), "get_filing_manifest", None)
    if getter is None:
        return None
    try:
        manifest = await getter(accession)
    except Exception:
        logger.exception("series_rebuild_cik_lookup_failed", accession=accession)
        return None
    return manifest.filing.cik if manifest is not None else None


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
    if isinstance(exc, (ArtifactIntegrityError, SchemaDriftError, UnsupportedSourceError)):
        return False
    if isinstance(
        exc,
        (
            EgressBusyError,
            DiskReserveError,
            SourceRateLimited,
            SourceRetryDeferred,
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
