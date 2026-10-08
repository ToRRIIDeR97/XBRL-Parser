from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_json

from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import XbrlExtractionError


def _json_chunks(value: Any, *, top: bool = True) -> Iterator[bytes]:
    """JSON for `value`, expanding only the top-level model and its containers.

    Nested models (facts, contexts, ...) are serialized whole by pydantic-core,
    which gives the same bytes as walking their fields one scalar at a time.
    """
    if isinstance(value, BaseModel):
        if not top:
            yield to_json(value)
            return
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            yield to_json(str(key)) + b":"
            yield from _json_chunks(item, top=False)
        yield b"}"
    elif isinstance(value, (tuple, list)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(item, top=False)
        yield b"]"
    else:
        yield to_json(value)


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_BUFFER_BYTES = 1024 * 1024


def write_json(value: Any, path: Path, max_bytes: int) -> None:
    """Serialize entity-by-entity without a whole-bundle dict/string/bytes copy.

    A path ending in `.zst` is written as a zstd stream; `max_bytes` always bounds
    the uncompressed JSON.
    """
    size = 0
    pending = bytearray()
    try:
        with _open_output(path) as output:
            for chunk in _json_chunks(value):
                size += len(chunk)
                if size > max_bytes:
                    raise XbrlExtractionError("Extraction exceeds configured serialized byte limit")
                pending += chunk
                if len(pending) >= _BUFFER_BYTES:
                    output.write(bytes(pending))
                    pending.clear()
            if pending:
                output.write(bytes(pending))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read_bundle(path: Path, max_bytes: int) -> XbrlExtractionBundle:
    """Read plain or zstd-compressed extraction JSON (detected by content).

    `max_bytes` bounds the uncompressed JSON, checked while decompressing.
    """
    with path.open("rb") as handle:
        compressed = handle.read(len(_ZSTD_MAGIC)) == _ZSTD_MAGIC
    if not compressed:
        if path.stat().st_size > max_bytes:
            raise XbrlExtractionError("Extraction exceeds configured serialized byte limit")
        return XbrlExtractionBundle.model_validate_json(path.read_bytes())
    import pyarrow as pa

    data = bytearray()
    with pa.CompressedInputStream(pa.OSFile(str(path), "rb"), "zstd") as source:
        while chunk := source.read(_BUFFER_BYTES):
            data += chunk
            if len(data) > max_bytes:
                raise XbrlExtractionError("Extraction exceeds configured serialized byte limit")
    return XbrlExtractionBundle.model_validate_json(data)


def _open_output(path: Path) -> Any:
    if path.suffix != ".zst":
        return path.open("wb")
    import pyarrow as pa

    return pa.CompressedOutputStream(str(path), "zstd")
