from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, cast

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from sec_xbrl_warehouse.config import Settings, get_settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import DatasetKind, JobState, JobType
from sec_xbrl_warehouse.domain.models import (
    BackfillPlan,
    DatasetArtifact,
    FilingRef,
)
from sec_xbrl_warehouse.domain.ports import ControlRepository
from sec_xbrl_warehouse.domain.xbrl import CURRENT_EXTRACTION_REVISION
from sec_xbrl_warehouse.exceptions import StateTransitionError
from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressLock
from sec_xbrl_warehouse.infrastructure.http.sec_client import (
    SecHttpClient,
    build_archive_directory_url,
)
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.hashing import stable_json_hash
from sec_xbrl_warehouse.infrastructure.storage.object_store import create_object_store
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
    TaxonomyPackageStatus,
    package_status,
    sync_taxonomy_packages,
)
from sec_xbrl_warehouse.ingestion.bulk_catalog import BulkCatalogService, filter_artifacts_by_year
from sec_xbrl_warehouse.ingestion.orchestrator import IngestionOrchestrator
from sec_xbrl_warehouse.logging import configure_logging
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository
from sec_xbrl_warehouse.worker import Worker, build_handlers, runtime_services

app = typer.Typer(help="Build and operate a local SEC XBRL warehouse.", no_args_is_help=True)
db_app = typer.Typer(help="Database schema and diagnostics.")
catalog_app = typer.Typer(help="SEC bulk dataset catalog.")
backfill_app = typer.Typer(help="Historical SEC bulk backfill.")
api_app = typer.Typer(help="HTTP query API.")
worker_app = typer.Typer(help="PostgreSQL job worker.")
filing_app = typer.Typer(help="Incremental filing discovery and ingestion.")
mapping_app = typer.Typer(help="Canonical metric and mapping review workflows.")
taxonomy_app = typer.Typer(help="Pinned local taxonomy packages for Arelle.")
company_app = typer.Typer(help="Companies from SEC submissions data.")
jobs_app = typer.Typer(help="Inspect and recover PostgreSQL queue jobs.")
series_app = typer.Typer(help="Standardized metric series.")
app.add_typer(db_app, name="db")
app.add_typer(catalog_app, name="catalog")
app.add_typer(backfill_app, name="backfill")
app.add_typer(api_app, name="api")
app.add_typer(worker_app, name="worker")
app.add_typer(filing_app, name="filing")
app.add_typer(mapping_app, name="mapping")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(company_app, name="company")
app.add_typer(jobs_app, name="jobs")
app.add_typer(series_app, name="series")
console = Console()


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.env != "development")


def _taxonomy_package_check(settings: Settings) -> tuple[str, bool, str]:
    """Every enabled package verified; production must parse offline."""
    try:
        statuses = package_status(settings)
    except Exception as exc:
        return ("Taxonomy packages", False, str(exc))
    problems = [
        f"{item.package_id}={item.state}"
        for item in statuses
        if item.state not in {"ok", "disabled"}
    ]
    if settings.env == "production" and not settings.arelle_offline:
        problems.append("XBRL_ARELLE_OFFLINE must be true in production")
    ready = sum(item.state == "ok" for item in statuses)
    details = "; ".join(problems) if problems else f"{ready} verified"
    if problems and any(item.state == "missing" for item in statuses):
        details += " (run `xbrl-warehouse taxonomy sync`)"
    return ("Taxonomy packages", not problems, details)


@app.command()
def doctor(skip_sec_identity: bool = False) -> None:
    """Check configuration, directories, and local service connectivity."""
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []
    if skip_sec_identity:
        checks.append(("SEC identity", True, "skipped by --skip-sec-identity"))
    else:
        try:
            settings.assert_sec_identity()
            checks.append(("SEC identity", True, settings.sec_user_agent))
        except Exception as exc:
            checks.append(("SEC identity", False, str(exc)))

    required_configs = (
        "sources.yml",
        "logging.yml",
        "canonical_metrics.yml",
        "concept_seed.yml",
        "validation.yml",
        "taxonomy_packages.yml",
    )
    try:
        for name in required_configs:
            load_yaml(settings.config_dir / name)
        checks.append(("Configuration files", True, f"loaded {len(required_configs)} YAML files"))
    except Exception as exc:
        checks.append(("Configuration files", False, str(exc)))

    try:
        settings.require_disk_reserve()
        checks.append(("Disk reserve", True, "data and scratch reserves available"))
    except Exception as exc:
        checks.append(("Disk reserve", False, str(exc)))
    data_ready = settings.data_dir.is_dir() and os.access(settings.data_dir, os.W_OK)
    free_gib = shutil.disk_usage(settings.data_dir).free / 1024**3 if data_ready else 0
    checks.append(
        (
            "Data directory",
            data_ready,
            f"{settings.data_dir.resolve()} ({free_gib:.1f} GiB free)"
            if data_ready
            else f"missing or not writable: {settings.data_dir.resolve()}",
        )
    )
    checks.append(
        (
            "Arelle directories",
            all(
                path.is_dir() and os.access(path, os.W_OK)
                for path in (settings.arelle_cache_dir, settings.arelle_taxonomy_packages_dir)
            ),
            f"cache={settings.arelle_cache_dir.resolve()}, packages={settings.arelle_taxonomy_packages_dir.resolve()}",
        )
    )

    checks.append(_taxonomy_package_check(settings))

    warehouse: ClickHouseWarehouse | None = None
    try:
        warehouse = ClickHouseWarehouse(settings)
        ddl = Path("sql/clickhouse")
        definitions = warehouse._table_definitions(ddl)
        current = {
            str(row["name"])
            for row in warehouse.query(
                "SELECT name FROM system.tables WHERE database = %(database)s",
                {"database": settings.clickhouse_database},
            )
        }
        expected = set(definitions) | {
            "v_sec_numeric_latest",
            "v_sec_text_latest",
            "v_xbrl_facts_latest",
            "v_canonical_facts_latest",
            "v_statement_lines_latest",
        }
        missing = sorted(expected - current)
        drift = warehouse.schema_drift(ddl)
        clickhouse_ok = warehouse.ping() and not missing and not drift
        clickhouse_details = (
            f"{settings.clickhouse_host}:{settings.clickhouse_port}/{settings.clickhouse_database}; "
            f"missing={missing or 'none'}; incompatible={list(drift) or 'none'}"
        )
    except Exception as exc:
        clickhouse_ok = False
        clickhouse_details = str(exc)
    finally:
        if warehouse is not None:
            warehouse.close()
    checks.append(("ClickHouse", clickhouse_ok, clickhouse_details))

    async def check_postgres() -> tuple[bool, str]:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from sqlalchemy import text

        control = PostgresControlRepository(settings)
        try:
            async with control.engine.connect() as connection:
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            return version == expected, f"revision={version or 'missing'}, expected={expected}"
        finally:
            await control.close()

    try:
        postgres_ok, postgres_details = asyncio.run(check_postgres())
    except Exception as exc:
        postgres_ok, postgres_details = False, str(exc)
    checks.append(("PostgreSQL", postgres_ok, postgres_details))
    try:
        store = create_object_store(settings)
        store.ensure_bucket()
        checks.append(("Object storage", True, store.bucket))
    except Exception as exc:
        checks.append(("Object storage", False, str(exc)))

    table = Table("Check", "Status", "Details")
    for name, ok, details in checks:
        table.add_row(name, "PASS" if ok else "FAIL", details)
    console.print(table)
    if not all(item[1] for item in checks):
        raise typer.Exit(code=1)


@db_app.command("init-clickhouse")
def init_clickhouse(
    ddl_directory: Annotated[Path, typer.Option()] = Path("sql/clickhouse"),
) -> None:
    settings = get_settings()
    warehouse = ClickHouseWarehouse(settings)
    try:
        applied = warehouse.apply_ddl(ddl_directory)
        for name in applied:
            console.print(f"Applied {name}")
    finally:
        warehouse.close()


@db_app.command("upgrade-clickhouse")
def upgrade_clickhouse(
    yes: bool = typer.Option(False, "--yes", help="Copy and atomically swap incompatible tables."),
    ddl_directory: Annotated[Path, typer.Option()] = Path("sql/clickhouse"),
) -> None:
    """Preserve old tables as named backups while applying new sort keys."""
    if not yes:
        raise typer.BadParameter(
            "Pass --yes after reviewing the backup and disk-space requirements"
        )
    warehouse = ClickHouseWarehouse(get_settings())
    try:
        backups = warehouse.upgrade_ddl(ddl_directory)
        if not backups:
            console.print("ClickHouse schema is current")
        for table, backup in backups.items():
            console.print(f"Upgraded {table}; retained {backup}")
    finally:
        warehouse.close()


@catalog_app.command("sync")
def catalog_sync(
    dataset: DatasetKind = DatasetKind.NOTES,
    output: Path | None = None,
) -> None:
    artifacts = asyncio.run(_sync_catalog(dataset))
    destination = output or Path(f"data/catalog/{dataset.value}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps([item.model_dump(mode="json") for item in artifacts], indent=2),
        encoding="utf-8",
    )
    console.print(f"Discovered {len(artifacts)} artifacts; wrote {destination}")


async def _sync_catalog(dataset: DatasetKind) -> tuple[DatasetArtifact, ...]:
    settings = get_settings()
    sources = load_yaml(settings.config_dir / "sources.yml")
    raw_urls = sources["sec"]["catalog_pages"]
    urls = {DatasetKind(key): value for key, value in raw_urls.items()}
    async with SecHttpClient(settings, egress_lock=SecEgressLock(settings)) as http:
        return await BulkCatalogService(http, urls).sync(dataset)


@backfill_app.command("plan")
def backfill_plan(
    dataset: DatasetKind = DatasetKind.NOTES,
    from_year: int = typer.Option(..., min=2009),
    to_year: int = typer.Option(..., min=2009),
    catalog_file: Path | None = None,
    output: Path = Path("data/plans/backfill.json"),
) -> None:
    settings = get_settings()
    if catalog_file and catalog_file.is_file():
        payload = json.loads(catalog_file.read_text(encoding="utf-8"))
        artifacts = tuple(DatasetArtifact.model_validate(item) for item in payload)
    else:
        artifacts = asyncio.run(_sync_catalog(dataset))
    selected = filter_artifacts_by_year(artifacts, from_year, to_year)
    sources = load_yaml(settings.config_dir / "sources.yml")
    control = PostgresControlRepository(settings)
    try:
        plan = IngestionOrchestrator(cast(ControlRepository, control)).build_backfill_plan(
            dataset=dataset,
            artifacts=selected,
            from_year=from_year,
            to_year=to_year,
            catalog_url=sources["sec"]["catalog_pages"][dataset.value],
        )
    finally:
        asyncio.run(control.close())
    IngestionOrchestrator.write_plan(plan, output)
    console.print(f"Planned {len(plan.artifacts)} artifacts; wrote {output}")


@backfill_app.command("enqueue")
def backfill_enqueue(
    plan_file: Path,
    refresh_unversioned: bool = typer.Option(
        False, help="Recheck sources with no ETag, Last-Modified, or checksum."
    ),
) -> None:
    plan = BackfillPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    asyncio.run(_enqueue_plan(plan, refresh_unversioned=refresh_unversioned))


async def _enqueue_plan(plan: BackfillPlan, *, refresh_unversioned: bool = False) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        ids = await IngestionOrchestrator(cast(ControlRepository, control)).enqueue_backfill(
            plan, refresh_unversioned=refresh_unversioned
        )
        console.print(f"Enqueued {len(ids)} jobs")
    finally:
        await control.close()


@api_app.command("serve")
def api_serve(
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
) -> None:
    settings = get_settings()
    uvicorn.run(
        "sec_xbrl_warehouse.api.app:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
    )


@worker_app.command("run")
def worker_run(
    job_type: Annotated[
        list[JobType] | None,
        typer.Option(help="Claim only this job type; repeat for several. Default: all."),
    ] = None,
) -> None:
    asyncio.run(_run_worker(job_type))


async def _run_worker(job_types: list[JobType] | None = None) -> None:
    from prometheus_client import REGISTRY, start_http_server

    from sec_xbrl_warehouse.observability.metrics import QueueCollector, snapshot_on_loop

    settings = get_settings()
    metrics_server, _ = start_http_server(
        settings.worker_metrics_port, addr=settings.worker_metrics_host
    )
    control = PostgresControlRepository(settings)
    # Scrapes run in the metrics server thread and reuse the worker's pool on its loop.
    collector = QueueCollector(
        snapshot_on_loop(lambda: control.queue_snapshot(), asyncio.get_running_loop(), timeout=5.0)
    )
    REGISTRY.register(collector)
    try:
        async with runtime_services(settings, control) as services:
            await Worker(settings, control, build_handlers(services=services), job_types).run()
    finally:
        REGISTRY.unregister(collector)
        await control.close()
        metrics_server.shutdown()
        metrics_server.server_close()


@filing_app.command("enqueue")
def filing_enqueue(
    accession: str = typer.Option(...),
    cik: int = typer.Option(..., min=0),
    company_name: str = typer.Option(...),
    form: str = typer.Option(...),
    filed_date: str = typer.Option(..., help="Filing date in YYYY-MM-DD format."),
    primary_document: str | None = typer.Option(None),
    instance_document: str | None = typer.Option(None),
    extraction_revision: int = typer.Option(CURRENT_EXTRACTION_REVISION, min=1),
) -> None:
    """Queue one accession without guessing issuer identity from its number."""
    parsed_filed_date = _parse_cli_date(filed_date, "filed-date")
    assert parsed_filed_date is not None
    archive = build_archive_directory_url(cik, accession)
    filing = FilingRef(
        accession=accession,
        cik=cik,
        company_name=company_name,
        form=form,
        filed_date=parsed_filed_date,
        primary_document=primary_document,
        instance_document=instance_document,
        archive_directory_url=archive,
        primary_document_url=f"{archive}/{primary_document}" if primary_document else None,
        is_amendment=form.endswith("/A"),
    )
    asyncio.run(_enqueue_filing(filing, extraction_revision))


async def _enqueue_filing(filing: FilingRef, revision: int) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        job_id = await control.enqueue_job(
            job_type=JobType.INGEST_FILING.value,
            idempotency_key=f"filing:{filing.accession}:{revision}",
            payload={
                "filing": filing.model_dump(mode="json"),
                "extraction_revision": revision,
            },
            max_attempts=settings.worker_max_attempts,
        )
        console.print(f"Enqueued filing job {job_id}")
    finally:
        await control.close()


@filing_app.command("discover")
def filing_discover(
    filed_from: str | None = typer.Option(None, help="First filing date in YYYY-MM-DD format."),
    filed_to: str | None = typer.Option(None, help="Last filing date in YYYY-MM-DD format."),
    forms: str | None = typer.Option(None, help="Comma-separated forms; defaults to sources.yml."),
    refresh: bool = typer.Option(False, help="Queue another scan of the same range."),
    cik: int | None = typer.Option(
        None,
        min=1,
        help="Discover one issuer from its SEC submissions JSON and store its company row; "
        "dates are optional filters.",
    ),
) -> None:
    """Queue discovery: a daily-index date range, or one issuer with --cik."""
    parsed_from = _parse_cli_date(filed_from, "filed-from")
    parsed_to = _parse_cli_date(filed_to, "filed-to")
    if cik is None and (parsed_from is None or parsed_to is None):
        raise typer.BadParameter("--filed-from and --filed-to are required without --cik")
    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        raise typer.BadParameter("filed-from must not exceed filed-to")
    asyncio.run(_enqueue_discovery(parsed_from, parsed_to, forms, refresh, cik=cik))


@filing_app.command("discover-latest")
def filing_discover_latest(
    lookback_days: int = typer.Option(
        3, min=1, max=31, help="Re-scan this many completed UTC calendar days."
    ),
    forms: str | None = typer.Option(None, help="Comma-separated forms; defaults to sources.yml."),
) -> None:
    """Queue an overlapping, idempotent daily-index scan for schedulers."""
    filed_to = datetime.now(UTC).date() - timedelta(days=1)
    filed_from = filed_to - timedelta(days=lookback_days - 1)
    asyncio.run(_enqueue_discovery(filed_from, filed_to, forms, False))


async def _enqueue_discovery(
    filed_from: date | None,
    filed_to: date | None,
    forms: str | None,
    refresh: bool,
    *,
    cik: int | None = None,
) -> None:
    settings = get_settings()
    configured = load_yaml(settings.config_dir / "sources.yml")["sec"]["default_forms"]
    selected = [item.strip() for item in forms.split(",") if item.strip()] if forms else configured
    payload: dict[str, object] = {
        "filed_from": filed_from.isoformat() if filed_from else None,
        "filed_to": filed_to.isoformat() if filed_to else None,
        "forms": selected,
    }
    if cik is not None:
        payload["cik"] = cik
    identity = stable_json_hash(payload)
    if refresh:
        identity += f":{datetime.now(UTC).isoformat()}"
    control = PostgresControlRepository(settings)
    try:
        job_id = await control.enqueue_job(
            job_type=JobType.DISCOVER_FILINGS.value,
            idempotency_key=f"daily-discovery:{identity}",
            payload=payload,
            max_attempts=settings.worker_max_attempts,
        )
        console.print(f"Enqueued discovery job {job_id}")
    finally:
        await control.close()


@mapping_app.command("import-metrics")
def mapping_import_metrics() -> None:
    asyncio.run(_mapping_import_metrics())


async def _mapping_import_metrics() -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    repository = PostgresMappingRepository(settings, engine=control.engine)
    try:
        mapper = CanonicalMapper.load_rules(
            settings.config_dir / "canonical_metrics.yml",
            settings.config_dir / "concept_seed.yml",
            repository,
        )
        count = await repository.seed_metrics(tuple(mapper.metrics.values()))
        console.print(f"Imported {count} metric definitions; candidate mappings remain unapproved")
    finally:
        await control.close()


@mapping_app.command("approve")
def mapping_approve(
    metric: str = typer.Option(...),
    namespace: str = typer.Option(...),
    concept: str = typer.Option(...),
    rationale: str = typer.Option(...),
    reviewer: str = typer.Option(...),
    taxonomy_version: str | None = typer.Option(None),
    cik: int | None = typer.Option(None, min=0),
    valid_from: str | None = typer.Option(None, help="First valid date in YYYY-MM-DD format."),
    valid_to: str | None = typer.Option(None, help="Last valid date in YYYY-MM-DD format."),
    review_id: str | None = typer.Option(None),
) -> None:
    asyncio.run(
        _mapping_approve(
            metric,
            namespace,
            concept,
            rationale,
            reviewer,
            taxonomy_version,
            cik,
            _parse_cli_date(valid_from, "valid-from"),
            _parse_cli_date(valid_to, "valid-to"),
            review_id,
        )
    )


async def _mapping_approve(
    metric: str,
    namespace: str,
    concept: str,
    rationale: str,
    reviewer: str,
    taxonomy_version: str | None,
    cik: int | None,
    valid_from: date | None,
    valid_to: date | None,
    review_id: str | None,
) -> None:
    from uuid import UUID

    settings = get_settings()
    control = PostgresControlRepository(settings)
    repository = PostgresMappingRepository(settings, engine=control.engine)
    try:
        mapping = await repository.approve_mapping(
            metric=metric,
            namespace=namespace,
            concept_name=concept,
            explanation=rationale,
            created_by=reviewer,
            taxonomy_version=taxonomy_version,
            cik=cik,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if review_id:
            await repository.resolve_review(UUID(review_id), mapping)
        console.print_json(mapping.model_dump_json())
    finally:
        await control.close()


@mapping_app.command("approve-seeds")
def mapping_approve_seeds(
    reviewer: str = typer.Option(..., help="Person approving the reviewed seed list."),
    rationale: str = typer.Option(..., help="Why these seeds are approved."),
    metric: Annotated[
        list[str] | None, typer.Option("--metric", help="Limit to these metrics (repeatable).")
    ] = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; write nothing."),
) -> None:
    """Approve seed candidates as family mappings (`us-gaap`, `ifrs-full`, `dei`, `srt`)."""
    asyncio.run(_mapping_approve_seeds(reviewer, rationale, metric, dry_run))


async def _mapping_approve_seeds(
    reviewer: str, rationale: str, metrics: list[str] | None, dry_run: bool
) -> None:
    from sec_xbrl_warehouse.mapping.seed_approval import approve_seed_candidates

    settings = get_settings()
    control = PostgresControlRepository(settings)
    repository = PostgresMappingRepository(settings, engine=control.engine)
    try:
        mapper = CanonicalMapper.load_rules(
            settings.config_dir / "canonical_metrics.yml",
            settings.config_dir / "concept_seed.yml",
            repository,
        )
        results = await approve_seed_candidates(
            mapper,
            repository,
            reviewer=reviewer,
            rationale=rationale,
            metrics=set(metrics) if metrics else None,
            dry_run=dry_run,
        )
        table = Table("Metric", "Namespace", "Concept", "Result")
        for item in results:
            if item.created:
                outcome = "approved"
            elif item.would_create:
                outcome = "would approve"
            else:
                outcome = "already approved"
            table.add_row(item.metric, item.namespace, item.concept_name, outcome)
        console.print(table)
        if dry_run:
            pending = sum(item.would_create for item in results)
            console.print(f"Dry run: {len(results)} candidates, {pending} would be approved")
        else:
            created = sum(item.created for item in results)
            console.print(f"{len(results)} candidates, {created} approved now")
    finally:
        await control.close()


@mapping_app.command("remap")
def mapping_remap(
    epoch: str = typer.Option(..., help="Label for this set of approvals, e.g. 2026-10-05."),
    accession: Annotated[
        list[str] | None, typer.Option("--accession", help="Limit to these filings (repeatable).")
    ] = None,
) -> None:
    """Enqueue one map_canonical job per published filing for this epoch."""
    asyncio.run(_mapping_remap(epoch, accession))


async def _mapping_remap(epoch: str, accessions: list[str] | None) -> None:
    from sec_xbrl_warehouse.mapping.remap import enqueue_canonical_remap

    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        created = await enqueue_canonical_remap(control, epoch=epoch, accessions=accessions)
        console.print(f"Enqueued {created} map_canonical jobs for epoch {epoch}")
    finally:
        await control.close()


@mapping_app.command("reviews")
def mapping_reviews(
    limit: int = typer.Option(100, min=1, max=10_000),
    offset: int = typer.Option(0, min=0),
) -> None:
    asyncio.run(_mapping_reviews(limit, offset))


async def _mapping_reviews(limit: int, offset: int) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    repository = PostgresMappingRepository(settings, engine=control.engine)
    try:
        console.print_json(json.dumps(await repository.list_reviews(limit, offset), default=str))
    finally:
        await control.close()


def _parse_cli_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must use YYYY-MM-DD") from exc


@taxonomy_app.command("status")
def taxonomy_status() -> None:
    """Verify registered taxonomy packages on disk (no network access)."""
    settings = get_settings()
    statuses = package_status(settings)
    _print_taxonomy_statuses(statuses)
    if any(item.state not in {"ok", "disabled"} for item in statuses):
        raise typer.Exit(code=1)


@taxonomy_app.command("sync")
def taxonomy_sync() -> None:
    """Download missing or corrupt enabled packages and verify their SHA-256."""
    settings = get_settings()
    statuses = asyncio.run(_sync_taxonomy_packages(settings))
    _print_taxonomy_statuses(statuses)
    if any(item.state not in {"ok", "disabled"} for item in statuses):
        raise typer.Exit(code=1)


async def _sync_taxonomy_packages(settings: Settings) -> list[TaxonomyPackageStatus]:
    async with SecHttpClient(
        settings,
        allowed_hosts=settings.allowed_taxonomy_hosts,
        egress_lock=SecEgressLock(settings),
    ) as http:
        return await sync_taxonomy_packages(settings, http)


def _print_taxonomy_statuses(statuses: list[TaxonomyPackageStatus]) -> None:
    table = Table("Package", "File", "State", "Details")
    for item in statuses:
        table.add_row(item.package_id, item.filename, item.state, item.detail)
    console.print(table)


@company_app.command("show")
def company_show(identifier: str = typer.Argument(..., help="CIK or ticker.")) -> None:
    """Show a stored company by CIK or ticker (no SEC request)."""
    asyncio.run(_company_show(identifier))


async def _company_show(identifier: str) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        text = identifier.strip()
        company = (
            await control.get_company(int(text))
            if text.isdigit()
            else await control.find_company_by_ticker(text)
        )
    finally:
        await control.close()
    if company is None:
        console.print(
            f"No company stored for {identifier}; run `xbrl-warehouse filing discover --cik <cik>`"
        )
        raise typer.Exit(code=1)
    console.print_json(company.model_dump_json())


@jobs_app.command("list")
def jobs_list(
    state: JobState | None = typer.Option(None, "--state", help="Filter by state."),
    job_type: JobType | None = typer.Option(None, "--type", help="Filter by job type."),
    limit: int = typer.Option(100, min=1, max=10_000),
) -> None:
    """List jobs, oldest first, with their last error."""
    asyncio.run(_jobs_list(state, job_type, limit))


async def _jobs_list(state: JobState | None, job_type: JobType | None, limit: int) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        jobs = await control.list_jobs(
            state=state.value if state else None,
            job_type=job_type.value if job_type else None,
            limit=limit,
        )
    finally:
        await control.close()
    table = Table("ID", "Type", "State", "Attempts", "Available at", "Last error")
    for job in jobs:
        table.add_row(
            str(job.id),
            job.job_type.value,
            job.state.value,
            f"{job.attempts}/{job.max_attempts}",
            job.available_at.isoformat(),
            (job.last_error or "")[:160],
        )
    console.print(table)


@jobs_app.command("retry")
def jobs_retry(job_id: str = typer.Argument(..., help="ID of a dead job.")) -> None:
    """Move a dead job back to `retry` with attempts reset; claimable immediately."""
    asyncio.run(_jobs_retry(job_id))


async def _jobs_retry(job_id: str) -> None:
    from uuid import UUID

    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        job = await control.retry_job(UUID(job_id))
    except StateTransitionError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    finally:
        await control.close()
    console.print(f"Job {job.id} ({job.job_type.value}) is {job.state.value}")


@series_app.command("rebuild")
def series_rebuild(cik: list[int] = typer.Option(..., "--cik", min=0, help="Repeatable.")) -> None:
    """Rebuild companies' metric series from their canonical facts (no SEC access)."""
    from sec_xbrl_warehouse.query.series import formulas_from_metrics, rebuild_metric_series

    settings = get_settings()
    mapper = CanonicalMapper.load_rules(
        settings.config_dir / "canonical_metrics.yml",
        settings.config_dir / "concept_seed.yml",
        None,  # type: ignore[arg-type]
    )
    warehouse = ClickHouseWarehouse(settings)
    try:
        for company in cik:
            written = rebuild_metric_series(
                warehouse,
                company,
                formulas=formulas_from_metrics(mapper.metrics),
                priorities=mapper.candidate_priorities,
            )
            console.print(f"CIK {company}: {written} series rows")
    finally:
        warehouse.close()
