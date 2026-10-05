"""Primary financial statements as the filer presented them.

Pure functions over an extraction bundle: `classify_roles` finds the primary
statement roles and `build_statement_lines` turns each role's presentation tree
into ordered lines with labels, depth, and one non-dimensional value per period.
Lines are derived data and can be rebuilt from the saved extraction.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sec_xbrl_warehouse.domain.enums import RelationshipType
from sec_xbrl_warehouse.domain.xbrl import XbrlConcept, XbrlExtractionBundle, XbrlFact, XbrlRole

PARENT_CHILD_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"
TOTAL_LABEL_ROLE = "http://www.xbrl.org/2003/role/totalLabel"


class StatementKind(StrEnum):
    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    COMPREHENSIVE_INCOME = "comprehensive_income"
    CASH_FLOW = "cash_flow"
    EQUITY = "equity"


@dataclass(frozen=True, slots=True)
class StatementRole:
    role_uri: str
    statement_kind: StatementKind
    # True when the role's tree holds a concept typical of that statement kind.
    has_anchor: bool
    # "definition" or "role_uri": what the classification was based on.
    source: str
    sort_code: str | None = None


@dataclass(frozen=True, slots=True)
class StatementValue:
    fact_id: str
    period_start: date | None
    period_end: date | None
    period_instant: date | None
    value_decimal: Decimal | None
    # The fact's concept: the line's own concept, or for a `member` line the line
    # item concept reported for that member.
    concept_qname: str | None = None


class LineRole(StrEnum):
    """What a statement line is: a reported concept or dimensional scaffolding."""

    CONCEPT = "concept"
    TABLE = "table"
    AXIS = "axis"
    DOMAIN = "domain"
    MEMBER = "member"


@dataclass(frozen=True, slots=True)
class StatementLine:
    accession: str
    role_uri: str
    statement_kind: StatementKind
    line_number: int
    depth: int
    concept_qname: str
    label: str
    preferred_label_role: str | None
    is_abstract: bool
    is_total: bool
    values: tuple[StatementValue, ...] = ()
    parent_concept_qname: str | None = None
    line_role: LineRole = LineRole.CONCEPT


# Keyword order matters: "comprehensive income" before "income", and so on.
_KIND_KEYWORDS: tuple[tuple[StatementKind, tuple[str, ...]], ...] = (
    (StatementKind.COMPREHENSIVE_INCOME, ("comprehensiveincome", "comprehensiveloss")),
    (StatementKind.CASH_FLOW, ("cashflow",)),
    (
        StatementKind.BALANCE_SHEET,
        ("balancesheet", "financialposition", "financialcondition", "statementofcondition"),
    ),
    (
        StatementKind.EQUITY,
        ("equity", "shareholders", "stockholders", "partnerscapital", "changesinnetassets"),
    ),
    (StatementKind.INCOME_STATEMENT, ("operations", "income", "earnings", "profitorloss")),
)
# Roles whose URI names these are never primary statements.
_EXCLUDED_URI_TOKENS = (
    "parenthetical",
    "details",
    "detail",
    "tables",
    "policies",
    "disclosure",
    "narrative",
    "notes",
)
_ANCHORS: dict[StatementKind, frozenset[str]] = {
    StatementKind.BALANCE_SHEET: frozenset(
        {"Assets", "LiabilitiesAndStockholdersEquity", "EquityAndLiabilities", "AssetsCurrent"}
    ),
    StatementKind.INCOME_STATEMENT: frozenset(
        {
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "NetIncomeLoss",
            "ProfitLoss",
            "OperatingIncomeLoss",
            "EarningsPerShareBasic",
            "EarningsPerShareDiluted",
            "Revenue",
        }
    ),
    StatementKind.COMPREHENSIVE_INCOME: frozenset(
        {
            "ComprehensiveIncomeNetOfTax",
            "ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest",
            "OtherComprehensiveIncomeLossNetOfTax",
            "ComprehensiveIncome",
        }
    ),
    StatementKind.CASH_FLOW: frozenset(
        {
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
            "CashAndCashEquivalentsPeriodIncreaseDecrease",
            "CashFlowsFromUsedInOperatingActivities",
        }
    ),
    StatementKind.EQUITY: frozenset(
        {
            "StockholdersEquity",
            "StatementEquityComponentsAxis",
            "StatementOfStockholdersEquityAbstract",
        }
    ),
}


def classify_roles(bundle: XbrlExtractionBundle) -> dict[str, StatementKind]:
    """Map each primary statement role URI to its kind.

    Parenthetical, disclosure, and detail roles are excluded. Role definitions
    decide first; without them (older extractions) the role URI's last segment
    is used, and then the role must also contain an anchor concept.
    """
    return {role.role_uri: role.statement_kind for role in statement_roles(bundle)}


def statement_roles(bundle: XbrlExtractionBundle) -> list[StatementRole]:
    """Primary statement roles in the filer's order, with how each was classified."""
    trees = _presentation_children(bundle)
    definitions = {role.role_uri: role for role in bundle.roles}
    result: list[StatementRole] = []
    for role_uri in trees:
        role = definitions.get(role_uri)
        local_names = {_local_name(qname) for qname in _tree_concepts(trees[role_uri])}
        if role is not None and role.definition:
            kind = _kind_from_definition(role)
            source = "definition"
            if kind == StatementKind.COMPREHENSIVE_INCOME and _combined_income_statement(
                role, local_names
            ):
                kind = StatementKind.INCOME_STATEMENT
        else:
            kind = _kind_from_uri(role_uri)
            source = "role_uri"
        if kind is None:
            continue
        has_anchor = bool(local_names & _ANCHORS[kind])
        if source == "role_uri" and not has_anchor:
            continue
        result.append(
            StatementRole(
                role_uri=role_uri,
                statement_kind=kind,
                has_anchor=has_anchor,
                source=source,
                sort_code=role.sort_code if role is not None else None,
            )
        )
    return sorted(result, key=lambda item: (_sort_key(item.sort_code), item.role_uri))


def build_statement_lines(bundle: XbrlExtractionBundle) -> tuple[StatementLine, ...]:
    """Ordered lines for every primary statement role.

    Each role's parent-child presentation arcs are walked pre-order by arc
    `order`; `line_number` starts at 1 per role and `depth` at 0 for a root.
    Labels use the arc's preferred label role, else the standard label. Values
    are the concept's non-dimensional, first-ordinal facts, one per period.

    Hypercube scaffolding is flagged by `line_role` (table, axis, domain,
    member). A `member` line's values are the facts of the role's line item
    concepts whose only dimension is that member on the nearest enclosing axis;
    each value records its concept.
    """
    trees = _presentation_children(bundle)
    concepts = {concept.qname: concept for concept in bundle.concepts}
    values = _values_by_concept(bundle.facts)
    member_facts = _single_dimension_facts(bundle.facts)
    lines: list[StatementLine] = []
    for statement in statement_roles(bundle):
        children = trees[statement.role_uri]
        line_concepts = {
            qname
            for qname in _tree_concepts(children)
            if _line_role(concepts.get(qname), qname) == LineRole.CONCEPT
        }
        ancestors: list[tuple[int, str, LineRole]] = []
        for number, (depth, qname, parent, preferred) in enumerate(_walk(children), start=1):
            concept = concepts.get(qname)
            role = _line_role(concept, qname)
            del ancestors[depth:]
            line_values = values.get(qname, ())
            if role == LineRole.MEMBER:
                axis = next(
                    (item[1] for item in reversed(ancestors) if item[2] == LineRole.AXIS), None
                )
                line_values = (
                    _member_values(
                        member_facts,
                        _qname_key(concepts.get(axis), axis),
                        _qname_key(concept, qname),
                        line_concepts,
                    )
                    if axis is not None
                    else ()
                )
            ancestors.append((depth, qname, role))
            lines.append(
                StatementLine(
                    accession=bundle.filing.accession,
                    role_uri=statement.role_uri,
                    statement_kind=statement.statement_kind,
                    line_number=number,
                    depth=depth,
                    concept_qname=qname,
                    label=_label(concept, preferred, qname),
                    preferred_label_role=preferred,
                    is_abstract=(
                        concept.abstract
                        if concept is not None
                        else _local_name(qname).endswith("Abstract")
                    ),
                    is_total=preferred == TOTAL_LABEL_ROLE,
                    values=line_values,
                    parent_concept_qname=parent,
                    line_role=role,
                )
            )
    return tuple(lines)


# ---------------------------------------------------------------------------


_Arc = tuple[Decimal, str, str | None]  # (order, child qname, preferred label)


def _presentation_children(bundle: XbrlExtractionBundle) -> dict[str, dict[str, list[_Arc]]]:
    trees: dict[str, dict[str, list[_Arc]]] = defaultdict(lambda: defaultdict(list))
    for arc in bundle.relationships:
        if (
            arc.relationship_type != RelationshipType.PRESENTATION
            or arc.arc_role_uri != PARENT_CHILD_ARCROLE
        ):
            continue
        trees[arc.link_role_uri][arc.from_qname].append(
            (arc.order, arc.to_qname, arc.preferred_label)
        )
    return {
        role: {
            parent: sorted(arcs, key=lambda item: (item[0], item[1]))
            for parent, arcs in tree.items()
        }
        for role, tree in trees.items()
    }


def _tree_concepts(children: dict[str, list[_Arc]]) -> set[str]:
    return set(children) | {child for arcs in children.values() for _, child, _ in arcs}


def _walk(
    children: dict[str, list[_Arc]],
) -> Iterable[tuple[int, str, str | None, str | None]]:
    """Pre-order (depth, qname, parent, preferred label); roots sorted by qname.

    An arc that would revisit a concept on the current path is skipped, so
    cyclic presentation arcs terminate.
    """
    targets = {child for arcs in children.values() for _, child, _ in arcs}
    roots = sorted(parent for parent in children if parent not in targets)

    def visit(
        qname: str, depth: int, parent: str | None, preferred: str | None, path: frozenset[str]
    ) -> Iterable[tuple[int, str, str | None, str | None]]:
        yield depth, qname, parent, preferred
        for _, child, child_preferred in children.get(qname, ()):
            if child not in path:  # guard against cyclic presentation arcs
                yield from visit(child, depth + 1, qname, child_preferred, path | {child})

    visited: set[str] = set()

    def walk_from(root: str) -> Iterable[tuple[int, str, str | None, str | None]]:
        for item in visit(root, 0, None, None, frozenset({root})):
            visited.add(item[1])
            yield item

    for root in roots:
        yield from walk_from(root)
    # A fully cyclic tree has no root; start from its unvisited parents, sorted.
    for parent in sorted(children):
        if parent not in visited:
            yield from walk_from(parent)


def _values_by_concept(facts: Iterable[XbrlFact]) -> dict[str, tuple[StatementValue, ...]]:
    by_concept: dict[str, dict[tuple[date | None, date | None, date | None], StatementValue]] = (
        defaultdict(dict)
    )
    for fact in facts:
        if fact.dimensions or fact.duplicate_ordinal != 1:
            continue
        period = (fact.period_start, fact.period_end, fact.period_instant)
        by_concept[fact.concept_qname].setdefault(
            period,
            StatementValue(
                fact_id=fact.fact_id,
                period_start=fact.period_start,
                period_end=fact.period_end,
                period_instant=fact.period_instant,
                value_decimal=fact.value_decimal,
                concept_qname=fact.concept_qname,
            ),
        )
    return {qname: _latest_first(periods.values()) for qname, periods in by_concept.items()}


def _latest_first(values: Iterable[StatementValue]) -> tuple[StatementValue, ...]:
    return tuple(
        sorted(
            values,
            key=lambda value: (
                value.period_instant or value.period_end or date.min,
                value.period_start or date.min,
                value.concept_qname or "",
            ),
            reverse=True,
        )
    )


_MemberKey = tuple[tuple[str, str], tuple[str, str]]  # ((axis ns, name), (member ns, name))


def _single_dimension_facts(facts: Iterable[XbrlFact]) -> dict[_MemberKey, list[XbrlFact]]:
    """First-ordinal facts whose only dimension is one explicit axis/member pair."""
    result: dict[_MemberKey, list[XbrlFact]] = defaultdict(list)
    for fact in facts:
        if len(fact.dimensions) != 1 or fact.duplicate_ordinal != 1:
            continue
        (dimension,) = fact.dimensions
        if dimension.member_name is None:
            continue  # typed dimensions have no member concept on the statement
        key = (
            (dimension.axis_namespace, dimension.axis_name),
            (dimension.member_namespace or "", dimension.member_name),
        )
        result[key].append(fact)
    return result


def _member_values(
    member_facts: dict[_MemberKey, list[XbrlFact]],
    axis: tuple[str, str],
    member: tuple[str, str],
    line_concepts: set[str],
) -> tuple[StatementValue, ...]:
    selected: dict[tuple[str, date | None, date | None, date | None], StatementValue] = {}
    for key, facts in member_facts.items():
        if not (_same_concept(key[0], axis) and _same_concept(key[1], member)):
            continue
        for fact in facts:
            if fact.concept_qname not in line_concepts:
                continue
            period = (fact.concept_qname, fact.period_start, fact.period_end, fact.period_instant)
            selected.setdefault(
                period,
                StatementValue(
                    fact_id=fact.fact_id,
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    period_instant=fact.period_instant,
                    value_decimal=fact.value_decimal,
                    concept_qname=fact.concept_qname,
                ),
            )
    return _latest_first(selected.values())


def _qname_key(concept: XbrlConcept | None, qname: str) -> tuple[str, str]:
    """(namespace, local name); the namespace is "" when the concept isn't in the bundle."""
    if concept is not None:
        return (concept.namespace, concept.name)
    return ("", _local_name(qname))


def _same_concept(left: tuple[str, str], right: tuple[str, str]) -> bool:
    if left[1] != right[1]:
        return False
    return not left[0] or not right[0] or left[0] == right[0]


_HYPERCUBE_GROUPS = ("hypercubeitem",)
_DIMENSION_GROUPS = ("dimensionitem",)


def _line_role(concept: XbrlConcept | None, qname: str) -> LineRole:
    """Classify a presented concept by substitution group and type, else by name."""
    group = (concept.substitution_group_qname or "").lower() if concept is not None else ""
    datatype = (concept.datatype_qname or "").lower() if concept is not None else ""
    name = _local_name(qname)
    if group.endswith(_HYPERCUBE_GROUPS):
        return LineRole.TABLE
    if group.endswith(_DIMENSION_GROUPS):
        return LineRole.AXIS
    if datatype.endswith("domainitemtype"):
        return LineRole.DOMAIN if name.endswith("Domain") else LineRole.MEMBER
    if concept is None or not (group or datatype):
        for suffix, role in (
            ("Table", LineRole.TABLE),
            ("Axis", LineRole.AXIS),
            ("Domain", LineRole.DOMAIN),
            ("Member", LineRole.MEMBER),
        ):
            if name.endswith(suffix):
                return role
    return LineRole.CONCEPT


def _label(concept: XbrlConcept | None, preferred: str | None, qname: str) -> str:
    if concept is not None:
        for role in (preferred, STANDARD_LABEL_ROLE):
            if role is None:
                continue
            texts = [label for label in concept.labels if label.role_uri == role]
            for language in ("en-US", "en"):
                match = next((label.text for label in texts if label.language == language), None)
                if match:
                    return match
            if texts:
                return texts[0].text
    return _local_name(qname)


def _kind_from_definition(role: XbrlRole) -> StatementKind | None:
    if role.category != "statement":
        return None
    parts = [part.strip() for part in (role.definition or "").split(" - ")]
    title = parts[-1] if parts else ""
    if "parenthetical" in title.lower():
        return None
    return _kind_from_text(title)


_INCOME_TITLE_TOKENS = ("operations", "income", "earnings")
_INCOME_STATEMENT_ANCHORS = frozenset(
    {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "NetIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
        "Revenue",
        "ProfitLoss",
    }
)


def _combined_income_statement(role: XbrlRole, local_names: set[str]) -> bool:
    """A "Statements of Operations and Comprehensive Income" role is an income statement.

    The title must name operations/income/earnings outside "comprehensive income"
    and the role must hold income-statement anchors; CI-only titles stay CI.
    """
    title = (role.definition or "").split(" - ")[-1]
    compact = re.sub(r"[^a-z]", "", title.lower())
    remainder = compact.replace("comprehensiveincome", "").replace("comprehensiveloss", "")
    return any(token in remainder for token in _INCOME_TITLE_TOKENS) and bool(
        local_names & _INCOME_STATEMENT_ANCHORS
    )


def _kind_from_uri(role_uri: str) -> StatementKind | None:
    segment = role_uri.rstrip("/").rsplit("/", 1)[-1]
    compact = re.sub(r"[^a-z]", "", segment.lower())
    if any(token in compact for token in _EXCLUDED_URI_TOKENS):
        return None
    return _kind_from_text(segment)


def _kind_from_text(text: str) -> StatementKind | None:
    compact = re.sub(r"[^a-z]", "", text.lower())
    for kind, keywords in _KIND_KEYWORDS:
        if any(keyword in compact for keyword in keywords):
            return kind
    return None


def _local_name(qname: str) -> str:
    return qname.rsplit(":", 1)[-1]


def _sort_key(sort_code: str | None) -> tuple[int, int]:
    return (0, int(sort_code)) if sort_code and sort_code.isdigit() else (1, 0)
