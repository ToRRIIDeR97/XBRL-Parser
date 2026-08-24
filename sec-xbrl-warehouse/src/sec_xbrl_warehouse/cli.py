from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, cast

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from sec_xbrl_warehouse.config import get_settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import DatasetKind
from sec_xbrl_warehouse.domain.models import BackfillPlan, DatasetArtifact
from sec_xbrl_warehouse.domain.ports import ControlRepository
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.object_store import create_object_store
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.ingestion.bulk_catalog import BulkCatalogService, filter_artifacts_by_year
from sec_xbrl_warehouse.ingestion.orchestrator import IngestionOrchestrator
from sec_xbrl_warehouse.logging import configure_logging
from sec_xbrl_warehouse.worker import Worker, build_handlers

app = typer.Typer(help="Build and operate a local SEC XBRL warehouse.", no_args_is_help=True)
db_app = typer.Typer(help="Database schema and diagnostics.")
catalog_app = typer.Typer(help="SEC bulk dataset catalog.")
backfill_app = typer.Typer(help="Historical SEC bulk backfill.")
api_app = typer.Typer(help="HTTP query API.")
worker_app = typer.Typer(help="PostgreSQL job worker.")
app.add_typer(db_app, name="db")
app.add_typer(catalog_app, name="catalog")
app.add_typer(backfill_app, name="backfill")
app.add_typer(api_app, name="api")
app.add_typer(worker_app, name="worker")
console = Console()


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.env != "development")


@app.command()
def doctor(skip_sec_identity: bool = False) -> None:
    """Check configuration, directories, and local service connectivity."""
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []
    try:
        if not skip_sec_identity:
            settings.assert_sec_identity()
        checks.append(("SEC identity", True, settings.sec_user_agent))
    except Exception as exc:
        checks.append(("SEC identity", False, str(exc)))
    checks.append(("Data directory", settings.data_dir.is_dir(), str(settings.data_dir.resolve())))
    checks.append(
        ("Config directory", settings.config_dir.is_dir(), str(settings.config_dir.resolve()))
    )

    try:
        warehouse = ClickHouseWarehouse(settings)
        clickhouse_ok = warehouse.ping()
        clickhouse_details = f"{settings.clickhouse_host}:{settings.clickhouse_port}"
    except Exception as exc:
        clickhouse_ok = False
        clickhouse_details = str(exc)
    checks.append(("ClickHouse", clickhouse_ok, clickhouse_details))

    control: PostgresControlRepository | None = None
    try:
        control = PostgresControlRepository(settings)
        postgres_ok = asyncio.run(control.ping())
        postgres_details = settings.postgres_dsn.split("@")[-1]
    except Exception as exc:
        postgres_ok = False
        postgres_details = str(exc)
    finally:
        if control is not None:
            asyncio.run(control.close())
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
    applied = ClickHouseWarehouse(settings).apply_ddl(ddl_directory)
    for name in applied:
        console.print(f"Applied {name}")


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
    async with SecHttpClient(settings) as http:
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
def backfill_enqueue(plan_file: Path) -> None:
    plan = BackfillPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    asyncio.run(_enqueue_plan(plan))


async def _enqueue_plan(plan: BackfillPlan) -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        ids = await IngestionOrchestrator(cast(ControlRepository, control)).enqueue_backfill(plan)
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
def worker_run() -> None:
    asyncio.run(_run_worker())


async def _run_worker() -> None:
    settings = get_settings()
    control = PostgresControlRepository(settings)
    try:
        await Worker(settings, control, build_handlers()).run()
    finally:
        await control.close()
