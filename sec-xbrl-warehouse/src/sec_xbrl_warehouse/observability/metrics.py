from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

if TYPE_CHECKING:
    from sec_xbrl_warehouse.domain.models import QueueSnapshot

SEC_REQUESTS = Counter(
    "xbrl_sec_requests_total",
    "SEC HTTP requests",
    labelnames=("host", "status"),
)
SEC_THROTTLE_SECONDS = Histogram(
    "xbrl_sec_throttle_seconds",
    "Time spent waiting for the SEC rate limiter",
)
SOURCE_BYTES = Counter(
    "xbrl_source_bytes_downloaded_total",
    "Downloaded source bytes",
    labelnames=("source", "dataset"),
)
ROWS_PARSED = Counter(
    "xbrl_rows_parsed_total",
    "Rows parsed from source artifacts",
    labelnames=("source", "table"),
)
ROWS_WRITTEN = Counter(
    "xbrl_rows_written_total",
    "Rows written to a storage layer",
    labelnames=("storage", "table"),
)
STAGE_LATENCY = Histogram(
    "xbrl_ingestion_stage_seconds",
    "Ingestion stage latency",
    labelnames=("pipeline", "stage"),
)
JOBS = Gauge(
    "xbrl_jobs",
    "Current jobs by state and type",
    labelnames=("state", "job_type"),
)
VALIDATION_ISSUES = Counter(
    "xbrl_validation_issues_total",
    "Validation issues observed",
    labelnames=("source", "severity", "code"),
)
SCHEMA_DRIFT = Counter(
    "xbrl_schema_drift_total",
    "Source schema drift events",
    labelnames=("dataset", "table", "kind"),
)
RECONCILIATION_DIFFERENCES = Counter(
    "xbrl_reconciliation_differences_total",
    "Source/silver/gold or bulk/incremental differences",
    labelnames=("comparison", "kind"),
)
JOB_TRANSITIONS = Counter(
    "xbrl_job_transitions_total",
    "Worker job outcomes",
    labelnames=("state", "job_type"),
)

JOB_DEAD_LETTERS = Counter(
    "xbrl_job_dead_letters_total",
    "Jobs moved to dead, by reason",
    labelnames=("job_type", "reason"),
)

DISK_FREE_BYTES = Gauge("xbrl_disk_free_bytes", "Free bytes on application volumes", ["volume"])
DISK_RESERVE_BYTES = Gauge("xbrl_disk_reserve_bytes", "Required free bytes before ingestion")


class QueueCollector(Collector):
    """Scrape-time queue gauges from `ControlRepository.queue_snapshot()`.

    `snapshot` is called on each scrape (it may raise or time out); the
    collector then reports `xbrl_queue_snapshot_up 0` instead of stale values.
    """

    def __init__(self, snapshot: Callable[[], QueueSnapshot]) -> None:
        self.snapshot = snapshot

    def collect(self) -> Iterator[GaugeMetricFamily]:
        up = GaugeMetricFamily(
            "xbrl_queue_snapshot_up", "Whether the last queue snapshot succeeded"
        )
        try:
            snapshot = self.snapshot()
        except Exception:
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        jobs = GaugeMetricFamily(
            "xbrl_queue_jobs", "Jobs in the queue by type and state", labels=["job_type", "state"]
        )
        for (job_type, state), count in sorted(snapshot.counts.items()):
            jobs.add_metric([job_type, state], count)
        yield jobs
        yield GaugeMetricFamily(
            "xbrl_queue_oldest_ready_age_seconds",
            "Age of the oldest claimable pending/retry job",
            value=snapshot.oldest_ready_age_seconds,
        )


def snapshot_on_loop(
    snapshot: Callable[[], Awaitable[QueueSnapshot]],
    loop: asyncio.AbstractEventLoop,
    *,
    timeout: float,
) -> Callable[[], QueueSnapshot]:
    """Run `snapshot()` on `loop` from another thread, cancelling it on timeout or error."""

    def run() -> QueueSnapshot:
        async def call() -> QueueSnapshot:
            return await snapshot()

        future = asyncio.run_coroutine_threadsafe(call(), loop)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            future.cancel()  # don't leave a slow snapshot running on the worker loop
            raise

    return run
