from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import QueryService

router = APIRouter(prefix="/v1/series", tags=["series"])

PeriodTypeParam = Literal["quarter", "annual", "ttm", "instant"]


@router.get("")
def series_by_ticker(
    ticker: str = Query(..., min_length=1, max_length=16, description="Ticker, e.g. AAPL."),
    metrics: str = Query(..., description="Comma-separated metric codes, e.g. revenue,net_income."),
    view: Literal["latest", "as_reported"] = "latest",
    as_of: datetime | None = Query(default=None, description="Values known at this time (UTC)."),
    period_type: list[PeriodTypeParam] | None = Query(default=None),
    limit: int = Query(default=5_000, ge=1, le=10_000),
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    """Series for a ticker, resolved through the ClickHouse companies mirror."""
    try:
        cik = service.cik_for_ticker(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if cik is None:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")
    return _series(service, cik, metrics, view, as_of, period_type, limit) | {
        "ticker": ticker.upper()
    }


@router.get("/{cik}")
def series_by_cik(
    cik: int,
    metrics: str = Query(..., description="Comma-separated metric codes, e.g. revenue,net_income."),
    view: Literal["latest", "as_reported"] = "latest",
    as_of: datetime | None = Query(default=None, description="Values known at this time (UTC)."),
    period_type: list[PeriodTypeParam] | None = Query(default=None),
    limit: int = Query(default=5_000, ge=1, le=10_000),
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    """Standardized quarterly, annual, TTM, and instant points for one company.

    Example: `/v1/series/320193?metrics=revenue,operating_cash_flow&view=latest&as_of=2025-01-01T00:00:00Z`.
    """
    return _series(service, cik, metrics, view, as_of, period_type, limit)


def _series(
    service: QueryService,
    cik: int,
    metrics: str,
    view: str,
    as_of: datetime | None,
    period_type: list[PeriodTypeParam] | None,
    limit: int,
) -> dict[str, Any]:
    try:
        rows = service.metric_series(
            cik=cik,
            metrics=metrics.split(","),
            view=view,
            as_of=as_of,
            period_types=period_type,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "cik": cik,
        "view": view,
        "as_of": as_of,
        "items": rows,
        "count": len(rows),
        "limit": limit,
    }
