"""Worker-visible acceptance tests for 17-filing-and-entity-keys."""

from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sec_xbrl_warehouse.domain.models import FilingRef

ESEF_KEY = "esef-5493001KJTIIGC8Y1R12-2023-12-31-ESEF-GB-0"
LEI_SCHEME = "http://standards.iso.org/iso/17442"
integration = pytest.mark.skipif(
    os.getenv("RUN_XBRL_INTEGRATION") != "1", reason="Set RUN_XBRL_INTEGRATION=1"
)


def esef_filing(key: str = ESEF_KEY, entity_key: int = 10_000_000_001) -> FilingRef:
    return FilingRef(
        accession=key,
        cik=entity_key,
        company_name="Example plc",
        form="AFR",
        filed_date=date(2024, 4, 2),
        archive_directory_url="https://filings.xbrl.org/5493001KJTIIGC8Y1R12/2023-12-31/ESEF/GB/0",
    )


# AC-1: SEC accessions and `<source>-<native id>` keys both resolve to a source.
@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("0000320193-25-000079", "sec"),
        (ESEF_KEY, "esef"),
        ("edinet-S100TR7I", "edinet"),
        ("dart-20240312000736", "dart"),
    ],
)
def test_ac1_filing_source(key: str, source: str) -> None:
    from sec_xbrl_warehouse.domain.sources import filing_source

    assert filing_source(key) == source


# AC-1: malformed keys are rejected.
@pytest.mark.parametrize(
    "key",
    [
        "0000320193-25-00007",  # short SEC accession
        "ESEF-abc",  # source must be lower case
        "esef-",  # empty native id
        "sec-0000320193-25-000079",  # SEC filings use the bare accession
        "esef-a/b",  # no path separators
        "esef-" + "x" * 124,  # longer than 128 characters
    ],
)
def test_ac1_malformed_keys_are_rejected(key: str) -> None:
    from sec_xbrl_warehouse.domain.sources import filing_source

    with pytest.raises(ValueError):
        filing_source(key)


# AC-2: FilingRef accepts a non-SEC key with an entity key from the non-SEC range,
# and keeps SEC filings on CIKs below it.
def test_ac2_filing_ref_key_and_entity_ranges() -> None:
    from sec_xbrl_warehouse.domain.sources import ENTITY_KEY_BASE

    assert ENTITY_KEY_BASE == 10_000_000_000
    assert esef_filing().accession == ESEF_KEY
    with pytest.raises(ValidationError):
        esef_filing(entity_key=320193)  # a non-SEC filing can't use a CIK
    with pytest.raises(ValidationError):
        FilingRef(
            accession="0000320193-25-000079",
            cik=ENTITY_KEY_BASE + 5,  # an SEC filing can't use a non-SEC entity key
            company_name="Apple Inc.",
            form="10-K",
            filed_date=date(2025, 10, 31),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
        )


# AC-5: object keys carry the filing's source; SEC keys are unchanged.
def test_ac5_object_keys_use_the_filing_source() -> None:
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_object_key, filing_package_key

    checksum = "0" * 64
    assert filing_package_key(esef_filing(), checksum) == (
        f"bronze/esef/filings/10000000001/{ESEF_KEY}/packages/sha256={checksum}/package.zip"
    )
    assert filing_object_key(esef_filing(), "report.xhtml", checksum).startswith(
        f"bronze/esef/filings/10000000001/{ESEF_KEY}/sha256={checksum}/"
    )
    sec = FilingRef(
        accession="0000320193-25-000079",
        cik=320193,
        company_name="Apple Inc.",
        form="10-K",
        filed_date=date(2025, 10, 31),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
    )
    assert filing_package_key(sec, checksum) == (
        f"bronze/sec/filings/320193/0000320193-25-000079/packages/sha256={checksum}/package.zip"
    )


# AC-6: the SEC downloader refuses a non-SEC filing before any request.
async def test_ac6_sec_downloader_refuses_other_sources(tmp_path) -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import UnsupportedSourceError
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
    from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader
    from sec_xbrl_warehouse.worker import is_retryable_exception

    class NoNetwork:
        """Records any client method the downloader touches."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def __getattr__(self, name):
            async def call(*args, **kwargs):
                self.calls.append(name)
                raise AssertionError("no request expected")

            return call

    http = NoNetwork()
    settings = Settings(_env_file=None, env="test", data_dir=tmp_path)
    downloader = FilingPackageDownloader(settings, http, LocalObjectStore(tmp_path / "objects"))  # type: ignore[arg-type]
    with pytest.raises(UnsupportedSourceError) as raised:
        await downloader.download_package(esef_filing())
    assert http.calls == []
    assert not is_retryable_exception(raised.value)


# AC-7: API routes accept non-SEC filing keys.
def test_ac7_api_accepts_non_sec_filing_keys() -> None:
    from sec_xbrl_warehouse.api.app import create_app
    from sec_xbrl_warehouse.api.dependencies import get_query_service
    from sec_xbrl_warehouse.query.service import FactQuery, QueryService

    class Warehouse:
        def __init__(self) -> None:
            self.queries: list = []

        def query(self, sql, parameters=None):
            self.queries.append((sql, parameters))
            return []

    assert FactQuery(accession=ESEF_KEY).accession == ESEF_KEY
    warehouse = Warehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"/v1/filings/{ESEF_KEY}").status_code == 404
    assert client.get(f"/v1/facts?accession={ESEF_KEY}").status_code == 200
    assert client.get("/v1/filings/esef-a.b%2Fc").status_code in {404, 422}
    assert client.get("/v1/filings/ESEF-abc").status_code == 422
    assert all(ESEF_KEY not in sql for sql, _ in warehouse.queries)
    client.close()


# AC-3: entity keys are allocated once per (scheme, identifier), above every CIK.
@integration
async def test_ac3_entity_keys_are_stable(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    lei = f"TEST{uuid4().hex[:16].upper()}"
    repo = PostgresControlRepository(postgres_settings)
    try:
        assert await repo.resolve_entity_key("http://www.sec.gov/CIK", "0000320193") == 320193
        first = await repo.resolve_entity_key(LEI_SCHEME, lei)
        assert first >= 10_000_000_000
        assert await repo.resolve_entity_key(LEI_SCHEME, lei) == first
        other = await repo.resolve_entity_key(LEI_SCHEME, lei + "X")
        assert other != first and other >= 10_000_000_000
    finally:
        await repo.close()


# AC-4: the control plane stores a non-SEC filing under its full key.
@integration
async def test_ac4_control_plane_stores_long_filing_keys(postgres_settings) -> None:
    from sqlalchemy import text

    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    key = f"esef-{uuid4().hex}-{'Y' * 70}"
    assert len(key) > 100
    repo = PostgresControlRepository(postgres_settings)
    try:
        await repo.upsert_filing(esef_filing(key))
        async with repo.engine.connect() as connection:
            stored = (
                await connection.execute(
                    text("SELECT accession, cik FROM filings WHERE accession = :key"), {"key": key}
                )
            ).one()
        assert tuple(stored) == (key, 10_000_000_001)
    finally:
        await repo.close()
