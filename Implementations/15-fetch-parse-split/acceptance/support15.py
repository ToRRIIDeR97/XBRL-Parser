"""Fakes for 15 acceptance tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sec_xbrl_warehouse.domain.enums import FactValueKind, JobState, JobType, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, JobRecord
from sec_xbrl_warehouse.domain.xbrl import XbrlContext, XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact, XbrlUnit

ACCESSION = "0000000001-26-000001"


def filing() -> FilingRef:
    return FilingRef(
        accession=ACCESSION, cik=1, company_name="Example Corp", form="10-K", filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def manifest() -> FilingPackageManifest:
    return FilingPackageManifest(
        filing=filing(), members=(), acquired_at=datetime.now(UTC),
        package_object_key="bronze/sec/filings/1/x/packages/sha256=0.zip", package_checksum_sha256="0" * 64,
    )


def job(job_type: JobType, payload: dict) -> JobRecord:
    return JobRecord(
        id=uuid4(), job_type=job_type, state=JobState.RUNNING, idempotency_key=f"{job_type.value}:{uuid4()}",
        payload=payload, attempts=1, max_attempts=5, priority=100, available_at=datetime.now(UTC),
        locked_at=datetime.now(UTC), locked_by="w1",
    )


def bundle() -> XbrlExtractionBundle:
    fact = XbrlFact(
        fact_id="f" * 64, concept_namespace="http://fasb.org/us-gaap/2025", concept_name="Revenues",
        concept_qname="us-gaap:Revenues", context_id="c1", context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)), value_kind=FactValueKind.NUMERIC,
        raw_value="1", value_decimal=Decimal(1), period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1), period_end=date(2025, 12, 31), entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001", dimension_hash="0" * 64, duplicate_set_hash="0" * 64,
        source_document="x.htm",
    )
    return XbrlExtractionBundle(
        filing=filing(),
        metadata=XbrlDocumentMetadata(entrypoint="x.htm", document_type="inline", taxonomy_namespaces=(),
                                      source_documents=("x.htm",), software_name="arelle", software_version="2.44.4"),
        contexts=(XbrlContext(context_id="c1", context_hash="0" * 64, entity_scheme="http://www.sec.gov/CIK",
                              entity_identifier="0000000001", period_kind=PeriodKind.DURATION,
                              period_start=date(2025, 1, 1), period_end=date(2025, 12, 31), source_document="x.htm"),),
        concepts=(), facts=(fact,), relationships=(), footnotes=(), validation_issues=(),
    )


class Control:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.checkpoints: dict = {}
        self.packages: dict[str, FilingPackageManifest] = {}

    async def enqueue_job(self, **kwargs):
        self.jobs.append(kwargs)
        return uuid4()

    async def upsert_filing(self, filing) -> None:
        pass

    async def update_filing_package(self, manifest, extraction_revision=None) -> None:
        self.packages[manifest.filing.accession] = manifest

    async def get_filing_manifest(self, accession: str):
        return self.packages.get(accession)

    async def record_validation_issues(self, issues) -> None:
        pass

    async def get_checkpoint(self, job_id, stage, key="default"):
        return self.checkpoints.get((stage, key))

    async def save_checkpoint(self, job_id, stage, payload, key="default") -> None:
        self.checkpoints[(stage, key)] = payload


class NoNetworkStore:
    """A package store whose download path must never be used by parsing."""

    async def download_package(self, filing):
        raise AssertionError("parse must not download from the SEC")

    def materialize_package(self, manifest, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        entry = destination / "x.htm"
        entry.write_text("<html/>", encoding="utf-8")
        return entry

    def archive_package(self, manifest, source: Path):
        return manifest

    def delete_source_files(self, manifest) -> None:
        pass


class Writer:
    def write_batches(self, batches, *, destination_prefix, partition_values):
        list(batches)
        return ()


class Warehouse:
    def insert_xbrl_bundle(self, bundle):
        return {"contexts": 1, "concepts": 0, "facts": 1, "relationships": 0, "footnotes": 0}

    def publish_xbrl_bundle(self, bundle, counts) -> None:
        pass

    def query(self, sql, parameters=None):
        return []


class Extractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, entrypoint, filing, *, extraction_revision=1):
        self.calls += 1
        return bundle().model_copy(update={"extraction_revision": extraction_revision})


async def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)
