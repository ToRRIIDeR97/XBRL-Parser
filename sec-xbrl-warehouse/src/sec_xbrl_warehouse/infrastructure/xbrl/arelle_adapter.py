from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import multiprocessing
import re
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import BaseHandler

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
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
    XbrlRole,
    XbrlUnit,
)
from sec_xbrl_warehouse.exceptions import XbrlExtractionError
from sec_xbrl_warehouse.infrastructure.xbrl import sec_transforms
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle, write_json
from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
    TaxonomyPackage,
    check_package,
    coverage_gaps,
    registered_packages,
    uri_scheme_variants,
)
from sec_xbrl_warehouse.validation.xbrl_validation import ArelleIssueNormalizer

# ponytail: Arelle's legacy registries are process-global; use worker processes for parallelism.
_ARELLE_LOCK = threading.RLock()

_LABEL_ARCROLE = "http://www.xbrl.org/2003/arcrole/concept-label"
_REFERENCE_ARCROLE = "http://www.xbrl.org/2003/arcrole/concept-reference"
_DOCUMENTATION_ROLE = "http://www.xbrl.org/2003/role/documentation"
_PRESENTATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
_CALCULATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
_DEFINITION_ARCROLES = (
    "http://xbrl.org/int/dim/arcrole/all",
    "http://xbrl.org/int/dim/arcrole/notAll",
    "http://xbrl.org/int/dim/arcrole/dimension-domain",
    "http://xbrl.org/int/dim/arcrole/dimension-default",
    "http://xbrl.org/int/dim/arcrole/domain-member",
    "http://www.xbrl.org/2003/arcrole/requires-element",
    "http://www.xbrl.org/2003/arcrole/general-special",
    "http://www.xbrl.org/2003/arcrole/essence-alias",
    "http://www.xbrl.org/2003/arcrole/similar-tuples",
    "http://xbrl.org/int/dim/arcrole/hypercube-dimension",
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
        """Keep the parser's model in a killable process with a finite lifetime."""
        if not entrypoint.is_file():
            raise FileNotFoundError(entrypoint)
        self.settings.ensure_directories()
        with tempfile.TemporaryDirectory(prefix="arelle-", dir=self.settings.temp_dir) as directory:
            output = Path(directory) / "bundle.json"
            error_path = Path(directory) / "error.json"
            process = multiprocessing.get_context("spawn").Process(
                target=_extract_child,
                args=(
                    self.settings,
                    entrypoint.resolve(),
                    filing,
                    extraction_revision,
                    output,
                    error_path,
                ),
            )
            process.start()
            try:
                process.join(self.settings.arelle_timeout_seconds)
                if process.is_alive():
                    raise XbrlExtractionError("Arelle exceeded its extraction time budget")
                if error_path.is_file():
                    error = json.loads(error_path.read_bytes())
                    failure = XbrlExtractionError(error["message"])
                    failure.validation_issues = tuple(
                        ValidationIssue.model_validate(item) for item in error["issues"]
                    )
                    raise failure
                if process.exitcode != 0 or not output.is_file():
                    raise XbrlExtractionError(
                        f"Arelle process failed (exit {process.exitcode}); check its memory budget"
                    )
                return read_bundle(output, self.settings.extraction_max_bytes)
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                    if process.is_alive():
                        process.kill()
                        process.join()
                process.close()

    def _extract(
        self, entrypoint: Path, filing: FilingRef, extraction_revision: int
    ) -> XbrlExtractionBundle:
        if not entrypoint.is_file():
            raise FileNotFoundError(entrypoint)
        controller, log_records = self._create_controller()
        model_xbrl = None
        try:
            model_xbrl = self._load_model(controller, entrypoint)
            if model_xbrl.modelDocument is None:
                raise XbrlExtractionError(f"Arelle did not load a model document for {entrypoint}")
            _require_complete_dts(log_records)
            if self.settings.arelle_validate_xbrl or self.settings.arelle_validate_efm:
                from arelle import Validate

                Validate.validate(model_xbrl)
            metadata = self._extract_document_metadata(model_xbrl, entrypoint)
            contexts = self._extract_contexts(model_xbrl)
            contexts_by_id = {
                (context.source_document, context.context_id): context for context in contexts
            }
            concepts = self._extract_concepts(model_xbrl)
            facts = self._extract_facts(model_xbrl, contexts_by_id, accession=filing.accession)
            footnotes = self._extract_footnotes(model_xbrl, facts)
            links: dict[str, list[str]] = {}
            for footnote in footnotes:
                for fact_id in footnote.linked_fact_ids:
                    links.setdefault(fact_id, []).append(footnote.footnote_id)
            facts = tuple(
                fact.model_copy(update={"footnote_ids": tuple(sorted(links.get(fact.fact_id, ())))})
                for fact in facts
            )
            relationships = self._extract_relationships(model_xbrl, facts, footnotes)
            roles = self._extract_roles(model_xbrl, relationships)
            issues = self._extract_validation_issues(log_records, filing)
            metadata = metadata.model_copy(
                update={
                    "provenance": {
                        **metadata.provenance,
                        "validation_log": log_records,
                    }
                }
            )
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
                roles=roles,
            )
        except Exception as exc:
            # Retain engine diagnostics even when no bundle can be produced.
            error = exc if isinstance(exc, XbrlExtractionError) else XbrlExtractionError(str(exc))
            error.validation_issues = self._extract_validation_issues(log_records, filing)
            if error is exc:
                raise
            raise error from exc
        finally:
            if model_xbrl is not None:
                with contextlib.suppress(Exception):
                    model_xbrl.close()
            with contextlib.suppress(Exception):
                controller.logger.removeHandler(controller._warehouse_capture)
                controller.close()

    def _create_controller(self) -> tuple[Any, list[dict[str, Any]]]:
        """Create a headless Arelle controller and structured log sink."""
        from arelle import Cntlr, PackageManager, PluginManager
        from arelle.ModelFormulaObject import FormulaOptions
        from arelle.ModelValue import qname

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
        controller.modelManager.formulaOptions = FormulaOptions()
        controller.logger.setLevel(logging.DEBUG)
        cache = controller.webCache
        cache.cacheDir = str(sync_cache.resolve())
        cache.workOffline = self.settings.arelle_offline
        cache.httpUserAgent = self.settings.sec_user_agent
        cache.timeout = self.settings.sec_read_timeout_seconds

        records: list[dict[str, Any]] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                refs = getattr(record, "refs", ()) or ()
                location = refs[0] if refs and isinstance(refs[0], dict) else {}
                records.append(
                    {
                        "level": record.levelname.lower(),
                        "name": record.name,
                        "code": getattr(record, "messageCode", None)
                        or _extract_codes(record.getMessage()),
                        "message": record.getMessage(),
                        "file": location.get("href"),
                        "line": location.get("sourceLine"),
                        "column": location.get("sourceColumn"),
                        "details": {"refs": _json_safe(refs), "args": _json_safe(record.args)},
                    }
                )

        capture = _Capture()
        capture.setLevel(logging.DEBUG)
        controller.logger.addHandler(capture)
        controller._warehouse_capture = capture
        try:
            PluginManager.init(controller, loadPluginConfig=False)
            plugins = ("inlineXbrlDocumentSet", *self.settings.arelle_plugins)
            if self.settings.arelle_validate_efm:
                plugins = (*plugins, "validate/EFM")
            for plugin in dict.fromkeys(plugins):
                if PluginManager.addPluginModule(plugin) is None:
                    raise XbrlExtractionError(f"Cannot load configured Arelle plugin: {plugin}")
            # The SEC `ixt-sec` registry is always on; EDGAR filings depend on it.
            if PluginManager.addPluginModule(str(Path(sec_transforms.__file__).resolve())) is None:
                raise XbrlExtractionError("Cannot load the SEC inline transformation registry")
            controller.modelManager.customTransforms = None
            controller.modelManager.loadCustomTransforms()
            loaded = controller.modelManager.customTransforms or {}
            missing = sorted(
                name
                for name in sec_transforms.SEC_TRANSFORMS
                if qname(sec_transforms.SEC_TRANSFORM_NAMESPACE, f"ixt-sec:{name}") not in loaded
            )
            if missing:
                raise XbrlExtractionError(
                    f"SEC inline transformation registry is incomplete: missing {missing}"
                )
            _restrict_taxonomy_access(cache, self.settings)
            PackageManager.init(controller, loadPackagesConfig=False)
            missing_packages: list[str] = []
            strict = self.settings.arelle_offline or self.settings.env == "production"
            packages = registered_packages(self.settings)
            gaps = coverage_gaps(self.settings, packages)
            if gaps and strict:
                raise XbrlExtractionError(
                    f"No pinned taxonomy source for {gaps}; see `xbrl-warehouse taxonomy status`"
                )
            prefix_sources: list[TaxonomyPackage] = []
            for package in packages:
                status = check_package(package)
                if status.state == "disabled":
                    continue
                if status.state == "mapping_missing":
                    raise XbrlExtractionError(
                        f"Taxonomy source {package.package_id}: {status.detail}"
                    )
                if status.state == "checksum_mismatch":
                    raise XbrlExtractionError(
                        f"Taxonomy package checksum mismatch: {package.filename}"
                    )
                if status.state == "missing" and not strict:
                    # Development hosts may run before `taxonomy sync`; the taxonomy
                    # then resolves through the allowlisted web cache.
                    missing_packages.append(package.package_id)
                    continue
                if status.state != "ok" or package.path is None:
                    raise XbrlExtractionError(
                        f"Missing or unsafe taxonomy package: {package.filename}"
                    )
                if package.kind != "package":
                    prefix_sources.append(package)
                elif PackageManager.addPackage(controller, str(package.path)) is None:
                    raise XbrlExtractionError(
                        f"Arelle rejected taxonomy package: {package.filename}"
                    )
            PackageManager.rebuildRemappings(controller)
            _register_prefix_sources(PackageManager.packagesConfig, prefix_sources)
            if missing_packages:
                records.append(
                    {
                        "level": "warning",
                        "name": "sec_xbrl_warehouse",
                        "code": "warehouse:taxonomyPackageMissing",
                        "message": (
                            f"{len(missing_packages)} registered taxonomy packages are not "
                            "installed; run `xbrl-warehouse taxonomy sync`"
                        ),
                        "file": None,
                        "line": None,
                        "column": None,
                        "details": {"package_ids": missing_packages},
                    }
                )
            if self.settings.arelle_validate_efm:
                if not controller.modelManager.disclosureSystem.select("efm-pragmatic"):
                    raise XbrlExtractionError("The EFM disclosure system is not available")
                controller.modelManager.validateDisclosureSystem = True
        except Exception:
            controller.logger.removeHandler(capture)
            controller.close()
            raise
        return controller, records

    def _load_model(self, controller: Any, entrypoint: Path) -> Any:
        """Load the local entrypoint and return `ModelXbrl`."""
        from arelle.FileSource import FileSource
        from arelle.UrlUtil import IXDS_DOC_SEPARATOR, IXDS_SURROGATE

        controller.webCache._filing_root = entrypoint.parent.resolve()
        source = str(entrypoint)
        if entrypoint.suffix.lower() in {".htm", ".html"}:
            # Only inline XBRL documents join the document set; exhibits and
            # rendered pages in the same directory are not XBRL inputs.
            documents = sorted(
                path
                for path in entrypoint.parent.iterdir()
                if path.suffix.lower() in {".htm", ".html"}
                and (path == entrypoint or _declares_inline_xbrl(path))
            )
            if len(documents) > 1:
                source = str(entrypoint.parent / IXDS_SURROGATE) + IXDS_DOC_SEPARATOR.join(
                    map(str, documents)
                )
        file_source = FileSource(source)
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
            qname = getattr(fact, "qname", None)
            qname_ns = _qname_attribute(qname, "namespaceURI")
            if qname is not None and qname_ns:
                namespaces.add(str(qname_ns))
        sorted_namespaces = tuple(sorted(namespaces))
        source_documents = sorted(
            str(url) if urlsplit(str(url)).scheme in {"http", "https"} else Path(url).name
            for url, doc in (model_xbrl.urlDocs or {}).items()
            if doc is not None
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
            provenance={
                "validate_xbrl": self.settings.arelle_validate_xbrl,
                "validate_efm": self.settings.arelle_validate_efm,
                "offline": self.settings.arelle_offline,
                "plugins": self.settings.arelle_plugins,
                "transform_registries": _transform_registries(model_xbrl),
                "taxonomy_packages": load_yaml(
                    self.settings.config_dir / "taxonomy_packages.yml"
                ).get("packages", [])
                if (self.settings.config_dir / "taxonomy_packages.yml").is_file()
                else [],
            },
        )

    def _extract_metadata(self, model_xbrl: Any, entrypoint: Path) -> XbrlDocumentMetadata:
        """Compatibility alias for `_extract_document_metadata`."""
        return self._extract_document_metadata(model_xbrl, entrypoint)

    def _extract_contexts(self, model_xbrl: Any) -> tuple[XbrlContext, ...]:
        contexts: list[XbrlContext] = []
        context_dict = dict(getattr(model_xbrl, "contexts", {}) or {})
        for fact in model_xbrl.factsInInstance:
            if getattr(fact, "context", None) is not None:
                context_dict[id(fact.context)] = fact.context
        unique = {id(context): context for context in context_dict.values()}
        for context in unique.values():
            entity_scheme, entity_identifier = _context_entity(context)
            period_kind, start, end, instant = _context_period(context)
            dimensions = _context_dimensions(context)
            segment_xml = _child_xml(context, "segment")
            scenario_xml = _child_xml(context, "scenario")
            context_hash = _context_semantic_hash(
                entity_scheme=entity_scheme,
                entity_identifier=entity_identifier,
                period_kind=period_kind.value,
                period_start=start,
                period_end=end,
                period_instant=instant,
                dimensions=dimensions,
                segment_xml=segment_xml,
                scenario_xml=scenario_xml,
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
                    segment_xml=segment_xml,
                    scenario_xml=scenario_xml,
                    source_document=_source_document(context),
                )
            )
        return tuple(
            sorted(contexts, key=lambda context: (context.source_document, context.context_id))
        )

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
            lang = getattr(resource, "xmlLang", None) or getattr(resource, "lang", None)
            if role == _DOCUMENTATION_ROLE:
                documentation_map[key] = text
                continue
            label_map.setdefault(key, []).append(
                ConceptLabel(role_uri=role, language=lang or "en", text=text)
            )
        for rel in model_xbrl.relationshipSet(_REFERENCE_ARCROLE).modelRelationships:
            concept, resource = rel.fromModelObject, rel.toModelObject
            if getattr(concept, "qname", None) is None or resource is None:
                continue
            reference_map.setdefault(concept.qname.clarkNotation, []).append(
                {
                    "role": getattr(resource, "role", None),
                    "parts": [
                        {"name": str(part.tag), "value": "".join(part.itertext())}
                        for part in resource
                    ],
                    "xml": _xml(resource),
                }
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
                    substitution_group_qname=str(
                        getattr(concept, "substitutionGroupQname", "") or ""
                    )
                    or None,
                    period_type=getattr(concept, "periodType", None) or None,
                    balance=getattr(concept, "balance", None) or None,
                    abstract=bool(concept.isAbstract),
                    nillable=bool(getattr(concept, "isNillable", False)),
                    is_extension=namespace not in standard_namespaces,
                    labels=labels,
                    documentation=documentation_map.get(qname.clarkNotation),
                    references=tuple(reference_map.get(qname.clarkNotation, [])),
                )
            )
        return tuple(sorted(concepts, key=lambda concept: (concept.namespace, concept.name)))

    def _extract_facts(
        self,
        model_xbrl: Any,
        contexts_by_id: dict[tuple[str, str], XbrlContext],
        *,
        accession: str = "",
    ) -> tuple[XbrlFact, ...]:
        facts: list[XbrlFact] = []
        for fact in model_xbrl.factsInInstance:
            concept = fact.concept
            if concept is None:
                raise XbrlExtractionError(f"Fact {fact.qname} has no resolved taxonomy concept")
            context_object = getattr(fact, "context", None)
            context = contexts_by_id.get(
                (
                    _document_name(getattr(context_object, "modelDocument", None)),
                    str(fact.contextID),
                )
            )
            if context is None and not getattr(fact, "isTuple", False):
                raise XbrlExtractionError(
                    f"Fact {fact.qname} refers to missing context {fact.contextID}"
                )
            raw_value = _fact_raw_value(fact)
            is_nil = bool(getattr(fact, "isNil", False))
            is_numeric = bool(getattr(fact, "isNumeric", False))
            transformed = getattr(fact, "value", raw_value)
            # A failed inline transformation yields Arelle's error sentinel. Arelle
            # has already logged the diagnostic; keep raw_value and store no typed value.
            failed = not is_nil and _is_transform_error(fact, transformed)
            value_kind, typed = _coerce_fact_value(
                None if failed else str(transformed) if not is_nil else raw_value,
                is_nil=is_nil,
                is_numeric=is_numeric,
                base_xsd_type=str(
                    getattr(concept, "baseXsdType", None) or getattr(concept, "typeQname", "")
                ),
            )
            if failed and is_numeric:
                value_kind = FactValueKind.NUMERIC
            unit = _fact_unit(fact)
            dimension_hash = _context_dimension_hash(context)
            fact_qname = fact.qname
            facts.append(
                XbrlFact(
                    fact_id="",  # assigned after duplicate grouping
                    concept_namespace=str(getattr(fact_qname, "namespaceURI", "") or ""),
                    concept_name=str(getattr(fact_qname, "localName", "") or ""),
                    concept_qname=str(fact_qname),
                    # The concept's own namespace. `concept.namespaceURI` is the
                    # namespace of its xs:element declaration (XML Schema, "2001").
                    taxonomy_version=_taxonomy_version(
                        str(getattr(fact_qname, "namespaceURI", "") or ""), model_xbrl
                    ),
                    context_id=str(fact.contextID or ""),
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
                    language=getattr(fact, "xmlLang", None) or None,
                    period_kind=context.period_kind if context else PeriodKind.FOREVER,
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
                    source_document=_source_document(fact),
                    source_fact_id=getattr(fact, "id", None) or None,
                    source_attributes={
                        **dict(fact.attrib),
                        "source_path": fact.getroottree().getpath(fact),
                    },
                )
            )
        facts = _assign_fact_identity(facts, accession=accession)
        return tuple(facts)

    def _extract_relationships(
        self,
        model_xbrl: Any,
        facts: tuple[XbrlFact, ...],
        footnotes: tuple[XbrlFootnote, ...],
    ) -> tuple[XbrlRelationship, ...]:
        relationships: list[XbrlRelationship] = []
        fact_ids_by_source = _fact_ids_by_source(facts)
        footnote_ids_by_resource = _footnote_ids_by_resource(model_xbrl, fact_ids_by_source)
        known_footnotes = {footnote.footnote_id for footnote in footnotes}
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
                if relationship_type is RelationshipType.FOOTNOTE:
                    source = (
                        _source_document(from_obj),
                        from_obj.getroottree().getpath(from_obj),
                    )
                    fact_id = fact_ids_by_source.get(source)
                    footnote_id = footnote_ids_by_resource.get(id(to_obj))
                    if fact_id is None or footnote_id not in known_footnotes:
                        raise XbrlExtractionError("Footnote relationship lost its fact/resource")
                    from_name, to_name = f"fact:{fact_id}", f"footnote:{footnote_id}"
                elif relationship_type in {RelationshipType.LABEL, RelationshipType.REFERENCE}:
                    if getattr(from_obj, "qname", None) is None:
                        continue
                    from_name = str(from_obj.qname)
                    to_name = _resource_endpoint(relationship_type, to_obj)
                else:
                    if (
                        getattr(from_obj, "qname", None) is None
                        or getattr(to_obj, "qname", None) is None
                    ):
                        continue
                    from_name, to_name = str(from_obj.qname), str(to_obj.qname)
                relationships.append(
                    XbrlRelationship(
                        relationship_id="",  # assigned below
                        relationship_type=relationship_type,
                        link_role_uri=str(getattr(rel, "linkrole", "") or ""),
                        arc_role_uri=str(getattr(rel, "arcrole", "") or ""),
                        from_qname=from_name,
                        to_qname=to_name,
                        order=_decimal_or_default(getattr(rel, "orderDecimal", None)),
                        priority=_optional_int(getattr(rel, "priority", None)),
                        weight=_decimal_or_default(
                            getattr(rel, "weightDecimal", None), default=None
                        ),
                        preferred_label=getattr(rel, "preferredLabel", None) or None,
                        use=getattr(rel, "use", None) or None,
                        closed=getattr(rel, "isClosed", None)
                        if relationship_type == RelationshipType.DEFINITION
                        else None,
                        usable=getattr(rel, "isUsable", None)
                        if relationship_type == RelationshipType.DEFINITION
                        else None,
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
        return _extract_footnote_objects(model_xbrl, facts)

    def _extract_roles(
        self, model_xbrl: Any, relationships: tuple[XbrlRelationship, ...]
    ) -> tuple[XbrlRole, ...]:
        """Role types the filing declares or uses in its relationship networks.

        Roles declared only by standard taxonomies and not used by this filing's
        arcs are left out, so a filing's roles stay a few dozen rows.
        """
        used = {relationship.link_role_uri for relationship in relationships}
        roles: list[XbrlRole] = []
        for role_uri, role_types in sorted((getattr(model_xbrl, "roleTypes", None) or {}).items()):
            local = any(
                _is_local_document(getattr(item, "modelDocument", None)) for item in role_types
            )
            if str(role_uri) not in used and not local:
                continue
            definition = next(
                (
                    str(item.definition).strip()
                    for item in role_types
                    if getattr(item, "definition", None) and str(item.definition).strip()
                ),
                None,
            )
            used_on = sorted(
                {str(qname) for item in role_types for qname in getattr(item, "usedOns", ()) or ()}
            )
            roles.append(
                XbrlRole(
                    role_uri=str(role_uri),
                    definition=definition,
                    used_on=tuple(used_on),
                    sort_code=role_sort_code(definition),
                    category=role_category(definition),
                )
            )
        return tuple(roles)

    def _extract_validation_issues(
        self,
        log_records: Iterable[dict[str, Any]],
        filing: FilingRef,
    ) -> tuple[ValidationIssue, ...]:
        return ArelleIssueNormalizer().normalize(log_records, accession=filing.accession)

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


def _extract_child(
    settings: Settings,
    entrypoint: Path,
    filing: FilingRef,
    revision: int,
    output: Path,
    error_path: Path,
) -> None:
    try:
        # ponytail: Linux enforces address-space limits; macOS/Windows use the Compose memory limit.
        if sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (settings.arelle_memory_limit_bytes,) * 2)
        with _ARELLE_LOCK:
            bundle = ArelleExtractor(settings)._extract(entrypoint, filing, revision)
        write_json(bundle, output, settings.extraction_max_bytes)
    except Exception as exc:
        write_json(
            {"message": str(exc), "issues": getattr(exc, "validation_issues", ())},
            error_path,
            settings.extraction_max_bytes,
        )


# Arelle logs these and keeps going, silently omitting facts whose schemas
# did not load. Treat them as a failed extraction instead of a partial one.
_INCOMPLETE_DTS_CODES = frozenset(
    {"FileNotLoadable", "webCache:unsuccessfulRetrieval", "ix11.12.1.2:missingReferences"}
)


def _require_complete_dts(log_records: list[dict[str, Any]]) -> None:
    for record in log_records:
        if record.get("code") in _INCOMPLETE_DTS_CODES:
            raise XbrlExtractionError(
                f"Taxonomy did not load completely ({record['code']}): {record.get('message')}"
            )


_INLINE_XBRL_NAMESPACES = frozenset(
    {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
)


def _declares_inline_xbrl(path: Path) -> bool:
    """Return whether `path` declares the inline XBRL namespace.

    Streams the document without entity resolution or network access and stops
    at the first inline declaration. Documents that are not well-formed XML
    before any such declaration (typical of exhibits) are not inline XBRL.
    """
    from lxml import etree

    try:
        for event, item in etree.iterparse(
            str(path),
            events=("start-ns", "end"),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        ):
            if event == "start-ns":
                if item[1] in _INLINE_XBRL_NAMESPACES:
                    return True
            else:
                item.clear()
    except (etree.XMLSyntaxError, OSError):
        return False
    return False


def _register_prefix_sources(packages_config: Any, sources: list[TaxonomyPackage]) -> None:
    """Add verified archive/file mappings and http/https twins to Arelle's remappings.

    Taxonomy packages declare one scheme in their catalogs; filings use either,
    so each package prefix also maps under the other scheme. Two enabled
    sources may not claim the same prefix.
    """
    remappings: dict[str, str] = packages_config["remappings"]
    for prefix, target in list(remappings.items()):
        if prefix.startswith(("http://", "https://")):
            for variant in uri_scheme_variants(prefix):
                remappings.setdefault(variant, target)
    for source in sources:
        for prefix, target in source.remappings().items():
            if remappings.get(prefix, target) != target:
                raise XbrlExtractionError(
                    f"Taxonomy sources overlap on {prefix}: {remappings[prefix]} and {target}"
                )
            remappings[prefix] = target


_W3C_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas" / "www.w3.org"
_W3C_SCHEMAS = {
    "/2001/xml.xsd": _W3C_SCHEMA_DIR / "2001" / "xml.xsd",
    "/2001/03/xml.xsd": _W3C_SCHEMA_DIR / "2001" / "03" / "xml.xsd",
}


def _restrict_taxonomy_access(cache: Any, settings: Settings) -> None:
    """Use pinned caches first and enforce policy on initial requests and redirects."""
    original = cache.getfilename
    allowed = frozenset(settings.allowed_taxonomy_hosts)
    original_cache_path = getattr(cache, "urlToCacheFilepath", None)
    if original_cache_path is not None:

        def contained_cache_path(
            url: str, cacheDir: str | None = None, useRedirectFallback: bool = True
        ) -> str:
            result = str(original_cache_path(url, cacheDir, useRedirectFallback))
            if (
                not Path(result)
                .resolve()
                .is_relative_to(Path(cacheDir or cache.cacheDir).resolve())
            ):
                raise XbrlExtractionError("Taxonomy cache path escapes cache root")
            return result

        cache.urlToCacheFilepath = contained_cache_path

    class _TaxonomyRequests(BaseHandler):
        next_request = 0.0
        downloaded = 0
        requests = 0

        def http_request(self, request: Any) -> Any:
            parsed = urlsplit(request.full_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in allowed
                or parsed.port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise XbrlExtractionError(f"Taxonomy network access denied: {request.full_url}")
            if settings.arelle_offline:
                raise XbrlExtractionError("Network access attempted in Arelle offline mode")
            self.requests += 1
            if self.requests > settings.filing_max_members:
                raise XbrlExtractionError("Taxonomy request count exceeds configured limit")
            settings.assert_sec_identity()
            time.sleep(max(0.0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 1 / settings.sec_max_requests_per_second
            request.add_header("User-Agent", settings.sec_user_agent)
            # Arelle inflates gzip without an output bound; require unencoded responses.
            # Arelle sends its own Accept-Encoding as an unredirected header, which
            # urllib sends in preference to ordinary headers, so override both.
            request.add_header("Accept-Encoding", "identity")
            request.add_unredirected_header("Accept-Encoding", "identity")
            return request

        https_request = http_request

        def http_response(self, request: Any, response: Any) -> Any:
            limit = min(
                settings.response_max_bytes, settings.extraction_max_bytes - self.downloaded
            )
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            length = response.headers.get("Content-Length")
            try:
                declared_size = int(length) if length is not None else None
            except (TypeError, ValueError) as exc:
                response.close()
                raise XbrlExtractionError("Invalid taxonomy Content-Length") from exc
            if encoding not in {"", "identity"} or (
                declared_size is not None and (declared_size < 0 or declared_size > limit)
            ):
                response.close()
                raise XbrlExtractionError(
                    "Taxonomy response exceeds byte limit or uses compression"
                )
            original_read = response.read
            received = 0

            def read(size: int | None = -1) -> bytes:
                nonlocal received
                remaining = min(limit - received, settings.extraction_max_bytes - self.downloaded)
                amount = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
                data: bytes = original_read(amount)
                if len(data) > remaining:
                    response.close()
                    raise XbrlExtractionError("Taxonomy response exceeds configured byte limit")
                received += len(data)
                self.downloaded += len(data)
                return data

            response.read = read
            return response

        https_response = http_response

    cache.opener.add_handler(_TaxonomyRequests())

    def getfilename(
        url: str | None, base: str | None = None, *args: Any, **kwargs: Any
    ) -> str | None:
        if url is None:
            return None
        normalized = cache.normalizeUrl(url, base) if base or kwargs.get("normalize") else url
        parsed = urlsplit(normalized)
        # W3C's xml.xsd (imported by the taxonomy package schema and others) is
        # shipped with the application and never fetched.
        if parsed.scheme in {"http", "https"} and parsed.hostname == "www.w3.org":
            local_schema = _W3C_SCHEMAS.get(parsed.path)
            if local_schema is not None:
                return str(local_schema)
        if parsed.scheme not in {"http", "https"}:
            if parsed.scheme not in {"", "file"}:
                raise XbrlExtractionError(f"Unsupported taxonomy URI: {normalized}")
            local = Path(normalized.removeprefix("file://")).resolve()
            roots = (
                getattr(cache, "_filing_root", settings.temp_dir),
                settings.arelle_cache_dir,
                settings.arelle_taxonomy_packages_dir,
                _W3C_SCHEMA_DIR,
                Path(cache.builtInCacheDir).parent.parent,
            )
            if not any(local.is_relative_to(Path(root).resolve()) for root in roots):
                raise XbrlExtractionError(
                    f"Reference escapes the filing/taxonomy package: {normalized}"
                )
            result = original(normalized, None, *args, **{**kwargs, "normalize": False})
            return str(result) if result is not None else None
        if (
            parsed.hostname not in allowed
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise XbrlExtractionError(f"Unlisted taxonomy host: {parsed.hostname}")
        # HTTP namespace/schema references may resolve to approved local caches;
        # any actual network request is upgraded to HTTPS and checked again on redirect.
        for cache_root in (cache.cacheDir, cache.builtInCacheDir):
            cached = Path(
                cache.normalizeFilepath(
                    cache.urlToCacheFilepath(normalized, cacheDir=cache_root), normalized
                )
            )
            cached = cached.resolve()
            if not cached.is_relative_to(Path(cache_root).resolve()):
                raise XbrlExtractionError("Taxonomy cache path escapes cache root")
            if cached.is_file():
                return str(cached)
        if settings.arelle_offline:
            raise XbrlExtractionError(
                f"Taxonomy is not cached for offline extraction: {normalized}"
            )
        secure = parsed._replace(scheme="https", netloc=parsed.hostname).geturl()
        result = original(secure, None, *args, **{**kwargs, "normalize": False})
        if result is not None and not any(
            Path(result).resolve().is_relative_to(Path(root).resolve())
            for root in (cache.cacheDir, cache.builtInCacheDir)
        ):
            raise XbrlExtractionError("Returned taxonomy path escapes cache root")
        return str(result) if result is not None else None

    cache.getfilename = getfilename


_ROLE_SORT_CODE = re.compile(r"^\s*(\d+)")


def role_sort_code(definition: str | None) -> str | None:
    """Leading digits of a role definition ("100000 - Statement - ..." -> "100000")."""
    match = _ROLE_SORT_CODE.match(definition or "")
    return match.group(1) if match else None


def role_category(definition: str | None) -> str:
    """Middle token of the SEC "<code> - <Category> - <Title>" convention, lower-cased."""
    parts = [part.strip() for part in (definition or "").split(" - ")]
    if len(parts) >= 3 and parts[1]:
        category = parts[1].lower()
        return category if category in {"statement", "disclosure", "document"} else "other"
    return "other"


def _is_local_document(document: Any) -> bool:
    uri = str(getattr(document, "uri", "") or "")
    return bool(uri) and urlsplit(uri).scheme not in {"http", "https"}


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
        parsed = _as_date(instant)
        if parsed is None:
            raise XbrlExtractionError(f"Invalid context instant: {instant}")
        return PeriodKind.INSTANT, None, None, parsed
    if start is not None and end is not None:
        parsed_start, parsed_end = _as_date(start), _as_date(end)
        if parsed_start is None or parsed_end is None:
            raise XbrlExtractionError(f"Invalid context duration: {start} to {end}")
        return PeriodKind.DURATION, parsed_start, parsed_end, None
    raise XbrlExtractionError("Context period is neither instant, duration, nor forever")


def _context_dimensions(context: Any) -> tuple[DimensionMember, ...]:
    dimensions: list[DimensionMember] = []
    dim_values = context.qnameDims
    for qname, value in (dim_values or {}).items():
        axis_ns = str(getattr(qname, "namespaceURI", "") or "")
        axis_name = str(getattr(qname, "localName", "") or "")
        member_qname = value.memberQname if value.isExplicit else None
        dimensions.append(
            DimensionMember(
                axis_namespace=axis_ns,
                axis_name=axis_name,
                member_namespace=getattr(member_qname, "namespaceURI", None),
                member_name=getattr(member_qname, "localName", None),
                typed_value_xml=None if value.isExplicit else _xml(value.typedMember),
                context_element=getattr(value, "contextElement", None),
            )
        )
    return canonical_dimensions(dimensions)


def _child_xml(element: Any, child_name: str) -> str | None:
    for child in element.iterdescendants():
        if (
            isinstance(child.tag, str)
            and child.tag == f"{{http://www.xbrl.org/2003/instance}}{child_name}"
        ):
            return _xml(child)
    return None


def _xml(element: Any) -> str | None:
    if element is None:
        return None
    from lxml import etree

    return str(etree.tostring(element, encoding="unicode", with_tail=False))


def _document_name(document: Any) -> str:
    name = getattr(document, "basename", None)
    if name:
        return str(name)
    url = getattr(document, "url", None) or getattr(document, "filepath", None)
    if url:
        return Path(str(url)).name
    return str(getattr(document, "qname", "") or "unknown")


def _source_document(value: Any) -> str:
    document = getattr(value, "modelDocument", None)
    if document is None:
        document = getattr(value, "document", None)
    return _document_name(document)


def _is_transform_error(fact: Any, value: Any) -> bool:
    from arelle.ModelValue import INVALIDixVALUE

    return value is INVALIDixVALUE or (
        getattr(fact, "format", None) is not None and str(value) == str(INVALIDixVALUE)
    )


def _transform_registries(model_xbrl: Any) -> list[dict[str, str]]:
    """List the inline transformation registries available to this extraction."""
    from importlib.metadata import version as package_version

    from arelle.FunctionIxt import ixtNamespacesByVersion

    try:
        arelle_version = package_version("arelle-release")
    except Exception:
        arelle_version = "unknown"
    registries = [
        {
            "namespace": namespace,
            "version": f"TR{number}",
            "source": f"arelle-release {arelle_version}",
        }
        for number, namespace in sorted(ixtNamespacesByVersion.items())
    ]
    custom = getattr(model_xbrl.modelManager, "customTransforms", None) or {}
    if any(
        getattr(key, "namespaceURI", None) == sec_transforms.SEC_TRANSFORM_NAMESPACE
        for key in custom
    ):
        registries.append(
            {
                "namespace": sec_transforms.SEC_TRANSFORM_NAMESPACE,
                "version": sec_transforms.SEC_TRANSFORM_REGISTRY_VERSION,
                "source": "sec_xbrl_warehouse.infrastructure.xbrl.sec_transforms",
            }
        )
    return registries


def _fact_raw_value(fact: Any) -> str | None:
    """Return the lexical text of a fact where available."""
    if getattr(fact, "isNil", False):
        return None
    raw_value = getattr(fact, "rawValue", None)
    if raw_value is not None:
        return str(raw_value)
    return "".join(fact.itertext())


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
            value = Decimal(str(raw_value))
            # Arrow decimals cannot store INF/NaN; the as-filed lexical remains intact.
            return FactValueKind.NUMERIC, value if value.is_finite() else None
        except Exception:
            # Arelle reports invalid inline transformations in its validation log.
            # Preserve the fact and its as-filed lexical value instead of aborting the filing.
            return FactValueKind.NUMERIC, None
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
            if qname is not None and getattr(ancestor, "isTuple", False):
                path.append(f"{qname}[{ancestor.getroottree().getpath(ancestor)}]")
    except Exception:
        return None
    return "/".join(reversed(path)) or None


def _assign_fact_identity(facts: list[XbrlFact], *, accession: str = "") -> list[XbrlFact]:
    """Assign duplicate ordinals and deterministic fact IDs."""
    groups: dict[tuple[object, ...], list[XbrlFact]] = {}
    for fact in facts:
        key = (_fact_duplicate_hash(fact),)
        groups.setdefault(key, []).append(fact)
    ordered: list[XbrlFact] = []
    for group in groups.values():
        group.sort(
            key=lambda item: (
                item.source_document,
                item.source_fact_id or "",
                item.source_attributes.get("source_path", ""),
            )
        )
        duplicate_hash = _fact_duplicate_hash(group[0])
        for ordinal, fact in enumerate(group, start=1):
            fact = fact.model_copy(
                update={
                    "duplicate_ordinal": ordinal,
                    "duplicate_set_hash": duplicate_hash,
                    "fact_id": _fact_id(fact, ordinal, accession=accession),
                }
            )
            ordered.append(fact)
    ordered.sort(key=lambda item: (item.source_document, item.source_fact_id or "", item.fact_id))
    return ordered


def _fact_duplicate_hash(fact: XbrlFact) -> str:
    return semantic_hash(
        {
            "concept": [fact.concept_namespace, fact.concept_name],
            "context": fact.context_hash,
            "unit": [sorted(fact.unit.numerator_measures), sorted(fact.unit.denominator_measures)]
            if fact.unit
            else None,
            "language": fact.language,
            "tuple": fact.tuple_path,
            "period": [fact.period_start, fact.period_end, fact.period_instant],
            "dimensions": fact.dimension_hash,
        }
    )


def _fact_id(fact: XbrlFact, ordinal: int, *, accession: str = "") -> str:
    return semantic_hash(
        {
            "accession": accession,
            "concept": [fact.concept_namespace, fact.concept_name],
            "context": fact.context_hash,
            "unit": fact.unit.unit_id if fact.unit else None,
            "value": str(fact.raw_value),
            "source_document": fact.source_document,
            "source_fact_id": fact.source_fact_id,
            "source_attributes": fact.source_attributes,
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
                        relationship.model_dump(mode="json", exclude={"relationship_id"})
                    )
                }
            )
        )
    return tuple(result)


def _extract_footnote_objects(
    model_xbrl: Any, facts: tuple[XbrlFact, ...]
) -> tuple[XbrlFootnote, ...]:
    footnotes: list[XbrlFootnote] = []
    try:
        relationship_set = model_xbrl.relationshipSet(_FOOTNOTE_ARCROLE)
    except Exception:
        return ()
    by_resource: dict[Any, list[str]] = {}
    fact_ids_by_source = _fact_ids_by_source(facts)
    for rel in relationship_set.modelRelationships:
        from_fact = rel.fromModelObject
        to_resource = rel.toModelObject
        source = (_source_document(from_fact), from_fact.getroottree().getpath(from_fact))
        fact_id = fact_ids_by_source.get(source)
        if fact_id is None:
            raise XbrlExtractionError(f"Footnote links to an unextracted fact: {source}")
        by_resource.setdefault(to_resource, []).append(str(fact_id))
    for resource, fact_ids in by_resource.items():
        text = getattr(resource, "textValue", None) or ""
        html = _xml(resource)
        footnotes.append(
            XbrlFootnote(
                footnote_id=_footnote_identity(resource, fact_ids),
                role_uri=str(getattr(resource, "role", None) or ""),
                language=getattr(resource, "xmlLang", None)
                or getattr(resource, "lang", None)
                or None,
                text=str(text),
                html=html,
                linked_fact_ids=tuple(sorted(fact_ids)),
            )
        )
    return tuple(footnotes)


def _fact_ids_by_source(facts: tuple[XbrlFact, ...]) -> dict[tuple[str, str | None], str]:
    return {
        (fact.source_document, fact.source_attributes.get("source_path")): fact.fact_id
        for fact in facts
    }


def _footnote_ids_by_resource(
    model_xbrl: Any,
    fact_ids_by_source: dict[tuple[str, str | None], str],
) -> dict[int, str]:
    grouped: dict[Any, list[str]] = {}
    for rel in model_xbrl.relationshipSet(_FOOTNOTE_ARCROLE).modelRelationships:
        fact = rel.fromModelObject
        source = (_source_document(fact), fact.getroottree().getpath(fact))
        fact_id = fact_ids_by_source.get(source)
        if fact_id is None:
            raise XbrlExtractionError(f"Footnote links to an unextracted fact: {source}")
        grouped.setdefault(rel.toModelObject, []).append(fact_id)
    return {id(resource): _footnote_identity(resource, ids) for resource, ids in grouped.items()}


def _footnote_identity(resource: Any, fact_ids: Iterable[str]) -> str:
    return semantic_hash(
        {
            "text": getattr(resource, "textValue", None) or "",
            "html": _xml(resource),
            "language": getattr(resource, "xmlLang", None),
            "role": getattr(resource, "role", None),
            "facts": sorted(fact_ids),
        }
    )


def _resource_endpoint(relationship_type: RelationshipType, resource: Any) -> str:
    identity = semantic_hash(
        {
            "type": relationship_type.value,
            "role": getattr(resource, "role", None),
            "language": getattr(resource, "xmlLang", None),
            "text": getattr(resource, "textValue", None),
            "xml": _xml(resource),
        }
    )
    return f"{relationship_type.value}:{identity}"


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
    for qname in model_xbrl.qnameConcepts:
        namespace = str(qname.namespaceURI or "")
        if urlsplit(namespace).hostname in {
            "fasb.org",
            "xbrl.fasb.org",
            "xbrl.sec.gov",
            "xbrl.ifrs.org",
            "ifrs.org",
            "www.xbrl.org",
            "xbrl.org",
        }:
            standard.add(namespace)
    return standard


def _taxonomy_version(namespace: str, model_xbrl: Any) -> str | None:
    matches = re.findall(r"(?<!\d)(?:19|20)\d{2}(?:-\d{2}-\d{2})?(?!\d)", namespace)
    return matches[-1] if matches else None


def _context_semantic_hash(
    *,
    entity_scheme: str,
    entity_identifier: str,
    period_kind: str,
    period_start: date | None,
    period_end: date | None,
    period_instant: date | None,
    dimensions: tuple[DimensionMember, ...],
    segment_xml: str | None = None,
    scenario_xml: str | None = None,
) -> str:
    return semantic_hash(
        {
            "entity": [entity_scheme, entity_identifier],
            "period": [period_kind, period_start, period_end, period_instant],
            "segment_xml": segment_xml,
            "scenario_xml": scenario_xml,
            "dims": [
                [
                    dim.axis_namespace,
                    dim.axis_name,
                    dim.member_namespace,
                    dim.member_name,
                    dim.typed_value_xml,
                    dim.context_element,
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
    from sec_xbrl_warehouse.normalization.dimensions import dimensions_hash

    return dimensions_hash(context.dimensions if context else ())


def _target_role(rel: Any) -> str | None:
    return getattr(rel, "targetRole", None) or None


def _context_element(rel: Any) -> str | None:
    return getattr(rel, "contextElement", None) or None


def _arc_attributes(rel: Any) -> dict[str, Any]:
    arc = getattr(rel, "arcElement", None)
    return dict(arc.attrib) if arc is not None else {}


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
