from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import QueryService

router = APIRouter(prefix="/v1/filings", tags=["filings"])


@router.get("/{accession}")
def get_filing(
    accession: str,
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    result = service.filing_summary(accession)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found")
    return result
