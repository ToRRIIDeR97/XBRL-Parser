"""Canned filings.xbrl.org responses for the 19-esef-source tests (no live calls)."""

from __future__ import annotations

import io
import zipfile
from hashlib import sha256

import httpx

HOST = "filings.xbrl.org"
LEI_A = "213800H2PQMIF3OVZY47"
LEI_B = "5493001KJTIIGC8Y1R12"


def report_package(lei: str = LEI_A, period: str = "2022-03-31") -> bytes:
    """A minimal ESEF report package: top folder, META-INF, one inline XBRL report."""
    buffer = io.BytesIO()
    root = f"{lei}-{period}-ESEF-GB-0"
    entries = {
        f"{root}/META-INF/reportPackage.json": '{"documentInfo": {"documentType": "https://xbrl.org/report-package/2023/xbri"}}',
        f"{root}/META-INF/taxonomyPackage.xml": "<taxonomyPackage/>",
        f"{root}/reports/{lei}-{period}-en.xhtml": "<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'/>",
        f"{root}/www.example.com/2022/ext.xsd": "<schema/>",
    }
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in entries.items():
            # Fixed timestamps: the same arguments must always give the same bytes.
            archive.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


def filing_record(fxo_id: str, lei: str, *, added: str, period_end: str, digest: str, record_id: str) -> dict:
    base = f"/{lei}/{period_end}/ESEF/GB/0"
    return {
        "type": "filing",
        "id": record_id,
        "attributes": {
            "fxo_id": fxo_id,
            "country": "GB",
            "date_added": f"{added} 00:00:00",
            "processed": f"{added} 01:02:03.000000",
            "period_end": period_end,
            "sha256": digest,
            "package_url": f"{base}/{fxo_id}.zip",
            "report_url": f"{base}/reports/{lei}-{period_end}-en.xhtml",
            "json_url": f"{base}/{fxo_id}.json",
            "viewer_url": f"{base}/viewer.html",
            "error_count": 0,
            "warning_count": 2,
            "inconsistency_count": 0,
        },
        "relationships": {"entity": {"data": {"type": "entity", "id": f"e-{lei}"}}},
    }


def entity_record(lei: str, name: str) -> dict:
    return {"type": "entity", "id": f"e-{lei}", "attributes": {"identifier": lei, "name": name}}


class FakeFxo:
    """Routes /api/filings pages and package downloads; records every request."""

    def __init__(self, pages: list[dict], packages: dict[str, bytes]) -> None:
        self.pages = pages
        self.packages = packages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == HOST
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
            number = int(request.url.params.get("page[number]", "1"))
            return httpx.Response(200, json=self.pages[number - 1])
        if request.url.path in self.packages:
            return httpx.Response(200, content=self.packages[request.url.path])
        return httpx.Response(404)


def two_page_feed() -> tuple[list[dict], dict[str, bytes]]:
    """Newest first: page 1 has two 2024-04 filings, page 2 one 2024-03 and one 2024-01 filing."""
    package_a = report_package(LEI_A, "2023-12-31")
    package_b = report_package(LEI_B, "2023-12-31")
    fxo_a = f"{LEI_A}-2023-12-31-ESEF-GB-0"
    fxo_b = f"{LEI_B}-2023-12-31-ESEF-GB-0"
    fxo_c = f"{LEI_A}-2022-12-31-ESEF-GB-1"
    fxo_d = f"{LEI_B}-2022-12-31-ESEF-GB-0"
    page1 = {
        "data": [
            filing_record(fxo_a, LEI_A, added="2024-04-10", period_end="2023-12-31", digest=sha256(package_a).hexdigest(), record_id="10"),
            filing_record(fxo_b, LEI_B, added="2024-04-02", period_end="2023-12-31", digest=sha256(package_b).hexdigest(), record_id="9"),
        ],
        "included": [entity_record(LEI_A, "KAINOS GROUP PLC"), entity_record(LEI_B, "Example Holdings SE")],
        "links": {"next": "https://filings.xbrl.org/api/filings?page[number]=2"},
    }
    page2 = {
        "data": [
            filing_record(fxo_c, LEI_A, added="2024-03-15", period_end="2022-12-31", digest="0" * 64, record_id="8"),
            filing_record(fxo_d, LEI_B, added="2024-01-05", period_end="2022-12-31", digest="0" * 64, record_id="7"),
        ],
        "included": [entity_record(LEI_A, "KAINOS GROUP PLC"), entity_record(LEI_B, "Example Holdings SE")],
        "links": {},
    }
    base_a = f"/{LEI_A}/2023-12-31/ESEF/GB/0/{fxo_a}.zip"
    base_b = f"/{LEI_B}/2023-12-31/ESEF/GB/0/{fxo_b}.zip"
    return [page1, page2], {base_a: package_a, base_b: package_b}


class EntityKeys:
    """Stands in for PostgresControlRepository.resolve_entity_key."""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], int] = {}

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        return self.keys.setdefault((scheme, identifier), 10_000_000_000 + len(self.keys) + 1)
