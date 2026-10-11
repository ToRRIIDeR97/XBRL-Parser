from pathlib import Path

from fastapi.testclient import TestClient

from sec_xbrl_warehouse.api.app import create_app
from sec_xbrl_warehouse.api.dependencies import get_query_service
from sec_xbrl_warehouse.query.service import QueryService


class Warehouse:
    def __init__(self):
        self.queries = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        return []


def test_queries_validate_input_and_use_latest_views():
    warehouse = Warehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health/live").status_code == 200
    metrics = client.get("/metrics/")
    assert metrics.status_code == 200
    assert "xbrl_job_transitions_total" in metrics.text
    for path in [
        "/v1/facts",
        "/v1/facts?cik=-1",
        "/v1/facts?cik=1&limit=10001",
        "/v1/facts?cik=1&filed_from=2026-02-01&filed_to=2026-01-01",
    ]:
        assert client.get(path).status_code == 422
    assert not warehouse.queries
    response = client.get("/v1/facts?cik=1&offset=2&limit=5")
    assert response.status_code == 200
    assert response.json()["offset"] == 2
    assert "v_xbrl_facts_latest" in warehouse.queries[-1][0]
    response = client.get("/v1/facts/canonical/1/revenue?limit=3&offset=1")
    assert response.status_code == 200
    assert "v_canonical_facts_latest" in warehouse.queries[-1][0]
    assert warehouse.queries[-1][1]["offset"] == 1
    assert client.get("/v1/filings/0000000001-26-000001").status_code == 404
    client.close()


def test_derived_metric_query_keeps_formula_and_mapping_lineage():
    warehouse = Warehouse()
    service = QueryService(warehouse, Path("config/canonical_metrics.yml"))
    service.canonical_series(cik=1, metric="free_cash_flow", limit=10)
    sql, parameters = warehouse.queries[-1]
    assert "maxIf" in sql
    assert "raw_fact_ids" in sql
    assert "mapping_lineage" in sql
    assert "source.dimensions_json = '[]'" in sql
    assert parameters["left"] == "operating_cash_flow"
    assert parameters["right"] == "capital_expenditures"
    assert parameters["formula"] == "operating_cash_flow - capital_expenditures"


def test_broad_and_deep_queries_are_rejected():
    warehouse = Warehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    for query in (
        "filed_to=2100-01-01",
        "concept_name=Revenue",
        "cik=1&offset=10001",
        "filed_from=2000-01-01&filed_to=2026-01-01",
    ):
        assert client.get("/v1/facts?" + query).status_code == 422
    assert client.get("/v1/facts/canonical/1/revenue?offset=10001").status_code == 422
    assert not warehouse.queries
    assert client.get("/v1/facts?filed_from=2026-01-01&filed_to=2026-03-31").status_code == 200
    client.close()


async def test_api_authentication_and_query_capacity():
    import asyncio
    import threading

    import httpx

    from sec_xbrl_warehouse.config import Settings

    entered, release = threading.Event(), threading.Event()

    class SlowWarehouse:
        def query(self, *args):
            entered.set()
            assert release.wait(3)
            return []

    key = "a" * 32
    app = create_app(Settings(_env_file=None, env="test", api_key=key, api_max_concurrency=1))
    app.dependency_overrides[get_query_service] = lambda: QueryService(SlowWarehouse())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/metrics/")).status_code == 401
        headers = {"Authorization": "Bearer " + key}
        request = asyncio.create_task(client.get("/v1/facts?cik=1", headers=headers))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/v1/facts?cik=2", headers=headers)).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
        finally:
            release.set()
        assert (await request).status_code == 200


def test_production_api_requires_credentials_before_connecting():
    import pytest

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import ConfigurationError

    app = create_app(Settings(_env_file=None, env="production"))
    with pytest.raises(ConfigurationError, match="API key and separate reader"), TestClient(app):
        pass

    for password in ("", "   "):
        app = create_app(
            Settings(
                _env_file=None,
                env="production",
                api_key="a" * 32,
                api_clickhouse_username="reader",
                api_clickhouse_password=password,
            )
        )
        with (
            pytest.raises(ConfigurationError, match="API key and separate reader"),
            TestClient(app),
        ):
            pass


async def test_readiness_probes_are_bounded_without_blocking_liveness_or_queries():
    import asyncio
    import threading

    import httpx

    from sec_xbrl_warehouse.api.dependencies import get_control, get_warehouse
    from sec_xbrl_warehouse.config import Settings

    entered, release = threading.Event(), threading.Event()

    class SlowWarehouse:
        calls = 0

        def ping(self):
            self.calls += 1
            entered.set()
            assert release.wait(3)
            return True

    class Control:
        async def ping(self):
            return True

    warehouse = SlowWarehouse()
    app = create_app(Settings(_env_file=None, env="test", api_key="a" * 32))
    app.dependency_overrides[get_warehouse] = lambda: warehouse
    app.dependency_overrides[get_control] = Control
    app.dependency_overrides[get_query_service] = lambda: QueryService(Warehouse())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/health/ready"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/health/ready")).status_code == 503
            assert warehouse.calls == 1
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/v1/facts?cik=1")).status_code == 401
            assert (
                await client.get("/v1/facts?cik=1", headers={"Authorization": "Bearer " + "a" * 32})
            ).status_code == 200
        finally:
            release.set()
        assert (await request).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200


def test_health_exemption_uses_routing_path_not_host_header():
    from sec_xbrl_warehouse.config import Settings

    app = create_app(Settings(_env_file=None, env="test", api_key="a" * 32))
    client = TestClient(app, raise_server_exceptions=False)
    try:
        for host in ("test/health/live#", "test/health/ready?", "test"):
            assert client.get("/metrics/", headers={"host": host}).status_code == 401
    finally:
        client.close()


def test_filing_summary_validates_accession_and_uses_known_bounds():
    from datetime import date

    import pytest

    warehouse = Warehouse()
    service = QueryService(warehouse)
    with pytest.raises(ValueError):
        service.filing_summary("bad")
    service.filing_summary("0000000001-26-000001", cik=1, filed_date=date(2026, 1, 2))
    sql, parameters = warehouse.queries[-1]
    assert "AND cik = %(cik)s" in sql
    assert parameters["filed_date"] == date(2026, 1, 2)


def test_series_routes_validate_and_resolve_tickers():
    class SeriesWarehouse(Warehouse):
        def query(self, sql, parameters=None):
            self.queries.append((sql, parameters))
            if "FROM companies" in sql:
                return [{"cik": 320193}] if parameters["ticker"] == "AAPL" else []
            return [{"metric": "revenue", "period_type": "annual", "value": 1}]

    warehouse = SeriesWarehouse()
    app = create_app()
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    for path in [
        "/v1/series/320193",
        "/v1/series/320193?metrics=Revenue",
        "/v1/series/320193?metrics=revenue&view=restated",
        "/v1/series/320193?metrics=revenue&period_type=weekly",
        "/v1/series?ticker=AA%20PL&metrics=revenue",
    ]:
        assert client.get(path).status_code == 422, path
    assert not warehouse.queries
    response = client.get(
        "/v1/series/320193?metrics=revenue,net_income&view=as_reported"
        "&as_of=2025-01-01T00:00:00Z&period_type=annual"
    )
    assert response.status_code == 200
    sql, parameters = warehouse.queries[-1]
    assert "metric_series" in sql and "known_from <= %(as_of)s" in sql
    assert parameters["metrics"] == ["net_income", "revenue"]
    assert parameters["view"] == "as_reported" and parameters["period_types"] == ["annual"]
    response = client.get("/v1/series?ticker=aapl&metrics=revenue")
    assert response.status_code == 200 and response.json()["cik"] == 320193
    assert client.get("/v1/series?ticker=MSFT&metrics=revenue").status_code == 404
    client.close()


def test_production_api_rejects_development_credentials_and_plaintext():
    import pytest

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import ConfigurationError

    ready = {
        "_env_file": None,
        "env": "production",
        "api_key": "a" * 32,
        "api_clickhouse_username": "reader",
        "api_clickhouse_password": "reader-secret",
        "postgres_dsn": "postgresql+asyncpg://w:secret@db/x",
        "clickhouse_password": "writer-secret",
        "object_store_secret_key": "store-secret",
        "clickhouse_secure": True,
        "object_store_secure": True,
    }
    Settings(**ready).assert_production_ready()
    for change, message in (
        ({"api_clickhouse_password": "xbrl_api_dev_password"}, "XBRL_API_CLICKHOUSE_PASSWORD"),
        ({"clickhouse_password": "xbrl_dev_password"}, "XBRL_CLICKHOUSE_PASSWORD"),
        ({"clickhouse_secure": False}, "XBRL_CLICKHOUSE_SECURE"),
        ({"object_store_secure": False}, "TLS"),
    ):
        with pytest.raises(ConfigurationError, match=message):
            Settings(**{**ready, **change}).assert_production_ready()
    app = create_app(Settings(**{**ready, "clickhouse_secure": False}))
    with pytest.raises(ConfigurationError, match="XBRL_CLICKHOUSE_SECURE"), TestClient(app):
        pass


def test_settings_repr_hides_secrets():
    from sec_xbrl_warehouse.config import Settings

    settings = Settings(
        _env_file=None,
        postgres_dsn="postgresql+asyncpg://w:pg-secret@db/x",
        clickhouse_password="ch-secret",
        object_store_secret_key="s3-secret",
    )
    text = repr(settings) + str(settings.model_dump())
    for secret in ("pg-secret", "ch-secret", "s3-secret"):
        assert secret not in text


def test_disclosure_routes_validate_filter_and_shape_rows():
    from datetime import date
    from decimal import Decimal

    import pytest

    from sec_xbrl_warehouse.config import Settings

    class DisclosureWarehouse(Warehouse):
        def query(self, sql, parameters=None):
            self.queries.append((sql, parameters))
            if "v_cover_pages_latest" in sql:
                return [
                    {
                        "accession": parameters["accession"],
                        "amendment": 0,
                        "shares_outstanding": Decimal("15000000000.000000000000000000"),
                        "public_float": Decimal("2.500000000000000000"),
                        "security_members": ["us-gaap:CommonStockMember", "ex:NotesMember"],
                        "security_titles": ["Common Stock", "Notes"],
                        "security_symbols": ["AAPL", None],
                        "security_exchanges": ["NASDAQ", None],
                    }
                ]
            if "v_segment_facts_latest" in sql:
                return [
                    {
                        "fact_id": "a" * 32,
                        "kind": "segment",
                        "member_axes": ["segment", "product"],
                        "member_qnames": ["ex:AmericasMember", "us-gaap:ProductMember"],
                        "member_labels": ["Americas", "Products"],
                        "value": Decimal("178000.000000000000000000"),
                        "period_end": date(2025, 9, 27),
                        "is_elimination": 1,
                    }
                ]
            return []

    warehouse = DisclosureWarehouse()
    app = create_app(Settings(_env_file=None, env="test", api_key="k" * 32))
    app.dependency_overrides[get_query_service] = lambda: QueryService(warehouse)
    client = TestClient(app, raise_server_exceptions=False)
    base = "/v1/filings/0000320193-25-000079"
    assert client.get(f"{base}/segments").status_code == 401
    client.headers["Authorization"] = "Bearer " + "k" * 32
    for path in [
        f"{base}/segments?include_eliminations=maybe",
        f"{base}/segments?concept=us-gaap:Revenues'%20OR%201=1--",
        f"{base}/segments?offset=10001",
        f"{base}/segments?offset=-1",
        f"{base}/text-blocks?policy=maybe",
        f"{base}/text-blocks?include_text=true&limit=101",
        f"{base}/text-blocks?concept=us-gaap:{'A' * 512}",
    ]:
        assert client.get(path).status_code == 422, path
    assert not warehouse.queries
    with pytest.raises(ValueError):
        QueryService(warehouse).text_blocks(base[12:], include_text=True, limit=101)

    cover = client.get(f"{base}/cover-page").json()
    assert cover["amendment"] is False
    assert (cover["shares_outstanding"], cover["public_float"]) == (15_000_000_000, 2.5)
    assert cover["securities"][1] == {
        "member": "ex:NotesMember",
        "title": "Notes",
        "symbol": None,
        "exchange": None,
    }
    assert "security_members" not in cover

    response = client.get(
        f"{base}/segments?kind=segment&kind=product&concept=us-gaap:Revenues"
        "&include_eliminations=false&limit=2&offset=4"
    )
    sql, parameters = warehouse.queries[-1]
    assert "hasAny(member_axes, %(kinds)s)" in sql and "is_elimination = 0" in sql
    assert parameters == {
        "accession": "0000320193-25-000079",
        "kinds": ["product", "segment"],
        "concept": "us-gaap:Revenues",
        "limit": 2,
        "offset": 4,
    }
    body = response.json()
    assert (body["limit"], body["offset"], body["count"]) == (2, 4, 1)
    item = body["items"][0]
    assert item["members"][1] == {
        "kind": "product",
        "qname": "us-gaap:ProductMember",
        "label": "Products",
    }
    assert item["is_elimination"] is True and item["value"] == 178000
    assert item["period_end"] == "2025-09-27"

    body = client.get(f"{base}/text-blocks?policy=false&include_text=true&limit=100").json()
    sql, parameters = warehouse.queries[-1]
    assert "value_text AS text" in sql and parameters["policy"] == 0
    assert body["limit"] == 100
    client.get(f"{base}/text-blocks?include_text=true")
    assert warehouse.queries[-1][1]["limit"] == 100
    client.get(f"{base}/text-blocks")
    sql, parameters = warehouse.queries[-1]
    assert "value_text AS text" not in sql and parameters["limit"] == 200
    assert "is_policy" not in parameters
    client.close()
