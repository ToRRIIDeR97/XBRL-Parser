from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sec_xbrl_warehouse.domain.enums import (
    FactValueKind,
    PeriodKind,
    RelationshipType,
)
from sec_xbrl_warehouse.domain.models import FilingRef, ValidationIssue


class XbrlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DimensionMember(XbrlModel):
    axis_namespace: str
    axis_name: str
    member_namespace: str | None = None
    member_name: str | None = None
    typed_value_xml: str | None = None
    context_element: str | None = None


class XbrlContext(XbrlModel):
    context_id: str
    context_hash: str
    entity_scheme: str
    entity_identifier: str
    period_kind: PeriodKind
    period_start: date | None = None
    period_end: date | None = None
    period_instant: date | None = None
    dimensions: tuple[DimensionMember, ...] = ()
    segment_xml: str | None = None
    scenario_xml: str | None = None
    source_document: str


class XbrlUnit(XbrlModel):
    unit_id: str
    numerator_measures: tuple[str, ...]
    denominator_measures: tuple[str, ...] = ()


class ConceptLabel(XbrlModel):
    role_uri: str
    language: str
    text: str


class XbrlConcept(XbrlModel):
    namespace: str
    name: str
    qname: str
    taxonomy_version: str | None = None
    datatype_qname: str | None = None
    substitution_group_qname: str | None = None
    period_type: str | None = None
    balance: str | None = None
    abstract: bool = False
    nillable: bool = False
    is_extension: bool = False
    labels: tuple[ConceptLabel, ...] = ()
    documentation: str | None = None
    references: tuple[dict[str, Any], ...] = ()


class XbrlFact(XbrlModel):
    fact_id: str
    concept_namespace: str
    concept_name: str
    concept_qname: str
    taxonomy_version: str | None = None
    context_id: str
    context_hash: str
    unit: XbrlUnit | None = None
    value_kind: FactValueKind
    raw_value: str | None = None
    value_decimal: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    value_date: date | None = None
    is_nil: bool = False
    decimals: int | None = None
    precision: int | None = None
    language: str | None = None
    period_kind: PeriodKind
    period_start: date | None = None
    period_end: date | None = None
    period_instant: date | None = None
    entity_scheme: str
    entity_identifier: str
    dimensions: tuple[DimensionMember, ...] = ()
    dimension_hash: str
    tuple_path: str | None = None
    duplicate_ordinal: int = Field(default=1, ge=1)
    duplicate_set_hash: str
    footnote_ids: tuple[str, ...] = ()
    source_document: str
    source_fact_id: str | None = None
    source_attributes: dict[str, str] = Field(default_factory=dict)


class XbrlRelationship(XbrlModel):
    relationship_id: str
    relationship_type: RelationshipType
    link_role_uri: str
    arc_role_uri: str
    from_qname: str
    to_qname: str
    order: Decimal = Decimal("1")
    priority: int | None = None
    weight: Decimal | None = None
    preferred_label: str | None = None
    use: str | None = None
    closed: bool | None = None
    usable: bool | None = None
    target_role: str | None = None
    context_element: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_document: str | None = None


class XbrlFootnote(XbrlModel):
    footnote_id: str
    role_uri: str | None = None
    language: str | None = None
    text: str
    html: str | None = None
    linked_fact_ids: tuple[str, ...]


class XbrlDocumentMetadata(XbrlModel):
    entrypoint: str
    document_type: str
    taxonomy_namespaces: tuple[str, ...]
    source_documents: tuple[str, ...]
    software_name: str
    software_version: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: dict[str, Any] = Field(default_factory=dict)


# Extraction revision for newly queued filing jobs. Increment it when a change
# alters extraction input or output (package selection, Arelle loading, fact
# shaping) so re-ingestion writes a new revision instead of replacing lineage.
# 3: inline document sets exclude exhibits and rendered pages, and `-xbrl.zip`
# packages are expanded (filing package selection policy 2), and filing fiscal
# fields come from dei facts.
CURRENT_EXTRACTION_REVISION = 3


class XbrlExtractionBundle(XbrlModel):
    filing: FilingRef
    metadata: XbrlDocumentMetadata
    contexts: tuple[XbrlContext, ...]
    concepts: tuple[XbrlConcept, ...]
    facts: tuple[XbrlFact, ...]
    relationships: tuple[XbrlRelationship, ...]
    footnotes: tuple[XbrlFootnote, ...]
    validation_issues: tuple[ValidationIssue, ...]
    extraction_revision: int = 1
