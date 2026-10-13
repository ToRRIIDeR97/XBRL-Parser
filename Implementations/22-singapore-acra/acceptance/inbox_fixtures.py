"""Inbox builders for the 21-file-inbox-india (and 22-singapore-acra) tests."""

from __future__ import annotations

import csv
import io
import zipfile
from hashlib import sha256
from pathlib import Path

COLUMNS = (
    "native_id", "entity_id", "entity_scheme", "company_name", "form", "filed_date",
    "period_end", "fiscal_period", "file", "sha256", "source_url",
)


def write_inbox(root: Path, code: str, rows: list[dict], files: dict[str, bytes]) -> Path:
    inbox = root / code
    inbox.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = inbox / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    with (inbox / "index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    return inbox


def row(native_id: str, file: str, **values) -> dict:
    base = {
        "native_id": native_id,
        "entity_id": "INE002A01018",
        "company_name": "Reliance Industries Limited",
        "form": "results",
        "filed_date": "2024-05-10",
        "period_end": "2024-03-31",
        "fiscal_period": "FY",
        "file": file,
    }
    base.update(values)
    return base


def instance(text: str = "fact") -> bytes:
    return f"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'><!-- {text} --></xbrli:xbrl>".encode()


def zipped(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


class EntityKeys:
    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], int] = {}

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        return self.keys.setdefault((scheme, identifier), 10_000_000_000 + len(self.keys) + 1)
