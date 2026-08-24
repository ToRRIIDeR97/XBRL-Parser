from sec_xbrl_warehouse.domain.xbrl import DimensionMember
from sec_xbrl_warehouse.normalization.dimensions import (
    canonicalize_dimensions,
    dimensions_hash,
    dimensions_to_json,
)


def test_dimension_order_does_not_change_hash() -> None:
    a = DimensionMember(
        axis_namespace="urn:test",
        axis_name="B",
        member_namespace="urn:test",
        member_name="Two",
    )
    b = DimensionMember(
        axis_namespace="urn:test",
        axis_name="A",
        member_namespace="urn:test",
        member_name="One",
    )
    assert dimensions_hash((a, b)) == dimensions_hash((b, a))
    assert canonicalize_dimensions((a, b))[0].axis_name == "A"
    assert '"axis_name":"A"' in dimensions_to_json((a, b))
