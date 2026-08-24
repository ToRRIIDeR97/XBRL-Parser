from __future__ import annotations

import zipfile
from pathlib import Path

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import DatasetArtifact, StoredObject
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient


class BulkArtifactDownloader:
    """Acquire SEC bulk ZIPs into immutable bronze storage."""

    def __init__(
        self,
        settings: Settings,
        http: SecHttpClient,
        object_store: ObjectStore,
    ) -> None:
        self.settings = settings
        self.http = http
        self.object_store = object_store

    async def download(self, artifact: DatasetArtifact) -> tuple[StoredObject, dict[str, str]]:
        """Download, checksum, test ZIP integrity, and archive one artifact.

        If the object key for the artifact already exists (matching the catalog
        checksum when known), no network transfer occurs. Otherwise the download
        is resumable: the ETag/Last-Modified headers from the prior attempt are
        sent as conditional headers, so an unchanged server object is not
        re-transferred.
        """
        self.settings.ensure_directories()
        destination = self.settings.temp_dir / artifact.filename

        if artifact.checksum_sha256:
            key = bronze_bulk_key(artifact, artifact.checksum_sha256)
            if self.object_store.exists(key):
                stored = self.object_store.get_file(key, destination)
                return stored, {"source": "object-store"}

        _, sha256, _, headers = await self.http.download(
            artifact.source_url,
            destination,
            expected_sha256=artifact.checksum_sha256,
        )
        validate_zip_integrity(destination)
        key = bronze_bulk_key(artifact, sha256)
        stored = self.object_store.put_file(
            destination,
            key,
            expected_sha256=sha256,
            metadata={
                "source-url": artifact.source_url,
                "source-period": artifact.period,
                "dataset": artifact.dataset.value,
            },
        )
        destination.unlink(missing_ok=True)
        return stored, headers


def validate_zip_integrity(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names:
                raise ArtifactIntegrityError(f"ZIP contains no members: {path}")
            normalized: set[str] = set()
            for name in names:
                member = Path(name)
                if member.is_absolute() or ".." in member.parts:
                    raise ArtifactIntegrityError(f"Unsafe ZIP member: {name}")
                lowered = name.lower()
                if lowered in normalized:
                    raise ArtifactIntegrityError(f"Duplicate ZIP member ignoring case: {name}")
                normalized.add(lowered)
            bad_member = archive.testzip()
            if bad_member:
                raise ArtifactIntegrityError(f"Corrupt ZIP member: {bad_member}")
    except zipfile.BadZipFile as exc:
        raise ArtifactIntegrityError(f"Invalid ZIP file: {path}") from exc


def bronze_bulk_key(artifact: DatasetArtifact, sha256: str) -> str:
    return (
        f"bronze/sec/fsnds/{artifact.dataset.value}/{artifact.period}/"
        f"sha256={sha256}/{artifact.filename}"
    )
