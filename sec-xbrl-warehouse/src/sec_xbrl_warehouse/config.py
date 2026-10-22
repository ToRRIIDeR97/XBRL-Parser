from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from sec_xbrl_warehouse.domain.sources import is_source_code
from sec_xbrl_warehouse.exceptions import ConfigurationError


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or `.env`.

    Environment variables use the `XBRL_` prefix except for the service image
    bootstrap variables consumed directly by Docker Compose.
    """

    model_config = SettingsConfigDict(
        env_prefix="XBRL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    disk_reserve_bytes: int = Field(default=5 * 1024**3, ge=0)
    data_dir: Path = Path("data")
    temp_dir: Path = Path("data/tmp")
    config_dir: Path = Path("config")

    # Regulators this deployment fetches from (`XBRL_ENABLED_SOURCES=sec,esef`). Every
    # process sharing a control database should use the same list: a job for a source
    # that isn't enabled fails permanently.
    enabled_sources: Annotated[tuple[str, ...], NoDecode] = ("sec",)
    # filings.xbrl.org (ESEF) discovery: records per page, pages per discovery run (per
    # country when `esef_countries` is set), and ISO country codes (empty: all).
    esef_page_size: int = Field(default=100, ge=1, le=500)
    esef_max_pages: int = Field(default=50, ge=1, le=10_000)
    esef_countries: Annotated[tuple[str, ...], NoDecode] = ()
    # EDINET (Japan, FSA) and OpenDART (Korea, FSS) API keys. Each key is the
    # operator's own and goes only into that regulator's requests: never into filing
    # references, manifests, object keys, job payloads, or logs.
    edinet_api_key: SecretStr | None = None
    dart_api_key: SecretStr | None = None
    # EDINET discovery reads one document list per day: at most this many days per
    # run, keeping these document type codes when a run names no forms.
    edinet_max_days: int = Field(default=31, ge=1, le=366)
    edinet_doc_types: Annotated[tuple[str, ...], NoDecode] = (
        "120",
        "130",
        "140",
        "150",
        "160",
        "170",
    )
    # OpenDART disclosure-search page size (the API allows 1 to 100).
    dart_page_count: int = Field(default=100, ge=1, le=100)
    # Operator file inbox: `<inbox_dir>/<code>/index.csv` plus the files it lists, for
    # sources without a usable API (`nse`, `mca`, `acra`). Read only; nothing is fetched.
    inbox_dir: Path = Path("data/inbox")

    sec_user_agent: str = "Your Name or Organization monitored-email@example.com"
    sec_max_requests_per_second: float = Field(default=8.0, gt=0, le=10)
    sec_max_concurrency: int = Field(default=4, gt=0, le=20)
    sec_connect_timeout_seconds: float = Field(default=20.0, gt=0)
    sec_read_timeout_seconds: float = Field(default=120.0, gt=0)
    sec_max_retries: int = Field(default=6, ge=0, le=20)
    download_max_bytes: int = Field(default=32 * 1024**3, gt=0)
    response_max_bytes: int = Field(default=64 * 1024**2, gt=0)
    zip_max_members: int = Field(default=10_000, gt=0)
    zip_max_directory_bytes: int = Field(default=64 * 1024**2, ge=1024 * 1024)
    zip_max_member_bytes: int = Field(default=64 * 1024**3, gt=0)
    zip_max_total_bytes: int = Field(default=128 * 1024**3, gt=0)
    zip_max_compression_ratio: float = Field(default=200, gt=0)
    filing_max_members: int = Field(default=1_000, gt=0)
    filing_max_bytes: int = Field(default=512 * 1024**2, gt=0)
    retain_filing_source_files: bool = True
    reconciliation_max_rows: int = Field(default=50_000, gt=0)
    reconciliation_max_bytes: int = Field(default=16 * 1024**2, gt=0)

    # Secrets are SecretStr so settings reprs, dumps, and tracebacks never print them.
    postgres_dsn: SecretStr = SecretStr(
        "postgresql+asyncpg://xbrl:xbrl_dev_password@localhost:5432/xbrl_control"
    )

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "xbrl"
    clickhouse_username: str = "xbrl"
    clickhouse_password: SecretStr = SecretStr("xbrl_dev_password")
    clickhouse_secure: bool = False

    object_store_backend: Literal["s3", "local"] = "s3"
    object_store_endpoint: str = "http://localhost:9002"
    object_store_access_key: str = "xbrl_minio"
    object_store_secret_key: SecretStr = SecretStr("xbrl_minio_dev_password")
    object_store_bucket: str = "sec-xbrl"
    object_store_region: str = "us-east-1"
    object_store_secure: bool = False

    arrow_batch_rows: int = Field(default=50_000, gt=0)
    arrow_batch_bytes: int = Field(default=32 * 1024**2, gt=0)
    parquet_compression: Literal["zstd", "snappy", "gzip", "none"] = "zstd"
    parquet_compression_level: int | None = Field(default=6, ge=1, le=22)

    arelle_validate_xbrl: bool = True
    arelle_validate_efm: bool = False
    arelle_plugin_paths: str = ""
    arelle_cache_dir: Path = Path("data/cache/arelle")
    arelle_taxonomy_packages_dir: Path = Path("data/taxonomy-packages")
    arelle_offline: bool = False
    arelle_allowed_taxonomy_hosts: str = (
        "www.sec.gov,xbrl.sec.gov,xbrl.fasb.org,xbrl.ifrs.org,www.xbrl.org,"
        "www.esma.europa.eu,www.ifrs.org,disclosure.edinet-fsa.go.jp,"
        "disclosure2dl.edinet-fsa.go.jp"
    )
    arelle_timeout_seconds: float = Field(default=600, gt=0)
    arelle_memory_limit_bytes: int = Field(default=2 * 1024**3, gt=0)
    # Successful extractions per warm Arelle child before it is replaced (1 = a new
    # process per filing, the earlier behaviour).
    arelle_child_max_tasks: int = Field(default=20, ge=1, le=1000)
    extraction_max_bytes: int = Field(default=512 * 1024**2, gt=0)

    worker_id: str = "local-worker"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # Jobs one worker process runs at once (each with its own lease and heartbeat).
    # worker_concurrency x arelle_memory_limit_bytes must fit the container limit.
    worker_concurrency: int = Field(default=1, ge=1, le=32)
    # A job that finds the SEC egress lock held elsewhere is retried after this delay.
    sec_egress_busy_retry_seconds: float = Field(default=60.0, gt=0)
    # A job deferred this long (from its first deferral) on a busy lock goes dead.
    sec_egress_max_defer_seconds: float = Field(default=6 * 3600.0, gt=0)
    # How often a held SEC egress lock's connection is checked before SEC requests.
    sec_egress_check_seconds: float = Field(default=30.0, gt=0)
    # A company's series rebuild runs this long after the first filing that asks
    # for it; later filings within the window share the same rebuild.
    series_rebuild_delay_seconds: float = Field(default=60.0, ge=0)
    # Daily-index discovery refetches a stored company older than this.
    company_refresh_days: int = Field(default=30, ge=1)
    worker_lock_timeout_seconds: int = Field(default=900, gt=30)
    worker_max_attempts: int = Field(default=5, ge=1)
    worker_metrics_host: str = "127.0.0.1"
    worker_metrics_port: int = Field(default=9091, ge=1, le=65535)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_key: SecretStr | None = Field(default=None, min_length=32)
    # `api serve` refuses a non-loopback bind without an API key unless this is set
    # (Compose sets it: the container binds 0.0.0.0 behind a 127.0.0.1-only port).
    api_allow_unauthenticated_remote: bool = False
    api_max_concurrency: int = Field(default=4, gt=0, le=64)
    api_clickhouse_username: str | None = None
    api_clickhouse_password: SecretStr | None = None
    api_query_timeout_seconds: int = Field(default=30, gt=0)
    api_query_max_memory_bytes: int = Field(default=512 * 1024**2, gt=0)
    api_query_max_rows: int = Field(default=5_000_000, gt=0)
    api_query_max_bytes: int = Field(default=1024**3, gt=0)
    api_result_max_bytes: int = Field(default=16 * 1024**2, gt=0)

    @field_validator("api_key", mode="before")
    @classmethod
    def empty_api_key(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("enabled_sources", mode="before")
    @classmethod
    def split_enabled_sources(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("enabled_sources")
    @classmethod
    def validate_enabled_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("enabled_sources needs at least one source")
        invalid = [code for code in value if not is_source_code(code)]
        if invalid:
            raise ValueError(f"enabled_sources has invalid source codes: {invalid}")
        if len(set(value)) != len(value):
            raise ValueError("enabled_sources lists a source more than once")
        return value

    @field_validator("esef_countries", mode="before")
    @classmethod
    def split_esef_countries(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part for part in value.split(",") if part.strip())
        return value

    @field_validator("esef_countries")
    @classmethod
    def validate_esef_countries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        codes = tuple(dict.fromkeys(code.strip().upper() for code in value))
        invalid = [code for code in codes if re.fullmatch(r"[A-Z]{2}", code) is None]
        if invalid:
            raise ValueError(f"esef_countries must be two-letter country codes: {invalid}")
        return codes

    @field_validator("edinet_api_key", "dart_api_key", mode="before")
    @classmethod
    def empty_source_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("edinet_doc_types", mode="before")
    @classmethod
    def split_edinet_doc_types(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("edinet_doc_types")
    @classmethod
    def validate_edinet_doc_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        codes = tuple(dict.fromkeys(value))
        invalid = [code for code in codes if re.fullmatch(r"[0-9]{3}", code) is None]
        if invalid or not codes:
            raise ValueError(f"edinet_doc_types must be three-digit codes: {invalid}")
        return codes

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.upper()
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("clickhouse_database")
    @classmethod
    def validate_clickhouse_database(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise ValueError("clickhouse_database must be a plain SQL identifier")
        return value

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> Settings:
        placeholder_tokens = ("monitored-email@example.com", "Your Name or Organization")
        if self.env != "test" and any(token in self.sec_user_agent for token in placeholder_tokens):
            # Keep settings instantiable for `--help`; doctor and network clients call
            # `assert_sec_identity()` before any real SEC traffic.
            pass
        if self.parquet_compression == "none":
            self.parquet_compression_level = None
        return self

    def assert_production_ready(self) -> None:
        """Reject development credentials and plaintext transport in production.

        Called by the API at startup and by `doctor`; other environments pass.
        """
        if self.env != "production":
            return
        problems = []
        secrets = {
            "XBRL_POSTGRES_DSN": self.postgres_dsn.get_secret_value(),
            "XBRL_CLICKHOUSE_PASSWORD": self.clickhouse_password.get_secret_value(),
            "XBRL_OBJECT_STORE_SECRET_KEY": self.object_store_secret_key.get_secret_value(),
            "XBRL_API_CLICKHOUSE_PASSWORD": (
                self.api_clickhouse_password.get_secret_value()
                if self.api_clickhouse_password is not None
                else ""
            ),
        }
        problems += [
            f"{name} uses a development credential"
            for name, value in secrets.items()
            if _DEV_CREDENTIAL in value
        ]
        if not self.clickhouse_secure:
            problems.append("XBRL_CLICKHOUSE_SECURE must be true")
        if self.object_store_backend == "s3" and not (
            self.object_store_secure or self.object_store_endpoint.startswith("https://")
        ):
            problems.append("the object store endpoint must use TLS")
        if problems:
            raise ConfigurationError("Production settings rejected: " + "; ".join(problems))

    def assert_sec_identity(self) -> None:
        """Reject the template SEC identity before a network request is made."""
        placeholder_tokens = ("monitored-email@example.com", "Your Name or Organization")
        if any(token in self.sec_user_agent for token in placeholder_tokens):
            raise ConfigurationError(
                "Set XBRL_SEC_USER_AGENT to a real name/organization and monitored email."
            )
        if "@" not in self.sec_user_agent:
            raise ConfigurationError(
                "XBRL_SEC_USER_AGENT should include a monitored email address."
            )

    @property
    def arelle_plugins(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.arelle_plugin_paths.split(",") if part.strip())

    @property
    def allowed_taxonomy_hosts(self) -> tuple[str, ...]:
        return tuple(
            part.strip().lower()
            for part in self.arelle_allowed_taxonomy_hosts.split(",")
            if part.strip()
        )

    def require_disk_reserve(self, required_bytes: int = 0) -> None:
        import shutil

        from sec_xbrl_warehouse.exceptions import DiskReserveError
        from sec_xbrl_warehouse.observability.metrics import DISK_FREE_BYTES, DISK_RESERVE_BYTES

        DISK_RESERVE_BYTES.set(self.disk_reserve_bytes)
        for name, path in (("data", self.data_dir), ("scratch", self.temp_dir)):
            path.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(path).free
            DISK_FREE_BYTES.labels(volume=name).set(free)
            if free < self.disk_reserve_bytes + required_bytes:
                raise DiskReserveError(f"Disk reserve exhausted at {path.resolve()}")

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.arelle_cache_dir.mkdir(parents=True, exist_ok=True)
        self.arelle_taxonomy_packages_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)


# Every development default and `.env.example` credential contains this marker.
_DEV_CREDENTIAL = "dev_password"


def is_loopback_host(host: str) -> bool:
    """Whether a bind address only accepts local connections."""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
