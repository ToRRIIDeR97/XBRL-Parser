from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
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
from sec_xbrl_warehouse.ingestion.bulk_downloader import validate_zip_integrity

# Bump when `select_package_files` changes which files a package contains. The
# version is part of the manifest cache key, so manifests cached under an
# older policy stay in place but are not reused.
PACKAGE_SELECTION_POLICY_VERSION = 2
_FILING_SUMMARY = "filingsummary.xml"
_RENDERED_REPORT = re.compile(r"^r\d+\.(?:htm|html|xml)$")
_INDEX_PAGE = re.compile(r"-index(?:-headers)?\.html?$")
_LINKBASE_SUFFIXES = ("_cal", "_def", "_lab", "_pre", "_ref")
_ARCHIVE_MANIFEST = "manifest.json"


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
        """Download the filing files required for offline Arelle processing.

        `FilingSummary.xml`, when listed, is fetched first because its
        `<InputFiles>` decide which extra `.htm` documents are inline XBRL
        inputs. The remaining members download concurrently, bounded by
        `sec_max_concurrency`; the shared SEC client still enforces the rate
        limit. A filing whose index lists only the `-xbrl.zip` archive is
        expanded from that archive.

        Members are staged locally and stored as **one** deterministic package
        archive (`package_object_key`); no member is stored loose. The manifest
        records each member's SHA-256 and size.
        """
        self.settings.ensure_directories()
        index_payload = await self.fetch_index(filing)
        index_checksum = sha256_bytes(
            json.dumps(index_payload, sort_keys=True, separators=(",", ":")).encode()
        )
        # Bind the key to the filing metadata too: a corrected FilingRef (for
        # example an added instance document) must not collide with an
        # immutable manifest cached for the earlier metadata. The selection
        # policy version keeps manifests cached under an older policy in place
        # without blocking re-ingestion.
        filing_checksum = sha256_bytes(
            json.dumps(
                filing.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        )
        cache_key = (
            f"manifests/sec/filings/{filing.accession}/"
            f"selection-v{PACKAGE_SELECTION_POLICY_VERSION}/index-sha256={index_checksum}/"
            f"filing-sha256={filing_checksum}.json"
        )
        preliminary = select_package_files(index_payload, filing)
        summary_name = next((name for name in preliminary if name.lower() == _FILING_SUMMARY), None)
        cached = await asyncio.to_thread(self._load_cached_manifest, cache_key, filing)
        budget = _ByteBudget(self.settings.filing_max_bytes)
        summary_member: PackageMember | None = None
        with tempfile.TemporaryDirectory(
            prefix="filing-download-", dir=self.settings.temp_dir
        ) as root:
            staging = Path(root)
            semaphore = asyncio.Semaphore(self.settings.sec_max_concurrency)
            selected = preliminary
            if summary_name is not None:
                summary_bytes = None
                if cached is not None:
                    summary_bytes = await asyncio.to_thread(
                        self._cached_member_bytes, cached, summary_name
                    )
                if summary_bytes is None:
                    summary_member = await self._fetch_member(
                        filing, summary_name, staging, semaphore, budget
                    )
                    summary_bytes = await asyncio.to_thread((staging / summary_name).read_bytes)
                selected = select_package_files(index_payload, filing, filing_summary=summary_bytes)
            if len(selected) > self.settings.filing_max_members:
                raise ArtifactIntegrityError("Filing exceeds configured member count")
            if cached is not None:
                reusable = await asyncio.to_thread(
                    self._validate_cached_manifest, cached, cache_key, filing, selected
                )
                if reusable is not None:
                    return reusable
            if _is_xbrl_archive(selected, filing):
                members, sources = await self._download_archive(
                    filing, selected[0], staging, semaphore, budget
                )
            else:
                members = [summary_member] if summary_member is not None else []
                pending = [
                    name
                    for name in selected
                    if summary_member is None or name != summary_member.name
                ]
                try:
                    async with asyncio.TaskGroup() as group:
                        tasks = [
                            group.create_task(
                                self._fetch_member(filing, name, staging, semaphore, budget)
                            )
                            for name in pending
                        ]
                except ExceptionGroup as failures:
                    raise failures.exceptions[0] from None
                members.extend(task.result() for task in tasks)
                sources = {member.name: staging / member.name for member in members}
            manifest = FilingPackageManifest(
                filing=filing,
                members=tuple(sorted(members, key=lambda member: member.name)),
                acquired_at=datetime.now(UTC),
            )
            manifest = await asyncio.to_thread(self._store_archive, manifest, sources)
        cache_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
        try:
            await asyncio.to_thread(self.object_store.put_bytes, cache_bytes, cache_key)
        except ArtifactIntegrityError:
            cached = await asyncio.to_thread(self._cached_manifest, cache_key, filing, selected)
            if cached is None:
                raise
            return cached
        manifest_checksum = sha256_bytes(cache_bytes)
        return manifest.model_copy(
            update={
                "manifest_object_key": cache_key,
                "manifest_checksum_sha256": manifest_checksum,
            }
        )

    async def _fetch_member(
        self,
        filing: FilingRef,
        name: str,
        staging: Path,
        semaphore: asyncio.Semaphore,
        budget: _ByteBudget,
    ) -> PackageMember:
        """Download one listed file into staging (stored later in the package archive)."""
        url = f"{filing.archive_directory_url}/{name}"
        destination = staging / _safe_package_name(name)
        async with semaphore:
            _, checksum, size, headers = await self.http.download(
                url, destination, max_bytes=budget.remaining
            )
        budget.consume(size)
        return PackageMember(
            name=name,
            source_url=url,
            size_bytes=size,
            content_type=headers.get("content-type") or mimetypes.guess_type(name)[0],
            checksum_sha256=checksum,
            is_primary_document=name == filing.primary_document,
            is_xbrl_entrypoint=(
                name == filing.primary_document or name == filing.instance_document
            ),
        )

    async def _download_archive(
        self,
        filing: FilingRef,
        archive_name: str,
        staging: Path,
        semaphore: asyncio.Semaphore,
        budget: _ByteBudget,
    ) -> tuple[list[PackageMember], dict[str, Path]]:
        """Fetch `<accession>-xbrl.zip` and expand its members after validation.

        Returns the members (the archive itself plus the expanded files, which
        name it in `source_archive`) and their staged paths. A rejected archive is
        stored loose under its checksum key, like bulk ZIPs, so it stays
        available for diagnosis.
        """
        archive = await self._fetch_member(filing, archive_name, staging, semaphore, budget)
        archive_path = staging / archive_name
        expanded = staging / "expanded"
        try:
            entries = await asyncio.to_thread(
                _expand_filing_archive, archive_path, expanded, self.settings, budget
            )
            entrypoint = _archive_entrypoint([name for name, _, _ in entries], filing)
            if entrypoint is None:
                raise ArtifactIntegrityError(
                    f"{archive_name} contains no XBRL instance document to use as the entrypoint"
                )
        except ArtifactIntegrityError:
            assert archive.checksum_sha256 is not None
            await asyncio.to_thread(
                self.object_store.put_file,
                archive_path,
                filing_object_key(filing, archive_name, archive.checksum_sha256),
                expected_sha256=archive.checksum_sha256,
                metadata={"accession": filing.accession, "source-url": archive.source_url},
            )
            raise
        members = [archive]
        sources = {archive_name: archive_path}
        for name, checksum, size in entries:
            sources[name] = expanded / name
            members.append(
                PackageMember(
                    name=name,
                    source_url=archive.source_url,
                    size_bytes=size,
                    content_type=mimetypes.guess_type(name)[0],
                    checksum_sha256=checksum,
                    is_primary_document=name == filing.primary_document,
                    is_xbrl_entrypoint=name == entrypoint,
                    source_archive=archive_name,
                )
            )
        return members, sources

    def _cached_manifest(
        self, key: str, filing: FilingRef, expected_names: tuple[str, ...]
    ) -> FilingPackageManifest | None:
        """Validate a bounded private-store cache entry before reusing it.

        The cache is a trusted private-object-store optimization. These checks
        bind its metadata to the current filing and index-derived member set;
        they do not cryptographically authenticate SEC source bytes.
        """
        manifest = self._load_cached_manifest(key, filing)
        if manifest is None:
            return None
        return self._validate_cached_manifest(manifest, key, filing, expected_names)

    def _load_cached_manifest(self, key: str, filing: FilingRef) -> FilingPackageManifest | None:
        if not self.object_store.exists(key):
            return None
        with tempfile.TemporaryDirectory(dir=self.settings.temp_dir) as directory:
            manifest_path = Path(directory) / "manifest.json"
            stored = self.object_store.get_file(
                key,
                manifest_path,
                max_bytes=self.settings.response_max_bytes,
            )
            manifest = FilingPackageManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.filing != filing:
            raise ArtifactIntegrityError(f"Cached manifest metadata mismatch: {key}")
        return manifest.model_copy(update={"manifest_checksum_sha256": stored.checksum_sha256})

    def _cached_member_bytes(self, manifest: FilingPackageManifest, name: str) -> bytes | None:
        """Read one cached member (such as FilingSummary.xml) from the store."""
        member = next((item for item in manifest.members if item.name == name), None)
        if member is None or member.checksum_sha256 is None or member.source_archive:
            return None
        if manifest.package_object_key:
            if not self.object_store.exists(manifest.package_object_key):
                return None
            with tempfile.TemporaryDirectory(dir=self.settings.temp_dir) as directory:
                path = Path(directory) / member.name
                self._extract_from_archive(
                    manifest,
                    Path(directory),
                    only=member,
                    max_bytes=self.settings.response_max_bytes,
                )
                return path.read_bytes()
        key = filing_object_key(manifest.filing, name, member.checksum_sha256)
        if not self.object_store.exists(key):
            return None
        with tempfile.TemporaryDirectory(dir=self.settings.temp_dir) as directory:
            path = Path(directory) / "member"
            stored = self.object_store.get_file(
                key, path, max_bytes=self.settings.response_max_bytes
            )
            if stored.checksum_sha256 != member.checksum_sha256:
                raise ArtifactIntegrityError(f"Cached member checksum mismatch: {name}")
            return path.read_bytes()

    def _validate_cached_manifest(
        self,
        manifest: FilingPackageManifest,
        key: str,
        filing: FilingRef,
        expected_names: tuple[str, ...],
    ) -> FilingPackageManifest | None:
        if manifest.filing != filing:
            raise ArtifactIntegrityError(f"Cached manifest metadata mismatch: {key}")
        if len(manifest.members) > self.settings.filing_max_members:
            raise ArtifactIntegrityError("Cached filing exceeds configured member count")
        if any(
            member.size_bytes is not None and member.size_bytes < 0 for member in manifest.members
        ):
            raise ArtifactIntegrityError("Cached filing contains a negative member size")
        known_size = sum(
            member.size_bytes for member in manifest.members if member.size_bytes is not None
        )
        if known_size > self.settings.filing_max_bytes:
            raise ArtifactIntegrityError("Cached filing exceeds configured byte limit")

        expected = set(expected_names)
        actual = [member.name for member in manifest.members]
        if len(actual) != len(set(actual)):
            raise ArtifactIntegrityError("Cached filing contains duplicate members")
        # Members expanded from an archive are bound to a listed archive member.
        listed = {member.name for member in manifest.members if member.source_archive is None}
        if listed != expected:
            missing = sorted(expected - listed)
            unexpected = sorted(listed - expected)
            raise ArtifactIntegrityError(
                f"Cached filing members do not match index (missing={missing}, unexpected={unexpected})"
            )
        for member in manifest.members:
            origin = member.source_archive or member.name
            if member.source_archive is not None and member.source_archive not in listed:
                raise ArtifactIntegrityError(
                    f"Cached filing member names an unlisted archive: {member.name}"
                )
            expected_url = f"{filing.archive_directory_url}/{origin}"
            if member.source_url != expected_url:
                raise ArtifactIntegrityError(
                    f"Cached filing member source URL mismatch: {member.name}"
                )
        if manifest.package_object_key:
            if any(member.checksum_sha256 is None for member in manifest.members):
                raise ArtifactIntegrityError("Cached archived filing lacks member checksums")
            if not self.object_store.exists(manifest.package_object_key):
                return None
        elif any(
            member.checksum_sha256 is None
            or not self.object_store.exists(
                filing_object_key(filing, member.name, member.checksum_sha256)
            )
            for member in manifest.members
        ):
            return None
        return manifest.model_copy(update={"manifest_object_key": key})

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
            total = 0
            for member in manifest.members:
                _safe_package_name(member.name)
                local = package_directory / member.name
                if not local.is_file() and member.checksum_sha256:
                    local = self._materialize_member(
                        manifest.filing,
                        member,
                        staging,
                        max_bytes=self.settings.filing_max_bytes - total,
                    )
                if not local.is_file():
                    raise ArtifactIntegrityError(
                        f"Cannot locate package member for archiving: {member.name}"
                    )
                if member.checksum_sha256 and sha256_file(local) != member.checksum_sha256:
                    raise ArtifactIntegrityError(f"Checksum mismatch while archiving {member.name}")
                total += local.stat().st_size
                if total > self.settings.filing_max_bytes:
                    raise ArtifactIntegrityError("Filing exceeds configured byte limit")
                member_entries.append((member.name, local))

            # 2-3. Write, checksum, and store the deterministic archive.
            archived = self._write_archive(manifest, dict(member_entries), staging)
            # 4. Regenerate and store the manifest that records archive lineage.
            updated = archived
            updated_bytes = updated.model_dump_json(indent=2).encode("utf-8")
            updated_checksum = sha256_bytes(updated_bytes)
            updated_key = (
                f"manifests/sec/filings/{manifest.filing.accession}/sha256={updated_checksum}.json"
            )
            self.object_store.put_bytes(
                updated_bytes,
                updated_key,
            )
            return updated.model_copy(
                update={
                    "manifest_object_key": updated_key,
                    "manifest_checksum_sha256": updated_checksum,
                }
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _store_archive(
        self, manifest: FilingPackageManifest, sources: dict[str, Path]
    ) -> FilingPackageManifest:
        """Store staged members as the one deterministic package archive."""
        staging = Path(tempfile.mkdtemp(prefix="xbrl-package-", dir=self.settings.temp_dir))
        try:
            return self._write_archive(manifest, sources, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _write_archive(
        self, manifest: FilingPackageManifest, sources: dict[str, Path], staging: Path
    ) -> FilingPackageManifest:
        """Write sorted members with fixed timestamps plus a deterministic manifest.

        Identical member bytes give an identical archive, key, and checksum: the
        embedded `manifest.json` leaves out acquisition time and object keys.
        """
        if _ARCHIVE_MANIFEST in sources:
            raise ArtifactIntegrityError(f"Package member shadows {_ARCHIVE_MANIFEST}")
        archive_path = staging / _package_archive_name(manifest)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(sources):
                _write_member(zf, name, sources[name])
            _write_member(zf, _ARCHIVE_MANIFEST, _archive_manifest_bytes(manifest))
        archive_checksum = sha256_file(archive_path)
        package_key = filing_package_key(manifest.filing, archive_checksum)
        self.object_store.put_file(
            archive_path,
            package_key,
            expected_sha256=archive_checksum,
            metadata={"accession": manifest.filing.accession},
        )
        return manifest.model_copy(
            update={
                "package_object_key": package_key,
                "package_checksum_sha256": archive_checksum,
            }
        )

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path:
        """Materialize all members locally and return the chosen entrypoint.

        Archived manifests extract from the package archive; legacy manifests
        (no `package_object_key`) read each loose member.
        """
        entrypoint = _choose_entrypoint(manifest)
        destination.mkdir(parents=True, exist_ok=True)
        if len(manifest.members) > self.settings.filing_max_members:
            raise ArtifactIntegrityError("Filing exceeds configured member count")
        if manifest.package_object_key:
            self._extract_from_archive(manifest, destination)
            return _entrypoint_path(destination, entrypoint)
        total = 0
        for member in manifest.members:
            if (
                member.size_bytes is not None
                and total + member.size_bytes > self.settings.filing_max_bytes
            ):
                raise ArtifactIntegrityError("Filing exceeds configured byte limit")
            target = (destination / member.name).resolve()
            root = destination.resolve()
            if root not in target.parents and target != root:
                raise ArtifactIntegrityError(f"Unsafe package member path: {member.name}")
            if member.checksum_sha256 is None:
                raise ArtifactIntegrityError(f"Member {member.name} has no checksum to locate")
            key = filing_object_key(manifest.filing, member.name, member.checksum_sha256)
            stored = self.object_store.get_file(
                key,
                target,
                max_bytes=self.settings.filing_max_bytes - total,
            )
            total += stored.size_bytes
            if total > self.settings.filing_max_bytes:
                raise ArtifactIntegrityError("Filing exceeds configured byte limit")
            if member.checksum_sha256 and stored.checksum_sha256 != member.checksum_sha256:
                raise ArtifactIntegrityError(
                    f"Materialized member checksum mismatch: {member.name}"
                )
        return _entrypoint_path(destination, entrypoint)

    def _extract_from_archive(
        self,
        manifest: FilingPackageManifest,
        destination: Path,
        *,
        only: PackageMember | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Extract manifest members from the package archive, verifying each one.

        The archive must match `package_checksum_sha256` and pass the bulk ZIP
        safety checks; it may hold only manifest members plus `manifest.json`;
        each member must match its recorded size and SHA-256.
        """
        assert manifest.package_object_key is not None
        limit = self.settings.filing_max_bytes if max_bytes is None else max_bytes
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with tempfile.TemporaryDirectory(dir=self.settings.temp_dir) as directory:
            archive_path = Path(directory) / "package.zip"
            stored = self.object_store.get_file(
                manifest.package_object_key,
                archive_path,
                max_bytes=self.settings.download_max_bytes,
            )
            if (
                manifest.package_checksum_sha256
                and stored.checksum_sha256 != manifest.package_checksum_sha256
            ):
                raise ArtifactIntegrityError(
                    f"Package archive checksum mismatch: {manifest.package_object_key}"
                )
            validate_zip_integrity(archive_path, self.settings)
            with zipfile.ZipFile(archive_path) as archive:
                names = {info.filename for info in archive.infolist() if not info.is_dir()}
                expected = {member.name for member in manifest.members}
                unexpected = sorted(names - expected - {_ARCHIVE_MANIFEST})
                if unexpected:
                    raise ArtifactIntegrityError(
                        f"Package archive holds members not in the manifest: {unexpected}"
                    )
                total = 0
                for member in (only,) if only is not None else manifest.members:
                    if member.checksum_sha256 is None:
                        raise ArtifactIntegrityError(f"Member {member.name} has no checksum")
                    if member.name not in names:
                        raise ArtifactIntegrityError(
                            f"Package archive is missing member: {member.name}"
                        )
                    target = (destination / _safe_package_name(member.name)).resolve()
                    if root not in target.parents:
                        raise ArtifactIntegrityError(f"Unsafe package member path: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(member.name) as source, target.open("wb") as sink:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if total > limit:
                                raise ArtifactIntegrityError("Filing exceeds configured byte limit")
                            digest.update(chunk)
                            sink.write(chunk)
                    if digest.hexdigest() != member.checksum_sha256 or (
                        member.size_bytes is not None and size != member.size_bytes
                    ):
                        raise ArtifactIntegrityError(
                            f"Materialized member checksum mismatch: {member.name}"
                        )

    def _materialize_member(
        self,
        filing: FilingRef,
        member: PackageMember,
        staging: Path,
        *,
        max_bytes: int,
    ) -> Path:
        if member.checksum_sha256 is None:
            raise ArtifactIntegrityError(f"Member {member.name} has no checksum to locate")
        key = filing_object_key(filing, member.name, member.checksum_sha256)
        destination = staging / member.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.object_store.get_file(key, destination, max_bytes=max_bytes)
        return destination


def select_package_files(
    index_payload: dict[str, Any],
    filing: FilingRef,
    *,
    filing_summary: bytes | None = None,
) -> tuple[str, ...]:
    """Select the EDGAR directory files Arelle needs for one filing.

    Keeps the primary document, the instance document, schemas, XBRL
    instance/linkbase XML, `FilingSummary.xml`, and `MetaLinks.json`. Rendered
    `R<n>` pages, index pages, the `.txt` submission, presentation assets, and
    `.htm` documents that are not XBRL inputs (exhibits) are dropped. Extra
    `.htm` documents of a multi-document inline set are kept only when the
    `<InputFiles>` of `filing_summary` list them. When the index lists no XBRL
    input but does list `<accession>-xbrl.zip`, only that archive is returned.
    """
    items = index_payload.get("directory", {}).get("item", [])
    names = [str(item.get("name")) for item in items if item.get("name")]
    for name in names:
        _safe_package_name(name)
    inputs = _filing_summary_inputs(filing_summary) if filing_summary is not None else set()
    selected: set[str] = set()
    has_xbrl_input = False
    for name in names:
        suffix = PurePosixPath(name).suffix.lower()
        lowered = name.lower()
        if name in {filing.primary_document, filing.instance_document}:
            selected.add(name)
            has_xbrl_input = has_xbrl_input or name == filing.instance_document
        elif lowered in {_FILING_SUMMARY, "metalinks.json"}:
            selected.add(name)
        elif _RENDERED_REPORT.match(lowered) or _INDEX_PAGE.search(lowered):
            continue
        elif suffix in {".xsd", ".xml", ".xbrl"}:
            selected.add(name)
            has_xbrl_input = True
        elif suffix in {".htm", ".html"} and lowered in inputs:
            selected.add(name)
    archive = next(
        (name for name in names if name.lower() == f"{filing.accession}-xbrl.zip".lower()), None
    )
    if not has_xbrl_input and archive is not None:
        return (archive,)
    if filing.primary_document:
        selected.add(_safe_package_name(filing.primary_document))
    return tuple(sorted(selected))


def _filing_summary_inputs(data: bytes) -> set[str]:
    """Return the lower-cased `<InputFiles>` names of an EDGAR FilingSummary."""
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise ArtifactIntegrityError(f"Malformed FilingSummary.xml: {exc}") from exc
    return {
        (element.text or "").strip().lower()
        for element in root.iterfind("InputFiles/File")
        if (element.text or "").strip()
    }


def _is_xbrl_archive(selected: tuple[str, ...], filing: FilingRef) -> bool:
    return len(selected) == 1 and selected[0].lower() == f"{filing.accession}-xbrl.zip".lower()


class _ByteBudget:
    """Shared filing byte budget for concurrent member downloads."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise ArtifactIntegrityError("Filing exceeds configured byte limit")


def _expand_filing_archive(
    archive_path: Path,
    destination: Path,
    settings: Settings,
    budget: _ByteBudget,
) -> list[tuple[str, str, int]]:
    """Validate and expand a filing XBRL ZIP; return (name, sha256, size) rows.

    Applies the bulk ZIP checks (path traversal, member count, expanded bytes,
    compression ratio, CRC) and then the filing member and byte limits. Nothing
    is published here.
    """
    validate_zip_integrity(archive_path, settings)
    with zipfile.ZipFile(archive_path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) + 1 > settings.filing_max_members:
            raise ArtifactIntegrityError("Filing exceeds configured member count")
        expanded_total = sum(info.file_size for info in infos)
        if expanded_total > budget.remaining:
            raise ArtifactIntegrityError("Filing exceeds configured byte limit")
        rows: list[tuple[str, str, int]] = []
        for info in infos:
            name = _safe_package_name(info.filename)
            if name.lower() == archive_path.name.lower():
                raise ArtifactIntegrityError(f"Archive member shadows its archive: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(info) as source, target.open("wb") as sink:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > info.file_size:
                        raise ArtifactIntegrityError(f"ZIP member size mismatch: {name}")
                    digest.update(chunk)
                    sink.write(chunk)
            rows.append((name, digest.hexdigest(), size))
        budget.consume(expanded_total)
    return rows


def _archive_entrypoint(names: list[str], filing: FilingRef) -> str | None:
    """Pick the XBRL entrypoint among members expanded from `-xbrl.zip`."""
    for candidate in (filing.instance_document, filing.primary_document):
        if candidate and candidate in names:
            return candidate
    instances = sorted(
        name
        for name in names
        if PurePosixPath(name).suffix.lower() in {".xml", ".xbrl"}
        and not PurePosixPath(name).stem.lower().endswith(_LINKBASE_SUFFIXES)
        and name.lower() != _FILING_SUMMARY
        and not _RENDERED_REPORT.match(name.lower())
    )
    return instances[0] if instances else None


def filing_object_key(filing: FilingRef, filename: str, checksum: str) -> str:
    safe_name = _safe_package_name(filename)
    return f"bronze/sec/filings/{filing.cik}/{filing.accession}/sha256={checksum}/{safe_name}"


def filing_package_key(filing: FilingRef, archive_checksum: str) -> str:
    return (
        f"bronze/sec/filings/{filing.cik}/{filing.accession}/"
        f"packages/sha256={archive_checksum}/package.zip"
    )


def _write_member(zf: zipfile.ZipFile, name: str, data: bytes | Path) -> None:
    info = zipfile.ZipInfo(name)
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    with zf.open(info, "w") as target:
        if isinstance(data, Path):
            with data.open("rb") as source:
                shutil.copyfileobj(source, target, 1024 * 1024)
        else:
            target.write(data)


def _safe_package_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ArtifactIntegrityError(f"Unsafe package member path: {name}")
    return path.as_posix()


def _archive_manifest_bytes(manifest: FilingPackageManifest) -> bytes:
    """The manifest embedded in a package archive: filing and members only."""
    payload = manifest.model_dump(mode="json", include={"filing", "members"})
    return json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")


def _entrypoint_path(destination: Path, entrypoint: str) -> Path:
    target = (destination / entrypoint).resolve()
    if not target.is_relative_to(destination.resolve()) or not target.is_file():
        raise ArtifactIntegrityError("Entrypoint escapes materialized package or is missing")
    return target


def _package_archive_name(manifest: FilingPackageManifest) -> str:
    return f"{manifest.filing.accession}.zip"


def _choose_entrypoint(manifest: FilingPackageManifest) -> str:
    """Deterministic entrypoint: instance doc, then primary, then any XBRL XML.

    A package expanded from `-xbrl.zip` uses the entrypoint flagged during
    expansion, because the index-derived primary document is not a member.
    """
    archived = sorted(
        member.name
        for member in manifest.members
        if member.source_archive is not None and member.is_xbrl_entrypoint
    )
    if archived:
        return _manifest_entrypoint(manifest, archived[0])
    if manifest.filing.instance_document:
        return _manifest_entrypoint(manifest, manifest.filing.instance_document)
    if manifest.filing.primary_document:
        return _manifest_entrypoint(manifest, manifest.filing.primary_document)
    preferred = [
        member.name
        for member in manifest.members
        if member.is_xbrl_entrypoint or member.name.endswith(".xml")
    ]
    if preferred:
        return _manifest_entrypoint(manifest, sorted(preferred)[0])
    raise ArtifactIntegrityError(f"No XBRL entrypoint found in {manifest.filing.accession}")


def _manifest_entrypoint(manifest: FilingPackageManifest, name: str) -> str:
    safe = _safe_package_name(name)
    if safe not in {member.name for member in manifest.members}:
        raise ArtifactIntegrityError("Entrypoint is not a package manifest member")
    return safe
