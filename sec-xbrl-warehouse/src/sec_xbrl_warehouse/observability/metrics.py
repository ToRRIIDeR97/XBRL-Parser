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
JOB_TRANSITIONS = Counter(
    "xbrl_job_transitions_total",
    "Worker job outcomes",
    labelnames=("state", "job_type"),
)
