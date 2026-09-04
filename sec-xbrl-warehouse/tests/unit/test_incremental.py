import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def _bundle(extraction_revision: int = 1) -> XbrlExtractionBundle:
    filing = _filing()
    context = XbrlContext(
        context_id="c1",
        context_hash="0" * 64,
        entity_scheme="https://www.sec.gov/CIK",
        entity_identifier="0000000001",
        period_kind=PeriodKind.INSTANT,
        period_instant=date(2025, 12, 31),
        source_document="example.xml",
    )
    fact = XbrlFact(
        fact_id="f1",
        concept_namespace="http://example.com",
        concept_name="Revenue",
        concept_qname="ex:Revenue",
        context_id="c1",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value="123",
        value_decimal=Decimal("123"),
        period_kind=PeriodKind.INSTANT,
        period_instant=date(2025, 12, 31),
        entity_scheme="https://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="d",
        duplicate_set_hash="s",
        source_document="example.xml",
    )
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="example.xml",
            document_type="instance",
            taxonomy_namespaces=("http://example.com",),
            source_documents=("example.xml",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(context,),
        concepts=(),
        facts=(fact,),
        relationships=(),
        footnotes=(),
        validation_issues=(),
        extraction_revision=extraction_revision,
    )


class _FakeControl:
    def __init__(self) -> None:
        self.upserts: list[FilingRef] = []
        self.checkpoints = {}

    async def upsert_filing(self, filing: FilingRef) -> None:
        self.upserts.append(filing)

    async def update_filing_package(self, manifest, extraction_revision=None) -> None:
        self.manifest = manifest
        self.extraction_revision = extraction_revision

    async def record_validation_issues(self, issues) -> None:
        self.issues = tuple(issues)

    async def get_checkpoint(self, job_id, stage, key="default"):
        return self.checkpoints.get((stage, key))

    async def save_checkpoint(self, job_id, stage, payload, key="default") -> None:
        self.checkpoints[(stage, key)] = payload


class _FakePackageStore:
    def __init__(self) -> None:
        self.local_directory = None
        self.downloads = 0
        self.archives = 0

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        self.downloads += 1
        return FilingPackageManifest(
            filing=filing,
            members=(),
            acquired_at=datetime.now(),
        )

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        entry = destination / "example.xml"
        entry.write_text("<xbrl/>", encoding="utf-8")
        return entry

    def archive_package(self, manifest: FilingPackageManifest, source: Path):
        self.archives += 1
        return manifest.model_copy(update={"package_object_key": "bronze/filing.zip"})


class _FakeWriter:
    def __init__(self) -> None:
        self.calls = 0

    def write_batches(self, batches, *, destination_prefix, partition_values):
        self.calls += len(list(batches))
        return ()


class _FakeWarehouse:
    def __init__(self) -> None:
        self.bundles = []
        self.fail_once = False

    def insert_xbrl_bundle(self, bundle) -> dict[str, int]:
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("temporary ClickHouse failure")
        self.bundles.append(bundle)
        return {"contexts": 1, "concepts": 0, "facts": 1, "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        self.published = (bundle, counts)

    def query(self, sql, parameters=None) -> list[dict]:
        if "sec_numeric_facts" in sql:
            return []  # no bulk representation yet
        return []


class _FakeExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, entrypoint, filing, *, extraction_revision=1) -> XbrlExtractionBundle:
        self.calls += 1
        assert entrypoint is not None
        return _bundle(extraction_revision)


def test_ingest_runs_pipeline(tmp_path: Path) -> None:
    control = _FakeControl()
    store = _FakePackageStore()
    writer = _FakeWriter()
    warehouse = _FakeWarehouse()
    ingestor = IncrementalFilingIngestor(control, store, _FakeExtractor(), writer, warehouse)
    result = asyncio.run(ingestor.ingest(_filing(), extraction_revision=2))
    assert result["status"] == "complete"
    assert result["extraction_revision"] == 2
    assert result["entity_counts"]["facts"] == 1
    assert control.upserts[0].accession == "0000000001-26-000001"
    assert writer.calls == 2  # only the non-empty context and fact tables emit batches
    assert warehouse.bundles[0].filing.accession == "0000000001-26-000001"


def test_reconcile_reports_bulk_absence() -> None:
    control = _FakeControl()
    warehouse = _FakeWarehouse()
    ingestor = IncrementalFilingIngestor(
        control, _FakePackageStore(), _FakeExtractor(), _FakeWriter(), warehouse
    )
    result = asyncio.run(ingestor.ingest(_filing()))
    reconciliation = result["reconciliation"]
    assert reconciliation["available"] is False


def test_ingest_resumes_from_download_extract_and_silver_checkpoints(tmp_path: Path) -> None:
    control = _FakeControl()
    package_store = _FakePackageStore()
    extractor = _FakeExtractor()
    writer = _FakeWriter()
    warehouse = _FakeWarehouse()
    warehouse.fail_once = True
    settings = Settings(
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        config_dir=tmp_path / "config",
    )
    object_store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    ingestor = IncrementalFilingIngestor(
        control,
        package_store,
        extractor,
        writer,
        warehouse,
        settings=settings,
        object_store=object_store,
    )
    job_id = uuid4()
    with pytest.raises(ConnectionError):
        asyncio.run(ingestor.ingest(_filing(), job_id=job_id))

    result = asyncio.run(ingestor.ingest(_filing(), job_id=job_id))
    assert result["status"] == "complete"
    assert package_store.downloads == 1
    assert package_store.archives == 1
    assert extractor.calls == 1
    assert writer.calls == 2
