from __future__ import annotations


class XbrlWarehouseError(Exception):
    """Base class for application-specific errors."""


class ConfigurationError(XbrlWarehouseError):
    """Configuration is missing or internally inconsistent."""


class SecAccessError(XbrlWarehouseError):
    """SEC request failed or violated a source-access invariant."""


class ArtifactIntegrityError(XbrlWarehouseError):
    """A downloaded or stored artifact failed checksum/archive validation."""


class SchemaDriftError(XbrlWarehouseError):
    """A source table no longer matches the approved schema contract."""


class XbrlExtractionError(XbrlWarehouseError):
    """Arelle could not load, validate, or extract an XBRL document set."""


class DataQualityError(XbrlWarehouseError):
    """Post-load reconciliation or quality checks failed."""


class JobStateError(XbrlWarehouseError):
    """An invalid ingestion job state transition was attempted."""


class StateTransitionError(JobStateError):
    """An artifact or job attempted an invalid lifecycle transition."""


class WarehouseWriteError(XbrlWarehouseError):
    """A typed batch could not be written to the analytical warehouse."""
