from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import traceback
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import SEC_TABLE_LOAD_ORDER, ArtifactState, SecTable, Severity
from sec_xbrl_warehouse.domain.models import LoadStats, SourceArtifactRef, ValidationIssue
from sec_xbrl_warehouse.domain.ports import BatchWriter, ControlRepository, ObjectStore, Warehouse
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, DataQualityError, SchemaDriftError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import sec_warehouse_schema
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file, stable_json_hash
from sec_xbrl_warehouse.ingestion.bulk_downloader import validate_zip_integrity
from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader
from sec_xbrl_warehouse.observability.metrics import (
    ROWS_PARSED,
    ROWS_WRITTEN,
    SCHEMA_DRIFT,
    STAGE_LATENCY,
)
from sec_xbrl_warehouse.validation.data_quality import ArtifactReconciler, require_quality

_SUB_DERIVED = {
    SecTable.NUM: ("cik", "filed", "form", "fy", "fp"),
    SecTable.TXT: ("cik", "filed", "form", "fy", "fp"),
}
_LINEAGE = ("_artifact_id", "_source_period", "_source_revision", "_row_hash", "_ingested_at")


class BulkArtifactLoader:
    """Load one verified SEC FSNDS ZIP into Parquet and ClickHouse."""

    def __init__(
        self,
        reader: SecTsvBatchReader,
        parquet_writer: BatchWriter,
        warehouse: Warehouse,
        control: ControlRepository,
        object_store: ObjectStore | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.reader = reader
        self.parquet_writer = parquet_writer
        self.warehouse = warehouse
        self.control = control
        self.object_store = object_store
        self.settings = settings or Settings()

    async def load_artifact(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
        *,
        job_id: UUID | None = None,
    ) -> LoadStats:
        """Execute a restartable, idempotent artifact load.

        Stages: checksum -> VALIDATING -> LOADING (per table) -> RECONCILING ->
        COMPLETE. Schema/data failures mark QUARANTINED; the caller re-raises so
        transient infrastructure failures mark FAILED.
        """
        artifact = await self.control.get_source_artifact(artifact.id)
        if artifact.state == ArtifactState.COMPLETE:
            return LoadStats.model_validate(artifact.details["load_stats"])
        if artifact.state in {ArtifactState.LOADING, ArtifactState.RECONCILING}:
            await self.control.mark_artifact_state(artifact.id, ArtifactState.FAILED)
        started = datetime.now(UTC)
        try:
            await self.control.mark_artifact_state(artifact.id, ArtifactState.VALIDATING)
            await asyncio.to_thread(self._validate_source, artifact, local_zip)
            await self.control.mark_artifact_state(artifact.id, ArtifactState.LOADING)
            sub_lookup = await asyncio.to_thread(self._build_sub_lookup, local_zip)
            try:
                rows_by_table: dict[str, int] = {}
                parquet_keys: list[str] = []
                for table in SEC_TABLE_LOAD_ORDER:
                    count, keys = await self._load_table(
                        artifact, local_zip, table, sub_lookup=sub_lookup, job_id=job_id
                    )
                    rows_by_table[table.value] = count
                    parquet_keys.extend(keys)
            finally:
                await asyncio.to_thread(sub_lookup.close)
            await self.control.mark_artifact_state(artifact.id, ArtifactState.RECONCILING)
            issue_count, details = await self._reconcile(artifact.id, rows_by_table)
            stats = LoadStats(
                artifact_id=artifact.id,
                source_period=artifact.period,
                rows_by_table=rows_by_table,
                parquet_objects=tuple(parquet_keys),
                elapsed_seconds=(datetime.now(UTC) - started).total_seconds(),
                validation_issue_count=issue_count,
            )
            await self.control.mark_artifact_state(
                artifact.id,
                ArtifactState.COMPLETE,
                details={"load_stats": stats.model_dump(mode="json"), "reconciliation": details},
            )
            return stats
        except Exception as exc:
            quarantined = isinstance(
                exc, (ArtifactIntegrityError, SchemaDriftError, DataQualityError)
            )
            details = {
                "error": str(exc),
                "exception": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
            diagnostic_key = None
            if self.object_store is not None:
                encoded = json.dumps(details, sort_keys=True).encode()
                diagnostic_key = f"quarantine/bulk/{artifact.id}/{stable_json_hash(details)}.json"
                await asyncio.to_thread(self.object_store.put_bytes, encoded, diagnostic_key)
            if isinstance(exc, SchemaDriftError):
                SCHEMA_DRIFT.labels(
                    dataset=artifact.dataset.value, table="unknown", kind="schema"
                ).inc()
            await self.control.record_validation_issues(
                [
                    ValidationIssue(
                        source="bulk",
                        code=type(exc).__name__,
                        severity=Severity.ERROR,
                        message=str(exc)[:16000],
                        artifact_id=artifact.id,
                        details={
                            "diagnostic_object_key": diagnostic_key,
                            "source_object_key": artifact.object_key,
                        },
                    )
                ]
            )
            await self.control.mark_artifact_state(
                artifact.id,
                ArtifactState.QUARANTINED if quarantined else ArtifactState.FAILED,
                details={"last_error": str(exc)[:16000], "diagnostic_object_key": diagnostic_key},
            )
            raise

    def _validate_source(self, artifact: SourceArtifactRef, local_zip: Path) -> None:
        import csv
        import io

        if not local_zip.is_file():
            raise ArtifactIntegrityError(f"Local ZIP missing: {local_zip}")
        if artifact.checksum_sha256 and sha256_file(local_zip) != artifact.checksum_sha256:
            raise ArtifactIntegrityError(
                f"Checksum mismatch for {local_zip}: expected {artifact.checksum_sha256}"
            )
        validate_zip_integrity(local_zip, self.settings)
        with zipfile.ZipFile(local_zip) as archive:
            manifest = self.reader.validate_manifest(archive)
            for table in SEC_TABLE_LOAD_ORDER:
                with archive.open(manifest.members[table]) as binary:
                    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                    header = next(csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE), None)
                    self.reader.validate_header(table, header or [])

    def _build_sub_lookup(self, local_zip: Path) -> _SubLookup:
        """Read the SUB member once to denormalize CIK/form/filed fields on NUM/TXT."""
        lookup = _SubLookup()
        try:
            for batch in self.reader.iter_batches(local_zip, SecTable.SUB):
                # Coerce to the typed SUB contract so denormalized values carry
                # the right Arrow types (cik as int, filed as date, ...).
                from sec_xbrl_warehouse.ingestion.sec_tsv import coerce_sec_batch

                typed_rows = coerce_sec_batch(
                    SecTable.SUB,
                    batch.to_pylist(),
                    start_line=int(batch.schema.metadata.get(b"source_start_line", b"2")),
                ).to_pylist()
                lookup.add(typed_rows)
            lookup.commit()
            return lookup
        except Exception:
            lookup.close()
            raise

    async def _load_table(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
        table: SecTable,
        *,
        sub_lookup: _SubLookup | dict[str, dict[str, Any]],
        job_id: UUID | None = None,
    ) -> tuple[int, tuple[str, ...]]:
        """Stream, type, enrich, archive, and load one SEC source table.

        The ZIP is re-opened per stage so no stage holds a decompressed member;
        memory stays bounded to `batch_rows`.
        """
        count = 0
        keys: list[str] = []
        batches = self.reader.iter_batches(local_zip, table)
        index = 0
        while (batch := await asyncio.to_thread(next, batches, None)) is not None:
            key = f"{artifact.id}:{artifact.revision}:{table.value}:{index}"
            checkpoint = (
                await self.control.get_checkpoint(job_id, "bulk_batch", key) if job_id else None
            )
            if checkpoint is None:
                with STAGE_LATENCY.labels(pipeline="bulk", stage=table.value).time():
                    batch_lookup = (
                        await asyncio.to_thread(
                            sub_lookup.select,
                            set(filter(None, batch.column("adsh").to_pylist()))
                            if "adsh" in batch.schema.names
                            else set(),
                        )
                        if isinstance(sub_lookup, _SubLookup)
                        else sub_lookup
                    )
                    enriched = await asyncio.to_thread(
                        enrich_sec_batch,
                        table,
                        batch,
                        artifact_id=str(artifact.id),
                        source_period=artifact.period,
                        source_revision=artifact.revision,
                        sub_lookup=batch_lookup,
                        ingested_at=artifact.created_at,
                    )
                    ROWS_PARSED.labels(source="bulk", table=table.value).inc(enriched.num_rows)
                    prefix = f"silver/sec/fsnds/{table.value}/artifact={artifact.id}/revision={artifact.revision}/batch={index}"
                    objects = await asyncio.to_thread(
                        self.parquet_writer.write_batches,
                        [enriched],
                        destination_prefix=prefix,
                        partition_values={"source_period": artifact.period},
                    )
                    ROWS_WRITTEN.labels(storage="parquet", table=table.value).inc(enriched.num_rows)
                    written = await asyncio.to_thread(
                        self.warehouse.insert_sec_batch, table, enriched
                    )
                    checkpoint = {"rows": written, "objects": [item.key for item in objects]}
                    if job_id:
                        await self.control.save_checkpoint(job_id, "bulk_batch", checkpoint, key)
            count += int(checkpoint["rows"])
            keys.extend(checkpoint["objects"])
            index += 1
        return count, tuple(keys)

    async def _reconcile(
        self,
        artifact_id: UUID,
        expected_rows: dict[str, int],
    ) -> tuple[int, dict[str, Any]]:
        """Return `(issue_count, details)` after source-to-gold row-count checks."""
        checks = await asyncio.to_thread(
            ArtifactReconciler(self.warehouse).run, str(artifact_id), expected_rows
        )
        require_quality(checks)
        return 0, {
            check.name: {"observed": check.observed, "expected": check.expected} for check in checks
        }


def enrich_sec_batch(
    table: SecTable,
    batch: pa.RecordBatch | pa.Table,
    *,
    artifact_id: str,
    source_period: str,
    source_revision: int,
    sub_lookup: dict[str, dict[str, Any]] | None = None,
    ingested_at: datetime | None = None,
) -> pa.RecordBatch:
    """Enrich a typed batch into the exact `sec_warehouse_schema` contract.

    Adds SUB-derived columns (NUM/TXT), ingest lineage, and a deterministic row
    hash over source-controlled values (timestamps excluded).
    """
    target = sec_warehouse_schema(table)
    typed = batch if isinstance(batch, pa.RecordBatch) else batch.to_batches()[0]
    rows = typed.to_pylist()
    lookup = sub_lookup or {}

    # Raw reader batches are string-typed; coerce to the typed contract so
    # dates/ints/decimals have Arrow-native Python values before enrichment.
    if not any(
        not isinstance(value, str) for row in rows for value in row.values() if value is not None
    ):
        from sec_xbrl_warehouse.ingestion.sec_tsv import coerce_sec_batch

        metadata = typed.schema.metadata or {}
        start_line = int(metadata.get(b"source_start_line", b"2"))
        typed = coerce_sec_batch(table, rows, start_line=start_line)
        rows = typed.to_pylist()

    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        enriched: dict[str, Any] = {field.name: None for field in target}
        for field in target:
            name = field.name
            if name in row:
                enriched[name] = row[name]
            elif name in _SUB_DERIVED.get(table, ()):
                sub = lookup.get(str(row.get("adsh")))
                enriched[name] = sub.get(name) if sub else None
            elif name == "_artifact_id":
                enriched[name] = artifact_id
            elif name == "_source_period":
                enriched[name] = source_period
            elif name == "_source_revision":
                enriched[name] = source_revision
            elif name == "_row_hash":
                enriched[name] = stable_json_hash(
                    _row_payload(table, {k: v for k, v in row.items() if not k.startswith("_")})
                )
            elif name == "_ingested_at":
                enriched[name] = ingested_at or datetime.now(UTC)
        enriched_rows.append(enriched)

    return pa.RecordBatch.from_pylist(enriched_rows, schema=target)


def _row_payload(table: SecTable, row: dict[str, Any]) -> dict[str, Any]:
    return {"table": table.value, **row}


class _SubLookup:
    """Disk-backed bounded-memory projection of SUB used by NUM/TXT batches."""

    def __init__(self) -> None:
        with tempfile.NamedTemporaryFile(
            prefix="xbrl-sub-", suffix=".sqlite", delete=False
        ) as handle:
            self.path = Path(handle.name)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE sub (adsh TEXT PRIMARY KEY, cik INTEGER, filed TEXT, "
            "form TEXT, fy INTEGER, fp TEXT)"
        )

    def add(self, rows: list[dict[str, Any]]) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO sub VALUES (?, ?, ?, ?, ?, ?)",
            (
                (row["adsh"], row["cik"], str(row["filed"]), row["form"], row["fy"], row["fp"])
                for row in rows
            ),
        )

    def commit(self) -> None:
        self.connection.commit()

    def select(self, accessions: set[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        ordered = sorted(accessions)
        for offset in range(0, len(ordered), 900):
            chunk = ordered[offset : offset + 900]
            if not chunk:
                continue
            rows = self.connection.execute(
                f"SELECT * FROM sub WHERE adsh IN ({','.join('?' for _ in chunk)})", chunk
            )
            for adsh, cik, filed, form, fy, fp in rows:
                result[adsh] = {
                    "cik": cik,
                    "filed": date.fromisoformat(filed),
                    "form": form,
                    "fy": fy,
                    "fp": fp,
                }
        return result

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)
