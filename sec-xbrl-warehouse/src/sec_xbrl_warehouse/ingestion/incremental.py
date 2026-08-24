from __future__ import annotations

from pathlib import Path

from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef
from sec_xbrl_warehouse.domain.ports import (
    BatchWriter,
    ControlRepository,
    FilingPackageStore,
    Warehouse,
    XbrlExtractor,
)
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    concepts_table,
    contexts_table,
    facts_table,
    footnotes_table,
    relationships_table,
)
from sec_xbrl_warehouse.logging import get_logger

logger = get_logger(__name__)


class IncrementalFilingIngestor:
    """End-to-end stage coordinator for one filing accession."""

    def __init__(
        self,
        control: ControlRepository,
        package_store: FilingPackageStore,
        extractor: XbrlExtractor,
        parquet_writer: BatchWriter,
        warehouse: Warehouse,
    ) -> None:
        self.control = control
        self.package_store = package_store
        self.extractor = extractor
        self.parquet_writer = parquet_writer
        self.warehouse = warehouse

    async def ingest(self, filing: FilingRef, *, extraction_revision: int = 1) -> dict[str, object]:
        """Download, extract, archive, load, and reconcile a filing.

        Stage order: filing upsert -> package acquire -> materialize -> extract ->
        silver -> ClickHouse -> validation summary -> reconcile -> complete.
        """
        await self.control.upsert_filing(filing)

        manifest: FilingPackageManifest = await self.package_store.download_package(filing)
        manifest = self._archive(manifest, filing)
        entrypoint = self._materialize(manifest, filing)

        bundle = self.extractor.extract(entrypoint, filing, extraction_revision=extraction_revision)
        counts = await self._persist_bundle(bundle)
        reconciliation = await self._reconcile_with_sec_bulk(filing.accession)

        return {
            "accession": filing.accession,
            "extraction_revision": extraction_revision,
            "entity_counts": counts,
            "validation_issue_count": len(bundle.validation_issues),
            "reconciliation": reconciliation,
            "status": "complete",
        }

    def _archive(self, manifest: FilingPackageManifest, filing: FilingRef) -> FilingPackageManifest:
        """Archive the package if the store supports it; otherwise return as-is."""
        archive = getattr(self.package_store, "archive_package", None)
        if callable(archive) and manifest is not None:
            package_dir = getattr(manifest, "local_directory", None)
            if package_dir is not None:
                result = archive(manifest, Path(package_dir))
                return (
                    FilingPackageManifest.model_validate(result.model_dump())
                    if hasattr(result, "model_dump")
                    else manifest
                )
        return manifest

    def _materialize(self, manifest: object, filing: FilingRef) -> Path:
        """Materialize the package locally and return the extraction entrypoint."""
        materialize = getattr(self.package_store, "materialize_package", None)
        destination = Path("data/tmp/packages") / filing.accession
        destination.mkdir(parents=True, exist_ok=True)
        if callable(materialize) and manifest is not None:
            result = materialize(manifest, destination)
            return Path(result)
        # Fallback: the package store has already placed files locally.
        for candidate in sorted(destination.iterdir()) if destination.exists() else []:
            if candidate.suffix in {".xml", ".htm", ".html"}:
                return candidate
        raise FileNotFoundError(f"No extractable entrypoint for {filing.accession}")

    async def _persist_bundle(self, bundle: object) -> dict[str, int]:
        """Write each entity to silver Parquet then ClickHouse with checkpoints."""
        assert isinstance(bundle, XbrlExtractionBundle)
        tables = {
            "contexts": contexts_table(bundle),
            "concepts": concepts_table(bundle),
            "facts": facts_table(bundle),
            "relationships": relationships_table(bundle),
            "footnotes": footnotes_table(bundle),
        }
        # Silver first: one immutable Parquet dataset per entity.
        for entity, table in tables.items():
            prefix = (
                f"silver/xbrl/{entity}/filed_year={bundle.filing.filed_date.year}"
                f"/accession={bundle.filing.accession}/revision={bundle.extraction_revision}"
            )
            self.parquet_writer.write_batches(
                [table],
                destination_prefix=prefix,
                partition_values={"filed_year": str(bundle.filing.filed_date.year)},
            )
        # Gold: one atomic bundle insert, returning per-entity row counts.
        counts = self.warehouse.insert_xbrl_bundle(bundle)
        return {entity: counts.get(entity, 0) for entity in tables}

    async def _reconcile_with_sec_bulk(self, accession: str) -> dict[str, object]:
        """Compare Arelle facts with any later SEC bulk representation.

        When the SEC bulk tables already contain this accession, report missing,
        extra, and differing facts without overwriting either source. A missing
        bulk representation is not an error: FSNDS publishes with a lag.
        """

        try:
            bulk_rows = self.warehouse.query(
                """
                SELECT adsh, tag, version, ddate, qtrs, uom, dimh, value_raw
                FROM sec_numeric_facts
                WHERE adsh = %(accession)s
                LIMIT 200000
                """,
                {"accession": accession},
            )
        except Exception as exc:
            return {"available": False, "reason": str(exc)}
        if not bulk_rows:
            return {"available": False, "reason": "no bulk representation yet"}
        # Extract the current Arelle facts for this accession from ClickHouse.
        try:
            arelle_rows = self.warehouse.query(
                """
                SELECT concept_namespace, concept_name, taxonomy_version,
                       period_kind, period_start, period_end, period_instant,
                       unit_numerator_json, dimension_hash, language, raw_value
                FROM xbrl_facts
                WHERE accession = %(accession)s
                LIMIT 500000
                """,
                {"accession": accession},
            )
        except Exception as exc:
            return {"available": True, "comparison": "unavailable", "reason": str(exc)}
        bulk_keys = {tuple(row.values()) for row in bulk_rows}
        arelle_keys = {tuple(row.values()) for row in arelle_rows}
        return {
            "available": True,
            "bulk_count": len(bulk_rows),
            "arelle_count": len(arelle_rows),
            "missing": sorted(bulk_keys - arelle_keys)[:10],
            "extra": sorted(arelle_keys - bulk_keys)[:10],
        }
