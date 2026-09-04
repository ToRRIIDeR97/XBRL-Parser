from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from sec_xbrl_warehouse import __version__
from sec_xbrl_warehouse.api.routers import facts, filings, health
from sec_xbrl_warehouse.config import get_settings
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.query.service import QueryService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    warehouse = ClickHouseWarehouse(settings)
    control = PostgresControlRepository(settings)
    app.state.warehouse = warehouse
    app.state.control = control
    app.state.query_service = QueryService(warehouse, settings.config_dir / "canonical_metrics.yml")
    try:
        yield
    finally:
        await control.close()
        warehouse.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SEC XBRL Warehouse API",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(facts.router)
    app.include_router(filings.router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
