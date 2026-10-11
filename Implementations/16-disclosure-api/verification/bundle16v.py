"""Synthetic bundle builder for the verification tests of 16-disclosure-api."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    DimensionMember,
    XbrlConcept,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)

GAAP = "http://fasb.org/us-gaap/2025"
SRT = "http://fasb.org/srt/2025"
DEI = "http://xbrl.sec.gov/dei/2025"
EXT = "http://www.example.com/20250927"
LABEL = "http://www.xbrl.org/2003/role/label"


def h(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def dim(axis_ns: str, axis: str, member_ns: str, member: str) -> DimensionMember:
    return DimensionMember(axis_namespace=axis_ns, axis_name=axis, member_namespace=member_ns, member_name=member)


def fact(ns, name, value, *, kind=FactValueKind.NUMERIC, dims=(), instant=None, unit="iso4217:USD", fid=None):
    numeric = kind == FactValueKind.NUMERIC
    prefix = {GAAP: "us-gaap", SRT: "srt", DEI: "dei", EXT: "ex"}[ns]
    return XbrlFact(
        fact_id=h(fid or f"{name}{value}{dims}"),
        concept_namespace=ns,
        concept_name=name,
        concept_qname=f"{prefix}:{name}",
        context_id="c",
        context_hash=h(f"ctx{dims}{instant}"),
        unit=XbrlUnit(unit_id="u", numerator_measures=(unit,)) if numeric else None,
        value_kind=kind,
        raw_value=str(value),
        value_decimal=Decimal(value) if numeric else None,
        value_text=None if numeric or kind == FactValueKind.BOOLEAN else str(value),
        value_boolean=value if kind == FactValueKind.BOOLEAN else None,
        value_date=value if kind == FactValueKind.DATE else None,
        period_kind=PeriodKind.INSTANT if instant else PeriodKind.DURATION,
        period_start=None if instant else date(2024, 9, 29),
        period_end=None if instant else date(2025, 9, 27),
        period_instant=instant,
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000320193",
        dimensions=dims,
        dimension_hash=h(str(dims)),
        duplicate_set_hash=h(f"dup{name}{dims}"),
        source_document="aapl-20250927.htm",
    )


def concept(ns, name, label):
    prefix = {GAAP: "us-gaap", SRT: "srt", DEI: "dei", EXT: "ex"}[ns]
    return XbrlConcept(
        namespace=ns, name=name, qname=f"{prefix}:{name}",
        labels=(ConceptLabel(role_uri=LABEL, language="en-US", text=label),),
    )


ACCESSION = "0000320193-25-000079"


def bundle(facts, concepts=(), *, accession=ACCESSION, revision=1) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        extraction_revision=revision,
        filing=FilingRef(
            accession=accession, cik=320193, company_name="Apple Inc.", form="10-K",
            filed_date=date(2025, 10, 31), accepted_at=datetime(2025, 10, 31, 6, tzinfo=UTC),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="aapl-20250927.htm", document_type="inline", taxonomy_namespaces=(GAAP, DEI, SRT),
            source_documents=("aapl-20250927.htm",), software_name="arelle", software_version="2.44.4",
        ),
        contexts=(), concepts=tuple(concepts), facts=tuple(facts), relationships=(), footnotes=(),
        validation_issues=(),
    )


SEGMENTS = (GAAP, "StatementBusinessSegmentsAxis")
GEOGRAPHY = (SRT, "StatementGeographicalAxis")
PRODUCT = (SRT, "ProductOrServiceAxis")
CLASS = (GAAP, "StatementClassOfStockAxis")
