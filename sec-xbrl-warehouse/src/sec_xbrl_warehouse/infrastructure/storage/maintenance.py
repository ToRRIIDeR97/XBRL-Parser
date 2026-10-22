"""Operator storage maintenance: retire superseded revisions, recompress checkpoints.

Both default to a dry run. They delete or rewrite stored data, so they need the
owner/admin credentials (the `admin` Compose service); the worker identities
can't run them.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingPackageManifest
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.sources import is_filing_key
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle

# ClickHouse tables with one set of rows per (accession, extraction_revision).
REVISIONED_TABLES = (
    "xbrl_contexts",
    "xbrl_concepts",
    "xbrl_facts",
    "xbrl_relationships",
    "xbrl_footnotes",
    "xbrl_roles",
    "xbrl_statement_lines",
    "xbrl_cover_pages",
    "xbrl_segment_facts",
    "xbrl_concept_labels",
    "canonical_facts",
)
_CHUNK = 500


def delete_filing_source(
    store: ObjectStore,
    settings: Settings,
    manifest: FilingPackageManifest,
    extraction: dict[str, Any],
    revision: int,
    *,
    apply: bool = False,
) -> bool:
    """Verify the replay checkpoint before retiring exactly one source package."""
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_package_key

    if apply and settings.retain_filing_source_files:
        raise ValueError("Set XBRL_RETAIN_FILING_SOURCE_FILES=false before deleting sources")
    checksum = manifest.package_checksum_sha256 or ""
    key = manifest.package_object_key
    if (
        not key
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or key != filing_package_key(manifest.filing, checksum)
    ):
        raise ArtifactIntegrityError("Source cleanup requires a checksum-addressed filing package")
    if not store.exists(key):
        return False
    sha = extraction.get("sha256", "")
    prefix = f"silver/xbrl/extractions/accession={manifest.filing.accession}/revision={revision}/"
    if re.fullmatch(r"[0-9a-f]{64}", sha) is None or extraction.get("object_key") not in {
        f"{prefix}sha256={sha}.json",
        f"{prefix}sha256={sha}.json.zst",
    }:
        raise ArtifactIntegrityError("Source cleanup requires the filing's saved extraction")
    settings.ensure_directories()
    with tempfile.TemporaryDirectory(prefix="source-retention-", dir=settings.temp_dir) as tmp:
        saved = store.get_file(
            extraction["object_key"], Path(tmp) / "extract", max_bytes=settings.extraction_max_bytes
        )
        if saved.checksum_sha256 != sha:
            raise ArtifactIntegrityError("Saved extraction checksum mismatch; source retained")
    if apply:
        store.delete(key)
    return True


async def prune_filing_sources(
    control: Any, warehouse: _Warehouse, store: Any, settings: Settings, *, apply: bool = False
) -> dict[str, Any]:
    """Retire source ZIPs referenced by completed, published ingestion jobs only."""
    import asyncio

    if apply and settings.retain_filing_source_files:
        raise ValueError("Set XBRL_RETAIN_FILING_SOURCE_FILES=false before deleting sources")
    published = {
        (str(row["accession"]), int(row["revision"]))
        for row in warehouse.query(
            "SELECT accession, extraction_revision AS revision FROM xbrl_extractions "
            "GROUP BY accession, extraction_revision"
        )
    }
    sizes = dict(store.list_objects("bronze/"))
    report: dict[str, Any] = {"dry_run": not apply, "packages": [], "bytes": 0, "skipped": []}
    seen: set[str] = set()
    for item in await control.completed_filing_sources():
        manifest = FilingPackageManifest.model_validate(item["package"])
        key = manifest.package_object_key
        complete = item["complete"]
        revision = int(complete.get("extraction_revision", 0))
        if not key or key in seen or key not in sizes:
            continue
        if (
            complete.get("status") != "complete"
            or complete.get("accession") != manifest.filing.accession
            or (manifest.filing.accession, revision) not in published
            or await control.source_package_in_use(manifest.filing.accession)
        ):
            report["skipped"].append(key)
            continue
        ready = await asyncio.to_thread(
            delete_filing_source, store, settings, manifest, item["extract"], revision, apply=apply
        )
        if ready:
            seen.add(key)
            report["packages"].append(key)
            report["bytes"] += sizes[key]
    return report


class _Warehouse(Protocol):
    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> Any: ...


def retire_superseded_revisions(
    warehouse: _Warehouse, store: Any, *, apply: bool = False
) -> dict[str, Any]:
    """Delete ClickHouse rows of extraction revisions older than the published one.

    Every `xbrl_*` table keeps each revision's rows (the revision is in the sort
    key), so a parser upgrade doubles them. Readers only see each filing's latest
    published revision. A superseded revision is retired only when its saved
    extraction is still in the object store (the rebuild source, kept forever);
    `xbrl_extractions` and silver Parquet keep its lineage. Unpublished revisions
    are left alone.
    """
    latest = {
        str(row["accession"]): int(row["revision"])
        for row in warehouse.query(
            "SELECT accession, max(extraction_revision) AS revision "
            "FROM xbrl_extractions GROUP BY accession"
        )
    }
    found: dict[tuple[str, int], dict[str, int]] = {}
    for table in REVISIONED_TABLES:
        for row in warehouse.query(
            f"SELECT accession, extraction_revision AS revision, count() AS n FROM {table} "
            "GROUP BY accession, extraction_revision"
        ):
            accession, revision = str(row["accession"]), int(row["revision"])
            if accession in latest and revision < latest[accession]:
                found.setdefault((accession, revision), {})[table] = int(row["n"])
    retire: list[tuple[str, int]] = []
    kept: list[tuple[str, int]] = []
    for accession, revision in sorted(found):
        prefix = f"silver/xbrl/extractions/accession={accession}/revision={revision}/"
        # A valid filing key has no quotes or spaces, so the DELETE below can format it.
        saved = is_filing_key(accession) and any(True for _ in store.list_objects(prefix))
        (retire if saved else kept).append((accession, revision))
    rows: dict[str, int] = {
        table: sum(found[pair].get(table, 0) for pair in retire) for table in REVISIONED_TABLES
    }
    if apply:
        for table in REVISIONED_TABLES:
            pairs = [pair for pair in retire if table in found[pair]]
            for start in range(0, len(pairs), _CHUNK):
                values = ", ".join(f"('{a}', {int(r)})" for a, r in pairs[start : start + _CHUNK])
                warehouse.command(
                    f"ALTER TABLE {table} DELETE WHERE (accession, extraction_revision) "
                    f"IN ({values}) SETTINGS mutations_sync = 1"
                )
    return {
        "dry_run": not apply,
        "retired_revisions": [{"accession": a, "revision": r} for a, r in retire],
        "kept_without_saved_extraction": [{"accession": a, "revision": r} for a, r in kept],
        "rows": {table: count for table, count in rows.items() if count},
    }


async def recompress_extraction_checkpoints(
    control: Any,
    store: Any,
    settings: Settings,
    *,
    apply: bool = False,
    delete_legacy: bool = False,
) -> dict[str, Any]:
    """Rewrite legacy plain `.json` saved extractions as `.json.zst` and repoint them.

    The JSON bytes are compressed unchanged; the new object is checked to load as
    the same filing and revision before any checkpoint points to it. With
    `delete_legacy`, the plain object is deleted once no checkpoint uses it (on a
    versioned bucket it stays recoverable until its noncurrent version expires).
    """
    import asyncio

    import pyarrow as pa

    pointers = await control.legacy_extraction_pointers()
    sizes = dict(store.list_objects("silver/xbrl/extractions/"))
    report: dict[str, Any] = {
        "dry_run": not apply,
        "legacy_objects": len(pointers),
        "legacy_bytes": sum(sizes.get(item["object_key"], 0) for item in pointers),
        "rewritten": 0,
        "compressed_bytes": 0,
        "checkpoints_repointed": 0,
        "deleted": 0,
        "skipped": [],
    }
    if not apply:
        return report
    settings.ensure_directories()
    for pointer in pointers:
        old_key, old_sha = pointer["object_key"], pointer["sha256"]
        match = re.search(r"accession=([^/]+)/revision=(\d+)/", old_key)
        with tempfile.TemporaryDirectory(prefix="recompress-", dir=settings.temp_dir) as tmp:
            plain, packed = Path(tmp) / "extraction.json", Path(tmp) / "extraction.json.zst"
            stored = await asyncio.to_thread(
                store.get_file, old_key, plain, max_bytes=settings.extraction_max_bytes
            )
            if stored.checksum_sha256 != old_sha or match is None:
                report["skipped"].append({"object_key": old_key, "reason": "checksum mismatch"})
                continue

            def compress(source: Path = plain, target: Path = packed) -> None:
                with (
                    source.open("rb") as reader,
                    pa.CompressedOutputStream(str(target), "zstd") as writer,
                ):
                    while chunk := reader.read(1024 * 1024):
                        writer.write(chunk)

            await asyncio.to_thread(compress)
            bundle = await asyncio.to_thread(read_bundle, packed, settings.extraction_max_bytes)
            if (bundle.filing.accession, bundle.extraction_revision) != (
                match.group(1),
                int(match.group(2)),
            ):
                report["skipped"].append({"object_key": old_key, "reason": "content mismatch"})
                continue
            new_sha = await asyncio.to_thread(sha256_file, packed)
            new_key = old_key.rsplit("/", 1)[0] + f"/sha256={new_sha}.json.zst"
            await asyncio.to_thread(store.put_file, packed, new_key, expected_sha256=new_sha)
            report["rewritten"] += 1
            report["compressed_bytes"] += packed.stat().st_size
        report["checkpoints_repointed"] += await control.repoint_extraction_checkpoints(
            old_key, new_key, new_sha
        )
        if delete_legacy:
            remaining = {item["object_key"] for item in await control.legacy_extraction_pointers()}
            if old_key not in remaining:
                await asyncio.to_thread(store.delete, old_key)
                report["deleted"] += 1
    return report
