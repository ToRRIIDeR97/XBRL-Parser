from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow as pa

from sec_xbrl_warehouse.domain.enums import SEC_TABLE_LOAD_ORDER, SecTable
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SchemaDriftError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import sec_typed_schema

SEC_HEADERS: dict[SecTable, tuple[str, ...]] = {
    SecTable.SUB: (
        "adsh",
        "cik",
        "name",
        "sic",
        "countryba",
        "stprba",
        "cityba",
        "zipba",
        "bas1",
        "bas2",
        "baph",
        "countryma",
        "stprma",
        "cityma",
        "zipma",
        "mas1",
        "mas2",
        "countryinc",
        "stprinc",
        "ein",
        "former",
        "changed",
        "afs",
        "wksi",
        "fye",
        "form",
        "period",
        "fy",
        "fp",
        "filed",
        "accepted",
        "prevrpt",
        "detail",
        "instance",
        "nciks",
        "aciks",
        "pubfloatusd",
        "floatdate",
        "floataxis",
        "floatmems",
    ),
    SecTable.TAG: (
        "tag",
        "version",
        "custom",
        "abstract",
        "datatype",
        "iord",
        "crdr",
        "tlabel",
        "doc",
    ),
    SecTable.DIM: ("dimh", "segments", "segt"),
    SecTable.NUM: (
        "adsh",
        "tag",
        "version",
        "ddate",
        "qtrs",
        "uom",
        "dimh",
        "iprx",
        "value",
        "footnote",
        "footlen",
        "dimn",
        "coreg",
        "durp",
        "datp",
        "dcml",
    ),
    SecTable.TXT: (
        "adsh",
        "tag",
        "version",
        "ddate",
        "qtrs",
        "iprx",
        "lang",
        "dcml",
        "durp",
        "datp",
        "dimh",
        "dimn",
        "coreg",
        "escaped",
        "srclen",
        "txtlen",
        "footnote",
        "footlen",
        "context",
        "value",
    ),
    SecTable.REN: (
        "adsh",
        "report",
        "rfile",
        "menucat",
        "shortname",
        "longname",
        "roleuri",
        "parentroleuri",
        "parentreport",
        "ultparentrpt",
    ),
    SecTable.PRE: (
        "adsh",
        "report",
        "line",
        "stmt",
        "inpth",
        "tag",
        "version",
        "prole",
        "plabel",
        "negating",
    ),
    SecTable.CAL: ("adsh", "grp", "arc", "negative", "ptag", "pversion", "ctag", "cversion"),
}


@dataclass(frozen=True, slots=True)
class ZipManifest:
    members: dict[SecTable, str]
    ignored_members: tuple[str, ...]


class SecTsvBatchReader:
    """Bounded-memory reader for tab-delimited SEC bulk ZIP members."""

    def __init__(self, batch_rows: int = 50_000, batch_bytes: int = 32 * 1024**2) -> None:
        if batch_rows <= 0 or batch_bytes <= 0:
            raise ValueError("Batch budgets must be positive")
        self.batch_rows = batch_rows
        self.batch_bytes = batch_bytes

    def validate_manifest(self, archive: zipfile.ZipFile) -> ZipManifest:
        by_stem: dict[str, list[str]] = {}
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            stem = Path(name).stem.lower()
            by_stem.setdefault(stem, []).append(name)

        members: dict[SecTable, str] = {}
        for table in SEC_TABLE_LOAD_ORDER:
            matches = by_stem.get(table.value, [])
            if len(matches) != 1:
                raise ArtifactIntegrityError(
                    f"Expected exactly one {table.value}.txt member, found {matches}"
                )
            members[table] = matches[0]
        ignored = tuple(
            name
            for names in by_stem.values()
            for name in names
            if Path(name).stem.lower() not in {t.value for t in SecTable}
        )
        return ZipManifest(members=members, ignored_members=ignored)

    def validate_header(self, table: SecTable, actual: list[str] | tuple[str, ...] | None) -> None:
        normalized = [column.removeprefix("\ufeff").strip().lower() for column in (actual or [])]
        expected = list(SEC_HEADERS[table])
        if normalized != expected:
            missing = [name for name in expected if name not in normalized]
            unknown = [name for name in normalized if name not in expected]
            raise SchemaDriftError(
                f"{table.value}.txt header changed; missing={missing}, unknown={unknown}, "
                f"expected_order={expected}, actual_order={normalized}"
            )

    def iter_batches(self, zip_path: Path, table: SecTable) -> Iterator[pa.RecordBatch]:
        """Yield raw UTF-8 string batches from one member.

        Each batch carries ``sec_table``/``source_start_line`` metadata. The
        loader passes these to `coerce_sec_batch` for strict typed conversion.
        """
        with zipfile.ZipFile(zip_path) as archive:
            manifest = self.validate_manifest(archive)
            member_name = manifest.members[table]
            with (
                archive.open(member_name, "r") as binary,
                io.TextIOWrapper(binary, encoding="utf-8-sig", errors="strict", newline="") as text,
            ):
                reader = csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
                if reader.fieldnames is None:
                    raise SchemaDriftError(f"Missing header in {member_name}")
                self.validate_header(table, list(reader.fieldnames))
                rows: list[dict[str, str | None]] = []
                batch_size = 0
                start_line = 0
                for source_line, row in enumerate(reader, start=2):
                    if None in row:
                        raise SchemaDriftError(
                            f"Extra fields in {member_name} at source line {source_line}"
                        )
                    normalized = {key: _normalize_empty(value) for key, value in row.items()}
                    size = sum(
                        len(value.encode("utf-8"))
                        for value in normalized.values()
                        if value is not None
                    )
                    if size > self.batch_bytes:
                        raise ArtifactIntegrityError(
                            f"Row exceeds byte budget in {member_name} at line {source_line}"
                        )
                    if rows and batch_size + size > self.batch_bytes:
                        yield self._raw_batch(table, member_name, rows, start_line)
                        rows, batch_size = [], 0
                    if not rows:
                        start_line = source_line
                    rows.append(normalized)
                    batch_size += size
                    if len(rows) >= self.batch_rows:
                        yield self._raw_batch(table, member_name, rows, start_line)
                        rows = []
                        batch_size = 0
                if rows:
                    yield self._raw_batch(table, member_name, rows, start_line)

    @staticmethod
    def _raw_batch(
        table: SecTable,
        member_name: str,
        rows: list[dict[str, str | None]],
        start_line: int,
    ) -> pa.RecordBatch:
        schema = pa.schema(
            [pa.field(name, pa.string(), nullable=True) for name in SEC_HEADERS[table]]
        )
        batch = pa.RecordBatch.from_pylist(rows, schema=schema)
        return batch.replace_schema_metadata(
            {
                "sec_table": table.value,
                "source_member": member_name,
                "source_start_line": str(start_line),
                "source_rows": str(len(rows)),
            }
        )


def coerce_sec_batch(
    table: SecTable,
    rows: list[dict[str, str | None]],
    *,
    start_line: int,
) -> pa.RecordBatch:
    """Convert source strings into a typed Arrow batch matching `sec_typed_schema`.

    - SEC dates parse as ``YYYYMMDD``; `accepted` as ``YYYYMMDDHHMMSS`` (UTC).
    - Flags parse only from 0/1; integers carry range checks.
    - NUM retains the lexical value as `value_raw` and a Decimal256-compatible
      `value_decimal`; parsing never passes through binary floating point.
    - A malformed cell raises `SchemaDriftError` naming table, source line,
      column, and lexical value.
    """
    script = _TableCoercion_Class[table]
    converted = [script(line=start_line + index).convert(row) for index, row in enumerate(rows)]
    batch = pa.RecordBatch.from_pylist(converted, schema=sec_typed_schema(table))
    return batch.replace_schema_metadata(
        {
            "sec_table": table.value,
            "source_start_line": str(start_line),
            "source_rows": str(len(rows)),
        }
    )


class _TableCoercion:
    """Per-table source-row converter producing the typed contract values."""

    def __init__(self, *, line: int) -> None:
        self.line = line

    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        raise NotImplementedError

    def _fail(self, name: str, raw: str | None, exc: Exception) -> None:
        raise SchemaDriftError(
            f"Malformed row at source line {self.line}, column {name}={raw!r}: {exc}"
        ) from exc

    def _string(self, raw: str | None) -> str | None:
        return _normalize_empty(raw)

    def _int(
        self,
        raw: str | None,
        *,
        required: bool = True,
        low: int | None = None,
        high: int | None = None,
    ) -> int | None:
        text = _normalize_empty(raw)
        if text is None:
            if required:
                raise ValueError("required integer column is empty")
            return None
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"integer value {raw!r}") from exc
        if low is not None and value < low:
            raise ValueError(f"integer {value} below minimum {low}")
        if high is not None and value > high:
            raise ValueError(f"integer {value} above maximum {high}")
        return value

    def _flag(self, raw: str | None, *, required: bool = True) -> int | None:
        value = self._int(raw, required=required, low=0, high=1)
        return value

    def _date(self, raw: str | None) -> date | None:
        text = _normalize_empty(raw)
        if text is None:
            return None
        try:
            if len(text) == 8 and text.isdigit():
                return datetime.strptime(text, "%Y%m%d").date()
            return date.fromisoformat(text[:10])
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"date value {raw!r}") from exc

    def _accepted(self, raw: str | None) -> datetime | None:
        text = _normalize_empty(raw)
        if text is None:
            return None
        try:
            if len(text) == 14 and text.isdigit():
                return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"datetime value {raw!r}") from exc

    def _decimal(self, raw: str | None) -> Decimal | None:
        text = _normalize_empty(raw)
        if text is None:
            return None
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"decimal value {raw!r}") from exc

    def _float(self, raw: str | None) -> float | None:
        text = _normalize_empty(raw)
        if text is None:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"float value {raw!r}") from exc


class _SubCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in ("cik",):
                    out[name] = self._int(raw)
                elif name in ("sic", "nciks", "floatmems"):
                    out[name] = self._int(raw, required=False)
                elif name in ("wksi", "prevrpt", "detail"):
                    out[name] = self._flag(raw)
                elif name in ("changed", "period", "filed", "floatdate"):
                    out[name] = self._date(raw)
                elif name == "accepted":
                    out[name] = self._accepted(raw)
                elif name == "pubfloatusd":
                    out[name] = self._decimal(raw)
                elif name == "fy":
                    out[name] = self._int(raw, required=False)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _TagCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in ("custom", "abstract"):
                    out[name] = self._flag(raw)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _DimCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name == "dimh":
                    out[name] = _normalize_empty(raw) or "0" * 32
                elif name == "segt":
                    out[name] = self._flag(raw)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _NumCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name == "value":
                    out["value_raw"] = self._string(raw)
                    out["value_decimal"] = self._decimal(raw)
                elif name in ("adsh", "tag", "version", "uom", "footnote", "coreg"):
                    out[name] = self._string(raw)
                elif name in ("dimh",):
                    out[name] = _normalize_empty(raw) or "0" * 32
                elif name in ("ddate",):
                    out[name] = self._date(raw)
                elif name in ("qtrs", "iprx", "footlen", "dimn", "dcml"):
                    out[name] = self._int(raw)
                elif name in ("durp", "datp"):
                    out[name] = self._float(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        out.setdefault("value_raw", None)
        out.setdefault("value_decimal", None)
        return out


class _TxtCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in (
                    "adsh",
                    "tag",
                    "version",
                    "lang",
                    "dimh",
                    "coreg",
                    "footnote",
                    "context",
                    "value",
                ):
                    out[name] = self._string(raw)
                elif name in ("ddate",):
                    out[name] = self._date(raw)
                elif name in ("qtrs", "iprx", "dcml", "escaped", "srclen", "txtlen", "dimn"):
                    out[name] = (
                        self._int(raw, required=False)
                        if name in ("txtlen", "dimn")
                        else self._int(raw)
                    )
                elif name in ("durp", "datp"):
                    out[name] = self._float(raw)
                elif name == "footlen":
                    out[name] = self._int(raw, required=False)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _RenCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in ("report",):
                    out[name] = self._int(raw)
                elif name in ("parentreport", "ultparentrpt"):
                    out[name] = self._int(raw, required=False)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _PreCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in ("report", "line"):
                    out[name] = self._int(raw)
                elif name in ("inpth", "negating"):
                    out[name] = self._flag(raw)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


class _CalCoercion(_TableCoercion):
    def convert(self, row: dict[str, str | None]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, raw in row.items():
            try:
                if name in ("grp", "arc"):
                    out[name] = self._int(raw)
                elif name == "negative":
                    out[name] = self._flag(raw)
                else:
                    out[name] = self._string(raw)
            except (ValueError, InvalidOperation) as exc:
                self._fail(name, raw, exc)
        return out


_TableCoercion_Class: dict[SecTable, type[_TableCoercion]] = {
    SecTable.SUB: _SubCoercion,
    SecTable.TAG: _TagCoercion,
    SecTable.DIM: _DimCoercion,
    SecTable.NUM: _NumCoercion,
    SecTable.TXT: _TxtCoercion,
    SecTable.REN: _RenCoercion,
    SecTable.PRE: _PreCoercion,
    SecTable.CAL: _CalCoercion,
}


def _normalize_empty(value: str | None) -> str | None:
    if value is None or value == "" or value == "\\N":
        return None
    return value
