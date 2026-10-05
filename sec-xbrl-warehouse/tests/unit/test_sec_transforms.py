import pytest

from sec_xbrl_warehouse.infrastructure.xbrl import sec_transforms as t


@pytest.mark.parametrize(
    ("function", "displayed", "expected"),
    [
        (t.boolballotbox, "☒", "true"),
        (t.boolballotbox, " ☑ ", "true"),
        (t.boolballotbox, "☐", "false"),
        (t.boolfalse, "No", "false"),
        (t.booltrue, "Yes", "true"),
        (t.countrynameen, "Canada", "CA"),
        (t.countrynameen, "Korea, Republic of", "KR"),
        (t.countrynameen, "Côte d" + chr(0x2019) + "Ivoire", "CI"),
        (t.countrynameen, "the Netherlands", "NL"),
        (t.stateprovnameen, "California", "CA"),
        (t.stateprovnameen, "Ontario", "ON"),
        (t.edgarprovcountryen, "Delaware", "DE"),
        (t.edgarprovcountryen, "Ontario, Canada", "A6"),
        (t.edgarprovcountryen, "Cayman Islands", "E9"),
        (t.exchnameen, "The Nasdaq Stock Market LLC", "NASDAQ"),
        (t.exchnameen, "Nasdaq Global Select Market", "NASDAQ"),
        (t.exchnameen, "New York Stock Exchange", "NYSE"),
        (t.exchnameen, "NYSE Arca, Inc.", "NYSEArca"),
        (t.exchnameen, "NYSE American LLC", "NYSEAMER"),
        (t.exchnameen, "Cboe BZX Exchange, Inc.", "CboeBZX"),
        (t.entityfilercategoryen, "large accelerated filer", "Large Accelerated Filer"),
        (t.entityfilercategoryen, "Non-Accelerated Filer", "Non-accelerated Filer"),
        (t.numwordsen, "twelve thousand", "12000"),
        (t.numwordsen, "one hundred and twenty-five", "125"),
        (t.numwordsen, "two million three hundred thousand", "2300000"),
        (t.numwordsen, "none", "0"),
        (t.numinf, "unlimited", "INF"),
        (t.numneginf, "x", "-INF"),
        (t.numnan, "n/a", "NaN"),
        (t.duryear, "22.3", "P22Y3M18D"),
        (t.duryear, "5", "P5Y"),
        (t.duryear, "-1.5", "-P1Y6M"),
        (t.durmonth, "22.3", "P22M9D"),
        (t.durweek, "22.3", "P156D"),
        (t.durday, "22.3", "P22DT7H12M"),
        (t.durhour, "22.3", "PT22H18M"),
        (t.duryear, "0", "P0D"),
        (t.durwordsen, "two years, six months", "P2Y6M"),
        (t.durwordsen, "Three weeks and 2 days", "P23D"),
        (t.datequarterend, "Q1 2025", "2025-03-31"),
        (t.datequarterend, "2025 Q3", "2025-09-30"),
        (t.datequarterend, "fourth quarter of 2024", "2024-12-31"),
        (t.datequarterend, "2nd Qtr. 2023", "2023-06-30"),
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
        (t.duryear, "about two"),
        (t.durwordsen, "two"),
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
        "boolballotbox", "boolfalse", "booltrue", "countrynameen", "stateprovnameen",
        "edgarprovcountryen", "exchnameen", "entityfilercategoryen", "numwordsen", "numinf",
        "numneginf", "numnan", "duryear", "durmonth", "durweek", "durday", "durhour",
        "durwordsen", "datequarterend",
    }  # fmt: skip
    assert required <= set(t.SEC_TRANSFORMS)
    registered: dict = {}
    t.load_custom_transforms(registered)
    assert {key.namespaceURI for key in registered} == {t.SEC_TRANSFORM_NAMESPACE}
    assert {key.localName for key in registered} == set(t.SEC_TRANSFORMS)
