import pytest

from sec_xbrl_warehouse.infrastructure.xbrl import sec_transforms as t


@pytest.mark.parametrize(
    ("function", "displayed", "expected"),
    [
        (t.boolballotbox, "☒", "true"),
        (t.boolballotbox, " ☑ ", "true"),
        (t.boolballotbox, "☐", "false"),
        (t.yesnoballotbox, "\u2610", "No"),
        (t.yesnoballotbox, "\u2611", "Yes"),
        (t.yesnoballotbox, "\u2612", "Yes"),
        (t.countrynameen, "Canada", "CA"),
        (t.countrynameen, "Republic of Korea", "KR"),
        (t.countrynameen, "Côte d" + chr(0x2019) + "Ivoire", "CI"),
        (t.countrynameen, "Netherlands", "NL"),
        (t.countrynameen, "United States of America", "US"),
        (t.countrynameen, "U.S.A.", "US"),
        (t.stateprovnameen, "California", "CA"),
        (t.stateprovnameen, "Ontario", "ON"),
        (t.stateprovnameen, "United States Minor Outlying Islands", "UM"),
        (t.stateprovnameen, "Georgia", "GA"),
        (t.edgarprovcountryen, "Ontario, Canada", "A6"),
        (t.edgarprovcountryen, "Cayman Islands", "E9"),
        (t.edgarprovcountryen, "Norway", "Q8"),
        (t.edgarprovcountryen, "Netherlands Antilles", "P8"),
        (t.edgarprovcountryen, "Georgia", "2Q"),
        (t.edgarprovcountryen, "American Samoa", "B5"),
        (t.edgarprovcountryen, "Northern Mariana Islands", "1V"),
        (t.edgarprovcountryen, "United States Minor Outlying Islands", "2J"),
        (t.edgarprovcountryen, "United States", "X1"),
        (t.edgarprovcountryen, "Canada", "Z4"),
        (t.edgarprovcountryen, "Viet Nam", "Q1"),
        (t.exchnameen, "The Nasdaq Stock Market LLC", "NASDAQ"),
        (t.exchnameen, "Nasdaq Global Select Market", "NASDAQ"),
        (t.exchnameen, "Nasdaq PHLX LLC", "Phlx"),
        (t.exchnameen, "Nasdaq ISE", "ISE"),
        (t.exchnameen, "Nasdaq GEMX", "GEMX"),
        (t.exchnameen, "Nasdaq MRX", "MRX"),
        (t.exchnameen, "Miami International Securities Exchange", "MIAX"),
        (t.exchnameen, "MIAX PEARL", "PEARL"),
        (t.exchnameen, "New York Stock Exchange", "NYSE"),
        (t.exchnameen, "NYSE Arca, Inc.", "NYSEArca"),
        (t.exchnameen, "NYSE American LLC", "NYSEAMER"),
        (t.exchnameen, "Cboe BZX Exchange, Inc.", "CboeBZX"),
        (t.entityfilercategoryen, "large accelerated filer", "Large Accelerated Filer"),
        (t.entityfilercategoryen, "Non-Accelerated Filer", "Non-accelerated Filer"),
        (t.entityfilercategoryen, "Non accelerated filer", "Non-accelerated Filer"),
        (t.numwordsen, "twelve thousand", "12000"),
        (t.numwordsen, "one hundred and twenty-five", "125"),
        (t.numwordsen, "two million three hundred thousand", "2300000"),
        (t.numwordsen, "none", "0"),
        (t.numwordsen, "nil", "0"),
        (t.numwordsen, "two thousand twenty", "2020"),
        (t.numwordsen, "twelve hundred", "1200"),
        (t.numinf, "unlimited", "INF"),
        (t.numneginf, "x", "-INF"),
        (t.numnan, "n/a", "NaN"),
        (t.duryear, "22.3", "P22Y3M18D"),
        (t.duryear, "5", "P5Y"),
        (t.duryear, "-1.5", "-P1Y6M"),
        (t.durmonth, "22.3", "P22M9D"),
        (t.durweek, "22.3", "P156D"),
        (t.durday, "22.3", "P22DT7H"),
        (t.durhour, "22.3", "PT22H"),
        (t.duryear, "0", "P0Y"),
        (t.duryear, "0.5", "P0Y6M"),
        (t.durmonth, "0", "P0M"),
        (t.durday, "1.51", "P1DT12H"),
        (t.durday, "0", "P0D"),
        (t.durhour, "1.5", "PT1H"),
        (t.durhour, "0", "PT0H"),
        (t.durweek, "-0", "P0D"),
        (t.durwordsen, "two years, six months", "P2Y6M"),
        (t.durwordsen, "two years and three days", "P2Y3D"),
        (t.durwordsen, "1 year", "P1Y"),
        (t.datequarterend, "Q1 2025", "2025-03-31"),
        (t.datequarterend, "2025 Q3", "2025-09-30"),
        (t.datequarterend, "fourth quarter of 2024", "2024-12-31"),
        (t.datequarterend, "2nd Qtr. 2023", "2023-06-30"),
        (t.datequarterend, "last quarter 2025", "2025-12-31"),
        (t.datequarterend, "fourth quarter of fiscal year 2025", "2025-12-31"),
        (t.datequarterend, "Quarter 3 2025", "2025-09-30"),
        (t.datequarterend, "2025, third quarter", "2025-09-30"),
    ],
)
def test_sec_transform_outputs(function, displayed: str, expected: str) -> None:
    assert function(displayed) == expected


@pytest.mark.parametrize(
    ("function", "displayed"),
    [
        (t.boolballotbox, "X"),
        (t.countrynameen, "Atlantis"),
        (t.stateprovnameen, "Canada"),
        (t.edgarprovcountryen, "Atlantis"),
        (t.exchnameen, "Unknown Exchange"),
        (t.entityfilercategoryen, "Big Filer"),
        (t.numwordsen, "twelve apples"),
        (t.numwordsen, "1.5 million"),
        (t.numwordsen, "a hundred"),
        (t.duryear, "about two"),
        (t.duryear, "1,000"),
        (t.duryear, "1e2"),
        (t.durwordsen, "two"),
        (t.durwordsen, "three weeks"),
        (t.exchnameen, "NYSE Texas, Inc."),
        (t.exchnameen, "Long-Term Stock Exchange"),
        (t.exchnameen, "MEMX LLC"),
        (t.exchnameen, "The Nasdaq Capital Market"),
        (t.entityfilercategoryen, "Smaller Reporting Company"),
        (t.entityfilercategoryen, "Smaller Reporting Accelerated Filer"),
        (t.entityfilercategoryen, "Nonaccelerated Filer"),
        (t.edgarprovcountryen, "Delaware"),
        (t.yesnoballotbox, "Yes"),
        (t.datequarterend, "Q5 2025"),
    ],
)
def test_sec_transform_rejects_unmapped_input(function, displayed: str, monkeypatch) -> None:
    import builtins

    from arelle.formula.XPathContext import FunctionArgType

    # Arelle installs gettext's `_` when a controller starts; these run without one.
    monkeypatch.setattr(builtins, "_", str, raising=False)

    with pytest.raises(FunctionArgType):
        function(displayed)


def test_registry_covers_required_transforms() -> None:
    required = {
        "boolballotbox", "yesnoballotbox", "countrynameen", "stateprovnameen",
        "edgarprovcountryen", "exchnameen", "entityfilercategoryen", "numwordsen", "numinf",
        "numneginf", "numnan", "duryear", "durmonth", "durweek", "durday", "durhour",
        "durwordsen", "datequarterend",
    }  # fmt: skip
    assert required <= set(t.SEC_TRANSFORMS)
    registered: dict = {}
    t.load_custom_transforms(registered)
    assert {key.namespaceURI for key in registered} == {t.SEC_TRANSFORM_NAMESPACE}
    assert {key.localName for key in registered} == set(t.SEC_TRANSFORMS)


def test_booltrue_and_boolfalse_are_not_sec_transforms() -> None:
    # They are not in the SEC registry, so Arelle must report them as invalid.
    assert not {"booltrue", "boolfalse"} & set(t.SEC_TRANSFORMS)
