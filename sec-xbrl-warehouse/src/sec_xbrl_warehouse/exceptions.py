from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sec_xbrl_warehouse.domain.models import ValidationIssue


class XbrlWarehouseError(Exception):
    """Base class for application-specific errors."""


class ConfigurationError(XbrlWarehouseError):
    """Configuration is missing or internally inconsistent."""


class SourceAccessError(XbrlWarehouseError):
    """A source (regulator) request failed or violated a source-access invariant."""


class SecAccessError(SourceAccessError):
    """SEC request failed or violated a source-access invariant."""


class ArtifactIntegrityError(XbrlWarehouseError):
    """A downloaded or stored artifact failed checksum/archive validation."""


class SchemaDriftError(XbrlWarehouseError):
    """A source table no longer matches the approved schema contract."""


class XbrlExtractionError(XbrlWarehouseError):
    """Arelle could not load, validate, or extract an XBRL document set."""

    validation_issues: tuple[ValidationIssue, ...] = ()


class DataQualityError(XbrlWarehouseError):
    """Post-load reconciliation or quality checks failed."""


class JobStateError(XbrlWarehouseError):
    """An invalid ingestion job state transition was attempted."""


class StateTransitionError(JobStateError):
    """An artifact or job attempted an invalid lifecycle transition."""


class WarehouseWriteError(XbrlWarehouseError):
    """A typed batch could not be written to the analytical warehouse."""


class SourceRetryDeferred(SourceAccessError):
    """Release the worker while retaining the server's earliest retry time."""

    def __init__(self, retry_at: datetime, source: str = "source") -> None:
        self.retry_at = retry_at
        super().__init__(f"{source} retry deferred until {retry_at.isoformat()}")


class SecRetryDeferred(SecAccessError, SourceRetryDeferred):
    """Release the worker while retaining the SEC's earliest retry time."""

    def __init__(self, retry_at: datetime) -> None:
        SourceRetryDeferred.__init__(self, retry_at, "SEC")


class DiskReserveError(XbrlWarehouseError):
    """Retained data needs more free space before ingestion may continue."""


class UnsupportedSourceError(XbrlWarehouseError):
    """A filing's source has no adapter in this component (never retried)."""
