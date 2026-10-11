"""Source-neutral filing keys and entity keys (plan 17)."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.sources import ENTITY_KEY_BASE, filing_source
from sec_xbrl_warehouse.exceptions import UnsupportedSourceError
from sec_xbrl_warehouse.infrastructure.storage.inventory import classify
from sec_xbrl_warehouse.infrastructure.storage.maintenance import retire_superseded_revisions
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    filing_object_key,
    filing_package_key,
)
from sec_xbrl_warehouse.query.service import FactQuery
from sec_xbrl_warehouse.worker import is_retryable_exception

SEC_KEY = "0000320193-25-000079"
ESEF_KEY = "esef-5493001KJTIIGC8Y1R12-2023-12-31-ESEF-GB-0"
CHECKSUM = "a" * 64


def _filing(key: str = ESEF_KEY, cik: int = ENTITY_KEY_BASE + 1) -> FilingRef:
    return FilingRef(
        accession=key,
        cik=cik,
        company_name="Example",
        form="AFR",
        filed_date=date(2024, 4, 2),
        archive_directory_url="https://example.invalid/filing",
    )


@pytest.mark.parametrize(
    ("key", "source"),
    [
        (SEC_KEY, "sec"),
        (ESEF_KEY, "esef"),
        ("se-1", "se"),
        ("sea-1", "sea"),
        ("secx-1", "secx"),
        ("s1-1", "s1"),
        ("a" * 16 + "-x", "a" * 16),
        ("esef-" + "x" * 123, "esef"),  # exactly 128 characters
        ("edinet-a.b_c-D", "edinet"),
    ],
)
def test_filing_source_accepts_both_key_forms(key: str, source: str) -> None:
    assert filing_source(key) == source


@pytest.mark.parametrize(
    "key",
    [
        "",
        "sec-1",
        "sec-0000320193-25-000079",
        "a-1",  # source too short
        "a" * 17 + "-1",  # source too long
        "1sef-1",
        "esef-" + "x" * 124,  # 129 characters
        "esef-.hidden",
        "esef-_x",
        "esef-a b",
        "esef-a\n",
        f"{SEC_KEY}\n",
        "esef-a'b",
        "esef-a\\b",
        "esef-é",
        "esef-a/b",
        "٠٠٠٠٣٢٠١٩٣-25-000079",  # non-ASCII digits
        "esef_x-1",
    ],
)
def test_filing_source_rejects_malformed_keys(key: str) -> None:
    with pytest.raises(ValueError):
        filing_source(key)


def test_filing_ref_entity_key_range_edges() -> None:
    assert _filing(cik=ENTITY_KEY_BASE).cik == ENTITY_KEY_BASE
    assert _filing(SEC_KEY, ENTITY_KEY_BASE - 1).cik == 9_999_999_999
    with pytest.raises(ValidationError):
        _filing(cik=ENTITY_KEY_BASE - 1)
    with pytest.raises(ValidationError):
        _filing(SEC_KEY, ENTITY_KEY_BASE)
    with pytest.raises(ValidationError):
        _filing("sec-1", ENTITY_KEY_BASE)


def test_object_keys_use_the_source_and_inventory_classifies_both() -> None:
    sec = _filing(SEC_KEY, 320193)
    esef = _filing()
    assert filing_package_key(sec, CHECKSUM) == (
        f"bronze/sec/filings/320193/{SEC_KEY}/packages/sha256={CHECKSUM}/package.zip"
    )
    assert filing_object_key(sec, "a.htm", CHECKSUM) == (
        f"bronze/sec/filings/320193/{SEC_KEY}/sha256={CHECKSUM}/a.htm"
    )
    for filing in (sec, esef):
        assert classify(filing_package_key(filing, CHECKSUM)) == "bronze_packages"
        assert classify(filing_object_key(filing, "a.htm", CHECKSUM)) == "bronze_loose_members"
    assert filing_package_key(esef, CHECKSUM).startswith(
        f"bronze/esef/filings/{ENTITY_KEY_BASE + 1}/{ESEF_KEY}/"
    )


def test_downloader_refuses_non_sec_filings_without_a_client(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, env="test", data_dir=tmp_path)
    downloader = FilingPackageDownloader(settings, None, LocalObjectStore(tmp_path))
    for call in (downloader.fetch_index, downloader.download_package):
        with pytest.raises(UnsupportedSourceError) as raised:
            asyncio.run(call(_filing()))
        assert not is_retryable_exception(raised.value)


def test_retire_revisions_accepts_non_sec_keys(tmp_path: Path) -> None:
    class Warehouse:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def query(self, sql, parameters=None):
            if "FROM xbrl_extractions" in sql:
                return [{"accession": ESEF_KEY, "revision": 2}, {"accession": "x'y", "revision": 2}]
            if "FROM xbrl_facts " in sql:
                return [
                    {"accession": ESEF_KEY, "revision": 1, "n": 4},
                    {"accession": "x'y", "revision": 1, "n": 4},
                ]
            return []

        def command(self, sql, parameters=None):
            self.commands.append(sql)

    store = LocalObjectStore(tmp_path)
    for key in (ESEF_KEY, "x'y"):
        store.put_bytes(b"{}", f"silver/xbrl/extractions/accession={key}/revision=1/s.json.zst")
    warehouse = Warehouse()
    report = retire_superseded_revisions(warehouse, store, apply=True)
    assert report["retired_revisions"] == [{"accession": ESEF_KEY, "revision": 1}]
    assert report["kept_without_saved_extraction"] == [{"accession": "x'y", "revision": 1}]
    assert warehouse.commands == [
        "ALTER TABLE xbrl_facts DELETE WHERE (accession, extraction_revision) "
        f"IN (('{ESEF_KEY}', 1)) SETTINGS mutations_sync = 1"
    ]


def test_api_rejects_malformed_keys_without_a_query() -> None:
    from sec_xbrl_warehouse.api.app import create_app
    from sec_xbrl_warehouse.api.dependencies import get_query_service
    from sec_xbrl_warehouse.query.service import QueryService

    class Warehouse:
        def __init__(self) -> None:
            self.queries: list = []

        def query(self, sql, parameters=None):
            self.queries.append((sql, parameters))
            return []

    assert FactQuery(accession=ESEF_KEY).accession == ESEF_KEY
    with pytest.raises(ValidationError):
        FactQuery(accession="sec-1")
    warehouse = Warehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    with TestClient(app, raise_server_exceptions=False) as client:
        for key in ("sec-1", "ESEF-1", "esef-" + "x" * 124, "esef-a%20b", "esef-a%27b"):
            for suffix in ("", "/cover-page", "/segments", "/text-blocks"):
                assert client.get(f"/v1/filings/{key}{suffix}").status_code == 422
            assert client.get(f"/v1/facts?accession={key}").status_code == 422
        assert warehouse.queries == []
        for suffix in ("/cover-page", "/segments", "/text-blocks"):
            assert client.get(f"/v1/filings/{ESEF_KEY}{suffix}").status_code in {200, 404}
        assert all(parameters.get("accession") == ESEF_KEY for _, parameters in warehouse.queries)
