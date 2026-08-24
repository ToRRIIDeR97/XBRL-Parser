from datetime import date

import pytest

from sec_xbrl_warehouse.domain.enums import (  # noqa: F401
    Severity,
)
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.ingestion.filing_discovery import (
    SecDailyIndexDiscovery,
    merge_discovery_results,
)

MASTER_SAMPLE = """Description:           Daily Index of EDGAR Dissemination Feed
Last Data Received:    Dec 16, 2024
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/

CIK|Company Name|Form Type|Date Filed|File Name
--------------------------------------------------------------------------------
1000275|ROYAL BANK OF CANADA|424B2|20241216|edgar/data/1000275/0000950103-24-017793.txt
1|Example Corp|10-K|20241216|edgar/data/1/0000000001-24-000001/example-10k.htm
1|Example Corp|10-K/A|20241216|edgar/data/1/0000000001-24-000002/example-amendment.htm
2|Other Inc|10-Q|20241216|edgar/data/2/0000000002-24-000003/other-10q.htm
"""


def test_parse_master_index_records() -> None:
    filings = SecDailyIndexDiscovery._parse_master(
        MASTER_SAMPLE, filed_date=date(2024, 12, 16), forms={"10-K", "10-K/A", "10-Q"}
    )
    accessions = {item.accession for item in filings}
    assert "0001000275-24-000001" not in accessions  # padding is applied by EDGAR path only
    assert "0000000001-24-000001" in accessions
    assert "0000000001-24-000002" in accessions
    assert "0000000002-24-000003" in accessions
    by_accession = {item.accession: item for item in filings}
    assert by_accession["0000000001-24-000001"].company_name == "Example Corp"
    assert by_accession["0000000001-24-000002"].is_amendment is True
    assert by_accession["0000000002-24-000003"].form == "10-Q"


def test_parse_master_filters_forms() -> None:
    filings = SecDailyIndexDiscovery._parse_master(
        MASTER_SAMPLE, filed_date=date(2024, 12, 16), forms={"10-K"}
    )
    assert len(filings) == 1
    assert filings[0].form == "10-K"


def test_parse_master_handles_pipes_in_company_name() -> None:
    sample = MASTER_SAMPLE.replace(
        "1|Example Corp|10-K|20241216",
        "1|Example Corp | Public|10-K|20241216",
    )
    # The company with a pipe: CIK=1, name contains a pipe. Verify we don't crash.
    filings = SecDailyIndexDiscovery._parse_master(
        sample, filed_date=date(2024, 12, 16), forms={"10-K"}
    )
    assert filings
    assert filings[0].accession == "0000000001-24-000001"


def test_accession_extraction_from_path() -> None:
    assert (
        SecDailyIndexDiscovery._parse_master(
            MASTER_SAMPLE, filed_date=date(2024, 12, 16), forms={"424B2"}
        )[0].accession
        == "0000950103-24-017793"
    )


def test_merge_rejects_conflicting_metadata() -> None:
    a = FilingRef(
        accession="0000000001-24-000001",
        cik=1,
        company_name="A",
        form="10-K",
        filed_date=date(2024, 12, 16),
        archive_directory_url="u",
    )
    b = a.model_copy(update={"cik": 999})
    with pytest.raises(ValueError, match="Conflicting"):
        merge_discovery_results([a], [b])


def test_merge_dedupes_by_accession() -> None:
    a = FilingRef(
        accession="0000000001-24-000001",
        cik=1,
        company_name="A",
        form="10-K",
        filed_date=date(2024, 12, 16),
        archive_directory_url="u",
    )
    b = a.model_copy(update={"company_name": "B"})
    merged = merge_discovery_results([a], [b])
    assert len(merged) == 1
    assert merged[0].company_name == "A"
