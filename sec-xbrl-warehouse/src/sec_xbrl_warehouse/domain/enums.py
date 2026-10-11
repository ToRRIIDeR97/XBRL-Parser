from __future__ import annotations

from enum import StrEnum


class DatasetKind(StrEnum):
    NOTES = "notes"
    FACE = "face"


class SecTable(StrEnum):
    SUB = "sub"
    TAG = "tag"
    DIM = "dim"
    NUM = "num"
    TXT = "txt"
    REN = "ren"
    PRE = "pre"
    CAL = "cal"


SEC_TABLE_LOAD_ORDER: tuple[SecTable, ...] = (
    SecTable.SUB,
    SecTable.TAG,
    SecTable.DIM,
    SecTable.REN,
    SecTable.NUM,
    SecTable.TXT,
    SecTable.PRE,
    SecTable.CAL,
)


class ArtifactState(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    ACQUIRED = "acquired"
    VALIDATING = "validating"
    LOADING = "loading"
    RECONCILING = "reconciling"
    COMPLETE = "complete"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    COMPLETE = "complete"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    DOWNLOAD_BULK = "download_bulk"
    LOAD_BULK = "load_bulk"
    DISCOVER_FILINGS = "discover_filings"
    INGEST_FILING = "ingest_filing"  # legacy: fetch and parse in one job
    FETCH_FILING = "fetch_filing"  # SEC download into the package archive
    PARSE_FILING = "parse_filing"  # Arelle, validation, and loading (no SEC download)
    RECONCILE_ARTIFACT = "reconcile_artifact"
    RECONCILE_FILING = "reconcile_filing"
    MAP_CANONICAL = "map_canonical"
    REBUILD_SERIES = "rebuild_series"  # one company's metric_series, coalesced per CIK


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class PeriodKind(StrEnum):
    INSTANT = "instant"
    DURATION = "duration"
    FOREVER = "forever"


class FactValueKind(StrEnum):
    NUMERIC = "numeric"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    NIL = "nil"


class RelationshipType(StrEnum):
    PRESENTATION = "presentation"
    CALCULATION = "calculation"
    DEFINITION = "definition"
    LABEL = "label"
    REFERENCE = "reference"
    FOOTNOTE = "footnote"
    OTHER = "other"


class StatementType(StrEnum):
    COVER = "cover"
    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    CASH_FLOW = "cash_flow"
    EQUITY = "equity"
    COMPREHENSIVE_INCOME = "comprehensive_income"
    NOTES = "notes"
    OTHER = "other"
