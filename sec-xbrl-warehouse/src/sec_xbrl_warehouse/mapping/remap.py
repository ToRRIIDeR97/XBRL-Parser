"""Re-apply approved mappings to filings that are already published."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import JobType
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import XbrlExtractionError
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle
from sec_xbrl_warehouse.normalization.fiscal_focus import derive_fiscal_focus


class RemapControl(Protocol):
    async def published_filing_revisions(
        self, accessions: Sequence[str] | None = None
    ) -> list[tuple[str, int]]: ...

    async def enqueue_job_if_absent(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 100,
        max_attempts: int = 5,
    ) -> UUID | None: ...


async def enqueue_canonical_remap(
    control: RemapControl, *, epoch: str, accessions: Sequence[str] | None = None
) -> int:
    """Enqueue one `map_canonical` job per published filing; return how many are new.

    The payload is `{accession, extraction_revision}`; the worker loads the facts
    from that revision's saved extraction. The idempotency key includes `epoch`
    (a label for the set of approved mappings, such as a date), so re-running
    with the same epoch enqueues nothing new and a new epoch maps every filing
    again. Filings without a published extraction are skipped.
    """
    if not epoch.strip() or ":" in epoch:
        raise ValueError("Remap epoch must be a non-empty label without ':'")
    created = 0
    for accession, revision in await control.published_filing_revisions(accessions):
        job_id = await control.enqueue_job_if_absent(
            job_type=JobType.MAP_CANONICAL.value,
            idempotency_key=f"map_canonical:{accession}:{revision}:{epoch}",
            payload={"accession": accession, "extraction_revision": revision},
        )
        created += job_id is not None
    return created


async def load_published_extraction(
    control: Any, object_store: ObjectStore, settings: Settings, accession: str, revision: int
) -> XbrlExtractionBundle:
    """Load the saved (silver) extraction of a completed filing ingestion, checksum-verified."""
    pointer = await control.extraction_checkpoint(accession, revision)
    if pointer is None:
        raise XbrlExtractionError(
            f"No saved extraction for {accession} revision {revision}; re-ingest the filing"
        )
    settings.ensure_directories()
    with tempfile.TemporaryDirectory(prefix="remap-", dir=settings.temp_dir) as directory:
        path = Path(directory) / "extraction.json"
        stored = await asyncio.to_thread(
            object_store.get_file,
            str(pointer["object_key"]),
            path,
            max_bytes=settings.extraction_max_bytes,
        )
        if stored.checksum_sha256 != pointer["sha256"]:
            raise XbrlExtractionError("Saved extraction checksum mismatch")
        bundle = await asyncio.to_thread(read_bundle, path, settings.extraction_max_bytes)
    if bundle.filing.accession != accession or bundle.extraction_revision != revision:
        raise XbrlExtractionError("Saved extraction belongs to a different filing or revision")
    # The saved extraction keeps discovery metadata; derive the dei fiscal fields
    # as ingestion does, so canonical facts carry them.
    enriched, _ = derive_fiscal_focus(bundle)
    return bundle.model_copy(update={"filing": enriched})
