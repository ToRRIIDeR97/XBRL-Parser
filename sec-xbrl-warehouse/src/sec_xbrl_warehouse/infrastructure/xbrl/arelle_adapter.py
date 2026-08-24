from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType, Severity
from sec_xbrl_warehouse.domain.models import FilingRef, ValidationIssue
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    DimensionMember,
    XbrlConcept,
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlFootnote,
    XbrlRelationship,
    XbrlUnit,
)
from sec_xbrl_warehouse.exceptions import XbrlExtractionError

_LABEL_ARCROLE = "http://www.xbrl.org/2003/arcrole/concept-label"
_REFERENCE_ARCROLE = "http://www.xbrl.org/2003/arcrole/concept-reference"
_DOCUMENTATION_ROLE = "http://www.xbrl.org/2003/role/documentation"
_PRESENTATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
_CALCULATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
_DEFINITION_ARCROLES = (
    "http://www.xbrl.org/2003/arcrole/dimension-domain",
    "http://www.xbrl.org/2003/arcrole/dimension-default",
    "http://www.xbrl.org/2003/arcrole/domain-member",
    "http://www.xbrl.org/2003/arcrole/requires-element",
    "http://www.xbrl.org/2003/arcrole/hypercube-dimension",
)
_FOOTNOTE_ARCROLE = "http://www.xbrl.org/2003/arcrole/fact-footnote"


class ArelleExtractor:
    """Full-fidelity XBRL/iXBRL adapter around Arelle's Python API.

    Arelle types are kept behind `Any` at this boundary because its public Python
    objects are not comprehensively typed. All values crossing into the domain
    layer must be converted to immutable Pydantic models.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract(
        self,
        entrypoint: Path,
        filing: FilingRef,
        *,
        extraction_revision: int = 1,
    ) -> XbrlExtractionBundle:
        """Load, validate, and normalize one filing package."""
        if not entrypoint.is_file():
            raise FileNotFoundError(entrypoint)
        controller, log_records = self._create_controller()
        model_xbrl = None
        try:
            model_xbrl = self._load_model(controller, entrypoint)
            if model_xbrl.modelDocument is None:
                raise XbrlExtractionError(f"Arelle did not load a model document for {entrypoint}")
            metadata = self._extract_document_metadata(model_xbrl, entrypoint)
            contexts = self._extract_contexts(model_xbrl)
            contexts_by_id = {context.context_id: context for context in contexts}
            concepts = self._extract_concepts(model_xbrl)
            facts = self._extract_facts(model_xbrl, contexts_by_id)
            footnotes = self._extract_footnotes(model_xbrl, facts)
            relationships = self._extract_relationships(model_xbrl, facts, footnotes)
            issues = self._extract_validation_issues(log_records, filing)
            return XbrlExtractionBundle(
                filing=filing,
                metadata=metadata,
                contexts=contexts,
                concepts=concepts,
                facts=facts,
                relationships=relationships,
                footnotes=footnotes,
                validation_issues=issues,
                extraction_revision=extraction_revision,
            )
        finally:
            if model_xbrl is not None:
                with contextlib.suppress(Exception):
                    model_xbrl.close()
            with contextlib.suppress(Exception):
                controller.close()

    def _create_controller(self) -> tuple[Any, list[dict[str, Any]]]:
        """Create a headless Arelle controller and structured log sink."""
        from arelle import Cntlr

        self.settings.ensure_directories()
        cache_dir = self.settings.arelle_cache_dir
        sync_cache = cache_dir / "webcache"
        sync_cache.mkdir(parents=True, exist_ok=True)
        packages_dir = self.settings.arelle_taxonomy_packages_dir
        packages_dir.mkdir(parents=True, exist_ok=True)

        controller = Cntlr.Cntlr(
            disable_persistent_config=True,
            logFileName="logToStdErr",
            logFormat="%(message)s",
        )
        controller.logger.setLevel(logging.DEBUG)
        # Arelle's default web-cache path is user-global; pin it beneath our data dir.
        try:
            if hasattr(controller, "webCacheDir"):
                controller.webCacheDir = str(sync_cache)
        except Exception:
            pass

        records: list[dict[str, Any]] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(
                    {
                        "level": record.levelname.lower(),
                        "name": record.name,
                        "code": _extract_codes(record.getMessage()),
                        "message": record.getMessage(),
                        "args": getattr(record, "args", None),
                    }
                )

        capture = _Capture()
        capture.setLevel(logging.DEBUG)
        controller.logger.addHandler(capture)
        return controller, records

    def _load_model(self, controller: Any, entrypoint: Path) -> Any:
        """Load the local entrypoint and return `ModelXbrl`."""
        from arelle.FileSource import FileSource

        file_source = FileSource(str(entrypoint))
        try:
            model_xbrl = controller.modelManager.load(filesource=file_source)
        except Exception as exc:
            with contextlib.suppress(Exception):
                file_source.close()
            raise XbrlExtractionError(f"Arelle failed to load {entrypoint}: {exc}") from exc
        if model_xbrl is None:
            raise XbrlExtractionError(f"Arelle returned no model for {entrypoint}")
        return model_xbrl

    def _extract_document_metadata(self, model_xbrl: Any, entrypoint: Path) -> XbrlDocumentMetadata:
        document = model_xbrl.modelDocument
        doc_type = str(getattr(document, "type", "unknown"))
        namespaces = {
            str(getattr(qname, "namespaceURI", ""))
            for qname in model_xbrl.qnameConcepts
            if getattr(qname, "namespaceURI", "")
        }
        for fact in model_xbrl.factsInInstance:
            qname = getattr(fact, "elementQname", None) or getattr(fact, "qname", None)
            qname_ns = _qname_attribute(qname, "namespaceURI")
            if qname is not None and qname_ns:
                namespaces.add(str(qname_ns))
        sorted_namespaces = tuple(sorted(namespaces))
        source_documents = sorted(
            Path(url).name for url, doc in (model_xbrl.urlDocs or {}).items() if doc is not None
        )
        # Arelle package version is stable across the install.
        from importlib.metadata import version as _pkg_version

        try:
            software_version = _pkg_version("arelle-release")
        except Exception:
            software_version = "unknown"
        return XbrlDocumentMetadata(
            entrypoint=entrypoint.name,
            document_type=doc_type,
            taxonomy_namespaces=sorted_namespaces,
            source_documents=tuple(source_documents),
            software_name="arelle",
            software_version=software_version,
        )

    def _extract_metadata(self, model_xbrl: Any, entrypoint: Path) -> XbrlDocumentMetadata:
        """Compatibility alias for `_extract_document_metadata`."""
        return self._extract_document_metadata(model_xbrl, entrypoint)

    def _extract_contexts(self, model_xbrl: Any) -> tuple[XbrlContext, ...]:
        contexts: list[XbrlContext] = []
        context_dict = getattr(model_xbrl, "contexts", {}) or {}
        for context in context_dict.values():
            entity_scheme, entity_identifier = _context_entity(context)
            period_kind, start, end, instant = _context_period(context)
            dimensions = _context_dimensions(context)
            context_hash = _context_semantic_hash(
                context_id=str(context.id),
                entity_scheme=entity_scheme,
                entity_identifier=entity_identifier,
                period_kind=period_kind.value,
                period_start=start,
                period_end=end,
                period_instant=instant,
                dimensions=dimensions,
            )
            contexts.append(
                XbrlContext(
                    context_id=str(context.id),
                    context_hash=context_hash,
                    entity_scheme=entity_scheme,
                    entity_identifier=entity_identifier,
                    period_kind=period_kind,
                    period_start=start,
                    period_end=end,
                    period_instant=instant,
                    dimensions=dimensions,
                    segment_xml=_child_xml(context, "segment"),
                    scenario_xml=_child_xml(context, "scenario"),
                    source_document=_document_name(context.document),
                )
            )
        return tuple(contexts)

    def _extract_concepts(self, model_xbrl: Any) -> tuple[XbrlConcept, ...]:
        concepts: list[XbrlConcept] = []
        standard_namespaces = _standard_namespaces(model_xbrl)
        label_rs = model_xbrl.relationshipSet(_LABEL_ARCROLE)
        label_map: dict[str, list[ConceptLabel]] = {}
        documentation_map: dict[str, str] = {}
        reference_map: dict[str, list[dict[str, Any]]] = {}
        for rel in label_rs.modelRelationships:
            concept = rel.fromModelObject
            resource = rel.toModelObject
            if not hasattr(concept, "localName"):
                continue
            key = concept.qname.clarkNotation
            text = getattr(resource, "textValue", None) or str(resource).strip()
            role = getattr(resource, "role", None) or ""
            lang = getattr(resource, "lang", None)
            if role == _DOCUMENTATION_ROLE:
                documentation_map[key] = text
                continue
            label_map.setdefault(key, []).append(
                ConceptLabel(role_uri=role, language=lang or "en", text=text)
            )
        for qname, concept in model_xbrl.qnameConcepts.items():
            namespace = str(getattr(qname, "namespaceURI", "") or "")
            name = str(getattr(qname, "localName", "") or getattr(concept, "localName", ""))
            labels = tuple(label_map.get(qname.clarkNotation, ()))
            concepts.append(
                XbrlConcept(
                    namespace=namespace,
                    name=name,
                    qname=str(qname),
                    taxonomy_version=_taxonomy_version(namespace, model_xbrl),
                    datatype_qname=str(getattr(concept, "typeQname", "") or "") or None,
                    substitution_group_qname=_qname_str(
                        getattr(concept, "substitutionGroup", None)
                    ),
                    period_type=getattr(concept, "periodType", None) or None,
                    balance=getattr(concept, "balance", None) or None,
                    abstract=bool(concept.abstract),
                    nillable=bool(getattr(concept, "nillable", False)),
                    is_extension=namespace not in standard_namespaces,
                    labels=labels,
                    documentation=documentation_map.get(qname.clarkNotation),
                    references=tuple(reference_map.get(qname.clarkNotation, [])),
                )
            )
        return tuple(concepts)

    def _extract_facts(
        self,
        model_xbrl: Any,
        contexts_by_id: dict[str, XbrlContext],
    ) -> tuple[XbrlFact, ...]:
        facts: list[XbrlFact] = []
        for fact in model_xbrl.factsInInstance:
            concept = fact.concept
            if concept is None:
                continue
            context = contexts_by_id.get(str(fact.contextID))
            if context is None:
                context = _fallback_context(fact, model_xbrl)
            raw_value = _fact_raw_value(fact)
            is_nil = bool(getattr(fact, "isNil", False))
            is_numeric = bool(getattr(fact, "isNumeric", False))
            value_kind, typed = _coerce_fact_value(
                raw_value,
                is_nil=is_nil,
                is_numeric=is_numeric,
                base_xsd_type=str(getattr(concept, "typeQname", None) or ""),
            )
            unit = _fact_unit(fact)
            dimension_hash = _context_dimension_hash(context)
            fact_qname = (
                getattr(fact, "elementQname", None) or getattr(fact, "qname", None) or concept.qname
            )
            facts.append(
                XbrlFact(
                    fact_id="",  # assigned after duplicate grouping
                    concept_namespace=str(getattr(fact_qname, "namespaceURI", "") or ""),
                    concept_name=str(getattr(fact_qname, "localName", "") or ""),
                    concept_qname=str(fact_qname),
                    taxonomy_version=_taxonomy_version(str(concept.namespaceURI), model_xbrl),
                    context_id=str(fact.contextID),
                    context_hash=context.context_hash if context else "",
                    unit=unit,
                    value_kind=value_kind,
                    raw_value=raw_value,
                    value_decimal=typed if isinstance(typed, Decimal) else None,
                    value_text=typed if isinstance(typed, str) else None,
                    value_boolean=typed if isinstance(typed, bool) else None,
                    value_date=typed if isinstance(typed, date) else None,
                    is_nil=is_nil,
                    decimals=_optional_int(getattr(fact, "decimals", None)),
                    precision=_optional_int(getattr(fact, "precision", None)),
                    language=getattr(fact, "lang", None) or None,
                    period_kind=context.period_kind if context else PeriodKind.INSTANT,
                    period_start=context.period_start if context else None,
                    period_end=context.period_end if context else None,
                    period_instant=context.period_instant if context else None,
                    entity_scheme=context.entity_scheme if context else "",
                    entity_identifier=context.entity_identifier if context else "",
                    dimensions=context.dimensions if context else (),
                    dimension_hash=dimension_hash,
                    tuple_path=_tuple_path(fact),
                    duplicate_ordinal=1,
                    duplicate_set_hash="",
                    footnote_ids=(),
                    source_document=_document_name(fact.document),
                    source_fact_id=getattr(fact, "id", None) or None,
                )
            )
        facts = _assign_fact_identity(facts)
        return tuple(facts)

    def _extract_relationships(
        self,
        model_xbrl: Any,
        facts: tuple[XbrlFact, ...],
        footnotes: tuple[XbrlFootnote, ...],
    ) -> tuple[XbrlRelationship, ...]:
        relationships: list[XbrlRelationship] = []
        arcroles: list[tuple[str, RelationshipType]] = [
            (_PRESENTATION_ARCROLE, RelationshipType.PRESENTATION),
            (_CALCULATION_ARCROLE, RelationshipType.CALCULATION),
            (_LABEL_ARCROLE, RelationshipType.LABEL),
            (_REFERENCE_ARCROLE, RelationshipType.REFERENCE),
            (_FOOTNOTE_ARCROLE, RelationshipType.FOOTNOTE),
        ]
        for arcrole in _DEFINITION_ARCROLES:
            arcroles.append((arcrole, RelationshipType.DEFINITION))
        for arcrole, relationship_type in arcroles:
            relationship_set = model_xbrl.relationshipSet(arcrole)
            for rel in relationship_set.modelRelationships:
                from_obj, to_obj = rel.fromModelObject, rel.toModelObject
                if not hasattr(from_obj, "qname") or not hasattr(to_obj, "qname"):
                    continue
                relationships.append(
                    XbrlRelationship(
                        relationship_id="",  # assigned below
                        relationship_type=relationship_type,
                        link_role_uri=str(getattr(rel, "linkrole", "") or ""),
                        arc_role_uri=str(getattr(rel, "arcrole", "") or ""),
                        from_qname=str(from_obj.qname),
                        to_qname=str(to_obj.qname),
                        order=_decimal_or_default(getattr(rel, "orderDecimal", None)),
                        priority=_optional_int(getattr(rel, "priority", None)),
                        weight=_decimal_or_default(getattr(rel, "weight", None), default=None),
                        preferred_label=getattr(rel, "preferredLabel", None) or None,
                        use=str(getattr(rel, "use", None)) or None,
                        closed=bool(getattr(rel, "isClosed", False)) or None,
                        usable=bool(getattr(rel, "isUsable", False)) or None,
                        target_role=_target_role(rel),
                        context_element=_context_element(rel),
                        attributes=_arc_attributes(rel),
                        source_document=_document_name(getattr(rel, "modelDocument", None)),
                    )
                )
        return _assign_relationship_ids(tuple(relationships))

    def _extract_footnotes(
        self,
        model_xbrl: Any,
        facts: tuple[XbrlFact, ...],
    ) -> tuple[XbrlFootnote, ...]:
        return tuple(_extract_footnote_objects(model_xbrl))

    def _extract_validation_issues(
        self,
        log_records: Iterable[dict[str, Any]],
        filing: FilingRef,
    ) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        for record in log_records:
            level = str(record.get("level", "warning")).lower()
            severity = {
                "info": Severity.INFO,
                "warning": Severity.WARNING,
                "error": Severity.ERROR,
                "critical": Severity.FATAL,
                "fatal": Severity.FATAL,
            }.get(level, Severity.WARNING)
            issues.append(
                ValidationIssue(
                    source="arelle",
                    code=str(record.get("code") or "ARELLE_UNCLASSIFIED"),
                    severity=severity,
                    message=str(record.get("message") or "")[:16_000],
                    accession=filing.accession,
                    source_document=None,
                    line=None,
                    column=None,
                    details={
                        "logger": record.get("name"),
                        "args": _json_safe(record.get("args")),
                    },
                )
            )
        return tuple(issues)

    def _collect_validation_issues(
        self,
        log_records: Iterable[dict[str, Any]],
        filing: FilingRef,
    ) -> tuple[ValidationIssue, ...]:
        """Compatibility entry point named in the engineering handoff."""
        return self._extract_validation_issues(log_records, filing)


# ---------------------------------------------------------------------------
# Arelle object helpers (kept in this module; never leak into the domain layer)
# ---------------------------------------------------------------------------


def _context_entity(context: Any) -> tuple[str, str]:
    identifier = getattr(context, "entityIdentifier", None)
    if isinstance(identifier, tuple) and len(identifier) == 2:
        return str(identifier[0]), str(identifier[1])
    return "", ""


def _context_period(context: Any) -> tuple[PeriodKind, date | None, date | None, date | None]:
    start = getattr(context, "startDate", None) or getattr(context, "startDatetime", None)
    end = getattr(context, "endDate", None) or getattr(context, "endDatetime", None)
    instant = getattr(context, "instantDate", None) or getattr(context, "instantDatetime", None)
    is_forever = bool(getattr(context, "isForeverPeriod", False))
    if is_forever:
        return PeriodKind.FOREVER, None, None, None
    if instant is not None:
        return PeriodKind.INSTANT, None, None, _as_date(instant)
    if start is not None and end is not None:
        return PeriodKind.DURATION, _as_date(start), _as_date(end), None
    return PeriodKind.INSTANT, None, None, None


def _context_dimensions(context: Any) -> tuple[DimensionMember, ...]:
    dimensions: list[DimensionMember] = []
    try:
        dim_values = context.dimValues()
    except Exception:
        dim_values = {}
    for qname, value in (dim_values or {}).items():
        axis_ns = str(getattr(qname, "namespaceURI", "") or "")
        axis_name = str(getattr(qname, "localname", "") or "")
        member_qname = getattr(value, "qname", None) or value
        dimensions.append(
            DimensionMember(
                axis_namespace=axis_ns,
                axis_name=axis_name,
                member_namespace=str(getattr(member_qname, "namespaceURI", "") or ""),
                member_name=str(getattr(member_qname, "localname", "") or ""),
                typed_value_xml=None,
            )
        )
    return canonical_dimensions(dimensions)


def _child_xml(element: Any, child_name: str) -> str | None:
    try:
        for child in element.iterchildren():
            if child.tag.split("}")[-1] == child_name:
                import lxml.etree as etree

                return str(etree.tostring(child, encoding="unicode"))
    except Exception:
        pass
    return None


def _document_name(document: Any) -> str:
    name = getattr(document, "basename", None)
    if name:
        return str(name)
    url = getattr(document, "url", None) or getattr(document, "filepath", None)
    if url:
        return Path(str(url)).name
    return str(getattr(document, "qname", "") or "unknown")


def _fact_raw_value(fact: Any) -> str | None:
    """Return the lexical text of a fact where available."""
    try:
        # ModelFact stores lexical on the element for non-numeric facts.
        numeric = bool(getattr(fact, "isNumeric", False))
        if not numeric:
            text = "".join(fact.itertext())
            return text.strip() or None
        return str(fact.xValue) if fact.xValue is not None else None
    except Exception:
        return None


def _coerce_fact_value(
    raw_value: str | None,
    *,
    is_nil: bool,
    is_numeric: bool,
    base_xsd_type: str,
) -> tuple[FactValueKind, Any]:
    if is_nil:
        return FactValueKind.NIL, None
    if raw_value is None:
        return FactValueKind.TEXT, None
    if is_numeric:
        try:
            return FactValueKind.NUMERIC, Decimal(str(raw_value))
        except Exception as exc:
            raise XbrlExtractionError(f"Invalid numeric lexical value: {raw_value!r}") from exc
    normalized = base_xsd_type.lower()
    if "boolean" in normalized or "bool" in normalized:
        lowered = raw_value.strip().lower()
        if lowered in {"true", "1"}:
            return FactValueKind.BOOLEAN, True
        if lowered in {"false", "0"}:
            return FactValueKind.BOOLEAN, False
    if "date" in normalized:
        try:
            return FactValueKind.DATE, date.fromisoformat(raw_value.strip()[:10])
        except ValueError:
            pass
    return FactValueKind.TEXT, raw_value


def _fact_unit(fact: Any) -> XbrlUnit | None:
    unit = getattr(fact, "unit", None)
    if unit is None:
        return None
    numerator, denominator = unit.measures
    return XbrlUnit(
        unit_id=str(unit.id),
        numerator_measures=tuple(str(measure) for measure in numerator),
        denominator_measures=tuple(str(measure) for measure in denominator),
    )


def _tuple_path(fact: Any) -> str | None:
    path: list[str] = []
    try:
        for ancestor in fact.iterancestors():
            qname = getattr(ancestor, "qname", None) or getattr(ancestor, "elementQname", None)
            if qname is not None and str(qname) != str(getattr(fact, "qname", "")):
                path.append(str(qname))
    except Exception:
        return None
    return "/".join(reversed(path)) or None


def _assign_fact_identity(facts: list[XbrlFact]) -> list[XbrlFact]:
    """Assign duplicate ordinals and deterministic fact IDs."""
    groups: dict[tuple[object, ...], list[XbrlFact]] = {}
    for fact in facts:
        key = (
            fact.concept_namespace,
            fact.concept_name,
            fact.taxonomy_version,
            fact.context_hash,
            fact.unit.unit_id if fact.unit else None,
            fact.raw_value,
            fact.period_kind.value,
            fact.period_start,
            fact.period_end,
            fact.period_instant,
            fact.dimension_hash,
            fact.tuple_path,
        )
        groups.setdefault(key, []).append(fact)
    ordered: list[XbrlFact] = []
    for group in groups.values():
        group.sort(key=lambda item: (item.source_document, item.source_fact_id or ""))
        duplicate_hash = _fact_duplicate_hash(group[0])
        for ordinal, fact in enumerate(group, start=1):
            fact = fact.model_copy(
                update={
                    "duplicate_ordinal": ordinal,
                    "duplicate_set_hash": duplicate_hash,
                    "fact_id": _fact_id(fact, ordinal),
                }
            )
            ordered.append(fact)
    ordered.sort(key=lambda item: (item.source_document, item.source_fact_id or "", item.fact_id))
    return ordered


def _fact_duplicate_hash(fact: XbrlFact) -> str:
    return semantic_hash(
        {
            "concept": fact.concept_qname,
            "context": fact.context_hash,
            "unit": fact.unit.unit_id if fact.unit else None,
            "value": str(fact.raw_value),
            "period": [fact.period_start, fact.period_end, fact.period_instant],
            "dimensions": fact.dimension_hash,
        }
    )


def _fact_id(fact: XbrlFact, ordinal: int) -> str:
    return semantic_hash(
        {
            "concept": fact.concept_qname,
            "context": fact.context_hash,
            "unit": fact.unit.unit_id if fact.unit else None,
            "value": str(fact.raw_value),
            "source_document": fact.source_document,
            "source_fact_id": fact.source_fact_id,
            "ordinal": ordinal,
        }
    )


def _assign_relationship_ids(
    relationships: tuple[XbrlRelationship, ...],
) -> tuple[XbrlRelationship, ...]:
    result = []
    for relationship in relationships:
        result.append(
            relationship.model_copy(
                update={
                    "relationship_id": semantic_hash(
                        {
                            "type": relationship.relationship_type.value,
                            "link_role": relationship.link_role_uri,
                            "arc_role": relationship.arc_role_uri,
                            "from": relationship.from_qname,
                            "to": relationship.to_qname,
                            "order": str(relationship.order),
                        }
                    )
                }
            )
        )
    return tuple(result)


def _extract_footnote_objects(model_xbrl: Any) -> tuple[XbrlFootnote, ...]:
    footnotes: list[XbrlFootnote] = []
    try:
        relationship_set = model_xbrl.relationshipSet(_FOOTNOTE_ARCROLE)
    except Exception:
        return ()
    by_resource: dict[Any, list[str]] = {}
    for rel in relationship_set.modelRelationships:
        from_fact = rel.fromModelObject
        to_resource = rel.toModelObject
        fact_id = getattr(from_fact, "id", None) or str(from_fact)
        by_resource.setdefault(to_resource, []).append(str(fact_id))
    for resource, fact_ids in by_resource.items():
        text = getattr(resource, "textValue", None) or ""
        html = None
        try:
            import lxml.etree as etree

            html = etree.tostring(resource, encoding="unicode")
        except Exception:
            pass
        footnotes.append(
            XbrlFootnote(
                footnote_id=semantic_hash({"text": text, "facts": sorted(fact_ids)}),
                role_uri=str(getattr(resource, "role", None) or ""),
                language=getattr(resource, "lang", None) or None,
                text=str(text),
                html=html,
                linked_fact_ids=tuple(sorted(fact_ids)),
            )
        )
    return tuple(footnotes)


def _standard_namespaces(model_xbrl: Any) -> set[str]:
    standard = {
        "http://www.xbrl.org/2003/instance",
        "http://www.xbrl.org/2003/linkbase",
        "http://www.xbrl.org/2003/iso4217",
        "http://www.xbrl.org/2003/xbrl-linkbase-2003-12-31",
        "http://www.xbrl.org/2003/xl-2003-12-31",
        "http://www.w3.org/2001/XMLSchema",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/XML/1998/namespace",
    }
    # Add known standard taxonomy namespaces observed on the model.
    for namespace in (
        "http://fasb.org/us-gaap",
        "http://xbrl.sec.gov/dei",
        "http://xbrl.sec.gov/srt",
        "http://xbrl.sec.gov/country",
        "http://xbrl.sec.gov/currency",
        "http://ifrs.org/taxonomy",
    ):
        if any(str(ns).startswith(namespace) for ns in model_xbrl.qnameConcepts):
            standard.add(namespace)
    return standard


def _taxonomy_version(namespace: str, model_xbrl: Any) -> str | None:
    for _, concept in model_xbrl.qnameConcepts.items():
        if str(concept.namespaceURI) == namespace:
            return getattr(concept, "version", None) or None
    return None


def _context_semantic_hash(
    *,
    context_id: str,
    entity_scheme: str,
    entity_identifier: str,
    period_kind: str,
    period_start: date | None,
    period_end: date | None,
    period_instant: date | None,
    dimensions: tuple[DimensionMember, ...],
) -> str:
    return semantic_hash(
        {
            "entity": [entity_scheme, entity_identifier],
            "period": [period_kind, period_start, period_end, period_instant],
            "dims": [
                [
                    dim.axis_namespace,
                    dim.axis_name,
                    dim.member_namespace,
                    dim.member_name,
                    dim.typed_value_xml,
                ]
                for dim in sorted(
                    dimensions,
                    key=lambda d: (
                        d.axis_namespace,
                        d.axis_name,
                        d.member_namespace or "",
                        d.member_name or "",
                    ),
                )
            ],
        }
    )


def _context_dimension_hash(context: XbrlContext | None) -> str:
    if context is None:
        return ""
    return semantic_hash(
        [
            [
                dim.axis_namespace,
                dim.axis_name,
                dim.member_namespace,
                dim.member_name,
                dim.typed_value_xml,
            ]
            for dim in context.dimensions
        ]
    )


def _fallback_context(fact: Any, model_xbrl: Any) -> XbrlContext:
    """Build a minimal context when a fact's context is missing from the map."""
    try:
        context_obj = fact.context
        entity_scheme, entity_identifier = _context_entity(context_obj)
        period_kind, start, end, instant = _context_period(context_obj)
        return XbrlContext(
            context_id=str(fact.contextID),
            context_hash="",
            entity_scheme=entity_scheme,
            entity_identifier=entity_identifier,
            period_kind=period_kind,
            period_start=start,
            period_end=end,
            period_instant=instant,
            source_document=_document_name(fact.document),
        )
    except Exception:
        return XbrlContext(
            context_id=str(fact.contextID),
            context_hash="",
            entity_scheme="",
            entity_identifier="",
            period_kind=PeriodKind.INSTANT,
            source_document=_document_name(fact.document),
        )


def _target_role(rel: Any) -> str | None:
    try:
        target = rel.get("targetRole")
        return str(target) if target else None
    except Exception:
        return None


def _context_element(rel: Any) -> str | None:
    try:
        value = rel.get("contextElement")
        return str(value) if value else None
    except Exception:
        return None


def _arc_attributes(rel: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    try:
        for key in ("weight", "order", "priority", "preferredLabel", "use", "targetRole"):
            value = rel.get(key)
            if value is not None:
                attributes[key] = value
    except Exception:
        pass
    return attributes


def _decimal_or_default(value: Any, *, default: Decimal | None = Decimal("1")) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _extract_codes(message: str) -> str:
    """Extract the first bracketed code list from an Arelle log message."""
    import re

    match = re.search(r"\[\[(.*?)\]\]", message)
    if match:
        codes = match.group(1).strip("'\"[]")
        return codes.split(",")[0] if codes else "ARELLE_UNCLASSIFIED"
    return "ARELLE_UNCLASSIFIED"


def _qname_str(value: Any) -> str | None:
    """Render an Arelle QName or lxml element without truth-testing it."""
    if value is None:
        return None
    try:
        qname = getattr(value, "qname", None)
        if qname is not None:
            return str(qname)
        tag = getattr(value, "tag", None)
        if isinstance(tag, str) and "}" in tag:
            return tag.split("}")[-1]
        return None
    except Exception:
        return None


def _qname_attribute(value: Any, name: str) -> Any:
    """Read a QName attribute safely from an `Any` Arelle object."""
    if value is None:
        return None
    attr = getattr(value, name, None)
    return attr if attr not in (None, "") else None


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def semantic_hash(payload: Any) -> str:
    """Stable SHA-256 over a JSON-compatible semantic payload."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_dimensions(
    dimensions: Iterable[DimensionMember],
) -> tuple[DimensionMember, ...]:
    from sec_xbrl_warehouse.normalization.dimensions import canonicalize_dimensions

    return canonicalize_dimensions(dimensions)


def dimension_hash(dimensions: Iterable[DimensionMember]) -> str:
    return _context_dimension_hash(
        XbrlContext(
            context_id="",
            context_hash="",
            entity_scheme="",
            entity_identifier="",
            period_kind=PeriodKind.INSTANT,
            dimensions=tuple(dimensions),
            source_document="",
        )
    )


def classify_period(
    *,
    start: date | None,
    end: date | None,
    instant: date | None,
    is_forever: bool = False,
) -> PeriodKind:
    if is_forever:
        return PeriodKind.FOREVER
    if instant is not None:
        return PeriodKind.INSTANT
    if start is not None and end is not None:
        return PeriodKind.DURATION
    raise XbrlExtractionError("Context period is neither instant, duration, nor forever")


def coerce_typed_value(
    raw_value: str | None,
    *,
    is_nil: bool,
    is_numeric: bool,
    base_xsd_type: str | None,
) -> tuple[FactValueKind, Decimal | str | bool | date | None]:
    return _coerce_fact_value(
        raw_value,
        is_nil=is_nil,
        is_numeric=is_numeric,
        base_xsd_type=base_xsd_type or "",
    )


def relationship_type_from_arcrole(arcrole: str) -> RelationshipType:
    lowered = arcrole.lower()
    if "summation-item" in lowered:
        return RelationshipType.CALCULATION
    if "parent-child" in lowered:
        return RelationshipType.PRESENTATION
    if "dimension" in lowered or "domain-member" in lowered or "hypercube" in lowered:
        return RelationshipType.DEFINITION
    if "concept-label" in lowered:
        return RelationshipType.LABEL
    if "concept-reference" in lowered:
        return RelationshipType.REFERENCE
    if "fact-footnote" in lowered:
        return RelationshipType.FOOTNOTE
    return RelationshipType.OTHER
