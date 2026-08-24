from pathlib import Path

from sec_xbrl_warehouse.domain.enums import DatasetKind
from sec_xbrl_warehouse.ingestion.bulk_catalog import (
    derive_source_period,
    parse_catalog_html,
    parse_display_size,
)


def test_parse_catalog_snapshot() -> None:
    path = Path(__file__).parents[1] / "fixtures/catalog_notes_sample.html"
    artifacts = parse_catalog_html(
        path.read_text(encoding="utf-8"),
        page_url="https://www.sec.gov/data-research/example",
        dataset=DatasetKind.NOTES,
    )
    assert [item.period for item in artifacts] == ["2025Q4", "2026-01"]
    assert artifacts[0].expected_size_bytes == 125 * 1024**2


def test_period_derivation() -> None:
    assert derive_source_period("2024q3_notes.zip") == "2024Q3"
    assert derive_source_period("2026_07_notes.zip") == "2026-07"


def test_display_size() -> None:
    assert parse_display_size("Download (1.5 GB)") == int(1.5 * 1024**3)
    assert parse_display_size("unknown") is None
