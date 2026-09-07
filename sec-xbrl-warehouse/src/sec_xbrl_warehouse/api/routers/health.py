from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response, status

from sec_xbrl_warehouse.api.dependencies import get_control, get_warehouse
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    response: Response,
    warehouse: ClickHouseWarehouse = Depends(get_warehouse),
    control: PostgresControlRepository = Depends(get_control),
) -> dict[str, object]:
    clickhouse_ok = await asyncio.to_thread(warehouse.ping)
    postgres_ok = await control.ping()
    ready_state = clickhouse_ok and postgres_ok
    if not ready_state:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready_state else "not_ready",
        "clickhouse": clickhouse_ok,
        "postgres": postgres_ok,
    }
