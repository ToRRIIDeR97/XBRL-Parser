from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.domain.sources import FILING_KEY_MAX_LENGTH, FILING_KEY_PATTERN
from sec_xbrl_warehouse.query.service import QNAME_PATTERN, QueryService, text_block_limit

router = APIRouter(prefix="/v1/filings", tags=["filings"])

Accession = Annotated[
    str,
    Path(
        pattern=FILING_KEY_PATTERN,
        max_length=FILING_KEY_MAX_LENGTH,
        description=(
            "Filing key: an SEC accession (0000320193-25-000079) or <source>-<native id>."
        ),
    ),
]
BreakdownKind = Literal["segment", "product", "geography", "consolidation"]
Concept = Annotated[
    str | None,
    Query(
        max_length=512, pattern=QNAME_PATTERN, description="Concept qname, e.g. us-gaap:Revenues."
    ),
]


@router.get("/{accession}")
def get_filing(
    accession: Accession,
    service: QueryService = Depends(get_query_service),
    cik: int | None = Query(default=None, ge=0),
    filed_date: date | None = None,
) -> dict[str, Any]:
    result = service.filing_summary(accession, cik=cik, filed_date=filed_date)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found")
    return result


@router.get("/{accession}/cover-page")
def get_cover_page(
    accession: Accession,
    service: QueryService = Depends(get_query_service),
) -> dict[str, Any]:
    """The filing's dei cover page, with one `securities` entry per class of stock.

    Example: `/v1/filings/0000320193-25-000079/cover-page`.
    """
    result = _validated(service.cover_page, accession)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cover page not found")
    return result


@router.get("/{accession}/segments")
def list_segments(
    accession: Accession,
    service: QueryService = Depends(get_query_service),
    kind: list[BreakdownKind] | None = Query(
        default=None,
        description="Repeatable. Keeps rows with any member of these kinds.",
    ),
    concept: Concept = None,
    include_eliminations: bool = Query(
        default=True, description="False drops intersegment and consolidation eliminations."
    ),
    limit: int = Query(default=1_000, ge=1, le=10_000, description="Rows per page."),
    offset: int = Query(default=0, ge=0, le=10_000, description="Rows to skip."),
) -> dict[str, Any]:
    """Segment, product, geographic, and consolidation breakdown rows of one filing.

    Example: `/v1/filings/0000320193-25-000079/segments?kind=segment&concept=us-gaap:Revenues`.
    """
    rows = _validated(
        service.segment_facts,
        accession,
        kinds=kind,
        concept=concept,
        include_eliminations=include_eliminations,
        limit=limit,
        offset=offset,
    )
    return {
        "accession": accession,
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{accession}/text-blocks")
def list_text_blocks(
    accession: Accession,
    service: QueryService = Depends(get_query_service),
    policy: bool | None = Query(
        default=None, description="True keeps only accounting policies, false only the others."
    ),
    concept: Concept = None,
    include_text: bool = Query(
        default=False, description="Return each block's HTML as filed (limit at most 100)."
    ),
    limit: int | None = Query(
        default=None, ge=1, le=1_000, description="Default 200, or 100 with include_text."
    ),
    offset: int = Query(default=0, ge=0, le=10_000, description="Rows to skip."),
) -> dict[str, Any]:
    """Footnote and policy text blocks of one filing; `text_length` counts characters.

    Example: `/v1/filings/0000320193-25-000079/text-blocks?policy=true&include_text=true`.
    """
    limit = text_block_limit(limit, include_text=include_text)
    rows = _validated(
        service.text_blocks,
        accession,
        policy=policy,
        concept=concept,
        include_text=include_text,
        limit=limit,
        offset=offset,
    )
    return {
        "accession": accession,
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
    }


def _validated[T](method: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return method(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
