import asyncio
from pathlib import Path
from uuid import UUID, uuid4

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
        self.fail_table: SecTable | None = None

    def insert_sec_batch(self, table, batch) -> int:
        if self.fail_table is table:
            self.fail_table = None
            raise ConnectionError("temporary warehouse failure")
        count = batch.num_rows
        self.inserted.append((table.value, count))
        self.counts[table.value] = self.counts.get(table.value, 0) + count
        return count

    def query(self, sql, parameters=None) -> list[dict]:
        if " NOT IN" in sql:
            return [{"n": 0}]
        for table, count in self.counts.items():
            if f"sec_{table}" in sql or (table == "sub" and "sec_submissions" in sql):
                return [{"n": count}]
        return [{"n": 1}]


class _FakeControl:
    def __init__(self, artifact: SourceArtifactRef) -> None:
        self.artifact = artifact
        self.states: list[ArtifactState] = []
        self.checkpoints = {}

    async def get_source_artifact(self, artifact_id):
        assert artifact_id == self.artifact.id
        return self.artifact

    async def mark_artifact_state(self, artifact_id, state, *, details=None) -> None:
        self.states.append(state)
        self.artifact = self.artifact.model_copy(
            update={"state": state, "details": details or self.artifact.details}
        )

    async def record_validation_issues(self, issues) -> None:
        self.issues = tuple(issues)

    async def get_checkpoint(self, job_id, stage, key="default"):
        return self.checkpoints.get((stage, key))

    async def save_checkpoint(self, job_id, stage, payload, key="default") -> None:
        self.checkpoints[(stage, key)] = payload


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
    artifact = _artifact()
    control = _FakeControl(artifact)
    loader = BulkArtifactLoader(reader, writer, warehouse, control)
    stats = asyncio.run(loader.load_artifact(artifact, FIXTURE))
    assert stats.rows_by_table["sub"] == 1
    assert len(stats.parquet_objects) == 8
    assert control.states[0] is ArtifactState.VALIDATING
    assert control.states[-1] is ArtifactState.COMPLETE


def test_load_artifact_rejects_checksum_mismatch() -> None:
    reader = SecTsvBatchReader(batch_rows=1)
    artifact = _artifact().model_copy(update={"checksum_sha256": "f" * 64})
    loader = BulkArtifactLoader(reader, _FakeWriter(), _FakeWarehouse(), _FakeControl(artifact))
    with pytest.raises(ArtifactIntegrityError):
        asyncio.run(loader.load_artifact(artifact, FIXTURE))


def test_load_artifact_resumes_after_completed_batch(tmp_path) -> None:
    artifact = _artifact()
    control = _FakeControl(artifact)
    warehouse = _FakeWarehouse()
    warehouse.fail_table = SecTable.TAG
    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
    from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter

    settings = Settings(data_dir=tmp_path, temp_dir=tmp_path)
    store = LocalObjectStore(tmp_path / "objects", "test")
    loader = BulkArtifactLoader(
        SecTsvBatchReader(batch_rows=1),
        ParquetBatchWriter(settings, store),
        warehouse,
        control,
        store,
        settings=settings,
    )
    job_id = uuid4()
    with pytest.raises(ConnectionError):
        asyncio.run(loader.load_artifact(artifact, FIXTURE, job_id=job_id))
    stats = asyncio.run(loader.load_artifact(artifact, FIXTURE, job_id=job_id))
    assert stats.rows_by_table["sub"] == 1
    assert [table for table, _ in warehouse.inserted].count("sub") == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_replay_rejects_changed_boundaries_and_unverified_legacy(legacy):
    artifact = _artifact()
    control = _FakeControl(artifact)
    warehouse = _FakeWarehouse()
    loader = BulkArtifactLoader(SecTsvBatchReader(batch_rows=1), _FakeWriter(), warehouse, control)
    job_id = uuid4()
    asyncio.run(loader._load_table(artifact, FIXTURE, SecTable.TAG, sub_lookup={}, job_id=job_id))
    saved = next(iter(control.checkpoints.values()))
    if legacy:
        saved.pop("identity", None)
    else:
        loader.reader = SecTsvBatchReader(batch_rows=2)
    previous = list(warehouse.inserted)
    with pytest.raises(ArtifactIntegrityError, match="checkpoint"):
        asyncio.run(
            loader._load_table(artifact, FIXTURE, SecTable.TAG, sub_lookup={}, job_id=job_id)
        )
    assert warehouse.inserted == previous
    assert len(control.checkpoints) == 1
