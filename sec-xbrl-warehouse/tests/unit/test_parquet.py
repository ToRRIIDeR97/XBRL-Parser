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
