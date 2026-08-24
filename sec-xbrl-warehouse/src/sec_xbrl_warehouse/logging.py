from __future__ import annotations

import logging
import sys
from typing import Any

try:
    import structlog
except ModuleNotFoundError:  # Allows lightweight parser/unit-test use before full install.
    structlog = None  # type: ignore[assignment]


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Configure stdlib logging and structlog once per process."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )
    if structlog is None:
        return

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class _StdlibLoggerAdapter:
    """Accept structlog-style keyword fields when structlog is unavailable."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def debug(self, event: str, **fields: Any) -> None:
        self.logger.debug("%s %s", event, fields or "")

    def info(self, event: str, **fields: Any) -> None:
        self.logger.info("%s %s", event, fields or "")

    def warning(self, event: str, **fields: Any) -> None:
        self.logger.warning("%s %s", event, fields or "")

    def error(self, event: str, **fields: Any) -> None:
        self.logger.error("%s %s", event, fields or "")

    def exception(self, event: str, **fields: Any) -> None:
        self.logger.exception("%s %s", event, fields or "")


def get_logger(name: str | None = None) -> Any:
    if structlog is None:
        return _StdlibLoggerAdapter(logging.getLogger(name))
    return structlog.get_logger(name)
