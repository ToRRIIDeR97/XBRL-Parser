from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter


def test_parquet_writer_partitions_and_replays_immutable_batches(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        temp_dir=tmp_path / "tmp",
        parquet_compression="none",
    )
    settings.ensure_directories()
    store = LocalObjectStore(tmp_path / "objects", "test")
    writer = ParquetBatchWriter(settings, store)
    batches = (
        pa.record_batch({"id": [1, 2], "value": ["a", "b"]}),
        pa.table({"id": [3], "value": ["c"]}),
    )

    first = writer.write_batches(
        batches,
        destination_prefix="silver/sub",
        partition_values={"period": "2025Q1", "dataset": "notes"},
    )
    second = writer.write_batches(
        batches,
        destination_prefix="silver/sub",
        partition_values={"period": "2025Q1", "dataset": "notes"},
    )

    assert [item.key for item in first] == [item.key for item in second]
    assert "/dataset=notes/period=2025Q1/" in first[0].key
    assert first[0].local_path is not None
    assert pq.read_table(first[0].local_path).column("id").to_pylist() == [1, 2]


def test_checkpoint_verifies_bytes_and_rows(tmp_path):
    import pyarrow as pa
    import pytest

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
    from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
    from sec_xbrl_warehouse.infrastructure.storage.parquet import (
        ParquetBatchWriter,
        verify_checkpoint_objects,
    )

    settings = Settings(temp_dir=tmp_path)
    store = LocalObjectStore(tmp_path / "objects", "test")
    writer = ParquetBatchWriter(settings, store)
    objects = writer.write_batches(
        [pa.table({"value": ["retained"]})], destination_prefix="silver", partition_values={}
    )
    checkpoint = {
        "rows": 1,
        "objects": [o.key for o in objects],
        "object_records": [o.model_dump(mode="json") for o in objects],
    }
    verify_checkpoint_objects(checkpoint, store, settings)
    with pytest.raises(ArtifactIntegrityError, match="row count"):
        verify_checkpoint_objects({**checkpoint, "rows": 2}, store, settings)
    path = next((tmp_path / "objects").rglob("*.parquet"))
    path.write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        verify_checkpoint_objects(checkpoint, store, settings)
