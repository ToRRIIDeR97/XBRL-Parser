from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pydantic import ValidationError

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import DatasetArtifact
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.bulk_downloader import (
    BulkArtifactDownloader,
    validate_zip_integrity,
)


def artifact(filename="source.zip"):
    return DatasetArtifact(
        dataset="notes",
        period="2026q1",
        source_url="https://www.sec.gov/file",
        filename=filename,
        published_label="Test",
    )


@pytest.mark.parametrize(
    "name",
    [
        "../escape.zip",
        "/tmp/escape.zip",
        "..\\escape.zip",
        "C:escape.zip",
        "CON",
        ".",
        "bad\x00name",
        "file.zip ",
    ],
)
def test_artifact_rejects_unsafe_names(name):
    with pytest.raises(ValidationError):
        artifact(name)


def test_zip_limits_are_checked_before_decompression(tmp_path, monkeypatch):
    path = tmp_path / "source.zip"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("num.txt", b"0" * (1024 * 1024))
    settings = Settings(_env_file=None, env="test")
    with pytest.raises(ArtifactIntegrityError, match="compression ratio"):
        validate_zip_integrity(path, settings)
    with pytest.raises(ArtifactIntegrityError, match="expanded byte"):
        validate_zip_integrity(path, settings.model_copy(update={"zip_max_total_bytes": 5}))
    with ZipFile(path, "w") as archive:
        archive.writestr("a.txt", b"a")
        archive.writestr("b.txt", b"b")

    def no_read(*args, **kwargs):
        pytest.fail("Oversized archive should be rejected before decompressing")

    monkeypatch.setattr(ZipFile, "open", no_read)
    with pytest.raises(ArtifactIntegrityError, match="member count"):
        validate_zip_integrity(path, settings.model_copy(update={"zip_max_members": 1}))


async def test_bulk_staging_isolated_and_bad_source_retained(tmp_path):
    settings = Settings(
        _env_file=None, env="test", data_dir=tmp_path / "data", temp_dir=tmp_path / "tmp"
    )
    paths = []

    class Http:
        async def download(self, url, destination, **kwargs):
            paths.append(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"not a zip")
            return destination, sha256_file(destination), 9, {}

    store = LocalObjectStore(tmp_path / "objects", "raw")
    downloader = BulkArtifactDownloader(settings, Http(), store)
    for source in (
        artifact(),
        artifact().model_copy(update={"source_url": "https://www.sec.gov/other"}),
    ):
        with pytest.raises(ArtifactIntegrityError, match="Invalid ZIP"):
            await downloader.download(source)
    assert paths[0] != paths[1]
    assert all(path.is_relative_to(settings.temp_dir.resolve()) for path in paths)
    assert list(store.bucket_root.rglob("source.zip"))


def test_zip_directory_budget_applies_before_entry_allocation(tmp_path):
    path = tmp_path / "large-directory.zip"
    with ZipFile(path, "w") as archive:
        for i in range(18):
            archive.writestr(str(i) + "x" * 60000, b"")
    settings = Settings(_env_file=None, env="test", zip_max_directory_bytes=1024 * 1024)
    with pytest.raises(ArtifactIntegrityError, match="central directory"):
        validate_zip_integrity(path, settings)
