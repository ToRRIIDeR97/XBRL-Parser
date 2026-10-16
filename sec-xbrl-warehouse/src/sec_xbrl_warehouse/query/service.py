from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sec_xbrl_warehouse.config_files import load_yaml
from sec_xbrl_warehouse.domain.ports import Warehouse
from sec_xbrl_warehouse.domain.sources import (
    FILING_KEY_MAX_LENGTH,
    FILING_KEY_PATTERN,
    is_filing_key,
)


class FactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cik: int | None = Field(default=None, ge=0)
    accession: str | None = Field(
        default=None, pattern=FILING_KEY_PATTERN, max_length=FILING_KEY_MAX_LENGTH
    )
    concept_namespace: str | None = Field(default=None, max_length=2048)
    concept_name: str | None = Field(default=None, max_length=512)
    filed_from: date | None = None
    filed_to: date | None = None
    limit: int = Field(default=500, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def require_scan_bound(self) -> FactQuery:
        if self.filed_from and self.filed_to and self.filed_from > self.filed_to:
            raise ValueError("filed_from must not exceed filed_to")
        if (
            self.cik is None
            and not self.accession
            and not (
                self.filed_from and self.filed_to and (self.filed_to - self.filed_from).days <= 366
            )
        ):
            raise ValueError(
                "A company, accession, or complete date window of at most 366 days is required"
            )
        return self


class QueryService:
    """Safe, bounded ClickHouse query façade for API and CLI consumers."""

    def __init__(self, warehouse: Warehouse, metrics_path: Path | None = None) -> None:
        self.warehouse = warehouse
        self.metrics: list[dict[str, Any]] = (
            list(load_yaml(metrics_path).get("metrics", [])) if metrics_path else []
        )
        self.derived = _derived_metrics(metrics_path) if metrics_path else {}

    def raw_facts(self, query: FactQuery) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: dict[str, Any] = {"limit": query.limit, "offset": query.offset}
        for field, value in (
            ("cik", query.cik),
            ("accession", query.accession),
            ("concept_namespace", query.concept_namespace),
            ("concept_name", query.concept_name),
        ):
            if value is not None:
                clauses.append(f"{field} = %({field})s")
                params[field] = value
        if query.filed_from:
            clauses.append("filed_date >= %(filed_from)s")
            params["filed_from"] = query.filed_from
        if query.filed_to:
            clauses.append("filed_date <= %(filed_to)s")
            params["filed_to"] = query.filed_to
        sql = f"""
            SELECT
                fact_id, accession, cik, company_name, form, filed_date,
                fiscal_year, fiscal_period,
                concept_namespace, concept_name, concept_qname,
                value_kind, raw_value, value_decimal, value_text, value_boolean, value_date, is_nil,
                context_id, context_hash, unit_id, unit_numerator_json, unit_denominator_json,
                taxonomy_version, decimals, precision, language, source_fact_id, footnote_ids,
                period_kind, period_start, period_end, period_instant,
                entity_scheme, entity_identifier, dimensions_json, dimension_hash,
                tuple_path, duplicate_ordinal, duplicate_set_hash,
                source_document, source_attributes_json,
                extraction_revision
            FROM v_xbrl_facts_latest
            WHERE {" AND ".join(clauses)}
            ORDER BY filed_date DESC, accession DESC, concept_name, fact_id
            LIMIT %(limit)s OFFSET %(offset)s
        """
        return self.warehouse.query(sql, params)

    def canonical_series(
        self,
        *,
        cik: int,
        metric: str,
        filed_from: date | None = None,
        filed_to: date | None = None,
        limit: int = 5_000,
        offset: int = 0,
        include_dimensions: bool = False,
    ) -> list[dict[str, Any]]:
        if metric in self.derived:
            return self._derived_series(
                cik=cik,
                metric=metric,
                filed_from=filed_from,
                filed_to=filed_to,
                limit=limit,
                offset=offset,
                include_dimensions=include_dimensions,
            )
        clauses = ["cik = %(cik)s", "canonical_metric = %(metric)s"]
        FactQuery(cik=cik, filed_from=filed_from, filed_to=filed_to, limit=limit, offset=offset)
        params: dict[str, Any] = {"cik": cik, "metric": metric, "limit": limit, "offset": offset}
        if filed_from:
            clauses.append("filed_date >= %(filed_from)s")
            params["filed_from"] = filed_from
        if filed_to:
            clauses.append("filed_date <= %(filed_to)s")
            params["filed_to"] = filed_to
        if not include_dimensions:
            clauses.append("dimensions_json = '[]'")
        return self.warehouse.query(
            f"""
            SELECT *
            FROM v_canonical_facts_latest
            WHERE {" AND ".join(clauses)}
            ORDER BY coalesce(period_end, period_instant), filed_date, accession
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        )

    def _derived_series(
        self,
        *,
        cik: int,
        metric: str,
        filed_from: date | None,
        filed_to: date | None,
        limit: int,
        offset: int,
        include_dimensions: bool,
    ) -> list[dict[str, Any]]:
        left, operator, right, formula = self.derived[metric]
        FactQuery(cik=cik, filed_from=filed_from, filed_to=filed_to, limit=limit, offset=offset)
        clauses = [
            "source.cik = %(cik)s",
            "source.canonical_metric IN (%(left)s, %(right)s)",
        ]
        params: dict[str, Any] = {
            "cik": cik,
            "left": left,
            "right": right,
            "metric": metric,
            "formula": formula,
            "limit": limit,
            "offset": offset,
        }
        if filed_from:
            clauses.append("source.filed_date >= %(filed_from)s")
            params["filed_from"] = filed_from
        if filed_to:
            clauses.append("source.filed_date <= %(filed_to)s")
            params["filed_to"] = filed_to
        if not include_dimensions:
            clauses.append("source.dimensions_json = '[]'")
        return self.warehouse.query(
            f"""
            SELECT accession, cik, any(filed_date) AS filed_date, any(form) AS form,
                   %(metric)s AS canonical_metric,
                   maxIf(normalized_value_decimal, source.canonical_metric = %(left)s)
                       {operator} maxIf(normalized_value_decimal, source.canonical_metric = %(right)s)
                       AS normalized_value_decimal,
                   any(normalized_unit) AS normalized_unit,
                   any(period_kind) AS period_kind, period_start, period_end, period_instant,
                   dimensions_json,
                   arraySort(groupArray(raw_fact_id)) AS raw_fact_ids,
                   arraySort(groupArray(tuple(toString(mapping_id), mapping_version))) AS mapping_lineage,
                   %(formula)s AS formula,
                   min(mapping_confidence) AS mapping_confidence,
                   max(extraction_revision) AS extraction_revision
            FROM v_canonical_facts_latest AS source
            WHERE {" AND ".join(clauses)}
              AND normalized_value_decimal IS NOT NULL
            GROUP BY accession, cik, period_start, period_end, period_instant,
                     dimensions_json, normalized_unit
            HAVING countIf(source.canonical_metric = %(left)s) = 1
               AND countIf(source.canonical_metric = %(right)s) = 1
               AND countDistinct(normalized_unit) = 1
            ORDER BY coalesce(period_end, period_instant), filed_date, accession
            LIMIT %(limit)s OFFSET %(offset)s
        """,
            params,
        )

    def metric_series(
        self,
        *,
        cik: int,
        metrics: Sequence[str],
        view: str = "latest",
        as_of: datetime | None = None,
        period_types: Sequence[str] | None = None,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        """Standardized series points from the company's newest `metric_series` build.

        `latest` returns each point's newest version known at `as_of` (now when
        omitted); `as_reported` returns first-reported values available by then.
        """
        if cik < 0 or not 1 <= limit <= 10_000:
            raise ValueError("Invalid company or limit")
        if view not in {"latest", "as_reported"}:
            raise ValueError("view must be latest or as_reported")
        selected = sorted({metric.strip() for metric in metrics if metric.strip()})
        if not selected or len(selected) > 50:
            raise ValueError("Request between 1 and 50 metrics")
        if any(re.fullmatch(r"[a-z][a-z0-9_]{0,127}", metric) is None for metric in selected):
            raise ValueError("Invalid metric code")
        kinds = sorted(set(period_types or ())) or None
        if kinds and not set(kinds) <= {"quarter", "annual", "ttm", "instant"}:
            raise ValueError("Unknown period type")
        params: dict[str, Any] = {"cik": cik, "view": view, "metrics": selected, "limit": limit}
        filters = ""
        if as_of is not None:
            filters += " AND known_from <= %(as_of)s"
            params["as_of"] = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        if kinds:
            filters += " AND period_type IN %(period_types)s"
            params["period_types"] = kinds
        return self.warehouse.query(
            f"""
            SELECT metric, view, period_type, fiscal_year, fiscal_quarter, period_start,
                   period_end, value, unit, derivation, concept_qname, source_accessions,
                   available_at, known_from
            FROM metric_series
            WHERE cik = %(cik)s
              AND built_at = (SELECT max(built_at) FROM metric_series WHERE cik = %(cik)s)
              AND view = %(view)s
              AND metric IN %(metrics)s{filters}
            ORDER BY metric, period_type, period_end, known_from DESC
            LIMIT 1 BY metric, period_type, period_start, period_end
            LIMIT %(limit)s
            """,
            params,
        )

    def cik_for_ticker(self, ticker: str) -> int | None:
        """Resolve a ticker through the ClickHouse `companies` mirror (newest holder wins)."""
        symbol = ticker.strip().upper()
        if re.fullmatch(r"[A-Z0-9.\-]{1,16}", symbol) is None:
            raise ValueError("Invalid ticker")
        rows = self.warehouse.query(
            """
            SELECT cik FROM companies FINAL
            WHERE has(tickers, %(ticker)s)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"ticker": symbol},
        )
        return int(rows[0]["cik"]) if rows else None

    def search_companies(self, text: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Companies in the warehouse whose ticker or name matches `text` (tickers first)."""
        term = text.strip()
        if not 1 <= len(term) <= 64 or not 1 <= limit <= 100:
            raise ValueError("Search text must be 1 to 64 characters and limit 1 to 100")
        return self.warehouse.query(
            """
            SELECT cik, name, tickers, exchanges, sic, fiscal_year_end
            FROM companies FINAL
            WHERE has(tickers, upper(%(term)s))
               OR positionCaseInsensitiveUTF8(name, %(term)s) > 0
               OR toString(cik) = %(term)s
            ORDER BY has(tickers, upper(%(term)s)) DESC, name
            LIMIT %(limit)s
            """,
            {"term": term, "limit": limit},
        )

    def company_filings(
        self, cik: int, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """One row per loaded filing of the company, newest first (from cover pages)."""
        if cik < 0:
            raise ValueError("Invalid company")
        _check_page(limit, offset, max_limit=1_000)
        rows = self.warehouse.query(
            """
            SELECT accession, form, filed_date, document_type, period_end_date,
                   registrant_name, amendment, extraction_revision
            FROM v_cover_pages_latest
            WHERE cik = %(cik)s
            ORDER BY filed_date DESC, accession DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"cik": cik, "limit": limit, "offset": offset},
        )
        for row in rows:
            if row["amendment"] is not None:
                row["amendment"] = bool(row["amendment"])
        return rows

    def metric_catalogue(self) -> list[dict[str, Any]]:
        """Canonical metric codes with their name, statement, and unit family."""
        keys = ("code", "name", "statement", "period_type", "unit_family", "description")
        return [{key: item.get(key) for key in keys} for item in self.metrics]

    def filing_summary(
        self, accession: str, *, cik: int | None = None, filed_date: date | None = None
    ) -> dict[str, Any] | None:
        _check_accession(accession)
        filters = ""
        params: dict[str, Any] = {"accession": accession}
        if cik is not None:
            filters += " AND cik = %(cik)s"
            params["cik"] = cik
        if filed_date is not None:
            filters += " AND filed_date = %(filed_date)s"
            params["filed_date"] = filed_date
        rows = self.warehouse.query(
            f"""
            SELECT
                accession,
                any(cik) AS cik,
                any(company_name) AS company_name,
                any(form) AS form,
                any(filed_date) AS filed_date,
                count() AS fact_count,
                uniqExact(concept_qname) AS concept_count,
                uniqExact(context_hash) AS context_count,
                max(extraction_revision) AS latest_extraction_revision
            FROM v_xbrl_facts_latest
            WHERE accession = %(accession)s {filters}
            GROUP BY accession
            """,
            params,
        )
        return rows[0] if rows else None

    def cover_page(self, accession: str) -> dict[str, Any] | None:
        """The filing's cover page from its newest published extraction revision."""
        _check_accession(accession)
        rows = self.warehouse.query(
            """
            SELECT accession, cik, form, filed_date, document_type, period_end_date,
                   registrant_name, filer_category, amendment, trading_symbol, exchange,
                   auditor_name, shares_outstanding, shares_outstanding_date, public_float,
                   security_members, security_titles, security_symbols, security_exchanges,
                   extraction_revision
            FROM v_cover_pages_latest
            WHERE accession = %(accession)s
            LIMIT 1
            """,
            {"accession": accession},
        )
        if not rows:
            return None
        row = rows[0]
        securities = zip(
            row.pop("security_members"),
            row.pop("security_titles"),
            row.pop("security_symbols"),
            row.pop("security_exchanges"),
            strict=True,
        )
        if row["amendment"] is not None:
            row["amendment"] = bool(row["amendment"])
        for column in ("shares_outstanding", "public_float"):
            row[column] = _json_number(row[column])
        row["securities"] = [
            {"member": member, "title": title, "symbol": symbol, "exchange": exchange}
            for member, title, symbol, exchange in securities
        ]
        return row

    def segment_facts(
        self,
        accession: str,
        *,
        kinds: Sequence[str] | None = None,
        concept: str | None = None,
        include_eliminations: bool = True,
        limit: int = 1_000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Segment, product, geographic, and consolidation rows of one filing."""
        _check_accession(accession)
        _check_page(limit, offset, max_limit=10_000)
        selected = sorted(set(kinds or ()))
        if not set(selected) <= BREAKDOWN_KINDS:
            raise ValueError("Unknown breakdown kind")
        params: dict[str, Any] = {"accession": accession, "limit": limit, "offset": offset}
        filters = ""
        if selected:
            filters += " AND hasAny(member_axes, %(kinds)s)"
            params["kinds"] = selected
        if concept is not None:
            filters += " AND concept_qname = %(concept)s"
            params["concept"] = _check_qname(concept)
        if not include_eliminations:
            filters += " AND is_elimination = 0"
        rows = self.warehouse.query(
            f"""
            SELECT fact_id, concept_qname, axis AS kind, member_label,
                   member_axes, member_qnames, member_labels,
                   value, unit, period_start, period_end, period_instant, is_elimination
            FROM v_segment_facts_latest
            WHERE accession = %(accession)s{filters}
            ORDER BY concept_qname, kind, member_label,
                     coalesce(period_end, period_instant), fact_id
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        )
        for row in rows:
            members = zip(
                row.pop("member_axes"),
                row.pop("member_qnames"),
                row.pop("member_labels"),
                strict=True,
            )
            row["members"] = [
                {"kind": kind, "qname": qname, "label": label} for kind, qname, label in members
            ]
            row["is_elimination"] = bool(row["is_elimination"])
            row["value"] = _json_number(row["value"])
        return rows

    def text_blocks(
        self,
        accession: str,
        *,
        policy: bool | None = None,
        concept: str | None = None,
        include_text: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Text blocks of one filing; the HTML itself only when `include_text` is set.

        `limit` defaults to 200, or 100 with `include_text`, which also caps it at 100.
        """
        _check_accession(accession)
        limit = text_block_limit(limit, include_text=include_text)
        _check_page(limit, offset, max_limit=100 if include_text else 1_000)
        params: dict[str, Any] = {"accession": accession, "limit": limit, "offset": offset}
        filters = ""
        if policy is not None:
            filters += " AND is_policy = %(policy)s"
            params["policy"] = int(policy)
        if concept is not None:
            filters += " AND concept_qname = %(concept)s"
            params["concept"] = _check_qname(concept)
        text = ", value_text AS text" if include_text else ""
        rows = self.warehouse.query(
            f"""
            SELECT fact_id, concept_qname, is_policy, period_start, period_end, period_instant,
                   language, dimensions_json,
                   lengthUTF8(coalesce(value_text, '')) AS text_length{text}
            FROM v_text_blocks_latest
            WHERE accession = %(accession)s{filters}
            ORDER BY concept_qname, fact_id
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        )
        for row in rows:
            row["is_policy"] = bool(row["is_policy"])
        return rows


BREAKDOWN_KINDS = frozenset({"segment", "product", "geography", "consolidation"})
QNAME_PATTERN = r"^[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*$"


def text_block_limit(limit: int | None, *, include_text: bool) -> int:
    if limit is not None:
        return limit
    return 100 if include_text else 200


def _json_number(value: Decimal | None) -> int | float | None:
    """A stored decimal as a JSON number: exact when integral, else the nearest float."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _check_accession(accession: str) -> None:
    if not is_filing_key(accession):
        raise ValueError("Invalid accession")


def _check_page(limit: int, offset: int, *, max_limit: int) -> None:
    if not 1 <= limit <= max_limit or not 0 <= offset <= 10_000:
        raise ValueError(f"limit must be 1-{max_limit} and offset 0-10000")


def _check_qname(concept: str) -> str:
    if len(concept) > 512 or re.fullmatch(QNAME_PATTERN, concept) is None:
        raise ValueError("concept must be a prefix:LocalName qname")
    return concept


def _derived_metrics(path: Path) -> dict[str, tuple[str, str, str, str]]:
    metrics = load_yaml(path).get("metrics", [])
    result: dict[str, tuple[str, str, str, str]] = {}
    for item in metrics:
        formula = item.get("formula")
        if not formula:
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_]*)\s*([+-])\s*([a-z][a-z0-9_]*)", formula)
        if match is None:
            raise ValueError(f"Unsupported derived metric formula: {formula}")
        result[str(item["code"])] = (*match.groups(), formula)
    return result
