"""Synthetic bundles for 14 acceptance tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    XbrlConcept,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)

GAAP = "http://fasb.org/us-gaap/2025"
EXT = "http://www.example.com/20251231"


def h(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def concept(namespace: str, name: str) -> XbrlConcept:
    prefix = "us-gaap" if namespace == GAAP else "ex"
    return XbrlConcept(
        namespace=namespace, name=name, qname=f"{prefix}:{name}", taxonomy_version="2025" if namespace == GAAP else None,
        datatype_qname="xbrli:monetaryItemType", period_type="duration", balance="credit",
        is_extension=namespace != GAAP,
        labels=(ConceptLabel(role_uri="http://www.xbrl.org/2003/role/label", language="en-US", text=name),),
    )


def fact(name: str, *, namespace: str = GAAP, raw: str = "1000", value: Decimal | None = Decimal(1000),
         text: str | None = None, kind: FactValueKind = FactValueKind.NUMERIC) -> XbrlFact:
    prefix = "us-gaap" if namespace == GAAP else "ex"
    return XbrlFact(
        fact_id=h(f"fact-{name}-{raw}"), concept_namespace=namespace, concept_name=name,
        concept_qname=f"{prefix}:{name}", context_id="c1", context_hash=h("ctx"),
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)) if kind == FactValueKind.NUMERIC else None,
        value_kind=kind, raw_value=raw, value_decimal=value, value_text=text,
        period_kind=PeriodKind.DURATION, period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK", entity_identifier="0000000001",
        dimension_hash=h("nodims"), duplicate_set_hash=h(f"dup-{name}"), source_document="x.htm",
    )


def bundle(accession: str = "0000000001-26-000001", facts=(), concepts=()) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession=accession, cik=1, company_name="Example Corp", form="10-K", filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="x.htm", document_type="inline", taxonomy_namespaces=(GAAP,), source_documents=("x.htm",),
            software_name="arelle", software_version="2.44.4",
        ),
        contexts=(), concepts=tuple(concepts), facts=tuple(facts), relationships=(), footnotes=(),
        validation_issues=(),
    )
