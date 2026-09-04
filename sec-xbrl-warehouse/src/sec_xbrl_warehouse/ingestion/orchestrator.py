from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sec_xbrl_warehouse.domain.enums import ArtifactState, DatasetKind, JobType
from sec_xbrl_warehouse.domain.models import BackfillPlan, DatasetArtifact, SourceArtifactRef
from sec_xbrl_warehouse.domain.ports import ControlRepository
from sec_xbrl_warehouse.exceptions import JobStateError
from sec_xbrl_warehouse.infrastructure.storage.hashing import stable_json_hash
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

    async def enqueue_backfill(
        self, plan: BackfillPlan, *, refresh_unversioned: bool = False
    ) -> tuple[str, ...]:
        """Enqueue download jobs; workers enqueue load jobs after acquisition.

        Every payload carries the immutable snapshot identity: the semantic plan
        checksum, plan version, and catalog URL. A stable pre-download
        idempotency key keeps re-enqueueing the same plan safe.
        """
        job_ids: list[str] = []
        for artifact in plan.artifacts:
            revision_payload: dict[str, Any] = {
                "checksum": artifact.checksum_sha256,
                "etag": artifact.etag,
                "modified": artifact.last_modified,
                "size": artifact.expected_size_bytes,
            }
            if refresh_unversioned and not any(
                (artifact.checksum_sha256, artifact.etag, artifact.last_modified)
            ):
                revision_payload["refresh"] = plan.generated_at.isoformat()
            revision = stable_json_hash(revision_payload)
            key = f"bulk-download:{artifact.dataset.value}:{artifact.period}:{artifact.source_url}:{revision}"
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

    async def download_artifact_job(
        self, payload: dict[str, object], *, job_id: UUID | None = None
    ) -> dict[str, object]:
        if self.downloader is None:
            raise JobStateError("BulkArtifactDownloader is not wired into this orchestrator")
        checkpoint = await self.control.get_checkpoint(job_id, "acquire") if job_id else None
        if checkpoint is None:
            artifact = DatasetArtifact.model_validate(
                {name: payload[name] for name in DatasetArtifact.model_fields if name in payload}
            )
            stored, headers = await self.downloader.download(artifact)
            artifact = artifact.model_copy(
                update={
                    "etag": headers.get("etag", artifact.etag),
                    "last_modified": headers.get("last-modified", artifact.last_modified),
                }
            )
            artifact_id = await self.control.upsert_source_artifact(
                artifact, checksum_sha256=stored.checksum_sha256, object_key=stored.key
            )
            source = await self.control.get_source_artifact(artifact_id)
            checkpoint = source.model_dump(mode="json")
            if job_id:
                await self.control.save_checkpoint(job_id, "acquire", checkpoint)
        source = SourceArtifactRef.model_validate(checkpoint)
        load_job = await self.control.enqueue_job(
            job_type=JobType.LOAD_BULK.value,
            idempotency_key=f"bulk-load:{source.id}:{source.revision}",
            payload={"artifact": source.model_dump(mode="json")},
        )
        return {
            "artifact_id": str(source.id),
            "revision": source.revision,
            "load_job_id": str(load_job),
        }

    async def run_artifact_job(
        self, payload: dict[str, object], *, job_id: UUID | None = None
    ) -> dict[str, object]:
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
        if self.object_store is None or artifact.object_key is None:
            raise JobStateError("Bulk load requires the immutable bronze object key and store")
        with tempfile.TemporaryDirectory(prefix=f"xbrl-{artifact.id}-") as staging:
            local_zip = Path(staging) / "source.zip"
            await asyncio.to_thread(self.object_store.get_file, artifact.object_key, local_zip)
            stats = await self.loader.load_artifact(artifact, local_zip, job_id=job_id)
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
        artifact = await self.control.get_source_artifact(artifact_uuid)
        counts = expected_rows or artifact.details.get("load_stats", {}).get("rows_by_table")
        if not counts:
            raise JobStateError("Reconciliation requires the recorded source row counts")
        issue_count, details = await self.loader._reconcile(artifact_uuid, counts)
        if artifact.state == ArtifactState.RECONCILING:
            await self.control.mark_artifact_state(artifact_uuid, ArtifactState.COMPLETE)
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
