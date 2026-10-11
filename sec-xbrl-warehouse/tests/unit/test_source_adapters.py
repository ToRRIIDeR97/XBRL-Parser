"""Source registry, per-source HTTP client, SEC adapter, and source-aware discovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, JobType, PeriodKind
from sec_xbrl_warehouse.domain.models import Company, FilingPackageManifest, FilingRef, JobRecord
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import (
    ConfigurationError,
    SecRetryDeferred,
    SourceAccessError,
    SourceRetryDeferred,
    UnsupportedSourceError,
)
from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, DiscoveryResult, SourcePolicy
from sec_xbrl_warehouse.sources.registry import SourceRegistry
from sec_xbrl_warehouse.sources.sec import SecSourceAdapter
from sec_xbrl_warehouse.worker import build_handlers, is_retryable_exception

DEMO_KEY = "demo-REPORT-2024-001"


def _settings(**values) -> Settings:
    return Settings(_env_file=None, env="test", sec_max_retries=0, **values)


def _filing(key: str = DEMO_KEY, cik: int = 10_000_000_050) -> FilingRef:
    return FilingRef(
        accession=key,
        cik=cik,
        company_name="Demo plc",
        form="AFR",
        filed_date=date(2024, 4, 2),
        archive_directory_url="https://api.demo.invalid/filings/x",
    )


def _bundle(filing: FilingRef, *facts: XbrlFact) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="example.htm",
            document_type="inline",
            taxonomy_namespaces=(),
            source_documents=("example.htm",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(),
        concepts=(),
        facts=facts,
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


def _dei_year(text: str) -> XbrlFact:
    return XbrlFact(
        fact_id="year",
        concept_namespace="http://xbrl.sec.gov/dei/2024",
        concept_name="DocumentFiscalYearFocus",
        concept_qname="dei:DocumentFiscalYearFocus",
        context_id="c1",
        context_hash="0" * 64,
        value_kind=FactValueKind.TEXT,
        raw_value=text,
        value_text=text,
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000320193",
        dimension_hash="0" * 64,
        duplicate_set_hash="dup-year",
        source_document="example.htm",
    )


def _job(payload: dict) -> JobRecord:
    return JobRecord(
        id=uuid4(),
        job_type=JobType.DISCOVER_FILINGS,
        state="running",
        payload=payload,
        idempotency_key=f"k-{uuid4()}",
        attempts=1,
        max_attempts=3,
        priority=100,
        available_at=datetime.now(UTC),
    )


class _Adapter:
    taxonomy_prefixes: tuple[str, ...] = ()

    def __init__(self, code: str, result: DiscoveryResult | None = None) -> None:
        self.code = code
        self.result = result or DiscoveryResult(filings=())
        self.requests: list[DiscoveryRequest] = []
        self.focused: list[str] = []

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        self.requests.append(request)
        return self.result

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        return FilingPackageManifest(filing=filing, members=(), acquired_at=datetime.now(UTC))

    def fiscal_focus(self, bundle):
        self.focused.append(bundle.filing.accession)
        return bundle, ()


class _Control:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.companies: list[Company] = []

    async def enqueue_job(self, **kwargs):
        self.jobs.append(kwargs)
        return uuid4()

    async def upsert_company(self, company: Company) -> None:
        self.companies.append(company)


def _policy(**values) -> SourcePolicy:
    return SourcePolicy(
        **{
            "code": "demo",
            "allowed_hosts": frozenset({"api.demo.invalid"}),
            "max_requests_per_second": 5,
            "max_concurrency": 2,
            "headers": {"User-Agent": "Warehouse test@example.com"},
            **values,
        }
    )


class _Lock:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> None:
        self.acquired += 1

    async def release(self) -> None:
        self.released += 1


# Registry


def test_registry_rejects_duplicate_and_malformed_codes() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        SourceRegistry([_Adapter("demo"), _Adapter("demo")])
    for code in ("Demo", "a", "has space", None):
        with pytest.raises(ValueError, match="Invalid"):
            SourceRegistry([_Adapter(code)])  # type: ignore[arg-type]


def test_registry_dispatches_fiscal_focus_and_needs_a_package_handler(tmp_path) -> None:
    demo, sec = _Adapter("demo"), _Adapter("sec")
    registry = SourceRegistry([sec, demo])
    focused, issues = registry.fiscal_focus(_bundle(_filing()))
    assert demo.focused == [DEMO_KEY] and sec.focused == [] and tuple(issues) == ()
    assert focused.filing.accession == DEMO_KEY
    manifest = FilingPackageManifest(filing=_filing(), members=(), acquired_at=datetime.now(UTC))
    with pytest.raises(ConfigurationError):
        registry.materialize_package(manifest, tmp_path)
    with pytest.raises(UnsupportedSourceError):
        registry.fiscal_focus(_bundle(_filing("esef-NOT-ENABLED")))


# Source HTTP client


async def test_source_client_takes_its_lock_once_and_not_for_refused_urls() -> None:
    lock = _Lock()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with SourceHttpClient(
        _settings(), _policy(), client=httpx.AsyncClient(transport=transport), egress_lock=lock
    ) as client:
        with pytest.raises(SourceAccessError):
            await client.get_json("https://www.sec.gov/files/x.json")
        with pytest.raises(SourceAccessError):
            await client.get_json("http://api.demo.invalid/plain-http")
        assert lock.acquired == 0
        await asyncio.gather(*[client.get_json("https://api.demo.invalid/a") for _ in range(4)])
        assert lock.acquired == 1
    assert lock.released == 1


async def test_source_client_refuses_off_policy_redirects_before_following(tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://www.sec.gov/elsewhere"})

    settings = _settings(data_dir=tmp_path / "data", temp_dir=tmp_path, disk_reserve_bytes=0)
    async with SourceHttpClient(
        settings, _policy(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as client:
        with pytest.raises(SourceAccessError, match="non-demo host"):
            await client.get_text("https://api.demo.invalid/start")
        with pytest.raises(SourceAccessError, match="non-demo host"):
            await client.download("https://api.demo.invalid/start", tmp_path / "file.bin")
    assert seen == ["https://api.demo.invalid/start", "https://api.demo.invalid/start"]


def test_source_client_uses_the_policy_rate_and_concurrency() -> None:
    client = SourceHttpClient(_settings(), _policy(max_requests_per_second=2, max_concurrency=3))
    assert client.rate_limiter._interval == pytest.approx(0.5)
    assert client._semaphore._value == 3
    assert client.client.headers["User-Agent"] == "Warehouse test@example.com"


async def test_source_client_defers_long_retry_after_as_a_source_deferral() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "7200"})
    )
    async with SourceHttpClient(
        _settings(), _policy(), client=httpx.AsyncClient(transport=transport)
    ) as client:
        with pytest.raises(SourceRetryDeferred) as raised:
            await client.get_text("https://api.demo.invalid/busy")
    assert not isinstance(raised.value, SecRetryDeferred)
    assert str(raised.value).startswith("demo retry deferred until")
    assert is_retryable_exception(raised.value)


def test_policy_and_settings_validate_source_codes(monkeypatch) -> None:
    with pytest.raises(ValueError):
        _policy(code="Not a code")
    with pytest.raises(ValueError):
        _policy(max_requests_per_second=0)
    monkeypatch.setenv("XBRL_ENABLED_SOURCES", "sec, demo")
    assert Settings(_env_file=None, env="test").enabled_sources == ("sec", "demo")
    for invalid in (("sec", "sec"), ()):
        with pytest.raises(ValueError):
            _settings(enabled_sources=invalid)


# SEC adapter


async def test_sec_adapter_maps_entity_to_cik_and_needs_its_collaborators() -> None:
    class _Submissions:
        async def discover_with_company(self, cik, *, forms, filed_from, filed_to):
            self.received = (cik, forms, filed_from, filed_to)
            return Company(cik=cik, name="Apple Inc."), ()

    submissions = _Submissions()
    adapter = SecSourceAdapter(submissions=submissions)
    found = await adapter.discover(DiscoveryRequest(entity="320193", forms=("10-K",)))
    assert submissions.received == (320193, ("10-K",), None, None)
    assert [company.cik for company in found.companies] == [320193]
    with pytest.raises(ValueError, match="CIK digits"):
        await adapter.discover(DiscoveryRequest(entity="AAPL"))
    with pytest.raises(ValueError, match="filed_from and filed_to"):
        await adapter.discover(DiscoveryRequest(filed_from=date(2024, 1, 1)))
    with pytest.raises(ConfigurationError):
        await adapter.discover(
            DiscoveryRequest(filed_from=date(2024, 1, 1), filed_to=date(2024, 1, 2))
        )
    with pytest.raises(ConfigurationError):
        await SecSourceAdapter().download_package(_filing("0000320193-25-000079", 320193))


def test_sec_adapter_fiscal_focus_matches_derive_fiscal_focus() -> None:
    from sec_xbrl_warehouse.normalization.fiscal_focus import derive_fiscal_focus

    filing = _filing("0000320193-25-000079", 320193).model_copy(update={"fiscal_year": 2024})
    bundle = _bundle(filing, _dei_year("2025"))
    focused, issues = SecSourceAdapter().fiscal_focus(bundle)
    expected_filing, expected_issues = derive_fiscal_focus(bundle)
    assert focused.filing == expected_filing and focused.filing.fiscal_year == 2025
    assert tuple(issues) == tuple(expected_issues) and issues  # dei replaced discovery
    assert focused.facts == bundle.facts


# Discovery jobs


def test_discovery_defaults_to_sec_and_maps_legacy_cik_to_entity() -> None:
    company = Company(cik=320193, name="Apple Inc.")
    sec = _Adapter("sec", DiscoveryResult(filings=(), companies=(company,)))
    control = _Control()
    handlers = build_handlers(
        services={"control": control, "sources": SourceRegistry([sec]), "forms": ["10-K"]}
    )
    result = asyncio.run(handlers[JobType.DISCOVER_FILINGS](_job({"cik": 320193})))
    assert result == {"filings_enqueued": 0, "companies_stored": 1}
    assert sec.requests == [DiscoveryRequest(entity="320193", forms=("10-K",))]
    assert control.companies == [company]


def test_discovery_rejects_filings_of_another_source_before_any_write() -> None:
    sec_filing = _filing("0000320193-25-000079", 320193)
    company = Company(cik=10_000_000_050, name="Demo plc")
    demo = _Adapter("demo", DiscoveryResult(filings=(sec_filing,), companies=(company,)))
    control = _Control()
    handlers = build_handlers(services={"control": control, "sources": SourceRegistry([demo])})
    with pytest.raises(ValueError, match="other sources"):
        asyncio.run(handlers[JobType.DISCOVER_FILINGS](_job({"source": "demo"})))
    assert control.jobs == [] and control.companies == []
    # SEC default forms never reach another source's adapter.
    assert demo.requests[0].forms == ()


def test_legacy_services_serve_sec_discovery_only() -> None:
    class _Daily:
        async def discover_range(self, filed_from, filed_to, forms):
            return ()

    handlers = build_handlers(services={"control": _Control(), "discovery": _Daily()})
    with pytest.raises(UnsupportedSourceError):
        asyncio.run(handlers[JobType.DISCOVER_FILINGS](_job({"source": "demo"})))


def test_non_sec_egress_busy_defers_without_spending_an_attempt() -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import EgressBusyError
    from sec_xbrl_warehouse.worker import Worker

    failures: list[dict] = []

    class _WorkerControl:
        async def fail_job(self, job_id, error, *, retryable, **kwargs):
            failures.append({"retryable": retryable, **kwargs})
            return "retry"

        async def heartbeat_job(self, *args):
            return None

    async def busy(job):
        raise EgressBusyError("another process holds the demo egress lock")

    settings = _settings(sec_egress_busy_retry_seconds=42)
    worker = Worker(settings, _WorkerControl(), {JobType.FETCH_FILING: busy})  # type: ignore[arg-type]
    asyncio.run(worker._execute(_job({}).model_copy(update={"job_type": JobType.FETCH_FILING})))
    assert failures and failures[0]["retryable"] is True
    assert failures[0]["defer_seconds"] == 42


def test_runtime_services_refuse_an_enabled_source_without_an_adapter() -> None:
    from sec_xbrl_warehouse.worker import runtime_services

    async def start() -> None:
        async with runtime_services(_settings(enabled_sources=("sec", "demo")), object()):  # type: ignore[arg-type]
            pass

    with pytest.raises(ConfigurationError, match="demo"):
        asyncio.run(start())


def test_requires_network_per_source() -> None:
    from sec_xbrl_warehouse.worker import requires_network

    assert requires_network([JobType.DOWNLOAD_BULK], "sec") is True
    assert requires_network([JobType.DOWNLOAD_BULK], "esef") is False
    assert requires_network([JobType.FETCH_FILING], "esef") is True
    assert requires_network([JobType.PARSE_FILING], "esef") is False
    assert requires_network(None, "esef") is True


# Taxonomy prefixes


def test_source_prefixes_strip_schemes_and_tolerate_missing_entries(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import required_prefixes

    config = tmp_path / "config"
    config.mkdir()
    registry = config / "taxonomy_packages.yml"
    registry.write_text(
        "packages: []\n"
        "required_prefixes:\n  - http://xbrl.fasb.org/us-gaap/2024/\n"
        "source_required_prefixes:\n"
        "  sec:\n    - https://xbrl.sec.gov/dei/2024/\n"
        "  demo:\n    - https://xbrl.demo.invalid/taxonomy/2024/\n",
        encoding="utf-8",
    )
    assert required_prefixes(_settings(config_dir=config)) == (
        "xbrl.fasb.org/us-gaap/2024/",
        "xbrl.sec.gov/dei/2024/",
    )
    # An enabled source with no entry adds nothing.
    assert required_prefixes(_settings(config_dir=config, enabled_sources=("sec", "other"))) == (
        "xbrl.fasb.org/us-gaap/2024/",
        "xbrl.sec.gov/dei/2024/",
    )
    registry.write_text("packages: []\nsource_required_prefixes:\n  sec: one\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        required_prefixes(_settings(config_dir=config))
