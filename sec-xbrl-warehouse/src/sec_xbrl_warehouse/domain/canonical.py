from __future__ import annotations

from datetime import date
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


class ConceptMapping(CanonicalModel):
    id: UUID
    namespace: str
    concept_name: str
    taxonomy_version: str | None = None
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
