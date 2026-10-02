from sec_xbrl_warehouse.config import Settings


def test_disk_reserve_checks_data_and_scratch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from sec_xbrl_warehouse.exceptions import DiskReserveError

    settings = Settings(
        data_dir=tmp_path / "data", temp_dir=tmp_path / "scratch", disk_reserve_bytes=100
    )
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda path: SimpleNamespace(free=99 if path == settings.temp_dir else 1000),
    )
    with pytest.raises(DiskReserveError, match="scratch"):
        settings.require_disk_reserve()
