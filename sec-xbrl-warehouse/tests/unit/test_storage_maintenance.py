import asyncio
from datetime import date
from pathlib import Path

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file
from sec_xbrl_warehouse.infrastructure.storage.maintenance import (
    recompress_extraction_checkpoints,
    retire_superseded_revisions,
)
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle, write_json

A, B = "0000000001-26-000001", "0000000001-26-000002"


class _Warehouse:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def query(self, sql, parameters=None):
        if "FROM xbrl_extractions" in sql:
            return [{"accession": A, "revision": 3}, {"accession": B, "revision": 2}]
        if "FROM xbrl_facts " in sql:
            return [
                {"accession": A, "revision": 1, "n": 10},  # superseded, saved: retired
                {"accession": A, "revision": 3, "n": 12},  # published: kept
                {"accession": B, "revision": 1, "n": 5},  # superseded, not saved: kept
                {"accession": B, "revision": 4, "n": 7},  # unpublished: kept
            ]
        return []

    def command(self, sql, parameters=None):
        self.commands.append(sql)


def test_retire_deletes_only_saved_superseded_revisions(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    store.put_bytes(b"{}", f"silver/xbrl/extractions/accession={A}/revision=1/sha256=x.json.zst")
    warehouse = _Warehouse()

    dry = retire_superseded_revisions(warehouse, store)
    assert dry["dry_run"] and warehouse.commands == []
    assert dry["retired_revisions"] == [{"accession": A, "revision": 1}]
    assert dry["kept_without_saved_extraction"] == [{"accession": B, "revision": 1}]
    assert dry["rows"] == {"xbrl_facts": 10}

    retire_superseded_revisions(warehouse, store, apply=True)
    assert warehouse.commands == [
        "ALTER TABLE xbrl_facts DELETE WHERE (accession, extraction_revision) "
        f"IN (('{A}', 1)) SETTINGS mutations_sync = 1"
    ]


def test_recompress_rewrites_json_as_zstd_and_repoints(tmp_path: Path) -> None:
    settings = Settings(env="test", data_dir=tmp_path / "data", temp_dir=tmp_path / "tmp")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    bundle = XbrlExtractionBundle(
        filing=FilingRef(
            accession=A,
            cik=1,
            company_name="Example Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="example.xml",
            document_type="instance",
            taxonomy_namespaces=(),
            source_documents=("example.xml",),
            software_name="Arelle",
            software_version="test",
        ),
        contexts=(),
        concepts=(),
        facts=(),
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )
    plain = tmp_path / "plain.json"
    write_json(bundle, plain, 1_000_000)
    sha = sha256_file(plain)
    old_key = (
        f"silver/xbrl/extractions/accession={bundle.filing.accession}/revision=1/sha256={sha}.json"
    )
    store.put_file(plain, old_key, expected_sha256=sha)

    class _Control:
        def __init__(self) -> None:
            self.pointers = [{"object_key": old_key, "sha256": sha}]

        async def legacy_extraction_pointers(self):
            return list(self.pointers)

        async def repoint_extraction_checkpoints(self, old, new, new_sha):
            self.pointers = [p for p in self.pointers if p["object_key"] != old]
            self.repointed = (old, new, new_sha)
            return 2

    control = _Control()
    dry = asyncio.run(recompress_extraction_checkpoints(control, store, settings))
    assert dry["dry_run"] and dry["legacy_objects"] == 1 and dry["rewritten"] == 0

    report = asyncio.run(
        recompress_extraction_checkpoints(control, store, settings, apply=True, delete_legacy=True)
    )
    assert report["rewritten"] == 1 and report["checkpoints_repointed"] == 2
    assert report["deleted"] == 1 and not store.exists(old_key)
    _, new_key, new_sha = control.repointed
    assert new_key.endswith(f"sha256={new_sha}.json.zst")
    restored = tmp_path / "restored.json.zst"
    store.get_file(new_key, restored)
    assert read_bundle(restored, 1_000_000) == bundle
