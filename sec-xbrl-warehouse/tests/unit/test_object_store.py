from pathlib import Path

import pytest

from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore


def test_local_object_store_is_immutable(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "bucket")
    first = tmp_path / "first.bin"
    first.write_bytes(b"same")
    stored = store.put_file(first, "path/item.bin")
    assert store.exists(stored.key)

    second = tmp_path / "second.bin"
    second.write_bytes(b"different")
    with pytest.raises(ArtifactIntegrityError):
        store.put_file(second, "path/item.bin")
