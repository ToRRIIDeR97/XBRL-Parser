from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import DatasetArtifact, StoredObject
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
from sec_xbrl_warehouse.infrastructure.storage.hashing import stable_json_hash


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
        filename = DatasetArtifact.safe_filename(artifact.filename)
        identity = stable_json_hash(artifact.model_dump(mode="json"))
        destination = (self.settings.temp_dir / "bulk-downloads" / identity / filename).resolve()
        if not destination.is_relative_to(self.settings.temp_dir.resolve()):
            raise ArtifactIntegrityError("Bulk destination escapes the temporary directory")

        if artifact.checksum_sha256:
            key = bronze_bulk_key(artifact, artifact.checksum_sha256)
            if await asyncio.to_thread(self.object_store.exists, key):
                stored = await asyncio.to_thread(self.object_store.get_file, key, destination)
                if stored.checksum_sha256 != artifact.checksum_sha256:
                    raise ArtifactIntegrityError("Cached bulk checksum mismatch")
                await asyncio.to_thread(validate_zip_integrity, destination, self.settings)
                return stored, {"source": "object-store"}

        _, sha256, _, headers = await self.http.download(
            artifact.source_url,
            destination,
            expected_sha256=artifact.checksum_sha256,
        )
        key = bronze_bulk_key(artifact, sha256)
        # Archive source bytes before validation so malformed inputs remain recoverable.
        stored = await asyncio.to_thread(
            self.object_store.put_file,
            destination,
            key,
            expected_sha256=sha256,
            metadata={
                "source-url": artifact.source_url,
                "source-period": artifact.period,
                "dataset": artifact.dataset.value,
            },
        )
        await asyncio.to_thread(validate_zip_integrity, destination, self.settings)
        destination.unlink(missing_ok=True)
        return stored, headers


class _ZipInput(io.BufferedReader):
    """Bound central-directory allocation before ZipFile materializes its entries."""

    def __init__(self, path: Path, max_read_bytes: int) -> None:
        super().__init__(io.FileIO(path, "r"))
        self.max_read_bytes = max_read_bytes

    def read(self, size: int | None = -1) -> bytes:
        requested = (
            os.fstat(self.fileno()).st_size - self.tell() if size is None or size < 0 else size
        )
        if requested > self.max_read_bytes:
            raise ArtifactIntegrityError("ZIP central directory exceeds configured byte limit")
        return super().read(size)


def validate_zip_integrity(path: Path, settings: Settings | None = None) -> None:
    settings = settings or Settings()
    try:
        if path.stat().st_size > settings.download_max_bytes:
            raise ArtifactIntegrityError("ZIP exceeds configured compressed byte limit")
        with (
            _ZipInput(path, settings.zip_max_directory_bytes) as source,
            zipfile.ZipFile(source) as archive,
        ):
            members = archive.infolist()
            if not members:
                raise ArtifactIntegrityError(f"ZIP contains no members: {path}")
            if len(members) > settings.zip_max_members:
                raise ArtifactIntegrityError("ZIP exceeds configured member count")
            normalized: set[str] = set()
            total = 0
            for info in members:
                name = info.filename
                member = PurePosixPath(name)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or "\\" in name
                    or PureWindowsPath(name).drive
                ):
                    raise ArtifactIntegrityError(f"Unsafe ZIP member: {name}")
                lowered = name.lower()
                if lowered in normalized:
                    raise ArtifactIntegrityError(f"Duplicate ZIP member ignoring case: {name}")
                normalized.add(lowered)
                total += info.file_size
                if (
                    info.file_size > settings.zip_max_member_bytes
                    or total > settings.zip_max_total_bytes
                ):
                    raise ArtifactIntegrityError("ZIP exceeds configured expanded byte limit")
                if info.file_size > max(1, info.compress_size) * settings.zip_max_compression_ratio:
                    raise ArtifactIntegrityError("ZIP exceeds configured compression ratio")
            total = 0
            for info in members:
                count = 0
                with archive.open(info) as member_source:
                    while chunk := member_source.read(1024 * 1024):
                        count += len(chunk)
                        total += len(chunk)
                        if (
                            count > settings.zip_max_member_bytes
                            or total > settings.zip_max_total_bytes
                        ):
                            raise ArtifactIntegrityError(
                                "ZIP exceeds configured expanded byte limit"
                            )
                if count != info.file_size:
                    raise ArtifactIntegrityError(f"ZIP member size mismatch: {info.filename}")
    except zipfile.BadZipFile as exc:
        raise ArtifactIntegrityError(f"Invalid ZIP file: {path}") from exc


def bronze_bulk_key(artifact: DatasetArtifact, sha256: str) -> str:
    return (
        f"bronze/sec/fsnds/{artifact.dataset.value}/{artifact.period}/"
        f"sha256={sha256}/{artifact.filename}"
    )
