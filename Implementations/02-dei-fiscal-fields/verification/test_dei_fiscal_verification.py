"""Independent verification tests for 02-dei-fiscal-fields."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    DimensionMember,
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import facts_table
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor

DEI = "http://xbrl.sec.gov/dei/2024"


def _filing(**updates: object) -> FilingRef:
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    return filing.model_copy(update=updates)


def _fact(name: str, text: str, namespace: str = DEI, fact_id: str | None = None) -> XbrlFact:
    return XbrlFact(
        fact_id=fact_id or f"{name}-{text}",
        concept_namespace=namespace,
        concept_name=name,
        concept_qname=f"dei:{name}",
        context_id="FY2025",
        context_hash="0" * 64,
        value_kind=FactValueKind.TEXT,
        raw_value=text,
        value_text=text,
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="0" * 64,
        duplicate_set_hash=f"dup-{name}-{text}",
        source_document="example-20251231.htm",
    )


def _revenue() -> XbrlFact:
    return XbrlFact(
        fact_id="revenue",
        concept_namespace="http://example.com/2026",
        concept_name="Revenue",
        concept_qname="ex:Revenue",
        context_id="FY2025",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="USD", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value="1234",
        value_decimal=Decimal("1234000000"),
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="0" * 64,
        duplicate_set_hash="dup-revenue",
        source_document="example-20251231.htm",
    )


def _bundle(filing: FilingRef, facts: tuple[XbrlFact, ...]) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="example-20251231.htm",
            document_type="inline",
            taxonomy_namespaces=(DEI,),
            source_documents=("example-20251231.htm",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(
            XbrlContext(
                context_id="FY2025",
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
        facts=facts,
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


class _Control:
    def __init__(self) -> None:
        self.upserts: list[FilingRef] = []
        self.issues: list = []
        self.checkpoints: dict = {}

    async def upsert_filing(self, filing: FilingRef) -> None:
        self.upserts.append(filing)

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
    def __init__(self) -> None:
        self.bundles: list[XbrlExtractionBundle] = []

    def insert_xbrl_bundle(self, bundle) -> dict[str, int]:
        self.bundles.append(bundle)
        return {"contexts": 1, "concepts": 0, "facts": len(bundle.facts), "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        pass

    def query(self, sql, parameters=None) -> list[dict]:
        return []


class _Extractor:
    def __init__(self, bundle: XbrlExtractionBundle) -> None:
        self.bundle = bundle

    def extract(self, entrypoint, filing, *, extraction_revision=1) -> XbrlExtractionBundle:
        return self.bundle.model_copy(update={"extraction_revision": extraction_revision})


def _ingest(bundle: XbrlExtractionBundle, filing: FilingRef):
    control, warehouse = _Control(), _Warehouse()
    ingestor = IncrementalFilingIngestor(
        control, _Store(), _Extractor(bundle), _Writer(), warehouse, settings=Settings(env="test")
    )
    result = asyncio.run(ingestor.ingest(filing, extraction_revision=1))
    return result, control, warehouse


def _fiscal_columns(warehouse: _Warehouse) -> set[tuple[object, object]]:
    table = facts_table(warehouse.bundles[0])
    return set(zip(table.column("fiscal_year").to_pylist(), table.column("fiscal_period").to_pylist()))


def _dimensional(fact: XbrlFact, member: str) -> XbrlFact:
    return fact.model_copy(
        update={
            "fact_id": f"{fact.fact_id}-{member}",
            "dimensions": (
                DimensionMember(
                    axis_namespace=DEI,
                    axis_name="LegalEntityAxis",
                    member_namespace="http://example.com/2026",
                    member_name=member,
                ),
            ),
            "dimension_hash": "1" * 64,
        }
    )


def _issues(control: _Control, code: str) -> list:
    return [issue for issue in control.issues if issue.code == code]


# AC-3: dimensional (co-registrant) dei facts never decide the filing focus.
def test_ac3_dimensional_dei_facts_are_ignored() -> None:
    bundle = _bundle(
        _filing(),
        (
            _fact("DocumentFiscalYearFocus", "2025"),
            _fact("DocumentFiscalPeriodFocus", "FY"),
            _dimensional(_fact("DocumentFiscalYearFocus", "2019"), "SubsidiaryMember"),
            _dimensional(_fact("DocumentFiscalPeriodFocus", "Q2"), "SubsidiaryMember"),
            _revenue(),
        ),
    )
    _, control, warehouse = _ingest(bundle, _filing())
    assert _fiscal_columns(warehouse) == {(2025, "FY")}
    assert not _issues(control, "warehouse:fiscalFocusConflict")


# AC-4: conflicting non-dimensional values are not guessed; ingestion still completes.
def test_ac4_conflicting_values_leave_fields_null_with_error_issue() -> None:
    bundle = _bundle(
        _filing(),
        (
            _fact("DocumentFiscalYearFocus", "2025"),
            _fact("DocumentFiscalYearFocus", "2024", fact_id="second-year"),
            _fact("DocumentFiscalPeriodFocus", "FY"),
            _revenue(),
        ),
    )
    result, control, warehouse = _ingest(bundle, _filing())
    assert result["status"] == "complete"
    assert _fiscal_columns(warehouse) == {(None, "FY")}
    conflicts = _issues(control, "warehouse:fiscalFocusConflict")
    assert conflicts and conflicts[0].severity.value == "error"
    assert conflicts[0].accession == "0000000001-26-000001"


# AC-5: values are normalized; values outside the dei enumeration are rejected.
@pytest.mark.parametrize(
    ("raw", "expected", "invalid"),
    [("fy", "FY", False), (" q3 ", "Q3", False), ("H1", "H1", False), ("Q5", None, True)],
)
def test_ac5_fiscal_period_normalization(raw: str, expected: str | None, invalid: bool) -> None:
    bundle = _bundle(
        _filing(),
        (_fact("DocumentFiscalYearFocus", "2025"), _fact("DocumentFiscalPeriodFocus", raw), _revenue()),
    )
    _, control, warehouse = _ingest(bundle, _filing())
    assert _fiscal_columns(warehouse) == {(2025, expected)}
    assert bool(_issues(control, "warehouse:fiscalFocusInvalid")) is invalid


# AC-6: no dei facts means no fabricated values and no fiscal diagnostics.
def test_ac6_missing_dei_keeps_discovery_values() -> None:
    filing = _filing(fiscal_year=2025, fiscal_period="FY")
    _, control, warehouse = _ingest(_bundle(filing, (_revenue(),)), filing)
    assert _fiscal_columns(warehouse) == {(2025, "FY")}
    assert not [issue for issue in control.issues if issue.code.startswith("warehouse:fiscal")]

    _, control, warehouse = _ingest(_bundle(_filing(), (_revenue(),)), _filing())
    assert _fiscal_columns(warehouse) == {(None, None)}


# AC-1 boundary: report_date comes from dei only when discovery did not supply one.
def test_ac1_report_date_fills_gap_but_keeps_discovery_value() -> None:
    end = _fact("DocumentPeriodEndDate", "2025-12-31").model_copy(
        update={"value_kind": FactValueKind.DATE, "value_text": None, "value_date": date(2025, 12, 31)}
    )
    facts = (_fact("DocumentFiscalYearFocus", "2025"), _fact("DocumentFiscalPeriodFocus", "FY"), end, _revenue())
    _, control, _ = _ingest(_bundle(_filing(), facts), _filing())
    assert control.upserts[-1].report_date == date(2025, 12, 31)

    supplied = _filing(report_date=date(2025, 12, 31))
    _, control, _ = _ingest(_bundle(supplied, facts), supplied)
    assert control.upserts[-1].report_date == date(2025, 12, 31)
