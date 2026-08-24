from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import FactQuery, QueryService

router = APIRouter(prefix="/v1/facts", tags=["facts"])


@router.get("")
def list_raw_facts(
    cik: int | None = Query(default=None, ge=0),
    accession: str | None = None,
    concept_namespace: str | None = None,
    concept_name: str | None = None,
    filed_from: date | None = None,
    filed_to: date | None = None,
    limit: int = Query(default=500, ge=1, le=10_000),
    offset: int = Query(default=0, ge=0),
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    query = FactQuery(
        cik=cik,
        accession=accession,
        concept_namespace=concept_namespace,
        concept_name=concept_name,
        filed_from=filed_from,
        filed_to=filed_to,
        limit=limit,
        offset=offset,
    )
    rows = service.raw_facts(query)
    return {"items": rows, "limit": limit, "offset": offset, "count": len(rows)}


@router.get("/canonical/{cik}/{metric}")
def canonical_series(
    cik: int,
    metric: str,
    filed_from: date | None = None,
    filed_to: date | None = None,
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    rows = service.canonical_series(
        cik=cik,
        metric=metric,
        filed_from=filed_from,
        filed_to=filed_to,
    )
    return {"cik": cik, "metric": metric, "items": rows, "count": len(rows)}
