from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.exceptions import ConfigurationError, SecAccessError
from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
    package_status,
    registered_packages,
    validate_package_url,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _settings(tmp_path: Path, registry: str, **updates: object) -> Settings:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "taxonomy_packages.yml").write_text(registry, encoding="utf-8")
    return Settings(
        env="test",
        config_dir=config,
        arelle_taxonomy_packages_dir=tmp_path / "packages",
        **updates,
    )


def test_registry_scalars_are_read_as_written(tmp_path: Path) -> None:
    digits = "0123456789" * 6 + "0123"
    settings = _settings(
        tmp_path,
        "packages:\n"
        f"  - id: off\n    filename: no.zip\n    sha256: {digits}\n    enabled: false\n"
        "  - id: on\n    filename: yes.zip\n    sha256: 1e10\n",
    )
    first, second = registered_packages(settings)
    assert (first.package_id, first.filename, first.sha256, first.enabled) == (
        "off",
        "no.zip",
        digits,
        False,
    )
    assert (second.package_id, second.sha256, second.enabled) == ("on", "1e10", True)


def test_status_detects_ok_and_nested_paths(tmp_path: Path) -> None:
    payload = b"package"
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "ok.zip").write_bytes(payload)
    settings = _settings(
        tmp_path,
        "packages:\n"
        f"  - id: ok\n    filename: ok.zip\n    sha256: {sha256(payload).hexdigest()}\n"
        "  - id: nested\n    filename: sub/ok.zip\n    sha256: x\n",
    )
    assert {item.package_id: item.state for item in package_status(settings)} == {
        "ok": "ok",
        "nested": "unsafe_path",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://xbrl.fasb.org:8443/srt/2026/srt-2026.zip",
        "ftp://xbrl.fasb.org/srt.zip",
        "https://xbrl.fasb.org.evil.example/srt.zip",
        None,
    ],
)
def test_package_url_policy(tmp_path: Path, url: str | None) -> None:
    settings = _settings(tmp_path, "packages: []\n")
    with pytest.raises(ConfigurationError):
        validate_package_url(url, settings)


def test_repository_registry_entries_are_on_allowed_hosts() -> None:
    settings = Settings(_env_file=None, env="test")
    registry = load_yaml(REPO_CONFIG / "taxonomy_packages.yml")
    for entry in registry["packages"]:
        validate_package_url(entry["url"], settings)
        assert entry["filename"] == entry["url"].rsplit("/", 1)[1] or entry["id"].startswith("sec-")
    hosts = registry["network_policy"]["allowed_taxonomy_hosts"]
    assert set(hosts) == set(settings.allowed_taxonomy_hosts)


def test_sec_client_accepts_taxonomy_hosts_only_when_asked() -> None:
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient

    settings = Settings(env="test", sec_user_agent="Test Suite test@example.com")
    default = SecHttpClient(settings)
    with pytest.raises(SecAccessError):
        default._validate_url("https://xbrl.fasb.org/srt/2026/srt-2026.zip")
    taxonomy = SecHttpClient(settings, allowed_hosts=settings.allowed_taxonomy_hosts)
    taxonomy._validate_url("https://xbrl.fasb.org/srt/2026/srt-2026.zip")
    with pytest.raises(SecAccessError):
        taxonomy._validate_url("https://data.sec.gov/submissions/CIK0000320193.json")


def test_doctor_taxonomy_check(tmp_path: Path) -> None:
    from sec_xbrl_warehouse import cli

    registry = "packages:\n  - id: srt\n    filename: srt.zip\n    sha256: " + "a" * 64 + "\n"
    name, ok, details = cli._taxonomy_package_check(_settings(tmp_path, registry))
    assert (name, ok) == ("Taxonomy packages", False)
    assert "srt=missing" in details and "taxonomy sync" in details

    empty = _settings(tmp_path, "packages: []\n")
    assert cli._taxonomy_package_check(empty)[1] is True
    production = empty.model_copy(update={"env": "production", "arelle_offline": False})
    assert cli._taxonomy_package_check(production)[1] is False


@pytest.mark.arelle
def test_missing_package_is_a_warning_online_and_fatal_offline(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    registry = "packages:\n  - id: srt\n    filename: srt.zip\n    sha256: " + "a" * 64 + "\n"
    online = _settings(
        tmp_path,
        registry,
        temp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
        arelle_cache_dir=Path("data/cache/arelle"),
    )
    for name in ("sources.yml", "validation.yml"):
        if (REPO_CONFIG / name).is_file():
            (online.config_dir / name).write_bytes((REPO_CONFIG / name).read_bytes())
    controller, records = ArelleExtractor(online)._create_controller()
    controller.close()
    warnings = [r for r in records if r["code"] == "warehouse:taxonomyPackageMissing"]
    assert warnings and warnings[0]["details"]["package_ids"] == ["srt"]

    offline = online.model_copy(update={"arelle_offline": True})
    with pytest.raises(XbrlExtractionError, match="Missing or unsafe taxonomy package"):
        ArelleExtractor(offline)._create_controller()


# --- archive/file entries mapped by URI prefix ---------------------------------

FIXTURE_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        xmlns:xbrli="http://www.xbrl.org/2003/instance"
        targetNamespace="http://xbrl.sec.gov/fx/2025" elementFormDefault="qualified">
  <import namespace="http://www.xbrl.org/2003/instance"
          schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>
  <element name="Label" id="fx_Label" type="xbrli:stringItemType"
           substitutionGroup="xbrli:item" xbrli:periodType="duration" nillable="true"/>
</schema>
"""


def _fixture_filing(directory: Path, schema_url: str) -> Path:
    directory.mkdir(parents=True)
    (directory / "f.xsd").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<schema xmlns="http://www.w3.org/2001/XMLSchema" targetNamespace="http://example.com/f">'
        f'<import namespace="http://xbrl.sec.gov/fx/2025" schemaLocation="{schema_url}"/></schema>',
        encoding="utf-8",
    )
    instance = directory / "f.xml"
    instance.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:fx="http://xbrl.sec.gov/fx/2025">'
        '<link:schemaRef xlink:type="simple" xlink:href="f.xsd"/>'
        '<xbrli:context id="c"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1'
        "</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01"
        "</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>"
        '<fx:Label contextRef="c">pinned</fx:Label></xbrli:xbrl>',
        encoding="utf-8",
    )
    return instance


def _archive_settings(tmp_path: Path, entry: str, required: str = "") -> Settings:
    registry = "packages:\n" + entry + (f"required_prefixes:\n  - {required}\n" if required else "")
    return _settings(
        tmp_path,
        registry,
        temp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
        arelle_cache_dir=tmp_path / "empty-cache",
        arelle_offline=True,
    )


def _fixture_zip(packages: Path) -> str:
    import zipfile

    packages.mkdir(exist_ok=True)
    with zipfile.ZipFile(packages / "fx-2025.zip", "w") as archive:
        archive.writestr("published/xbrl.sec.gov/fx/2025/fx-2025.xsd", FIXTURE_SCHEMA)
    return sha256((packages / "fx-2025.zip").read_bytes()).hexdigest()


def test_archive_entry_parses_mappings_and_both_schemes(tmp_path: Path) -> None:
    digest = _fixture_zip(tmp_path / "packages")
    settings = _archive_settings(
        tmp_path,
        "  - id: fx-2025\n    kind: archive\n    filename: fx-2025.zip\n"
        f"    url: https://xbrl.sec.gov/2025.zip\n    sha256: {digest}\n    mappings:\n"
        '      https://xbrl.sec.gov/fx/2025/: "published/xbrl.sec.gov/fx/2025/"\n',
        required="xbrl.sec.gov/fx/2025/",
    )
    (package,) = registered_packages(settings)
    target = f"{package.path}/published/xbrl.sec.gov/fx/2025/"
    assert package.remappings() == {
        "http://xbrl.sec.gov/fx/2025/": target,
        "https://xbrl.sec.gov/fx/2025/": target,
    }
    assert [item.state for item in package_status(settings)] == ["ok"]


def test_required_prefix_without_enabled_source_is_uncovered(tmp_path: Path) -> None:
    from sec_xbrl_warehouse import cli

    settings = _archive_settings(
        tmp_path,
        "  - id: fx-2025\n    kind: archive\n    filename: fx-2025.zip\n"
        "    url: https://xbrl.sec.gov/2025.zip\n    sha256: " + "a" * 64 + "\n"
        '    enabled: false\n    mappings:\n      https://xbrl.sec.gov/fx/2025/: ""\n',
        required="https://xbrl.sec.gov/fx/2025/",
    )
    states = {item.package_id: item.state for item in package_status(settings)}
    assert states == {"fx-2025": "disabled", "xbrl.sec.gov/fx/2025/": "uncovered"}
    _, ok, details = cli._taxonomy_package_check(settings)
    assert not ok and "uncovered" in details


@pytest.mark.parametrize(
    "mapping",
    [
        '      https://xbrl.sec.gov/fx/2025: ""\n',  # archive prefixes end with /
        '      https://xbrl.sec.gov/fx/2025/: "../escape/"\n',
        '      xbrl.sec.gov/fx/2025/: ""\n',
    ],
)
def test_invalid_archive_mappings_are_rejected(tmp_path: Path, mapping: str) -> None:
    settings = _archive_settings(
        tmp_path,
        "  - id: fx\n    kind: archive\n    filename: fx.zip\n    url: https://xbrl.sec.gov/fx.zip\n"
        "    sha256: " + "a" * 64 + "\n    mappings:\n" + mapping,
    )
    with pytest.raises(ConfigurationError):
        registered_packages(settings)


@pytest.mark.arelle
def test_offline_extraction_resolves_through_archive_prefix_mapping(tmp_path: Path) -> None:
    from datetime import date

    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    digest = _fixture_zip(tmp_path / "packages")
    settings = _archive_settings(
        tmp_path,
        "  - id: fx-2025\n    kind: archive\n    filename: fx-2025.zip\n"
        f"    url: https://xbrl.sec.gov/2025.zip\n    sha256: {digest}\n    mappings:\n"
        '      https://xbrl.sec.gov/fx/2025/: "published/xbrl.sec.gov/fx/2025/"\n',
        required="xbrl.sec.gov/fx/2025/",
    )
    # The filing uses the http form; the registry declares https.
    instance = _fixture_filing(tmp_path / "filing", "http://xbrl.sec.gov/fx/2025/fx-2025.xsd")
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    bundle = ArelleExtractor(settings).extract(instance, filing)
    assert [(fact.concept_name, fact.value_text) for fact in bundle.facts] == [("Label", "pinned")]

    # Offline extraction refuses to start when a required prefix has no source.
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError

    uncovered = _archive_settings(
        tmp_path,
        "  - id: fx-2025\n    kind: archive\n    filename: fx-2025.zip\n"
        f"    url: https://xbrl.sec.gov/2025.zip\n    sha256: {digest}\n    mappings:\n"
        '      https://xbrl.sec.gov/fx/2025/: "published/xbrl.sec.gov/fx/2025/"\n',
        required="xbrl.sec.gov/fx/2026/",
    )
    with pytest.raises(XbrlExtractionError, match="No pinned taxonomy source"):
        ArelleExtractor(uncovered).extract(instance, filing)


@pytest.mark.arelle
def test_offline_dei_schema_resolves_through_pinned_sec_zip(tmp_path: Path) -> None:
    """Real dei 2024 schema, offline, from the pinned SEC 2024 zip.

    Needs the zip on disk: run `xbrl-warehouse taxonomy sync` first, or point
    XBRL_TEST_TAXONOMY_PACKAGES_DIR at a directory holding `sec-2024.zip`.
    """
    import os
    import shutil
    from datetime import date

    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    registry = load_yaml(REPO_CONFIG / "taxonomy_packages.yml")
    entry = next(item for item in registry["packages"] if item["id"] == "sec-2024")
    source_dir = Path(
        os.environ.get("XBRL_TEST_TAXONOMY_PACKAGES_DIR")
        or Settings(env="test").arelle_taxonomy_packages_dir
    )
    source = source_dir / entry["filename"]
    if not source.is_file() or sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
        pytest.skip(
            f"{entry['filename']} not installed in {source_dir}; run `xbrl-warehouse taxonomy "
            "sync` or set XBRL_TEST_TAXONOMY_PACKAGES_DIR"
        )
    (tmp_path / "packages").mkdir()
    shutil.copy(source, tmp_path / "packages" / entry["filename"])
    lines = ["packages:", f"  - id: {entry['id']}", "    kind: archive"]
    lines += [f"    filename: {entry['filename']}", f"    url: {entry['url']}"]
    lines += [f"    sha256: {entry['sha256']}", "    mappings:"]
    lines += [f'      {prefix}: "{inner}"' for prefix, inner in entry["mappings"].items()]
    settings = _settings(
        tmp_path,
        "\n".join(lines) + "\nrequired_prefixes:\n  - xbrl.sec.gov/dei/2024/\n",
        temp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
        arelle_cache_dir=tmp_path / "empty-cache",
        arelle_offline=True,
    )
    filing_dir = tmp_path / "filing"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "fixtures" / "taxonomy_offline", filing_dir
    )
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    bundle = ArelleExtractor(settings).extract(filing_dir / "dei-filing.xml", filing)
    values = {fact.concept_name: fact.value_text for fact in bundle.facts}
    assert values["EntityRegistrantName"] == "Example Corp"
    assert all(fact.concept_namespace == "http://xbrl.sec.gov/dei/2024" for fact in bundle.facts)


def test_archive_mapping_to_missing_directory_is_reported(tmp_path: Path) -> None:
    digest = _fixture_zip(tmp_path / "packages")
    settings = _archive_settings(
        tmp_path,
        "  - id: fx-2025\n    kind: archive\n    filename: fx-2025.zip\n"
        f"    url: https://xbrl.sec.gov/2025.zip\n    sha256: {digest}\n    mappings:\n"
        '      https://xbrl.sec.gov/fx/2025/: "published/xbrl.sec.gov/fx/2025/"\n'
        '      https://xbrl.sec.gov/fy/2025/: "published/xbrl.sec.gov/fy/2025/"\n'
        '      https://xbrl.sec.gov/fz/2025/: "published/"\n',
    )
    (status,) = package_status(settings)
    assert status.state == "mapping_missing"
    assert "published/xbrl.sec.gov/fy/2025/" in status.detail
    assert "fx/2025" not in status.detail and "'published/'" not in status.detail
