"""The operator file inbox: filings the operator obtained and placed on local disk.

Some regulators publish XBRL without an API whose terms allow automated access
(India's NSE/BSE results and MCA21 company filings). The operator obtains those files
under the source's terms and puts them in `<inbox_dir>/<code>/` with an `index.csv`;
an inbox adapter turns each index row into a `FilingRef` keyed `<code>-<native_id>`
and stores the file as the filing's package. Inbox sources send no network request:
they have no HTTP client, no host policy, and no egress lock.

Everything in the inbox is untrusted input. A row's `file` must resolve, after
symlinks, inside `<inbox_dir>/<code>/`; the bytes are copied (bounded by
`filing_max_bytes`) before they are checked and stored, and the inbox is never
modified.
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any
from urllib.parse import urlsplit

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import (
    Company,
    FilingPackageManifest,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.sources import SEC_CIK_SCHEME, filing_source, is_filing_key
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, UnsupportedSourceError
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file
from sec_xbrl_warehouse.ingestion.filing_downloader import _safe_package_name, _write_member
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.normalization.fiscal_focus import FISCAL_PERIODS
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, DiscoveryResult
from sec_xbrl_warehouse.sources.keyed import EntityKeys, single_entrypoint, store_zip_package

logger = get_logger(__name__)

INDEX_FILE = "index.csv"
REQUIRED_COLUMNS = (
    "native_id",
    "entity_id",
    "company_name",
    "form",
    "filed_date",
    "period_end",
    "file",
)
OPTIONAL_COLUMNS = ("entity_scheme", "fiscal_period", "sha256", "source_url")
# Suffixes of a single-file package; a `.zip` is a package of members.
INSTANCE_SUFFIXES = (".xbrl", ".xml", ".xhtml", ".html")

_XBRL_ROOT = "{http://www.xbrl.org/2003/instance}xbrl"
_INLINE_XBRL_NAMESPACES = frozenset(
    {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_URL = 2048
_MAX_TEXT = 512


@dataclass(frozen=True)
class InboxProfile:
    """One inbox source: its code, entity identifier scheme, and defaults.

    `entity_pattern` is a regular expression every `entity_id` must match;
    `taxonomy_prefixes` must equal `source_required_prefixes.<code>` in
    `config/taxonomy_packages.yml`.
    """

    code: str
    entity_scheme: str
    entity_pattern: str
    default_fiscal_period: str = "FY"
    taxonomy_prefixes: tuple[str, ...] = ()


# NSE (and BSE) financial results, keyed by the issuer's ISIN. The taxonomy prefixes
# are unverified (no filing was read); confirm them with the canary's schema
# references. No pinned package covers them, so with `nse` enabled `taxonomy status`
# reports them `uncovered` (fail closed).
NSE_PROFILE = InboxProfile(
    code="nse",
    entity_scheme="urn:isin",
    entity_pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$",
    default_fiscal_period="FY",
    taxonomy_prefixes=("www.bseindia.com/xbrl/", "www.nseindia.com/xbrl/"),
)
# MCA21 (AOC-4 XBRL) financial statements, keyed by the Corporate Identification
# Number. The taxonomy prefix is unverified, as for NSE.
MCA_PROFILE = InboxProfile(
    code="mca",
    entity_scheme="https://www.mca.gov.in/cin",
    entity_pattern=r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$",
    default_fiscal_period="FY",
    taxonomy_prefixes=("www.mca.gov.in/XBRL/",),
)
INBOX_PROFILES: dict[str, InboxProfile] = {
    profile.code: profile for profile in (NSE_PROFILE, MCA_PROFILE)
}


@dataclass(frozen=True)
class InboxRow:
    """One index row after validation (`problems` empty when the row is usable)."""

    line: int
    native_id: str
    entity_id: str
    entity_scheme: str
    company_name: str
    form: str
    filed_date: date | None
    period_end: date | None
    fiscal_period: str
    file: str
    sha256: str
    source_url: str
    problems: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.native_id or f"line {self.line}"


@dataclass(frozen=True)
class InboxIndex:
    rows: tuple[InboxRow, ...]
    # Header columns the index doesn't know (ignored; a misspelt `sha256` would
    # otherwise silently skip the digest check).
    unknown_columns: tuple[str, ...] = ()


def inbox_root(settings: Settings, code: str) -> Path:
    """`<inbox_dir>/<code>` (unresolved)."""
    return settings.inbox_dir / code


def read_index(
    settings: Settings, profile: InboxProfile, *, allow_duplicates: bool = False
) -> InboxIndex:
    """Read and validate `<inbox_dir>/<code>/index.csv`.

    No folder or no index gives no rows. A missing required column, or a repeated
    `native_id` unless `allow_duplicates`, raises `ValueError` naming it; any other
    problem is recorded on the row (`InboxRow.problems`). The index itself must lie
    inside the folder.
    """
    root = inbox_root(settings, profile.code)
    if not root.is_dir():
        return InboxIndex(rows=())
    try:
        path = contained_path(root, INDEX_FILE)
    except ArtifactIntegrityError as exc:
        raise ValueError(str(exc)) from None
    if not path.is_file():
        return InboxIndex(rows=())
    if path.stat().st_size > settings.response_max_bytes:
        raise ValueError(f"{profile.code}/{INDEX_FILE} exceeds {settings.response_max_bytes} bytes")
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = [(name or "").strip() for name in reader.fieldnames or ()]
            missing = [column for column in REQUIRED_COLUMNS if column not in header]
            if missing:
                raise ValueError(
                    f"{profile.code}/{INDEX_FILE} is missing required column {missing[0]}"
                )
            known = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
            unknown = tuple(name for name in header if name not in known)
            reader.fieldnames = header
            rows = [
                _row(profile, reader.line_num, values)
                for values in reader
                if any((value or "").strip() for value in _cells(values))
            ]
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(
            f"{profile.code}/{INDEX_FILE} is not a readable UTF-8 CSV: {exc}"
        ) from None
    seen: set[str] = set()
    for row in rows:
        if not allow_duplicates and row.native_id and row.native_id in seen:
            raise ValueError(f"{profile.code}/{INDEX_FILE} repeats native_id {row.native_id}")
        seen.add(row.native_id)
    return InboxIndex(rows=tuple(rows), unknown_columns=unknown)


def _cells(values: dict[str | None, Any]) -> Iterator[str]:
    for key, value in values.items():
        if key is None:  # cells beyond the header
            yield from (str(item) for item in value or ())
        elif isinstance(value, str):
            yield value


def _row(profile: InboxProfile, line: int, values: dict[str | None, Any]) -> InboxRow:
    def text(column: str) -> str:
        value = values.get(column)
        return value.strip() if isinstance(value, str) else ""

    problems: list[str] = []
    native_id = text("native_id")
    if not native_id:
        problems.append("native_id is empty")
    elif not is_filing_key(f"{profile.code}-{native_id}"):
        problems.append(f"native_id {native_id!r} does not make a valid filing key")
    if values.get(None):
        problems.append("row has more cells than the header")
    entity_id = text("entity_id")
    if re.fullmatch(profile.entity_pattern, entity_id) is None:
        problems.append(f"entity_id {entity_id!r} does not match the {profile.code} pattern")
    scheme = text("entity_scheme") or profile.entity_scheme
    if scheme == SEC_CIK_SCHEME or len(scheme) > _MAX_TEXT or re.search(r"\s", scheme):
        problems.append(f"entity_scheme {scheme!r} is not allowed")
    company_name = text("company_name")
    if len(company_name) > _MAX_TEXT:
        problems.append("company_name is too long")
    form = text("form")
    if not form or len(form) > 64:
        problems.append("form is empty or too long")
    filed = _iso_date(text("filed_date"), "filed_date", problems)
    period_end = _iso_date(text("period_end"), "period_end", problems)
    fiscal_period = text("fiscal_period") or profile.default_fiscal_period
    if fiscal_period not in FISCAL_PERIODS:
        problems.append(f"fiscal_period {fiscal_period!r} is not one of {sorted(FISCAL_PERIODS)}")
    file = text("file")
    if not file:
        problems.append("file is empty")
    source_url = text("source_url")
    if source_url and not _is_web_url(source_url):
        problems.append("source_url is not an http(s) URL without credentials")
    return InboxRow(
        line=line,
        native_id=native_id,
        entity_id=entity_id,
        entity_scheme=scheme,
        company_name=company_name or entity_id,
        form=form,
        filed_date=filed,
        period_end=period_end,
        fiscal_period=fiscal_period,
        file=file,
        sha256=text("sha256").lower(),
        source_url=source_url,
        problems=tuple(problems),
    )


def _iso_date(value: str, column: str, problems: list[str]) -> date | None:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    problems.append(f"{column} {value!r} is not an ISO date (YYYY-MM-DD)")
    return None


def _is_web_url(value: str) -> bool:
    if len(value) > _MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", value):
        return False
    try:
        parts = urlsplit(value)
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def contained_path(root: Path, file: str) -> Path:
    """`root/file` with symlinks resolved; `ArtifactIntegrityError` unless it stays inside.

    Refuses empty, absolute (POSIX or Windows), backslash and `..` paths, and any
    path whose resolved location (after following every symlink) is outside the
    resolved `root` or is `root` itself. The result need not exist.
    """
    posix = PurePosixPath(file)
    if (
        not file
        or "\x00" in file
        or "\\" in file
        or posix.is_absolute()
        or PureWindowsPath(file).drive
        or ".." in posix.parts
    ):
        raise ArtifactIntegrityError(f"Inbox file {file!r} is not a relative path inside the inbox")
    base = root.resolve()
    target = (base / posix).resolve()
    if target == base or not target.is_relative_to(base):
        raise ArtifactIntegrityError(f"Inbox file {file!r} resolves outside the inbox")
    return target


def file_problems(settings: Settings, profile: InboxProfile, row: InboxRow) -> list[str]:
    """What stops `row`'s file from being fetched: containment, existence, size, digest."""
    if not row.file:
        return []
    try:
        path = contained_path(inbox_root(settings, profile.code), row.file)
    except ArtifactIntegrityError as exc:
        return [str(exc)]
    if not path.is_file():
        return [f"file {row.file} does not exist"]
    if path.stat().st_size > settings.filing_max_bytes:
        return [f"file {row.file} exceeds filing_max_bytes ({settings.filing_max_bytes})"]
    if row.sha256:
        if _SHA256.fullmatch(row.sha256) is None:
            return ["sha256 is not 64 hexadecimal characters"]
        if sha256_file(path) != row.sha256:
            return [f"file {row.file} does not match its sha256"]
    return []


class InboxSourceAdapter:
    """Discovery, package building, and fiscal focus for one inbox profile.

    `entity_keys` allocates entity keys (the control repository). There is no HTTP
    client: discovery reads the index and fetch reads the file, both on local disk.
    """

    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore,
        entity_keys: EntityKeys,
        profile: InboxProfile,
    ) -> None:
        self.settings = settings
        self.object_store = object_store
        self.entity_keys = entity_keys
        self.profile = profile
        self.code = profile.code
        self.taxonomy_prefixes = profile.taxonomy_prefixes

    @property
    def root(self) -> Path:
        return inbox_root(self.settings, self.code)

    # Discovery -------------------------------------------------------------------

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Index rows filed in `[filed_from, filed_to]` for `entity` and `forms`.

        Rows with an invalid key, date, or field are skipped and logged; the file is
        checked at fetch (or by `inbox check`).
        """
        index = await asyncio.to_thread(read_index, self.settings, self.profile)
        if index.unknown_columns:
            logger.warning(
                "inbox_unknown_columns", source=self.code, columns=list(index.unknown_columns)
            )
        entity = request.entity.strip() if request.entity is not None else None
        forms = frozenset(request.forms)
        filings: list[FilingRef] = []
        companies: dict[int, Company] = {}
        keys: dict[tuple[str, str], int] = {}
        for row in index.rows:
            if row.problems:
                logger.warning(
                    "inbox_row_skipped",
                    source=self.code,
                    native_id=row.label,
                    reason="; ".join(row.problems),
                )
                continue
            assert row.filed_date is not None and row.period_end is not None
            if (request.filed_from is not None and row.filed_date < request.filed_from) or (
                request.filed_to is not None and row.filed_date > request.filed_to
            ):
                continue
            if entity is not None and row.entity_id != entity:
                continue
            if forms and row.form not in forms:
                continue
            identity = (row.entity_scheme, row.entity_id)
            if identity not in keys:
                keys[identity] = await self.entity_keys.resolve_entity_key(*identity)
            try:
                filing = FilingRef(
                    accession=f"{self.code}-{row.native_id}",
                    cik=keys[identity],
                    company_name=row.company_name,
                    form=row.form,
                    filed_date=row.filed_date,
                    report_date=row.period_end,
                    fiscal_year=row.period_end.year,
                    fiscal_period=row.fiscal_period,
                    archive_directory_url=f"inbox://{self.code}/{row.native_id}",
                    primary_document_url=row.source_url or None,
                )
            except ValueError as exc:
                logger.warning(
                    "inbox_row_skipped", source=self.code, native_id=row.label, reason=str(exc)
                )
                continue
            filings.append(filing)
            companies.setdefault(filing.cik, Company(cik=filing.cik, name=filing.company_name))
        return DiscoveryResult(filings=tuple(filings), companies=tuple(companies.values()))

    # Package ---------------------------------------------------------------------

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        """Build the filing's package from its inbox file and store it once.

        The row is read again from the index, so the current file and digest apply.
        A `.zip` keeps its members; a single instance file becomes a one-member
        package. Either way the package needs exactly one instance and is stored at
        `filing_package_key(filing, sha256)`; a package that fails the archive or
        instance checks is kept under `quarantine/<code>/`. Containment, digest, size,
        and type failures raise `ArtifactIntegrityError` and store nothing.
        """
        if filing_source(filing.accession) != self.code:
            raise UnsupportedSourceError(
                f"The {self.code} inbox can't fetch {filing.source} filing {filing.accession}"
            )
        return await asyncio.to_thread(self._package, filing)

    def _package(self, filing: FilingRef) -> FilingPackageManifest:
        native_id = filing.accession.removeprefix(f"{self.code}-")
        rows = read_index(self.settings, self.profile).rows
        row = next((item for item in rows if item.native_id == native_id), None)
        if row is None:
            raise ArtifactIntegrityError(f"{native_id} is not in {self.code}/{INDEX_FILE}")
        if row.sha256 and _SHA256.fullmatch(row.sha256) is None:
            raise ArtifactIntegrityError(f"{native_id}: sha256 is not 64 hexadecimal characters")
        path = contained_path(self.root, row.file)
        name = _safe_package_name(PurePosixPath(row.file).name)
        suffix = PurePosixPath(name).suffix.lower()
        if suffix != ".zip" and suffix not in INSTANCE_SUFFIXES:
            raise ArtifactIntegrityError(
                f"Inbox file {row.file} is neither a .zip nor an XBRL instance "
                f"({', '.join(INSTANCE_SUFFIXES)})"
            )
        self.settings.ensure_directories()
        with tempfile.TemporaryDirectory(
            prefix=f"{self.code}-inbox-", dir=self.settings.temp_dir
        ) as directory:
            copy = Path(directory) / "source"
            _bounded_copy(path, copy, self.settings.filing_max_bytes, row.file)
            if row.sha256 and sha256_file(copy) != row.sha256:
                raise ArtifactIntegrityError(f"Inbox file {row.file} does not match its sha256")
            if suffix == ".zip":
                archive = copy
            else:
                archive = Path(directory) / "package.zip"
                with zipfile.ZipFile(archive, "w") as package:
                    _write_member(package, name, copy)
            return store_zip_package(
                self.settings,
                self.object_store,
                filing,
                archive,
                source=self.code,
                source_url=filing.archive_directory_url,
                entrypoint=_instance_picker(archive),
            )

    # Fiscal focus ----------------------------------------------------------------

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        """Keep the index's fiscal fields; fill the profile default period and the
        report date's year when absent."""
        filing = bundle.filing
        update: dict[str, Any] = {}
        if filing.fiscal_period is None:
            update["fiscal_period"] = self.profile.default_fiscal_period
        if filing.fiscal_year is None and filing.report_date is not None:
            update["fiscal_year"] = filing.report_date.year
        if not update:
            return bundle, ()
        return bundle.model_copy(update={"filing": filing.model_copy(update=update)}), ()


def _bounded_copy(source: Path, target: Path, limit: int, label: str) -> None:
    """Copy a regular file of at most `limit` bytes (no symlink at the last step)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError:
        raise ArtifactIntegrityError(f"Inbox file {label} does not exist") from None
    except OSError as exc:
        raise ArtifactIntegrityError(f"Inbox file {label} can't be read: {exc.strerror}") from None
    with os.fdopen(descriptor, "rb") as handle, target.open("wb") as sink:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactIntegrityError(f"Inbox file {label} is not a regular file")
        if info.st_size > limit:
            raise ArtifactIntegrityError(f"Inbox file {label} exceeds filing_max_bytes ({limit})")
        copied = 0
        while chunk := handle.read(1024 * 1024):
            copied += len(chunk)
            if copied > limit:
                raise ArtifactIntegrityError(
                    f"Inbox file {label} exceeds filing_max_bytes ({limit})"
                )
            sink.write(chunk)


def _instance_picker(archive: Path) -> Callable[[Sequence[str]], str]:
    """The entrypoint chooser for `store_zip_package`: exactly one instance member."""

    def pick(names: Sequence[str]) -> str:
        with zipfile.ZipFile(archive) as package:
            found = [name for name in names if _is_instance(package, name)]
        return single_entrypoint(found, "XBRL instance")

    return pick


def _is_instance(package: zipfile.ZipFile, name: str) -> bool:
    """`.xbrl`, an `.xml` whose root is `xbrli:xbrl`, or an inline `.xhtml`/`.html`."""
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".xbrl":
        return True
    if suffix == ".xml":
        with package.open(name) as stream:
            return _root_tag(stream) == _XBRL_ROOT
    if suffix in {".xhtml", ".html"}:
        with package.open(name) as stream:
            return _declares_inline_xbrl(stream)
    return False


def _iterparse(stream: IO[bytes], events: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    from lxml import etree

    return etree.iterparse(  # type: ignore[no-any-return]
        stream,
        events=events,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )


def _root_tag(stream: IO[bytes]) -> str | None:
    """The root element's `{namespace}name`, without entities, DTDs or network."""
    from lxml import etree

    try:
        for _, element in _iterparse(stream, ("start",)):
            return str(element.tag)
    except etree.XMLSyntaxError:
        return None
    return None


def _declares_inline_xbrl(stream: IO[bytes]) -> bool:
    """Whether a well-formed XHTML document declares the inline XBRL namespace."""
    from lxml import etree

    try:
        for event, item in _iterparse(stream, ("start-ns", "end")):
            if event == "start-ns":
                if item[1] in _INLINE_XBRL_NAMESPACES:
                    return True
            else:
                item.clear()
    except etree.XMLSyntaxError:
        return False
    return False
