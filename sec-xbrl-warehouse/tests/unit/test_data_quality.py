from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.validation.data_quality import (
    ArtifactReconciler,
    check_rate,
    reconcile_row_count,
    require_quality,
)


class _FakeWarehouse:
    """Warehouse fake returning deterministic counts per queried table."""

    def __init__(self) -> None:
        self.counts = {
            "sec_submissions": 1,
            "sec_tags": 1,
            "sec_dimensions": 1,
            "sec_numeric_facts": 1,
            "sec_text_facts": 1,
            "sec_renderings": 1,
            "sec_presentations": 1,
            "sec_calculations": 1,
        }
        self.queries: list[str] = []

    def query(self, sql, parameters=None) -> list[dict]:
        self.queries.append(sql)
        if " NOT IN" in sql:
            return [{"n": 0}]  # no orphans
        if "count()" in sql:
            return [{"n": 1}]
        return []


def test_reconcile_row_count_reports_delta() -> None:
    result = reconcile_row_count("sub.gold", 5, 3)
    assert result.passed is False
    assert result.observed == 3
    assert result.expected == 5
    assert result.details == {"delta": -2}


def test_check_rate_threshold() -> None:
    assert check_rate("rate", 2, 10, 0.5).passed is True
    assert check_rate("rate", 6, 10, 0.5).passed is False


def test_require_quality_raises_on_failure() -> None:
    try:
        require_quality([reconcile_row_count("x", 2, 1)])
        raised = False
    except Exception:
        raised = True
    assert raised


def test_reconciler_runs_all_table_checks() -> None:
    warehouse = _FakeWarehouse()
    reconciler = ArtifactReconciler(warehouse)
    results = reconciler.run(
        "a1",
        {table.value: 1 for table in SecTable},
    )
    names = {item.name for item in results}
    assert "sub.gold_count" in names
    assert "num.gold_count" in names
    assert "cal.gold_count" in names
    assert all(item.passed for item in results), [item.name for item in results if not item.passed]


def test_reconciler_handles_missing_tables() -> None:
    class _Missing:
        def query(self, sql, parameters=None) -> list[dict]:
            raise RuntimeError("table missing")

    results = ArtifactReconciler(_Missing()).run("a1", {"sub": 1})
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].observed == "error"


def test_reconciler_writes_report(tmp_path) -> None:
    report = tmp_path / "report.json"
    results = ArtifactReconciler(_FakeWarehouse()).run("a1", {"sub": 1}, report_path=report)
    assert report.is_file()
    assert "artifact_id" in report.read_text(encoding="utf-8")
    assert results
