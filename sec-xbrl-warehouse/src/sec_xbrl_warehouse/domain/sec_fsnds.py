from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SecRecord(BaseModel):
    """Typed representation of a row from the SEC flattened data sets.

    `extra="allow"` is intentional: the schema-drift layer records unexpected
    fields before typed coercion, while this model allows a quarantined row to
    retain those values for diagnostics.
    """

    model_config = ConfigDict(extra="allow", frozen=True)


class SubRecord(SecRecord):
    adsh: str
    cik: int
    name: str
    sic: int | None = None
    countryba: str
    stprba: str | None = None
    cityba: str
    zipba: str | None = None
    bas1: str | None = None
    bas2: str | None = None
    baph: str | None = None
    countryma: str | None = None
    stprma: str | None = None
    cityma: str | None = None
    zipma: str | None = None
    mas1: str | None = None
    mas2: str | None = None
    countryinc: str
    stprinc: str | None = None
    ein: str | None = None
    former: str | None = None
    changed: date | None = None
    afs: str | None = None
    wksi: bool
    fye: str
    form: str
    period: date
    fy: int
    fp: str
    filed: date
    accepted: datetime
    prevrpt: bool
    detail: bool
    instance: str
    nciks: int
    aciks: str | None = None
    pubfloatusd: Decimal | None = None
    floatdate: date | None = None
    floataxis: str | None = None
    floatmems: int | None = None


class TagRecord(SecRecord):
    tag: str
    version: str
    custom: bool
    abstract: bool
    datatype: str | None = None
    iord: str
    crdr: str | None = None
    tlabel: str | None = None
    doc: str | None = None


class DimRecord(SecRecord):
    dimh: str
    segments: str
    segt: bool


class NumRecord(SecRecord):
    adsh: str
    tag: str
    version: str
    ddate: date
    qtrs: int
    uom: str
    dimh: str
    iprx: int
    value: Decimal | None = None
    footnote: str | None = None
    footlen: int
    dimn: int
    coreg: str | None = None
    durp: float
    datp: float
    dcml: int
    segments: str | None = None


class TxtRecord(SecRecord):
    adsh: str
    tag: str
    version: str
    ddate: date
    qtrs: int
    iprx: int
    lang: str
    dcml: int
    durp: float
    datp: float
    dimh: str
    dimn: int | None = None
    coreg: str | None = None
    escaped: bool
    srclen: int
    txtlen: int | None = None
    footnote: str | None = None
    footlen: int | None = None
    context: str
    value: str | None = None


class RenRecord(SecRecord):
    adsh: str
    report: int
    rfile: str
    menucat: str | None = None
    shortname: str
    longname: str
    roleuri: str
    parentroleuri: str | None = None
    parentreport: int | None = None
    ultparentrpt: int | None = None


class PreRecord(SecRecord):
    adsh: str
    report: int
    line: int
    stmt: str
    inpth: bool
    tag: str
    version: str
    prole: str
    plabel: str
    negating: bool


class CalRecord(SecRecord):
    adsh: str
    grp: int
    arc: int
    negative: int = Field(ge=-1, le=1)
    ptag: str
    pversion: str
    ctag: str
    cversion: str
