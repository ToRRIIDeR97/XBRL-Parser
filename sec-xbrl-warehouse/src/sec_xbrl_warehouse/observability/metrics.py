from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

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

_REQUEST_ATTR = "_xbrl_metrics_request_instrumented"


def instrument_sec_client() -> None:
    """Attach request/response hooks to `SecHttpClient`.

    Measurement uses host + status labels only, never URLs, so Prometheus
    cardinality stays bounded. Idempotent: repeated calls are no-ops.
    """
    from sec_xbrl_warehouse.infrastructure.http.rate_limiter import AsyncRateLimiter
    from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient

    if not getattr(SecHttpClient, _REQUEST_ATTR, False):
        setattr(SecHttpClient, _REQUEST_ATTR, True)
        SecHttpClient._request = _instrument_request(SecHttpClient._request)  # type: ignore[method-assign]

    if not getattr(AsyncRateLimiter, _REQUEST_ATTR, False):
        setattr(AsyncRateLimiter, _REQUEST_ATTR, True)
        AsyncRateLimiter.acquire = _instrument_acquire(AsyncRateLimiter.acquire)  # type: ignore[method-assign,assignment]


def _instrument_request(original: Callable[..., Any]) -> Callable[..., Any]:
    from urllib.parse import urlparse

    async def wrapped(self: Any, method: str, url: str, **kwargs: Any) -> Any:
        host = urlparse(url).hostname or "unknown"
        try:
            response = await original(self, method, url, **kwargs)
            SEC_REQUESTS.labels(host=str(host), status=str(response.status_code)).inc()
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit():
                SOURCE_BYTES.labels(source=str(host), dataset="sec").inc(int(content_length))
            return response
        except Exception as exc:
            SEC_REQUESTS.labels(host=str(host), status=_exception_status(exc)).inc()
            raise

    return wrapped


def _instrument_acquire(
    original: Callable[[Any], Awaitable[Any]],
) -> Callable[[Any], Awaitable[Any]]:
    async def wrapped(self: Any) -> Any:
        start = time.monotonic()
        try:
            return await original(self)
        finally:
            SEC_THROTTLE_SECONDS.observe(time.monotonic() - start)

    return wrapped


def _exception_status(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        return str(response.status_code)
    return "error"
