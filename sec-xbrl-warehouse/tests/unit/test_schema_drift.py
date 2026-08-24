import pytest

from sec_xbrl_warehouse.exceptions import SchemaDriftError
from sec_xbrl_warehouse.validation.schema_drift import compare_headers, require_exact_header


def test_exact_header_is_compatible() -> None:
    difference = compare_headers(("a", "b"), ("a", "b"))
    assert difference.compatible


def test_reordered_header_is_drift() -> None:
    difference = compare_headers(("a", "b"), ("b", "a"))
    assert difference.reordered
    with pytest.raises(SchemaDriftError):
        require_exact_header(("a", "b"), ("b", "a"), "fixture")


def test_bom_is_removed() -> None:
    assert compare_headers(("a",), ("\ufeffa",)).compatible
