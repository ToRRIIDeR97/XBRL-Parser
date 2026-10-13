"""Operator file inbox (NSE, MCA): index, packages, containment, CLI, wiring."""

from __future__ import annotations

import asyncio
import csv
import io
import os
import zipfile
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError, UnsupportedSourceError
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.sources.base import DiscoveryRequest
from sec_xbrl_warehouse.sources.inbox import (
    INBOX_PROFILES,
    MCA_PROFILE,
    NSE_PROFILE,
    InboxSourceAdapter,
    contained_path,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
COLUMNS = (
    "native_id",
    "entity_id",
    "entity_scheme",
    "company_name",
    "form",
    "filed_date",
    "period_end",
    "fiscal_period",
    "file",
    "sha256",
    "source_url",
)
RELIANCE = "INE002A01018"
TCS = "INE467B01029"
CIN = "L17110MH1973PLC019786"


def _instance(text: str = "fact") -> bytes:
    return (
        f"<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance'><!-- {text} --></xbrli:xbrl>"
    ).encode()


def _inline() -> bytes:
    return (
        b"<html xmlns='http://www.w3.org/1999/xhtml' "
        b"xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><body/></html>"
    )


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _row(native_id: str, file: str, **values: str) -> dict[str, str]:
    return {
        "native_id": native_id,
        "entity_id": RELIANCE,
        "company_name": "Reliance Industries Limited",
        "form": "results",
        "filed_date": "2024-05-10",
        "period_end": "2024-03-31",
        "file": file,
        **values,
    }


def _write(
    root: Path,
    code: str,
    rows: list[dict[str, str]],
    files: dict[str, bytes] | None = None,
    columns: tuple[str, ...] = COLUMNS,
) -> Path:
    inbox = root / code
    inbox.mkdir(parents=True, exist_ok=True)
    for name, data in (files or {}).items():
        target = inbox / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    with (inbox / "index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return inbox


class _Keys:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_entity_key(self, scheme: str, identifier: str) -> int:
        self.calls.append((scheme, identifier))
        return 10_000_000_000 + len(set(self.calls))


def _settings(tmp_path: Path, **values) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        inbox_dir=tmp_path / "inbox",
        **values,
    )


def _adapter(tmp_path: Path, profile=NSE_PROFILE, keys: _Keys | None = None, **values):
    store = LocalObjectStore(tmp_path / "objects", bucket="t")
    adapter = InboxSourceAdapter(_settings(tmp_path, **values), store, keys or _Keys(), profile)
    return adapter, store


def _run(coroutine):
    return asyncio.run(coroutine)


def _discover(adapter: InboxSourceAdapter, **values) -> tuple[FilingRef, ...]:
    return _run(adapter.discover(DiscoveryRequest(**values))).filings


def _objects(store: LocalObjectStore, prefix: str = "") -> list[str]:
    return [key for key, _ in store.list_objects(prefix)]


# Index ---------------------------------------------------------------------------


def test_missing_column_and_duplicates_are_refused(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    columns = tuple(column for column in COLUMNS if column != "period_end")
    _write(tmp_path / "inbox", "nse", [_row("A-1", "a.xml")], columns=columns)
    with pytest.raises(ValueError, match="period_end"):
        _discover(adapter)
    _write(tmp_path / "inbox", "nse", [_row("A-1", "a.xml"), _row("A-1", "b.xml")])
    with pytest.raises(ValueError, match="A-1"):
        _discover(adapter)


def test_no_folder_or_no_rows_is_empty(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    assert _discover(adapter) == ()
    (tmp_path / "inbox" / "nse").mkdir(parents=True)
    assert _discover(adapter) == ()
    _write(tmp_path / "inbox", "nse", [])
    result = _run(adapter.discover(DiscoveryRequest()))
    assert result.filings == () and result.companies == ()


def test_filters_defaults_and_bad_rows(tmp_path) -> None:
    keys = _Keys()
    adapter, _ = _adapter(tmp_path, keys=keys)
    _write(
        tmp_path / "inbox",
        "nse",
        [
            _row("REL-Q4", "r.xml", fiscal_period="", source_url="https://example.org/r"),
            _row(
                "REL-Q3",
                "r3.xml",
                fiscal_period="Q3",
                filed_date="2024-01-20",
                period_end="2023-12-31",
            ),
            _row("TCS-Q4", "t.xml", entity_id=TCS, company_name="", form="annual"),
            _row("BAD DATE", "x.xml"),
            _row("BAD-DATE", "x.xml", filed_date="10/05/2024"),
            _row("BAD-ISIN", "x.xml", entity_id="ine002a01018"),
            _row("BAD-PERIOD", "x.xml", fiscal_period="Q5"),
            _row("BAD-URL", "x.xml", source_url="ftp://example.org/x"),
            _row("SEC-SCHEME", "x.xml", entity_scheme="http://www.sec.gov/CIK"),
            _row("", "x.xml"),
        ],
    )
    filings = _discover(adapter)
    assert [f.accession for f in filings] == ["nse-REL-Q4", "nse-REL-Q3", "nse-TCS-Q4"]
    first = filings[0]
    assert (first.fiscal_period, first.fiscal_year) == ("FY", 2024)
    assert first.archive_directory_url == "inbox://nse/REL-Q4"
    assert first.primary_document_url == "https://example.org/r"
    assert filings[1].fiscal_year == 2023 and filings[1].fiscal_period == "Q3"
    assert filings[2].company_name == TCS  # empty name falls back to the identifier
    # One key per entity, resolved once per run.
    assert keys.calls == [("urn:isin", RELIANCE), ("urn:isin", TCS)]
    assert filings[0].cik == filings[1].cik != filings[2].cik

    assert [f.accession for f in _discover(adapter, entity=TCS)] == ["nse-TCS-Q4"]
    assert [f.accession for f in _discover(adapter, forms=("annual",))] == ["nse-TCS-Q4"]
    window = _discover(adapter, filed_from=date(2024, 1, 1), filed_to=date(2024, 1, 31))
    assert [f.accession for f in window] == ["nse-REL-Q3"]
    assert _discover(adapter, filed_from=date(2024, 5, 11)) == ()


def test_entity_scheme_override_and_companies(tmp_path) -> None:
    keys = _Keys()
    adapter, _ = _adapter(tmp_path, keys=keys)
    _write(
        tmp_path / "inbox",
        "nse",
        [
            _row("A-1", "a.xml"),
            _row("A-2", "a.xml", company_name="Other name"),
            _row("B-1", "b.xml", entity_scheme="https://www.bseindia.com/isin"),
        ],
    )
    result = _run(adapter.discover(DiscoveryRequest()))
    assert keys.calls == [("urn:isin", RELIANCE), ("https://www.bseindia.com/isin", RELIANCE)]
    assert [(c.cik, c.name) for c in result.companies] == [
        (result.filings[0].cik, "Reliance Industries Limited"),
        (result.filings[2].cik, "Reliance Industries Limited"),
    ]


def test_mca_profile_uses_the_cin(tmp_path) -> None:
    keys = _Keys()
    adapter, _ = _adapter(tmp_path, MCA_PROFILE, keys)
    _write(
        tmp_path / "inbox",
        "mca",
        [_row("AOC4-2024", "a.zip", entity_id=CIN), _row("NOT-A-CIN", "a.zip")],
    )
    assert [f.accession for f in _discover(adapter)] == ["mca-AOC4-2024"]
    assert keys.calls == [("https://www.mca.gov.in/cin", CIN)]


# Packages ------------------------------------------------------------------------


def test_changed_bytes_make_a_new_package_and_replay_is_idempotent(tmp_path) -> None:
    adapter, store = _adapter(tmp_path)
    inbox = _write(tmp_path / "inbox", "nse", [_row("A-1", "a.xml")], {"a.xml": _instance("one")})
    filing = _discover(adapter)[0]
    first = _run(adapter.download_package(filing))
    again = _run(adapter.download_package(filing))
    assert again.package_object_key == first.package_object_key
    assert again.manifest_object_key == first.manifest_object_key
    (inbox / "a.xml").write_bytes(_instance("two"))
    second = _run(adapter.download_package(filing))
    assert second.package_object_key != first.package_object_key
    assert store.exists(first.package_object_key) and store.exists(second.package_object_key)
    assert first.members[0].source_url == "inbox://nse/A-1"


def test_inline_single_file_and_subfolders(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    _write(
        tmp_path / "inbox",
        "nse",
        [_row("A-1", "2024/q4/report.xhtml")],
        {"2024/q4/report.xhtml": _inline()},
    )
    manifest = _run(adapter.download_package(_discover(adapter)[0]))
    assert [(m.name, m.is_xbrl_entrypoint) for m in manifest.members] == [("report.xhtml", True)]


def test_zip_with_xml_instance_skips_linkbases(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    package = _zip(
        {
            "f/instance.xml": _instance(),
            "f/schema_lab.xml": b"<link:linkbase xmlns:link='http://www.xbrl.org/2003/linkbase'/>",
            "META-INF/catalog.xml": b"<catalog/>",
            "f/schema.xsd": b"<schema/>",
        }
    )
    _write(tmp_path / "inbox", "nse", [_row("A-1", "a.zip")], {"a.zip": package})
    manifest = _run(adapter.download_package(_discover(adapter)[0]))
    assert [m.name for m in manifest.members if m.is_xbrl_entrypoint] == ["f/instance.xml"]


@pytest.mark.parametrize(
    ("name", "data", "quarantined"),
    [
        ("two.zip", _zip({"a.xbrl": _instance(), "b.xbrl": _instance()}), True),
        ("none.zip", _zip({"a.xsd": b"<schema/>"}), True),
        ("broken.zip", b"not a zip", True),
        ("traversal.zip", _zip({"../evil.xbrl": _instance()}), True),
        ("plain.xml", b"<root>not xbrl</root>", True),
        ("notes.txt", b"plain text", False),
        ("report.pdf", b"%PDF-1.7", False),
    ],
)
def test_invalid_packages_store_no_bronze_object(tmp_path, name, data, quarantined) -> None:
    adapter, store = _adapter(tmp_path)
    _write(tmp_path / "inbox", "nse", [_row("A-1", name)], {name: data})
    with pytest.raises(ArtifactIntegrityError):
        _run(adapter.download_package(_discover(adapter)[0]))
    assert _objects(store, "bronze/") == [] and _objects(store, "manifests/") == []
    assert bool(_objects(store, "quarantine/nse/filings/nse-A-1/")) is quarantined
    assert (tmp_path / "inbox" / "nse" / name).read_bytes() == data


def test_stored_packages_materialize_their_instance(tmp_path) -> None:
    from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader

    adapter, store = _adapter(tmp_path)
    package = _zip({"f/instance.xbrl": _instance(), "f/schema.xsd": b"<schema/>"})
    _write(
        tmp_path / "inbox",
        "nse",
        [_row("ZIP-1", "a.zip"), _row("ONE-1", "one.xml")],
        {"a.zip": package, "one.xml": _instance("one")},
    )
    packages = FilingPackageDownloader(adapter.settings, None, store)
    for filing, expected in zip(_discover(adapter), ("f/instance.xbrl", "one.xml"), strict=True):
        manifest = _run(adapter.download_package(filing))
        entrypoint = packages.materialize_package(manifest, tmp_path / "out" / filing.accession)
        assert entrypoint == (tmp_path / "out" / filing.accession / expected).resolve()
    stored = _objects(store, "bronze/nse/filings/")
    assert len([key for key in stored if key.endswith("/package.zip")]) == 2


def test_containment_size_and_missing_files(tmp_path) -> None:
    adapter, store = _adapter(tmp_path, filing_max_bytes=1024)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.xml").write_bytes(_instance())
    inbox = _write(
        tmp_path / "inbox",
        "nse",
        [
            _row("LINK-FILE", "escape.xml"),
            _row("LINK-DIR", "dir/o.xml"),
            _row("BIG", "big.xml"),
            _row("GONE", "gone.xml"),
            _row("WIN", "C:\\data\\a.xml"),
            _row("ROOT", "."),
            _row("INSIDE-LINK", "alias.xml"),
        ],
        {"big.xml": _instance("x" * 2048), "real.xml": _instance()},
    )
    (inbox / "escape.xml").symlink_to(outside / "o.xml")
    (inbox / "dir").symlink_to(outside, target_is_directory=True)
    (inbox / "alias.xml").symlink_to(inbox / "real.xml")
    filings = {f.accession: f for f in _discover(adapter)}
    for key in ("LINK-FILE", "LINK-DIR", "BIG", "GONE", "WIN", "ROOT"):
        with pytest.raises(ArtifactIntegrityError):
            _run(adapter.download_package(filings[f"nse-{key}"]))
    assert _objects(store) == []
    # A symlink that stays inside the inbox is followed.
    manifest = _run(adapter.download_package(filings["nse-INSIDE-LINK"]))
    assert [m.name for m in manifest.members] == ["alias.xml"]


def test_contained_path_resolves_inside_the_root(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    assert contained_path(root, "a/b.xml") == (root / "a" / "b.xml").resolve()
    for value in ("", "../x", "a/../../x", "/etc/hosts", "a\\b", "C:/x", "a/.."):
        with pytest.raises(ArtifactIntegrityError):
            contained_path(root, value)


def test_row_removed_or_foreign_filing_is_refused(tmp_path) -> None:
    adapter, store = _adapter(tmp_path)
    _write(tmp_path / "inbox", "nse", [_row("A-1", "a.xml")], {"a.xml": _instance()})
    filing = _discover(adapter)[0]
    _write(tmp_path / "inbox", "nse", [])
    with pytest.raises(ArtifactIntegrityError, match="A-1"):
        _run(adapter.download_package(filing))
    foreign = filing.model_copy(update={"accession": "mca-A-1"})
    with pytest.raises(UnsupportedSourceError):
        _run(adapter.download_package(foreign))
    assert _objects(store) == []


def test_fiscal_focus_keeps_index_values_and_fills_gaps(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    filing = FilingRef(
        accession="nse-A-1",
        cik=10_000_000_001,
        company_name="X",
        form="results",
        filed_date=date(2024, 5, 10),
        report_date=date(2024, 3, 31),
        fiscal_year=2023,
        fiscal_period="Q4",
        archive_directory_url="inbox://nse/A-1",
    )
    kept, issues = adapter.fiscal_focus(_bundle(filing))
    assert (kept.filing.fiscal_year, kept.filing.fiscal_period, issues) == (2023, "Q4", ())
    bare = filing.model_copy(update={"fiscal_year": None, "fiscal_period": None})
    filled, _ = adapter.fiscal_focus(_bundle(bare))
    assert (filled.filing.fiscal_year, filled.filing.fiscal_period) == (2024, "FY")


def _bundle(filing: FilingRef) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="a.xml",
            document_type="xbrl",
            taxonomy_namespaces=(),
            source_documents=("a.xml",),
            software_name="arelle",
            software_version="test",
        ),
        contexts=(),
        concepts=(),
        facts=(),
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


# CLI -----------------------------------------------------------------------------


def _check(monkeypatch, tmp_path, source: str = "nse"):
    import sec_xbrl_warehouse.cli as cli
    from sec_xbrl_warehouse.config import get_settings

    class _NoControl:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("inbox check must not touch the control database")

    monkeypatch.setattr(cli, "PostgresControlRepository", _NoControl)
    monkeypatch.setenv("XBRL_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("XBRL_ENV", "test")
    get_settings.cache_clear()
    try:
        return CliRunner().invoke(cli.app, ["inbox", "check", "--source", source])
    finally:
        get_settings.cache_clear()


def test_inbox_check_reports_each_problem(monkeypatch, tmp_path) -> None:
    good = _instance()
    _write(
        tmp_path / "inbox",
        "nse",
        [
            _row("OK-1", "good.xml", sha256=sha256(good).hexdigest()),
            _row("OK-1", "good.xml"),
            _row("DIGEST", "good.xml", sha256="0" * 64),
            _row("OUTSIDE", "../x.xml"),
            _row("ISIN", "good.xml", entity_id="XX"),
        ],
        {"good.xml": good},
    )
    result = _check(monkeypatch, tmp_path)
    assert result.exit_code == 1, result.output
    lines = result.output.strip().splitlines()
    assert lines[-1] == "1 valid, 4 problems"
    assert any("OK-1" in line and "repeated" in line for line in lines)
    assert any("DIGEST" in line and "sha256" in line for line in lines)
    assert any("OUTSIDE" in line and "../x.xml" in line for line in lines)
    assert any("ISIN" in line and "entity_id" in line for line in lines)


def test_inbox_check_passes_and_reports_bad_headers(monkeypatch, tmp_path) -> None:
    _write(tmp_path / "inbox", "mca", [_row("A-1", "a.xml", entity_id=CIN)], {"a.xml": b"x"})
    result = _check(monkeypatch, tmp_path, "mca")
    assert result.exit_code == 0, result.output
    assert "1 valid, 0 problems" in result.output

    columns = tuple(column for column in COLUMNS if column != "file")
    _write(tmp_path / "inbox", "mca", [_row("A-1", "a.xml", entity_id=CIN)], columns=columns)
    result = _check(monkeypatch, tmp_path, "mca")
    assert result.exit_code == 1 and "missing required column file" in result.output
    assert "0 valid, 1 problems" in result.output

    assert _check(monkeypatch, tmp_path, "sec").exit_code == 2


# Wiring and taxonomies -----------------------------------------------------------


def test_profiles_have_no_network_policy(tmp_path) -> None:
    from sec_xbrl_warehouse.sources.registry import source_policies

    assert set(INBOX_PROFILES) == {"nse", "mca"}
    assert not set(INBOX_PROFILES) & set(source_policies(_settings(tmp_path)))
    adapter, _ = _adapter(tmp_path)
    assert not hasattr(adapter, "http")


def test_taxonomy_prefixes_fail_closed_until_pinned(tmp_path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
        coverage_gaps,
        registered_packages,
    )

    registry = load_yaml(REPO_CONFIG / "taxonomy_packages.yml")
    for code, profile in INBOX_PROFILES.items():
        assert tuple(registry["source_required_prefixes"][code]) == profile.taxonomy_prefixes
        enabled = _settings(tmp_path, config_dir=REPO_CONFIG, enabled_sources=("sec", code))
        gaps = set(coverage_gaps(enabled, registered_packages(enabled)))
        assert profile.taxonomy_prefixes and set(profile.taxonomy_prefixes) <= gaps


def test_settings_create_the_inbox_dir(tmp_path) -> None:
    settings = _settings(
        tmp_path, arelle_cache_dir=tmp_path / "c", arelle_taxonomy_packages_dir=tmp_path / "p"
    )
    settings.ensure_directories()
    assert (tmp_path / "inbox").is_dir()
    assert Settings(_env_file=None, env="test").inbox_dir == Path("data/inbox")


def test_runtime_services_build_inbox_adapters_without_clients(monkeypatch, tmp_path) -> None:
    from test_esef_source import _patched_runtime

    from sec_xbrl_warehouse.worker import runtime_services

    built: list[str] = []
    _patched_runtime(monkeypatch, built)

    class _Control:
        engine = None
        object_store = None

        async def resolve_entity_key(self, scheme, identifier):
            return 10_000_000_001

    control = _Control()
    settings = _settings(
        tmp_path,
        sec_user_agent="Test Suite test@example.com",
        arelle_offline=True,
        object_store_backend="local",
        enabled_sources=("sec", "nse", "mca"),
        arelle_cache_dir=tmp_path / "c",
        arelle_taxonomy_packages_dir=tmp_path / "p",
    )

    async def services(job_types):
        async with runtime_services(settings, control, job_types) as found:  # type: ignore[arg-type]
            return found["sources"]

    registry = _run(services([JobType.FETCH_FILING, JobType.DISCOVER_FILINGS]))
    assert registry.codes == ("mca", "nse", "sec")
    for code in ("nse", "mca"):
        adapter = registry.adapter(code)
        assert isinstance(adapter, InboxSourceAdapter)
        assert adapter.entity_keys is control and adapter.profile is INBOX_PROFILES[code]
    assert built == ["lock:sec", "sec-client"]  # no client or lock for inbox sources

    built.clear()
    inbox_only = settings.model_copy(update={"enabled_sources": ("nse",)})

    async def inbox_services():
        async with runtime_services(inbox_only, control, [JobType.FETCH_FILING]) as found:  # type: ignore[arg-type]
            return found["sources"]

    assert _run(inbox_services()).codes == ("nse",)
    assert built == []


# Round 2: every missing column, special files, long names, races, terminal safety ----


def test_every_missing_column_is_named(monkeypatch, tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    _write(
        tmp_path / "inbox",
        "nse",
        [_row("A-1", "a.xml")],
        columns=("native_id", "company_name", "file"),
    )
    with pytest.raises(ValueError) as caught:
        _discover(adapter)
    for column in ("entity_id", "form", "filed_date", "period_end"):
        assert column in str(caught.value)
    result = _check(monkeypatch, tmp_path)
    assert result.exit_code == 1, result.output
    for column in ("entity_id", "form", "filed_date", "period_end"):
        assert f"missing required column {column}" in result.output
    assert "0 valid, 4 problems" in result.output


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_and_directory_are_refused_without_blocking(monkeypatch, tmp_path) -> None:
    adapter, store = _adapter(tmp_path)
    inbox = _write(tmp_path / "inbox", "nse", [_row("PIPE", "p.xml"), _row("DIR", "d.xml")])
    os.mkfifo(inbox / "p.xml")
    (inbox / "d.xml").mkdir()
    filings = {f.accession: f for f in _discover(adapter)}
    for key in ("nse-PIPE", "nse-DIR"):
        with pytest.raises(ArtifactIntegrityError, match="not a regular file"):
            _run(asyncio.wait_for(adapter.download_package(filings[key]), timeout=10))
    assert _objects(store) == []
    result = _check(monkeypatch, tmp_path)
    assert result.exit_code == 1, result.output
    assert "PIPE (file p.xml): Inbox file p.xml is not a regular file" in result.output
    assert "DIR (file d.xml): Inbox file d.xml is not a regular file" in result.output
    assert "0 valid, 2 problems" in result.output


def test_overlong_file_name_is_a_problem_not_a_crash(monkeypatch, tmp_path) -> None:
    long_name = "x" * 300 + ".xml"
    adapter, _ = _adapter(tmp_path)
    _write(
        tmp_path / "inbox",
        "nse",
        [_row("LONG", long_name), _row("OK", "ok.xml")],
        {"ok.xml": _instance()},
    )
    with pytest.raises(ArtifactIntegrityError):
        _run(adapter.download_package(_discover(adapter)[0]))
    result = _check(monkeypatch, tmp_path)
    assert result.exit_code == 1, result.output
    assert any(line.startswith("LONG (file x") for line in result.output.splitlines())
    assert "1 valid, 1 problems" in result.output


def test_component_swapped_for_a_symlink_is_refused(monkeypatch, tmp_path) -> None:
    import sec_xbrl_warehouse.sources.inbox as inbox_module

    if os.open not in os.supports_dir_fd:
        pytest.skip("needs dir_fd")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.xml").write_bytes(_instance())
    root = tmp_path / "inbox" / "nse"
    root.mkdir(parents=True)
    (root / "sub").symlink_to(outside, target_is_directory=True)
    (root / "last.xml").symlink_to(outside / "o.xml")
    # As if each path was checked before the folder or file became a link.
    monkeypatch.setattr(inbox_module, "contained_path", lambda base, file: base.resolve() / file)
    for file in ("sub/o.xml", "last.xml"):
        with pytest.raises(ArtifactIntegrityError, match="can't be opened safely"):
            inbox_module.open_contained(root, file)


def test_index_symlink_outside_is_refused(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    outside = _write(tmp_path / "elsewhere", "nse", [_row("A-1", "a.xml")])
    root = tmp_path / "inbox" / "nse"
    root.mkdir(parents=True)
    (root / "index.csv").symlink_to(outside / "index.csv")
    with pytest.raises(ValueError, match=r"index\.csv"):
        _discover(adapter)


def test_check_escapes_control_characters(monkeypatch, tmp_path) -> None:
    _write(
        tmp_path / "inbox",
        "nse",
        [_row("A-1", "evil\x1b[31m.xml"), _row("B\x1b]0;x\x07", "b.xml")],
    )
    result = _check(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "evil\\x1b[31m.xml" in result.output
