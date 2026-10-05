"""Shared builders for 05 acceptance tests (kept local so tests stay self-contained)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, Severity
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, ValidationIssue
from sec_xbrl_warehouse.domain.xbrl import (
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor

ACCESSION = "0000000001-26-000001"


def filing() -> FilingRef:
    return FilingRef(
        accession=ACCESSION,
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def fact(concept: str = "Revenues", fact_id: str = "f1", namespace: str = "http://fasb.org/us-gaap/2025") -> XbrlFact:
    return XbrlFact(
        fact_id=fact_id,
        concept_namespace=namespace,
        concept_name=concept,
        concept_qname=f"us-gaap:{concept}",
        taxonomy_version="2025",
        context_id="c1",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value="100",
        value_decimal=Decimal("100"),
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="0" * 64,
        duplicate_set_hash=f"dup-{fact_id}",
        source_document="example-20251231.htm",
    )


def issue(code: str, document: str, line: int, severity: Severity = Severity.ERROR) -> ValidationIssue:
    return ValidationIssue(
        source="arelle",
        code=code,
        severity=severity,
        message=f"{code} at {document} line {line}",
        accession=ACCESSION,
        source_document=document,
        line=line,
    )


def bundle(issues: tuple[ValidationIssue, ...]) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=filing(),
        metadata=XbrlDocumentMetadata(
            entrypoint="example-20251231.htm",
            document_type="inline",
            taxonomy_namespaces=(),
            source_documents=("example-20251231.htm",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(
            XbrlContext(
                context_id="c1",
                context_hash="0" * 64,
                entity_scheme="http://www.sec.gov/CIK",
                entity_identifier="0000000001",
                period_kind=PeriodKind.DURATION,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                source_document="example-20251231.htm",
            ),
        ),
        concepts=(),
        facts=(fact(),),
        relationships=(),
        footnotes=(),
        validation_issues=issues,
    )


class Control:
    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []
        self.checkpoints: dict = {}

    async def upsert_filing(self, filing: FilingRef) -> None:
        pass

    async def update_filing_package(self, manifest, extraction_revision=None) -> None:
        pass

    async def record_validation_issues(self, issues) -> None:
        self.issues.extend(issues)

    async def get_checkpoint(self, job_id, stage, key="default"):
        return self.checkpoints.get((stage, key))

    async def save_checkpoint(self, job_id, stage, payload, key="default") -> None:
        self.checkpoints[(stage, key)] = payload


class _Store:
    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        return FilingPackageManifest(filing=filing, members=(), acquired_at=datetime.now())

    def materialize_package(self, manifest, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        entry = destination / "example-20251231.htm"
        entry.write_text("<html/>", encoding="utf-8")
        return entry

    def archive_package(self, manifest, source: Path):
        return manifest.model_copy(update={"package_object_key": "bronze/filing.zip"})

    def delete_source_files(self, manifest) -> None:
        pass


class _Writer:
    def write_batches(self, batches, *, destination_prefix, partition_values):
        list(batches)
        return ()


class _Warehouse:
    def insert_xbrl_bundle(self, bundle) -> dict[str, int]:
        return {"contexts": 1, "concepts": 0, "facts": 1, "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        pass

    def query(self, sql, parameters=None) -> list[dict]:
        return []


class _Extractor:
    def __init__(self, bundle: XbrlExtractionBundle) -> None:
        self.bundle = bundle

    def extract(self, entrypoint, filing, *, extraction_revision=1) -> XbrlExtractionBundle:
        return self.bundle.model_copy(update={"extraction_revision": extraction_revision})


def ingest(issues: tuple[ValidationIssue, ...], revision: int = 2) -> Control:
    control = Control()
    ingestor = IncrementalFilingIngestor(
        control, _Store(), _Extractor(bundle(issues)), _Writer(), _Warehouse(), settings=Settings(env="test")
    )
    asyncio.run(ingestor.ingest(filing(), extraction_revision=revision))
    return control


class MappingRepository:
    def __init__(self) -> None:
        self.requests: list[tuple] = []

    async def approved_mappings_for_concept(self, namespace, concept_name, taxonomy_version, cik, filed_date):
        return ()

    async def create_reviews(self, filing, reviews):
        self.requests.extend(reviews)
        return {request[0].fact_id: None for request in reviews}
