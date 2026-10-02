import csv
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, SchemaDriftError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    sec_source_schema,
    sec_typed_schema,
    sec_warehouse_schema,
)
from sec_xbrl_warehouse.ingestion.sec_tsv import (
    SEC_CSV_FIELD_LIMIT,
    SecTsvBatchReader,
    coerce_sec_batch,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/fsnds-mini.zip"


def _text_archive(tmp_path: Path, value: str, *, header: bool = False) -> Path:
    archive = tmp_path / "large-text.zip"
    with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(archive, "w") as target:
        for member in source.namelist():
            data = source.read(member)
            if member == "txt.txt":
                lines = data.decode().splitlines()
                fields = lines[0 if header else 1].split("\t")
                fields[-1] = value
                # Retain the original row to exercise failure after a yielded batch.
                data = (
                    "\n".join(["\t".join(fields)] if header else [*lines, "\t".join(fields)]) + "\n"
                ).encode()
            target.writestr(member, data)
    return archive


def test_large_text_field_loads_and_replays(tmp_path: Path) -> None:
    value = "é" * 131_073
    archive = _text_archive(tmp_path, value)
    reader = SecTsvBatchReader(batch_rows=1)
    batches = list(reader.iter_batches(archive, SecTable.TXT))
    assert len(batches) == 2
    assert batches[1].column("value")[0].as_py() == value
    typed = coerce_sec_batch(SecTable.TXT, batches[1].to_pylist(), start_line=3)
    assert typed.column("value")[0].as_py() == value
    replay = list(reader.iter_batches(archive, SecTable.TXT))
    assert all(
        left.equals(right, check_metadata=True) for left, right in zip(batches, replay, strict=True)
    )


@pytest.mark.parametrize("header, source_line", [(False, 3), (True, 1)])
def test_csv_field_ceiling_names_member_and_line(
    tmp_path: Path, header: bool, source_line: int
) -> None:
    archive = _text_archive(tmp_path, "x" * (SEC_CSV_FIELD_LIMIT + 1), header=header)
    with pytest.raises(
        ArtifactIntegrityError, match=rf"txt.txt near source line {source_line}"
    ) as error:
        list(
            SecTsvBatchReader(batch_rows=1, batch_bytes=2 * SEC_CSV_FIELD_LIMIT).iter_batches(
                archive, SecTable.TXT
            )
        )
    assert isinstance(error.value.__cause__, csv.Error)
    assert f"field limit {SEC_CSV_FIELD_LIMIT} characters" in str(error.value)


def test_large_text_still_obeys_utf8_row_budget(tmp_path: Path) -> None:
    archive = _text_archive(tmp_path, "é" * 131_073)
    with pytest.raises(
        ArtifactIntegrityError, match=r"Row exceeds byte budget in txt.txt at line 3"
    ):
        list(SecTsvBatchReader(batch_bytes=200_000).iter_batches(archive, SecTable.TXT))


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


def test_dimhash_header_is_normalized(tmp_path: Path) -> None:
    archive = tmp_path / "dimhash.zip"
    with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(archive, "w") as target:
        for member in source.namelist():
            data = source.read(member)
            if member == "dim.txt":
                data = data.replace(b"dimh\t", b"dimhash\t", 1)
            target.writestr(member, data)

    batch = next(SecTsvBatchReader().iter_batches(archive, SecTable.DIM))
    assert batch.schema.names == ["dimh", "segments", "segt"]


def test_sec_optional_fields_are_nullable() -> None:
    expected = {
        SecTable.SUB: {"countryba", "cityba", "countryinc", "fye", "period", "fy", "fp"},
        SecTable.TAG: {"iord"},
        SecTable.DIM: {"segments"},
        SecTable.TXT: {"value"},
        SecTable.REN: {"shortname"},
        SecTable.PRE: {"stmt", "plabel"},
    }
    for table, names in expected.items():
        schema = sec_typed_schema(table)
        assert all(schema.field(name).nullable for name in names)
    assert sec_warehouse_schema(SecTable.NUM).field("fp").nullable
    assert sec_warehouse_schema(SecTable.TXT).field("fp").nullable


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


def test_cal_signed_values_survive_parquet_and_replay(tmp_path):
    import pyarrow.parquet as pq

    from sec_xbrl_warehouse.domain.sec_fsnds import CalRecord

    row = dict(
        adsh="0000002969-26-000036",
        grp="4",
        arc="14",
        negative="-1",
        ptag="Parent",
        pversion="us-gaap/2026",
        ctag="Child",
        cversion="us-gaap/2026",
    )
    rows = [{**row, "negative": str(value)} for value in (-1, 0, 1)]
    batch = coerce_sec_batch(SecTable.CAL, rows, start_line=5)
    assert batch.column("negative").to_pylist() == [-1, 0, 1]
    assert [CalRecord.model_validate(r).negative for r in batch.to_pylist()] == [-1, 0, 1]
    path = tmp_path / "cal.parquet"
    pq.write_table(pa.Table.from_batches([batch]), path)
    replay = coerce_sec_batch(SecTable.CAL, rows, start_line=5)
    assert pq.read_table(path).equals(pa.Table.from_batches([replay]))
    for invalid in ("-2", "2", "1.5", "", None):
        with pytest.raises(SchemaDriftError, match="column negative"):
            coerce_sec_batch(SecTable.CAL, [{**row, "negative": invalid}], start_line=5)


def test_quoted_tsv_is_bounded_and_preserves_physical_source_lines(tmp_path):
    import io

    from sec_xbrl_warehouse.ingestion.sec_tsv import bounded_tsv_lines

    value = 'LegalEntity=EBP001;InvestmentIdentifier=Security\t\t | "Citibank";\ncontinued'
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(["dimh", "segments", "segt"])
    writer.writerow(["0x123", value, "0"])
    writer.writerow(["0x456", "plain", "0"])
    payload = stream.getvalue().encode()
    archive = tmp_path / "quoted.zip"
    with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(archive, "w") as target:
        for member in source.namelist():
            target.writestr(member, payload if member == "dim.txt" else source.read(member))
    reader = SecTsvBatchReader(batch_rows=1)
    first, second = list(reader.iter_batches(archive, SecTable.DIM))
    assert first.column("segments").to_pylist() == [value]
    assert first.schema.metadata[b"source_start_line"] == b"2"
    assert second.schema.metadata[b"source_start_line"] == b"4"
    replay = list(reader.iter_batches(archive, SecTable.DIM))
    assert all(
        a.equals(b, check_metadata=True) for a, b in zip((first, second), replay, strict=True)
    )
    with pytest.raises(SchemaDriftError, match="Extra fields"):
        list(bounded_tsv_lines(io.BytesIO(b'a\t"quoted\tvalue"\t0\textra\n'), 100, "dim.tsv", 3))
    with pytest.raises(ArtifactIntegrityError, match="byte budget"):
        list(bounded_tsv_lines(io.BytesIO(b'a\t"' + b"x\n" * 40 + b'"\t0\n'), 32, "dim.tsv", 3))
    with pytest.raises(csv.Error):
        list(
            csv.reader(
                bounded_tsv_lines(io.BytesIO(b'a\t"unterminated\n'), 100, "dim.tsv", 3),
                delimiter="\t",
                strict=True,
            )
        )
