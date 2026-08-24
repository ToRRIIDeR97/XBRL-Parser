from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.exceptions import SchemaDriftError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    sec_source_schema,
    sec_typed_schema,
)
from sec_xbrl_warehouse.ingestion.sec_tsv import (
    SecTsvBatchReader,
    coerce_sec_batch,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/fsnds-mini.zip"


def test_fixture_manifest_reads_all_tables() -> None:
    reader = SecTsvBatchReader(batch_rows=1)
    for table in SecTable:
        batches = list(reader.iter_batches(FIXTURE, table))
        assert len(batches) == 1, table
        assert batches[0].num_rows == 1, table
        assert batches[0].schema.names == list(sec_source_schema(table).names)


def test_fixture_manifest_and_batch() -> None:
    batches = list(SecTsvBatchReader(batch_rows=1).iter_batches(FIXTURE, SecTable.NUM))
    assert len(batches) == 1
    assert batches[0].num_rows == 1
    assert batches[0].column("value")[0].as_py() == "123456789"


def test_batch_metadata_carries_source_line() -> None:
    batch = next(SecTsvBatchReader(batch_rows=1).iter_batches(FIXTURE, SecTable.NUM))
    metadata = batch.schema.metadata or {}
    assert metadata.get(b"source_start_line") == b"2"
    assert metadata.get(b"sec_table") == b"num"


def test_coerce_num_preserves_raw_and_decimal() -> None:
    row = {
        "adsh": "0000000001-26-000001",
        "tag": "Revenues",
        "version": "us-gaap/2025",
        "ddate": "20251231",
        "qtrs": "4",
        "uom": "USD",
        "dimh": "0" * 32,
        "iprx": "0",
        "value": "123456789",
        "footnote": "",
        "footlen": "0",
        "dimn": "0",
        "coreg": "",
        "durp": "1.0",
        "datp": "1.0",
        "dcml": "-3",
    }
    typed = coerce_sec_batch(SecTable.NUM, [row], start_line=2)
    assert typed.column("value_raw")[0].as_py() == "123456789"
    assert typed.column("value_decimal")[0].as_py() == Decimal("123456789")
    assert typed.schema.names == list(sec_typed_schema(SecTable.NUM).names)


def test_coerce_sub_parses_dates_and_flags() -> None:
    row = {
        "adsh": "0000000001-26-000001",
        "cik": "1",
        "name": "Example Corp",
        "sic": "3571",
        "countryba": "US",
        "cityba": "San Francisco",
        "countryinc": "US",
        "wksi": "0",
        "fye": "1231",
        "form": "10-K",
        "period": "20251231",
        "fy": "2025",
        "fp": "FY",
        "filed": "20260215",
        "accepted": "20260215120000",
        "prevrpt": "0",
        "detail": "1",
        "instance": "example.htm",
        "nciks": "1",
    }
    typed = coerce_sec_batch(SecTable.SUB, [row], start_line=2)
    assert typed.column("period")[0].as_py().isoformat() == "2025-12-31"
    assert typed.column("filed")[0].as_py().isoformat() == "2026-02-15"
    accepted = typed.column("accepted")[0].as_py()
    assert accepted.isoformat().startswith("2026-02-15T12:00:00")
    assert typed.column("cik")[0].as_py() == 1
    assert typed.column("wksi")[0].as_py() == 0
    assert typed.column("detail")[0].as_py() == 1


def test_coerce_malformed_cell_names_line_and_column() -> None:
    row = {
        "adsh": "0000000001-26-000001",
        "tag": "Revenues",
        "version": "us-gaap/2025",
        "ddate": "not-a-date",
        "qtrs": "4",
        "uom": "USD",
        "dimh": "0" * 32,
        "iprx": "0",
        "value": "123456789",
        "footnote": "",
        "footlen": "0",
        "dimn": "0",
        "coreg": "",
        "durp": "1.0",
        "datp": "1.0",
        "dcml": "-3",
    }
    with pytest.raises(SchemaDriftError, match=r"source line 17.*column ddate"):
        coerce_sec_batch(SecTable.NUM, [row], start_line=17)


def test_coerce_uses_decimal_without_float_loss() -> None:
    row = {
        "adsh": "0000000001-26-000001",
        "tag": "Revenues",
        "version": "us-gaap/2025",
        "ddate": "20251231",
        "qtrs": "4",
        "uom": "USD",
        "dimh": "0" * 32,
        "iprx": "0",
        "value": "0.1",
        "footnote": "",
        "footlen": "0",
        "dimn": "0",
        "coreg": "",
        "durp": "1.0",
        "datp": "1.0",
        "dcml": "1",
    }
    typed = coerce_sec_batch(SecTable.NUM, [row], start_line=2)
    # decimal256(76,18) rescales 0.1 to an 18-digit fraction; compare numerically.
    assert typed.column("value_decimal")[0].as_py() == Decimal("0.1")


def test_coerce_null_markers_become_none() -> None:
    row = {
        "adsh": "0000000001-26-000001",
        "tag": "Revenues",
        "version": "us-gaap/2025",
        "ddate": "20251231",
        "qtrs": "4",
        "uom": "USD",
        "dimh": "0" * 32,
        "iprx": "0",
        "value": "\\N",
        "footnote": "\\N",
        "footlen": "0",
        "dimn": "0",
        "coreg": "\\N",
        "durp": "1.0",
        "datp": "1.0",
        "dcml": "-3",
    }
    typed = coerce_sec_batch(SecTable.NUM, [row], start_line=2)
    assert typed.column("value_raw")[0].as_py() is None
    assert typed.column("value_decimal")[0].as_py() is None
    assert typed.column("footnote")[0].as_py() is None
