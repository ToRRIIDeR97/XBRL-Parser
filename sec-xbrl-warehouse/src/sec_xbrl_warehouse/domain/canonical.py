from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanonicalMetric(CanonicalModel):
    code: str
    name: str
    statement: str
    period_type: Literal["instant", "duration", "derived"]
    unit_family: str
    description: str | None = None
    formula: str | None = None
    # False for averages, per-share values, and point counts: series never derive
    # quarters by year-to-date difference, Q4 = FY - 9M, or TTM sums from them.
    additive: bool = True


class ConceptMapping(CanonicalModel):
    id: UUID
    namespace: str
    concept_name: str
    taxonomy_version: str | None = None
    cik: int | None = None
    canonical_metric: str
    mapping_type: Literal["exact", "namespace", "filing_specific", "candidate"]
    confidence: float = Field(ge=0, le=1)
    approved: bool = False
    valid_from: date | None = None
    valid_to: date | None = None
    explanation: str
    version: int = Field(ge=1)


class CanonicalFact(CanonicalModel):
    canonical_fact_id: str
    raw_fact_id: str
    accession: str
    cik: int
    filed_date: date
    form: str
    canonical_metric: str
    normalized_value_decimal: Decimal | None = None
    normalized_value_text: str | None = None
    normalized_unit: str | None = None
    sign_multiplier: int = Field(default=1, ge=-1, le=1)
    period_kind: str
    period_start: date | None = None
    period_end: date | None = None
    period_instant: date | None = None
    dimensions_json: str
    mapping_id: UUID
    mapping_version: int
    mapping_confidence: float
    mapping_explanation: str
    extraction_revision: int
    # Filing context for point-in-time series: SEC acceptance time (None when
    # discovery didn't supply it) and the filing's dei fiscal focus and period end.
    accepted_at: datetime | None = None
    # The raw fact's concept, so series can rank competing concepts by seed priority.
    concept_namespace: str | None = None
    concept_qname: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    report_date: date | None = None
