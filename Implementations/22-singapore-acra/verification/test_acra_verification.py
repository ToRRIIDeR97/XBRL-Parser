"""Independent verification tests for 22-singapore-acra."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from inbox_fixtures import EntityKeys, instance, row, write_inbox  # noqa: E402

from sec_xbrl_warehouse.config import Settings  # noqa: E402
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore  # noqa: E402


def request():
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(filed_from=None, filed_to=None, entity=None, forms=())


# AC-2: the three UEN shapes are accepted; malformed or lower-case ones are skipped.
@pytest.mark.parametrize(
    ("uen", "accepted"),
    [
        ("53312345D", True),  # business (8 digits + letter)
        ("201912345K", True),  # local company (9 digits + letter)
        ("T08LL1234A", True),  # other entities (T/S/R, year, type, serial, letter)
        ("S99PF0001Z", True),
        ("201912345k", False),
        ("2019123456", False),
        ("X08LL1234A", False),
        ("", False),
    ],
)
async def test_ac2_uen_shapes(tmp_path, uen, accepted) -> None:
    from sec_xbrl_warehouse.sources.inbox import ACRA_PROFILE, InboxSourceAdapter

    write_inbox(tmp_path / "inbox", "acra", [row("FS-1", "fs.xml", entity_id=uen, form="FS")], {"fs.xml": instance()})
    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "d", inbox_dir=tmp_path / "inbox")
    source = InboxSourceAdapter(settings, LocalObjectStore(tmp_path / "o", bucket="t"), EntityKeys(), ACRA_PROFILE)
    filings = (await source.discover(request())).filings
    assert bool(filings) is accepted


# AC-4: `inbox check` knows the acra profile.
def test_ac4_inbox_check_acra(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from sec_xbrl_warehouse.cli import app
    from sec_xbrl_warehouse.config import get_settings

    write_inbox(tmp_path / "inbox", "acra",
                [row("FS-1", "fs.xml", entity_id="201912345K", form="FS"),
                 row("FS-2", "fs.xml", entity_id="bad", form="FS")],
                {"fs.xml": instance()})
    monkeypatch.setenv("XBRL_INBOX_DIR", str(tmp_path / "inbox"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["inbox", "check", "--source", "acra"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "FS-2" in result.output and "1 valid, 1 problems" in result.output


# AC-1: enabling `acra` is a valid source setting.
def test_ac1_enable_setting() -> None:
    settings = Settings(_env_file=None, env="test", enabled_sources=("sec", "acra"))
    assert "acra" in settings.enabled_sources
