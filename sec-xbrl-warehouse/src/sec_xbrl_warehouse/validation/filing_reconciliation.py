from __future__ import annotations

import calendar
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from bs4 import BeautifulSoup

from sec_xbrl_warehouse.normalization.concepts import namespace_family
from sec_xbrl_warehouse.observability.metrics import RECONCILIATION_DIFFERENCES


def reconcile_filing_rows(
    accession: str,
    numeric: list[dict[str, Any]],
    text: list[dict[str, Any]],
    raw: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare the SEC's lossy projection, retaining both originals unchanged.

    Projection rules: https://www.sec.gov/files/aqfsn_1.pdf (DIM/NUM/TXT).
    Typed dimensions, truncated dimension strings, and forever periods cannot
    be compared losslessly and are explicitly counted instead of called errors.
    """
    bulk: dict[tuple[Any, ...], set[str | None]] = defaultdict(set)
    arelle: dict[tuple[Any, ...], set[str | None]] = defaultdict(set)
    excluded: Counter[str] = Counter()
    truncated: set[tuple[Any, ...]] = set()
    for kind, rows in (("numeric", numeric), ("text", text)):
        for row in rows:
            if row.get("segt"):
                excluded["bulk_truncated_dimensions"] += 1
                continue
            key = (
                kind,
                row["tag"],
                row["version"],
                str(row["ddate"]),
                int(row["qtrs"]),
                row.get("uom", "") if kind == "numeric" else row.get("lang", ""),
                row.get("segments") or "",
            )
            value = row.get("value_raw")
            bulk[key].add(_number(value) if kind == "numeric" else _text(value))
            if (
                kind == "text"
                and value is not None
                and (row.get("txtlen") or 0) > len(str(value).encode())
            ):
                truncated.add(key)

    namespaces: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for row in raw:
        dims = json.loads(row.get("dimensions_json") or "[]")
        if any(dim.get("typed_value_xml") is not None for dim in dims):
            excluded["arelle_typed_dimensions"] += 1
            continue
        if row["period_kind"] == "forever" or not (
            row.get("period_end") or row.get("period_instant")
        ):
            excluded["arelle_forever_or_tuple"] += 1
            continue
        segments = "".join(
            sorted(
                f"{_short_tag(dim['axis_name'])}={_short_tag(dim['member_name'])};" for dim in dims
            )
        )
        if len(segments) > 1024:
            excluded["arelle_long_dimensions"] += 1
            continue
        family = namespace_family(row["concept_namespace"])
        version = (
            accession
            if family.startswith("extension")
            else f"{family}/{str(row.get('taxonomy_version') or '')[:4]}"
        )
        instant = row.get("period_instant")
        end = _date(instant or row["period_end"])
        qtrs = (
            0
            if instant
            else int(
                (
                    Decimal((_date(row["period_end"]) - _date(row["period_start"])).days + 1)
                    / Decimal("91.25")
                ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
        )
        kind = "numeric" if row.get("value_kind") == "numeric" or row.get("unit_id") else "text"
        unit = _unit(row) if kind == "numeric" else row.get("language") or ""
        key = (
            kind,
            row["concept_name"],
            version,
            str(_nearest_month_end(end)),
            qtrs,
            unit,
            segments,
        )
        value = row.get("value_decimal") if kind == "numeric" else row.get("value_text")
        if value is None and not row.get("is_nil"):
            value = row.get("raw_value")
        arelle[key].add(_number(value) if kind == "numeric" else _text(value))
        namespaces[key].add(row["concept_namespace"])

    for key, values in namespaces.items():
        if len(values) > 1:
            excluded["ambiguous_namespace_projection"] += 1
            arelle.pop(key, None)
            bulk.pop(key, None)
    missing = sorted(bulk.keys() - arelle.keys(), key=str)
    extra = sorted(arelle.keys() - bulk.keys(), key=str)
    differing = []
    for key in sorted(bulk.keys() & arelle.keys(), key=str):
        left, right = bulk[key], arelle[key]
        if left == right:
            continue
        if key in truncated and all(
            any(str(value).startswith(str(prefix)) for value in right) for prefix in left
        ):
            continue
        differing.append(
            {"key": key, "bulk": sorted(left, key=str), "arelle": sorted(right, key=str)}
        )
    for kind, count in (
        ("missing", len(missing)),
        ("extra", len(extra)),
        ("differing", len(differing)),
    ):
        RECONCILIATION_DIFFERENCES.labels(comparison="bulk_arelle", kind=kind).inc(count)
    return {
        "available": True,
        "accession": accession,
        "bulk_count": len(numeric) + len(text),
        "arelle_count": len(raw),
        "compared_bulk_keys": len(bulk),
        "compared_arelle_keys": len(arelle),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "differing_count": len(differing),
        "excluded": dict(excluded),
        "truncated_text_keys": len(truncated),
        "missing": missing[:100],
        "extra": extra[:100],
        "differing": differing[:100],
        "samples_truncated": any(len(items) > 100 for items in (missing, extra, differing)),
        "projection": "SEC month-end/quarter/4-decimal/lexical-dimension projection; duplicates compared as value sets",
    }


def _short_tag(value: str) -> str:
    return (
        value.removeprefix("Statement")
        .removesuffix("Axis")
        .removesuffix("Member")
        .removesuffix("Domain")
    )


def _date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _nearest_month_end(value: date) -> date:
    current = value.replace(day=calendar.monthrange(value.year, value.month)[1])
    previous = value.replace(day=1) - timedelta(days=1)
    return min((current, previous), key=lambda candidate: abs((candidate - value).days))


def _number(value: Any) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 100
        parsed = Decimal(str(value))
        return (
            format(parsed.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")
            if parsed.is_finite()
            else str(parsed)
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    plain = BeautifulSoup(str(value), "html.parser").get_text() if "<" in str(value) else str(value)
    return " ".join(plain.split())


def _unit(row: dict[str, Any]) -> str:
    numerator = json.loads(row.get("unit_numerator_json") or "[]")
    denominator = json.loads(row.get("unit_denominator_json") or "[]")

    def name(value: str) -> str:
        return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]

    top = "*".join(sorted(name(value) for value in numerator)) or "1"
    bottom = "*".join(sorted(name(value) for value in denominator))
    return f"{top}/{bottom}" if bottom else top
