from __future__ import annotations

import re
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import clickhouse_connect
import pyarrow as pa

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import ConfigurationError, WarehouseWriteError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    canonical_fact_schema,
    require_exact_schema,
    sec_warehouse_schema,
    xbrl_warehouse_schema,
)
from sec_xbrl_warehouse.infrastructure.storage.hashing import stable_json_hash
from sec_xbrl_warehouse.normalization.statements import (
    StatementLine,
    build_statement_lines,
    classify_roles,
)
from sec_xbrl_warehouse.observability.metrics import ROWS_WRITTEN

_SEC_TABLE_NAMES: dict[SecTable, str] = {
    SecTable.SUB: "sec_submissions",
    SecTable.TAG: "sec_tags",
    SecTable.DIM: "sec_dimensions",
    SecTable.NUM: "sec_numeric_facts",
    SecTable.TXT: "sec_text_facts",
    SecTable.REN: "sec_renderings",
    SecTable.PRE: "sec_presentations",
    SecTable.CAL: "sec_calculations",
}


class ClickHouseWarehouse:
    """Arrow-first ClickHouse warehouse adapter.

    All production writes should arrive as `pyarrow.Table` or
    `pyarrow.RecordBatch`. The adapter intentionally refuses row-at-a-time
    inserts because they are both slow and easy to make type-inconsistent.
    """

    def __init__(
        self, settings: Settings, client: Any | None = None, *, read_only: bool = False
    ) -> None:
        self.settings = settings
        self.database = settings.clickhouse_database
        query_settings = (
            {
                "readonly": 1,
                "max_execution_time": settings.api_query_timeout_seconds,
                "timeout_before_checking_execution_speed": 0,
                "max_memory_usage": settings.api_query_max_memory_bytes,
                "max_rows_to_read": settings.api_query_max_rows,
                "max_bytes_to_read": settings.api_query_max_bytes,
                "max_result_bytes": settings.api_result_max_bytes,
                "max_result_rows": 10_000,
                "result_overflow_mode": "throw",
                "read_overflow_mode": "throw",
                "timeout_overflow_mode": "throw",
                "max_threads": 2,
            }
            if read_only
            else {}
        )
        self.client = client or clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=(settings.api_clickhouse_username or settings.clickhouse_username)
            if read_only
            else settings.clickhouse_username,
            password=settings.api_clickhouse_password.get_secret_value()
            if read_only and settings.api_clickhouse_password is not None
            else settings.clickhouse_password,
            database=settings.clickhouse_database,
            secure=settings.clickhouse_secure,
            autogenerate_session_id=False,
            settings=query_settings,
        )
        if read_only:
            try:
                effective = {
                    row["name"]: row["value"]
                    for row in self.query(
                        "SELECT name, value FROM system.settings WHERE name IN %(names)s",
                        {"names": tuple(query_settings)},
                    )
                }
                for name, budget in query_settings.items():
                    value = effective.get(name)
                    if name == "max_threads" and value is not None:
                        auto = re.fullmatch(r"'?auto\((\d+)\)'?", str(value))
                        if auto:
                            value = auto.group(1)
                    if (
                        value is None
                        or (isinstance(budget, str) and value != budget)
                        or (
                            isinstance(budget, int)
                            and (float(value) > budget or (budget > 0 and float(value) <= 0))
                        )
                    ):
                        raise ConfigurationError(
                            f"API query budget {name} is not enforced; configure the reader's ClickHouse settings profile"
                        )
            except Exception:
                self.client.close()
                raise

    def ping(self) -> bool:
        try:
            return str(self.client.command("SELECT 1")).strip() == "1"
        except Exception:
            return False

    def close(self) -> None:
        self.client.close()

    def apply_ddl(self, ddl_directory: Path) -> tuple[str, ...]:
        """Render and execute all numbered ClickHouse SQL files in lexical order."""
        drift = self.schema_drift(ddl_directory)
        if drift:
            raise ConfigurationError(
                "ClickHouse tables require a preserving upgrade: "
                f"{', '.join(drift)}. Run `xbrl-warehouse db upgrade-clickhouse --yes`."
            )
        executed: list[str] = []
        for path in sorted(ddl_directory.glob("[0-9][0-9][0-9]_*.sql")):
            rendered = path.read_text(encoding="utf-8").replace("{{database}}", self.database)
            for statement in self._split_sql(rendered):
                self.client.command(statement)
            executed.append(path.name)
        return tuple(executed)

    def schema_drift(self, ddl_directory: Path) -> tuple[str, ...]:
        definitions = self._table_definitions(ddl_directory)
        result = self.client.query(
            "SELECT name, sorting_key FROM system.tables WHERE database = %(database)s",
            parameters={"database": self.database},
        )
        current = {str(name): str(key) for name, key in result.result_rows}
        return tuple(
            sorted(
                name
                for name, (_, expected_key) in definitions.items()
                if name in current
                and (
                    _normalize_sql(current[name]) != _normalize_sql(expected_key)
                    or self._column_contract(name) != _ddl_columns(definitions[name][0])
                )
            )
        )

    def upgrade_ddl(self, ddl_directory: Path) -> dict[str, str]:
        """Copy, count, atomically exchange, and retain every incompatible table."""
        definitions = self._table_definitions(ddl_directory)
        drift = self.schema_drift(ddl_directory)
        if drift:
            sizes = (
                self.client.query(
                    "SELECT sum(total_bytes) FROM system.tables "
                    "WHERE database = %(database)s AND name IN %(tables)s",
                    parameters={"database": self.database, "tables": drift},
                ).result_rows[0][0]
                or 0
            )
            free = self.client.query("SELECT min(free_space) FROM system.disks").result_rows[0][0]
            if int(free or 0) < int(sizes) * 11 // 10:
                raise WarehouseWriteError(
                    f"Insufficient ClickHouse disk space for preserving upgrade: need about {int(sizes) * 11 // 10} bytes"
                )
        backups: dict[str, str] = {}
        for name in drift:
            statement, _ = definitions[name]
            token = uuid4().hex[:8]
            temporary = f"{name}__upgrade_{token}"
            backup = f"{name}__backup_{token}"
            create = re.sub(
                rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(self.database)}\.{re.escape(name)}",
                f"CREATE TABLE {self.database}.{temporary}",
                statement,
                count=1,
                flags=re.IGNORECASE,
            )
            self.client.command(create)
            exchanged = False
            try:
                with ExitStack() as merges:
                    # Preserve physical rows while copying, including replay duplicates.
                    self.client.command(f"SYSTEM STOP MERGES {self.database}.{name}")
                    merges.callback(
                        self.client.command, f"SYSTEM START MERGES {self.database}.{name}"
                    )
                    self.client.command(f"SYSTEM STOP MERGES {self.database}.{temporary}")

                    def resume_retained(name: str = name, temporary: str = temporary) -> None:
                        self.client.command(
                            f"SYSTEM START MERGES {self.database}.{backups.get(name, temporary)}"
                        )

                    merges.callback(resume_retained)
                    deadline = time.monotonic() + 30
                    while self.query(
                        "SELECT count() AS n FROM system.merges "
                        "WHERE database=%(database)s AND table=%(table)s",
                        {"database": self.database, "table": name},
                    )[0]["n"]:
                        if time.monotonic() >= deadline:
                            raise WarehouseWriteError(f"Timed out waiting for merges on {name}")
                        time.sleep(0.1)
                    source_columns = self._columns(name)
                    target_columns = self._columns(temporary)
                    source_contract = self._column_contract(name)
                    target_contract = self._column_contract(temporary)
                    for column, (target_type, _, _) in target_contract.items():
                        if column not in source_contract:
                            if (
                                column not in {"value_kind", "source_attributes_json"}
                                or name != "xbrl_facts"
                            ):
                                raise WarehouseWriteError(
                                    f"No migration for new column {name}.{column}"
                                )
                        elif not _preserving_type(source_contract[column][0], target_type):
                            raise WarehouseWriteError(f"Unsafe type conversion for {name}.{column}")
                    if set(source_columns) - set(target_columns):
                        raise WarehouseWriteError(f"Upgrade would discard columns from {name}")
                    common = [column for column in target_columns if column in source_columns]
                    selects = list(common)
                    if name == "xbrl_facts" and "value_kind" not in source_columns:
                        common.append("value_kind")
                        selects.append(
                            "if(is_nil = 1, 'nil', if(value_decimal IS NOT NULL OR unit_id IS NOT NULL, "
                            "'numeric', if(value_boolean IS NOT NULL, 'boolean', "
                            "if(value_date IS NOT NULL, 'date', 'text'))))"
                        )
                    columns = ", ".join(common)
                    projection = ", ".join(selects)
                    self.client.command(
                        f"INSERT INTO {self.database}.{temporary} ({columns}) "
                        f"SELECT {projection} FROM {self.database}.{name} SETTINGS optimize_on_insert=0"
                    )
                    before = int(
                        self.client.query(
                            f"SELECT count() FROM {self.database}.{name}"
                        ).result_rows[0][0]
                    )
                    after = int(
                        self.client.query(
                            f"SELECT count() FROM {self.database}.{temporary}"
                        ).result_rows[0][0]
                    )
                    if before != after:
                        raise WarehouseWriteError(
                            f"Upgrade row-count mismatch for {name}: {before} != {after}"
                        )
                    self.client.command(
                        f"EXCHANGE TABLES {self.database}.{name} AND {self.database}.{temporary}"
                    )
                    exchanged = True
                    self.client.command(
                        f"RENAME TABLE {self.database}.{temporary} TO {self.database}.{backup}"
                    )
                    backups[name] = backup
            except Exception:
                if not exchanged:
                    self.client.command(f"DROP TABLE IF EXISTS {self.database}.{temporary}")
                raise
        # Create newly introduced tables and refresh views only after all swaps succeed.
        self.apply_ddl(ddl_directory)
        return backups

    def _column_contract(self, table: str) -> dict[str, tuple[str, str, str]]:
        result = self.client.query(
            "SELECT name, type, default_kind, default_expression FROM system.columns "
            "WHERE database = %(database)s AND table = %(table)s ORDER BY position",
            parameters={"database": self.database, "table": table},
        )
        return {
            str(name): (_normalize_sql(str(kind)), str(default), str(expression).strip())
            for name, kind, default, expression in result.result_rows
        }

    def _columns(self, table: str) -> list[str]:
        result = self.client.query(
            "SELECT name FROM system.columns WHERE database = %(database)s AND table = %(table)s "
            "ORDER BY position",
            parameters={"database": self.database, "table": table},
        )
        return [str(row[0]) for row in result.result_rows]

    def _table_definitions(self, ddl_directory: Path) -> dict[str, tuple[str, str]]:
        definitions: dict[str, tuple[str, str]] = {}
        for path in sorted(ddl_directory.glob("[0-9][0-9][0-9]_*.sql")):
            rendered = path.read_text(encoding="utf-8").replace("{{database}}", self.database)
            for statement in self._split_sql(rendered):
                match = re.match(
                    rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(self.database)}\.([A-Za-z0-9_]+)",
                    statement,
                    re.IGNORECASE,
                )
                order = re.search(
                    r"\bORDER BY\s+(.+?)(?:\nSETTINGS|;|$)", statement, re.DOTALL | re.IGNORECASE
                )
                if match and order:
                    definitions[match.group(1)] = (statement, order.group(1).strip())
        return definitions

    def insert_sec_batch(self, table: SecTable, batch: pa.RecordBatch | pa.Table) -> int:
        handlers = {
            SecTable.SUB: self.insert_sec_submissions,
            SecTable.TAG: self.insert_sec_tags,
            SecTable.DIM: self.insert_sec_dimensions,
            SecTable.NUM: self.insert_sec_numeric_facts,
            SecTable.TXT: self.insert_sec_text_facts,
            SecTable.REN: self.insert_sec_renderings,
            SecTable.PRE: self.insert_sec_presentations,
            SecTable.CAL: self.insert_sec_calculations,
        }
        return handlers[table](batch)

    def insert_sec_submissions(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.SUB, batch)

    def insert_sec_tags(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.TAG, batch)

    def insert_sec_dimensions(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.DIM, batch)

    def insert_sec_numeric_facts(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.NUM, batch)

    def insert_sec_text_facts(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.TXT, batch)

    def insert_sec_renderings(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.REN, batch)

    def insert_sec_presentations(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.PRE, batch)

    def insert_sec_calculations(self, batch: pa.RecordBatch | pa.Table) -> int:
        return self._insert_sec_typed(SecTable.CAL, batch)

    def _insert_sec_typed(self, table: SecTable, batch: pa.RecordBatch | pa.Table) -> int:
        """Validate and insert one typed/enriched SEC batch.

        The incoming schema must match `sec_warehouse_schema(table)` exactly —
        column order, types, decimal precision/scale, nullability, and ingest
        lineage columns included.
        """
        target = _SEC_TABLE_NAMES[table]
        arrow_table = batch if isinstance(batch, pa.Table) else pa.Table.from_batches([batch])
        require_exact_schema(
            arrow_table.schema,
            sec_warehouse_schema(table),
            name=f"{self.database}.{target}",
        )
        if table in {SecTable.NUM, SecTable.TXT}:
            # Keep full-range dates in Parquet; ClickHouse's Arrow Date32 reader
            # rejects years outside 1900..2299 even when the target is String.
            index = arrow_table.schema.get_field_index("ddate")
            arrow_table = arrow_table.set_column(
                index, "ddate", arrow_table.column(index).cast(pa.string())
            )
        return self._insert_arrow(target, arrow_table)

    def insert_xbrl_bundle(self, bundle: XbrlExtractionBundle) -> dict[str, int]:
        """Persist all entities from one Arelle extraction at lineage level.

        Includes the derived `roles` and `statement_lines` (primary statements as
        presented), so they publish with the same revision marker.
        """
        counts: dict[str, int] = {}
        for entity in XBRL_ENTITIES:
            counts[entity] = sum(
                self._insert_arrow(f"xbrl_{entity}", table)
                for table in iter_xbrl_tables(
                    bundle, entity, self.settings.arrow_batch_rows, self.settings.arrow_batch_bytes
                )
            )
        return counts

    def insert_canonical_facts(self, facts: Sequence[CanonicalFact]) -> int:
        """Insert mapped facts with raw fact and mapping lineage."""
        if not facts:
            return 0
        validate_canonical_facts(facts)
        rows: list[dict[str, Any]] = []
        size = written = 0
        for fact in facts:
            record_size = len(fact.model_dump_json().encode("utf-8")) + 512
            if record_size > self.settings.arrow_batch_bytes:
                raise WarehouseWriteError("Single canonical record exceeds byte budget")
            if rows and (
                size + record_size > self.settings.arrow_batch_bytes
                or len(rows) >= self.settings.arrow_batch_rows
            ):
                written += self._insert_arrow(
                    "canonical_facts", pa.Table.from_pylist(rows, schema=canonical_fact_schema())
                )
                rows, size = [], 0
            rows.append(_canonical_fact_row(fact))
            size += record_size
        if rows:
            written += self._insert_arrow(
                "canonical_facts", pa.Table.from_pylist(rows, schema=canonical_fact_schema())
            )
        return written

    def publish_xbrl_bundle(self, bundle: XbrlExtractionBundle, counts: dict[str, int]) -> None:
        """Verify all raw entities before exposing this immutable revision to readers.

        Entities missing from `counts` (written before roles and statement lines
        existed) are not checked.
        """
        for entity in (entity for entity in XBRL_ENTITIES if entity in counts):
            result = self.query(
                f"SELECT count() AS count FROM xbrl_{entity} FINAL WHERE accession = %(accession)s AND extraction_revision = %(revision)s",
                {"accession": bundle.filing.accession, "revision": bundle.extraction_revision},
            )
            if not result or int(result[0]["count"]) != counts[entity]:
                raise WarehouseWriteError(f"Extraction row count mismatch for {entity}")
        schema = pa.schema(
            [
                pa.field("accession", pa.string(), False),
                pa.field("extraction_revision", pa.uint32(), False),
                pa.field("entity_counts_json", pa.string(), False),
                pa.field("metadata_json", pa.string(), False),
                pa.field("completed_at", pa.timestamp("ms", tz="UTC"), False),
            ]
        )
        self._insert_arrow(
            "xbrl_extractions",
            pa.Table.from_pylist(
                [
                    {
                        "accession": bundle.filing.accession,
                        "extraction_revision": bundle.extraction_revision,
                        "entity_counts_json": _json(counts),
                        "metadata_json": bundle.metadata.model_dump_json(),
                        "completed_at": bundle.metadata.extracted_at,
                    }
                ],
                schema=schema,
            ),
        )

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.client.query(sql, parameters=parameters or {})
        names = list(result.column_names)
        return [dict(zip(names, row, strict=True)) for row in result.result_rows]

    def _insert_arrow(self, table_name: str, table: pa.Table) -> int:
        if table.num_rows == 0:
            return 0
        if len(set(table.column_names)) != len(table.column_names):
            raise WarehouseWriteError(f"Duplicate Arrow column names for {table_name}")
        try:
            for batch in table.to_batches(max_chunksize=self.settings.arrow_batch_rows):
                self.client.insert_arrow(
                    f"{self.database}.{table_name}", pa.Table.from_batches([batch])
                )
                ROWS_WRITTEN.labels(table=table_name, storage="clickhouse").inc(batch.num_rows)
        except Exception as exc:
            raise WarehouseWriteError(
                f"ClickHouse Arrow insert failed for {table_name}: {exc}"
            ) from exc
        return table.num_rows  # type: ignore[no-any-return]

    @staticmethod
    def _split_sql(sql: str) -> tuple[str, ...]:
        """Split simple DDL files on semicolons outside quoted strings.

        The checked-in DDL deliberately avoids procedures and semicolons inside
        string literals. Replace this helper with a real SQL lexer if that changes.
        """
        cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
        return tuple(part.strip() for part in cleaned.split(";") if part.strip())


def validate_canonical_facts(facts: Sequence[CanonicalFact]) -> None:
    """Reject canonical facts missing mapping lineage before any database write."""
    for fact in facts:
        if fact.mapping_id == UUID(int=0) or fact.mapping_version < 1:
            raise WarehouseWriteError(
                f"CanonicalFact for raw fact {fact.raw_fact_id} is missing mapping lineage"
            )


def iter_xbrl_tables(
    bundle: XbrlExtractionBundle,
    entity: str,
    batch_rows: int,
    batch_bytes: int,
) -> Iterator[pa.Table]:
    """Convert bounded entity groups, retaining only one Arrow table at a time."""
    if batch_rows < 1 or batch_bytes < 1:
        raise ValueError("Arrow batch budgets must be positive")
    build = {
        "contexts": contexts_table,
        "concepts": concepts_table,
        "facts": facts_table,
        "relationships": relationships_table,
        "footnotes": footnotes_table,
        "roles": roles_table,
        "statement_lines": _statement_lines_builder(bundle),
    }[entity]

    def bounded(table: pa.Table) -> Iterator[pa.Table]:
        if table.nbytes <= batch_bytes:
            yield table
        elif table.num_rows == 1:
            raise WarehouseWriteError(f"Single {entity} row exceeds Arrow byte budget")
        else:
            middle = table.num_rows // 2
            yield from bounded(table.slice(0, middle))
            yield from bounded(table.slice(middle))

    records: list[Any] = []
    size = 0
    for record in xbrl_entity_records(bundle, entity):
        record_size = _record_size(record)
        if record_size > batch_bytes:
            raise WarehouseWriteError(f"Single {entity} record exceeds Arrow byte budget")
        if records and (len(records) >= batch_rows or size + record_size > batch_bytes):
            yield from bounded(_build_chunk(build, bundle, entity, records))
            records, size = [], 0
        records.append(record)
        size += record_size
    if records:
        yield from bounded(_build_chunk(build, bundle, entity, records))


XBRL_ENTITIES = (
    "contexts",
    "concepts",
    "facts",
    "relationships",
    "footnotes",
    "roles",
    "statement_lines",
)
# Entities derived from the bundle rather than stored on it.
DERIVED_XBRL_ENTITIES = frozenset({"statement_lines"})


def xbrl_entity_records(bundle: XbrlExtractionBundle, entity: str) -> Sequence[Any]:
    """The records one entity table holds for a bundle."""
    if entity == "statement_lines":
        return build_statement_lines(bundle)
    return tuple(getattr(bundle, entity))


def _record_size(record: Any) -> int:
    if isinstance(record, StatementLine):
        return len(_json(asdict(record)).encode("utf-8")) + 512
    return len(record.model_dump_json().encode("utf-8")) + 512


def _build_chunk(
    build: Any, bundle: XbrlExtractionBundle, entity: str, records: list[Any]
) -> pa.Table:
    if entity in DERIVED_XBRL_ENTITIES:
        return build(records)
    return build(bundle.model_copy(update={entity: tuple(records)}))


def roles_table(bundle: XbrlExtractionBundle) -> pa.Table:
    kinds = classify_roles(bundle) if bundle.roles else {}
    rows = [
        {
            "accession": bundle.filing.accession,
            "role_uri": role.role_uri,
            "definition": role.definition,
            "used_on": list(role.used_on),
            "sort_code": role.sort_code,
            "category": role.category,
            "statement_kind": kinds[role.role_uri].value if role.role_uri in kinds else None,
            "extraction_revision": bundle.extraction_revision,
            "_row_hash": stable_json_hash(
                {"accession": bundle.filing.accession, "role": role.model_dump(mode="json")}
            ),
            "_ingested_at": bundle.metadata.extracted_at,
        }
        for role in bundle.roles
    ]
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("roles"))


def _statement_lines_builder(bundle: XbrlExtractionBundle) -> Any:
    def build(lines: list[StatementLine]) -> pa.Table:
        return statement_lines_table(bundle, lines)

    return build


def statement_lines_table(
    bundle: XbrlExtractionBundle, lines: Sequence[StatementLine] | None = None
) -> pa.Table:
    """One row per line; each line's values are parallel arrays (one entry per period)."""
    selected = build_statement_lines(bundle) if lines is None else lines
    rows = [
        {
            "accession": line.accession,
            "cik": bundle.filing.cik,
            "form": bundle.filing.form,
            "filed_date": bundle.filing.filed_date,
            "role_uri": line.role_uri,
            "statement_kind": line.statement_kind.value,
            "line_number": line.line_number,
            "depth": line.depth,
            "concept_qname": line.concept_qname,
            "parent_concept_qname": line.parent_concept_qname,
            "label": line.label,
            "preferred_label_role": line.preferred_label_role,
            "is_abstract": int(line.is_abstract),
            "is_total": int(line.is_total),
            "value_fact_ids": [value.fact_id for value in line.values],
            "value_period_starts": [value.period_start for value in line.values],
            "value_period_ends": [value.period_end for value in line.values],
            "value_period_instants": [value.period_instant for value in line.values],
            "value_decimals": [value.value_decimal for value in line.values],
            "extraction_revision": bundle.extraction_revision,
            "_row_hash": stable_json_hash(
                {"line": asdict(line), "revision": bundle.extraction_revision}
            ),
            "_ingested_at": bundle.metadata.extracted_at,
        }
        for line in selected
    ]
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("statement_lines"))


def contexts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    rows = []
    for context in bundle.contexts:
        rows.append(
            {
                "accession": bundle.filing.accession,
                "cik": bundle.filing.cik,
                "context_id": context.context_id,
                "context_hash": context.context_hash,
                "entity_scheme": context.entity_scheme,
                "entity_identifier": context.entity_identifier,
                "period_kind": context.period_kind.value,
                "period_start": context.period_start,
                "period_end": context.period_end,
                "period_instant": context.period_instant,
                "dimensions_json": _dimensions_json(context.dimensions),
                "segment_xml": context.segment_xml,
                "scenario_xml": context.scenario_xml,
                "source_document": context.source_document,
                "extraction_revision": bundle.extraction_revision,
                "_row_hash": stable_json_hash(
                    {
                        "accession": bundle.filing.accession,
                        "context": context.model_dump(mode="json"),
                    }
                ),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("contexts"))


def concepts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    rows = []
    for concept in bundle.concepts:
        rows.append(
            {
                "accession": bundle.filing.accession,
                "concept_namespace": concept.namespace,
                "concept_name": concept.name,
                "concept_qname": concept.qname,
                "taxonomy_version": concept.taxonomy_version,
                "datatype_qname": concept.datatype_qname,
                "substitution_group_qname": concept.substitution_group_qname,
                "period_type": concept.period_type,
                "balance": concept.balance,
                "abstract": int(concept.abstract),
                "nillable": int(concept.nillable),
                "is_extension": int(concept.is_extension),
                "labels_json": _labels_json(concept.labels),
                "documentation": concept.documentation,
                "references_json": (None if not concept.references else _json(concept.references)),
                "extraction_revision": bundle.extraction_revision,
                "_row_hash": stable_json_hash(
                    {
                        "accession": bundle.filing.accession,
                        "concept": concept.model_dump(mode="json"),
                    }
                ),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("concepts"))


def facts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    rows = []
    for fact in bundle.facts:
        rows.append(
            fact_row(
                bundle.filing.accession,
                bundle.filing.cik,
                bundle.filing.company_name,
                bundle.filing.form,
                bundle.filing.filed_date,
                fact,
                bundle.extraction_revision,
                fiscal_year=bundle.filing.fiscal_year,
                fiscal_period=bundle.filing.fiscal_period,
                ingested_at=bundle.metadata.extracted_at,
            )
        )
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("facts"))


def relationships_table(bundle: XbrlExtractionBundle) -> pa.Table:
    rows = []
    for relationship in bundle.relationships:
        rows.append(
            {
                "relationship_id": relationship.relationship_id,
                "accession": bundle.filing.accession,
                "network_type": relationship.relationship_type.value,
                "link_role_uri": relationship.link_role_uri,
                "arc_role_uri": relationship.arc_role_uri,
                "from_qname": relationship.from_qname,
                "to_qname": relationship.to_qname,
                "arc_order": relationship.order,
                "priority": relationship.priority,
                "weight": relationship.weight,
                "preferred_label": relationship.preferred_label,
                "use": relationship.use,
                "closed": _optional_int(relationship.closed),
                "usable": _optional_int(relationship.usable),
                "target_role": relationship.target_role,
                "context_element": relationship.context_element,
                "attributes_json": _json(relationship.attributes)
                if relationship.attributes
                else None,
                "source_document": relationship.source_document,
                "extraction_revision": bundle.extraction_revision,
                "_row_hash": stable_json_hash(
                    {
                        "accession": bundle.filing.accession,
                        "relationship": relationship.model_dump(mode="json"),
                    }
                ),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("relationships"))


def footnotes_table(bundle: XbrlExtractionBundle) -> pa.Table:
    rows = []
    for footnote in bundle.footnotes:
        rows.append(
            {
                "footnote_id": footnote.footnote_id,
                "accession": bundle.filing.accession,
                "role_uri": footnote.role_uri,
                "language": footnote.language,
                "text": footnote.text,
                "html": footnote.html,
                "linked_fact_ids": list(footnote.linked_fact_ids),
                "extraction_revision": bundle.extraction_revision,
                "_row_hash": stable_json_hash(
                    {
                        "accession": bundle.filing.accession,
                        "footnote": footnote.model_dump(mode="json"),
                    }
                ),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("footnotes"))


def fact_row(
    accession: str,
    cik: int,
    company_name: str,
    form: str,
    filed_date: date,
    fact: XbrlFact,
    extraction_revision: int,
    *,
    fiscal_year: int | None = None,
    fiscal_period: str | None = None,
    ingested_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "accession": accession,
        "cik": cik,
        "company_name": company_name,
        "form": form,
        "filed_date": filed_date,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "concept_namespace": fact.concept_namespace,
        "concept_name": fact.concept_name,
        "concept_qname": fact.concept_qname,
        "taxonomy_version": fact.taxonomy_version,
        "context_id": fact.context_id,
        "context_hash": fact.context_hash,
        "unit_id": fact.unit.unit_id if fact.unit else None,
        "unit_numerator_json": _json(fact.unit.numerator_measures) if fact.unit else None,
        "unit_denominator_json": _json(fact.unit.denominator_measures) if fact.unit else None,
        "raw_value": fact.raw_value,
        "value_decimal": fact.value_decimal,
        "value_text": fact.value_text,
        "value_boolean": (1 if fact.value_boolean else 0)
        if fact.value_boolean is not None
        else None,
        "value_date": fact.value_date,
        "is_nil": int(fact.is_nil),
        "decimals": fact.decimals,
        "precision": fact.precision,
        "language": fact.language,
        "period_kind": fact.period_kind.value,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "period_instant": fact.period_instant,
        "entity_scheme": fact.entity_scheme,
        "entity_identifier": fact.entity_identifier,
        "dimensions_json": _dimensions_json(fact.dimensions),
        "dimension_hash": fact.dimension_hash,
        "tuple_path": fact.tuple_path,
        "duplicate_ordinal": fact.duplicate_ordinal,
        "duplicate_set_hash": fact.duplicate_set_hash,
        "footnote_ids": list(fact.footnote_ids),
        "source_document": fact.source_document,
        "source_fact_id": fact.source_fact_id,
        "source_attributes_json": _json(fact.source_attributes),
        "value_kind": fact.value_kind.value,
        "extraction_revision": extraction_revision,
        "_row_hash": stable_json_hash(
            {
                "fact": fact.model_dump(mode="json"),
                "accession": accession,
                "extraction_revision": extraction_revision,
                "filing": [cik, company_name, form, filed_date, fiscal_year, fiscal_period],
            }
        ),
        "_ingested_at": ingested_at or _now(),
    }


def _canonical_fact_row(fact: CanonicalFact) -> dict[str, Any]:
    return {
        "canonical_fact_id": fact.canonical_fact_id,
        "raw_fact_id": fact.raw_fact_id,
        "accession": fact.accession,
        "cik": fact.cik,
        "filed_date": fact.filed_date,
        "form": fact.form,
        "canonical_metric": fact.canonical_metric,
        "normalized_value_decimal": fact.normalized_value_decimal,
        "normalized_value_text": fact.normalized_value_text,
        "normalized_unit": fact.normalized_unit,
        "sign_multiplier": fact.sign_multiplier,
        "period_kind": fact.period_kind,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "period_instant": fact.period_instant,
        "dimensions_json": fact.dimensions_json,
        "mapping_id": str(fact.mapping_id),
        "mapping_version": fact.mapping_version,
        "mapping_confidence": fact.mapping_confidence,
        "mapping_explanation": fact.mapping_explanation,
        "extraction_revision": fact.extraction_revision,
        "_row_hash": stable_json_hash(fact.model_dump(mode="json")),
        "_ingested_at": _now(),
    }


def _dimensions_json(dimensions: Any) -> str:
    from sec_xbrl_warehouse.normalization.dimensions import dimensions_to_json

    return dimensions_to_json(dimensions)


def _labels_json(labels: Any) -> str:
    payload = [label.model_dump(mode="json") for label in labels]
    return _json(payload)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _optional_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_sql(value: str) -> str:
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    value = re.sub(r"\s+", "", value).lower()
    for bits, precision in ((32, 9), (64, 18), (128, 38), (256, 76)):
        value = value.replace(f"decimal{bits}(", f"decimal({precision},")
    return value


def _ddl_columns(statement: str) -> dict[str, tuple[str, str, str]]:
    # Repository DDL keeps one column per line; reject unsupported declarations.
    body = statement.split("\n(\n", 1)[1].split("\n)\n", 1)[0]
    columns = {}
    for line in body.splitlines():
        declaration = line.strip().rstrip(",")
        if not declaration or declaration.startswith("--"):
            continue
        name, kind = declaration.split(" ", 1)
        kind = re.split(r"\s+CODEC\(", kind, maxsplit=1)[0]
        parts = re.split(r"\s+DEFAULT\s+", kind, maxsplit=1)
        columns[name] = (
            _normalize_sql(parts[0]),
            "DEFAULT" if len(parts) == 2 else "",
            parts[1].strip() if len(parts) == 2 else "",
        )
    return columns


def _preserving_type(source: str, target: str) -> bool:
    source, target = _normalize_sql(source), _normalize_sql(target)
    if source == target:
        return True
    if (source, target) == ("uint8", "int16"):
        return True
    if source in {"date", "date32"} and target == "string":
        return True
    if target.startswith("lowcardinality(") and source.startswith("lowcardinality("):
        return _preserving_type(source[15:-1], target[15:-1])
    if target.startswith("nullable("):
        return _preserving_type(
            source[9:-1] if source.startswith("nullable(") else source, target[9:-1]
        )
    return bool(re.fullmatch(r"fixedstring\(\d+\)", source) and target == "string")
