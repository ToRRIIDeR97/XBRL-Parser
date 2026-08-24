from __future__ import annotations

from dataclasses import dataclass

from sec_xbrl_warehouse.exceptions import SchemaDriftError


@dataclass(frozen=True, slots=True)
class HeaderDifference:
    missing: tuple[str, ...]
    unknown: tuple[str, ...]
    reordered: bool
    expected: tuple[str, ...]
    actual: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return not self.missing and not self.unknown and not self.reordered


def compare_headers(expected: tuple[str, ...], actual: tuple[str, ...]) -> HeaderDifference:
    normalized_expected = tuple(item.strip().lower() for item in expected)
    normalized_actual = tuple(item.removeprefix("\ufeff").strip().lower() for item in actual)
    missing = tuple(item for item in normalized_expected if item not in normalized_actual)
    unknown = tuple(item for item in normalized_actual if item not in normalized_expected)
    reordered = not missing and not unknown and normalized_expected != normalized_actual
    return HeaderDifference(missing, unknown, reordered, normalized_expected, normalized_actual)


def require_exact_header(expected: tuple[str, ...], actual: tuple[str, ...], source: str) -> None:
    difference = compare_headers(expected, actual)
    if not difference.compatible:
        raise SchemaDriftError(
            f"Schema drift in {source}: missing={difference.missing}, "
            f"unknown={difference.unknown}, reordered={difference.reordered}"
        )
