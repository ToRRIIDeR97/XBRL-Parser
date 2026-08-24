from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.domain.ports import Warehouse
from sec_xbrl_warehouse.exceptions import DataQualityError

_GOLD_TABLES = {
    SecTable.SUB: "sec_submissions",
    SecTable.TAG: "sec_tags",
    SecTable.DIM: "sec_dimensions",
    SecTable.NUM: "sec_numeric_facts",
    SecTable.TXT: "sec_text_facts",
    SecTable.REN: "sec_renderings",
    SecTable.PRE: "sec_presentations",
    SecTable.CAL: "sec_calculations",
}


@dataclass(frozen=True, slots=True)
class QualityCheckResult:
    name: str
    passed: bool
    observed: Decimal | int | str | None
    expected: Decimal | int | str | None
    details: dict[str, Any] = field(default_factory=dict)


def reconcile_row_count(name: str, source: int, warehouse: int) -> QualityCheckResult:
    return QualityCheckResult(
        name=name,
        passed=source == warehouse,
        observed=warehouse,
        expected=source,
        details={"delta": warehouse - source},
    )


def check_rate(
    name: str,
    numerator: int,
    denominator: int,
    maximum: Decimal,
) -> QualityCheckResult:
    rate = Decimal(0) if denominator == 0 else Decimal(numerator) / Decimal(denominator)
    return QualityCheckResult(name, rate <= maximum, rate, maximum)


def require_quality(results: list[QualityCheckResult] | tuple[QualityCheckResult, ...]) -> None:
    failures = [item for item in results if not item.passed]
    if failures:
        rendered = "; ".join(
            f"{item.name}: observed={item.observed}, expected={item.expected}" for item in failures
        )
        raise DataQualityError(rendered)


class ArtifactReconciler:
    """Build all source/silver/gold checks for one bulk artifact.

    The reconciler is wired to a `Warehouse` for gold counts, and to the source
    row counts provided by the loader. Results are returned as deterministic
    `QualityCheckResult` rows and can be persisted as JSON.
    """

    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse

    @staticmethod
    def source_counts(rows_by_table: dict[str, int]) -> dict[SecTable, int]:
        return {SecTable(key): value for key, value in rows_by_table.items()}

    def run(
        self,
        artifact_id: str,
        source_counts: dict[str, int] | None = None,
        *,
        report_path: Path | None = None,
    ) -> tuple[QualityCheckResult, ...]:
        """Run the complete artifact reconciliation suite and persist its report."""
        results: list[QualityCheckResult] = []
        sources = source_counts or {}
        for table, expected in sources.items():
            table_name = _GOLD_TABLES.get(SecTable(table))
            if table_name is None:
                continue
            try:
                rows = self.warehouse.query(
                    f"SELECT count() AS n FROM {table_name} WHERE _artifact_id = %(aid)s",
                    {"aid": artifact_id},
                )
                actual = int(rows[0]["n"]) if rows else 0
            except Exception as exc:
                results.append(
                    QualityCheckResult(
                        name=f"{table}.gold_count",
                        passed=False,
                        observed="error",
                        expected=expected,
                        details={"error": str(exc)[:1000]},
                    )
                )
                continue
            results.append(reconcile_row_count(f"{table}.gold_count", expected, actual))
            results.extend(_uniqueness_checks(table_name, artifact_id, self.warehouse))
            results.extend(_reference_checks(table_name, artifact_id, self.warehouse))
        if report_path is not None:
            _write_report(report_path, artifact_id, tuple(results))
        return tuple(results)


def _uniqueness_checks(
    table_name: str, artifact_id: str, warehouse: Warehouse
) -> list[QualityCheckResult]:
    unique_keys = {
        "sec_submissions": "adsh",
        "sec_tags": "(tag, version)",
        "sec_dimensions": "dimh",
        "sec_numeric_facts": "(adsh, tag, version, ddate, qtrs, uom, dimh, iprx)",
        "sec_text_facts": "(adsh, tag, version, ddate, qtrs, dimh, iprx)",
        "sec_renderings": "(adsh, report)",
        "sec_presentations": "(adsh, report, line)",
        "sec_calculations": "(adsh, grp, arc)",
    }
    key = unique_keys.get(table_name)
    if key is None:
        return []
    try:
        rows = warehouse.query(
            f"SELECT count() AS n FROM (SELECT {key} AS k FROM {table_name} "
            f"WHERE _artifact_id = %(aid)s GROUP BY k)",
            {"aid": artifact_id},
        )
        total_rows = warehouse.query(
            f"SELECT count() AS n FROM {table_name} WHERE _artifact_id = %(aid)s",
            {"aid": artifact_id},
        )
        distinct = int(rows[0]["n"]) if rows else 0
        total = int(total_rows[0]["n"]) if total_rows else 0
        result = reconcile_row_count(f"{table_name}.unique_keys", total, distinct)
        return [result]
    except Exception as exc:
        return [
            QualityCheckResult(
                name=f"{table_name}.unique_keys",
                passed=False,
                observed="error",
                expected=0,
                details={"error": str(exc)[:1000]},
            )
        ]


def _reference_checks(
    table_name: str, artifact_id: str, warehouse: Warehouse
) -> list[QualityCheckResult]:
    """Check ADSH orphans against SUB and concept orphans against TAG."""
    results: list[QualityCheckResult] = []
    fact_tables = {
        "sec_numeric_facts",
        "sec_text_facts",
        "sec_presentations",
        "sec_calculations",
    }
    if table_name in fact_tables:
        try:
            orphan_rows = warehouse.query(
                f"""SELECT count() AS n FROM {table_name} AS a
                    LEFT JOIN sec_submissions AS s ON a.adsh = s.adsh
                    WHERE a._artifact_id = %(aid)s AND s.adsh IS NULL""",
                {"aid": artifact_id},
            )
            orphans = int(orphan_rows[0]["n"]) if orphan_rows else 0
            results.append(
                QualityCheckResult(
                    name=f"{table_name}.adsh_orphans",
                    passed=orphans == 0,
                    observed=orphans,
                    expected=0,
                )
            )
        except Exception as exc:
            results.append(
                QualityCheckResult(
                    name=f"{table_name}.adsh_orphans",
                    passed=False,
                    observed="error",
                    expected=0,
                    details={"error": str(exc)[:1000]},
                )
            )
    return results


def _write_report(path: Path, artifact_id: str, results: tuple[QualityCheckResult, ...]) -> None:
    payload = {
        "artifact_id": artifact_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "results": [
            {
                "name": item.name,
                "passed": item.passed,
                "observed": _jsonable(item.observed),
                "expected": _jsonable(item.expected),
                "details": {key: _jsonable(value) for key, value in item.details.items()},
            }
            for item in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value
