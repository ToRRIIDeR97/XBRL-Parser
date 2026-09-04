from __future__ import annotations

from typing import Any

from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.exceptions import WarehouseWriteError


def _pa() -> Any:
    """Import PyArrow lazily so configuration-only commands remain lightweight."""
    import pyarrow as pa

    return pa


def sec_source_schema(table: SecTable) -> Any:
    """Raw lexical schema for one SEC tab-delimited member.

    Every field is nullable UTF-8 at this stage. Typing occurs only after the
    lexical value and source line are available for diagnostics.
    """
    pa = _pa()
    from sec_xbrl_warehouse.ingestion.sec_tsv import SEC_HEADERS

    return pa.schema([pa.field(name, pa.string(), nullable=True) for name in SEC_HEADERS[table]])


def _lineage_fields(pa: Any) -> list[Any]:
    return [
        pa.field("_artifact_id", pa.string(), nullable=False),
        pa.field("_source_period", pa.string(), nullable=False),
        pa.field("_source_revision", pa.uint32(), nullable=False),
        pa.field("_row_hash", pa.string(), nullable=False),
        pa.field("_ingested_at", pa.timestamp("ms", tz="UTC"), nullable=False),
    ]


LINEAGE_COLUMNS: tuple[str, ...] = (
    "_artifact_id",
    "_source_period",
    "_source_revision",
    "_row_hash",
    "_ingested_at",
)

# NUM/TXT carry SUB-derived denormalization columns in the warehouse contract;
# the typed mid-step does not produce them.
_SUB_DERIVED_COLUMNS: dict[SecTable, tuple[str, ...]] = {
    SecTable.NUM: ("cik", "filed", "form", "fy", "fp"),
    SecTable.TXT: ("cik", "filed", "form", "fy", "fp"),
}


def sec_typed_schema(table: SecTable) -> Any:
    """Exact typed Arrow contract for one source table before enrichment.

    This is the boundary between `sec_source_schema` (raw lexical strings) and
    `sec_warehouse_schema` (enriched + lineage). Column order and types match the
    warehouse contract so enrichment can append the missing columns positionally.
    """
    pa = _pa()
    dropped = set(LINEAGE_COLUMNS) | set(_SUB_DERIVED_COLUMNS.get(table, ()))
    fields = [field for field in sec_warehouse_schema(table) if field.name not in dropped]
    return pa.schema(fields)


def sec_warehouse_schema(table: SecTable) -> Any:
    """Exact typed/enriched Arrow contract for a ClickHouse SEC bulk table."""
    pa = _pa()
    decimal = pa.decimal256(76, 18)
    schemas: dict[SecTable, list[Any]] = {
        SecTable.SUB: [
            pa.field("adsh", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("name", pa.string(), False),
            pa.field("sic", pa.uint16()),
            pa.field("countryba", pa.string(), False),
            pa.field("stprba", pa.string()),
            pa.field("cityba", pa.string(), False),
            pa.field("zipba", pa.string()),
            pa.field("bas1", pa.string()),
            pa.field("bas2", pa.string()),
            pa.field("baph", pa.string()),
            pa.field("countryma", pa.string()),
            pa.field("stprma", pa.string()),
            pa.field("cityma", pa.string()),
            pa.field("zipma", pa.string()),
            pa.field("mas1", pa.string()),
            pa.field("mas2", pa.string()),
            pa.field("countryinc", pa.string(), False),
            pa.field("stprinc", pa.string()),
            pa.field("ein", pa.string()),
            pa.field("former", pa.string()),
            pa.field("changed", pa.date32()),
            pa.field("afs", pa.string()),
            pa.field("wksi", pa.uint8(), False),
            pa.field("fye", pa.string(), False),
            pa.field("form", pa.string(), False),
            pa.field("period", pa.date32(), False),
            pa.field("fy", pa.int16(), False),
            pa.field("fp", pa.string(), False),
            pa.field("filed", pa.date32(), False),
            pa.field("accepted", pa.timestamp("ms", tz="UTC"), False),
            pa.field("prevrpt", pa.uint8(), False),
            pa.field("detail", pa.uint8(), False),
            pa.field("instance", pa.string(), False),
            pa.field("nciks", pa.uint16(), False),
            pa.field("aciks", pa.string()),
            pa.field("pubfloatusd", decimal),
            pa.field("floatdate", pa.date32()),
            pa.field("floataxis", pa.string()),
            pa.field("floatmems", pa.uint16()),
        ],
        SecTable.TAG: [
            pa.field("tag", pa.string(), False),
            pa.field("version", pa.string(), False),
            pa.field("custom", pa.uint8(), False),
            pa.field("abstract", pa.uint8(), False),
            pa.field("datatype", pa.string()),
            pa.field("iord", pa.string(), False),
            pa.field("crdr", pa.string()),
            pa.field("tlabel", pa.string()),
            pa.field("doc", pa.string()),
        ],
        SecTable.DIM: [
            pa.field("dimh", pa.string(), False),
            pa.field("segments", pa.string(), False),
            pa.field("segt", pa.uint8(), False),
        ],
        SecTable.NUM: [
            pa.field("adsh", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("filed", pa.date32(), False),
            pa.field("form", pa.string(), False),
            pa.field("fy", pa.int16()),
            pa.field("fp", pa.string(), False),
            pa.field("tag", pa.string(), False),
            pa.field("version", pa.string(), False),
            pa.field("ddate", pa.date32(), False),
            pa.field("qtrs", pa.int16(), False),
            pa.field("uom", pa.string(), False),
            pa.field("dimh", pa.string(), False),
            pa.field("iprx", pa.uint16(), False),
            pa.field("value_raw", pa.string()),
            pa.field("value_decimal", decimal),
            pa.field("footnote", pa.string()),
            pa.field("footlen", pa.uint32(), False),
            pa.field("dimn", pa.uint8(), False),
            pa.field("coreg", pa.string()),
            pa.field("durp", pa.float32(), False),
            pa.field("datp", pa.float32(), False),
            pa.field("dcml", pa.int16(), False),
        ],
        SecTable.TXT: [
            pa.field("adsh", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("filed", pa.date32(), False),
            pa.field("form", pa.string(), False),
            pa.field("fy", pa.int16()),
            pa.field("fp", pa.string(), False),
            pa.field("tag", pa.string(), False),
            pa.field("version", pa.string(), False),
            pa.field("ddate", pa.date32(), False),
            pa.field("qtrs", pa.int16(), False),
            pa.field("iprx", pa.uint16(), False),
            pa.field("lang", pa.string(), False),
            pa.field("dcml", pa.int16(), False),
            pa.field("durp", pa.float32(), False),
            pa.field("datp", pa.float32(), False),
            pa.field("dimh", pa.string(), False),
            pa.field("dimn", pa.uint8()),
            pa.field("coreg", pa.string()),
            pa.field("escaped", pa.uint8(), False),
            pa.field("srclen", pa.uint32(), False),
            pa.field("txtlen", pa.uint32()),
            pa.field("footnote", pa.string()),
            pa.field("footlen", pa.uint32()),
            pa.field("context", pa.string(), False),
            pa.field("value", pa.string(), False),
        ],
        SecTable.REN: [
            pa.field("adsh", pa.string(), False),
            pa.field("report", pa.uint32(), False),
            pa.field("rfile", pa.string(), False),
            pa.field("menucat", pa.string()),
            pa.field("shortname", pa.string(), False),
            pa.field("longname", pa.string(), False),
            pa.field("roleuri", pa.string(), False),
            pa.field("parentroleuri", pa.string()),
            pa.field("parentreport", pa.uint32()),
            pa.field("ultparentrpt", pa.uint32()),
        ],
        SecTable.PRE: [
            pa.field("adsh", pa.string(), False),
            pa.field("report", pa.uint32(), False),
            pa.field("line", pa.uint32(), False),
            pa.field("stmt", pa.string(), False),
            pa.field("inpth", pa.uint8(), False),
            pa.field("tag", pa.string(), False),
            pa.field("version", pa.string(), False),
            pa.field("prole", pa.string(), False),
            pa.field("plabel", pa.string(), False),
            pa.field("negating", pa.uint8(), False),
        ],
        SecTable.CAL: [
            pa.field("adsh", pa.string(), False),
            pa.field("grp", pa.uint16(), False),
            pa.field("arc", pa.uint16(), False),
            pa.field("negative", pa.uint8(), False),
            pa.field("ptag", pa.string(), False),
            pa.field("pversion", pa.string(), False),
            pa.field("ctag", pa.string(), False),
            pa.field("cversion", pa.string(), False),
        ],
    }
    return pa.schema([*schemas[table], *_lineage_fields(pa)])


def xbrl_warehouse_schema(entity: str) -> Any:
    """Exact Arrow contracts for full-fidelity Arelle entities."""
    pa = _pa()
    decimal = pa.decimal256(76, 18)
    common_revision = [
        pa.field("extraction_revision", pa.uint32(), False),
        pa.field("_row_hash", pa.string(), False),
        pa.field("_ingested_at", pa.timestamp("ms", tz="UTC"), False),
    ]
    schemas: dict[str, list[Any]] = {
        "contexts": [
            pa.field("accession", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("context_id", pa.string(), False),
            pa.field("context_hash", pa.string(), False),
            pa.field("entity_scheme", pa.string(), False),
            pa.field("entity_identifier", pa.string(), False),
            pa.field("period_kind", pa.string(), False),
            pa.field("period_start", pa.date32()),
            pa.field("period_end", pa.date32()),
            pa.field("period_instant", pa.date32()),
            pa.field("dimensions_json", pa.string(), False),
            pa.field("segment_xml", pa.string()),
            pa.field("scenario_xml", pa.string()),
            pa.field("source_document", pa.string(), False),
            *common_revision,
        ],
        "concepts": [
            pa.field("accession", pa.string(), False),
            pa.field("concept_namespace", pa.string(), False),
            pa.field("concept_name", pa.string(), False),
            pa.field("concept_qname", pa.string(), False),
            pa.field("taxonomy_version", pa.string()),
            pa.field("datatype_qname", pa.string()),
            pa.field("substitution_group_qname", pa.string()),
            pa.field("period_type", pa.string()),
            pa.field("balance", pa.string()),
            pa.field("abstract", pa.uint8(), False),
            pa.field("nillable", pa.uint8(), False),
            pa.field("is_extension", pa.uint8(), False),
            pa.field("labels_json", pa.string(), False),
            pa.field("documentation", pa.string()),
            pa.field("references_json", pa.string()),
            *common_revision,
        ],
        "facts": [
            pa.field("fact_id", pa.string(), False),
            pa.field("accession", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("company_name", pa.string(), False),
            pa.field("form", pa.string(), False),
            pa.field("filed_date", pa.date32(), False),
            pa.field("fiscal_year", pa.int16()),
            pa.field("fiscal_period", pa.string()),
            pa.field("concept_namespace", pa.string(), False),
            pa.field("concept_name", pa.string(), False),
            pa.field("concept_qname", pa.string(), False),
            pa.field("taxonomy_version", pa.string()),
            pa.field("context_id", pa.string(), False),
            pa.field("context_hash", pa.string(), False),
            pa.field("unit_id", pa.string()),
            pa.field("unit_numerator_json", pa.string()),
            pa.field("unit_denominator_json", pa.string()),
            pa.field("raw_value", pa.string()),
            pa.field("value_decimal", decimal),
            pa.field("value_text", pa.string()),
            pa.field("value_boolean", pa.uint8()),
            pa.field("value_date", pa.date32()),
            pa.field("is_nil", pa.uint8(), False),
            pa.field("decimals", pa.int32()),
            pa.field("precision", pa.int32()),
            pa.field("language", pa.string()),
            pa.field("period_kind", pa.string(), False),
            pa.field("period_start", pa.date32()),
            pa.field("period_end", pa.date32()),
            pa.field("period_instant", pa.date32()),
            pa.field("entity_scheme", pa.string(), False),
            pa.field("entity_identifier", pa.string(), False),
            pa.field("dimensions_json", pa.string(), False),
            pa.field("dimension_hash", pa.string(), False),
            pa.field("tuple_path", pa.string()),
            pa.field("duplicate_ordinal", pa.uint16(), False),
            pa.field("duplicate_set_hash", pa.string(), False),
            pa.field("footnote_ids", pa.list_(pa.string()), False),
            pa.field("source_document", pa.string(), False),
            pa.field("source_fact_id", pa.string()),
            pa.field("source_attributes_json", pa.string(), False),
            pa.field("value_kind", pa.string(), False),
            *common_revision,
        ],
        "relationships": [
            pa.field("relationship_id", pa.string(), False),
            pa.field("accession", pa.string(), False),
            pa.field("network_type", pa.string(), False),
            pa.field("link_role_uri", pa.string(), False),
            pa.field("arc_role_uri", pa.string(), False),
            pa.field("from_qname", pa.string(), False),
            pa.field("to_qname", pa.string(), False),
            pa.field("arc_order", pa.decimal128(18, 8), False),
            pa.field("priority", pa.int32()),
            pa.field("weight", pa.decimal128(18, 8)),
            pa.field("preferred_label", pa.string()),
            pa.field("use", pa.string()),
            pa.field("closed", pa.uint8()),
            pa.field("usable", pa.uint8()),
            pa.field("target_role", pa.string()),
            pa.field("context_element", pa.string()),
            pa.field("attributes_json", pa.string()),
            pa.field("source_document", pa.string()),
            *common_revision,
        ],
        "footnotes": [
            pa.field("footnote_id", pa.string(), False),
            pa.field("accession", pa.string(), False),
            pa.field("role_uri", pa.string()),
            pa.field("language", pa.string()),
            pa.field("text", pa.string(), False),
            pa.field("html", pa.string()),
            pa.field("linked_fact_ids", pa.list_(pa.string()), False),
            *common_revision,
        ],
    }
    try:
        return pa.schema(schemas[entity])
    except KeyError as exc:
        raise ValueError(f"Unknown XBRL entity schema: {entity}") from exc


def canonical_fact_schema() -> Any:
    pa = _pa()
    return pa.schema(
        [
            pa.field("canonical_fact_id", pa.string(), False),
            pa.field("raw_fact_id", pa.string(), False),
            pa.field("accession", pa.string(), False),
            pa.field("cik", pa.uint64(), False),
            pa.field("filed_date", pa.date32(), False),
            pa.field("form", pa.string(), False),
            pa.field("canonical_metric", pa.string(), False),
            pa.field("normalized_value_decimal", pa.decimal256(76, 18)),
            pa.field("normalized_value_text", pa.string()),
            pa.field("normalized_unit", pa.string()),
            pa.field("sign_multiplier", pa.int8(), False),
            pa.field("period_kind", pa.string(), False),
            pa.field("period_start", pa.date32()),
            pa.field("period_end", pa.date32()),
            pa.field("period_instant", pa.date32()),
            pa.field("dimensions_json", pa.string(), False),
            pa.field("mapping_id", pa.string(), False),
            pa.field("mapping_version", pa.uint32(), False),
            pa.field("mapping_confidence", pa.float32(), False),
            pa.field("mapping_explanation", pa.string(), False),
            pa.field("extraction_revision", pa.uint32(), False),
            pa.field("_row_hash", pa.string(), False),
            pa.field("_ingested_at", pa.timestamp("ms", tz="UTC"), False),
        ]
    )


def require_exact_schema(actual: Any, expected: Any, *, name: str) -> None:
    """Reject missing/reordered/type-incompatible Arrow fields before writing."""
    if actual.names != expected.names:
        raise WarehouseWriteError(
            f"Arrow column mismatch for {name}: expected={expected.names}, actual={actual.names}"
        )
    mismatches = [
        (actual_field.name, str(actual_field.type), str(expected_field.type))
        for actual_field, expected_field in zip(actual, expected, strict=True)
        if actual_field.type != expected_field.type
        or actual_field.nullable != expected_field.nullable
    ]
    if mismatches:
        raise WarehouseWriteError(f"Arrow type/nullability mismatch for {name}: {mismatches}")
