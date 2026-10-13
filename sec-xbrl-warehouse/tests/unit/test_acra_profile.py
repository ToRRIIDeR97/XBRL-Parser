"""Singapore ACRA inbox profile: UEN shapes, fiscal fields, `inbox check`, wiring."""

from __future__ import annotations

import re
from datetime import date

import pytest
from test_inbox_source import (
    _adapter,
    _check,
    _discover,
    _instance,
    _Keys,
    _row,
    _run,
    _settings,
    _write,
)

from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.sources.inbox import ACRA_PROFILE, INBOX_PROFILES, InboxSourceAdapter

SCHEME = "https://www.acra.gov.sg/uen"
VALID_UENS = (
    "53312345A",  # business: 8 digits and a check letter
    "201912345K",  # local company: year, 5 digits, check letter
    "199901234A",
    "T08LL0001A",  # other entity, registered 2008 (T = 20xx)
    "S99FC1234B",  # other entity, 1999 (S = 19xx)
    "R89SS0001Z",  # other entity, 1889 (R = 18xx)
)
INVALID_UENS = (
    "",
    "201912345k",  # lower-case check letter
    "t08LL0001A",  # lower-case prefix
    "T08ll0001A",  # lower-case entity type
    "U08LL0001A",  # wrong year prefix
    "A08LL0001A",
    "1999-01234",
    "1234567A",  # 7 digits
    "12345678901A",  # 11 digits
    "201912345",  # no check letter
    "2019123456",
    "201912 345K",
    "T0LLL0001A",
    "T08L10001A",
    "T08LL001A",
    "T08LL00011",
    "201912345KX",
)


def test_profile_is_registered() -> None:
    assert INBOX_PROFILES["acra"] is ACRA_PROFILE
    assert (ACRA_PROFILE.code, ACRA_PROFILE.entity_scheme) == ("acra", SCHEME)
    assert ACRA_PROFILE.default_fiscal_period == "FY"


@pytest.mark.parametrize("uen", VALID_UENS)
def test_valid_uen_shapes_match(uen: str) -> None:
    assert re.fullmatch(ACRA_PROFILE.entity_pattern, uen)


@pytest.mark.parametrize("uen", INVALID_UENS)
def test_invalid_uens_do_not_match(uen: str) -> None:
    assert re.fullmatch(ACRA_PROFILE.entity_pattern, uen) is None


def test_discovery_keys_every_shape_and_skips_bad_uens(tmp_path) -> None:
    keys = _Keys()
    adapter, _ = _adapter(tmp_path, ACRA_PROFILE, keys)
    good = [_row(f"OK-{index}", "fs.xml", entity_id=uen) for index, uen in enumerate(VALID_UENS)]
    bad = [_row(f"BAD-{index}", "fs.xml", entity_id=uen) for index, uen in enumerate(INVALID_UENS)]
    _write(tmp_path / "inbox", "acra", good + bad, {"fs.xml": _instance()})
    filings = _discover(adapter)
    assert [f.accession for f in filings] == [f"acra-OK-{i}" for i in range(len(VALID_UENS))]
    assert keys.calls == [(SCHEME, uen) for uen in VALID_UENS]


def test_surrounding_whitespace_is_trimmed(tmp_path) -> None:
    keys = _Keys()
    adapter, _ = _adapter(tmp_path, ACRA_PROFILE, keys)
    _write(tmp_path / "inbox", "acra", [_row("A-1", "fs.xml", entity_id=" 201912345K ")])
    assert [f.accession for f in _discover(adapter)] == ["acra-A-1"]
    assert keys.calls == [(SCHEME, "201912345K")]


@pytest.mark.parametrize(
    ("period_end", "fiscal_period", "expected"),
    [
        ("2024-06-30", "", (2024, "FY")),  # June year-end
        ("2024-12-31", "", (2024, "FY")),
        ("2025-03-31", "", (2025, "FY")),
        ("2024-06-30", "H1", (2024, "H1")),
    ],
)
def test_fiscal_fields(tmp_path, period_end, fiscal_period, expected) -> None:
    adapter, _ = _adapter(tmp_path, ACRA_PROFILE)
    row = _row("A-1", "fs.xml", entity_id="201912345K", period_end=period_end)
    row["fiscal_period"] = fiscal_period
    _write(tmp_path / "inbox", "acra", [row])
    (filing,) = _discover(adapter)
    assert (filing.fiscal_year, filing.fiscal_period) == expected
    assert filing.report_date == date.fromisoformat(period_end)


def test_inbox_check_reports_bad_uens(monkeypatch, tmp_path) -> None:
    _write(
        tmp_path / "inbox",
        "acra",
        [
            _row("OK-1", "fs.xml", entity_id="T08LL0001A"),
            _row("BAD-1", "fs.xml", entity_id="201912345k"),
        ],
        {"fs.xml": _instance()},
    )
    result = _check(monkeypatch, tmp_path, "acra")
    assert result.exit_code == 1, result.output
    assert any("BAD-1" in line and "acra pattern" in line for line in result.output.splitlines())
    assert "1 valid, 1 problems" in result.output


def test_enabled_sources_accept_acra(tmp_path) -> None:
    assert _settings(tmp_path, enabled_sources="sec,acra").enabled_sources == ("sec", "acra")


def test_runtime_services_build_the_acra_adapter_without_a_client(monkeypatch, tmp_path) -> None:
    from test_esef_source import _patched_runtime

    from sec_xbrl_warehouse.worker import runtime_services

    built: list[str] = []
    _patched_runtime(monkeypatch, built)

    class _Control:
        engine = None
        object_store = None

    settings = _settings(
        tmp_path,
        sec_user_agent="Test Suite test@example.com",
        arelle_offline=True,
        object_store_backend="local",
        enabled_sources=("acra",),
        arelle_cache_dir=tmp_path / "c",
        arelle_taxonomy_packages_dir=tmp_path / "p",
    )

    async def services():
        async with runtime_services(settings, _Control(), [JobType.FETCH_FILING]) as found:  # type: ignore[arg-type]
            return found["sources"]

    registry = _run(services())
    assert registry.codes == ("acra",)
    adapter = registry.adapter("acra")
    assert isinstance(adapter, InboxSourceAdapter) and adapter.profile is ACRA_PROFILE
    assert built == []
