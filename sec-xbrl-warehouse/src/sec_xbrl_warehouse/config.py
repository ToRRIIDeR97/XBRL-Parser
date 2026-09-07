from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    data_dir: Path = Path("data")
    temp_dir: Path = Path("data/tmp")
    config_dir: Path = Path("config")

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

    postgres_dsn: str = "postgresql+asyncpg://xbrl:xbrl_dev_password@localhost:5432/xbrl_control"

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "xbrl"
    clickhouse_username: str = "xbrl"
    clickhouse_password: str = "xbrl_dev_password"
    clickhouse_secure: bool = False

    object_store_backend: Literal["s3", "local"] = "s3"
    object_store_endpoint: str = "http://localhost:9002"
    object_store_access_key: str = "xbrl_minio"
    object_store_secret_key: str = "xbrl_minio_dev_password"
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
    arelle_allowed_taxonomy_hosts: str = "www.sec.gov,xbrl.fasb.org,xbrl.ifrs.org,www.xbrl.org"
    arelle_timeout_seconds: float = Field(default=600, gt=0)
    arelle_memory_limit_bytes: int = Field(default=2 * 1024**3, gt=0)
    extraction_max_bytes: int = Field(default=512 * 1024**2, gt=0)

    worker_id: str = "local-worker"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_lock_timeout_seconds: int = Field(default=900, gt=30)
    worker_max_attempts: int = Field(default=5, ge=1)
    worker_metrics_host: str = "127.0.0.1"
    worker_metrics_port: int = Field(default=9091, ge=1, le=65535)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_key: SecretStr | None = Field(default=None, min_length=32)
    api_max_concurrency: int = Field(default=4, gt=0, le=64)
    api_clickhouse_username: str | None = None
    api_clickhouse_password: SecretStr | None = None
    api_query_timeout_seconds: int = Field(default=30, gt=0)
    api_query_max_memory_bytes: int = Field(default=512 * 1024**2, gt=0)
    api_query_max_rows: int = Field(default=5_000_000, gt=0)
    api_query_max_bytes: int = Field(default=1024**3, gt=0)
    api_result_max_bytes: int = Field(default=16 * 1024**2, gt=0)

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

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.arelle_cache_dir.mkdir(parents=True, exist_ok=True)
        self.arelle_taxonomy_packages_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
