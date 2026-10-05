from datetime import date
from decimal import Decimal

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import (
    classify_period,
    coerce_typed_value,
    relationship_type_from_arcrole,
    semantic_hash,
)


def test_semantic_hash_is_key_order_independent() -> None:
    assert semantic_hash({"a": 1, "b": 2}) == semantic_hash({"b": 2, "a": 1})


def test_numeric_coercion_uses_decimal() -> None:
    kind, value = coerce_typed_value("0.1", is_nil=False, is_numeric=True, base_xsd_type=None)
    assert kind is FactValueKind.NUMERIC
    assert value == Decimal("0.1")


def test_invalid_numeric_coercion_preserves_numeric_fact_without_value() -> None:
    kind, value = coerce_typed_value(
        "(ixTransformValueError)", is_nil=False, is_numeric=True, base_xsd_type=None
    )
    assert kind is FactValueKind.NUMERIC
    assert value is None


def test_period_and_relationship_classification() -> None:
    assert classify_period(start=None, end=None, instant=date(2025, 1, 1)) is PeriodKind.INSTANT
    assert (
        relationship_type_from_arcrole("http://www.xbrl.org/2003/arcrole/summation-item")
        is RelationshipType.CALCULATION
    )


def test_taxonomy_transport_enforces_authority_and_stream_budgets(monkeypatch):
    import io
    from types import SimpleNamespace
    from urllib.request import Request

    import pytest

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import _restrict_taxonomy_access

    handlers = []
    cache = SimpleNamespace(
        getfilename=lambda url, *args, **kwargs: url,
        opener=SimpleNamespace(add_handler=handlers.append),
    )
    settings = Settings(
        _env_file=None,
        env="test",
        sec_user_agent="Test test@example.com",
        response_max_bytes=5,
        extraction_max_bytes=8,
        filing_max_members=2,
    )
    _restrict_taxonomy_access(cache, settings)
    handler = handlers[0]
    monkeypatch.setattr(
        "sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter.time.sleep", lambda _: None
    )
    for url in (
        "http://www.sec.gov/a.xsd",
        "https://www.sec.gov:444/a.xsd",
        "https://user@www.sec.gov/a.xsd",
        "https://localhost/a.xsd",
    ):
        with pytest.raises(XbrlExtractionError, match="denied"):
            handler.https_request(Request(url))
    arelle_request = Request("https://www.sec.gov/a.xsd")
    # Arelle sets this as an unredirected header, which urllib sends in
    # preference to ordinary headers.
    arelle_request.add_unredirected_header("Accept-encoding", "gzip, deflate")
    request = handler.https_request(arelle_request)
    sent = {**request.headers, **request.unredirected_hdrs}
    assert sent["Accept-encoding"] == "identity"

    def response(data, headers=None):
        stream = io.BytesIO(data)
        stream.headers = headers or {}
        return stream

    for headers in (
        {"Content-Length": "6"},
        {"Content-Encoding": "gzip"},
        {"Content-Length": "invalid"},
        {"Content-Length": "-1"},
    ):
        stream = response(b"abcdef", headers)
        with pytest.raises(XbrlExtractionError):
            handler.https_response(request, stream)
        assert stream.closed
    stream = handler.https_response(request, response(b"abcdef"))
    assert stream.read(5) == b"abcde"
    with pytest.raises(XbrlExtractionError, match="byte limit"):
        stream.read(1)
    assert stream.closed
    stream = handler.https_response(request, response(b"1234"))
    assert stream.read(3) == b"123"
    with pytest.raises(XbrlExtractionError, match="byte limit"):
        stream.read(1)
    handler.https_request(Request("https://www.sec.gov/b.xsd"))
    with pytest.raises(XbrlExtractionError, match="request count"):
        handler.https_request(Request("https://www.sec.gov/c.xsd"))


def test_cache_helper_rejects_escape_and_symlink(tmp_path):
    from types import SimpleNamespace

    import pytest
    from arelle.WebCache import WebCache

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import _restrict_taxonomy_access

    cache = WebCache(
        SimpleNamespace(
            userAppDir=str(tmp_path),
            hasFileSystem=True,
            isGAE=False,
            disablePersistentConfig=True,
            plugins=SimpleNamespace(hooks=lambda name: ()),
        ),
        None,
    )
    cache.workOffline = True
    root = tmp_path / "cache"
    (root / "https/www.sec.gov").mkdir(parents=True)
    (tmp_path / "outside.xsd").write_text("harmless")
    _restrict_taxonomy_access(cache, Settings(arelle_offline=True))
    with pytest.raises(XbrlExtractionError, match="escapes"):
        cache.getfilename("https://www.sec.gov/../../../outside.xsd")
    (root / "https/www.sec.gov/link.xsd").symlink_to(tmp_path / "outside.xsd")
    with pytest.raises(XbrlExtractionError, match="escapes"):
        cache.getfilename("https://www.sec.gov/link.xsd")


def test_incomplete_taxonomy_fails_extraction() -> None:
    import pytest

    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import _require_complete_dts

    _require_complete_dts([{"code": "ix11.11.1.2:invalidTransformation", "message": "kept"}])
    for code in (
        "FileNotLoadable",
        "webCache:unsuccessfulRetrieval",
        "ix11.12.1.2:missingReferences",
    ):
        with pytest.raises(XbrlExtractionError, match="did not load completely"):
            _require_complete_dts([{"code": code, "message": "srt-2022.xsd"}])
