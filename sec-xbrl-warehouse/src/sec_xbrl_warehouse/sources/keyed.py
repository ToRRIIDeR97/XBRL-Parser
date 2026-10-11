"""Shared pieces of the key-gated regulator APIs (EDINET and OpenDART).

Both APIs take the operator's key as a query parameter and answer errors with a
small JSON or XML body instead of the requested zip. The key lives only in the
outgoing request: URLs kept anywhere else (filing references, manifests, object
metadata, logs, exception messages) are built without it, and any failure whose
text or chain mentions it is re-raised with the key masked and the chain dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import tempfile
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import SecretStr

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, PackageMember
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    ConfigurationError,
    SourceAccessError,
)
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file
from sec_xbrl_warehouse.ingestion.bulk_downloader import validate_zip_integrity
from sec_xbrl_warehouse.ingestion.filing_downloader import _safe_package_name, filing_package_key
from sec_xbrl_warehouse.sources.base import REDACTED, SourcePolicy
from sec_xbrl_warehouse.sources.esef import _manifest_key

# Largest error body read back from a non-zip download.
_ERROR_BODY_BYTES = 64 * 1024


class EntityKeys(Protocol):
    async def resolve_entity_key(self, scheme: str, identifier: str) -> int: ...


def required_key(value: SecretStr | None, source: str, variable: str) -> str:
    """The configured API key; `ConfigurationError` (before any request) when unset."""
    key = value.get_secret_value().strip() if value is not None else ""
    if not key:
        raise ConfigurationError(f"{source} needs an API key: set {variable}")
    return key


async def redacted[T](call: Awaitable[T], key: str, policy: SourcePolicy) -> T:
    """Await `call`; a failure that mentions `key` is re-raised masked and unchained.

    `SourceHttpClient` already masks the policy's credential parameters; this is the
    adapter's own guard for anything else that might echo the key.
    """
    try:
        return await call
    except Exception as exc:
        failure = exc
    if not _mentions(failure, key):
        raise failure
    message = policy.redact(str(failure)).replace(key, REDACTED)
    replacement: Exception
    try:
        replacement = type(failure)(message)
    except Exception:
        replacement = SourceAccessError(message)
    if not isinstance(replacement, Exception) or _mentions(replacement, key):
        replacement = SourceAccessError(f"{policy.code} request failed ({type(failure).__name__})")
    raise replacement from None


def _mentions(exc: BaseException, key: str) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if key in str(current) or any(key in repr(arg) for arg in current.args):
            return True
        current = current.__cause__ or current.__context__
    return False


def read_error_body(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(_ERROR_BODY_BYTES)


def store_zip_package(
    settings: Settings,
    store: ObjectStore,
    filing: FilingRef,
    archive: Path,
    *,
    source: str,
    source_url: str,
    entrypoint: Callable[[Sequence[str]], str],
) -> FilingPackageManifest:
    """Validate a downloaded zip, store it once, and return (or reuse) its manifest.

    The archive gets the bulk zip checks bounded by the filing limits and safe member
    names; `entrypoint` picks the one instance among the member names (or raises
    `ArtifactIntegrityError`). A zip that fails is kept under
    `quarantine/<source>/filings/<key>/sha256=<digest>/package.zip`. The zip is
    stored byte-identical at `filing_package_key(filing, sha256)`; fetching identical
    bytes again returns the stored manifest. `source_url` must not carry a key.
    """
    digest = sha256_file(archive)
    package_key = filing_package_key(filing, digest)
    cache_key = _manifest_key(filing, digest)
    cached = _cached_manifest(settings, store, cache_key, package_key, filing)
    if cached is not None:
        return cached
    metadata = {"accession": filing.accession, "source-url": source_url}
    try:
        members = _members(archive, settings, source_url, entrypoint)
    except ArtifactIntegrityError:
        store.put_file(
            archive,
            f"quarantine/{source}/filings/{filing.accession}/sha256={digest}/package.zip",
            expected_sha256=digest,
            metadata=metadata,
        )
        raise
    store.put_file(archive, package_key, expected_sha256=digest, metadata=metadata)
    manifest = FilingPackageManifest(
        filing=filing,
        members=members,
        acquired_at=datetime.now(UTC),
        package_object_key=package_key,
        package_checksum_sha256=digest,
    )
    try:
        stored = store.put_bytes(manifest.model_dump_json().encode("utf-8"), cache_key)
    except ArtifactIntegrityError:
        # A concurrent fetch stored its manifest first; reuse it.
        cached = _cached_manifest(settings, store, cache_key, package_key, filing)
        if cached is None:
            raise
        return cached
    return manifest.model_copy(
        update={
            "manifest_object_key": cache_key,
            "manifest_checksum_sha256": stored.checksum_sha256,
        }
    )


async def store_zip_package_async(*args: Any, **kwargs: Any) -> FilingPackageManifest:
    return await asyncio.to_thread(store_zip_package, *args, **kwargs)


def _cached_manifest(
    settings: Settings, store: ObjectStore, key: str, package_key: str, filing: FilingRef
) -> FilingPackageManifest | None:
    if not store.exists(key) or not store.exists(package_key):
        return None
    with tempfile.TemporaryDirectory(dir=settings.temp_dir) as directory:
        path = Path(directory) / "manifest.json"
        stored = store.get_file(key, path, max_bytes=settings.response_max_bytes)
        manifest = FilingPackageManifest.model_validate_json(path.read_bytes())
    if manifest.filing != filing or manifest.package_object_key != package_key:
        raise ArtifactIntegrityError(f"Cached manifest metadata mismatch: {key}")
    return manifest.model_copy(
        update={"manifest_object_key": key, "manifest_checksum_sha256": stored.checksum_sha256}
    )


def _members(
    archive: Path,
    settings: Settings,
    url: str,
    entrypoint: Callable[[Sequence[str]], str],
) -> tuple[PackageMember, ...]:
    scoped = settings.model_copy(
        update={
            "zip_max_members": min(settings.zip_max_members, settings.filing_max_members),
            "zip_max_total_bytes": min(settings.zip_max_total_bytes, settings.filing_max_bytes),
        }
    )
    validate_zip_integrity(archive, scoped)
    rows: list[tuple[str, str, int]] = []
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            if info.is_dir():
                continue
            name = _safe_package_name(info.filename)
            digest = hashlib.sha256()
            size = 0
            with package.open(info) as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            rows.append((name, digest.hexdigest(), size))
    chosen = entrypoint(sorted(name for name, _, _ in rows))
    return tuple(
        PackageMember(
            name=name,
            source_url=url,
            size_bytes=size,
            content_type=mimetypes.guess_type(name)[0],
            checksum_sha256=checksum,
            is_primary_document=name == chosen,
            is_xbrl_entrypoint=name == chosen,
        )
        for name, checksum, size in sorted(rows)
    )


def single_entrypoint(candidates: Sequence[str], what: str) -> str:
    """The only candidate; `ArtifactIntegrityError` for none or several."""
    if not candidates:
        raise ArtifactIntegrityError(f"Package has no {what}")
    if len(candidates) > 1:
        raise ArtifactIntegrityError(f"Package has several {what}s: {list(candidates)}")
    return candidates[0]
