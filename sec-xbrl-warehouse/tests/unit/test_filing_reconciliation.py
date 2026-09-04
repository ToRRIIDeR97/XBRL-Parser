from sec_xbrl_warehouse.validation.filing_reconciliation import reconcile_filing_rows


def test_bulk_and_arelle_projection_match_without_overwriting_sources() -> None:
    numeric = [
        {
            "tag": "Revenues",
            "version": "us-gaap/2025",
            "ddate": "2025-12-31",
            "qtrs": 4,
            "uom": "USD",
            "segments": "",
            "segt": 0,
            "value_raw": "123.0000",
        }
    ]
    raw = [
        {
            "concept_namespace": "http://fasb.org/us-gaap/2025",
            "concept_name": "Revenues",
            "taxonomy_version": "2025",
            "period_kind": "duration",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "period_instant": None,
            "dimensions_json": "[]",
            "value_kind": "numeric",
            "unit_id": "USD",
            "unit_numerator_json": '["iso4217:USD"]',
            "unit_denominator_json": "[]",
            "value_decimal": "123",
            "raw_value": "123",
            "is_nil": 0,
        }
    ]
    result = reconcile_filing_rows("0000000001-26-000001", numeric, [], raw)
    assert result["missing_count"] == 0
    assert result["extra_count"] == 0
    assert result["differing_count"] == 0
    assert numeric[0]["value_raw"] == "123.0000"
    assert raw[0]["raw_value"] == "123"


def test_unprojectable_typed_dimensions_are_reported_as_exclusions() -> None:
    result = reconcile_filing_rows(
        "a",
        [],
        [],
        [
            {
                "concept_namespace": "http://example.com",
                "concept_name": "TypedFact",
                "period_kind": "instant",
                "period_instant": "2025-12-31",
                "dimensions_json": '[{"typed_value_xml":"<value>1</value>"}]',
            }
        ],
    )
    assert result["excluded"] == {"arelle_typed_dimensions": 1}
