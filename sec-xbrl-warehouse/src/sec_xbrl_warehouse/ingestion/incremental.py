from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, ValidationIssue
from sec_xbrl_warehouse.domain.ports import (
    BatchWriter,
    ControlRepository,
    FilingPackageStore,
    ObjectStore,
    Warehouse,
    XbrlExtractor,
)
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import XbrlExtractionError
from sec_xbrl_warehouse.infrastructure.http.egress_lock import SecEgressBusyError
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    iter_xbrl_tables,
)
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes, sha256_file
from sec_xbrl_warehouse.infrastructure.storage.parquet import verify_checkpoint_objects
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle, write_json
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
from sec_xbrl_warehouse.normalization.fiscal_focus import derive_fiscal_focus
from sec_xbrl_warehouse.observability.metrics import (
    ROWS_PARSED,
    ROWS_WRITTEN,
    STAGE_LATENCY,
    VALIDATION_ISSUES,
)
from sec_xbrl_warehouse.validation.filing_reconciliation import reconcile_filing_rows
from sec_xbrl_warehouse.validation.issue_grouping import group_issues
from sec_xbrl_warehouse.validation.xbrl_validation import reject_configured_issues


class IncrementalFilingIngestor:
    """Restartable, silver-first ingestion of an immutable extraction revision."""

    def __init__(
        self,
        control: ControlRepository,
        package_store: FilingPackageStore,
        extractor: XbrlExtractor,
        parquet_writer: BatchWriter,
        warehouse: Warehouse,
        *,
        settings: Settings | None = None,
        object_store: ObjectStore | None = None,
        mapper: CanonicalMapper | None = None,
        ensure_sec_egress: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.control = control
        self.package_store = package_store
        self.extractor = extractor
        self.parquet_writer = parquet_writer
        self.warehouse = warehouse
        self.settings = settings or Settings()
        self.object_store = object_store
        self.mapper = mapper
        # Online Arelle may fetch taxonomies from SEC hosts; hold the SEC egress
        # lock first so those requests count as this process's SEC traffic.
        self.ensure_sec_egress = ensure_sec_egress

    async def ingest(
        self, filing: FilingRef, *, extraction_revision: int = 1, job_id: UUID | None = None
    ) -> dict[str, Any]:
        if extraction_revision < 1:
            raise ValueError("Extraction revisions start at 1")
        if job_id and self.object_store is None:
            raise ValueError("Checkpointed extraction requires object storage")
        completed = await self._checkpoint(job_id, "complete")
        if completed is not None and self.object_store is not None:
            self.settings.ensure_directories()
            for entity in ("contexts", "concepts", "facts", "relationships", "footnotes"):
                checkpoint = await self._checkpoint(job_id, f"silver:{entity}")
                if checkpoint is None:
                    raise XbrlExtractionError("Completed filing has no silver checkpoint")
                await asyncio.to_thread(
                    verify_checkpoint_objects, checkpoint, self.object_store, self.settings
                )
            return completed
        self.settings.ensure_directories()
        await _upsert_discovered_filing(self.control, filing)
        try:
            with tempfile.TemporaryDirectory(
                prefix="filing-", dir=self.settings.temp_dir
            ) as directory:
                staging = Path(directory)
                saved = await self._checkpoint(job_id, "package")
                manifest = (
                    FilingPackageManifest.model_validate(saved)
                    if saved
                    else await self.package_store.download_package(filing)
                )
                if saved is None:
                    await self._save(job_id, "package", manifest.model_dump(mode="json"))
                extracted = await self._checkpoint(job_id, "extract")
                if extracted and self.object_store is not None:
                    path = staging / "extraction.json"
                    stored = await asyncio.to_thread(
                        self.object_store.get_file,
                        extracted["object_key"],
                        path,
                        max_bytes=self.settings.extraction_max_bytes,
                    )
                    if stored.checksum_sha256 != extracted["sha256"]:
                        raise XbrlExtractionError("Saved extraction checksum mismatch")
                    bundle = await asyncio.to_thread(
                        read_bundle, path, self.settings.extraction_max_bytes
                    )
                else:
                    entrypoint = await asyncio.to_thread(
                        self.package_store.materialize_package, manifest, staging
                    )
                    if not manifest.package_object_key:
                        manifest = await asyncio.to_thread(
                            self.package_store.archive_package, manifest, staging
                        )
                    await self.control.update_filing_package(manifest)
                    await self._save(job_id, "package", manifest.model_dump(mode="json"))
                    if self.ensure_sec_egress is not None and not self.settings.arelle_offline:
                        await self.ensure_sec_egress()
                    with STAGE_LATENCY.labels(pipeline="filing", stage="extract").time():
                        bundle = await asyncio.to_thread(
                            self.extractor.extract,
                            entrypoint,
                            filing,
                            extraction_revision=extraction_revision,
                        )
                    if (
                        bundle.extraction_revision != extraction_revision
                        or bundle.filing.accession != filing.accession
                    ):
                        raise XbrlExtractionError("Extractor returned a different filing/revision")
                    for entity, count in {
                        "contexts": len(bundle.contexts),
                        "concepts": len(bundle.concepts),
                        "facts": len(bundle.facts),
                        "relationships": len(bundle.relationships),
                        "footnotes": len(bundle.footnotes),
                    }.items():
                        ROWS_PARSED.labels(source="arelle", table=entity).inc(count)
                    if self.object_store is not None:
                        path = staging / "extraction.json"
                        await asyncio.to_thread(
                            write_json, bundle, path, self.settings.extraction_max_bytes
                        )
                        checksum = await asyncio.to_thread(sha256_file, path)
                        key = f"silver/xbrl/extractions/accession={filing.accession}/revision={extraction_revision}/sha256={checksum}.json"
                        await asyncio.to_thread(
                            self.object_store.put_file, path, key, expected_sha256=checksum
                        )
                        await self._save(job_id, "extract", {"object_key": key, "sha256": checksum})

                # Filing-level fiscal fields come from the bundle's dei facts. This
                # runs on fresh and checkpointed extractions alike, so replay
                # derives the same values; the saved extraction stays as extracted.
                enriched, fiscal_issues = derive_fiscal_focus(bundle)
                bundle = bundle.model_copy(
                    update={
                        "filing": enriched,
                        "validation_issues": (*bundle.validation_issues, *fiscal_issues),
                    }
                )
                filing = enriched
                await self.control.upsert_filing(filing)
                if await self._checkpoint(job_id, "validation") is None:
                    # Repeated diagnostics are stored once per group; the reject
                    # decision below still sees every individual issue.
                    await self.control.record_validation_issues(
                        group_issues(
                            bundle.validation_issues, extraction_revision=extraction_revision
                        )
                    )
                    for issue in bundle.validation_issues:
                        VALIDATION_ISSUES.labels(
                            source=issue.source, severity=issue.severity.value, code=issue.code
                        ).inc()
                    await self._save(
                        job_id, "validation", {"issues": len(bundle.validation_issues)}
                    )
                policy_path = self.settings.config_dir / "validation.yml"
                policy = load_yaml(policy_path).get("xbrl", {}) if policy_path.is_file() else {}
                rejected = reject_configured_issues(
                    bundle.validation_issues,
                    tuple(Severity(value) for value in policy.get("reject_severities", ["fatal"])),
                )
                if rejected:
                    raise XbrlExtractionError(
                        f"Rejected validation issues: {sorted({item.code for item in rejected})}"
                    )
                if not bundle.facts:
                    raise XbrlExtractionError(
                        "Arelle extracted no facts; the filing was not published"
                    )

                counts = await self._persist_bundle(bundle, job_id)
                if self.mapper is not None and await self._checkpoint(job_id, "canonical") is None:
                    mapped = 0
                    async for canonical in self.mapper.map_batches(
                        bundle.facts,
                        filing,
                        extraction_revision=extraction_revision,
                        batch_bytes=self.settings.arrow_batch_bytes,
                    ):
                        await asyncio.to_thread(self.warehouse.insert_canonical_facts, canonical)
                        mapped += len(canonical)
                    await self._save(job_id, "canonical", {"mapped": mapped})
                await asyncio.to_thread(self.warehouse.publish_xbrl_bundle, bundle, counts)
                await self.control.update_filing_package(manifest, extraction_revision)
                reconciliation = await self.reconcile(filing.accession)
                result = {
                    "accession": filing.accession,
                    "extraction_revision": extraction_revision,
                    "entity_counts": counts,
                    "validation_issue_count": len(bundle.validation_issues),
                    "reconciliation": reconciliation,
                    "status": "complete",
                }
                await self._save(job_id, "complete", result)
                return result
        except SecEgressBusyError:
            # A deferral, not a failure: the worker reschedules the job, so record no
            # diagnostics here (they would repeat on every deferral).
            raise
        except Exception as exc:
            details = {
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "extraction_revision": extraction_revision,
            }
            if self.object_store is not None:
                payload = json.dumps(details, sort_keys=True).encode()
                key = f"quarantine/filing/{filing.accession}/sha256={sha256_bytes(payload)}.json"
                await asyncio.to_thread(self.object_store.put_bytes, payload, key)
                details = {"diagnostic_object_key": key, "extraction_revision": extraction_revision}
            engine_issues = getattr(exc, "validation_issues", ())
            await self.control.record_validation_issues(
                group_issues(
                    (
                        *engine_issues,
                        ValidationIssue(
                            source="filing",
                            code=type(exc).__name__,
                            severity=Severity.ERROR,
                            accession=filing.accession,
                            message=str(exc)[:16000],
                            details=details,
                        ),
                    ),
                    extraction_revision=extraction_revision,
                )
            )
            raise

    async def _checkpoint(self, job_id: UUID | None, stage: str) -> dict[str, Any] | None:
        return await self.control.get_checkpoint(job_id, stage) if job_id else None

    async def _save(self, job_id: UUID | None, stage: str, payload: dict[str, Any]) -> None:
        if job_id:
            await self.control.save_checkpoint(job_id, stage, payload)

    async def _persist_bundle(
        self, bundle: XbrlExtractionBundle, job_id: UUID | None = None
    ) -> dict[str, int]:
        for entity in ("contexts", "concepts", "facts", "relationships", "footnotes"):
            checkpoint = await self._checkpoint(job_id, f"silver:{entity}")
            if checkpoint is not None and self.object_store is not None:
                await asyncio.to_thread(
                    verify_checkpoint_objects, checkpoint, self.object_store, self.settings
                )
                continue
            tables = iter_xbrl_tables(
                bundle, entity, self.settings.arrow_batch_rows, self.settings.arrow_batch_bytes
            )
            prefix = f"silver/xbrl/{entity}/filed_year={bundle.filing.filed_date.year}/accession={bundle.filing.accession}/revision={bundle.extraction_revision}"
            objects = await asyncio.to_thread(
                self.parquet_writer.write_batches,
                tables,
                destination_prefix=prefix,
                partition_values={},
            )
            count = len(getattr(bundle, entity))
            ROWS_WRITTEN.labels(storage="parquet", table=entity).inc(count)
            await self._save(
                job_id,
                f"silver:{entity}",
                {
                    "rows": count,
                    "objects": [item.key for item in objects],
                    "object_records": [item.model_dump(mode="json") for item in objects],
                },
            )
        saved = await self._checkpoint(job_id, "gold")
        if saved is not None:
            return {key: int(value) for key, value in saved.items()}
        # Inserts are replay-safe; publication is a separate, last-written marker.
        counts = await asyncio.to_thread(self.warehouse.insert_xbrl_bundle, bundle)
        await self._save(job_id, "gold", counts)
        return counts

    async def reconcile(self, accession: str) -> dict[str, Any]:
        # ponytail: comparison holds one bounded filing; stream/partition if this ceiling is reached.
        params = {
            "accession": accession,
            "max_rows": self.settings.reconciliation_max_rows,
            "max_bytes": self.settings.reconciliation_max_bytes,
        }
        budget = """ SETTINGS max_result_rows = %(max_rows)s,
            max_result_bytes = %(max_bytes)s, result_overflow_mode = 'throw',
            max_execution_time = 60, max_memory_usage = 536870912
        """
        bulk_numeric = await asyncio.to_thread(
            self.warehouse.query,
            """
            SELECT n.tag, n.version, n.ddate, n.qtrs, n.uom, n.value_raw,
                   d.segments, d.segt FROM v_sec_numeric_latest AS n
            LEFT JOIN sec_dimensions AS d FINAL ON n._artifact_id = d._artifact_id AND n.dimh = d.dimh
            WHERE n.adsh = %(accession)s
            """
            + budget,
            params,
        )
        bulk_text = await asyncio.to_thread(
            self.warehouse.query,
            """
            SELECT n.tag, n.version, n.ddate, n.qtrs, n.lang, n.txtlen, n.value AS value_raw,
                   d.segments, d.segt FROM v_sec_text_latest AS n
            LEFT JOIN sec_dimensions AS d FINAL ON n._artifact_id = d._artifact_id AND n.dimh = d.dimh
            WHERE n.adsh = %(accession)s
            """
            + budget,
            params,
        )
        if not bulk_numeric and not bulk_text:
            return {"available": False, "reason": "no bulk representation yet"}
        raw = await asyncio.to_thread(
            self.warehouse.query,
            """SELECT concept_namespace, concept_name, taxonomy_version, dimensions_json,
                      period_kind, period_start, period_end, period_instant, value_kind,
                      unit_id, unit_numerator_json, unit_denominator_json, language,
                      value_decimal, value_text, is_nil, raw_value
               FROM v_xbrl_facts_latest WHERE accession = %(accession)s"""
            + budget,
            params,
        )
        result = await asyncio.to_thread(
            reconcile_filing_rows, accession, bulk_numeric, bulk_text, raw
        )
        if self.object_store is not None:
            data = json.dumps(result, sort_keys=True, default=str).encode()
            key = f"reconciliation/filings/{accession}/sha256={sha256_bytes(data)}.json"
            await asyncio.to_thread(self.object_store.put_bytes, data, key)
            result["report_object_key"] = key
        return result


async def _upsert_discovered_filing(control: ControlRepository, filing: FilingRef) -> None:
    """Upsert discovery metadata without erasing fiscal fields an extraction stored.

    Repositories whose `upsert_filing` lacks the `keep_derived` keyword (test
    doubles, older adapters) get a plain upsert; the post-extraction upsert
    rewrites the derived fields either way.
    """
    parameters = inspect.signature(control.upsert_filing).parameters.values()
    if any(
        parameter.name == "keep_derived" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        await control.upsert_filing(filing, keep_derived=True)
    else:
        await control.upsert_filing(filing)
