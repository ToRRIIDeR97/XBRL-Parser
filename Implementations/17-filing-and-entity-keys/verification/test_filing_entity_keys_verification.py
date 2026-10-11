"""Independent verification tests for 17-filing-and-entity-keys."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sec_xbrl_warehouse.domain.models import FilingRef

LEI_SCHEME = "http://standards.iso.org/iso/17442"
integration = pytest.mark.skipif(
    os.getenv("RUN_XBRL_INTEGRATION") != "1", reason="Set RUN_XBRL_INTEGRATION=1"
)


def filing(key: str, entity_key: int) -> FilingRef:
    return FilingRef(
        accession=key,
        cik=entity_key,
        company_name="Example",
        form="AFR",
        filed_date=date(2024, 4, 2),
        archive_directory_url="https://example.invalid/filing",
    )


# AC-1: length and character boundaries.
def test_ac1_boundaries() -> None:
    from sec_xbrl_warehouse.domain.sources import filing_source

    longest = "edinet-" + "a" * (128 - len("edinet-"))
    assert len(longest) == 128 and filing_source(longest) == "edinet"
    assert filing_source("esef-ABC_def.123-x") == "esef"
    assert filing_source("in2-L12345") == "in2"
    for bad in (
        "e-abc",  # source shorter than two characters
        "abcdefghijklmnopq-1",  # source longer than sixteen characters
        "esef-abc def",
        "esef-abcé",
        "0000320193-25-000079\n",
        " 0000320193-25-000079",
        "esef_abc",
        "",
    ):
        with pytest.raises(ValueError):
            filing_source(bad)


# AC-2: the entity-key ranges meet exactly at ENTITY_KEY_BASE.
def test_ac2_range_edges() -> None:
    from sec_xbrl_warehouse.domain.sources import ENTITY_KEY_BASE

    assert filing("dart-20240312000736", ENTITY_KEY_BASE).cik == ENTITY_KEY_BASE
    with pytest.raises(ValidationError):
        filing("dart-20240312000736", ENTITY_KEY_BASE - 1)
    sec = FilingRef(
        accession="9999999999-25-000001",
        cik=ENTITY_KEY_BASE - 1,
        company_name="Largest CIK",
        form="10-K",
        filed_date=date(2025, 1, 1),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/9999999999/999999999925000001",
    )
    assert sec.cik == ENTITY_KEY_BASE - 1


# AC-5: storage inventory recognizes non-SEC packages and loose members.
def test_ac5_inventory_classifies_other_sources() -> None:
    from sec_xbrl_warehouse.infrastructure.storage.inventory import classify

    digest = "a" * 64
    assert classify(f"bronze/esef/filings/10000000007/esef-X1/packages/sha256={digest}/package.zip") == "bronze_packages"
    assert classify(f"bronze/dart/filings/10000000008/dart-1/sha256={digest}/a.xbrl") == "bronze_loose_members"
    assert classify(f"bronze/sec/filings/320193/0000320193-25-000079/packages/sha256={digest}/package.zip") == "bronze_packages"


# AC-5: retire-revisions treats a non-SEC filing's saved extraction like an SEC one.
def test_ac5_retire_revisions_handles_non_sec_keys() -> None:
    from sec_xbrl_warehouse.infrastructure.storage.maintenance import retire_superseded_revisions

    key = "esef-5493001KJTIIGC8Y1R12-2023-12-31-ESEF-GB-0"

    class Warehouse:
        def query(self, sql, parameters=None):
            if "max(extraction_revision)" in sql:
                return [{"accession": key, "revision": 2}]
            return [{"accession": key, "revision": 1, "n": 3}, {"accession": key, "revision": 2, "n": 3}]

        def command(self, sql, parameters=None):
            raise AssertionError("dry run must not write")

    class Store:
        def list_objects(self, prefix=""):
            if prefix.startswith(f"silver/xbrl/extractions/accession={key}/revision=1/"):
                yield prefix + "sha256=abc.json.zst", 10

    report = retire_superseded_revisions(Warehouse(), Store())
    assert report["retired_revisions"] == [{"accession": key, "revision": 1}], report
    assert report["kept_without_saved_extraction"] == []


# AC-6: refusal comes before any client use, even on a parse-only worker.
async def test_ac6_refusal_precedes_client_checks(tmp_path) -> None:
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import UnsupportedSourceError
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
    from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path)
    downloader = FilingPackageDownloader(settings, None, LocalObjectStore(tmp_path / "o"))
    target = filing("edinet-S100TR7I", 10_000_000_009)
    with pytest.raises(UnsupportedSourceError):
        await downloader.download_package(target)
    with pytest.raises(UnsupportedSourceError):
        await downloader.fetch_index(target)
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").rglob("*"))


# AC-7: invalid keys are rejected by the API without a query.
def test_ac7_api_rejects_malformed_keys_without_query() -> None:
    from sec_xbrl_warehouse.api.app import create_app
    from sec_xbrl_warehouse.api.dependencies import get_query_service
    from sec_xbrl_warehouse.query.service import QueryService

    class Warehouse:
        def __init__(self) -> None:
            self.queries: list = []

        def query(self, sql, parameters=None):
            self.queries.append(sql)
            return []

    warehouse = Warehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    for path in (
        "/v1/filings/e-abc",
        "/v1/filings/sec-0000320193-25-000079",
        "/v1/facts?accession=esef-a%20b",
        "/v1/facts?accession=" + "esef-" + "x" * 124,
    ):
        assert client.get(path).status_code == 422, path
    assert warehouse.queries == []
    assert client.get("/v1/filings/dart-20240312000736").status_code == 404
    client.close()


# AC-3: concurrent first use, restart, scheme separation, and bad identifiers.
@integration
async def test_ac3_concurrent_restart_and_schemes(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    identifier = f"LEI{uuid4().hex[:17].upper()}"
    repos = [PostgresControlRepository(postgres_settings) for _ in range(5)]
    try:
        keys = await asyncio.gather(
            *(repo.resolve_entity_key(LEI_SCHEME, identifier) for repo in repos for _ in range(2))
        )
        assert len(set(keys)) == 1 and keys[0] >= 10_000_000_000
        other_scheme = await repos[0].resolve_entity_key("urn:example:company-number", identifier)
        assert other_scheme != keys[0]
        for scheme, bad in (
            ("http://www.sec.gov/CIK", "ABC"),
            ("http://www.sec.gov/CIK", "12345678901"),
            (LEI_SCHEME, ""),
            ("", identifier),
        ):
            with pytest.raises(ValueError):
                await repos[0].resolve_entity_key(scheme, bad)
    finally:
        for repo in repos:
            await repo.close()
    fresh = PostgresControlRepository(postgres_settings)
    try:
        assert await fresh.resolve_entity_key(LEI_SCHEME, identifier) == keys[0]
    finally:
        await fresh.close()


# AC-4: every accession column in the control plane holds a 128-character key.
@integration
async def test_ac4_all_accession_columns_are_widened(postgres_settings) -> None:
    from sqlalchemy import text

    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository

    repo = PostgresControlRepository(postgres_settings)
    try:
        async with repo.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT table_name, character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'accession'"
                    )
                )
            ).all()
        assert rows, "expected accession columns"
        assert all(length is None or length >= 128 for _, length in rows), rows
        key = "esef-" + "z" * 123
        await repo.upsert_filing(filing(key, 10_000_000_010))
        await repo.upsert_filing(filing(key, 10_000_000_010))  # idempotent
        assert await repo.get_filing_manifest(key) is None
    finally:
        await repo.close()
