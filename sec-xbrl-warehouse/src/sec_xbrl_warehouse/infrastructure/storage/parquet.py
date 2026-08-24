from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import StoredObject
from sec_xbrl_warehouse.domain.ports import ObjectStore


class ParquetBatchWriter:
    """Write Arrow batches to immutable Parquet objects.

    One object is produced per incoming batch. This is simple and restartable;
    production compaction can be a separate job after source reconciliation.
    """

    def __init__(self, settings: Settings, object_store: ObjectStore) -> None:
        self.settings = settings
        self.object_store = object_store

    def write_batches(
        self,
        batches: Iterable[pa.RecordBatch | pa.Table],
        *,
        destination_prefix: str,
        partition_values: dict[str, str],
    ) -> tuple[StoredObject, ...]:
        objects: list[StoredObject] = []
        partition_path = "/".join(
            f"{key}={value}" for key, value in sorted(partition_values.items())
        )
        prefix = "/".join(part.strip("/") for part in (destination_prefix, partition_path) if part)

        for index, batch in enumerate(batches):
            table = batch if isinstance(batch, pa.Table) else pa.Table.from_batches([batch])
            object_key = f"{prefix}/part-{index:06d}-{uuid4().hex}.parquet"
            with tempfile.NamedTemporaryFile(
                dir=self.settings.temp_dir,
                prefix="xbrl-parquet-",
                suffix=".parquet",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
            try:
                pq.write_table(
                    table,
                    tmp_path,
                    compression=None
                    if self.settings.parquet_compression == "none"
                    else self.settings.parquet_compression,
                    compression_level=self.settings.parquet_compression_level,
                    use_dictionary=True,
                    write_statistics=True,
                )
                objects.append(self.object_store.put_file(tmp_path, object_key))
            finally:
                tmp_path.unlink(missing_ok=True)
        return tuple(objects)
