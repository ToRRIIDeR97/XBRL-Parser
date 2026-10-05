"""Builders and fakes for 13 verification tests."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

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

ACCESSION = "0000000001-26-000001"


def filing(**updates) -> FilingRef:
    return FilingRef(
        accession=ACCESSION, cik=1, company_name="Example Corp", form="10-K", filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001",
    ).model_copy(update=updates)


def settings(tmp_path: Path) -> Settings:
    return Settings(env="test", data_dir=tmp_path / "data", temp_dir=tmp_path / "tmp", config_dir=tmp_path / "config")


def big_bundle(count: int = 500) -> XbrlExtractionBundle:
    facts = tuple(
        XbrlFact(
            fact_id=sha256(f"f{i}".encode()).hexdigest(),
            concept_namespace="http://fasb.org/us-gaap/2025",
            concept_name=f"Concept{i % 25}",
            concept_qname=f"us-gaap:Concept{i % 25}",
            context_id=f"c{i % 10}",
            context_hash="0" * 64,
            unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
            value_kind=FactValueKind.NUMERIC,
            raw_value=str(i * 1000),
            value_decimal=Decimal(i * 1000),
            period_kind=PeriodKind.DURATION,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            entity_scheme="http://www.sec.gov/CIK",
            entity_identifier="0000000001",
            dimension_hash="0" * 64,
            duplicate_set_hash=sha256(f"d{i}".encode()).hexdigest(),
            source_document="example-20251231.htm",
        )
        for i in range(count)
    )
    return XbrlExtractionBundle(
        filing=filing(),
        metadata=XbrlDocumentMetadata(
            entrypoint="example-20251231.htm", document_type="inline", taxonomy_namespaces=(),
            source_documents=("example-20251231.htm",), software_name="arelle", software_version="2.44.4",
        ),
        contexts=(
            XbrlContext(
                context_id="c0", context_hash="0" * 64, entity_scheme="http://www.sec.gov/CIK",
                entity_identifier="0000000001", period_kind=PeriodKind.DURATION,
                period_start=date(2025, 1, 1), period_end=date(2025, 12, 31), source_document="example-20251231.htm",
            ),
        ),
        concepts=(), facts=facts, relationships=(), footnotes=(), validation_issues=(),
    )


class Control:
    def __init__(self) -> None:
        self.checkpoints: dict = {}

    async def upsert_filing(self, filing) -> None:
        pass

    async def update_filing_package(self, manifest, extraction_revision=None) -> None:
        pass

    async def record_validation_issues(self, issues) -> None:
        pass

    async def get_checkpoint(self, job_id, stage, key="default"):
        return self.checkpoints.get((stage, key))

    async def save_checkpoint(self, job_id, stage, payload, key="default") -> None:
        self.checkpoints[(stage, key)] = payload


class PackageStore:
    async def download_package(self, filing) -> FilingPackageManifest:
        return FilingPackageManifest(
            filing=filing, members=(), acquired_at=datetime.now(),
            package_object_key="bronze/package.zip", package_checksum_sha256="0" * 64,
        )

    def materialize_package(self, manifest, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        entry = destination / "example-20251231.htm"
        entry.write_text("<html/>", encoding="utf-8")
        return entry

    def archive_package(self, manifest, source: Path):
        return manifest

    def delete_source_files(self, manifest) -> None:
        pass


class Warehouse:
    def __init__(self, fail_once: bool = False) -> None:
        self.fail_once = fail_once

    def insert_xbrl_bundle(self, bundle) -> dict[str, int]:
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("temporary ClickHouse failure")
        return {"contexts": 1, "concepts": 0, "facts": len(bundle.facts), "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        pass

    def query(self, sql, parameters=None) -> list[dict]:
        return []


class Extractor:
    def __init__(self, bundle) -> None:
        self.bundle = bundle
        self.calls = 0

    def extract(self, entrypoint, filing, *, extraction_revision=1):
        self.calls += 1
        return self.bundle.model_copy(update={"extraction_revision": extraction_revision})


class Http:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def get_json(self, url: str):
        return {"directory": {"item": [{"name": name} for name in self.files]}}

    async def download(self, url: str, destination: Path, **kwargs):
        data = self.files[url.rsplit("/", 1)[-1]]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination, sha256(data).hexdigest(), len(data), {}
