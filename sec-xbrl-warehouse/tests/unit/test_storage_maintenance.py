import asyncio
from datetime import UTC, date, datetime
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


def test_source_cleanup_requires_exact_package_and_verified_replay(tmp_path: Path) -> None:
    import pytest

    from sec_xbrl_warehouse.domain.models import FilingPackageManifest
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
    from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes
    from sec_xbrl_warehouse.infrastructure.storage.maintenance import delete_filing_source
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_package_key

    settings = Settings(
        _env_file=None,
        env="test",
        retain_filing_source_files=False,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
    )
    store = LocalObjectStore(tmp_path)
    filing = FilingRef(
        accession=A,
        cik=1,
        company_name="Example",
        form="10-K",
        filed_date=date(2026, 1, 1),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    checksum = sha256_bytes(b"package")
    key = filing_package_key(filing, checksum)
    store.put_bytes(b"package", key)
    manifest = FilingPackageManifest(
        filing=filing,
        acquired_at=datetime.now(UTC),
        members=(),
        package_object_key=key,
        package_checksum_sha256=checksum,
    )
    snapshot = store.put_bytes(
        b"saved extraction",
        f"silver/xbrl/extractions/accession={A}/revision=1/sha256={sha256_bytes(b'saved extraction')}.json.zst",
    )
    extraction = {"object_key": snapshot.key, "sha256": snapshot.checksum_sha256}
    assert delete_filing_source(store, settings, manifest, extraction, 1)
    assert store.exists(key)
    for bad in (
        manifest.model_copy(update={"package_object_key": snapshot.key}),
        manifest.model_copy(update={"package_checksum_sha256": "bad"}),
    ):
        with pytest.raises(ArtifactIntegrityError):
            delete_filing_source(store, settings, bad, extraction, 1, apply=True)
    with pytest.raises(ArtifactIntegrityError):
        delete_filing_source(store, settings, manifest, extraction, 2, apply=True)
    with pytest.raises(ValueError, match="false"):
        delete_filing_source(
            store,
            settings.model_copy(update={"retain_filing_source_files": True}),
            manifest,
            extraction,
            1,
            apply=True,
        )
    assert store.exists(key) and store.exists(snapshot.key)
    replay_path = store.bucket_root / snapshot.key
    replay_path.write_bytes(b"corrupted extraction")
    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        delete_filing_source(store, settings, manifest, extraction, 1, apply=True)
    assert store.exists(key)
    replay_path.write_bytes(b"saved extraction")
    assert delete_filing_source(store, settings, manifest, extraction, 1, apply=True)
    assert not store.exists(key) and store.exists(snapshot.key)
    assert not delete_filing_source(store, settings, manifest, extraction, 1, apply=True)


async def test_source_cleanup_only_includes_completed_published_packages(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.domain.models import FilingPackageManifest
    from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes
    from sec_xbrl_warehouse.infrastructure.storage.maintenance import prune_filing_sources
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_package_key

    settings = Settings(
        _env_file=None,
        env="test",
        retain_filing_source_files=False,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
    )
    store = LocalObjectStore(tmp_path)
    items = []
    for accession in (A, B):
        filing = FilingRef(
            accession=accession,
            cik=1,
            company_name="Example",
            form="10-K",
            filed_date=date(2026, 1, 1),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        )
        checksum = sha256_bytes(b"package")
        key = filing_package_key(filing, checksum)
        store.put_bytes(b"package", key)
        snapshot_key = (
            f"silver/xbrl/extractions/accession={accession}/revision=1/sha256={checksum}.json.zst"
        )
        store.put_bytes(b"package", snapshot_key)
        items.append(
            {
                "package": FilingPackageManifest(
                    filing=filing,
                    acquired_at=datetime.now(UTC),
                    members=(),
                    package_object_key=key,
                    package_checksum_sha256=checksum,
                ).model_dump(mode="json"),
                "extract": {"object_key": snapshot_key, "sha256": checksum},
                "complete": {
                    "status": "complete",
                    "accession": accession,
                    "extraction_revision": 1,
                },
            }
        )

    class Control:
        busy = False

        async def completed_filing_sources(self):
            return [*items, items[0]]

        async def source_package_in_use(self, accession):
            return self.busy

    class Warehouse:
        def query(self, *args):
            return [{"accession": A, "revision": 1}]

    control = Control()
    dry = await prune_filing_sources(control, Warehouse(), store, settings)
    assert len(dry["packages"]) == 1 and dry["bytes"] == 7 and dry["dry_run"]
    control.busy = True
    assert not (await prune_filing_sources(control, Warehouse(), store, settings, apply=True))[
        "packages"
    ]
    control.busy = False
    done = await prune_filing_sources(control, Warehouse(), store, settings, apply=True)
    assert len(done["packages"]) == 1
    assert store.exists(items[1]["package"]["package_object_key"])
