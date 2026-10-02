from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from sec_xbrl_warehouse import __version__
from sec_xbrl_warehouse.api.routers import facts, filings, health
from sec_xbrl_warehouse.config import Settings, get_settings
from sec_xbrl_warehouse.exceptions import ConfigurationError
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.query.service import QueryService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    if settings.env == "production" and (
        not settings.api_key
        or not settings.api_clickhouse_username
        or settings.api_clickhouse_username == settings.clickhouse_username
        or not settings.api_clickhouse_password
        or not settings.api_clickhouse_password.get_secret_value().strip()
    ):
        raise ConfigurationError(
            "Production API requires an API key and separate reader credentials"
        )
    warehouse = await asyncio.to_thread(ClickHouseWarehouse, settings, read_only=True)
    control = PostgresControlRepository(settings)
    app.state.warehouse = warehouse
    app.state.control = control
    app.state.query_service = QueryService(warehouse, settings.config_dir / "canonical_metrics.yml")
    try:
        yield
    finally:
        await control.close()
        await asyncio.to_thread(warehouse.close)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="SEC XBRL Warehouse API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    slots = asyncio.Semaphore(settings.api_max_concurrency)
    readiness_slots = asyncio.Semaphore(1)

    @app.middleware("http")
    async def protect_queries(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.scope["path"]
        if path == "/health/live":
            return await call_next(request)
        if (
            path != "/health/ready"
            and settings.api_key is not None
            and not secrets.compare_digest(
                request.headers.get("authorization", "").encode(),
                f"Bearer {settings.api_key.get_secret_value()}".encode(),
            )
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        capacity = readiness_slots if path == "/health/ready" else slots
        if capacity.locked():
            return JSONResponse({"detail": "Query capacity reached"}, status_code=503)
        async with capacity:
            return await call_next(request)

    @app.exception_handler(DatabaseError)
    async def query_failed(request: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Query failed or exceeded its resource budget"}, status_code=503
        )

    app.include_router(health.router)
    app.include_router(facts.router)
    app.include_router(filings.router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
