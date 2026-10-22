"""ESEF (filings.xbrl.org) adapter: discovery, packages, fiscal focus, policy, wiring, parse path."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import FactValueKind, JobType, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    ConfigurationError,
    UnsupportedSourceError,
    XbrlExtractionError,
)
from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    filing_package_key,
)
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, SourcePolicy, check_disjoint_hosts
from sec_xbrl_warehouse.sources.esef import (
    ESEF_POLICY,
    ESEF_TAXONOMY_PREFIXES,
    LEI_SCHEME,
    EsefSourceAdapter,
    esef_policy,
    normalize_lei,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
HOST = "https://filings.xbrl.org"
LEI_A = "213800H2PQMIF3OVZY47"
LEI_B = "5493001KJTIIGC8Y1R12"
_ZIP_TIME = (2024, 1, 1, 0, 0, 0)


def _zip(entries: dict[str, str | bytes]) -> bytes:
    """A zip with fixed entry timestamps, so equal entries give equal bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=_ZIP_TIME), data)
    return buffer.getvalue()


def _package(lei: str = LEI_A, period: str = "2023-12-31", **extra: str | bytes) -> bytes:
    root = f"{lei}-{period}-ESEF-GB-0"
    return _zip(
        {
            f"{root}/META-INF/reportPackage.json": "{}",
            f"{root}/META-INF/taxonomyPackage.xml": "<taxonomyPackage/>",
            f"{root}/reports/{lei}-{period}-en.xhtml": "<html/>",
            **extra,
        }
    )


def _record(
    fxo_id: str,
    lei: str,
    *,
    added: str,
    digest: str = "0" * 64,
    period_end: str = "2023-12-31",
    package_url: str | None = None,
    entity_id: str | None = None,
) -> dict:
    base = f"/{lei}/{period_end}/ESEF/GB/0"
    return {
        "type": "filing",
        "id": fxo_id,
        "attributes": {
            "fxo_id": fxo_id,
            "country": "GB",
            "date_added": f"{added} 00:00:00",
            "period_end": period_end,
            "sha256": digest,
            "package_url": package_url if package_url is not None else f"{base}/{fxo_id}.zip",
            "report_url": f"{base}/reports/{lei}-{period_end}-en.xhtml",
        },
        "relationships": {"entity": {"data": {"type": "entity", "id": entity_id or f"e-{lei}"}}},
    }


def _entity(lei: str, name: str, entity_id: str | None = None) -> dict:
    return {
        "type": "entity",
        "id": entity_id or f"e-{lei}",
        "attributes": {"identifier": lei, "name": name},
    }


class _Fxo:
    def __init__(self, pages: list[dict], packages: dict[str, bytes] | None = None) -> None:
        self.pages = pages
        self.packages = packages or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "filings.xbrl.org"
        if request.url.path == "/api/filings":
            wanted = request.url.params.get("filter[fxo_id]")
            if wanted is not None:
                found = [
                    record
                    for page in self.pages
                    for record in page["data"]
                    if record["attributes"]["fxo_id"] == wanted
                ]
                return httpx.Response(200, json={"data": found, "links": {}})
            return httpx.Response(200, json=self.pages[int(request.url.params["page[number]"]) - 1])
        if request.url.path in self.packages:
            return httpx.Response(200, content=self.packages[request.url.path])
        return httpx.Response(404)


class _Keys:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.keys: dict[tuple[str, str], int] = {}

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        self.calls.append((scheme, identifier))
        return self.keys.setdefault((scheme, identifier), 10_000_000_001 + len(self.keys))


def _settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        sec_max_retries=0,
        **values,
    )


def _adapter(tmp_path: Path, fxo: _Fxo, keys: _Keys | None = None, **settings):
    configured = _settings(tmp_path, **settings)
    policy = replace_rate(ESEF_POLICY)
    http = SourceHttpClient(
        configured, policy, client=httpx.AsyncClient(transport=httpx.MockTransport(fxo))
    )
    store = LocalObjectStore(tmp_path / "objects", bucket="test")
    return EsefSourceAdapter(configured, http, store, keys or _Keys()), store


def replace_rate(policy: SourcePolicy) -> SourcePolicy:
    """The ESEF policy without its delay between canned responses (tests only)."""
    from dataclasses import replace

    return replace(policy, max_requests_per_second=1000.0)


def _feed(*records: dict, entities: tuple[dict, ...] = (), next_page: bool = False) -> dict:
    return {
        "data": list(records),
        "included": list(entities),
        "links": {"next": f"{HOST}/api/filings?page[number]=2"} if next_page else {},
    }


def _run(coro):
    return asyncio.run(coro)


# Discovery ----------------------------------------------------------------------


def test_page_cap_stops_paging(tmp_path) -> None:
    page = _feed(
        _record("A-1", LEI_A, added="2024-04-10"),
        entities=(_entity(LEI_A, "A plc"),),
        next_page=True,
    )
    fxo = _Fxo([page, page, page])
    adapter, _ = _adapter(tmp_path, fxo, esef_max_pages=2, esef_page_size=7)
    result = _run(adapter.discover(DiscoveryRequest()))
    assert len(fxo.requests) == 2
    assert [filing.accession for filing in result.filings] == ["esef-A-1"]  # deduplicated
    assert fxo.requests[0].url.params["page[size]"] == "7"


def test_open_window_pages_until_no_next_link(tmp_path) -> None:
    fxo = _Fxo(
        [
            _feed(
                _record("A-2", LEI_A, added="2024-04-10"),
                entities=(_entity(LEI_A, "A"),),
                next_page=True,
            ),
            _feed(_record("A-1", LEI_A, added="2019-01-01"), entities=(_entity(LEI_A, "A"),)),
        ]
    )
    adapter, _ = _adapter(tmp_path, fxo)
    result = _run(adapter.discover(DiscoveryRequest(filed_to=date(2024, 12, 31))))
    assert [filing.accession for filing in result.filings] == ["esef-A-2", "esef-A-1"]
    assert len(fxo.requests) == 2


def test_unsafe_records_are_skipped(tmp_path) -> None:
    good = _record("A-1", LEI_A, added="2024-04-10")
    bad_key = _record("bad id", LEI_A, added="2024-04-10")
    traversal = _record("A-2", LEI_A, added="2024-04-10", package_url="/x/../../etc/passwd.zip")
    other_host = _record("A-3", LEI_A, added="2024-04-10", package_url="https://evil.example/a.zip")
    no_lei = _record("A-4", LEI_A, added="2024-04-10", entity_id="missing")
    bad_lei = _record("A-5", "NOT-AN-LEI", added="2024-04-10")
    bad_date = _record("A-6", LEI_A, added="yesterday")
    keys = _Keys()
    fxo = _Fxo(
        [
            _feed(
                good,
                bad_key,
                traversal,
                other_host,
                no_lei,
                bad_lei,
                bad_date,
                entities=(_entity(LEI_A, "A plc"), _entity("NOT-AN-LEI", "Bad")),
            )
        ]
    )
    adapter, _ = _adapter(tmp_path, fxo, keys)
    result = _run(adapter.discover(DiscoveryRequest()))
    assert [filing.accession for filing in result.filings] == ["esef-A-1"]
    assert keys.calls == [(LEI_SCHEME, LEI_A)]


def test_one_entity_key_per_normalized_lei(tmp_path) -> None:
    keys = _Keys()
    fxo = _Fxo(
        [
            _feed(
                _record("A-2", LEI_A, added="2024-04-10", entity_id="x1"),
                _record("A-1", LEI_A, added="2024-04-09", entity_id="x2"),
                _record("B-1", LEI_B, added="2024-04-08"),
                entities=(
                    _entity(f" {LEI_A.lower()} ", "Kainos", "x1"),
                    _entity(LEI_A, "KAINOS GROUP PLC", "x2"),
                    _entity(LEI_B, "Example SE"),
                ),
            )
        ]
    )
    adapter, _ = _adapter(tmp_path, fxo, keys)
    result = _run(adapter.discover(DiscoveryRequest()))
    assert keys.calls == [(LEI_SCHEME, LEI_A), (LEI_SCHEME, LEI_B)]
    ciks = [filing.cik for filing in result.filings]
    assert ciks[0] == ciks[1] != ciks[2]
    assert len(result.companies) == 2
    assert {company.cik for company in result.companies} == set(ciks)

    # The request's LEI is normalized the same way.
    adapter, _ = _adapter(tmp_path, _Fxo(fxo.pages), keys)
    only_a = _run(adapter.discover(DiscoveryRequest(entity=f"  {LEI_A.lower()}\n")))
    assert [filing.accession for filing in only_a.filings] == ["esef-A-2", "esef-A-1"]
    assert normalize_lei(" 5493001kjtiigc8y1r12 ") == LEI_B
    assert normalize_lei("too-short") is None


def test_malformed_entity_request_is_refused(tmp_path) -> None:
    fxo = _Fxo([])
    adapter, _ = _adapter(tmp_path, fxo)
    with pytest.raises(ValueError, match="LEI"):
        _run(adapter.discover(DiscoveryRequest(entity="320193")))
    assert fxo.requests == []


def test_countries_and_forms(tmp_path) -> None:
    page = _feed(_record("A-1", LEI_A, added="2024-04-10"), entities=(_entity(LEI_A, "A"),))
    fxo = _Fxo([page])
    adapter, _ = _adapter(tmp_path, fxo, esef_countries="gb, de")
    _run(adapter.discover(DiscoveryRequest()))
    assert [request.url.params["filter[country]"] for request in fxo.requests] == ["GB", "DE"]

    other = _Fxo([page])
    adapter, _ = _adapter(tmp_path, other)
    assert _run(adapter.discover(DiscoveryRequest(forms=("10-K",)))).filings == ()
    assert other.requests == []
    with pytest.raises(ValueError):
        _settings(tmp_path, esef_countries="GBR")


def test_parse_only_adapter_has_no_network(tmp_path) -> None:
    adapter = EsefSourceAdapter(_settings(tmp_path), None, LocalObjectStore(tmp_path), _Keys())
    with pytest.raises(ConfigurationError):
        _run(adapter.discover(DiscoveryRequest()))


# Packages -----------------------------------------------------------------------


def _filing(lei: str = LEI_A) -> FilingRef:
    return FilingRef(
        accession=f"esef-{lei}-2023-12-31-ESEF-GB-0",
        cik=10_000_000_001,
        company_name="Kainos",
        form="ESEF",
        filed_date=date(2024, 4, 10),
        report_date=date(2023, 12, 31),
        archive_directory_url=f"{HOST}/{lei}/2023-12-31/ESEF/GB/0",
    )


def _download(tmp_path: Path, package: bytes, *, digest: str | None = None, **settings):
    filing = _filing()
    fxo_id = filing.accession.removeprefix("esef-")
    record = _record(
        fxo_id,
        LEI_A,
        added="2024-04-10",
        digest=sha256(package).hexdigest() if digest is None else digest,
    )
    fxo = _Fxo([_feed(record)], {f"/{LEI_A}/2023-12-31/ESEF/GB/0/{fxo_id}.zip": package})
    adapter, store = _adapter(tmp_path, fxo, **settings)
    return adapter, store, fxo, filing


@pytest.mark.parametrize(
    ("package", "settings"),
    [
        pytest.param(_package(**{"../evil.xsd": "x"}), {}, id="traversal"),
        pytest.param(_package(**{"/abs/evil.xsd": "x"}), {}, id="absolute"),
        pytest.param(b"not a zip at all", {}, id="not-zip"),
        pytest.param(_package(), {"filing_max_members": 2}, id="member-limit"),
        pytest.param(
            _zip({"top/META-INF/reportPackage.json": "{}", "top/x.xsd": "<schema/>"}),
            {},
            id="no-report",
        ),
        pytest.param(
            _zip({"top/reports/a.xhtml": "<html/>", "top/reports/b.xhtml": "<html/>"}),
            {},
            id="ambiguous-report",
        ),
    ],
)
def test_invalid_packages_are_rejected_and_quarantined(tmp_path, package, settings) -> None:
    adapter, store, _, filing = _download(tmp_path, package, **settings)
    with pytest.raises(ArtifactIntegrityError):
        _run(adapter.download_package(filing))
    assert not list(store.list_objects("bronze/"))
    assert not list(store.list_objects("manifests/"))
    quarantined = [key for key, _ in store.list_objects("quarantine/esef/")]
    assert quarantined == [
        f"quarantine/esef/filings/{filing.accession}/sha256={sha256(package).hexdigest()}/package.zip"
    ]


def test_missing_digest_is_refused_before_download(tmp_path) -> None:
    adapter, store, fxo, filing = _download(tmp_path, _package(), digest="")
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        _run(adapter.download_package(filing))
    assert [request.url.path for request in fxo.requests] == ["/api/filings"]
    assert not list(store.list_objects())


def test_download_is_idempotent(tmp_path) -> None:
    package = _package()
    adapter, store, fxo, filing = _download(tmp_path, package)
    first = _run(adapter.download_package(filing))
    second = _run(adapter.download_package(filing))
    assert first == second
    digest = sha256(package).hexdigest()
    assert first.package_object_key == filing_package_key(filing, digest)
    assert [key for key, _ in store.list_objects("bronze/")] == [first.package_object_key]
    assert len(list(store.list_objects("manifests/"))) == 1
    # The second call re-reads the record but finds the stored package and manifest.
    assert [request.url.path for request in fxo.requests].count("/api/filings") == 2
    assert len(fxo.requests) == 3
    stored = tmp_path / "copy.zip"
    store.get_file(first.package_object_key, stored)
    assert stored.read_bytes() == package  # byte-identical
    assert first.manifest_object_key and first.manifest_object_key.startswith(
        f"manifests/esef/filings/{filing.accession}/"
    )


def test_other_sources_are_refused(tmp_path) -> None:
    adapter, _, fxo, _ = _download(tmp_path, _package())
    sec = FilingRef(
        accession="0000320193-24-000123",
        cik=320193,
        company_name="Apple",
        form="10-K",
        filed_date=date(2024, 11, 1),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/x",
    )
    with pytest.raises(UnsupportedSourceError):
        _run(adapter.download_package(sec))
    assert fxo.requests == []


def test_stored_package_materializes_its_report_entrypoint(tmp_path) -> None:
    package = _package(
        **{
            f"{LEI_A}-2023-12-31-ESEF-GB-0/META-INF/catalog.xml": "<catalog/>",
            f"{LEI_A}-2023-12-31-ESEF-GB-0/www.example.com/ext.xsd": "<schema/>",
        }
    )
    adapter, store, _, filing = _download(tmp_path, package)
    manifest = _run(adapter.download_package(filing))
    packages = FilingPackageDownloader(_settings(tmp_path), None, store)
    entrypoint = packages.materialize_package(manifest, tmp_path / "staging")
    assert (
        entrypoint
        == (
            tmp_path
            / "staging"
            / f"{LEI_A}-2023-12-31-ESEF-GB-0"
            / "reports"
            / f"{LEI_A}-2023-12-31-en.xhtml"
        ).resolve()
    )
    assert (entrypoint.parents[1] / "www.example.com" / "ext.xsd").is_file()


# Fiscal focus -------------------------------------------------------------------


def _fact(
    end: date, *, dimensional: bool = False, kind: PeriodKind = PeriodKind.DURATION
) -> XbrlFact:
    from sec_xbrl_warehouse.domain.xbrl import DimensionMember

    return XbrlFact(
        fact_id=f"f-{end}-{dimensional}-{kind.value}",
        source_document="r.xhtml",
        concept_qname="ifrs-full:Revenue",
        concept_namespace="https://xbrl.ifrs.org/taxonomy/2023-03-23/ifrs-full",
        concept_name="Revenue",
        context_id="c",
        context_hash="h",
        entity_scheme=LEI_SCHEME,
        entity_identifier=LEI_A,
        period_kind=kind,
        period_start=date(end.year - 1, end.month, 1) if kind is PeriodKind.DURATION else None,
        period_end=end if kind is PeriodKind.DURATION else None,
        period_instant=end if kind is PeriodKind.INSTANT else None,
        dimensions=(
            (DimensionMember(axis_namespace="x", axis_name="SegmentsAxis", member_name="Seg"),)
            if dimensional
            else ()
        ),
        dimension_hash="d" if dimensional else "",
        value_kind=FactValueKind.NUMERIC,
        raw_value="1",
        duplicate_set_hash=f"g-{end}-{dimensional}",
    )


def _bundle(report_date: date | None, *facts: XbrlFact) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=_filing().model_copy(update={"report_date": report_date}),
        metadata=XbrlDocumentMetadata(
            entrypoint="r.xhtml",
            document_type="inline",
            taxonomy_namespaces=(),
            source_documents=("r.xhtml",),
            software_name="arelle",
            software_version="test",
        ),
        contexts=(),
        concepts=(),
        facts=facts,
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


def test_fiscal_year_from_latest_non_dimensional_duration_fact(tmp_path) -> None:
    adapter = EsefSourceAdapter(_settings(tmp_path), None, LocalObjectStore(tmp_path), _Keys())
    bundle = _bundle(
        None,
        _fact(date(2023, 3, 31)),
        _fact(date(2022, 3, 31)),
        _fact(date(2025, 3, 31), dimensional=True),
        _fact(date(2024, 3, 31), kind=PeriodKind.INSTANT),
    )
    focused, issues = adapter.fiscal_focus(bundle)
    assert (focused.filing.fiscal_year, focused.filing.fiscal_period) == (2023, "FY")
    assert issues == ()

    empty, issues = adapter.fiscal_focus(_bundle(None))
    assert (empty.filing.fiscal_year, empty.filing.fiscal_period) == (None, "FY")
    assert [issue.code for issue in issues] == ["warehouse:fiscalYearUnknown"]


# Policy, taxonomy prefixes, wiring ----------------------------------------------


def test_policies_have_disjoint_hosts(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SEC_HOSTS, SecHttpClient
    from sec_xbrl_warehouse.sources.registry import source_policies

    settings = _settings(tmp_path)
    policies = source_policies(settings)
    assert set(policies) == {"sec", "esef", "edinet", "dart"}
    assert (
        policies["esef"].allowed_hosts
        == ESEF_POLICY.allowed_hosts
        == frozenset({"filings.xbrl.org"})
    )
    assert not ESEF_POLICY.allowed_hosts & SEC_HOSTS
    assert SecHttpClient.ALLOWED_HOSTS == SEC_HOSTS
    assert not ESEF_POLICY.allowed_hosts & set(settings.allowed_taxonomy_hosts)
    assert esef_policy(settings).headers["User-Agent"] == settings.sec_user_agent

    overlapping = SourcePolicy("demo", frozenset({"WWW.SEC.GOV"}), 1, 1)
    with pytest.raises(ValueError, match="both allow host"):
        check_disjoint_hosts([*policies.values(), overlapping])
    with pytest.raises(ValueError, match="more than one"):
        check_disjoint_hosts([ESEF_POLICY, ESEF_POLICY])


def test_esef_client_refuses_sec_hosts(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError

    client = SourceHttpClient(_settings(tmp_path), ESEF_POLICY)
    for url in (
        "https://www.sec.gov/Archives/x",
        "https://xbrl.ifrs.org/x",
        "http://filings.xbrl.org/api",
    ):
        with pytest.raises(SourceAccessError):
            client._validate_url(url)


def test_esef_taxonomy_prefixes_fail_closed_until_pinned(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
        coverage_gaps,
        registered_packages,
        required_prefixes,
    )

    registry = load_yaml(REPO_CONFIG / "taxonomy_packages.yml")
    assert tuple(registry["source_required_prefixes"]["esef"]) == ESEF_TAXONOMY_PREFIXES
    hosts = set(registry["network_policy"]["allowed_taxonomy_hosts"])
    assert {"www.esma.europa.eu", "xbrl.ifrs.org", "www.ifrs.org"} <= hosts

    sec_only = _settings(tmp_path, config_dir=REPO_CONFIG)
    with_esef = _settings(tmp_path, config_dir=REPO_CONFIG, enabled_sources=("sec", "esef"))
    assert not set(ESEF_TAXONOMY_PREFIXES) & set(required_prefixes(sec_only))
    assert set(ESEF_TAXONOMY_PREFIXES) <= set(required_prefixes(with_esef))
    pinned = {"xbrl.ifrs.org/taxonomy/2019-03-27/", "xbrl.ifrs.org/taxonomy/2020-03-16/"}
    gaps = coverage_gaps(with_esef, registered_packages(with_esef))
    assert set(gaps) & set(ESEF_TAXONOMY_PREFIXES) == set(ESEF_TAXONOMY_PREFIXES) - pinned
    assert not set(ESEF_TAXONOMY_PREFIXES) & set(
        coverage_gaps(sec_only, registered_packages(sec_only))
    )


def _patched_runtime(monkeypatch, built: list[str]) -> None:
    import sec_xbrl_warehouse.infrastructure.http.egress_lock as egress
    import sec_xbrl_warehouse.infrastructure.http.sec_client as sec_client
    import sec_xbrl_warehouse.infrastructure.http.source_client as source_client
    import sec_xbrl_warehouse.infrastructure.storage.clickhouse as clickhouse
    import sec_xbrl_warehouse.mapping.postgres_repository as mapping_repository

    class _SecClient:
        def __init__(self, *args, **kwargs) -> None:
            built.append("sec-client")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def ensure_egress(self) -> None:
            return None

    class _SourceClient(_SecClient):
        def __init__(self, settings, policy, client=None, egress_lock=None) -> None:
            built.append(f"{policy.code}-client:{egress_lock.name}")
            self.policy = policy

    class _Lock:
        def __init__(self, settings, *, source="sec", **kwargs) -> None:
            self.name = f"xbrl:{source}-egress"
            built.append(f"lock:{source}")

    class _Warehouse:
        def __init__(self, settings) -> None:
            pass

        def close(self) -> None:
            pass

    class _Repository:
        def __init__(self, settings, engine=None) -> None:
            pass

        async def seed_metrics(self, metrics) -> None:
            pass

    monkeypatch.setattr(sec_client, "SecHttpClient", _SecClient)
    monkeypatch.setattr(source_client, "SourceHttpClient", _SourceClient)
    monkeypatch.setattr(egress, "SecEgressLock", _Lock)
    monkeypatch.setattr(egress, "SourceEgressLock", _Lock)
    monkeypatch.setattr(clickhouse, "ClickHouseWarehouse", _Warehouse)
    monkeypatch.setattr(mapping_repository, "PostgresMappingRepository", _Repository)


def test_runtime_services_build_the_esef_adapter(monkeypatch, tmp_path) -> None:
    from sec_xbrl_warehouse.worker import requires_network, runtime_services

    built: list[str] = []
    _patched_runtime(monkeypatch, built)

    class _Control:
        engine = None
        object_store = None

        async def resolve_entity_key(self, scheme, identifier):
            return 10_000_000_001

    control = _Control()

    async def services(job_types):
        settings = Settings(
            _env_file=None,
            env="test",
            sec_user_agent="Test Suite test@example.com",
            data_dir=tmp_path / "data",
            temp_dir=tmp_path / "tmp",
            arelle_offline=True,
            object_store_backend="local",
            enabled_sources=("sec", "esef"),
        )
        async with runtime_services(settings, control, job_types) as found:
            return found["sources"]

    assert requires_network([JobType.FETCH_FILING], "esef") is True
    assert requires_network([JobType.PARSE_FILING], "esef") is False
    registry = asyncio.run(services([JobType.FETCH_FILING]))
    assert registry.codes == ("esef", "sec")
    adapter = registry.adapter("esef")
    assert isinstance(adapter, EsefSourceAdapter)
    assert adapter.entity_keys is control
    assert adapter.http.policy.allowed_hosts == frozenset({"filings.xbrl.org"})
    assert built == ["lock:sec", "sec-client", "lock:esef", "esef-client:xbrl:esef-egress"]

    built.clear()
    parse_only = asyncio.run(services([JobType.PARSE_FILING]))
    assert built == []
    assert parse_only.adapter("esef").http is None


# Parse path (Arelle, offline) ---------------------------------------------------

_EXT_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema" xmlns:xbrli="http://www.xbrl.org/2003/instance"
        targetNamespace="http://www.example.com/2023/ext" elementFormDefault="qualified">
  <import namespace="http://www.xbrl.org/2003/instance"
          schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>
  <element name="Revenue" id="ext_Revenue" type="xbrli:monetaryItemType"
           substitutionGroup="xbrli:item" xbrli:periodType="duration" nillable="true"/>
</schema>
"""

_REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:link="http://www.xbrl.org/2003/linkbase"
      xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
      xmlns:ext="http://www.example.com/2023/ext">
  <head><title>Annual report</title></head>
  <body>
    <div style="display:none">
      <ix:header>
        <ix:references>
          <link:schemaRef xlink:type="simple" xlink:href="{schema}"/>
        </ix:references>
        <ix:resources>
          <xbrli:context id="FY2023">
            <xbrli:entity>
              <xbrli:identifier scheme="http://standards.iso.org/iso/17442">{lei}</xbrli:identifier>
            </xbrli:entity>
            <xbrli:period>
              <xbrli:startDate>2023-01-01</xbrli:startDate>
              <xbrli:endDate>2023-12-31</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
          <xbrli:unit id="EUR"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
        </ix:resources>
      </ix:header>
    </div>
    <p>Revenue: <ix:nonFraction name="ext:Revenue" contextRef="FY2023" unitRef="EUR"
      decimals="0">1234</ix:nonFraction></p>
  </body>
</html>
"""

_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">
  <rewriteURI uriStartString="http://www.example.com/2023/" rewritePrefix="../www.example.com/2023/"/>
</catalog>
"""


def _report_package(schema: str) -> bytes:
    root = f"{LEI_A}-2023-12-31-ESEF-GB-0"
    return _zip(
        {
            f"{root}/META-INF/reportPackage.json": '{"documentInfo": {"documentType": '
            '"https://xbrl.org/report-package/2023/xbri"}}',
            f"{root}/META-INF/catalog.xml": _CATALOG,
            f"{root}/www.example.com/2023/ext.xsd": _EXT_XSD,
            f"{root}/reports/{LEI_A}-2023-12-31-en.xhtml": _REPORT.format(schema=schema, lei=LEI_A),
        }
    )


@pytest.mark.arelle
@pytest.mark.parametrize(
    "schema",
    ["http://www.example.com/2023/ext.xsd", "../www.example.com/2023/ext.xsd"],
    ids=["catalog-remapped", "relative"],
)
def test_parse_extracts_a_stored_report_package_offline(tmp_path, schema) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    adapter, store, _, filing = _download(tmp_path, _report_package(schema))
    manifest = _run(adapter.download_package(filing))
    # No registry file: no required prefixes, so offline Arelle starts without pins
    # and can't reach any network host.
    settings = _settings(
        tmp_path,
        config_dir=tmp_path / "no-config",
        arelle_offline=True,
        arelle_cache_dir=tmp_path / "arelle-cache",
        arelle_taxonomy_packages_dir=tmp_path / "packages",
        arelle_child_max_tasks=1,
    )
    entrypoint = FilingPackageDownloader(settings, None, store).materialize_package(
        manifest, tmp_path / "staging"
    )
    extractor = ArelleExtractor(settings)
    try:
        bundle = extractor.extract(entrypoint, filing)
    finally:
        extractor.close()
    revenue = [fact for fact in bundle.facts if fact.concept_name == "Revenue"]
    assert len(revenue) == 1
    assert revenue[0].entity_scheme == LEI_SCHEME and revenue[0].entity_identifier == LEI_A
    assert str(revenue[0].value_decimal) == "1234"
    focused, _ = adapter.fiscal_focus(bundle)
    assert (focused.filing.fiscal_year, focused.filing.fiscal_period) == (2023, "FY")


def test_report_package_catalog_cannot_escape_or_override_pins(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import (
        _register_report_package,
        report_package_root,
    )

    root = tmp_path / "pkg"
    (root / "META-INF").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "reports" / "r.xhtml").write_text("<html/>")
    assert report_package_root(root / "reports" / "r.xhtml") == root
    assert report_package_root(tmp_path / "r.htm") is None

    def catalog(prefix: str, rewrite: str) -> None:
        (root / "META-INF" / "catalog.xml").write_text(
            '<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">'
            f'<rewriteURI uriStartString="{prefix}" rewritePrefix="{rewrite}"/></catalog>'
        )

    config = {"remappings": {"https://xbrl.ifrs.org/taxonomy/2023-03-23/": "/pinned/"}}
    catalog("http://www.example.com/2023/", "../ext/")
    _register_report_package(config, root)
    assert (
        config["remappings"]["https://www.example.com/2023/"] == str((root / "ext").resolve()) + "/"
    )

    catalog("http://www.example.com/x/", "../../outside/")
    with pytest.raises(XbrlExtractionError, match="escapes"):
        _register_report_package(config, root)
    catalog("http://xbrl.ifrs.org/taxonomy/2023-03-23/full_ifrs/", "../ifrs/")
    with pytest.raises(XbrlExtractionError, match="pinned"):
        _register_report_package(config, root)
