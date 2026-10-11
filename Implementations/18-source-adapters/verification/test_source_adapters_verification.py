"""Independent verification tests for 18-source-adapters."""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from demo_source import DEMO_KEY, DemoAdapter, RecordingSec, demo_filing  # noqa: E402

from sec_xbrl_warehouse.config import Settings  # noqa: E402
from sec_xbrl_warehouse.domain.enums import JobState, JobType  # noqa: E402
from sec_xbrl_warehouse.domain.models import Company, JobRecord  # noqa: E402

integration = pytest.mark.skipif(
    os.getenv("RUN_XBRL_INTEGRATION") != "1", reason="Set RUN_XBRL_INTEGRATION=1"
)


def settings(**values) -> Settings:
    return Settings(_env_file=None, env="test", **values)


def job(payload: dict) -> JobRecord:
    now = datetime.now(UTC)
    return JobRecord(
        id=uuid4(), job_type=JobType.DISCOVER_FILINGS, state=JobState.RUNNING, payload=payload,
        idempotency_key=f"d:{uuid4()}", attempts=1, max_attempts=3, priority=100, available_at=now,
    )


def policy(**overrides):
    from sec_xbrl_warehouse.sources.base import SourcePolicy

    values = dict(
        code="demo",
        allowed_hosts=frozenset({"api.demo.invalid"}),
        max_requests_per_second=50,
        max_concurrency=2,
        headers={"User-Agent": "Warehouse test@example.com"},
    )
    values.update(overrides)
    return SourcePolicy(**values)


class CountingLock:
    def __init__(self) -> None:
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1

    async def release(self) -> None:
        return None


# AC-1: duplicate or malformed adapter codes are rejected at construction.
def test_ac1_registry_rejects_bad_codes() -> None:
    from sec_xbrl_warehouse.sources.registry import SourceRegistry

    with pytest.raises(ValueError):
        SourceRegistry([DemoAdapter(), DemoAdapter()])
    bad = DemoAdapter()
    bad.code = "Demo"
    with pytest.raises(ValueError):
        SourceRegistry([bad])


# AC-1: fiscal fields are derived by the filing's own source.
def test_ac1_fiscal_focus_dispatches_by_filing() -> None:
    from sec_xbrl_warehouse.sources.registry import SourceRegistry

    calls: list[str] = []

    class Demo(DemoAdapter):
        def fiscal_focus(self, bundle):
            calls.append(bundle.filing.accession)
            return bundle, []

    class Bundle:
        filing = demo_filing()

    registry = SourceRegistry([RecordingSec(), Demo()])
    bundle, issues = registry.fiscal_focus(Bundle())
    assert calls == [DEMO_KEY] and issues == []


# AC-2: the egress lock is taken once, on the first allowed request only.
async def test_ac2_lock_taken_once_and_not_for_refused_urls() -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient

    lock = CountingLock()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with SourceHttpClient(
        settings(), policy(), client=httpx.AsyncClient(transport=transport), egress_lock=lock
    ) as client:
        with pytest.raises(SourceAccessError):
            await client.get_json("https://evil.invalid/x")
        assert lock.acquired == 0
        await client.get_json("https://api.demo.invalid/a")
        await client.get_json("https://api.demo.invalid/b")
    assert lock.acquired == 1


# AC-2: a redirect to another host is refused, not followed.
async def test_ac2_redirect_off_policy_is_refused() -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient

    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/file"})

    async with SourceHttpClient(
        settings(), policy(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(SourceAccessError):
            await client.get_bytes("https://api.demo.invalid/file")
    assert hosts == ["api.demo.invalid"]


# AC-2: the policy's request rate holds.
async def test_ac2_policy_rate_limit() -> None:
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with SourceHttpClient(
        settings(),
        policy(max_requests_per_second=4),
        client=httpx.AsyncClient(transport=transport),
    ) as client:
        started = time.monotonic()
        for index in range(9):
            await client.get_json(f"https://api.demo.invalid/{index}")
        elapsed = time.monotonic() - started
    assert elapsed >= 1.5, elapsed


# AC-4: a discovery job without `source` keeps the SEC path; companies are stored.
async def test_ac4_default_source_is_sec_and_companies_are_stored() -> None:
    from sec_xbrl_warehouse.sources.base import DiscoveryResult
    from sec_xbrl_warehouse.sources.registry import SourceRegistry
    from sec_xbrl_warehouse.worker import build_handlers

    company = Company(cik=320193, name="Apple Inc.")

    class Sec(RecordingSec):
        async def discover(self, request):
            self.requests.append(request)
            return DiscoveryResult(filings=(), companies=(company,))

    class Control:
        def __init__(self) -> None:
            self.companies: list = []

        async def enqueue_job(self, **kwargs):
            raise AssertionError("no filings")

        async def upsert_company(self, item):
            self.companies.append(item)

    sec, control = Sec(), Control()
    handlers = build_handlers(services={"control": control, "sources": SourceRegistry([sec, DemoAdapter()])})
    result = await handlers[JobType.DISCOVER_FILINGS](job({"cik": 320193}))
    assert result["filings_enqueued"] == 0
    assert sec.requests and sec.requests[0].entity == "320193"
    assert [item.cik for item in control.companies] == [320193]


# AC-4: a source's discovery returning filings of another source is rejected.
async def test_ac4_adapter_cannot_enqueue_foreign_keys() -> None:
    from sec_xbrl_warehouse.sources.registry import SourceRegistry
    from sec_xbrl_warehouse.worker import build_handlers

    class Control:
        async def enqueue_job(self, **kwargs):
            raise AssertionError("must not enqueue a foreign filing")

        async def upsert_company(self, item):
            return None

    from demo_source import sec_filing

    liar = DemoAdapter(filings=(sec_filing(),))
    handlers = build_handlers(services={"control": Control(), "sources": SourceRegistry([RecordingSec(), liar])})
    with pytest.raises(ValueError):
        await handlers[JobType.DISCOVER_FILINGS](
            job({"source": "demo", "filed_from": "2024-01-01", "filed_to": "2024-01-02"})
        )


# AC-6: an enabled source with no prefixes, and a listed source that isn't enabled.
def test_ac6_prefix_edges(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import required_prefixes

    config = tmp_path / "config"
    config.mkdir()
    (config / "taxonomy_packages.yml").write_text(
        "packages: []\n"
        "required_prefixes:\n  - https://xbrl.fasb.org/us-gaap/2024/\n"
        "source_required_prefixes:\n  other:\n    - xbrl.other.invalid/2024/\n",
        encoding="utf-8",
    )
    assert required_prefixes(settings(config_dir=config, enabled_sources=("sec", "demo"))) == (
        "xbrl.fasb.org/us-gaap/2024/",
    )


# AC-3: the SEC lock still raises the SEC-specific error; a released lock can be retaken.
@integration
async def test_ac3_sec_error_type_and_release(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import (
        SecEgressBusyError,
        SecEgressLock,
        SourceEgressLock,
    )

    first = SecEgressLock(postgres_settings)
    second = SourceEgressLock(postgres_settings, source="sec")
    other = SourceEgressLock(postgres_settings, source="demo")
    try:
        await first.acquire()
        with pytest.raises(SecEgressBusyError):
            await second.acquire()
        await first.release()
        await second.acquire()
        await other.acquire()
        await other.release()
        again = SourceEgressLock(postgres_settings, source="demo")
        await again.acquire()
        await again.release()
    finally:
        for lock in (first, second, other):
            await lock.release()
