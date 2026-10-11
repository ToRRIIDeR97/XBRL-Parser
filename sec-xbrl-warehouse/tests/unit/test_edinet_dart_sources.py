"""EDINET and DART adapters: discovery, packages, fiscal focus, secrets, policies, wiring."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import structlog

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import FactValueKind, JobType, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    ConfigurationError,
    SourceAccessError,
    SourceRateLimited,
    SourceRetryDeferred,
    UnsupportedSourceError,
)
from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, redact_params
from sec_xbrl_warehouse.sources.dart import (
    DART_POLICY,
    DART_SCHEME,
    DART_TAXONOMY_PREFIXES,
    DartSourceAdapter,
    dart_policy,
    report_code,
)
from sec_xbrl_warehouse.sources.edinet import (
    EDINET_POLICY,
    EDINET_SCHEME,
    EDINET_TAXONOMY_PREFIXES,
    EdinetSourceAdapter,
    edinet_policy,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
EDINET_KEY = "edinet-unit-key-5f0c9e1a7b"
DART_KEY = "k" * 39 + "Z"
_ZIP_TIME = (2024, 1, 1, 0, 0, 0)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=_ZIP_TIME), data)
    return buffer.getvalue()


def _settings(tmp_path: Path, **values) -> Settings:
    values.setdefault("edinet_api_key", EDINET_KEY)
    values.setdefault("dart_api_key", DART_KEY)
    values.setdefault("sec_max_retries", 0)
    return Settings(
        _env_file=None,
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        **values,
    )


class _Keys:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        self.calls.append((scheme, identifier))
        return 10_000_000_000 + len(set(self.calls))


def _run(coro):
    return asyncio.run(coro)


def _request(**values) -> DiscoveryRequest:
    return DiscoveryRequest(**values)


# EDINET fakes -------------------------------------------------------------------


def _doc(doc_id: str, **values) -> dict:
    record = {
        "seqNumber": 1,
        "docID": doc_id,
        "edinetCode": "E02144",
        "filerName": "Toyota Motor Corporation",
        "docTypeCode": "120",
        "periodStart": "2023-04-01",
        "periodEnd": "2024-03-31",
        "submitDateTime": "2024-06-25 15:00",
        "xbrlFlag": "1",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "disclosureStatus": "0",
    }
    record.update(values)
    return record


def _edinet_package(*names: str) -> bytes:
    return _zip({name: b"<xbrl/>" for name in names})


PUBLIC = "XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_2024-03-31_01_2024-06-25.xbrl"
AUDIT = "XBRL/AuditDoc/jpaud-aar-cn-001_E02144-000_2024-03-31_01_2024-06-25.xbrl"


class _Edinet:
    def __init__(self, days: dict[str, list[dict]] | None = None, **packages: object) -> None:
        self.days = days or {}
        self.packages = packages
        self.requests: list[httpx.Request] = []
        self.list_body: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.edinet-fsa.go.jp"
        assert request.url.params["Subscription-Key"] == EDINET_KEY
        if request.url.path == "/api/v2/documents.json":
            if self.list_body is not None:
                return httpx.Response(200, json=self.list_body)
            results = self.days.get(request.url.params["date"], [])
            return httpx.Response(
                200,
                json={
                    "metadata": {"status": "200", "message": "OK"},
                    "results": results,
                },
            )
        package = self.packages.get(request.url.path.rsplit("/", 1)[-1])
        if isinstance(package, httpx.Response):
            return package
        if isinstance(package, bytes):
            return httpx.Response(200, content=package)
        return httpx.Response(
            200, json={"metadata": {"title": "x", "status": "404", "message": "Not Found"}}
        )


def _edinet(tmp_path: Path, fake: _Edinet, keys: _Keys | None = None, **values):
    settings = _settings(tmp_path, **values)
    policy = replace(EDINET_POLICY, max_requests_per_second=1000.0)
    http = SourceHttpClient(
        settings, policy, client=httpx.AsyncClient(transport=httpx.MockTransport(fake))
    )
    store = LocalObjectStore(tmp_path / "objects", bucket="test")
    return EdinetSourceAdapter(settings, http, store, keys or _Keys()), store


def _edinet_filing(doc_id: str = "S100TR7I", form: str = "120") -> FilingRef:
    return FilingRef(
        accession=f"edinet-{doc_id}",
        cik=10_000_000_001,
        company_name="Toyota Motor Corporation",
        form=form,
        filed_date=date(2024, 6, 25),
        report_date=date(2024, 3, 31),
        archive_directory_url=f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}",
    )


# EDINET discovery ----------------------------------------------------------------


def test_edinet_missing_key_refuses_before_any_request(tmp_path) -> None:
    fake = _Edinet()
    adapter, _ = _edinet(tmp_path, fake, edinet_api_key="")
    assert adapter.settings.edinet_api_key is None
    window = _request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25))
    with pytest.raises(ConfigurationError, match="XBRL_EDINET_API_KEY"):
        _run(adapter.discover(window))
    with pytest.raises(ConfigurationError):
        _run(adapter.download_package(_edinet_filing()))
    assert fake.requests == []


@pytest.mark.parametrize(
    "window",
    [
        {"filed_from": date(2024, 6, 25)},
        {"filed_to": date(2024, 6, 25)},
        {"filed_from": date(2024, 6, 26), "filed_to": date(2024, 6, 25)},
        {"filed_from": date(2024, 6, 1), "filed_to": date(2024, 7, 2)},
        {"filed_from": date(2024, 6, 1), "filed_to": date(2024, 6, 2), "entity": "toyota"},
    ],
)
def test_edinet_window_and_entity_are_checked_first(tmp_path, window) -> None:
    fake = _Edinet()
    adapter, _ = _edinet(tmp_path, fake)
    with pytest.raises(ValueError):
        _run(adapter.discover(_request(**window)))
    assert fake.requests == []


def test_edinet_window_limit_is_inclusive(tmp_path) -> None:
    fake = _Edinet()
    adapter, _ = _edinet(tmp_path, fake, edinet_max_days=3)
    _run(adapter.discover(_request(filed_from=date(2024, 6, 1), filed_to=date(2024, 6, 3))))
    assert [r.url.params["date"] for r in fake.requests] == [
        "2024-06-01",
        "2024-06-02",
        "2024-06-03",
    ]


def test_edinet_filters_and_mapping(tmp_path) -> None:
    fake = _Edinet(
        {
            "2024-06-25": [
                _doc("S100AAAA"),
                _doc("S100BBBB", withdrawalStatus="1"),
                _doc("S100CCCC", withdrawalStatus="2"),
                _doc("S100DDDD", disclosureStatus="2"),
                _doc("S100EEEE", docInfoEditStatus="1"),
                _doc("S100FFFF", docTypeCode="130", periodEnd="2023-03-31"),
                _doc("S100GGGG", docTypeCode="180"),
                _doc("S100HHHH", edinetCode="E99999", filerName="Other"),
                _doc("bad id"),
                _doc("S100AAAA"),
            ],
        }
    )
    keys = _Keys()
    adapter, _ = _edinet(tmp_path, fake, keys)
    result = _run(
        adapter.discover(_request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25)))
    )
    assert [(f.accession, f.form, f.is_amendment) for f in result.filings] == [
        ("edinet-S100AAAA", "120", False),
        ("edinet-S100FFFF", "130", True),
        ("edinet-S100HHHH", "120", False),
    ]
    assert result.filings[1].report_date == date(2023, 3, 31)
    assert set(keys.calls) == {(EDINET_SCHEME, "E02144"), (EDINET_SCHEME, "E99999")}
    assert {company.name for company in result.companies} == {"Toyota Motor Corporation", "Other"}
    assert result.filings[0].archive_directory_url.endswith("/api/v2/documents/S100AAAA")

    only = _run(
        adapter.discover(
            _request(
                filed_from=date(2024, 6, 25),
                filed_to=date(2024, 6, 25),
                entity=" e99999 ",
                forms=("120", "130"),
            )
        )
    )
    assert [f.accession for f in only.filings] == ["edinet-S100HHHH"]
    amended = _run(
        adapter.discover(
            _request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25), forms=("130",))
        )
    )
    assert [f.accession for f in amended.filings] == ["edinet-S100FFFF"]


def test_edinet_error_shapes(tmp_path) -> None:
    window = _request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25))
    fake = _Edinet()
    fake.list_body = {"StatusCode": 401, "message": "Access denied due to invalid key."}
    adapter, _ = _edinet(tmp_path, fake)
    with pytest.raises(SourceAccessError, match="401") as raised:
        _run(adapter.discover(window))
    assert not isinstance(raised.value, SourceRateLimited)

    fake.list_body = {"StatusCode": 429, "message": "Too Many Requests"}
    with pytest.raises(SourceRateLimited) as limited:
        _run(adapter.discover(window))
    from sec_xbrl_warehouse.worker import is_retryable_exception

    assert is_retryable_exception(limited.value)
    assert limited.value.retry_at > datetime.now(UTC)

    fake.list_body = {"metadata": {"title": "x", "status": "400", "message": "Bad Request"}}
    with pytest.raises(SourceAccessError, match="400"):
        _run(adapter.discover(window))


# EDINET packages -------------------------------------------------------------------


def test_edinet_json_body_is_an_access_error_and_stores_nothing(tmp_path) -> None:
    fake = _Edinet(
        S100TR7I=httpx.Response(200, json={"StatusCode": 401, "message": "Access denied"})
    )
    adapter, store = _edinet(tmp_path, fake)
    with pytest.raises(SourceAccessError) as raised:
        _run(adapter.download_package(_edinet_filing()))
    assert EDINET_KEY not in str(raised.value)
    assert list(store.list_objects("")) == []

    missing, store = _edinet(tmp_path / "m", _Edinet())
    with pytest.raises(SourceAccessError, match="404"):
        _run(missing.download_package(_edinet_filing()))
    assert list(store.list_objects("")) == []


@pytest.mark.parametrize(
    "package",
    [
        _edinet_package(AUDIT, "XBRL/PublicDoc/manifest_PublicDoc.xml"),
        _edinet_package(PUBLIC, "XBRL/PublicDoc/other.xbrl"),
        _edinet_package("XBRL/PublicDoc/sub/nested.xbrl"),
        _edinet_package(PUBLIC, "../escape.xbrl"),
    ],
    ids=["no-instance", "two-instances", "nested-only", "traversal"],
)
def test_edinet_invalid_packages_are_rejected_and_quarantined(tmp_path, package) -> None:
    adapter, store = _edinet(tmp_path, _Edinet(S100TR7I=package))
    with pytest.raises(ArtifactIntegrityError):
        _run(adapter.download_package(_edinet_filing()))
    keys = [key for key, _ in store.list_objects("")]
    assert keys and all(
        key.startswith("quarantine/edinet/filings/edinet-S100TR7I/") for key in keys
    )


def test_edinet_download_is_idempotent_and_keyless(tmp_path) -> None:
    package = _edinet_package(PUBLIC, AUDIT, "XBRL/PublicDoc/0101010_honbun_ixbrl.htm")
    fake = _Edinet(S100TR7I=package)
    adapter, store = _edinet(tmp_path, fake)
    first = _run(adapter.download_package(_edinet_filing()))
    second = _run(adapter.download_package(_edinet_filing()))
    assert [m.name for m in first.members if m.is_xbrl_entrypoint] == [PUBLIC]
    assert second.manifest_object_key == first.manifest_object_key
    assert second.acquired_at == first.acquired_at
    assert {m.source_url for m in first.members} == {
        "https://api.edinet-fsa.go.jp/api/v2/documents/S100TR7I?type=1"
    }
    for key, _ in store.list_objects(""):
        assert EDINET_KEY not in key
        assert EDINET_KEY.encode() not in (store.bucket_root / key).read_bytes()


def test_edinet_refuses_other_sources(tmp_path) -> None:
    adapter, _ = _edinet(tmp_path, _Edinet())
    with pytest.raises(UnsupportedSourceError):
        _run(adapter.download_package(_dart_filing()))


# DART fakes ------------------------------------------------------------------------


def _item(rcept_no: str, report: str, **values) -> dict:
    item = {
        "corp_code": "00126380",
        "corp_name": "Samsung Electronics",
        "stock_code": "005930",
        "corp_cls": "Y",
        "report_nm": report,
        "rcept_no": rcept_no,
        "flr_nm": "Samsung Electronics",
        "rcept_dt": "20240312",
        "rm": "",
    }
    item.update(values)
    return item


class _Dart:
    def __init__(self, pages: list[list[dict]] | None = None, **packages: object) -> None:
        self.pages = pages or [[]]
        self.packages = packages
        self.status = "000"
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "opendart.fss.or.kr"
        assert request.url.params["crtfc_key"] == DART_KEY
        if request.url.path == "/api/list.json":
            if self.status != "000":
                return httpx.Response(200, json={"status": self.status, "message": "status"})
            number = int(request.url.params["page_no"])
            return httpx.Response(
                200,
                json={
                    "status": "000",
                    "message": "OK",
                    "page_no": number,
                    "total_page": len(self.pages),
                    "list": self.pages[number - 1],
                },
            )
        package = self.packages.get(request.url.params["rcept_no"])
        if isinstance(package, httpx.Response):
            return package
        if isinstance(package, bytes):
            return httpx.Response(200, content=package)
        return httpx.Response(
            200, content=b"<result><status>014</status><message>no file</message></result>"
        )


def _dart(tmp_path: Path, fake: _Dart, keys: _Keys | None = None, **values):
    settings = _settings(tmp_path, **values)
    policy = replace(DART_POLICY, max_requests_per_second=1000.0)
    http = SourceHttpClient(
        settings, policy, client=httpx.AsyncClient(transport=httpx.MockTransport(fake))
    )
    store = LocalObjectStore(tmp_path / "objects", bucket="test")
    return DartSourceAdapter(settings, http, store, keys or _Keys()), store


def _dart_filing(rcept_no: str = "20240312000736", form: str = "11011") -> FilingRef:
    return FilingRef(
        accession=f"dart-{rcept_no}",
        cik=10_000_000_002,
        company_name="Samsung Electronics",
        form=form,
        filed_date=date(2024, 3, 12),
        report_date=date(2023, 12, 31),
        archive_directory_url="https://opendart.fss.or.kr/api/fnlttXbrl.xml",
    )


WINDOW = {"filed_from": date(2024, 1, 1), "filed_to": date(2024, 3, 31)}


# DART discovery --------------------------------------------------------------------


def test_report_names_map_to_report_codes() -> None:
    assert report_code("사업보고서 (2023.12)") == ("11011", date(2023, 12, 31), False)
    assert report_code("반기보고서 (2024.06)") == ("11012", date(2024, 6, 30), False)
    assert report_code("분기보고서 (2024.03)") == ("11013", date(2024, 3, 31), False)
    assert report_code("분기보고서 (2024.09)") == ("11014", date(2024, 9, 30), False)
    assert report_code("[기재정정]사업보고서 (2023.12)") == ("11011", date(2023, 12, 31), True)
    assert report_code("[첨부정정]반기보고서 (2024.02)") == ("11012", date(2024, 2, 29), True)
    assert report_code("[첨부추가]사업보고서 (2023.12)") == ("11011", date(2023, 12, 31), False)
    # A June quarter (a March year-end company's first quarter) has no inferable code.
    assert report_code("분기보고서 (2024.06)") is None
    assert report_code("주요사항보고서(자기주식취득결정)") is None
    assert report_code("사업보고서 (2023.13)") is None


def test_dart_missing_key_and_bad_requests_send_nothing(tmp_path) -> None:
    fake = _Dart()
    adapter, _ = _dart(tmp_path, fake, dart_api_key=None)
    with pytest.raises(ConfigurationError, match="XBRL_DART_API_KEY"):
        _run(adapter.discover(_request(**WINDOW)))
    with pytest.raises(ConfigurationError):
        _run(adapter.download_package(_dart_filing()))
    keyed, _ = _dart(tmp_path / "k", fake)
    for bad in (
        {"filed_from": date(2024, 1, 1)},
        {**WINDOW, "entity": "126380"},
        {"filed_from": date(2024, 2, 1), "filed_to": date(2024, 1, 1)},
    ):
        with pytest.raises(ValueError):
            _run(keyed.discover(_request(**bad)))
    assert fake.requests == []


def test_dart_pages_filters_and_maps(tmp_path) -> None:
    fake = _Dart(
        [
            [
                _item("20240312000736", "사업보고서 (2023.12)"),
                _item("20240312000737", "사업보고서 (2023.12)", rm="철"),
                _item("20240312000738", "분기보고서 (2024.06)"),
            ],
            [
                _item("20241114000123", "분기보고서 (2024.09)", rcept_dt="20241114"),
                _item("2024111400012X", "사업보고서 (2023.12)"),
                _item("20240315000001", "사업보고서 (2023.12)", corp_code="00164779"),
            ],
        ]
    )
    keys = _Keys()
    adapter, _ = _dart(tmp_path, fake, keys, dart_page_count=3)
    result = _run(adapter.discover(_request(**WINDOW)))
    assert [(f.accession, f.form, f.report_date) for f in result.filings] == [
        ("dart-20240312000736", "11011", date(2023, 12, 31)),
        ("dart-20241114000123", "11014", date(2024, 9, 30)),
        ("dart-20240315000001", "11011", date(2023, 12, 31)),
    ]
    assert [r.url.params["page_no"] for r in fake.requests] == ["1", "2"]
    assert {r.url.params["page_count"] for r in fake.requests} == {"3"}
    assert "corp_code" not in fake.requests[0].url.params
    assert set(keys.calls) == {(DART_SCHEME, "00126380"), (DART_SCHEME, "00164779")}
    assert len(result.companies) == 2

    fake.requests.clear()
    one = _run(adapter.discover(_request(**WINDOW, entity="00164779", forms=("11011",))))
    assert [f.accession for f in one.filings] == ["dart-20240315000001"]
    assert {r.url.params["corp_code"] for r in fake.requests} == {"00164779"}
    quarter = _run(adapter.discover(_request(**WINDOW, forms=("11014",))))
    assert [f.accession for f in quarter.filings] == ["dart-20241114000123"]


def test_search_windows_split_into_three_calendar_months() -> None:
    from datetime import timedelta
    from itertools import pairwise

    from sec_xbrl_warehouse.sources.dart import search_windows

    def ymd(windows):
        return [(a.strftime("%Y%m%d"), b.strftime("%Y%m%d")) for a, b in windows]

    assert ymd(search_windows(date(2024, 1, 15), date(2024, 8, 10), False)) == [
        ("20240115", "20240414"),
        ("20240415", "20240714"),
        ("20240715", "20240810"),
    ]
    assert ymd(search_windows(date(2024, 3, 1), date(2024, 5, 31), False)) == [
        ("20240301", "20240531")
    ]
    # Month-end starts: a day the target month lacks becomes its last day.
    assert ymd(search_windows(date(2023, 11, 30), date(2024, 6, 1), False)) == [
        ("20231130", "20240228"),
        ("20240229", "20240528"),
        ("20240529", "20240601"),
    ]
    assert ymd(search_windows(date(2024, 1, 31), date(2024, 7, 31), False)) == [
        ("20240131", "20240429"),
        ("20240430", "20240729"),
        ("20240730", "20240731"),
    ]
    assert search_windows(date(2024, 1, 1), date(2025, 12, 31), True) == [
        (date(2024, 1, 1), date(2025, 12, 31))
    ]
    assert search_windows(date(2024, 5, 5), date(2024, 5, 5), False) == [
        (date(2024, 5, 5), date(2024, 5, 5))
    ]
    # Every start day over several years: windows tile the range exactly.
    start = date(2023, 1, 1)
    while start < date(2025, 1, 1):
        end = start + timedelta(days=400)
        windows = search_windows(start, end, False)
        assert windows[0][0] == start and windows[-1][1] == end
        for previous, following in pairwise(windows):
            assert following[0] == previous[1] + timedelta(days=1)
        # Three calendar months at most (92 days is the longest such span).
        assert all(a <= b and (b - a).days < 92 for a, b in windows)
        start += timedelta(days=1)


def test_dart_long_window_is_searched_in_order_and_paged(tmp_path) -> None:
    pages: dict[str, list[list[dict]]] = {
        "20240115": [
            [_item("20240212000001", "사업보고서 (2023.12)", rcept_dt="20240212")],
            [_item("20240313000002", "사업보고서 (2023.12)", rcept_dt="20240313")],
        ],
        "20240415": [],  # 013: no data in this window
        "20240715": [
            [_item("20240801000003", "반기보고서 (2024.06)", rcept_dt="20240801")],
        ],
    }
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        chunk = pages[request.url.params["bgn_de"]]
        if not chunk:
            return httpx.Response(200, json={"status": "013", "message": "none"})
        number = int(request.url.params["page_no"])
        return httpx.Response(
            200,
            json={
                "status": "000",
                "total_page": len(chunk),
                "list": chunk[number - 1],
            },
        )

    adapter, _ = _dart(tmp_path, handler)  # type: ignore[arg-type]
    window = {"filed_from": date(2024, 1, 15), "filed_to": date(2024, 8, 10)}
    result = _run(adapter.discover(_request(**window)))
    assert [f.accession for f in result.filings] == [
        "dart-20240212000001",
        "dart-20240313000002",
        "dart-20240801000003",
    ]
    assert [
        (r.url.params["bgn_de"], r.url.params["end_de"], r.url.params["page_no"]) for r in sent
    ] == [
        ("20240115", "20240414", "1"),
        ("20240115", "20240414", "2"),
        ("20240415", "20240714", "1"),
        ("20240715", "20240810", "1"),
    ]

    sent.clear()
    pages["20240115"] = [[_item("20240212000001", "사업보고서 (2023.12)", corp_code="00164779")]]
    _run(adapter.discover(_request(**window, entity="00164779")))
    assert [(r.url.params["bgn_de"], r.url.params["end_de"]) for r in sent] == [
        ("20240115", "20240810")
    ]


def test_dart_key_problems_are_access_errors_without_the_key(tmp_path) -> None:
    fake = _Dart()
    adapter, _ = _dart(tmp_path, fake)
    for status in ("010", "011", "012", "100", "800", "901"):
        fake.status = status
        with pytest.raises(SourceAccessError) as raised:
            _run(adapter.discover(_request(**WINDOW)))
        assert not isinstance(raised.value, SourceRateLimited)
        assert status in str(raised.value) and DART_KEY not in str(raised.value)


# DART packages ---------------------------------------------------------------------


def test_dart_download_errors(tmp_path) -> None:
    limit = httpx.Response(
        200, content=b"<result><status>020</status><message>limit</message></result>"
    )
    json_limit = httpx.Response(200, json={"status": "020", "message": "limit"})
    fake = _Dart(**{"20240312000736": limit, "20240312000737": json_limit})
    adapter, store = _dart(tmp_path, fake)
    with pytest.raises(SourceRateLimited) as raised:
        _run(adapter.download_package(_dart_filing()))
    assert raised.value.retry_at > datetime.now(UTC)
    assert isinstance(raised.value, SourceRetryDeferred)
    with pytest.raises(SourceRateLimited):
        _run(adapter.download_package(_dart_filing("20240312000737")))
    with pytest.raises(SourceAccessError, match="014"):
        _run(adapter.download_package(_dart_filing("20240312000738")))
    assert list(store.list_objects("")) == []
    with pytest.raises(ValueError):
        _run(adapter.download_package(_dart_filing(form="11015")))


@pytest.mark.parametrize(
    "entries",
    [
        {"entity00126380_2023-12-31.xsd": b"<schema/>"},
        {"a.xbrl": b"<xbrl/>", "b.xbrl": b"<xbrl/>"},
    ],
    ids=["no-instance", "two-instances"],
)
def test_dart_invalid_packages_are_quarantined(tmp_path, entries) -> None:
    adapter, store = _dart(tmp_path, _Dart(**{"20240312000736": _zip(entries)}))
    with pytest.raises(ArtifactIntegrityError):
        _run(adapter.download_package(_dart_filing()))
    keys = [key for key, _ in store.list_objects("")]
    assert keys and all(key.startswith("quarantine/dart/") for key in keys)


# Fiscal focus ----------------------------------------------------------------------


def _jpdei(name: str, value: str, *, dimensional: bool = False) -> XbrlFact:
    from sec_xbrl_warehouse.domain.xbrl import DimensionMember

    is_date = name.endswith("DateDEI")
    return XbrlFact(
        fact_id=f"{name}-{value}-{dimensional}",
        source_document="a.xbrl",
        concept_qname=f"jpdei_cor:{name}",
        concept_namespace="http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/jpdei_cor",
        concept_name=name,
        context_id="FilingDateInstant",
        context_hash="h",
        entity_scheme=EDINET_SCHEME,
        entity_identifier="E02144-000",
        period_kind=PeriodKind.INSTANT,
        period_instant=date(2024, 6, 25),
        dimensions=(
            (DimensionMember(axis_namespace="x", axis_name="A", member_name="M"),)
            if dimensional
            else ()
        ),
        dimension_hash="d" if dimensional else "",
        value_kind=FactValueKind.DATE if is_date else FactValueKind.TEXT,
        raw_value=value,
        value_date=date.fromisoformat(value) if is_date else None,
        value_text=None if is_date else value,
        duplicate_set_hash=f"g-{name}-{value}",
    )


def _bundle(filing: FilingRef, *facts: XbrlFact) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="a.xbrl",
            document_type="xbrl",
            taxonomy_namespaces=(),
            source_documents=("a.xbrl",),
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


def test_edinet_fiscal_focus_from_jpdei(tmp_path) -> None:
    adapter, _ = _edinet(tmp_path, _Edinet())
    half = _edinet_filing(form="160").model_copy(update={"report_date": date(2024, 9, 30)})
    enriched, issues = adapter.fiscal_focus(
        _bundle(
            half,
            _jpdei("CurrentFiscalYearEndDateDEI", "2025-03-31"),
            _jpdei("TypeOfCurrentPeriodDEI", "HY"),
            _jpdei("TypeOfCurrentPeriodDEI", "FY", dimensional=True),
        )
    )
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2025, "H1")
    assert issues == ()

    quarter = _edinet_filing(form="140")
    enriched, _ = adapter.fiscal_focus(_bundle(quarter, _jpdei("TypeOfCurrentPeriodDEI", "Q2")))
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (None, "Q2")
    unchanged, _ = adapter.fiscal_focus(_bundle(quarter))
    assert (unchanged.filing.fiscal_year, unchanged.filing.fiscal_period) == (None, None)

    conflict, issues = adapter.fiscal_focus(
        _bundle(
            _edinet_filing(),
            _jpdei("CurrentFiscalYearEndDateDEI", "2024-03-31"),
            _jpdei("CurrentFiscalYearEndDateDEI", "2023-03-31"),
        )
    )
    assert (conflict.filing.fiscal_year, conflict.filing.fiscal_period) == (2024, "FY")
    assert [issue.code for issue in issues] == ["warehouse:fiscalFocusInvalid"]


def test_dart_fiscal_focus_by_report_code(tmp_path) -> None:
    adapter, _ = _dart(tmp_path, _Dart())
    for form, report_date, expected in (
        ("11011", date(2023, 12, 31), (2023, "FY")),
        ("11012", date(2024, 6, 30), (2024, "H1")),
        ("11014", date(2024, 9, 30), (2024, "Q3")),
    ):
        filing = _dart_filing(form=form).model_copy(update={"report_date": report_date})
        enriched, _ = adapter.fiscal_focus(_bundle(filing))
        assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == expected


# Secrets ---------------------------------------------------------------------------


def test_redact_params_masks_every_spelling() -> None:
    text = (
        "GET https://h/x?date=1&Subscription-Key=abc&type=2 "
        "'https://h/y?crtfc_key=def' subscription-key%3Dghi crtfc_key=jkl;"
    )
    masked = redact_params(text, {"Subscription-Key", "crtfc_key"})
    for secret in ("abc", "def", "ghi", "jkl"):
        assert secret not in masked
    assert "date=1" in masked and "type=2" in masked


def test_keys_never_reach_logs_errors_or_stored_files(tmp_path, caplog) -> None:
    """Retries, transport errors, HTTP errors and successes: no record holds a key."""
    caplog.set_level(logging.DEBUG)
    calls = {"n": 0}

    def edinet(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/api/v2/documents.json":
            if calls["n"] == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            if calls["n"] == 2:
                raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
            return httpx.Response(
                200, json={"metadata": {"status": "200"}, "results": [_doc("S100TR7I")]}
            )
        return httpx.Response(200, content=_edinet_package(PUBLIC))

    def dart(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/list.json":
            return httpx.Response(401, json={"status": "010"})
        return httpx.Response(200, content=_zip({"a.xbrl": b"<xbrl/>"}))

    with structlog.testing.capture_logs() as events:
        adapter, store = _edinet(tmp_path, edinet, sec_max_retries=2)  # type: ignore[arg-type]
        window = _request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25))
        found = _run(adapter.discover(window))
        manifest = _run(adapter.download_package(found.filings[0]))
        dart_adapter, dart_store = _dart(tmp_path / "d", dart)  # type: ignore[arg-type]
        with pytest.raises(SourceAccessError) as raised:
            _run(dart_adapter.discover(_request(**WINDOW)))
        dart_manifest = _run(dart_adapter.download_package(_dart_filing()))

    assert any(event["event"] == "edinet_request_retry" for event in events)
    assert any(event["event"] == "edinet_request_exception_retry" for event in events)
    assert any("api.edinet-fsa.go.jp" in record.getMessage() for record in caplog.records)
    texts = [json.dumps(event, default=str) for event in events]
    texts += [record.getMessage() for record in caplog.records]
    error: BaseException | None = raised.value
    while error is not None:
        texts.append(str(error))
        error = error.__cause__ or error.__context__
    texts += [
        manifest.model_dump_json(),
        dart_manifest.model_dump_json(),
        json.dumps(found.filings[0].model_dump(mode="json")),
    ]
    for key, _ in [*store.list_objects(""), *dart_store.list_objects("")]:
        texts.append(key)
    for text in texts:
        assert EDINET_KEY not in text and DART_KEY not in text
    assert "401" in str(raised.value)
    for root in (store.bucket_root, dart_store.bucket_root):
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert EDINET_KEY.encode() not in data and DART_KEY.encode() not in data


def test_interrupted_download_sidecar_has_no_key(tmp_path) -> None:
    async def body():
        yield b"PK"
        raise httpx.ReadError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    settings = _settings(tmp_path)
    client = SourceHttpClient(
        settings, DART_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    destination = tmp_path / "out" / "package.zip"
    url = f"https://opendart.fss.or.kr/api/fnlttXbrl.xml?crtfc_key={DART_KEY}&rcept_no=1"
    with pytest.raises(httpx.ReadError):
        _run(client.download(url, destination))
    sidecars = list(destination.parent.glob("*.json"))
    assert sidecars
    for sidecar in sidecars:
        text = sidecar.read_text()
        assert DART_KEY not in text and "crtfc_key=[REDACTED]" in text


def test_parse_worker_and_api_hold_no_regulator_keys() -> None:
    import yaml

    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    parse = services["worker-parse"]["environment"]
    assert parse["XBRL_EDINET_API_KEY"] == "" and parse["XBRL_DART_API_KEY"] == ""
    assert "env_file" not in services["api"]
    assert not any("EDINET" in name or "DART" in name for name in services["api"]["environment"])


def test_adapter_guard_masks_a_key_echoed_by_any_failure() -> None:
    from sec_xbrl_warehouse.sources.keyed import redacted

    async def chained() -> None:
        try:
            raise ValueError(f"inner {EDINET_KEY}")
        except ValueError as exc:
            raise RuntimeError("outer") from exc

    async def echoed() -> None:
        raise SourceAccessError(f"refused https://x/?k={EDINET_KEY}")

    async def unrelated() -> None:
        raise SourceAccessError("plain failure")

    with pytest.raises(RuntimeError) as raised:
        _run(redacted(chained(), EDINET_KEY, EDINET_POLICY))
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    with pytest.raises(SourceAccessError, match=r"\[REDACTED\]") as masked:
        _run(redacted(echoed(), EDINET_KEY, EDINET_POLICY))
    assert EDINET_KEY not in str(masked.value)
    with pytest.raises(SourceAccessError, match="plain failure"):
        _run(redacted(unrelated(), EDINET_KEY, EDINET_POLICY))


def test_settings_keep_keys_secret(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert EDINET_KEY not in repr(settings) and DART_KEY not in repr(settings)
    assert EDINET_KEY not in settings.model_dump_json()
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import child_settings

    child = child_settings(settings)
    assert child.edinet_api_key is None and child.dart_api_key is None
    parsed = Settings(_env_file=None, env="test", edinet_doc_types=" 120, 130 ,120")
    assert parsed.edinet_doc_types == ("120", "130")
    with pytest.raises(ValueError):
        Settings(_env_file=None, env="test", edinet_doc_types="12A")


# Policies, taxonomies, wiring --------------------------------------------------------


def test_policies_are_disjoint_and_identify_the_operator(tmp_path) -> None:
    from sec_xbrl_warehouse.sources.registry import source_policies

    settings = _settings(tmp_path)
    policies = source_policies(settings)
    assert policies["edinet"].allowed_hosts == frozenset({"api.edinet-fsa.go.jp"})
    assert policies["dart"].allowed_hosts == frozenset({"opendart.fss.or.kr"})
    for code in ("edinet", "dart"):
        policy = policies[code]
        assert policy.max_requests_per_second <= 1 and policy.max_concurrency == 1
        assert policy.headers["User-Agent"] == settings.sec_user_agent
        assert not policy.allowed_hosts & set(settings.allowed_taxonomy_hosts)
    assert edinet_policy(settings).secret_params == frozenset({"Subscription-Key"})
    assert dart_policy(settings).secret_params == frozenset({"crtfc_key"})
    edinet_client = SourceHttpClient(settings, EDINET_POLICY)
    dart_client = SourceHttpClient(settings, DART_POLICY)
    for client, url in (
        (edinet_client, "https://opendart.fss.or.kr/api/list.json"),
        (edinet_client, "https://disclosure.edinet-fsa.go.jp/taxonomy/x.xsd"),
        (dart_client, "https://api.edinet-fsa.go.jp/api/v2/documents.json"),
        (dart_client, "http://opendart.fss.or.kr/api/list.json"),
    ):
        with pytest.raises(SourceAccessError):
            client._validate_url(url)


def test_taxonomy_prefixes_fail_closed_until_pinned(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
        coverage_gaps,
        registered_packages,
    )

    registry = load_yaml(REPO_CONFIG / "taxonomy_packages.yml")
    assert tuple(registry["source_required_prefixes"]["edinet"]) == EDINET_TAXONOMY_PREFIXES
    assert tuple(registry["source_required_prefixes"]["dart"]) == DART_TAXONOMY_PREFIXES
    hosts = set(registry["network_policy"]["allowed_taxonomy_hosts"])
    assert {"disclosure.edinet-fsa.go.jp", "disclosure2dl.edinet-fsa.go.jp"} <= hosts
    for code, prefixes in (("edinet", EDINET_TAXONOMY_PREFIXES), ("dart", DART_TAXONOMY_PREFIXES)):
        enabled = _settings(tmp_path, config_dir=REPO_CONFIG, enabled_sources=("sec", code))
        assert set(prefixes) <= set(coverage_gaps(enabled, registered_packages(enabled)))
    sec_only = _settings(tmp_path, config_dir=REPO_CONFIG)
    gaps = set(coverage_gaps(sec_only, registered_packages(sec_only)))
    assert not gaps & {*EDINET_TAXONOMY_PREFIXES, *DART_TAXONOMY_PREFIXES}


def _runtime_settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        sec_user_agent="Test Suite test@example.com",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        arelle_offline=True,
        object_store_backend="local",
        enabled_sources=("sec", "edinet", "dart"),
        **values,
    )


def test_runtime_services_build_edinet_and_dart(monkeypatch, tmp_path) -> None:
    from test_esef_source import _patched_runtime  # same patched services as ESEF

    from sec_xbrl_warehouse.worker import runtime_services

    built: list[str] = []
    _patched_runtime(monkeypatch, built)

    class _Control:
        engine = None
        object_store = None

        async def resolve_entity_key(self, scheme, identifier):
            return 10_000_000_001

    control = _Control()

    async def services(settings: Settings, job_types):
        async with runtime_services(settings, control, job_types) as found:  # type: ignore[arg-type]
            return found["sources"]

    keyed = _runtime_settings(tmp_path, edinet_api_key=EDINET_KEY, dart_api_key=DART_KEY)
    registry = _run(services(keyed, [JobType.FETCH_FILING]))
    assert registry.codes == ("dart", "edinet", "sec")
    assert isinstance(registry.adapter("edinet"), EdinetSourceAdapter)
    assert isinstance(registry.adapter("dart"), DartSourceAdapter)
    assert registry.adapter("dart").entity_keys is control
    assert built == [
        "lock:sec",
        "sec-client",
        "lock:edinet",
        "edinet-client:xbrl:edinet-egress",
        "lock:dart",
        "dart-client:xbrl:dart-egress",
    ]

    built.clear()
    keyless = _runtime_settings(tmp_path)
    parse_only = _run(services(keyless, [JobType.PARSE_FILING]))
    assert built == []
    assert parse_only.adapter("edinet").http is None
    assert parse_only.adapter("dart").http is None
    with pytest.raises(ConfigurationError, match="XBRL_EDINET_API_KEY"):
        _run(services(keyless, [JobType.FETCH_FILING]))
    assert built == []
