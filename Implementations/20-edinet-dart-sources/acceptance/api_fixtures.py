"""Canned EDINET API v2 and OpenDART responses for the 20-edinet-dart-sources tests."""

from __future__ import annotations

import io
import zipfile

import httpx

EDINET_HOST = "api.edinet-fsa.go.jp"
DART_HOST = "opendart.fss.or.kr"
EDINET_KEY = "edinet-secret-key-0123456789"
DART_KEY = "d" * 40


def zipped(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def edinet_package(doc_id: str) -> bytes:
    return zipped(
        {
            "XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_2024-03-31_01_2024-06-25.xbrl": b"<xbrl/>",
            "XBRL/PublicDoc/0101010_honbun_jpcrp030000-asr-001_E02144-000_2024-03-31_01_2024-06-25_ixbrl.htm": b"<html/>",
            "XBRL/PublicDoc/manifest_PublicDoc.xml": b"<manifest/>",
            "XBRL/AuditDoc/jpaud-aar-cn-001_E02144-000_2024-03-31_01_2024-06-25.xbrl": b"<xbrl/>",
        }
    )


def edinet_doc(doc_id: str, *, code: str = "E02144", doc_type: str = "120", xbrl: str = "1",
               withdrawn: str = "0", submitted: str = "2024-06-25 15:00") -> dict:
    return {
        "seqNumber": 1, "docID": doc_id, "edinetCode": code, "secCode": "72030", "JCN": "1180301018771",
        "filerName": "トヨタ自動車株式会社", "docTypeCode": doc_type, "periodStart": "2023-04-01",
        "periodEnd": "2024-03-31", "submitDateTime": submitted, "docDescription": "有価証券報告書",
        "xbrlFlag": xbrl, "withdrawalStatus": withdrawn,
    }


class FakeEdinet:
    def __init__(self, days: dict[str, list[dict]], packages: dict[str, bytes] | None = None,
                 status: str = "200") -> None:
        self.days = days
        self.packages = packages or {}
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == EDINET_HOST
        if request.url.path == "/api/v2/documents.json":
            if self.status != "200":
                return httpx.Response(200, json={"metadata": {"status": self.status, "message": "Access denied due to invalid subscription key."}})
            date = request.url.params["date"]
            results = self.days.get(date, [])
            return httpx.Response(200, json={"metadata": {"status": "200", "resultset": {"count": len(results)}}, "results": results})
        doc_id = request.url.path.rsplit("/", 1)[-1]
        if doc_id in self.packages:
            return httpx.Response(200, content=self.packages[doc_id], headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, json={"metadata": {"status": "404", "message": "Not Found"}})


def dart_item(rcept_no: str, report: str, *, corp_code: str = "00126380", date: str = "20240312") -> dict:
    return {
        "corp_code": corp_code, "corp_name": "삼성전자", "stock_code": "005930", "corp_cls": "Y",
        "report_nm": report, "rcept_no": rcept_no, "flr_nm": "삼성전자", "rcept_dt": date, "rm": "연",
    }


class FakeDart:
    def __init__(self, pages: list[list[dict]], packages: dict[str, bytes] | None = None,
                 status: str = "000") -> None:
        self.pages = pages
        self.packages = packages or {}
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == DART_HOST
        if request.url.path == "/api/list.json":
            if self.status != "000":
                return httpx.Response(200, json={"status": self.status, "message": "status"})
            number = int(request.url.params.get("page_no", "1"))
            return httpx.Response(200, json={
                "status": "000", "message": "정상", "page_no": number, "page_count": 100,
                "total_count": sum(len(page) for page in self.pages), "total_page": len(self.pages),
                "list": self.pages[number - 1],
            })
        if request.url.path == "/api/fnlttXbrl.xml":
            rcept_no = request.url.params["rcept_no"]
            if rcept_no in self.packages:
                return httpx.Response(200, content=self.packages[rcept_no], headers={"content-type": "application/x-msdownload"})
            return httpx.Response(200, content=b"<result><status>013</status><message>no data</message></result>",
                                  headers={"content-type": "application/xml"})
        return httpx.Response(404)


def dart_package() -> bytes:
    return zipped({"entity00126380_2023-12-31.xbrl": b"<xbrl/>", "entity00126380_2023-12-31.xsd": b"<schema/>"})


class EntityKeys:
    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], int] = {}

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        return self.keys.setdefault((scheme, identifier), 10_000_000_000 + len(self.keys) + 1)
