"""Independent verification tests for 15-fetch-parse-split."""

from __future__ import annotations

import asyncio

import pytest
from support15v import Control, Extractor, NoNetworkStore, Warehouse, Writer, filing, job, manifest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor
from sec_xbrl_warehouse.worker import build_handlers


# AC-4: which job types need SEC network access (and therefore an SEC client / egress lock).
@pytest.mark.parametrize(
    ("job_types", "expected"),
    [
        (None, True),
        (["parse_filing"], False),
        (["parse_filing", "map_canonical", "reconcile_filing", "load_bulk"], False),
        (["fetch_filing"], True),
        (["parse_filing", "discover_filings"], True),
        (["ingest_filing"], True),
        (["download_bulk"], True),
    ],
)
def test_ac4_requires_sec_matches_job_types(job_types, expected) -> None:
    from sec_xbrl_warehouse.worker import requires_sec

    types = None if job_types is None else [JobType(value) for value in job_types]
    assert requires_sec(types) is expected


# AC-5: discovery fans out fetch jobs (not monolithic ingest jobs).
def test_ac5_discovery_enqueues_fetch_jobs() -> None:
    class _Discovery:
        async def discover_range(self, filed_from, filed_to, forms):
            return (filing(),)

    control = Control()
    handlers = build_handlers(services={"discovery": _Discovery(), "forms": ["10-K"], "control": control})
    asyncio.run(handlers[JobType.DISCOVER_FILINGS](
        job(JobType.DISCOVER_FILINGS, {"filed_from": "2026-01-01", "filed_to": "2026-01-02"})
    ))
    assert [item["job_type"] for item in control.jobs] == [JobType.FETCH_FILING.value]
    # Discovery enqueues at the project's current extraction revision (plan 01 set it to 3).
    from sec_xbrl_warehouse.domain.xbrl import CURRENT_EXTRACTION_REVISION

    assert control.jobs[0]["idempotency_key"] == (
        f"fetch:{filing().accession}:{CURRENT_EXTRACTION_REVISION}"
    )


# AC-2: parsing without a stored package fails clearly (not by reaching for the network).
def test_ac2_parse_without_package_is_a_clear_permanent_error() -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
    from sec_xbrl_warehouse.worker import is_retryable_exception

    ingestor = IncrementalFilingIngestor(
        Control(), NoNetworkStore(), Extractor(), Writer(), Warehouse(), settings=Settings(env="test")
    )
    with pytest.raises(ArtifactIntegrityError) as caught:
        asyncio.run(ingestor.parse(filing().accession, extraction_revision=1))
    assert not is_retryable_exception(caught.value)


# AC-6: queued legacy ingest_filing jobs still work end to end (fetch then parse in one job).
def test_ac6_legacy_ingest_job_still_completes(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore

    class _Store(NoNetworkStore):
        async def download_package(self, filing):
            return manifest()

    control = Control()
    settings = Settings(env="test", data_dir=tmp_path / "data", temp_dir=tmp_path / "tmp", config_dir=tmp_path / "config")
    ingestor = IncrementalFilingIngestor(
        control, _Store(), Extractor(), Writer(), Warehouse(), settings=settings,
        object_store=LocalObjectStore(tmp_path / "objects", "sec-xbrl"),
    )
    handlers = build_handlers(services={"ingestor": ingestor, "control": control})
    payload = {"filing": filing().model_dump(mode="json"), "extraction_revision": 1}
    result = asyncio.run(handlers[JobType.INGEST_FILING](job(JobType.INGEST_FILING, payload)))
    assert result["status"] == "complete"


# AC-3: the default concurrency is one job at a time.
def test_ac3_default_concurrency_is_one() -> None:
    assert Settings(env="test").worker_concurrency == 1
