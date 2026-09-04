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
