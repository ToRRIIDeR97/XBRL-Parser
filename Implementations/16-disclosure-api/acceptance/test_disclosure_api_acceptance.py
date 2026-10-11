"""Worker-visible acceptance tests for 16-disclosure-api."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from bundle16 import ACCESSION, CLASS, DEI, EXT, GAAP, GEOGRAPHY, SEGMENTS, bundle, concept, dim, fact
from fastapi.testclient import TestClient

from sec_xbrl_warehouse.api.app import create_app
from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.domain.enums import FactValueKind
from sec_xbrl_warehouse.query.service import QueryService

T = FactValueKind.TEXT
BASE = f"/v1/filings/{ACCESSION}"


class RecordingWarehouse:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters or {}))
        return []


def client_for(warehouse) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    return TestClient(app, raise_server_exceptions=False)


# AC-4: malformed input is rejected with 422 before any warehouse query.
@pytest.mark.parametrize(
    "path",
    [
        "/v1/filings/320193-25-79/cover-page",
        "/v1/filings/320193-25-79/segments",
        "/v1/filings/320193-25-79/text-blocks",
        f"{BASE}/segments?kind=region",
        f"{BASE}/segments?concept=Revenues",
        f"{BASE}/segments?limit=0",
        f"{BASE}/segments?limit=10001",
        f"{BASE}/text-blocks?limit=1001",
        f"{BASE}/text-blocks?include_text=true&limit=101",
    ],
)
def test_ac4_invalid_input_is_rejected_without_a_query(path: str) -> None:
    warehouse = RecordingWarehouse()
    client = client_for(warehouse)
    assert client.get(path).status_code == 422
    assert warehouse.queries == []
    client.close()


# AC-4: user values travel as query parameters, never inside the SQL text.
@pytest.mark.parametrize("suffix", ["cover-page", "segments", "text-blocks"])
def test_ac4_accession_is_a_parameter(suffix: str) -> None:
    warehouse = RecordingWarehouse()
    client = client_for(warehouse)
    client.get(f"{BASE}/{suffix}")
    assert warehouse.queries, "the endpoint must query the warehouse"
    for sql, parameters in warehouse.queries:
        assert ACCESSION not in sql
        assert ACCESSION in parameters.values()
    client.close()


# AC-1: a filing without a cover page row is 404; AC-2/AC-3: empty lists are 200.
def test_ac1_missing_cover_page_is_404_and_empty_lists_are_200() -> None:
    client = client_for(RecordingWarehouse())
    assert client.get(f"{BASE}/cover-page").status_code == 404
    segments = client.get(f"{BASE}/segments")
    assert segments.status_code == 200
    assert segments.json()["items"] == [] and segments.json()["count"] == 0
    texts = client.get(f"{BASE}/text-blocks")
    assert texts.status_code == 200 and texts.json()["items"] == []
    client.close()


@pytest.fixture(scope="module")
def loaded_client(clickhouse_settings):
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    data = bundle(
        [
            fact(DEI, "DocumentType", "10-K", kind=T),
            fact(DEI, "EntityRegistrantName", "Apple Inc.", kind=T),
            fact(DEI, "TradingSymbol", "AAPL", kind=T),
            fact(DEI, "EntityCommonStockSharesOutstanding", 15_000_000_000, instant=date(2025, 10, 17),
                 unit="xbrli:shares"),
            fact(DEI, "Security12bTitle", "Common Stock", kind=T,
                 dims=(dim(*CLASS, GAAP, "CommonStockMember"),)),
            fact(GAAP, "Revenues", 178_000, dims=(dim(*SEGMENTS, EXT, "AmericasSegmentMember"),)),
            fact(GAAP, "Revenues", 111_000, dims=(dim(*SEGMENTS, EXT, "EuropeSegmentMember"),)),
            fact(GAAP, "Revenues", 64_000, dims=(dim(*GEOGRAPHY, EXT, "ChinaMember"),)),
            fact(GAAP, "RevenueRecognitionPolicyTextBlock", "<p>Revenue is recognized when...</p>", kind=T),
            fact(GAAP, "SegmentReportingDisclosureTextBlock", "<p>Five segments.</p>", kind=T),
        ],
        concepts=[
            concept(EXT, "AmericasSegmentMember", "Americas"),
            concept(EXT, "EuropeSegmentMember", "Europe"),
        ],
    )
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    warehouse.apply_ddl(Path("sql/clickhouse"))
    warehouse.publish_xbrl_bundle(data, warehouse.insert_xbrl_bundle(data))
    client = client_for(warehouse)
    try:
        yield client
    finally:
        client.close()
        warehouse.close()


# AC-1: the cover page comes back as one object with its securities.
@pytest.mark.integration
def test_ac1_cover_page_returns_the_filing_cover(loaded_client) -> None:
    response = loaded_client.get(f"{BASE}/cover-page")
    assert response.status_code == 200
    body = response.json()
    assert body["accession"] == ACCESSION
    assert body["document_type"] == "10-K"
    assert body["registrant_name"] == "Apple Inc."
    assert body["trading_symbol"] == "AAPL"
    assert Decimal(str(body["shares_outstanding"])) == Decimal(15_000_000_000)
    assert body["shares_outstanding_date"] == "2025-10-17"
    assert body["securities"] == [
        {"member": "us-gaap:CommonStockMember", "title": "Common Stock", "symbol": None, "exchange": None}
    ]
    assert "_row_hash" not in body and "_ingested_at" not in body


# AC-2: segment rows carry kind, label and members; `kind` filters.
@pytest.mark.integration
def test_ac2_segments_shape_and_kind_filter(loaded_client) -> None:
    body = loaded_client.get(f"{BASE}/segments").json()
    assert body["count"] == 3
    labels = [(item["kind"], item["member_label"], Decimal(str(item["value"]))) for item in body["items"]]
    assert sorted(labels) == [
        ("geography", "China", Decimal(64_000)),
        ("segment", "Americas", Decimal(178_000)),
        ("segment", "Europe", Decimal(111_000)),
    ]
    americas = next(item for item in body["items"] if item["member_label"] == "Americas")
    assert americas["concept_qname"] == "us-gaap:Revenues"
    assert americas["members"] == [{"kind": "segment", "qname": "ex:AmericasSegmentMember", "label": "Americas"}]
    assert americas["is_elimination"] is False
    assert americas["period_end"] == "2025-09-27"

    only_segments = loaded_client.get(f"{BASE}/segments?kind=segment").json()
    assert {item["member_label"] for item in only_segments["items"]} == {"Americas", "Europe"}


# AC-3: text blocks list policies separately; text is returned only on request.
@pytest.mark.integration
def test_ac3_text_blocks_policy_filter_and_text_opt_in(loaded_client) -> None:
    body = loaded_client.get(f"{BASE}/text-blocks").json()
    assert [(item["concept_qname"], item["is_policy"]) for item in body["items"]] == [
        ("us-gaap:RevenueRecognitionPolicyTextBlock", True),
        ("us-gaap:SegmentReportingDisclosureTextBlock", False),
    ]
    assert all("text" not in item for item in body["items"])
    assert body["items"][1]["text_length"] == len("<p>Five segments.</p>")

    policies = loaded_client.get(f"{BASE}/text-blocks?policy=true&include_text=true").json()
    assert [item["text"] for item in policies["items"]] == ["<p>Revenue is recognized when...</p>"]
