from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from sec_xbrl_warehouse.domain.xbrl import DimensionMember


def canonicalize_dimensions(
    dimensions: Iterable[DimensionMember],
) -> tuple[DimensionMember, ...]:
    """Sort dimensions deterministically and reject duplicate axes.

    Both explicit and typed dimensions are retained. Namespace URIs, not display
    prefixes, define identity.
    """
    ordered = tuple(
        sorted(
            dimensions,
            key=lambda item: (
                item.axis_namespace,
                item.axis_name,
                item.member_namespace or "",
                item.member_name or "",
                item.typed_value_xml or "",
            ),
        )
    )
    axes = [(item.axis_namespace, item.axis_name) for item in ordered]
    if len(axes) != len(set(axes)):
        raise ValueError(f"Context contains duplicate dimension axes: {axes}")
    return ordered


def dimensions_to_json(dimensions: Iterable[DimensionMember]) -> str:
    ordered = canonicalize_dimensions(dimensions)
    payload = [item.model_dump(mode="json", exclude_none=True) for item in ordered]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dimensions_hash(dimensions: Iterable[DimensionMember]) -> str:
    return hashlib.sha256(dimensions_to_json(dimensions).encode("utf-8")).hexdigest()


def is_consolidated_context(dimensions: Iterable[DimensionMember]) -> bool:
    """A dimensionless context is a candidate for a consolidated fact.

    This is deliberately not treated as universally authoritative: some filings
    use dimensions even for the consolidated total, and some dimensions describe
    legal/entity presentation rather than a segment. Mapping/query layers must
    inspect the filing's presentation and calculation context.
    """
    return not tuple(dimensions)
