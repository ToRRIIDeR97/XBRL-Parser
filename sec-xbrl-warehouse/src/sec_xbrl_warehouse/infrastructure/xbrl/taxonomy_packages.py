"""Pinned taxonomy package registry: status, verification, and download.

`config/taxonomy_packages.yml` lists immutable taxonomy package files by
filename, HTTPS source URL, and SHA-256. Packages live under
`XBRL_ARELLE_TAXONOMY_PACKAGES_DIR` and are verified before every use.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, ConfigurationError
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file

PackageState = Literal["ok", "missing", "checksum_mismatch", "disabled", "unsafe_path"]


@dataclass(frozen=True)
class TaxonomyPackageStatus:
    package_id: str
    filename: str
    state: PackageState
    detail: str = ""


@dataclass(frozen=True)
class TaxonomyPackage:
    package_id: str
    filename: str
    url: str | None
    sha256: str
    enabled: bool
    path: Path | None  # None when the filename escapes the packages directory


class PackageDownloader(Protocol):
    async def download(
        self, url: str, destination: Path, *, max_bytes: int | None = None
    ) -> tuple[Path, str, int, dict[str, str]]: ...


class _RegistryLoader(yaml.SafeLoader):
    """YAML loader that keeps registry scalars as written.

    YAML 1.1 reads `off`/`no` as booleans and digit-only checksums as integers
    (dropping leading zeros). Only `true`/`false` become booleans here; every
    other plain scalar stays a string.
    """


_RegistryLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag in {"tag:yaml.org,2002:null", "tag:yaml.org,2002:merge"}
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_RegistryLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def registered_packages(settings: Settings) -> list[TaxonomyPackage]:
    """Parse the registry; filenames that escape the packages directory get no path."""
    registry_path = settings.config_dir / "taxonomy_packages.yml"
    registry: dict[str, Any] = {}
    if registry_path.is_file():
        loaded = yaml.load(registry_path.read_text(encoding="utf-8"), Loader=_RegistryLoader)
        registry = loaded if isinstance(loaded, dict) else {}
    root = settings.arelle_taxonomy_packages_dir.resolve()
    packages: list[TaxonomyPackage] = []
    for entry in registry.get("packages") or []:
        filename = str(entry["filename"])
        candidate = (root / filename).resolve()
        safe = (
            bool(filename)
            and "\\" not in filename
            and candidate.parent == root
            and candidate.name == filename
        )
        packages.append(
            TaxonomyPackage(
                package_id=str(entry.get("id") or filename),
                filename=filename,
                url=str(entry["url"]) if entry.get("url") else None,
                sha256=str(entry.get("sha256") or "").lower(),
                enabled=bool(entry.get("enabled", True)),
                path=candidate if safe else None,
            )
        )
    return packages


def check_package(package: TaxonomyPackage) -> TaxonomyPackageStatus:
    """Verify one registered package on disk without network access."""

    def status(state: PackageState, detail: str = "") -> TaxonomyPackageStatus:
        return TaxonomyPackageStatus(package.package_id, package.filename, state, detail)

    if not package.enabled:
        return status("disabled")
    if package.path is None:
        return status("unsafe_path", "filename escapes the taxonomy packages directory")
    if not package.path.is_file():
        return status("missing", str(package.path))
    actual = sha256_file(package.path)
    if actual != package.sha256:
        return status("checksum_mismatch", f"expected {package.sha256}, found {actual}")
    return status("ok", str(package.path))


def package_status(settings: Settings) -> list[TaxonomyPackageStatus]:
    """Report every registered package: ok, missing, checksum_mismatch, disabled, unsafe_path."""
    return [check_package(package) for package in registered_packages(settings)]


def validate_package_url(url: str | None, settings: Settings) -> str:
    """Packages download only over HTTPS from an allowed taxonomy host, without credentials."""
    if not url:
        raise ConfigurationError("Taxonomy package has no download URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"Invalid taxonomy package URL: {url}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in settings.allowed_taxonomy_hosts
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError(f"Taxonomy package URL is outside the download policy: {url}")
    return url


async def sync_taxonomy_packages(
    settings: Settings, http: PackageDownloader
) -> list[TaxonomyPackageStatus]:
    """Download each enabled package that is missing or corrupt, verify it, and install it.

    Every URL is checked before the first request. A package is downloaded to a
    temporary file in the packages directory, verified against its pinned
    SHA-256, and only then renamed to its final name, so a failed or partial
    download never leaves a file under that name.
    """
    packages = registered_packages(settings)
    pending: list[TaxonomyPackage] = []
    for package in packages:
        if not package.enabled:
            continue
        if package.path is None:
            raise ConfigurationError(f"Unsafe taxonomy package filename: {package.filename}")
        if len(package.sha256) != 64:
            raise ConfigurationError(f"Taxonomy package {package.package_id} has no SHA-256")
        if check_package(package).state != "ok":
            validate_package_url(package.url, settings)
            pending.append(package)

    root = settings.arelle_taxonomy_packages_dir
    root.mkdir(parents=True, exist_ok=True)
    for package in pending:
        assert package.path is not None and package.url is not None
        staging = Path(tempfile.mkdtemp(prefix=".sync-", dir=root))
        try:
            download = staging / package.filename
            _, reported, _, _ = await http.download(
                package.url, download, max_bytes=settings.download_max_bytes
            )
            actual = sha256_file(download)
            if reported != package.sha256 or actual != package.sha256:
                raise ArtifactIntegrityError(
                    f"Taxonomy package {package.package_id} checksum mismatch: "
                    f"expected {package.sha256}, downloaded {actual}"
                )
            os.replace(download, package.path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return [check_package(package) for package in packages]
