from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_json

from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import XbrlExtractionError


def _json_chunks(value: Any) -> Iterator[bytes]:
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            yield to_json(str(key)) + b":"
            yield from _json_chunks(item)
        yield b"}"
    elif isinstance(value, (tuple, list)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _json_chunks(item)
        yield b"]"
    else:
        yield to_json(value)


def write_json(value: Any, path: Path, max_bytes: int) -> None:
    """Serialize entity-by-entity without a whole-bundle dict/string/bytes copy."""
    size = 0
    try:
        with path.open("wb") as output:
            for chunk in _json_chunks(value):
                size += len(chunk)
                if size > max_bytes:
                    raise XbrlExtractionError("Extraction exceeds configured serialized byte limit")
                output.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def read_bundle(path: Path, max_bytes: int) -> XbrlExtractionBundle:
    if path.stat().st_size > max_bytes:
        raise XbrlExtractionError("Extraction exceeds configured serialized byte limit")
    return XbrlExtractionBundle.model_validate_json(path.read_bytes())
