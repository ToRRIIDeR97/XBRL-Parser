import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
import typer
from typer.testing import CliRunner

from sec_xbrl_warehouse import cli
from sec_xbrl_warehouse.config import Settings


def test_cli_builds_all_commands_and_validates_iso_dates() -> None:
    runner = CliRunner()
    for command in (["--help"], ["filing", "--help"], ["mapping", "approve", "--help"]):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 0, result.output

    result = runner.invoke(
        cli.app,
        [
            "filing",
            "discover",
            "--filed-from",
            "not-a-date",
            "--filed-to",
            "2026-01-02",
        ],
    )
    assert result.exit_code == 2
    assert "filed-from must use YYYY-MM-DD" in result.output


class _Connection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def scalar(self, statement: object) -> str:
        return "0005_job_deferral"


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()


class _Control:
    def __init__(self, settings: Settings) -> None:
        self.engine = _Engine()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Warehouse:
    instances: ClassVar[list["_Warehouse"]] = []

    def __init__(self, settings: Settings) -> None:
        self.closed = False
        self.instances.append(self)

    def _table_definitions(self, path: Path) -> dict[str, object]:
        return {"sec_submissions": object()}

    def query(self, sql: str, parameters: dict[str, object]):
        return [
            {"name": name}
            for name in (
                "sec_submissions",
                "v_sec_numeric_latest",
                "v_sec_text_latest",
                "v_xbrl_facts_latest",
                "v_canonical_facts_latest",
                "v_statement_lines_latest",
            )
        ]

    def schema_drift(self, path: Path) -> tuple[()]:
        return ()

    def ping(self) -> bool:
        return True

    def apply_ddl(self, path: Path) -> tuple[str, ...]:
        return ("001_database.sql",)

    def upgrade_ddl(self, path: Path) -> dict[str, str]:
        return {"sec_submissions": "sec_submissions__backup_1"}

    def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "data/tmp",
        config_dir=tmp_path / "config",
        arelle_cache_dir=tmp_path / "data/cache",
        arelle_taxonomy_packages_dir=tmp_path / "data/packages",
        object_store_backend="local",
    )
    settings.ensure_directories()
    settings.config_dir.mkdir()
    return settings


def test_doctor_checks_config_schema_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    class _Store:
        bucket = "test"

        def ensure_bucket(self) -> None:
            return None

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_yaml", lambda path: {})
    monkeypatch.setattr(cli, "ClickHouseWarehouse", _Warehouse)
    monkeypatch.setattr(cli, "PostgresControlRepository", _Control)
    monkeypatch.setattr(cli, "create_object_store", lambda settings: _Store())

    cli.doctor(skip_sec_identity=True)
    assert _Warehouse.instances[-1].closed


def test_database_commands_close_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ClickHouseWarehouse", _Warehouse)

    cli.init_clickhouse(Path("sql/clickhouse"))
    cli.upgrade_clickhouse(True, Path("sql/clickhouse"))
    assert all(instance.closed for instance in _Warehouse.instances[-2:])
    with pytest.raises(typer.BadParameter, match="--yes"):
        cli.upgrade_clickhouse(False, Path("sql/clickhouse"))


def test_discover_latest_uses_completed_overlapping_utc_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    async def enqueue(filed_from, filed_to, forms, refresh):
        captured.extend((filed_from, filed_to, forms, refresh))

    monkeypatch.setattr(cli, "_enqueue_discovery", enqueue)
    cli.filing_discover_latest(3, "10-K,10-Q")

    filed_from, filed_to, forms, refresh = captured
    assert (filed_to - filed_from).days == 2
    assert filed_to < datetime.now(UTC).date()
    assert forms == "10-K,10-Q"
    assert refresh is False


def test_worker_metrics_server_lives_for_worker_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"worker_metrics_host": "127.0.0.1", "worker_metrics_port": 19091}
    )
    events: list[str] = []

    class _Server:
        def shutdown(self) -> None:
            events.append("metrics-shutdown")

        def server_close(self) -> None:
            events.append("metrics-close")

    class _Worker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def run(self) -> None:
            events.append("worker-run")

    class _RuntimeControl(_Control):
        async def close(self) -> None:
            events.append("control-close")

    @asynccontextmanager
    async def services(settings: Settings, control: _RuntimeControl):
        yield {"ready": True}

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PostgresControlRepository", _RuntimeControl)
    monkeypatch.setattr(cli, "runtime_services", services)
    monkeypatch.setattr(cli, "build_handlers", lambda services: {})
    monkeypatch.setattr(cli, "Worker", _Worker)
    monkeypatch.setattr(
        "prometheus_client.start_http_server",
        lambda port, addr: (_Server(), object()),
    )

    asyncio.run(cli._run_worker())
    assert events == ["worker-run", "control-close", "metrics-shutdown", "metrics-close"]


def test_worker_run_parses_job_type_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from sec_xbrl_warehouse.domain.enums import JobType

    received = []

    async def run_worker(job_types=None) -> None:
        received.append(job_types)

    monkeypatch.setattr(cli, "_run_worker", run_worker)
    result = CliRunner().invoke(cli.app, ["worker", "run", "--job-type", "ingest_filing"])
    assert result.exit_code == 0, result.output
    assert received == [[JobType.INGEST_FILING]]


def test_filing_discover_with_cik_queues_submissions_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    (settings.config_dir / "sources.yml").write_text("sec:\n  default_forms: [10-K]\n")
    jobs: list[dict[str, object]] = []

    class _Queue:
        def __init__(self, settings) -> None:
            pass

        async def enqueue_job(self, **kwargs):
            jobs.append(kwargs)
            return "job"

        async def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PostgresControlRepository", _Queue)
    cli.filing_discover(None, None, None, False, 320193)
    assert jobs[0]["payload"] == {
        "filed_from": None,
        "filed_to": None,
        "forms": ["10-K"],
        "cik": 320193,
    }
    # --refresh gives the same issuer a fresh idempotency key.
    cli.filing_discover(None, None, None, True, 320193)
    cli.filing_discover(None, None, None, False, 320193)
    keys = [job["idempotency_key"] for job in jobs]
    assert keys[0] == keys[2] != keys[1]
    with pytest.raises(typer.BadParameter):
        cli.filing_discover(None, None, None, False, None)


def test_company_show_resolves_cik_and_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sec_xbrl_warehouse.domain.models import Company

    apple = Company(cik=320193, name="Apple Inc.", tickers=("AAPL",), fiscal_year_end="0927")

    class _Companies:
        def __init__(self, settings) -> None:
            pass

        async def get_company(self, cik):
            return apple if cik == 320193 else None

        async def find_company_by_ticker(self, ticker):
            return apple if ticker.upper() == "AAPL" else None

        async def close(self) -> None:
            pass

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PostgresControlRepository", _Companies)
    cli.company_show("aapl")
    cli.company_show("320193")
    assert capsys.readouterr().out.count('"cik": 320193') == 2
    with pytest.raises(typer.Exit):
        cli.company_show("MSFT")


def test_jobs_list_and_retry_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from uuid import uuid4

    from sec_xbrl_warehouse.domain.enums import JobState, JobType
    from sec_xbrl_warehouse.domain.models import JobRecord
    from sec_xbrl_warehouse.exceptions import StateTransitionError

    dead = JobRecord(
        id=uuid4(),
        job_type=JobType.INGEST_FILING,
        state=JobState.DEAD,
        idempotency_key="k",
        payload={},
        attempts=5,
        max_attempts=5,
        priority=100,
        available_at=datetime.now(UTC),
        last_error="boom",
    )
    calls: list[object] = []

    class _Jobs:
        def __init__(self, settings) -> None:
            pass

        async def list_jobs(self, **kwargs):
            calls.append(kwargs)
            return [dead]

        async def retry_job(self, job_id):
            if job_id != dead.id:
                raise StateTransitionError("not dead")
            return dead.model_copy(update={"state": JobState.RETRY, "attempts": 0})

        async def close(self) -> None:
            pass

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PostgresControlRepository", _Jobs)
    cli.jobs_list(JobState.DEAD, JobType.INGEST_FILING, 10)
    assert calls == [{"state": "dead", "job_type": "ingest_filing", "limit": 10}]
    cli.jobs_retry(str(dead.id))
    assert "is retry" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        cli.jobs_retry(str(uuid4()))
