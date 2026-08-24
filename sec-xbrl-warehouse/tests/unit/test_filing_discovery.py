import json
from datetime import date
from pathlib import Path

from sec_xbrl_warehouse.ingestion.filing_discovery import parse_submissions_recent


def test_parse_recent_submissions() -> None:
    path = Path(__file__).parents[1] / "fixtures/submissions_sample.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    filings = parse_submissions_recent(
        payload,
        cik=1,
        company_name="Example Corp",
        forms={"10-K"},
        filed_from=date(2025, 1, 1),
        filed_to=None,
    )
    assert len(filings) == 1
    assert filings[0].accession == "0000000001-26-000001"
    assert filings[0].primary_document_url and filings[0].primary_document_url.endswith(
        "example.htm"
    )
