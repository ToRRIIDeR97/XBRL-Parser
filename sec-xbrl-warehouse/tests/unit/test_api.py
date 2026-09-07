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
