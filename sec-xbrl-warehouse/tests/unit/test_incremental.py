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
        self.queries = []

    def insert_xbrl_bundle(self, bundle) -> dict[str, int]:
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("temporary ClickHouse failure")
        self.bundles.append(bundle)
        return {"contexts": 1, "concepts": 0, "facts": 1, "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        self.published = (bundle, counts)

    def query(self, sql, parameters=None) -> list[dict]:
        self.queries.append(sql)
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
    ingestor = IncrementalFilingIngestor(
        control,
        store,
        _FakeExtractor(),
        writer,
        warehouse,
        settings=Settings(env="test"),
    )
    result = asyncio.run(ingestor.ingest(_filing(), extraction_revision=2))
    assert result["status"] == "complete"
    assert result["extraction_revision"] == 2
    assert result["entity_counts"]["facts"] == 1
    assert control.upserts[0].accession == "0000000001-26-000001"
    # the non-empty context and fact tables, plus the one-row cover page, emit batches
    assert writer.calls == 3
    assert warehouse.bundles[0].filing.accession == "0000000001-26-000001"


def test_ingestion_cannot_disable_raw_retention():
    with pytest.raises(ValueError, match="Raw source retention is required"):
        Settings(_env_file=None, env="test", retain_filing_source_files=False)


def test_reconcile_reports_bulk_absence() -> None:
    control = _FakeControl()
    warehouse = _FakeWarehouse()
    ingestor = IncrementalFilingIngestor(
        control, _FakePackageStore(), _FakeExtractor(), _FakeWriter(), warehouse
    )
    result = asyncio.run(ingestor.ingest(_filing()))
    reconciliation = result["reconciliation"]
    assert reconciliation["available"] is False
    for sql in warehouse.queries[:2]:
        # The filing filter runs before the latest-row LIMIT BY, not after it.
        assert sql.index("WHERE adsh = %(accession)s") < sql.index("LIMIT 1 BY")
        assert "FROM sec_dimensions FINAL" in sql


def test_ingest_resumes_from_download_extract_and_silver_checkpoints(tmp_path: Path) -> None:
    control = _FakeControl()
    package_store = _FakePackageStore()
    extractor = _FakeExtractor()
    warehouse = _FakeWarehouse()
    warehouse.fail_once = True
    settings = Settings(
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        config_dir=tmp_path / "config",
    )
    object_store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter

    writer = ParquetBatchWriter(settings, object_store)
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
    assert control.checkpoints[("silver:facts", "default")]["object_records"]


def test_extraction_checkpoint_streams_and_round_trips(tmp_path, monkeypatch):
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle, write_json

    bundle = _bundle()

    def no_whole_bundle(*args, **kwargs):
        pytest.fail("Whole-bundle serialization should not be used")

    monkeypatch.setattr(XbrlExtractionBundle, "model_dump_json", no_whole_bundle)
    path = tmp_path / "bundle.json"
    write_json(bundle, path, 100000)
    assert read_bundle(path, 100000) == bundle
    with pytest.raises(XbrlExtractionError, match="byte limit"):
        write_json(bundle, path, 5)
    assert not path.exists()


def test_extraction_json_bytes_match_the_field_by_field_encoding(tmp_path):
    """Records are serialized whole now; saved checkpoints must keep their bytes."""
    from pydantic import BaseModel
    from pydantic_core import to_json

    from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import write_json

    def field_by_field(value):
        if isinstance(value, BaseModel):
            value = {name: getattr(value, name) for name in type(value).model_fields}
        if isinstance(value, dict):
            items = [to_json(str(key)) + b":" + field_by_field(item) for key, item in value.items()]
            return b"{" + b",".join(items) + b"}"
        if isinstance(value, (tuple, list)):
            return b"[" + b",".join(field_by_field(item) for item in value) + b"]"
        return to_json(value)

    bundle = _bundle()
    bundle = bundle.model_copy(
        update={
            "metadata": bundle.metadata.model_copy(
                update={"provenance": {"validation_log": [{"level": "info", "args": [1, None]}]}}
            )
        }
    )
    path = tmp_path / "bundle.json"
    write_json(bundle, path, 1_000_000)
    assert path.read_bytes() == field_by_field(bundle)


def test_entity_tables_are_built_in_bounded_batches():
    from sec_xbrl_warehouse.exceptions import WarehouseWriteError
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import iter_xbrl_tables

    bundle = _bundle()
    bundle = bundle.model_copy(
        update={
            "facts": tuple(bundle.facts[0].model_copy(update={"fact_id": str(i)}) for i in range(5))
        }
    )
    tables = list(iter_xbrl_tables(bundle, "facts", 2, 100000))
    assert [table.num_rows for table in tables] == [2, 2, 1]
    assert [row["fact_id"] for table in tables for row in table.to_pylist()] == [
        "0",
        "1",
        "2",
        "3",
        "4",
    ]
    with pytest.raises(WarehouseWriteError, match="byte budget"):
        list(iter_xbrl_tables(bundle, "facts", 2, 5))


def test_arrow_byte_limit_includes_denormalized_filing_metadata():
    from sec_xbrl_warehouse.exceptions import WarehouseWriteError
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import iter_xbrl_tables

    bundle = _bundle()
    bundle = bundle.model_copy(
        update={
            "filing": bundle.filing.model_copy(update={"company_name": "x" * 6000}),
            "facts": bundle.facts * 5,
        }
    )
    tables = list(iter_xbrl_tables(bundle, "facts", 50, 10000))
    assert sum(table.num_rows for table in tables) == 5
    assert all(table.nbytes <= 10000 for table in tables)
    bundle = bundle.model_copy(
        update={"filing": bundle.filing.model_copy(update={"company_name": "x" * 20000})}
    )
    with pytest.raises(WarehouseWriteError, match="row exceeds Arrow byte budget"):
        list(iter_xbrl_tables(bundle, "facts", 50, 10000))


def test_ingestor_takes_egress_lock_before_online_extraction_only() -> None:
    for offline, expected in ((False, 1), (True, 0)):
        calls: list[str] = []

        async def ensure(calls: list[str] = calls) -> None:
            calls.append("lock")

        ingestor = IncrementalFilingIngestor(
            _FakeControl(),
            _FakePackageStore(),
            _FakeExtractor(),
            _FakeWriter(),
            _FakeWarehouse(),
            settings=Settings(env="test", arelle_offline=offline),
            ensure_sec_egress=ensure,
        )
        assert asyncio.run(ingestor.ingest(_filing()))["status"] == "complete"
        assert len(calls) == expected


def test_busy_egress_lock_records_no_diagnostics() -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError

    async def busy() -> None:
        raise SecEgressBusyError("busy")

    control = _FakeControl()
    ingestor = IncrementalFilingIngestor(
        control,
        _FakePackageStore(),
        _FakeExtractor(),
        _FakeWriter(),
        _FakeWarehouse(),
        settings=Settings(env="test", arelle_offline=False),
        ensure_sec_egress=busy,
    )
    for _ in range(3):
        with pytest.raises(SecEgressBusyError):
            asyncio.run(ingestor.ingest(_filing()))
    # The validation stage runs after extraction, so a busy lock records nothing.
    assert not hasattr(control, "issues")


def test_shared_standard_concepts_are_stored_once_across_filings(tmp_path):
    from datetime import UTC, datetime

    from sec_xbrl_warehouse.domain.xbrl import XbrlConcept
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
        filing_concepts,
        iter_shared_concept_tables,
    )
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
    from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter

    def concept(namespace, name, *, extension=False, version="2024"):
        prefix = "ext" if extension else "us-gaap"
        return XbrlConcept(
            namespace=namespace,
            name=name,
            qname=f"{prefix}:{name}",
            taxonomy_version=None if extension else version,
            is_extension=extension,
        )

    gaap = "http://fasb.org/us-gaap/2024"
    first = _bundle().model_copy(
        update={
            "concepts": (
                concept(gaap, "Revenues"),
                concept(gaap, "Assets"),
                concept("http://fasb.org/us-gaap/2023", "Assets", version="2023"),
                concept("http://example.com/ext", "Custom", extension=True),
            )
        }
    )
    second = first.model_copy(
        update={
            "filing": first.filing.model_copy(update={"accession": "0000000001-26-000002"}),
            "metadata": first.metadata.model_copy(
                update={"extracted_at": datetime(2030, 1, 1, tzinfo=UTC)}
            ),
            "concepts": tuple(reversed(first.concepts)),  # order must not matter
        }
    )
    settings = Settings(env="test", data_dir=tmp_path / "data", temp_dir=tmp_path / "tmp")
    writer = ParquetBatchWriter(settings, LocalObjectStore(tmp_path / "objects", "sec-xbrl"))
    keys = []
    for bundle in (first, second):
        tables = list(iter_shared_concept_tables(bundle, 1000, 10_000_000))
        shared_rows = sum(table.num_rows for _, table in tables)
        assert shared_rows + len(filing_concepts(bundle)) == len(bundle.concepts)
        keys.append(
            [
                item.key
                for prefix, table in tables
                for item in writer.write_batches(
                    (table,), destination_prefix=prefix, partition_values={}
                )
            ]
        )
    assert keys[0] == keys[1]  # one stored object for both filings
