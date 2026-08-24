from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sec_xbrl_warehouse.domain.ports import Warehouse


class FactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cik: int | None = Field(default=None, ge=0)
    accession: str | None = None
    concept_namespace: str | None = None
    concept_name: str | None = None
    filed_from: date | None = None
    filed_to: date | None = None
    limit: int = Field(default=500, ge=1, le=10_000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_scan_bound(self) -> FactQuery:
        if not any(
            (
                self.cik is not None,
                self.accession,
                self.concept_name,
                self.filed_from,
                self.filed_to,
            )
        ):
            raise ValueError(
                "At least one company, filing, concept, or date constraint is required"
            )
        return self


class QueryService:
    """Safe, bounded ClickHouse query façade for API and CLI consumers."""

    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse

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
                concept_namespace, concept_name, concept_qname,
                raw_value, value_decimal, value_text, is_nil,
                period_kind, period_start, period_end, period_instant,
                dimensions_json, dimension_hash, source_document,
                extraction_revision
            FROM xbrl_facts
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
    ) -> list[dict[str, Any]]:
        clauses = ["cik = %(cik)s", "canonical_metric = %(metric)s"]
        params: dict[str, Any] = {"cik": cik, "metric": metric, "limit": limit}
        if filed_from:
            clauses.append("filed_date >= %(filed_from)s")
            params["filed_from"] = filed_from
        if filed_to:
            clauses.append("filed_date <= %(filed_to)s")
            params["filed_to"] = filed_to
        return self.warehouse.query(
            f"""
            SELECT *
            FROM canonical_facts
            WHERE {" AND ".join(clauses)}
            ORDER BY coalesce(period_end, period_instant), filed_date, accession
            LIMIT %(limit)s
            """,
            params,
        )

    def filing_summary(self, accession: str) -> dict[str, Any] | None:
        rows = self.warehouse.query(
            """
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
            FROM xbrl_facts
            WHERE accession = %(accession)s
            GROUP BY accession
            """,
            {"accession": accession},
        )
        return rows[0] if rows else None
