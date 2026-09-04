import pytest
from pydantic import BaseModel

from sec_xbrl_warehouse.domain.sec_fsnds import (
    CalRecord,
    DimRecord,
    NumRecord,
    PreRecord,
    RenRecord,
    SubRecord,
    TagRecord,
    TxtRecord,
)


@pytest.mark.parametrize(
    ("model", "identity"),
    [
        (SubRecord, {"adsh", "cik", "form", "filed"}),
        (TagRecord, {"tag", "version"}),
        (DimRecord, {"dimh", "segments"}),
        (NumRecord, {"adsh", "tag", "version", "ddate", "dimh"}),
        (TxtRecord, {"adsh", "tag", "version", "ddate", "context"}),
        (RenRecord, {"adsh", "report", "roleuri"}),
        (PreRecord, {"adsh", "report", "line", "tag"}),
        (CalRecord, {"adsh", "grp", "arc", "ptag", "ctag"}),
    ],
)
def test_sec_record_contracts_keep_source_identity(
    model: type[BaseModel], identity: set[str]
) -> None:
    assert identity <= set(model.model_fields)
    assert model.model_config["extra"] == "allow"
    assert model.model_config["frozen"] is True
