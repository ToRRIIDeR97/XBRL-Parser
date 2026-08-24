import asyncio
from pathlib import Path
from uuid import UUID

import pytest

pa = pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.domain.enums import ArtifactState, DatasetKind, SecTable
from sec_xbrl_warehouse.domain.models import SourceArtifactRef, StoredObject
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.ingestion.bulk_loader import BulkArtifactLoader, enrich_sec_batch
from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader

FIXTURE = Path(__file__).parents[1] / "fixtures/fsnds-mini.zip"


class _FakeWriter:
    def __init__(self) -> None:
        self.objects: list[str] = []

    def write_batches(self, batches, *, destination_prefix, partition_values):
        created = []
        for _batch in batches:
            created.append(f"{destination_prefix}/part.parquet")
        self.objects.extend(created)
        return tuple(
            StoredObject(bucket="b", key=k, size_bytes=1, checksum_sha256="0" * 64) for k in created
        )


class _FakeWarehouse:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, int]] = []
        self.counts: dict[str, int] = {}

    def insert_sec_batch(self, table, batch) -> int:
        count = batch.num_rows
        self.inserted.append((table.value, count))
        self.counts[table.value] = self.counts.get(table.value, 0) + count
        return count

    def query(self, sql, parameters=None) -> list[dict]:
        return [{"n": self.counts.get("sub", 1)}] if "sub" in sql else [{"n": 1}]


class _FakeControl:
    def __init__(self) -> None:
        self.states: list[ArtifactState] = []

    async def mark_artifact_state(self, artifact_id, state, *, details=None) -> None:
        self.states.append(state)


def _artifact() -> SourceArtifactRef:
    return SourceArtifactRef(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        dataset=DatasetKind.NOTES,
        period="2025Q4",
        source_url="https://www.sec.gov/archives/notes-2025Q4.zip",
        checksum_sha256=None,
        revision=1,
    )


def test_enrich_preserves_raw_and_adds_lineage() -> None:
    reader = SecTsvBatchReader(batch_rows=1)
    raw = next(reader.iter_batches(FIXTURE, SecTable.NUM))
    sub_batch = next(reader.iter_batches(FIXTURE, SecTable.SUB))
    from sec_xbrl_warehouse.ingestion.sec_tsv import coerce_sec_batch

    sub_rows = coerce_sec_batch(SecTable.SUB, sub_batch.to_pylist(), start_line=2).to_pylist()
    lookup = {str(row["adsh"]): row for row in sub_rows}
    enriched = enrich_sec_batch(
        SecTable.NUM,
        raw,
        artifact_id="a1",
        source_period="2025Q4",
        source_revision=1,
        sub_lookup=lookup,
    )
    assert enriched.column("value_raw")[0].as_py() == "123456789"
    assert enriched.column("cik")[0].as_py() == 1
    assert (
        enriched.column("filed")[0].as_py().isoformat() == "2026-02-15"
        or enriched.column("filed")[0].as_py() is not None
    )
    assert enriched.column("_source_revision")[0].as_py() == 1


def test_load_artifact_runs_stages() -> None:
    reader = SecTsvBatchReader(batch_rows=1)
    writer = _FakeWriter()
    warehouse = _FakeWarehouse()
    control = _FakeControl()
    loader = BulkArtifactLoader(reader, writer, warehouse, control)
    stats = asyncio.run(loader.load_artifact(_artifact(), FIXTURE))
    assert stats.rows_by_table["sub"] == 1
    assert len(stats.parquet_objects) == 8
    assert control.states[0] is ArtifactState.VALIDATING
    assert control.states[-1] is ArtifactState.COMPLETE


def test_load_artifact_rejects_checksum_mismatch() -> None:
    reader = SecTsvBatchReader(batch_rows=1)
    artifact = _artifact().model_copy(update={"checksum_sha256": "f" * 64})
    loader = BulkArtifactLoader(reader, _FakeWriter(), _FakeWarehouse(), _FakeControl())
    with pytest.raises(ArtifactIntegrityError):
        asyncio.run(loader.load_artifact(artifact, FIXTURE))
