from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import QueryService

router = APIRouter(prefix="/v1/filings", tags=["filings"])


@router.get("/{accession}")
def get_filing(
    accession: Annotated[str, Path(pattern=r"^\d{10}-\d{2}-\d{6}$")],
    service: QueryService = Depends(get_query_service),
    cik: int | None = Query(default=None, ge=0),
    filed_date: date | None = None,
) -> dict[str, Any]:
    result = service.filing_summary(accession, cik=cik, filed_date=filed_date)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found")
    return result
