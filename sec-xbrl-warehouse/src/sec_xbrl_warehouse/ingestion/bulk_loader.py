from __future__ import annotations

import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa

from sec_xbrl_warehouse.domain.enums import SEC_TABLE_LOAD_ORDER, ArtifactState, SecTable
from sec_xbrl_warehouse.domain.models import LoadStats, SourceArtifactRef
from sec_xbrl_warehouse.domain.ports import BatchWriter, ControlRepository, Warehouse
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, DataQualityError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import sec_warehouse_schema
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file, stable_json_hash
from sec_xbrl_warehouse.ingestion.bulk_downloader import validate_zip_integrity
from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader

_SUB_DERIVED = {
    SecTable.NUM: ("cik", "filed", "form", "fy", "fp"),
    SecTable.TXT: ("cik", "filed", "form", "fy", "fp"),
}
_LINEAGE = ("_artifact_id", "_source_period", "_source_revision", "_row_hash", "_ingested_at")

_GOLD_TABLES = {
    "sub": "sec_submissions",
    "tag": "sec_tags",
    "dim": "sec_dimensions",
    "num": "sec_numeric_facts",
    "txt": "sec_text_facts",
    "ren": "sec_renderings",
    "pre": "sec_presentations",
    "cal": "sec_calculations",
}


class BulkArtifactLoader:
    """Load one verified SEC FSNDS ZIP into Parquet and ClickHouse."""

    def __init__(
        self,
        reader: SecTsvBatchReader,
        parquet_writer: BatchWriter,
        warehouse: Warehouse,
        control: ControlRepository,
    ) -> None:
        self.reader = reader
        self.parquet_writer = parquet_writer
        self.warehouse = warehouse
        self.control = control

    async def load_artifact(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
    ) -> LoadStats:
        """Execute a restartable, idempotent artifact load.

        Stages: checksum -> VALIDATING -> LOADING (per table) -> RECONCILING ->
        COMPLETE. Schema/data failures mark QUARANTINED; the caller re-raises so
        transient infrastructure failures mark FAILED.
        """
        if not os.path.isfile(local_zip):  # noqa: ASYNC240 - cheap startup check
            raise ArtifactIntegrityError(f"Local ZIP missing: {local_zip}")
        if artifact.checksum_sha256 and sha256_file(local_zip) != artifact.checksum_sha256:
            raise ArtifactIntegrityError(
                f"Checksum mismatch for {local_zip}: expected {artifact.checksum_sha256}"
            )

        await self.control.mark_artifact_state(artifact.id, ArtifactState.VALIDATING)
        validate_zip_integrity(local_zip)
        with zipfile.ZipFile(local_zip) as archive:
            manifest = self.reader.validate_manifest(archive)
            for table in SEC_TABLE_LOAD_ORDER:
                with archive.open(manifest.members[table]) as binary:
                    import csv
                    import io

                    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                    header = next(csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE), None)
                    self.reader.validate_header(table, header or [])
        await self.control.mark_artifact_state(artifact.id, ArtifactState.LOADING)

        sub_lookup = self._build_sub_lookup(local_zip)
        rows_by_table: dict[str, int] = {}
        parquet_keys: list[str] = []
        started = datetime.now(UTC)
        for table in SEC_TABLE_LOAD_ORDER:
            count, keys = await self._load_table(artifact, local_zip, table, sub_lookup=sub_lookup)
            rows_by_table[table.value] = count
            parquet_keys.extend(keys)

        await self.control.mark_artifact_state(artifact.id, ArtifactState.RECONCILING)
        issue_count, _details = await self._reconcile(artifact.id, rows_by_table)
        await self.control.mark_artifact_state(artifact.id, ArtifactState.COMPLETE)
        return LoadStats(
            artifact_id=artifact.id,
            source_period=artifact.period,
            rows_by_table=rows_by_table,
            parquet_objects=tuple(parquet_keys),
            elapsed_seconds=(datetime.now(UTC) - started).total_seconds(),
            validation_issue_count=issue_count,
        )

    def _build_sub_lookup(self, local_zip: Path) -> dict[str, dict[str, Any]]:
        """Read the SUB member once to denormalize CIK/form/filed fields on NUM/TXT."""
        if not local_zip.is_file():
            return {}
        lookup: dict[str, dict[str, Any]] = {}
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
                for row in typed_rows:
                    adsh = row.get("adsh")
                    if adsh:
                        lookup[str(adsh)] = row
        except Exception:
            # SUB is validated separately; a lookup failure must not mask it.
            return lookup
        return lookup

    async def _load_table(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
        table: SecTable,
        *,
        sub_lookup: dict[str, dict[str, Any]],
    ) -> tuple[int, tuple[str, ...]]:
        """Stream, type, enrich, archive, and load one SEC source table.

        The ZIP is re-opened per stage so no stage holds a decompressed member;
        memory stays bounded to `batch_rows`.
        """
        keys = await self._write_silver_parquet(artifact, local_zip, table, sub_lookup=sub_lookup)
        count = await self._write_clickhouse(artifact, local_zip, table, sub_lookup=sub_lookup)
        return count, keys

    async def _write_silver_parquet(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
        table: SecTable,
        *,
        sub_lookup: dict[str, dict[str, Any]],
    ) -> tuple[str, ...]:
        """Write deterministic, versioned Parquet parts and return object keys."""
        batches = (
            enrich_sec_batch(
                table,
                batch,
                artifact_id=str(artifact.id),
                source_period=artifact.period,
                source_revision=artifact.revision,
                sub_lookup=sub_lookup,
            )
            for batch in self.reader.iter_batches(local_zip, table)
        )
        prefix = (
            f"silver/sec/fsnds/{table.value}/"
            f"source_period={artifact.period}/artifact={artifact.id}/revision={artifact.revision}"
        )
        objects = self.parquet_writer.write_batches(
            batches,
            destination_prefix=prefix,
            partition_values={"source_period": artifact.period},
        )
        return tuple(item.key for item in objects)

    async def _write_clickhouse(
        self,
        artifact: SourceArtifactRef,
        local_zip: Path,
        table: SecTable,
        *,
        sub_lookup: dict[str, dict[str, Any]],
    ) -> int:
        """Write typed Arrow batches and return the total accepted row count."""
        total = 0
        for batch in self.reader.iter_batches(local_zip, table):
            enriched = enrich_sec_batch(
                table,
                batch,
                artifact_id=str(artifact.id),
                source_period=artifact.period,
                source_revision=artifact.revision,
                sub_lookup=sub_lookup,
            )
            total += self.warehouse.insert_sec_batch(table, enriched)
        return total

    async def _reconcile(
        self,
        artifact_id: UUID,
        expected_rows: dict[str, int],
    ) -> tuple[int, dict[str, Any]]:
        """Return `(issue_count, details)` after source-to-gold row-count checks."""
        details: dict[str, Any] = {}
        issues = 0
        for table_key, expected in expected_rows.items():
            table_name = _GOLD_TABLES[table_key]
            try:
                rows = self.warehouse.query(
                    f"SELECT count() AS n FROM {table_name} WHERE _artifact_id = %(aid)s",
                    {"aid": str(artifact_id)},
                )
                actual = int(rows[0]["n"]) if rows else 0
            except Exception as exc:
                actual = -1
                details[table_key] = {"source": expected, "gold": "error", "error": str(exc)}
                issues += 1
                continue
            details[table_key] = {"source": expected, "gold": actual}
            if actual != expected:
                issues += 1
                details[table_key]["delta"] = actual - expected
        if issues:
            raise DataQualityError(f"Artifact {artifact_id} failed reconciliation: {details}")
        return 0, details


def enrich_sec_batch(
    table: SecTable,
    batch: pa.RecordBatch | pa.Table,
    *,
    artifact_id: str,
    source_period: str,
    source_revision: int,
    sub_lookup: dict[str, dict[str, Any]] | None = None,
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
                enriched[name] = datetime.now(UTC)
        enriched_rows.append(enriched)

    return pa.RecordBatch.from_pylist(enriched_rows, schema=target)


def _row_payload(table: SecTable, row: dict[str, Any]) -> dict[str, Any]:
    return {"table": table.value, **row}
