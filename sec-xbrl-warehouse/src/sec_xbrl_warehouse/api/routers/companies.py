from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import QueryService

router = APIRouter(tags=["companies"])


@router.get("/v1/companies")
def search_companies(
    q: str = Query(..., min_length=1, max_length=64, description="Ticker, CIK, or name text."),
    limit: int = Query(default=20, ge=1, le=100),
    exchange: str | None = Query(default=None, min_length=1, max_length=64),
    source: Literal["sec", "esef", "edinet", "dart", "nse", "mca", "acra"] | None = None,
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    """Imported companies matching a ticker prefix, company ID, or part of the name.

    Optional source and exact, case-insensitive exchange filters run before LIMIT.

    Example: `/v1/companies?q=AAPL`.
    """
    try:
        rows = service.search_companies(q, limit=limit, exchange=exchange, source=source)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"items": rows, "count": len(rows), "limit": limit}


@router.get("/v1/companies/{cik}/filings")
def list_company_filings(
    cik: int,
    limit: int = Query(default=100, ge=1, le=1_000),
    offset: int = Query(default=0, ge=0, le=10_000),
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    """Loaded filings of one company, newest first.

    Example: `/v1/companies/320193/filings?limit=10`.
    """
    try:
        rows = service.company_filings(cik, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"cik": cik, "items": rows, "count": len(rows), "limit": limit, "offset": offset}


@router.get("/v1/metrics")
def list_metrics(service: QueryService = Depends(get_query_service)) -> dict[str, Any]:
    """The canonical metric catalogue accepted by `/v1/series`."""
    rows = service.metric_catalogue()
    return {"items": rows, "count": len(rows)}
