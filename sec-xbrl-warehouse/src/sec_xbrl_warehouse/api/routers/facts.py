from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import FactQuery, QueryService

router = APIRouter(prefix="/v1/facts", tags=["facts"])


@router.get("")
def list_raw_facts(
    query: Annotated[FactQuery, Query()],
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    rows = service.raw_facts(query)
    return {"items": rows, "limit": query.limit, "offset": query.offset, "count": len(rows)}


@router.get("/canonical/{cik}/{metric}")
def canonical_series(
    cik: int,
    metric: str,
    filed_from: date | None = None,
    filed_to: date | None = None,
    limit: int = Query(default=500, ge=1, le=10_000),
    offset: int = Query(default=0, ge=0, le=10_000),
    include_dimensions: bool = False,
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    if cik < 0 or (filed_from and filed_to and filed_from > filed_to):
        raise HTTPException(status_code=422, detail="Invalid company or filing date range")
    rows = service.canonical_series(
        cik=cik,
        metric=metric,
        filed_from=filed_from,
        filed_to=filed_to,
        limit=limit,
        offset=offset,
        include_dimensions=include_dimensions,
    )
    return {
        "cik": cik,
        "metric": metric,
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
    }
