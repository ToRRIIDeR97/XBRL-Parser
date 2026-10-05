"""Cover page and segment, product, and geographic breakdowns.

Pure functions over an extraction bundle (derived data, rebuildable from silver):

- `cover_page` reads the registrant's own non-dimensional dei facts. Shares
  outstanding sums the class-of-stock members when no non-dimensional value exists.
  Facts on any other axis, including co-registrants on `dei:LegalEntityAxis`, are
  ignored. A missing fact gives `None`; with several values the first in document
  order wins. Multi-security filers report `TradingSymbol`, `SecurityExchangeName`,
  and `Security12bTitle` per security on the class-of-stock axis: every security is
  kept in `securities`, and without a non-dimensional ticker/exchange both come from
  one security (common stock first, else the first in document order).
- `segment_rows` returns numeric facts whose dimensions are all on the allowlisted
  breakdown axes, with member labels from the bundle's concepts (standard label,
  else the member's local name without the `Member` suffix).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TypeVar

from sec_xbrl_warehouse.domain.xbrl import (
    DimensionMember,
    XbrlConcept,
    XbrlExtractionBundle,
    XbrlFact,
)
from sec_xbrl_warehouse.mapping.namespaces import namespace_family
from sec_xbrl_warehouse.normalization.facts import normalize_unit
from sec_xbrl_warehouse.normalization.statements import STANDARD_LABEL_ROLE

# Breakdown axes in label precedence order: (family, axis local name) -> kind.
BREAKDOWN_AXES: dict[tuple[str, str], str] = {
    ("us-gaap", "StatementBusinessSegmentsAxis"): "segment",
    ("srt", "ProductOrServiceAxis"): "product",
    ("srt", "StatementGeographicalAxis"): "geography",
    ("srt", "ConsolidationItemsAxis"): "consolidation",
}
_KIND_ORDER = {kind: index for index, kind in enumerate(BREAKDOWN_AXES.values())}
CLASS_OF_STOCK_AXIS = ("us-gaap", "StatementClassOfStockAxis")
# srt:IntersegmentEliminationMember, srt:ConsolidationEliminationsMember, and
# extension members named the same way.
_ELIMINATION = re.compile(r"Eliminations?Member$", re.IGNORECASE)
_SHARES = "EntityCommonStockSharesOutstanding"
_SECURITY_FIELDS = ("Security12bTitle", "TradingSymbol", "SecurityExchangeName")

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CoverSecurity:
    """One registered security from class-of-stock dei facts."""

    member: str
    title: str | None = None
    trading_symbol: str | None = None
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class CoverPage:
    accession: str
    document_type: str | None = None
    period_end_date: date | None = None
    registrant_name: str | None = None
    filer_category: str | None = None
    amendment: bool | None = None
    trading_symbol: str | None = None
    exchange: str | None = None
    auditor_name: str | None = None
    shares_outstanding: Decimal | None = None
    shares_outstanding_date: date | None = None
    public_float: Decimal | None = None
    securities: tuple[CoverSecurity, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentMember:
    axis: str  # breakdown kind: segment, product, geography, consolidation
    qname: str
    label: str


@dataclass(frozen=True, slots=True)
class SegmentRow:
    accession: str
    fact_id: str
    concept_qname: str
    axis: str
    member_label: str
    members: tuple[SegmentMember, ...]
    value: Decimal
    unit: str | None
    period_start: date | None
    period_end: date | None
    period_instant: date | None
    is_elimination: bool


def cover_page(bundle: XbrlExtractionBundle) -> CoverPage:
    """The filing's cover page from its non-dimensional dei facts."""
    plain: dict[str, list[XbrlFact]] = defaultdict(list)
    classes: list[XbrlFact] = []
    per_security: dict[str, dict[str, str]] = {}
    concepts = {(concept.namespace, concept.name): concept for concept in bundle.concepts}
    for fact in bundle.facts:
        if fact.is_nil or namespace_family(fact.concept_namespace) != "dei":
            continue
        if not fact.dimensions:
            plain[fact.concept_name].append(fact)
        elif _only_class_of_stock(fact.dimensions):
            if fact.concept_name == _SHARES:
                classes.append(fact)
            elif fact.concept_name in _SECURITY_FIELDS and (value := _text(fact)) is not None:
                dimension = fact.dimensions[0]
                concept = concepts.get(
                    (dimension.member_namespace or "", dimension.member_name or "")
                )
                member = concept.qname if concept is not None else _member_qname(dimension)
                per_security.setdefault(member, {}).setdefault(fact.concept_name, value)
    securities = tuple(
        CoverSecurity(
            member=member,
            title=fields.get("Security12bTitle"),
            trading_symbol=fields.get("TradingSymbol"),
            exchange=fields.get("SecurityExchangeName"),
        )
        for member, fields in per_security.items()
    )

    def first(name: str, read: Callable[[XbrlFact], T | None]) -> T | None:
        return next((value for value in map(read, plain[name]) if value is not None), None)

    shares = next((fact for fact in plain[_SHARES] if fact.value_decimal is not None), None)
    if shares is not None:
        shares_value, shares_date = shares.value_decimal, shares.period_instant
    else:
        shares_value, shares_date = _sum_classes(classes)
    symbol = first("TradingSymbol", _text)
    exchange = first("SecurityExchangeName", _text)
    if symbol is None or exchange is None:
        chosen = _primary_security(securities, symbol)
        if chosen is not None:
            symbol = symbol if symbol is not None else chosen.trading_symbol
            exchange = exchange if exchange is not None else chosen.exchange
    return CoverPage(
        accession=bundle.filing.accession,
        document_type=first("DocumentType", _text),
        period_end_date=first("DocumentPeriodEndDate", _date),
        registrant_name=first("EntityRegistrantName", _text),
        filer_category=first("EntityFilerCategory", _text),
        amendment=first("AmendmentFlag", _boolean),
        trading_symbol=symbol,
        exchange=exchange,
        auditor_name=first("AuditorName", _text),
        shares_outstanding=shares_value,
        shares_outstanding_date=shares_date,
        public_float=first("EntityPublicFloat", lambda fact: fact.value_decimal),
        securities=securities,
    )


def segment_rows(bundle: XbrlExtractionBundle) -> tuple[SegmentRow, ...]:
    """Numeric facts whose dimensions are all on allowlisted breakdown axes."""
    concepts = {(concept.namespace, concept.name): concept for concept in bundle.concepts}
    rows: list[SegmentRow] = []
    for fact in bundle.facts:
        if (
            not fact.dimensions
            or fact.is_nil
            or fact.value_decimal is None
            or fact.duplicate_ordinal != 1
        ):
            continue
        members: list[SegmentMember] = []
        eliminations = False
        for dimension in fact.dimensions:
            kind = _breakdown_kind(dimension)
            if kind is None or dimension.member_name is None:
                break
            concept = concepts.get((dimension.member_namespace or "", dimension.member_name))
            members.append(
                SegmentMember(
                    axis=kind,
                    qname=concept.qname if concept is not None else _member_qname(dimension),
                    label=_member_label(concept, dimension.member_name),
                )
            )
            eliminations = eliminations or bool(_ELIMINATION.search(dimension.member_name))
        else:
            ordered = tuple(sorted(members, key=lambda member: _KIND_ORDER[member.axis]))
            rows.append(
                SegmentRow(
                    accession=bundle.filing.accession,
                    fact_id=fact.fact_id,
                    concept_qname=fact.concept_qname,
                    axis=ordered[0].axis,
                    member_label=ordered[0].label,
                    members=ordered,
                    value=fact.value_decimal,
                    unit=normalize_unit(fact),
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    period_instant=fact.period_instant,
                    is_elimination=eliminations,
                )
            )
    return tuple(rows)


# --- helpers ----------------------------------------------------------------


def _breakdown_kind(dimension: DimensionMember) -> str | None:
    family = namespace_family(dimension.axis_namespace)
    return BREAKDOWN_AXES.get((family, dimension.axis_name)) if family else None


def _only_class_of_stock(dimensions: tuple[DimensionMember, ...]) -> bool:
    return (
        len(dimensions) == 1
        and (
            namespace_family(dimensions[0].axis_namespace),
            dimensions[0].axis_name,
        )
        == CLASS_OF_STOCK_AXIS
    )


def _primary_security(
    securities: tuple[CoverSecurity, ...], symbol: str | None
) -> CoverSecurity | None:
    """The security the cover ticker/exchange describe.

    The one matching a non-dimensional ticker, else common stock (member
    `CommonStockMember` or a 12(b) title containing "common stock"), else the first
    security with a ticker or exchange, in document order.
    """
    listed = [item for item in securities if item.trading_symbol or item.exchange]
    if symbol is not None:
        return next((item for item in listed if item.trading_symbol == symbol), None)
    common = next(
        (
            item
            for item in listed
            if item.member.rsplit(":", 1)[-1].rsplit("}", 1)[-1] == "CommonStockMember"
            or "common stock" in (item.title or "").lower()
        ),
        None,
    )
    return common or (listed[0] if listed else None)


def _sum_classes(facts: list[XbrlFact]) -> tuple[Decimal | None, date | None]:
    """Sum one value per share class at the latest instant reported for any class."""
    dated = [fact for fact in facts if fact.value_decimal is not None and fact.period_instant]
    if not dated:
        return None, None
    instant = max(fact.period_instant for fact in dated if fact.period_instant is not None)
    per_class: dict[str | None, Decimal] = {}
    for fact in dated:
        if fact.period_instant == instant and fact.value_decimal is not None:
            per_class.setdefault(fact.dimensions[0].member_name, fact.value_decimal)
    return sum(per_class.values(), Decimal(0)), instant


def _member_label(concept: XbrlConcept | None, member_name: str) -> str:
    labels = concept.labels if concept is not None else ()
    standard = [label for label in labels if label.role_uri == STANDARD_LABEL_ROLE]
    for language in ("en-US", "en"):
        match = next((label.text for label in standard if label.language == language), None)
        if match:
            return match
    if standard and standard[0].text:
        return standard[0].text
    return member_name.removesuffix("Member") or member_name


def _member_qname(dimension: DimensionMember) -> str:
    family = namespace_family(dimension.member_namespace or "")
    if family is not None:
        return f"{family}:{dimension.member_name}"
    return f"{{{dimension.member_namespace or ''}}}{dimension.member_name}"


def _text(fact: XbrlFact) -> str | None:
    value = fact.value_text if fact.value_text is not None else fact.raw_value
    value = value.strip() if value is not None else None
    return value or None


def _date(fact: XbrlFact) -> date | None:
    if fact.value_date is not None:
        return fact.value_date
    text = _text(fact)
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _boolean(fact: XbrlFact) -> bool | None:
    if fact.value_boolean is not None:
        return fact.value_boolean
    text = (_text(fact) or "").lower()
    return {"true": True, "1": True, "false": False, "0": False}.get(text)
