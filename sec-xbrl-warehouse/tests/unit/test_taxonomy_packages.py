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
    settings = Settings(env="test")
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
