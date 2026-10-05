"""Read-only object-store inventory: usage by kind and reclaimable legacy objects.

Nothing here deletes or writes objects. Retention of legacy loose bronze members
and uncompressed extraction checkpoints is an operator decision.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingPackageManifest

_PACKAGE = re.compile(r"^bronze/sec/filings/\d+/[^/]+/packages/sha256=[0-9a-f]{64}/package\.zip$")
_LOOSE = re.compile(r"^bronze/sec/filings/\d+/[^/]+/sha256=[0-9a-f]{64}/.+$")
_EXTRACTION = re.compile(
    r"^silver/xbrl/extractions/accession=(?P<accession>[^/]+)/revision=(?P<revision>\d+)/"
    r"sha256=[0-9a-f]{64}\.json(?P<zst>\.zst)?$"
)
KINDS = (
    "bronze_packages",
    "bronze_loose_members",
    "manifests",
    "extraction_json",
    "extraction_json_zst",
    "silver_parquet",
    "quarantine",
    "other",
)


class ListableObjectStore(Protocol):
    def list_objects(self, prefix: str = "") -> Iterator[tuple[str, int]]: ...

    def exists(self, key: str) -> bool: ...

    def get_file(self, key: str, destination: Path, *, max_bytes: int | None = None) -> Any: ...


def classify(key: str) -> str:
    if _PACKAGE.match(key):
        return "bronze_packages"
    if _LOOSE.match(key):
        return "bronze_loose_members"
    if key.startswith("manifests/"):
        return "manifests"
    match = _EXTRACTION.match(key)
    if match:
        return "extraction_json_zst" if match.group("zst") else "extraction_json"
    if key.startswith("silver/"):
        return "silver_parquet"
    if key.startswith("quarantine/"):
        return "quarantine"
    return "other"


def storage_report(store: ListableObjectStore, settings: Settings) -> dict[str, Any]:
    """Inventory every object and list reclaimable legacy objects (dry run only).

    Reclaimable:

    - `loose_members_in_archives`: loose bronze members that a stored manifest
      records inside a package archive which exists (materialization reads the
      archive, so the loose copy is redundant);
    - `json_checkpoints_with_zst`: uncompressed extraction checkpoints whose
      accession and revision also have a `.json.zst` checkpoint. Completed jobs may
      still point at the JSON key; see docs/OPERATIONS.md before deleting.
    """
    usage: dict[str, list[int]] = {kind: [0, 0] for kind in KINDS}
    sizes: dict[str, int] = {}
    manifests: list[str] = []
    compressed: set[tuple[str, str]] = set()
    plain: list[tuple[str, str, str]] = []
    for key, size in store.list_objects(""):
        kind = classify(key)
        usage[kind][0] += 1
        usage[kind][1] += size
        sizes[key] = size
        if kind == "manifests" and key.endswith(".json"):
            manifests.append(key)
        match = _EXTRACTION.match(key)
        if match and match.group("zst"):
            compressed.add((match.group("accession"), match.group("revision")))
        elif match:
            plain.append((key, match.group("accession"), match.group("revision")))

    archived = set(_archived_member_keys(store, settings, manifests, sizes))
    loose = sorted(
        key for key in sizes if classify(key) == "bronze_loose_members" and key in archived
    )
    superseded = sorted(
        key for key, accession, revision in plain if (accession, revision) in compressed
    )
    return {
        "dry_run": True,
        "usage": {kind: {"objects": count, "bytes": size} for kind, (count, size) in usage.items()},
        "total": {"objects": len(sizes), "bytes": sum(sizes.values())},
        "reclaimable": {
            "loose_members_in_archives": _summary(loose, sizes),
            "json_checkpoints_with_zst": _summary(superseded, sizes),
        },
    }


def _archived_member_keys(
    store: ListableObjectStore, settings: Settings, manifests: Iterable[str], sizes: dict[str, int]
) -> Iterator[str]:
    """Loose member keys that a manifest records inside an existing package archive."""
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_object_key

    with tempfile.TemporaryDirectory(dir=settings.temp_dir) as directory:
        path = Path(directory) / "manifest.json"
        for key in manifests:
            try:
                store.get_file(key, path, max_bytes=settings.response_max_bytes)
                manifest = FilingPackageManifest.model_validate_json(path.read_bytes())
            except Exception:
                continue
            if not manifest.package_object_key or manifest.package_object_key not in sizes:
                continue
            for member in manifest.members:
                if member.checksum_sha256:
                    yield filing_object_key(manifest.filing, member.name, member.checksum_sha256)


def _summary(keys: list[str], sizes: dict[str, int]) -> dict[str, Any]:
    return {"objects": len(keys), "bytes": sum(sizes[key] for key in keys), "keys": keys}
