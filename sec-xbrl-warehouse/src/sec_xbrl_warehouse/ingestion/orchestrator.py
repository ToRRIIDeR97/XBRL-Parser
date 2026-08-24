from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sec_xbrl_warehouse.domain.enums import ArtifactState, DatasetKind, JobType
from sec_xbrl_warehouse.domain.models import BackfillPlan, DatasetArtifact, SourceArtifactRef
from sec_xbrl_warehouse.domain.ports import ControlRepository
from sec_xbrl_warehouse.exceptions import JobStateError
from sec_xbrl_warehouse.ingestion.bulk_loader import BulkArtifactLoader


class IngestionOrchestrator:
    """Create deterministic plans and enqueue idempotent ingestion jobs."""

    def __init__(
        self,
        control: ControlRepository,
        *,
        downloader: Any | None = None,
        loader: BulkArtifactLoader | None = None,
        object_store: Any | None = None,
    ) -> None:
        self.control = control
        self.downloader = downloader
        self.loader = loader
        self.object_store = object_store

    def plan_backfill(
        self,
        *,
        dataset: DatasetKind,
        artifacts: tuple[DatasetArtifact, ...],
        from_year: int,
        to_year: int,
        catalog_url: str,
    ) -> BackfillPlan:
        selected = tuple(
            sorted(
                (item for item in artifacts if from_year <= int(item.period[:4]) <= to_year),
                key=lambda item: (item.period, item.source_url),
            )
        )
        checksum = backfill_plan_checksum(
            dataset=dataset,
            artifacts=selected,
            from_year=from_year,
            to_year=to_year,
            catalog_url=catalog_url,
        )
        return BackfillPlan(
            generated_at=datetime.now(UTC),
            dataset=dataset,
            from_year=from_year,
            to_year=to_year,
            artifacts=selected,
            catalog_url=catalog_url,
            plan_checksum_sha256=checksum,
        )

    def build_backfill_plan(
        self,
        *,
        dataset: DatasetKind,
        artifacts: tuple[DatasetArtifact, ...],
        from_year: int,
        to_year: int,
        catalog_url: str,
    ) -> BackfillPlan:
        """Compatibility alias for callers created before `plan_backfill`."""
        return self.plan_backfill(
            dataset=dataset,
            artifacts=artifacts,
            from_year=from_year,
            to_year=to_year,
            catalog_url=catalog_url,
        )

    async def enqueue_backfill(self, plan: BackfillPlan) -> tuple[str, ...]:
        """Enqueue download jobs; workers enqueue load jobs after acquisition.

        Every payload carries the immutable snapshot identity: the semantic plan
        checksum, plan version, and catalog URL. A stable pre-download
        idempotency key keeps re-enqueueing the same plan safe.
        """
        job_ids: list[str] = []
        for artifact in plan.artifacts:
            key = f"bulk-download:{artifact.dataset.value}:{artifact.period}:{artifact.source_url}"
            payload = artifact.model_dump(mode="json")
            payload.update(
                {
                    "plan_checksum_sha256": plan.plan_checksum_sha256,
                    "plan_version": plan.plan_version,
                    "catalog_url": plan.catalog_url,
                }
            )
            job_id = await self.control.enqueue_job(
                job_type=JobType.DOWNLOAD_BULK.value,
                idempotency_key=key,
                payload=payload,
            )
            job_ids.append(str(job_id))
        return tuple(job_ids)

    async def run_artifact_job(self, payload: dict[str, object]) -> dict[str, object]:
        """Run one versioned bulk artifact job from its checkpoint.

        Validates the payload schema, acquires the bronze ZIP (or re-uses it),
        invokes `BulkArtifactLoader`, and returns a small result object. The
        worker re-enqueues reconciliation after all tables load.
        """
        if self.loader is None:
            raise JobStateError("BulkArtifactLoader is not wired into this orchestrator")
        if "artifact" not in payload:
            raise JobStateError("Bulk artifact job payload is missing 'artifact'")

        artifact_data = payload["artifact"]
        if not isinstance(artifact_data, dict):
            raise JobStateError("Bulk artifact job payload 'artifact' must be an object")
        artifact = SourceArtifactRef.model_validate(artifact_data)
        local_zip = Path(str(payload.get("local_zip") or ""))
        if not os.path.isfile(local_zip):  # noqa: ASYNC240 - cheap startup check
            raise JobStateError(f"Local ZIP for artifact {artifact.id} is unavailable: {local_zip}")

        stats = await self.loader.load_artifact(artifact, local_zip)
        # Enqueue reconciliation once the full load has committed; expected row
        # counts are carried so the final check needs no extra read.
        await self.control.enqueue_job(
            job_type=JobType.RECONCILE_ARTIFACT.value,
            idempotency_key=f"reconcile:{artifact.id}:{artifact.revision}",
            payload={
                "artifact_id": str(artifact.id),
                "revision": artifact.revision,
                "expected_rows": stats.rows_by_table,
            },
        )
        return {
            "artifact_id": str(artifact.id),
            "revision": artifact.revision,
            "rows_by_table": stats.rows_by_table,
            "parquet_objects": list(stats.parquet_objects),
            "validation_issue_count": stats.validation_issue_count,
        }

    async def reconcile_artifact(
        self,
        artifact_id: str,
        expected_rows: dict[str, int] | None = None,
    ) -> dict[str, object]:
        """Run final source/silver/gold checks and promote artifact state."""
        if self.loader is None:
            raise JobStateError("BulkArtifactLoader is not wired into this orchestrator")
        try:
            artifact_uuid = UUID(artifact_id)
        except ValueError as exc:
            raise JobStateError(f"Invalid artifact ID: {artifact_id}") from exc
        try:
            issue_count, details = await self.loader._reconcile(artifact_uuid, expected_rows or {})
            state = ArtifactState.COMPLETE
        except Exception:
            issue_count, details = 1, {"reconciliation": "failed"}
            state = ArtifactState.FAILED
        await self.control.mark_artifact_state(artifact_uuid, state)
        return {"artifact_id": artifact_id, "issue_count": issue_count, "details": details}

    @staticmethod
    def write_plan(plan: BackfillPlan, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")


def backfill_plan_checksum(
    *,
    dataset: DatasetKind,
    artifacts: tuple[DatasetArtifact, ...],
    from_year: int,
    to_year: int,
    catalog_url: str,
) -> str:
    """Hash semantic plan content, excluding the non-deterministic generation time."""
    payload = {
        "dataset": dataset.value,
        "from_year": from_year,
        "to_year": to_year,
        "catalog_url": catalog_url,
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "plan_version": 1,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
