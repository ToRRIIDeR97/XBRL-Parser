"""Routes discovery, package downloads, and fiscal focus to the filing's source adapter."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, ValidationIssue
from sec_xbrl_warehouse.domain.sources import filing_source, is_source_code
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import ConfigurationError, UnsupportedSourceError
from sec_xbrl_warehouse.sources.base import SourceAdapter


class PackageArchive(Protocol):
    """Source-neutral bronze package handling (`FilingPackageDownloader` without HTTP)."""

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path: ...

    def archive_package(
        self, manifest: FilingPackageManifest, package_directory: Path
    ) -> FilingPackageManifest: ...


class SourceRegistry:
    """The enabled source adapters, keyed by source code.

    Also the ingestor's package store: `download_package` goes to the filing's
    adapter, while `materialize_package` and `archive_package` (stored archives,
    no network) go to the shared `packages` handler.
    """

    def __init__(
        self, adapters: Iterable[SourceAdapter], *, packages: PackageArchive | None = None
    ) -> None:
        by_code: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            code = getattr(adapter, "code", None)
            if not isinstance(code, str) or not is_source_code(code):
                raise ValueError(f"Invalid source adapter code: {code!r}")
            if code in by_code:
                raise ValueError(f"Duplicate source adapter code: {code}")
            by_code[code] = adapter
        self._adapters = by_code
        self._packages = packages

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter(self, code: str) -> SourceAdapter:
        """The adapter for `code`; `UnsupportedSourceError` if it isn't enabled."""
        adapter = self._adapters.get(code) if isinstance(code, str) else None
        if adapter is None:
            raise UnsupportedSourceError(
                f"Source {code!r} is not enabled here (enabled: {', '.join(self.codes)})"
            )
        return adapter

    def adapter_for(self, filing_key: str) -> SourceAdapter:
        """The adapter for a filing key's source (`ValueError` for an invalid key)."""
        return self.adapter(filing_source(filing_key))

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        return await self.adapter_for(filing.accession).download_package(filing)

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        return self.adapter_for(bundle.filing.accession).fiscal_focus(bundle)

    def materialize_package(self, manifest: FilingPackageManifest, destination: Path) -> Path:
        return self._package_archive().materialize_package(manifest, destination)

    def archive_package(
        self, manifest: FilingPackageManifest, package_directory: Path
    ) -> FilingPackageManifest:
        return self._package_archive().archive_package(manifest, package_directory)

    def _package_archive(self) -> PackageArchive:
        if self._packages is None:
            raise ConfigurationError("This source registry has no package archive handler")
        return self._packages
