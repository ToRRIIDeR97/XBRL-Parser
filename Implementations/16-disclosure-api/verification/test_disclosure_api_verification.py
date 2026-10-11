"""Independent verification tests for 16-disclosure-api."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from bundle16v import ACCESSION, DEI, EXT, GAAP, PRODUCT, SEGMENTS, SRT, bundle, concept, dim, fact
from fastapi.testclient import TestClient

from sec_xbrl_warehouse.api.app import create_app
from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind
from sec_xbrl_warehouse.query.service import QueryService

T = FactValueKind.TEXT
BASE = f"/v1/filings/{ACCESSION}"
OTHER = "0000789019-25-000100"


class RecordingWarehouse:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters or {}))
        return []


def client_for(warehouse, settings: Settings | None = None) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    return TestClient(app, raise_server_exceptions=False)


# AC-4: injection-shaped and out-of-range values never reach the warehouse.
@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/segments?concept=us-gaap:Revenues'%20OR%201=1",
        f"{BASE}/text-blocks?concept=us-gaap:X;DROP",
        f"{BASE}/segments?kind=segment&kind=region",
        f"{BASE}/segments?offset=10001",
        f"{BASE}/text-blocks?offset=-1",
        f"{BASE}/text-blocks?policy=maybe",
        f"{BASE}/segments?include_eliminations=sometimes",
        "/v1/filings/0000320193-25-0000790/text-blocks",
    ],
)
def test_ac4_hostile_and_out_of_range_inputs_are_rejected(path: str) -> None:
    warehouse = RecordingWarehouse()
    client = client_for(warehouse)
    assert client.get(path).status_code == 422
    assert warehouse.queries == []
    client.close()


# AC-4: the text-only cap applies at exactly 100 rows.
def test_ac4_include_text_cap_boundary() -> None:
    warehouse = RecordingWarehouse()
    client = client_for(warehouse)
    assert client.get(f"{BASE}/text-blocks?include_text=true&limit=100").status_code == 200
    assert client.get(f"{BASE}/text-blocks?include_text=false&limit=1000").status_code == 200
    client.close()


# AC-5: the new routes sit behind the API key like every other data route.
async def test_ac5_routes_require_the_api_key() -> None:
    key = "k" * 32
    app = create_app(Settings(_env_file=None, env="test", api_key=key))
    app.dependency_overrides[get_query_service] = lambda: QueryService(RecordingWarehouse())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for suffix in ("cover-page", "segments", "text-blocks"):
            assert (await client.get(f"{BASE}/{suffix}")).status_code == 401
            ok = await client.get(f"{BASE}/{suffix}", headers={"Authorization": f"Bearer {key}"})
            assert ok.status_code == (404 if suffix == "cover-page" else 200)


def _load(warehouse, *bundles) -> None:
    for data in bundles:
        warehouse.publish_xbrl_bundle(data, warehouse.insert_xbrl_bundle(data))


_SEQUENCE = iter(range(500_000, 600_000))


def fresh_accession() -> str:
    """The ClickHouse fixture is session-scoped, so each test loads its own filing."""
    return f"0000320193-25-{next(_SEQUENCE):06d}"


@pytest.fixture
def warehouse(clickhouse_settings):
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    store = ClickHouseWarehouse(clickhouse_settings)
    store.apply_ddl(Path("sql/clickhouse"))
    try:
        yield store
    finally:
        store.close()


# AC-6: a re-extraction replaces what every endpoint returns; nothing from revision 1 leaks.
@pytest.mark.integration
def test_ac6_endpoints_follow_the_latest_revision(warehouse) -> None:
    accession = fresh_accession()
    base = f"/v1/filings/{accession}"
    labels = [concept(EXT, "CloudSegmentMember", "Cloud")]
    first = bundle(
        [
            fact(DEI, "DocumentType", "10-K", kind=T),
            fact(DEI, "EntityRegistrantName", "Old Name", kind=T),
            fact(GAAP, "Revenues", 100, dims=(dim(*SEGMENTS, EXT, "CloudSegmentMember"),)),
            fact(GAAP, "LeasesPolicyTextBlock", "<p>old</p>", kind=T),
        ],
        labels,
        accession=accession,
        revision=1,
    )
    second = bundle(
        [
            fact(DEI, "DocumentType", "10-K", kind=T),
            fact(DEI, "EntityRegistrantName", "New Name", kind=T),
            fact(GAAP, "Revenues", 250, dims=(dim(*SEGMENTS, EXT, "CloudSegmentMember"),)),
            fact(GAAP, "LeasesPolicyTextBlock", "<p>new text</p>", kind=T),
        ],
        labels,
        accession=accession,
        revision=2,
    )
    _load(warehouse, first, second)
    client = client_for(warehouse)
    try:
        cover = client.get(f"{base}/cover-page").json()
        assert cover["registrant_name"] == "New Name"
        assert cover["extraction_revision"] == 2
        segments = client.get(f"{base}/segments").json()["items"]
        assert [Decimal(str(item["value"])) for item in segments] == [Decimal(250)]
        texts = client.get(f"{base}/text-blocks?include_text=true").json()["items"]
        assert [item["text"] for item in texts] == ["<p>new text</p>"]
    finally:
        client.close()


# AC-1/AC-2/AC-3: rows of one filing never appear under another; unknown filing is empty.
@pytest.mark.integration
def test_ac2_filings_are_isolated_and_unknown_filings_are_empty(warehouse) -> None:
    accession = fresh_accession()
    base = f"/v1/filings/{accession}"
    _load(
        warehouse,
        bundle([fact(GAAP, "Revenues", 7, dims=(dim(*PRODUCT, EXT, "ServicesMember"),))], accession=OTHER),
        bundle([fact(DEI, "DocumentType", "10-Q", kind=T)], accession=accession),
    )
    client = client_for(warehouse)
    try:
        assert client.get(f"{base}/segments").json()["items"] == []
        other = client.get(f"/v1/filings/{OTHER}/segments").json()
        assert [item["member_label"] for item in other["items"]] == ["Services"]
        assert client.get(f"/v1/filings/{OTHER}/cover-page").status_code in {200, 404}
        assert client.get("/v1/filings/0000000001-26-000001/cover-page").status_code == 404
        assert client.get("/v1/filings/0000000001-26-000001/text-blocks").json()["count"] == 0
    finally:
        client.close()


# AC-2: multi-axis members keep segment-product order; eliminations and concept filter.
@pytest.mark.integration
def test_ac2_members_eliminations_and_concept_filter(warehouse) -> None:
    accession = fresh_accession()
    base = f"/v1/filings/{accession}"
    _load(
        warehouse,
        bundle(
            [
                fact(GAAP, "Revenues", 120, dims=(
                    dim(*PRODUCT, EXT, "ServicesMember"), dim(*SEGMENTS, EXT, "CloudSegmentMember"))),
                fact(GAAP, "Revenues", -15, dims=(
                    dim(SRT, "ConsolidationItemsAxis", SRT, "IntersegmentEliminationMember"),)),
                fact(GAAP, "OperatingIncomeLoss", 40, dims=(dim(*SEGMENTS, EXT, "CloudSegmentMember"),)),
            ],
            [concept(EXT, "CloudSegmentMember", "Cloud"), concept(EXT, "ServicesMember", "Services")],
            accession=accession,
        ),
    )
    client = client_for(warehouse)
    try:
        everything = client.get(f"{base}/segments").json()["items"]
        combined = next(item for item in everything if Decimal(str(item["value"])) == 120)
        assert combined["kind"] == "segment" and combined["member_label"] == "Cloud"
        assert [(m["kind"], m["label"]) for m in combined["members"]] == [("segment", "Cloud"), ("product", "Services")]
        elimination = next(item for item in everything if Decimal(str(item["value"])) == -15)
        assert elimination["is_elimination"] is True and elimination["kind"] == "consolidation"

        kept = client.get(f"{base}/segments?include_eliminations=false").json()["items"]
        assert all(not item["is_elimination"] for item in kept) and len(kept) == 2

        profit = client.get(f"{base}/segments?concept=us-gaap:OperatingIncomeLoss").json()["items"]
        assert [Decimal(str(item["value"])) for item in profit] == [Decimal(40)]

        both = client.get(f"{base}/segments?kind=product&kind=consolidation").json()["items"]
        assert sorted(item["kind"] for item in both) == ["consolidation", "segment"]
    finally:
        client.close()


# AC-2/AC-3: limit and offset page through a stable order without overlap.
@pytest.mark.integration
def test_ac2_pagination_is_stable(warehouse) -> None:
    facts = [
        fact(GAAP, "Revenues", value, dims=(dim(*SEGMENTS, EXT, f"Seg{value}Member"),))
        for value in range(1, 8)
    ] + [fact(GAAP, f"Topic{n}TextBlock", f"<p>{n}</p>", kind=T) for n in range(5)]
    accession = fresh_accession()
    base = f"/v1/filings/{accession}"
    _load(warehouse, bundle(facts, accession=accession))
    client = client_for(warehouse)
    try:
        pages = [client.get(f"{base}/segments?limit=3&offset={offset}").json() for offset in (0, 3, 6)]
        values = [Decimal(str(item["value"])) for page in pages for item in page["items"]]
        assert sorted(values) == [Decimal(n) for n in range(1, 8)] and len(set(values)) == 7
        assert [page["count"] for page in pages] == [3, 3, 1]
        assert pages[1]["offset"] == 3 and pages[1]["limit"] == 3

        first = client.get(f"{base}/text-blocks?limit=2").json()["items"]
        rest = client.get(f"{base}/text-blocks?limit=10&offset=2").json()["items"]
        names = [item["concept_qname"] for item in first + rest]
        assert names == sorted(names) and len(set(names)) == 5

        non_policy = client.get(f"{base}/text-blocks?policy=false&concept=us-gaap:Topic3TextBlock").json()
        assert [item["concept_qname"] for item in non_policy["items"]] == ["us-gaap:Topic3TextBlock"]
        assert all(item["is_policy"] is False for item in non_policy["items"])
    finally:
        client.close()
