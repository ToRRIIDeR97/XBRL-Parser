from __future__ import annotations

from typing import cast

from fastapi import Request

from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
from sec_xbrl_warehouse.query.service import QueryService


def get_warehouse(request: Request) -> ClickHouseWarehouse:
    return cast(ClickHouseWarehouse, request.app.state.warehouse)


def get_control(request: Request) -> PostgresControlRepository:
    return cast(PostgresControlRepository, request.app.state.control)


def get_query_service(request: Request) -> QueryService:
    return cast(QueryService, request.app.state.query_service)
