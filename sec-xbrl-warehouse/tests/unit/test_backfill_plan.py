from sec_xbrl_warehouse.domain.enums import DatasetKind
from sec_xbrl_warehouse.domain.models import DatasetArtifact
from sec_xbrl_warehouse.ingestion.orchestrator import backfill_plan_checksum


def _artifact(period: str, url: str) -> DatasetArtifact:
    return DatasetArtifact(
        dataset=DatasetKind.NOTES,
        period=period,
        source_url=url,
        filename=url.rsplit("/", 1)[-1],
        published_label=period,
    )


def test_backfill_plan_checksum_is_stable_for_same_semantic_payload() -> None:
    artifacts = (
        _artifact("2025-01", "https://www.sec.gov/files/2025_01_notes.zip"),
        _artifact("2025-02", "https://www.sec.gov/files/2025_02_notes.zip"),
    )
    kwargs = {
        "dataset": DatasetKind.NOTES,
        "artifacts": artifacts,
        "from_year": 2025,
        "to_year": 2025,
        "catalog_url": "https://www.sec.gov/catalog",
    }
    assert backfill_plan_checksum(**kwargs) == backfill_plan_checksum(**kwargs)


def test_backfill_plan_checksum_changes_with_artifact_revision_metadata() -> None:
    base = _artifact("2025-01", "https://www.sec.gov/files/2025_01_notes.zip")
    revised = base.model_copy(update={"checksum_sha256": "a" * 64})
    shared = {
        "dataset": DatasetKind.NOTES,
        "from_year": 2025,
        "to_year": 2025,
        "catalog_url": "https://www.sec.gov/catalog",
    }
    assert backfill_plan_checksum(artifacts=(base,), **shared) != backfill_plan_checksum(
        artifacts=(revised,), **shared
    )
