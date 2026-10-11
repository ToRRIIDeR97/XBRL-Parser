"""Worker-visible acceptance tests for 18-source-adapters."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest
from demo_source import DEMO_KEY, DemoAdapter, RecordingSec, demo_filing, sec_filing

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobState, JobType
from sec_xbrl_warehouse.domain.models import JobRecord

integration = pytest.mark.skipif(
    os.getenv("RUN_XBRL_INTEGRATION") != "1", reason="Set RUN_XBRL_INTEGRATION=1"
)


def settings(**values) -> Settings:
    return Settings(_env_file=None, env="test", **values)


def job(job_type: JobType, payload: dict) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id=uuid4(), job_type=job_type, state=JobState.RUNNING, payload=payload,
        idempotency_key=f"{job_type.value}:{uuid4()}", attempts=1, max_attempts=3,
        priority=100, available_at=now,
    )


# AC-1: the registry holds enabled adapters and routes by filing key.
async def test_ac1_registry_routes_by_filing_key() -> None:
    from sec_xbrl_warehouse.exceptions import UnsupportedSourceError
    from sec_xbrl_warehouse.sources.registry import SourceRegistry

    demo, sec = DemoAdapter(), RecordingSec()
    registry = SourceRegistry([sec, demo])
    assert registry.codes == ("demo", "sec")
    assert registry.adapter_for(DEMO_KEY) is demo
    assert registry.adapter_for("0000320193-25-000079") is sec
    await registry.download_package(demo_filing())
    await registry.download_package(sec_filing())
    assert demo.downloaded == [DEMO_KEY] and sec.downloaded == ["0000320193-25-000079"]
    with pytest.raises(UnsupportedSourceError):
        registry.adapter_for("esef-NOT-ENABLED")
    with pytest.raises(UnsupportedSourceError):
        registry.adapter("esef")


# AC-1: enabled sources come from settings; SEC is the default.
def test_ac1_enabled_sources_setting() -> None:
    assert settings().enabled_sources == ("sec",)
    assert settings(enabled_sources=("sec", "demo")).enabled_sources == ("sec", "demo")
    with pytest.raises(ValueError):
        settings(enabled_sources=("Not A Source",))


# AC-2: a source client only talks to its own hosts, with its own identity.
async def test_ac2_source_client_enforces_its_policy() -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
    from sec_xbrl_warehouse.sources.base import SourcePolicy

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    policy = SourcePolicy(
        code="demo",
        allowed_hosts=frozenset({"api.demo.invalid"}),
        max_requests_per_second=5,
        max_concurrency=2,
        headers={"User-Agent": "Warehouse test@example.com"},
    )
    async with SourceHttpClient(
        settings(), policy, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as client:
        assert await client.get_json("https://api.demo.invalid/filings") == {"ok": True}
        with pytest.raises(SourceAccessError):
            await client.get_json("https://www.sec.gov/Archives/edgar/data/1/index.json")
    assert [request.url.host for request in seen] == ["api.demo.invalid"]
    assert seen[0].headers["User-Agent"] == "Warehouse test@example.com"


# AC-2: the SEC client is unchanged for callers.
def test_ac2_sec_client_keeps_its_hosts_and_errors() -> None:
    from sec_xbrl_warehouse.exceptions import SecAccessError, SourceAccessError
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient

    assert SecHttpClient.ALLOWED_HOSTS == frozenset({"www.sec.gov", "sec.gov", "data.sec.gov"})
    assert issubclass(SecAccessError, SourceAccessError)


# AC-3: one egress lock per source; the SEC lock keeps its name.
def test_ac3_lock_names() -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import LOCK_NAME, lock_name

    assert LOCK_NAME == "xbrl:sec-egress" == lock_name("sec")
    assert lock_name("demo") == "xbrl:demo-egress"


@integration
async def test_ac3_sources_lock_independently(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import EgressBusyError, SourceEgressLock

    sec_a = SourceEgressLock(postgres_settings, source="sec")
    sec_b = SourceEgressLock(postgres_settings, source="sec")
    demo = SourceEgressLock(postgres_settings, source="demo")
    try:
        await sec_a.acquire()
        await demo.acquire()  # a different source is not blocked
        with pytest.raises(EgressBusyError):
            await sec_b.acquire()
    finally:
        for lock in (sec_a, sec_b, demo):
            await lock.release()


# AC-4: discovery jobs name a source; fetch jobs are queued for what it finds.
async def test_ac4_discovery_job_uses_the_named_source() -> None:
    from sec_xbrl_warehouse.domain.xbrl import CURRENT_EXTRACTION_REVISION
    from sec_xbrl_warehouse.sources.registry import SourceRegistry
    from sec_xbrl_warehouse.worker import build_handlers

    class Control:
        def __init__(self) -> None:
            self.jobs: list[dict] = []

        async def enqueue_job(self, **kwargs):
            self.jobs.append(kwargs)
            return uuid4()

        async def upsert_company(self, company):
            return None

    demo = DemoAdapter(filings=(demo_filing(),))
    control = Control()
    handlers = build_handlers(
        services={"control": control, "sources": SourceRegistry([RecordingSec(), demo]), "forms": ["10-K"]}
    )
    result = await handlers[JobType.DISCOVER_FILINGS](
        job(JobType.DISCOVER_FILINGS, {"source": "demo", "filed_from": "2024-04-01", "filed_to": "2024-04-03"})
    )
    assert result["filings_enqueued"] == 1
    request = demo.requests[0]
    assert (request.filed_from, request.filed_to) == (date(2024, 4, 1), date(2024, 4, 3))
    assert [item["idempotency_key"] for item in control.jobs] == [
        f"fetch:{DEMO_KEY}:{CURRENT_EXTRACTION_REVISION}"
    ]
    assert control.jobs[0]["job_type"] == JobType.FETCH_FILING.value


# AC-4: a discovery job for a source that isn't enabled fails without retrying.
async def test_ac4_discovery_for_disabled_source_is_not_retried() -> None:
    from sec_xbrl_warehouse.exceptions import UnsupportedSourceError
    from sec_xbrl_warehouse.sources.registry import SourceRegistry
    from sec_xbrl_warehouse.worker import build_handlers, is_retryable_exception

    class Control:
        async def enqueue_job(self, **kwargs):
            raise AssertionError("nothing to enqueue")

    handlers = build_handlers(services={"control": Control(), "sources": SourceRegistry([RecordingSec()])})
    with pytest.raises(UnsupportedSourceError) as raised:
        await handlers[JobType.DISCOVER_FILINGS](
            job(JobType.DISCOVER_FILINGS, {"source": "demo", "filed_from": "2024-04-01", "filed_to": "2024-04-02"})
        )
    assert not is_retryable_exception(raised.value)


# AC-6: required taxonomy prefixes cover every enabled source, and only those.
def test_ac6_taxonomy_prefixes_follow_enabled_sources(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import required_prefixes

    config = tmp_path / "config"
    config.mkdir()
    (config / "taxonomy_packages.yml").write_text(
        "packages: []\n"
        "required_prefixes:\n  - xbrl.fasb.org/us-gaap/2024/\n"
        "source_required_prefixes:\n  demo:\n    - xbrl.demo.invalid/taxonomy/2024/\n",
        encoding="utf-8",
    )
    sec_only = required_prefixes(settings(config_dir=config))
    both = required_prefixes(settings(config_dir=config, enabled_sources=("sec", "demo")))
    assert sec_only == ("xbrl.fasb.org/us-gaap/2024/",)
    assert set(both) == {"xbrl.fasb.org/us-gaap/2024/", "xbrl.demo.invalid/taxonomy/2024/"}
