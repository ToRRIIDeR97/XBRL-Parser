"""Rebuild a company's standardized metric series in ClickHouse.

`rebuild_metric_series` reads the company's canonical facts (latest published
extraction revision per filing), builds the series with
`normalization.series.build_series`, and writes the `metric_series` table:

- `as_reported` points once, with `known_from = available_at`;
- `latest` points as versions: the series is rebuilt as of each filing's
  acceptance time and a new version row is written whenever a point changes, so
  an as-of query reads the newest version known at its cutoff.

The table is derived and fully rebuildable from `canonical_facts`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import pyarrow as pa

from sec_xbrl_warehouse.mapping.namespaces import namespace_family
from sec_xbrl_warehouse.normalization.series import (
    DEFAULT_FORMULAS,
    MetricObservation,
    SeriesPoint,
    build_series,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_PRIORITY = 100


class SeriesWarehouse(Protocol):
    database: str

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def insert_metric_series(self, table: Any) -> int: ...


_OBSERVATIONS_SQL = """
SELECT accession, cik, form, filed_date, accepted_at, fiscal_year, fiscal_period, report_date,
       canonical_metric, concept_namespace, concept_qname, normalized_value_decimal,
       normalized_unit, period_start, period_end, period_instant, dimensions_json
FROM canonical_facts FINAL
WHERE cik = %(cik)s
  AND normalized_value_decimal IS NOT NULL
  AND dimensions_json = '[]'
  AND (accession, extraction_revision) IN (
      -- The latest published revision per filing; filings without an
      -- extraction record (canonical facts loaded on their own) use their
      -- newest canonical revision.
      SELECT accession, max(extraction_revision)
      FROM xbrl_extractions
      WHERE accession IN (SELECT accession FROM canonical_facts WHERE cik = %(cik)s)
      GROUP BY accession
      UNION ALL
      SELECT accession, max(extraction_revision)
      FROM canonical_facts
      WHERE cik = %(cik)s
        AND accession NOT IN (SELECT accession FROM xbrl_extractions)
      GROUP BY accession
  )
ORDER BY mapping_version DESC, _ingested_at DESC
LIMIT 1 BY accession, raw_fact_id, canonical_metric
"""


def load_observations(
    warehouse: SeriesWarehouse,
    cik: int,
    priorities: Mapping[tuple[str, str], int] | None = None,
) -> list[MetricObservation]:
    """Consolidated canonical observations for one company.

    `priorities` maps `(namespace family, concept name)` to the seed priority
    (plan 06); other concepts rank after every seeded one. A missing acceptance
    time falls back to midnight UTC of the filing date.
    """
    rows = warehouse.query(_OBSERVATIONS_SQL, {"cik": cik})
    observations: list[MetricObservation] = []
    for row in rows:
        qname = row.get("concept_qname") or ""
        family = namespace_family(row.get("concept_namespace") or "")
        priority = (priorities or {}).get(
            (family or "", qname.rsplit(":", 1)[-1]), DEFAULT_PRIORITY
        )
        accepted = row.get("accepted_at")
        if accepted is None and row.get("filed_date") is not None:
            accepted = datetime.combine(row["filed_date"], datetime.min.time(), tzinfo=UTC)
        observations.append(
            MetricObservation(
                cik=int(row["cik"]),
                metric=str(row["canonical_metric"]),
                concept_qname=qname,
                accession=str(row["accession"]),
                form=str(row["form"]),
                accepted_at=_aware(accepted),
                fiscal_year=_optional_int(row.get("fiscal_year")),
                fiscal_period=row.get("fiscal_period"),
                filing_period_end=row.get("report_date"),
                period_start=row.get("period_start"),
                period_end=row.get("period_end"),
                period_instant=row.get("period_instant"),
                value=Decimal(str(row["normalized_value_decimal"])),
                unit=row.get("normalized_unit"),
                priority=priority,
                dimensions_json=str(row.get("dimensions_json") or "[]"),
            )
        )
    return observations


def series_versions(
    observations: list[MetricObservation],
    formulas: Mapping[str, str] | None = None,
    non_additive: Collection[str] | None = None,
) -> list[tuple[str, datetime, SeriesPoint]]:
    """`(view, known_from, point)` rows for both views (see the module docstring)."""
    rows: list[tuple[str, datetime, SeriesPoint]] = [
        ("as_reported", point.available_at or EPOCH, point)
        for point in build_series(
            observations, view="as_reported", formulas=formulas, non_additive=non_additive
        )
    ]
    cutoffs = sorted({item.accepted_at for item in observations if item.accepted_at is not None})
    current: dict[tuple[str, str, date | None, date], tuple[Any, ...]] = {}
    for cutoff in cutoffs:
        for point in build_series(
            observations,
            view="latest",
            as_of=cutoff,
            formulas=formulas,
            non_additive=non_additive,
        ):
            key = (point.metric, point.period_type, point.period_start, point.period_end)
            signature = (
                point.value,
                point.unit,
                point.derivation,
                point.concept_qname,
                point.source_accessions,
                point.available_at,
                point.fiscal_year,
                point.fiscal_quarter,
            )
            if current.get(key) != signature:
                current[key] = signature
                rows.append(("latest", cutoff, point))
    return rows


def rebuild_metric_series(
    warehouse: SeriesWarehouse,
    cik: int,
    *,
    formulas: Mapping[str, str] | None = None,
    priorities: Mapping[tuple[str, str], int] | None = None,
    non_additive: Collection[str] | None = None,
    built_at: datetime | None = None,
) -> int:
    """Rebuild one company's `metric_series` rows; returns the number written.

    Readers always use the company's newest `built_at`, so a rebuild is
    idempotent and atomic for them; `ReplacingMergeTree(built_at)` merges replace
    older builds' rows, so no delete mutation is issued.
    """
    observations = load_observations(warehouse, cik, priorities)
    build = _aware(built_at) or datetime.now(UTC)
    rows = [
        _row(view, known_from, point, build)
        for view, known_from, point in series_versions(
            observations,
            formulas if formulas is not None else DEFAULT_FORMULAS,
            non_additive,
        )
    ]
    return warehouse.insert_metric_series(pa.Table.from_pylist(rows, schema=metric_series_schema()))


def metric_series_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("cik", pa.uint64(), False),
            pa.field("metric", pa.string(), False),
            pa.field("view", pa.string(), False),
            pa.field("period_type", pa.string(), False),
            pa.field("fiscal_year", pa.uint16()),
            pa.field("fiscal_quarter", pa.uint8()),
            pa.field("period_start", pa.date32()),
            pa.field("period_end", pa.date32(), False),
            pa.field("value", pa.decimal256(76, 18), False),
            pa.field("unit", pa.string()),
            pa.field("derivation", pa.string(), False),
            pa.field("concept_qname", pa.string()),
            pa.field("source_accessions", pa.list_(pa.string()), False),
            pa.field("available_at", pa.timestamp("ms", tz="UTC")),
            pa.field("known_from", pa.timestamp("ms", tz="UTC"), False),
            pa.field("built_at", pa.timestamp("ms", tz="UTC"), False),
        ]
    )


def _row(view: str, known_from: datetime, point: SeriesPoint, built_at: datetime) -> dict[str, Any]:
    return {
        "cik": point.cik,
        "metric": point.metric,
        "view": view,
        "period_type": point.period_type,
        "fiscal_year": point.fiscal_year,
        "fiscal_quarter": point.fiscal_quarter,
        "period_start": point.period_start,
        "period_end": point.period_end,
        "value": point.value,
        "unit": point.unit,
        "derivation": point.derivation,
        "concept_qname": point.concept_qname,
        "source_accessions": list(point.source_accessions),
        "available_at": point.available_at,
        "known_from": known_from,
        "built_at": built_at,
    }


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def non_additive_from_metrics(metrics: Mapping[str, Any]) -> frozenset[str]:
    """Metric codes marked `additive: false` in a `CanonicalMapper.metrics` mapping."""
    return frozenset(
        code for code, metric in metrics.items() if not getattr(metric, "additive", True)
    )


def formulas_from_metrics(metrics: Mapping[str, Any]) -> dict[str, str]:
    """`{code: formula}` for formula metrics in a `CanonicalMapper.metrics` mapping."""
    return {
        code: metric.formula for code, metric in metrics.items() if getattr(metric, "formula", None)
    }
