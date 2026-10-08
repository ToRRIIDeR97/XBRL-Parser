from __future__ import annotations

import hashlib
import os
import re
import threading
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
from clickhouse_connect.driver.exceptions import ProgrammingError

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import FactValueKind, RelationshipType, SecTable
from sec_xbrl_warehouse.domain.models import Company
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import ConfigurationError, WarehouseWriteError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    canonical_fact_schema,
    clickhouse_facts_schema,
    concept_labels_schema,
    require_exact_schema,
    sec_warehouse_schema,
    taxonomy_concepts_schema,
    xbrl_warehouse_schema,
)
from sec_xbrl_warehouse.infrastructure.storage.hashing import stable_json_hash
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.normalization.disclosures import (
    CoverPage,
    SegmentRow,
    cover_page,
    segment_rows,
)
from sec_xbrl_warehouse.normalization.statements import (
    StatementLine,
    build_statement_lines,
    classify_roles,
)
from sec_xbrl_warehouse.observability.metrics import ROWS_WRITTEN

logger = get_logger(__name__)

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
        # `taxonomy_concepts` row hashes inserted by this process (see insert_xbrl_bundle).
        self._taxonomy_rows: set[str] = set()
        self._taxonomy_lock = threading.Lock()
        if read_only and not settings.api_clickhouse_username and settings.env == "production":
            raise ConfigurationError(
                "The API reader needs XBRL_API_CLICKHOUSE_USERNAME; it never uses the writer"
            )
        if read_only and not settings.api_clickhouse_username:
            logger.warning("api_clickhouse_writer_fallback", env=settings.env)
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
        connection: dict[str, Any] = {
            "host": settings.clickhouse_host,
            "port": settings.clickhouse_port,
            "username": (settings.api_clickhouse_username or settings.clickhouse_username)
            if read_only
            else settings.clickhouse_username,
            "password": settings.api_clickhouse_password.get_secret_value()
            if read_only and settings.api_clickhouse_password is not None
            else settings.clickhouse_password.get_secret_value(),
            "database": settings.clickhouse_database,
            "secure": settings.clickhouse_secure,
            "autogenerate_session_id": False,
        }
        if client is not None:
            self.client = client
        else:
            try:
                self.client = clickhouse_connect.get_client(**connection, settings=query_settings)
            except ProgrammingError as exc:
                if not read_only or "readonly" not in str(exc):
                    raise
                # A `readonly = 1` reader profile fixes every setting, so the budgets
                # can't be sent per query; the check below confirms the profile
                # enforces them instead.
                self.client = clickhouse_connect.get_client(**connection)
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
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        for name in drift:
            statement, _ = definitions[name]
            token = uuid4().hex[:8]
            temporary = f"{name}__upgrade_{token}"
            backup = f"{name}__backup_{stamp}_{token}"
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
                            # New Nullable columns start as NULL for existing rows.
                            if target_type.startswith("nullable(") or (
                                target_type.startswith("lowcardinality(nullable(")
                            ):
                                continue
                            if (
                                column not in {"value_kind", "source_attributes_json"}
                                or name != "xbrl_facts"
                            ):
                                raise WarehouseWriteError(
                                    f"No migration for new column {name}.{column}"
                                )
                        elif _is_hash_encoding(
                            name, column, source_contract[column][0], target_type
                        ):
                            continue
                        elif not _preserving_type(source_contract[column][0], target_type):
                            raise WarehouseWriteError(f"Unsafe type conversion for {name}.{column}")
                    if set(source_columns) - set(target_columns):
                        raise WarehouseWriteError(f"Upgrade would discard columns from {name}")
                    common = [column for column in target_columns if column in source_columns]
                    selects = [
                        _hash_projection(column, target_contract[column][0])
                        if column in source_contract
                        and _is_hash_encoding(
                            name,
                            column,
                            source_contract[column][0],
                            target_contract[column][0],
                        )
                        else column
                        for column in common
                    ]
                    if name == "xbrl_facts" and "value_kind" not in source_columns:
                        common.append("value_kind")
                        selects.append(
                            "if(is_nil = 1, 'nil', if(value_decimal IS NOT NULL OR unit_id IS NOT NULL, "
                            "'numeric', if(value_boolean IS NOT NULL, 'boolean', "
                            "if(value_date IS NOT NULL, 'date', 'text'))))"
                        )
                    columns = ", ".join(common)
                    projection = ", ".join(selects)
                    # xbrl_concepts keeps extension concepts; standard concepts move to
                    # the shared taxonomy_concepts table (the backup keeps every row).
                    split = name == "xbrl_concepts" and "is_extension" in source_columns
                    where = " WHERE is_extension = 1" if split else ""
                    self.client.command(
                        f"INSERT INTO {self.database}.{temporary} ({columns}) "
                        f"SELECT {projection} FROM {self.database}.{name}{where} "
                        "SETTINGS optimize_on_insert=0"
                    )
                    if split:
                        self._move_taxonomy_concepts(definitions, name, source_contract)
                    before = int(
                        self.client.query(
                            f"SELECT count() FROM {self.database}.{name}{where}"
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

    def _move_taxonomy_concepts(
        self, definitions: dict[str, tuple[str, str]], source: str, contract: dict[str, Any]
    ) -> None:
        """Copy a legacy table's standard concepts into `taxonomy_concepts` (deduplicated).

        Labels of concepts a filing labels itself (a label arc in one of its own
        documents, per `xbrl_relationships` and the published extraction's
        `source_documents`) go to `xbrl_concept_labels` for that filing; only the
        others' labels are shared. Rows of unpublished revisions share no labels.
        """
        self.client.command(definitions["taxonomy_concepts"][0])
        self.client.command(definitions["xbrl_concept_labels"][0])
        db = self.database
        # Unnest each extraction's own documents once into a small set. Joining
        # `metadata_json` (about 1 MB per extraction: it embeds Arelle's validation
        # log) onto every label arc made this upgrade exceed 4 GiB on 43 filings.
        own_documents = (
            f"SELECT accession, extraction_revision, arrayJoin(JSONExtract("
            f"metadata_json, 'source_documents', 'Array(String)')) FROM {db}.xbrl_extractions"
        )
        own = (
            f"(accession, extraction_revision, concept_qname) IN ("
            f"SELECT accession, extraction_revision, from_qname "
            f"FROM {db}.xbrl_relationships WHERE network_type = 'label' "
            f"AND (accession, extraction_revision, source_document) IN ({own_documents}))"
        )
        shared = (
            f"NOT ({own}) AND (accession, extraction_revision) IN "
            f"(SELECT accession, extraction_revision FROM {db}.xbrl_extractions)"
        )
        row_hash = (
            _hash_projection("_row_hash", "fixedstring(32)")
            if _normalize_sql(contract["_row_hash"][0]) != "fixedstring(32)"
            else "_row_hash"
        )
        target = self._columns("taxonomy_concepts")
        overrides = {
            "_row_hash": row_hash,
            "labels_json": f"if({shared}, labels_json, '[]') AS labels_json",
            "labels_rank": f"toUInt8({shared}) AS labels_rank",
        }
        selects = [overrides.get(column, column) for column in target]
        self.client.command(
            f"INSERT INTO {db}.taxonomy_concepts ({', '.join(target)}) "
            f"SELECT {', '.join(selects)} FROM {db}.{source} WHERE is_extension = 0"
        )
        labels = self._columns("xbrl_concept_labels")
        self.client.command(
            f"INSERT INTO {db}.xbrl_concept_labels ({', '.join(labels)}) "
            f"SELECT {', '.join(row_hash if c == '_row_hash' else c for c in labels)} "
            f"FROM {db}.{source} WHERE is_extension = 0 AND {own}"
        )

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

        Includes the derived `roles`, `statement_lines` (primary statements as
        presented), `cover_pages`, and `segment_facts`, so they publish with the same
        revision marker.
        """
        counts: dict[str, int] = {}
        for entity in XBRL_ENTITIES:
            counts[entity] = sum(
                self._insert_arrow(f"xbrl_{entity}", table)
                for table in iter_xbrl_tables(
                    bundle,
                    entity,
                    self.settings.arrow_batch_rows,
                    self.settings.arrow_batch_bytes,
                    target="clickhouse",
                )
            )
        # The filing's own labels for standard concepts belong to this revision.
        counts["concept_labels"] = sum(
            self._insert_arrow("xbrl_concept_labels", table)
            for table in iter_xbrl_tables(
                bundle,
                "concept_labels",
                self.settings.arrow_batch_rows,
                self.settings.arrow_batch_bytes,
                target="clickhouse",
            )
        )
        # Standard concepts are shared across filings (not part of the revision's
        # published row counts). A row's hash covers the stored row alone, so a row
        # this process already inserted is skipped: re-inserting it changes nothing.
        for table in iter_xbrl_tables(
            bundle,
            "taxonomy_concepts",
            self.settings.arrow_batch_rows,
            self.settings.arrow_batch_bytes,
            target="clickhouse",
        ):
            hashes = table.column("_row_hash").to_pylist()
            with self._taxonomy_lock:
                fresh = [hash_ not in self._taxonomy_rows for hash_ in hashes]
            if not any(fresh):
                continue
            self._insert_arrow("taxonomy_concepts", table.filter(pa.array(fresh)))
            with self._taxonomy_lock:
                if len(self._taxonomy_rows) > _TAXONOMY_ROW_CACHE:
                    self._taxonomy_rows.clear()
                self._taxonomy_rows.update(
                    hash_ for hash_, new in zip(hashes, fresh, strict=True) if new
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

        Entities missing from `counts` (written before the derived entities existed)
        are not checked. All counts run as one query; `xbrl_facts` is sorted by CIK,
        so its count also filters on the filing's CIK.
        """
        entities = [entity for entity in (*XBRL_ENTITIES, "concept_labels") if entity in counts]
        if entities:
            found = {
                str(row["entity"]): int(row["count"])
                for row in self.query(
                    " UNION ALL ".join(
                        f"SELECT '{entity}' AS entity, count() AS count FROM xbrl_{entity} FINAL "
                        "WHERE accession = %(accession)s AND extraction_revision = %(revision)s"
                        + (" AND cik = %(cik)s" if entity == "facts" else "")
                        for entity in entities
                    ),
                    {
                        "accession": bundle.filing.accession,
                        "revision": bundle.extraction_revision,
                        "cik": bundle.filing.cik,
                    },
                )
            }
            for entity in entities:
                if found.get(entity) != counts[entity]:
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
                        "metadata_json": extraction_metadata_json(bundle),
                        "completed_at": bundle.metadata.extracted_at,
                    }
                ],
                schema=schema,
            ),
        )

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        return self.client.command(sql, parameters=parameters or {})

    def insert_metric_series(self, table: pa.Table) -> int:
        """Append one company's rebuilt series rows (see `query.series`)."""
        return self._insert_arrow("metric_series", table)

    def upsert_company_mirror(self, company: Company) -> int:
        """Mirror a PostgreSQL company row for read-only ticker lookups in ClickHouse."""
        table = pa.Table.from_pylist(
            [
                {
                    "cik": company.cik,
                    "name": company.name,
                    "tickers": [ticker.upper() for ticker in company.tickers],
                    "exchanges": list(company.exchanges),
                    "sic": company.sic,
                    "fiscal_year_end": company.fiscal_year_end,
                    "updated_at": _now(),
                }
            ],
            schema=pa.schema(
                [
                    pa.field("cik", pa.uint64(), False),
                    pa.field("name", pa.string(), False),
                    pa.field("tickers", pa.list_(pa.string()), False),
                    pa.field("exchanges", pa.list_(pa.string()), False),
                    pa.field("sic", pa.uint32()),
                    pa.field("fiscal_year_end", pa.string()),
                    pa.field("updated_at", pa.timestamp("ms", tz="UTC"), False),
                ]
            ),
        )
        return self._insert_arrow("companies", table)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.client.query(sql, parameters=parameters or {})
        names = list(result.column_names)
        return [dict(zip(names, row, strict=True)) for row in result.result_rows]

    def _insert_arrow(self, table_name: str, table: pa.Table) -> int:
        if table.num_rows == 0:
            return 0
        table = encode_hash_columns(table_name, table)
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


# Upper bound on remembered `taxonomy_concepts` row hashes per process (each pinned
# taxonomy year holds about 20,000 concepts).
_TAXONOMY_ROW_CACHE = 1_000_000

# SHA-256 identity and row hashes stored as FixedString(32) bytes in ClickHouse; the
# v_* views return lower-case hex. SEC bulk tables keep their hex `_row_hash`.
HASH_COLUMNS: dict[str, frozenset[str]] = {
    "xbrl_contexts": frozenset({"context_hash", "_row_hash"}),
    "xbrl_concepts": frozenset({"_row_hash"}),
    "taxonomy_concepts": frozenset({"_row_hash"}),
    "xbrl_concept_labels": frozenset({"_row_hash"}),
    "xbrl_facts": frozenset(
        {"fact_id", "context_hash", "dimension_hash", "duplicate_set_hash", "_row_hash"}
    ),
    "xbrl_relationships": frozenset({"relationship_id", "_row_hash"}),
    "xbrl_footnotes": frozenset({"footnote_id", "_row_hash"}),
    "xbrl_roles": frozenset({"_row_hash"}),
    "xbrl_statement_lines": frozenset({"_row_hash"}),
    "xbrl_cover_pages": frozenset({"_row_hash"}),
    "xbrl_segment_facts": frozenset({"fact_id", "_row_hash"}),
    "canonical_facts": frozenset({"canonical_fact_id", "raw_fact_id", "_row_hash"}),
}
HASH_ARRAY_COLUMNS: dict[str, frozenset[str]] = {
    "xbrl_facts": frozenset({"footnote_ids"}),
    "xbrl_footnotes": frozenset({"linked_fact_ids"}),
    "xbrl_statement_lines": frozenset({"value_fact_ids"}),
}
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
_EMPTY_HASH = bytes(32)


def hash_bytes(value: str | None) -> bytes | None:
    """64-character hex SHA-256 -> its 32 bytes; '' (a tuple fact's context) -> zero bytes.

    Pipeline identifiers are always hex SHA-256. Any other identifier (only test
    fixtures use them) is stored as the SHA-256 of its text, the same mapping the
    upgrade applies, so joins still match; outside tests this logs a warning,
    because the view then can't return the original text.
    """
    if value is None:
        return None
    if value == "":
        return _EMPTY_HASH
    if _HEX_DIGEST.fullmatch(value):
        return bytes.fromhex(value)
    if os.environ.get("XBRL_ENV", "") != "test":
        logger.warning("non_hex_identifier_hashed", identifier=value[:80])
    return hashlib.sha256(value.encode("utf-8")).digest()


def encode_hash_columns(table_name: str, table: pa.Table) -> pa.Table:
    """Replace hex hash columns with `fixed_size_binary(32)` for a ClickHouse insert."""
    binary = pa.binary(32)
    arrays = HASH_ARRAY_COLUMNS.get(table_name, frozenset())
    for name in HASH_COLUMNS.get(table_name, frozenset()) | arrays:
        index = table.schema.get_field_index(name)
        if index < 0:
            continue
        field = table.schema.field(index)
        if name in arrays:
            values = [
                None if items is None else [hash_bytes(item) for item in items]
                for items in table.column(index).to_pylist()
            ]
            column = pa.array(values, type=pa.list_(binary))
            field = pa.field(name, pa.list_(binary), field.nullable)
        else:
            column = pa.array(
                [hash_bytes(item) for item in table.column(index).to_pylist()], binary
            )
            field = pa.field(name, binary, field.nullable)
        table = table.set_column(index, field, column)
    return table


def extraction_metadata_json(bundle: XbrlExtractionBundle) -> str:
    """`xbrl_extractions.metadata_json`: the bundle metadata without Arelle's log.

    The full validation log (about 1 MB per filing) stays in the saved extraction
    checkpoint, and its issues are grouped in PostgreSQL `validation_issues`; here
    only its record count is kept.
    """
    provenance = dict(bundle.metadata.provenance)
    log = provenance.pop("validation_log", None)
    if isinstance(log, list | tuple):
        provenance["validation_log_records"] = len(log)
    return bundle.metadata.model_copy(update={"provenance": provenance}).model_dump_json()


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
    *,
    target: str = "silver",
) -> Iterator[pa.Table]:
    """Convert bounded entity groups, retaining only one Arrow table at a time.

    `target="silver"` gives the lossless Parquet form (every concept, every raw
    value); `target="clickhouse"` gives the slim ClickHouse form: extension concepts
    only (`taxonomy_concepts` is its own entity there) and conditional raw values.
    """
    if batch_rows < 1 or batch_bytes < 1:
        raise ValueError("Arrow batch budgets must be positive")
    if target not in {"silver", "clickhouse"}:
        raise ValueError(f"Unknown table target: {target}")
    clickhouse = target == "clickhouse"
    build = {
        "contexts": contexts_table,
        # ClickHouse rows are selected below, so both targets share the builder.
        "concepts": silver_concepts_table,
        "taxonomy_concepts": taxonomy_concepts_table,
        "concept_labels": concept_labels_table,
        "facts": facts_table if clickhouse else silver_facts_table,
        "relationships": relationships_table,
        "footnotes": footnotes_table,
        "roles": roles_table,
        "statement_lines": _statement_lines_builder(bundle),
        "cover_pages": _with_bundle(bundle, cover_pages_table),
        "segment_facts": _with_bundle(bundle, segment_facts_table),
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

    selected = xbrl_entity_records(bundle, entity)
    if clickhouse and entity == "concepts":
        # Only extension concepts go to `xbrl_concepts`; filter before building and
        # hashing rows instead of building the whole DTS and discarding most of it.
        selected = tuple(concept for concept in selected if concept.is_extension)
    records: list[Any] = []
    size = 0
    for record in selected:
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
    "cover_pages",
    "segment_facts",
)
# Entities derived from the bundle rather than stored on it.
DERIVED_XBRL_ENTITIES = frozenset({"statement_lines", "cover_pages", "segment_facts"})


def xbrl_entity_records(bundle: XbrlExtractionBundle, entity: str) -> Sequence[Any]:
    """The records one entity table holds for a bundle."""
    if entity == "taxonomy_concepts":
        return tuple(concept for concept in bundle.concepts if not concept.is_extension)
    if entity == "concept_labels":
        filer = filer_labelled_concepts(bundle)
        return tuple(
            concept
            for concept in bundle.concepts
            if not concept.is_extension and concept.qname in filer
        )
    if entity == "statement_lines":
        return build_statement_lines(bundle)
    if entity == "cover_pages":
        return (cover_page(bundle),)
    if entity == "segment_facts":
        return segment_rows(bundle)
    return tuple(getattr(bundle, entity))


def _record_size(record: Any) -> int:
    if isinstance(record, StatementLine | CoverPage | SegmentRow):
        return len(_json(asdict(record)).encode("utf-8")) + 512
    return len(record.model_dump_json().encode("utf-8")) + 512


def _build_chunk(
    build: Any, bundle: XbrlExtractionBundle, entity: str, records: list[Any]
) -> pa.Table:
    if entity in DERIVED_XBRL_ENTITIES:
        return build(records)
    source = "concepts" if entity in {"taxonomy_concepts", "concept_labels"} else entity
    return build(bundle.model_copy(update={source: tuple(records)}))


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


def _with_bundle(bundle: XbrlExtractionBundle, build: Any) -> Any:
    def bound(records: list[Any]) -> pa.Table:
        return build(bundle, records)

    return bound


def cover_pages_table(
    bundle: XbrlExtractionBundle, pages: Sequence[CoverPage] | None = None
) -> pa.Table:
    """The filing's cover page (one row; see `normalization.disclosures.cover_page`)."""
    selected = (cover_page(bundle),) if pages is None else pages
    rows = [
        {
            **_filing_columns(bundle),
            "document_type": page.document_type,
            "period_end_date": page.period_end_date,
            "registrant_name": page.registrant_name,
            "filer_category": page.filer_category,
            "amendment": _optional_int(page.amendment),
            "trading_symbol": page.trading_symbol,
            "exchange": page.exchange,
            "auditor_name": page.auditor_name,
            "shares_outstanding": page.shares_outstanding,
            "shares_outstanding_date": page.shares_outstanding_date,
            "public_float": page.public_float,
            "security_members": [item.member for item in page.securities],
            "security_titles": [item.title for item in page.securities],
            "security_symbols": [item.trading_symbol for item in page.securities],
            "security_exchanges": [item.exchange for item in page.securities],
            **_revision_columns(bundle, {"cover_page": asdict(page)}),
        }
        for page in selected
    ]
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("cover_pages"))


def segment_facts_table(
    bundle: XbrlExtractionBundle, segments: Sequence[SegmentRow] | None = None
) -> pa.Table:
    """Breakdown facts (see `normalization.disclosures.segment_rows`)."""
    selected = segment_rows(bundle) if segments is None else segments
    rows = [
        {
            **_filing_columns(bundle),
            "fact_id": row.fact_id,
            "concept_qname": row.concept_qname,
            "axis": row.axis,
            "member_label": row.member_label,
            "member_axes": [member.axis for member in row.members],
            "member_qnames": [member.qname for member in row.members],
            "member_labels": [member.label for member in row.members],
            "value": row.value,
            "unit": row.unit,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "period_instant": row.period_instant,
            "is_elimination": int(row.is_elimination),
            **_revision_columns(bundle, {"segment_fact": asdict(row)}),
        }
        for row in selected
    ]
    return pa.Table.from_pylist(rows, schema=xbrl_warehouse_schema("segment_facts"))


def _filing_columns(bundle: XbrlExtractionBundle) -> dict[str, Any]:
    return {
        "accession": bundle.filing.accession,
        "cik": bundle.filing.cik,
        "form": bundle.filing.form,
        "filed_date": bundle.filing.filed_date,
    }


def _revision_columns(bundle: XbrlExtractionBundle, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "extraction_revision": bundle.extraction_revision,
        "_row_hash": stable_json_hash({**payload, "revision": bundle.extraction_revision}),
        "_ingested_at": bundle.metadata.extracted_at,
    }


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
            "line_role": line.line_role.value,
            "value_fact_ids": [value.fact_id for value in line.values],
            "value_concept_qnames": [value.concept_qname or "" for value in line.values],
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
    """The filing's extension concepts (ClickHouse `xbrl_concepts`)."""
    table = silver_concepts_table(bundle)
    import pyarrow.compute as pc

    return table.filter(pc.equal(table.column("is_extension"), 1))


def filer_labelled_concepts(bundle: XbrlExtractionBundle) -> frozenset[str]:
    """QNames the filing labels itself: a label arc in one of the filing's documents.

    SEC filings don't load the standard label linkbases, so these labels (for
    example AAPL's terse label "Net sales" on a us-gaap revenue concept) belong to
    the filer and must not be shared through `taxonomy_concepts`.
    """
    own_documents = set(bundle.metadata.source_documents)
    return frozenset(
        relationship.from_qname
        for relationship in bundle.relationships
        if relationship.relationship_type is RelationshipType.LABEL
        and relationship.source_document in own_documents
    )


def taxonomy_concepts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    """Standard taxonomy concepts, keyed by concept and taxonomy version only.

    Labels are kept only when taxonomy-owned (`labels_rank` 1); a concept the filing
    labels itself gets `[]` and rank 0, and its labels go to `concept_labels_table`.
    `_row_hash` covers the stored row alone, so identical concepts from different
    filings are the same row.
    """
    filer = filer_labelled_concepts(bundle)
    rows = []
    for concept in bundle.concepts:
        if concept.is_extension:
            continue
        own = concept.qname in filer
        row = {
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
            "labels_json": "[]" if own else _labels_json(concept.labels),
            "documentation": concept.documentation,
            "references_json": (None if not concept.references else _json(concept.references)),
            "labels_rank": 0 if own else 1,
        }
        rows.append(
            {
                **row,
                "_row_hash": stable_json_hash({"taxonomy_concept": row}),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=taxonomy_concepts_schema())


def concept_labels_table(bundle: XbrlExtractionBundle) -> pa.Table:
    """The filing's labels for standard concepts it labels itself (`xbrl_concept_labels`)."""
    filer = filer_labelled_concepts(bundle)
    rows = []
    for concept in bundle.concepts:
        if concept.is_extension or concept.qname not in filer:
            continue
        labels = _labels_json(concept.labels)
        rows.append(
            {
                "accession": bundle.filing.accession,
                "concept_namespace": concept.namespace,
                "concept_name": concept.name,
                "concept_qname": concept.qname,
                "labels_json": labels,
                "extraction_revision": bundle.extraction_revision,
                "_row_hash": stable_json_hash(
                    {
                        "accession": bundle.filing.accession,
                        "concept": concept.qname,
                        "labels": labels,
                        "revision": bundle.extraction_revision,
                    }
                ),
                "_ingested_at": bundle.metadata.extracted_at,
            }
        )
    return pa.Table.from_pylist(rows, schema=concept_labels_schema())


def silver_concepts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    """Every concept of the filing (silver Parquet keeps the full set)."""
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
    """Facts for ClickHouse: `raw_value` is dropped when it equals the typed value.

    `raw_value_scale` then records how the view re-renders it: the number of
    fraction digits of a numeric value, or 0 for text equal to `value_text`.
    """
    rows = []
    for row, fact in zip(_fact_rows(bundle), bundle.facts, strict=True):
        scale = raw_value_scale(fact)
        raw = None if scale is not None else row["raw_value"]
        rows.append({**row, "raw_value": raw, "raw_value_scale": scale})
    return pa.Table.from_pylist(rows, schema=clickhouse_facts_schema())


def silver_facts_table(bundle: XbrlExtractionBundle) -> pa.Table:
    """Facts with every raw value (silver Parquet stays lossless)."""
    return pa.Table.from_pylist(_fact_rows(bundle), schema=xbrl_warehouse_schema("facts"))


_PLAIN_DECIMAL = re.compile(r"-?\d+(?:\.(\d+))?")


def raw_value_scale(fact: XbrlFact) -> int | None:
    """None to keep `raw_value`; otherwise how to rebuild it exactly from the typed value.

    Numeric: `raw_value == str(value_decimal)` in plain notation with at most 18
    fraction digits (the stored scale) gives the fraction digit count. Text:
    `raw_value == value_text` gives 0. Nil, boolean, and date facts, and facts
    without a typed value, keep their raw value.
    """
    if fact.raw_value is None or fact.is_nil:
        return None
    if fact.value_kind is FactValueKind.NUMERIC and fact.value_decimal is not None:
        rendered = str(fact.value_decimal)
        match = _PLAIN_DECIMAL.fullmatch(rendered)
        if rendered != fact.raw_value or match is None:
            return None
        digits = len(match.group(1) or "")
        return digits if digits <= 18 else None
    if fact.value_kind is FactValueKind.TEXT and fact.value_text is not None:
        return 0 if fact.raw_value == fact.value_text else None
    return None


def _fact_rows(bundle: XbrlExtractionBundle) -> list[dict[str, Any]]:
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
                accepted_at=bundle.filing.accepted_at,
                ingested_at=bundle.metadata.extracted_at,
            )
        )
    return rows


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
    accepted_at: datetime | None = None,
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
        "accepted_at": accepted_at,
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
        "accepted_at": fact.accepted_at,
        "concept_namespace": fact.concept_namespace,
        "concept_qname": fact.concept_qname,
        "fiscal_year": fact.fiscal_year,
        "fiscal_period": fact.fiscal_period,
        "report_date": fact.report_date,
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
        if not declaration or declaration.startswith(("--", "INDEX ")):
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


def _is_hash_encoding(table: str, column: str, source: str, target: str) -> bool:
    """A hex hash column (FixedString(64)/String) becoming 32 raw bytes."""
    source, target = _normalize_sql(source), _normalize_sql(target)
    if column in HASH_COLUMNS.get(table, frozenset()):
        return target == "fixedstring(32)" and source in {"fixedstring(64)", "string"}
    if column in HASH_ARRAY_COLUMNS.get(table, frozenset()):
        return target == "array(fixedstring(32))" and source in {
            "array(fixedstring(64))",
            "array(string)",
        }
    return False


def _hash_projection(column: str, target: str) -> str:
    """SQL matching `hash_bytes`: hex digests -> bytes, blanks -> zero bytes, else SHA-256."""
    one = (
        "if(match(toString({x}), '^[0-9a-fA-F]{{64}}$'), unhex(toString({x})), "
        "if(replaceAll(toString({x}), '\\0', '') = '', '', "
        "toString(SHA256(replaceAll(toString({x}), '\\0', '')))))"
    )
    if _normalize_sql(target).startswith("array("):
        return f"arrayMap(x -> {one.format(x='x')}, {column}) AS {column}"
    return f"{one.format(x=column)} AS {column}"


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
