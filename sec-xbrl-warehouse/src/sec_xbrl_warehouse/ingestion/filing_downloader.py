from __future__ import annotations

import mimetypes
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import (
    FilingPackageManifest,
    FilingRef,
    PackageMember,
)
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes, sha256_file


class FilingPackageDownloader:
    """Acquire a lossless local package for one accession."""

    def __init__(self, settings: Settings, http: SecHttpClient, object_store: ObjectStore) -> None:
        self.settings = settings
        self.http = http
        self.object_store = object_store

    async def fetch_index(self, filing: FilingRef) -> dict[str, Any]:
        """Fetch and validate the accession directory index JSON."""
        return await self.http.get_json(f"{filing.archive_directory_url}/index.json")

    def select_xbrl_entrypoints(
        self, index_payload: dict[str, Any], filing: FilingRef
    ) -> tuple[str, ...]:
        """Return candidate entrypoints in deterministic preference order."""
        candidates = select_package_files(index_payload, filing)
        preferred = [
            name
            for name in candidates
            if name in {filing.instance_document, filing.primary_document}
        ]
        remainder = [name for name in candidates if name not in preferred]
        return tuple(preferred + remainder)

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        """Download all filing files required for offline Arelle processing."""
        index_payload = await self.fetch_index(filing)
        selected = select_package_files(index_payload, filing)
        members: list[PackageMember] = []
        for name in selected:
            url = f"{filing.archive_directory_url}/{name}"
            data = await self.http.get_bytes(url)
            checksum = sha256_bytes(data)
            key = filing_object_key(filing, name, checksum)
            self.object_store.put_bytes(
                data,
                key,
                metadata={"accession": filing.accession, "source-url": url},
            )
            members.append(
                PackageMember(
                    name=name,
                    source_url=url,
                    size_bytes=len(data),
                    content_type=mimetypes.guess_type(name)[0],
                    checksum_sha256=checksum,
                    is_primary_document=name == filing.primary_document,
                    is_xbrl_entrypoint=(
                        name == filing.primary_document or name == filing.instance_document
                    ),
                )
            )
        manifest = FilingPackageManifest(
            filing=filing,
            members=tuple(members),
            acquired_at=datetime.now(UTC),
        )
        manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
        manifest_checksum = sha256_bytes(manifest_bytes)
        manifest_key = f"manifests/sec/filings/{filing.accession}/sha256={manifest_checksum}.json"
        self.object_store.put_bytes(manifest_bytes, manifest_key)
        return manifest.model_copy(
            update={
                "manifest_object_key": manifest_key,
                "manifest_checksum_sha256": manifest_checksum,
            }
        )

    def archive_package(
        self,
        manifest: FilingPackageManifest,
        package_directory: Path,
    ) -> FilingPackageManifest:
        """Create a deterministic lossless archive and update the manifest.

        Member paths are sorted, archive metadata timestamps are normalized, the
        manifest itself is included, and the archive is stored under an immutable
        checksum-qualified key.
        """
        staging = Path(tempfile.mkdtemp(prefix="xbrl-package-", dir=self.settings.temp_dir))
        try:
            # 1. Collect member sources, resolving from object storage when the
            #    caller's directory does not already hold them.
            member_entries: list[tuple[str, Path]] = []
            for member in manifest.members:
                local = package_directory / member.name
                if not local.is_file() and member.checksum_sha256:
                    local = self._materialize_member(manifest.filing, member, staging)
                if not local.is_file():
                    raise ArtifactIntegrityError(
                        f"Cannot locate package member for archiving: {member.name}"
                    )
                if member.checksum_sha256 and sha256_file(local) != member.checksum_sha256:
                    raise ArtifactIntegrityError(f"Checksum mismatch while archiving {member.name}")
                member_entries.append((member.name, local))

            # 2. Write the deterministic archive (fixed timestamps, sorted names).
            archive_path = staging / _package_archive_name(manifest)
            serialized_manifest = manifest.model_dump_json(indent=2).encode("utf-8")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, source in sorted(member_entries, key=lambda item: item[0]):
                    _write_member(zf, name, source.read_bytes())
                _write_member(zf, "manifest.json", serialized_manifest)

            # 3. Checksum and store under an immutable key.
            archive_checksum = sha256_file(archive_path)
            package_key = filing_package_key(manifest.filing, archive_checksum)
            self.object_store.put_file(
                archive_path,
                package_key,
                expected_sha256=archive_checksum,
                metadata={"accession": manifest.filing.accession},
            )
            # 4. Regenerate and store the manifest that records archive lineage.
            updated = manifest.model_copy(
                update={
                    "package_object_key": package_key,
                    "package_checksum_sha256": archive_checksum,
                }
            )
            updated_bytes = updated.model_dump_json(indent=2).encode("utf-8")
            updated_checksum = sha256_bytes(updated_bytes)
            self.object_store.put_bytes(
                updated_bytes,
                f"manifests/sec/filings/{manifest.filing.accession}/sha256={updated_checksum}.json",
            )
            return updated
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path:
        """Materialize all members locally and return the chosen entrypoint."""
        destination.mkdir(parents=True, exist_ok=True)
        for member in manifest.members:
            target = (destination / member.name).resolve()
            root = destination.resolve()
            if root not in target.parents and target != root:
                raise ArtifactIntegrityError(f"Unsafe package member path: {member.name}")
            if member.checksum_sha256 is None:
                raise ArtifactIntegrityError(f"Member {member.name} has no checksum to locate")
            key = filing_object_key(manifest.filing, member.name, member.checksum_sha256)
            stored = self.object_store.get_file(key, target)
            if member.checksum_sha256 and stored.checksum_sha256 != member.checksum_sha256:
                raise ArtifactIntegrityError(
                    f"Materialized member checksum mismatch: {member.name}"
                )
        return destination / _choose_entrypoint(manifest)

    def _materialize_member(self, filing: FilingRef, member: PackageMember, staging: Path) -> Path:
        if member.checksum_sha256 is None:
            raise ArtifactIntegrityError(f"Member {member.name} has no checksum to locate")
        key = filing_object_key(filing, member.name, member.checksum_sha256)
        destination = staging / member.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.object_store.get_file(key, destination)
        return destination


def select_package_files(index_payload: dict[str, Any], filing: FilingRef) -> tuple[str, ...]:
    """Conservatively select likely XBRL package files from EDGAR index JSON.

    Includes the primary document plus XML/XSD/XBRL and obvious linkbase/inline
    HTML files. Reference chasing can enrich the selection later.
    """
    items = index_payload.get("directory", {}).get("item", [])
    names = [str(item.get("name")) for item in items if item.get("name")]
    selected: set[str] = set()
    for name in names:
        suffix = Path(name).suffix.lower()
        lowered = name.lower()
        if (
            name == filing.primary_document
            or name == filing.instance_document
            or suffix in {".xsd", ".xml", ".xbrl"}
            or (
                suffix in {".htm", ".html"}
                and ("_htm" in lowered or "inline" in lowered or "ix" in lowered)
            )
            or lowered in {"filingsummary.xml", "metalink.json"}
        ):
            selected.add(name)
    if filing.primary_document:
        selected.add(filing.primary_document)
    return tuple(sorted(selected))


def filing_object_key(filing: FilingRef, filename: str, checksum: str) -> str:
    safe_name = Path(filename).name
    return f"bronze/sec/filings/{filing.cik}/{filing.accession}/sha256={checksum}/{safe_name}"


def filing_package_key(filing: FilingRef, archive_checksum: str) -> str:
    return (
        f"bronze/sec/filings/{filing.cik}/{filing.accession}/"
        f"packages/sha256={archive_checksum}/package.zip"
    )


def _write_member(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name)
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def _package_archive_name(manifest: FilingPackageManifest) -> str:
    return f"{manifest.filing.accession}.zip"


def _choose_entrypoint(manifest: FilingPackageManifest) -> str:
    """Deterministic entrypoint: instance doc, then primary, then any XBRL XML."""
    if manifest.filing.instance_document:
        return manifest.filing.instance_document
    if manifest.filing.primary_document:
        return manifest.filing.primary_document
    preferred = [
        member.name
        for member in manifest.members
        if member.is_xbrl_entrypoint or member.name.endswith(".xml")
    ]
    if preferred:
        return sorted(preferred)[0]
    raise ArtifactIntegrityError(f"No XBRL entrypoint found in {manifest.filing.accession}")
