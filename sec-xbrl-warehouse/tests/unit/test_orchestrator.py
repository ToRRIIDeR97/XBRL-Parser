import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sec_xbrl_warehouse.domain.enums import ArtifactState, DatasetKind, JobType
from sec_xbrl_warehouse.domain.models import (
    DatasetArtifact,
    LoadStats,
    SourceArtifactRef,
    StoredObject,
)
from sec_xbrl_warehouse.exceptions import JobStateError
from sec_xbrl_warehouse.ingestion.orchestrator import IngestionOrchestrator


def _artifact(period: str = "2025Q1") -> DatasetArtifact:
    return DatasetArtifact(
        dataset=DatasetKind.NOTES,
        period=period,
        source_url=f"https://www.sec.gov/files/{period}.zip",
        filename=f"{period}.zip",
        published_label=period,
    )


class _Control:
    def __init__(self, source: SourceArtifactRef) -> None:
        self.source = source
        self.enqueued: list[dict[str, object]] = []
        self.checkpoints: dict[tuple[UUID, str], dict[str, object]] = {}
        self.states: list[ArtifactState] = []

    async def enqueue_job(self, **kwargs: object) -> UUID:
        self.enqueued.append(kwargs)
        return uuid4()

    async def get_checkpoint(self, job_id: UUID, stage: str) -> dict[str, object] | None:
        return self.checkpoints.get((job_id, stage))

    async def save_checkpoint(
        self, job_id: UUID, stage: str, checkpoint: dict[str, object]
    ) -> None:
        self.checkpoints[(job_id, stage)] = checkpoint

    async def upsert_source_artifact(self, *args: object, **kwargs: object) -> UUID:
        return self.source.id

    async def get_source_artifact(self, artifact_id: UUID) -> SourceArtifactRef:
        assert artifact_id == self.source.id
        return self.source

    async def mark_artifact_state(self, artifact_id: UUID, state: ArtifactState) -> None:
        assert artifact_id == self.source.id
        self.states.append(state)


class _Downloader:
    calls = 0

    async def download(self, artifact: DatasetArtifact) -> tuple[StoredObject, dict[str, str]]:
        self.calls += 1
        return (
            StoredObject(
                bucket="test",
                key="bronze/source.zip",
                size_bytes=3,
                checksum_sha256="a" * 64,
            ),
            {"etag": '"v1"', "last-modified": "today"},
        )


class _ObjectStore:
    def get_file(self, key: str, destination: Path) -> StoredObject:
        assert key == "bronze/source.zip"
        destination.write_bytes(b"zip")
        return StoredObject(
            bucket="test",
            key=key,
            size_bytes=3,
            checksum_sha256="a" * 64,
            local_path=destination,
        )


class _Loader:
    async def load_artifact(
        self, artifact: SourceArtifactRef, source_zip: Path, *, job_id: UUID | None = None
    ) -> LoadStats:
        assert await asyncio.to_thread(source_zip.read_bytes) == b"zip"
        return LoadStats(
            artifact_id=artifact.id,
            source_period=artifact.period,
            rows_by_table={"sub": 1},
            parquet_objects=("silver/sub.parquet",),
            elapsed_seconds=0.1,
            validation_issue_count=2,
        )

    async def _reconcile(
        self, artifact_id: UUID, expected_rows: dict[str, int]
    ) -> tuple[int, dict[str, object]]:
        return 0, {"expected": expected_rows, "artifact_id": str(artifact_id)}


async def test_orchestrator_runs_restartable_bulk_flow(tmp_path: Path) -> None:
    source = SourceArtifactRef(
        id=uuid4(),
        dataset=DatasetKind.NOTES,
        period="2025Q1",
        source_url="https://www.sec.gov/files/2025Q1.zip",
        object_key="bronze/source.zip",
        checksum_sha256="a" * 64,
        state=ArtifactState.RECONCILING,
        details={"load_stats": {"rows_by_table": {"sub": 1}}},
    )
    control = _Control(source)
    downloader = _Downloader()
    orchestrator = IngestionOrchestrator(
        control,  # type: ignore[arg-type]
        downloader=downloader,
        loader=_Loader(),  # type: ignore[arg-type]
        object_store=_ObjectStore(),
    )

    plan = orchestrator.plan_backfill(
        dataset=DatasetKind.NOTES,
        artifacts=(_artifact("2026Q1"), _artifact(), _artifact("2024Q4")),
        from_year=2025,
        to_year=2026,
        catalog_url="https://www.sec.gov/catalog",
    )
    assert [item.period for item in plan.artifacts] == ["2025Q1", "2026Q1"]
    assert (
        orchestrator.build_backfill_plan(
            dataset=plan.dataset,
            artifacts=plan.artifacts,
            from_year=plan.from_year,
            to_year=plan.to_year,
            catalog_url=plan.catalog_url,
        ).plan_checksum_sha256
        == plan.plan_checksum_sha256
    )

    plan_path = tmp_path / "plans" / "plan.json"
    orchestrator.write_plan(plan, plan_path)
    assert plan_path.is_file()
    await orchestrator.enqueue_backfill(plan, refresh_unversioned=True)
    assert len(control.enqueued) == 2
    assert all(item["job_type"] == JobType.DOWNLOAD_BULK.value for item in control.enqueued)

    job_id = uuid4()
    first = await orchestrator.download_artifact_job(
        _artifact().model_dump(mode="json"), job_id=job_id
    )
    second = await orchestrator.download_artifact_job(
        _artifact().model_dump(mode="json"), job_id=job_id
    )
    assert first["artifact_id"] == second["artifact_id"] == str(source.id)
    assert downloader.calls == 1

    loaded = await orchestrator.run_artifact_job(
        {"artifact": source.model_dump(mode="json")}, job_id=job_id
    )
    assert loaded["rows_by_table"] == {"sub": 1}
    reconciled = await orchestrator.reconcile_artifact(str(source.id))
    assert reconciled["issue_count"] == 0
    assert control.states == [ArtifactState.COMPLETE]


async def test_orchestrator_rejects_unwired_or_invalid_jobs() -> None:
    source = SourceArtifactRef(
        id=uuid4(),
        dataset=DatasetKind.NOTES,
        period="2025Q1",
        source_url="https://www.sec.gov/source.zip",
        created_at=datetime.now(UTC),
    )
    control = _Control(source)
    empty = IngestionOrchestrator(control)  # type: ignore[arg-type]
    with pytest.raises(JobStateError, match="Downloader"):
        await empty.download_artifact_job({})
    with pytest.raises(JobStateError, match="Loader"):
        await empty.run_artifact_job({})
    with pytest.raises(JobStateError, match="Loader"):
        await empty.reconcile_artifact(str(source.id))

    wired = IngestionOrchestrator(control, loader=_Loader())  # type: ignore[arg-type]
    with pytest.raises(JobStateError, match="missing 'artifact'"):
        await wired.run_artifact_job({})
    with pytest.raises(JobStateError, match="must be an object"):
        await wired.run_artifact_job({"artifact": "bad"})
    with pytest.raises(JobStateError, match="Invalid artifact ID"):
        await wired.reconcile_artifact("bad")
