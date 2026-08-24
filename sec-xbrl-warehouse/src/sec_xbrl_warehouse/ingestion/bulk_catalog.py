from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from sec_xbrl_warehouse.domain.enums import DatasetKind
from sec_xbrl_warehouse.domain.models import DatasetArtifact
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient

_YEAR_RE = re.compile(r"(?P<year>20\d{2})")
_QUARTER_RE = re.compile(r"(?i)(?:qtr|q)(?P<quarter>[1-4])")
_MONTH_RE = re.compile(r"(?i)(?P<year>20\d{2})[-_]?m?(?P<month>0[1-9]|1[0-2])")
_SIZE_RE = re.compile(r"(?P<number>[\d,.]+)\s*(?P<unit>KB|MB|GB)", re.IGNORECASE)


class BulkCatalogService:
    """Discover SEC bulk ZIPs from the human-facing catalog pages."""

    def __init__(self, http: SecHttpClient, catalog_urls: dict[DatasetKind, str]) -> None:
        self.http = http
        self.catalog_urls = catalog_urls

    async def sync(self, dataset: DatasetKind) -> tuple[DatasetArtifact, ...]:
        url = self.catalog_urls[dataset]
        html = await self.http.get_text(url)
        return parse_catalog_html(html, page_url=url, dataset=dataset)


def parse_catalog_html(
    html: str,
    *,
    page_url: str,
    dataset: DatasetKind,
) -> tuple[DatasetArtifact, ...]:
    """Parse all ZIP links and derive a stable source period.

    SEC page markup is not an API contract. Therefore:
    - accept both relative and absolute ZIP links;
    - derive the period from file/link text rather than DOM position;
    - de-duplicate by canonical URL;
    - reject links outside `sec.gov`;
    - sort oldest to newest;
    - keep `published_label` for diagnostics.

    A catalog snapshot fixture protects this parser against future markup drift.
    """
    soup = BeautifulSoup(html, "html.parser")
    by_url: dict[str, DatasetArtifact] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if ".zip" not in href.lower():
            continue
        url = urljoin(page_url, href)
        host = urlparse(url).hostname or ""
        if not (host == "sec.gov" or host.endswith(".sec.gov")):
            continue
        filename = PurePosixPath(urlparse(url).path).name
        if not filename.lower().endswith(".zip"):
            continue
        label = " ".join(anchor.stripped_strings) or filename
        container = anchor.find_parent("tr") or anchor.parent
        surrounding = " ".join(container.stripped_strings) if container else label
        period = derive_source_period(filename, label, surrounding)
        size = parse_display_size(surrounding)
        by_url[url] = DatasetArtifact(
            dataset=dataset,
            period=period,
            source_url=url,
            filename=filename,
            published_label=label,
            expected_size_bytes=size,
        )
    return tuple(sorted(by_url.values(), key=lambda item: (item.period, item.source_url)))


def derive_source_period(*texts: str) -> str:
    combined = " ".join(texts)
    month = _MONTH_RE.search(combined)
    if month:
        return f"{month.group('year')}-{month.group('month')}"
    year_match = _YEAR_RE.search(combined)
    quarter_match = _QUARTER_RE.search(combined)
    if year_match and quarter_match:
        return f"{year_match.group('year')}Q{quarter_match.group('quarter')}"
    if year_match:
        return year_match.group("year")
    raise ValueError(f"Cannot derive SEC source period from: {combined!r}")


def parse_display_size(text: str) -> int | None:
    match = _SIZE_RE.search(text)
    if not match:
        return None
    multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[match.group("unit").upper()]
    return int(float(match.group("number").replace(",", "")) * multiplier)


def filter_artifacts_by_year(
    artifacts: Iterable[DatasetArtifact], from_year: int, to_year: int
) -> tuple[DatasetArtifact, ...]:
    if from_year > to_year:
        raise ValueError("from_year must not exceed to_year")
    return tuple(item for item in artifacts if from_year <= int(item.period[:4]) <= to_year)
