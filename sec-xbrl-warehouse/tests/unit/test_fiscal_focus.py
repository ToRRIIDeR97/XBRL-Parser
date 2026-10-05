from datetime import date
from pathlib import Path

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import read_bundle, write_json
from sec_xbrl_warehouse.normalization.fiscal_focus import derive_fiscal_focus

DEI = "http://xbrl.sec.gov/dei/2024"


def _dei(name: str, text: str | None, **updates: object) -> XbrlFact:
    fact = XbrlFact(
        fact_id=f"{name}-{text}-{len(updates)}",
        concept_namespace=DEI,
        concept_name=name,
        concept_qname=f"dei:{name}",
        context_id="c1",
        context_hash="0" * 64,
        value_kind=FactValueKind.TEXT,
        raw_value=text,
        value_text=text,
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="0" * 64,
        duplicate_set_hash=f"dup-{name}",
        source_document="example.htm",
    )
    return fact.model_copy(update=updates)


def _with(*facts: XbrlFact) -> XbrlExtractionBundle:
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession="0000000001-26-000001",
            cik=1,
            company_name="Example Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="example.htm",
            document_type="inline",
            taxonomy_namespaces=(DEI,),
            source_documents=("example.htm",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(),
        concepts=(),
        facts=facts,
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


def test_duplicate_equal_values_and_case_variants_do_not_conflict() -> None:
    filing, issues = derive_fiscal_focus(
        _with(
            _dei("DocumentFiscalYearFocus", "2025"),
            _dei("DocumentFiscalYearFocus", "2025Z", source_document="part2.htm"),
            _dei("DocumentFiscalPeriodFocus", "fy"),
            _dei("DocumentFiscalPeriodFocus", "FY", source_document="part2.htm"),
        )
    )
    assert (filing.fiscal_year, filing.fiscal_period) == (2025, "FY")
    assert issues == ()


def test_nil_and_non_dei_namespace_facts_are_ignored() -> None:
    filing, issues = derive_fiscal_focus(
        _with(
            _dei("DocumentFiscalYearFocus", None, is_nil=True),
            _dei("DocumentFiscalPeriodFocus", "Q2", concept_namespace="http://example.com/2026"),
        )
    )
    assert (filing.fiscal_year, filing.fiscal_period) == (None, None)
    assert issues == ()


def test_invalid_year_is_null_with_warning_and_period_kept() -> None:
    filing, issues = derive_fiscal_focus(
        _with(
            _dei("DocumentFiscalYearFocus", "twenty"),
            _dei("DocumentFiscalPeriodFocus", "Q1"),
        )
    )
    assert (filing.fiscal_year, filing.fiscal_period) == (None, "Q1")
    assert [issue.code for issue in issues] == ["warehouse:fiscalFocusInvalid"]


def test_derivation_is_identical_after_checkpoint_round_trip(tmp_path: Path) -> None:
    bundle = _with(
        _dei("DocumentFiscalYearFocus", "2025"),
        _dei("DocumentFiscalPeriodFocus", "Q5"),
        _dei(
            "DocumentPeriodEndDate",
            "2025-12-31",
            value_kind=FactValueKind.DATE,
            value_text=None,
            value_date=date(2025, 12, 31),
        ),
    )
    path = tmp_path / "extraction.json"
    write_json(bundle, path, 1_000_000)
    assert derive_fiscal_focus(read_bundle(path, 1_000_000)) == derive_fiscal_focus(bundle)
    filing, _ = derive_fiscal_focus(bundle)
    assert filing.report_date == date(2025, 12, 31)
    assert bundle.facts == read_bundle(path, 1_000_000).facts


def test_discovery_period_case_difference_is_not_a_mismatch() -> None:
    bundle = _with(
        _dei("DocumentFiscalYearFocus", "2025"),
        _dei("DocumentFiscalPeriodFocus", "Q1"),
    )
    bundle = bundle.model_copy(
        update={"filing": bundle.filing.model_copy(update={"fiscal_period": "q1"})}
    )
    filing, issues = derive_fiscal_focus(bundle)
    assert filing.fiscal_period == "Q1"
    assert issues == ()


def test_filing_upsert_keeps_stored_fiscal_fields_when_new_value_is_null() -> None:
    from sqlalchemy.dialects import postgresql

    from sec_xbrl_warehouse.infrastructure.storage.postgres import filing_upsert_statement

    values = {"accession": "0000000001-26-000001", "form": "10-K", "fiscal_year": None} | {
        "fiscal_period": None,
        "report_date": None,
    }
    sql = str(
        filing_upsert_statement(values, keep_derived=True).compile(dialect=postgresql.dialect())
    )
    for column in ("fiscal_year", "fiscal_period", "report_date"):
        assert f"{column} = coalesce(excluded.{column}, filings.{column})" in sql
    assert "coalesce(excluded.form" not in sql
    # The post-extraction upsert writes derived values as they are, NULL included.
    plain = str(filing_upsert_statement(values).compile(dialect=postgresql.dialect()))
    assert "coalesce" not in plain


def test_ingest_preserves_derived_fields_only_on_the_discovery_upsert() -> None:
    import asyncio

    from sec_xbrl_warehouse.ingestion.incremental import _upsert_discovered_filing

    calls: list[dict[str, object]] = []

    class _Control:
        async def upsert_filing(self, filing, *, keep_derived=False):
            calls.append({"keep_derived": keep_derived})

    class _PlainControl:
        async def upsert_filing(self, filing):
            calls.append({"plain": True})

    filing = _with().filing
    asyncio.run(_upsert_discovered_filing(_Control(), filing))  # type: ignore[arg-type]
    asyncio.run(_upsert_discovered_filing(_PlainControl(), filing))  # type: ignore[arg-type]
    assert calls == [{"keep_derived": True}, {"plain": True}]
