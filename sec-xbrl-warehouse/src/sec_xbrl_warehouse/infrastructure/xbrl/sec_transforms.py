"""Arelle plugin registering the SEC inline XBRL transformation registry.

EDGAR filings format cover-page and some numeric facts with `ixt-sec`
transforms (namespace `http://www.sec.gov/inlineXBRL/transformation/2015-08-31`).
`arelle-release` does not ship that registry, so this in-repo plugin registers
the transforms through Arelle's `ModelManager.LoadCustomTransforms` hook,
following the SEC "Inline XBRL Transformation Registry" semantics. Arelle still
parses the document, applies each transform, and validates the result against
the concept's type; these functions only map displayed text to an XBRL lexical
value.

A transform that cannot map its input raises Arelle's `FunctionArgType`, which
Arelle reports as `ix11.10.1.2:transformValueError`. Formats in the namespace
that are not registered here are reported by Arelle as `invalidTransformation`.

The module imports nothing from `sec_xbrl_warehouse`; Arelle loads it by path.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

SEC_TRANSFORM_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
# Version of this implementation. Increment it (and the extraction revision)
# whenever a mapping or a transform's output changes.
SEC_TRANSFORM_REGISTRY_VERSION = "2015-08-31/sec-xbrl-warehouse-1"


def _fail(expected: str) -> Exception:
    from arelle.formula.XPathContext import FunctionArgType

    error: Exception = FunctionArgType(0, expected)
    return error


def _key(text: str) -> str:
    """Normalize a displayed name for table lookup."""
    text = unicodedata.normalize("NFKD", text.replace("\u2019", "'"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.removeprefix("the ")


def _table(rows: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in rows.strip().splitlines():
        code, _, names = line.partition(":")
        for name in names.split("|"):
            table[_key(name)] = code.strip()
    return table


# --- Boolean transforms ----------------------------------------------------


def boolballotbox(arg: str) -> str:
    text = arg.strip()
    if text == "☐":  # ballot box
        return "false"
    if text in {"☑", "☒"}:  # ballot box with check / with X
        return "true"
    raise _fail("ixt-sec:boolballotbox")


def boolfalse(arg: str) -> str:
    return "false"


def booltrue(arg: str) -> str:
    return "true"


# --- Name-to-code transforms -----------------------------------------------

_US_STATES = """
AL:Alabama
AK:Alaska
AZ:Arizona
AR:Arkansas
CA:California
CO:Colorado
CT:Connecticut
DE:Delaware
DC:District of Columbia
FL:Florida
GA:Georgia
HI:Hawaii
ID:Idaho
IL:Illinois
IN:Indiana
IA:Iowa
KS:Kansas
KY:Kentucky
LA:Louisiana
ME:Maine
MD:Maryland
MA:Massachusetts
MI:Michigan
MN:Minnesota
MS:Mississippi
MO:Missouri
MT:Montana
NE:Nebraska
NV:Nevada
NH:New Hampshire
NJ:New Jersey
NM:New Mexico
NY:New York
NC:North Carolina
ND:North Dakota
OH:Ohio
OK:Oklahoma
OR:Oregon
PA:Pennsylvania
RI:Rhode Island
SC:South Carolina
SD:South Dakota
TN:Tennessee
TX:Texas
UT:Utah
VT:Vermont
VA:Virginia
WA:Washington
WV:West Virginia
WI:Wisconsin
WY:Wyoming
AS:American Samoa
GU:Guam
MP:Northern Mariana Islands
PR:Puerto Rico
VI:Virgin Islands|U.S. Virgin Islands|United States Virgin Islands
"""

_CANADIAN_PROVINCES = """
AB:Alberta
BC:British Columbia
MB:Manitoba
NB:New Brunswick
NL:Newfoundland and Labrador|Newfoundland
NS:Nova Scotia
NT:Northwest Territories
NU:Nunavut
ON:Ontario
PE:Prince Edward Island
QC:Quebec|Québec
SK:Saskatchewan
YT:Yukon|Yukon Territory
"""

_STATES = _table(_US_STATES + _CANADIAN_PROVINCES)

_COUNTRIES = _table(
    """
AF:Afghanistan
AX:Aland Islands|Åland Islands
AL:Albania
DZ:Algeria
AS:American Samoa
AD:Andorra
AO:Angola
AI:Anguilla
AQ:Antarctica
AG:Antigua and Barbuda
AR:Argentina
AM:Armenia
AW:Aruba
AU:Australia
AT:Austria
AZ:Azerbaijan
BS:Bahamas|The Bahamas
BH:Bahrain
BD:Bangladesh
BB:Barbados
BY:Belarus
BE:Belgium
BZ:Belize
BJ:Benin
BM:Bermuda
BT:Bhutan
BO:Bolivia|Bolivia, Plurinational State of
BQ:Bonaire, Sint Eustatius and Saba
BA:Bosnia and Herzegovina
BW:Botswana
BV:Bouvet Island
BR:Brazil
IO:British Indian Ocean Territory
VG:British Virgin Islands|Virgin Islands, British
BN:Brunei|Brunei Darussalam
BG:Bulgaria
BF:Burkina Faso
BI:Burundi
CV:Cabo Verde|Cape Verde
KH:Cambodia
CM:Cameroon
CA:Canada
KY:Cayman Islands
CF:Central African Republic
TD:Chad
CL:Chile
CN:China|People's Republic of China
CX:Christmas Island
CC:Cocos (Keeling) Islands|Cocos Islands
CO:Colombia
KM:Comoros
CG:Congo|Republic of the Congo
CD:Democratic Republic of the Congo|Congo, the Democratic Republic of the
CK:Cook Islands
CR:Costa Rica
CI:Cote d'Ivoire|Côte d'Ivoire|Ivory Coast
HR:Croatia
CU:Cuba
CW:Curacao|Curaçao
CY:Cyprus
CZ:Czech Republic|Czechia
DK:Denmark
DJ:Djibouti
DM:Dominica
DO:Dominican Republic
EC:Ecuador
EG:Egypt
SV:El Salvador
GQ:Equatorial Guinea
ER:Eritrea
EE:Estonia
SZ:Eswatini|Swaziland
ET:Ethiopia
FK:Falkland Islands|Falkland Islands (Malvinas)
FO:Faroe Islands
FJ:Fiji
FI:Finland
FR:France
GF:French Guiana
PF:French Polynesia
TF:French Southern Territories
GA:Gabon
GM:Gambia|The Gambia
GE:Georgia
DE:Germany
GH:Ghana
GI:Gibraltar
GR:Greece
GL:Greenland
GD:Grenada
GP:Guadeloupe
GU:Guam
GT:Guatemala
GG:Guernsey
GN:Guinea
GW:Guinea-Bissau
GY:Guyana
HT:Haiti
HM:Heard Island and McDonald Islands
VA:Holy See|Vatican City
HN:Honduras
HK:Hong Kong
HU:Hungary
IS:Iceland
IN:India
ID:Indonesia
IR:Iran|Iran, Islamic Republic of
IQ:Iraq
IE:Ireland
IM:Isle of Man
IL:Israel
IT:Italy
JM:Jamaica
JP:Japan
JE:Jersey
JO:Jordan
KZ:Kazakhstan
KE:Kenya
KI:Kiribati
KP:North Korea|Korea, Democratic People's Republic of
KR:South Korea|Korea, Republic of|Republic of Korea|Korea
KW:Kuwait
KG:Kyrgyzstan
LA:Laos|Lao People's Democratic Republic
LV:Latvia
LB:Lebanon
LS:Lesotho
LR:Liberia
LY:Libya
LI:Liechtenstein
LT:Lithuania
LU:Luxembourg
MO:Macao|Macau
MG:Madagascar
MW:Malawi
MY:Malaysia
MV:Maldives
ML:Mali
MT:Malta
MH:Marshall Islands
MQ:Martinique
MR:Mauritania
MU:Mauritius
YT:Mayotte
MX:Mexico
FM:Micronesia|Micronesia, Federated States of
MD:Moldova|Moldova, Republic of
MC:Monaco
MN:Mongolia
ME:Montenegro
MS:Montserrat
MA:Morocco
MZ:Mozambique
MM:Myanmar|Burma
NA:Namibia
NR:Nauru
NP:Nepal
NL:Netherlands|The Netherlands
NC:New Caledonia
NZ:New Zealand
NI:Nicaragua
NE:Niger
NG:Nigeria
NU:Niue
NF:Norfolk Island
MK:North Macedonia|Macedonia
MP:Northern Mariana Islands
NO:Norway
OM:Oman
PK:Pakistan
PW:Palau
PS:Palestine|Palestine, State of
PA:Panama
PG:Papua New Guinea
PY:Paraguay
PE:Peru
PH:Philippines
PN:Pitcairn
PL:Poland
PT:Portugal
PR:Puerto Rico
QA:Qatar
RE:Reunion|Réunion
RO:Romania
RU:Russia|Russian Federation
RW:Rwanda
BL:Saint Barthelemy|Saint Barthélemy
SH:Saint Helena
KN:Saint Kitts and Nevis
LC:Saint Lucia
MF:Saint Martin
PM:Saint Pierre and Miquelon
VC:Saint Vincent and the Grenadines
WS:Samoa
SM:San Marino
ST:Sao Tome and Principe
SA:Saudi Arabia
SN:Senegal
RS:Serbia
SC:Seychelles
SL:Sierra Leone
SG:Singapore
SX:Sint Maarten
SK:Slovakia
SI:Slovenia
SB:Solomon Islands
SO:Somalia
ZA:South Africa
GS:South Georgia and the South Sandwich Islands
SS:South Sudan
ES:Spain
LK:Sri Lanka
SD:Sudan
SR:Suriname
SJ:Svalbard and Jan Mayen
SE:Sweden
CH:Switzerland
SY:Syria|Syrian Arab Republic
TW:Taiwan|Taiwan, Province of China
TJ:Tajikistan
TZ:Tanzania|Tanzania, United Republic of
TH:Thailand
TL:Timor-Leste|East Timor
TG:Togo
TK:Tokelau
TO:Tonga
TT:Trinidad and Tobago
TN:Tunisia
TR:Turkey|Türkiye|Turkiye
TM:Turkmenistan
TC:Turks and Caicos Islands
TV:Tuvalu
UG:Uganda
UA:Ukraine
AE:United Arab Emirates
GB:United Kingdom|United Kingdom of Great Britain and Northern Ireland|Great Britain
US:United States|United States of America|USA|U.S.A.
UM:United States Minor Outlying Islands
UY:Uruguay
UZ:Uzbekistan
VU:Vanuatu
VE:Venezuela|Venezuela, Bolivarian Republic of
VN:Vietnam|Viet Nam
VI:Virgin Islands, U.S.|U.S. Virgin Islands
WF:Wallis and Futuna
EH:Western Sahara
YE:Yemen
ZM:Zambia
ZW:Zimbabwe
"""
)

# EDGAR state and country codes. Only codes confirmed against the EDGAR code
# list are included; other names fail with a transform diagnostic rather than
# a guessed code.
_EDGAR_LOCATIONS = {
    **{
        name: code
        for name, code in _table(_US_STATES).items()
        if code not in {"AS", "MP"}  # EDGAR uses its own codes for these
    },
    **_table(
        """
A0:Alberta|Alberta, Canada
A1:British Columbia|British Columbia, Canada
A2:Manitoba|Manitoba, Canada
A3:New Brunswick|New Brunswick, Canada
A4:Newfoundland|Newfoundland, Canada|Newfoundland and Labrador
A5:Nova Scotia|Nova Scotia, Canada
A6:Ontario|Ontario, Canada
A7:Prince Edward Island|Prince Edward Island, Canada
A8:Quebec|Quebec, Canada|Québec
A9:Saskatchewan|Saskatchewan, Canada
B0:Yukon|Yukon, Canada
Z4:Canada|Canada (Federal Level)
C3:Australia
C4:Austria
C5:Bahamas
C9:Belgium
D0:Bermuda
D5:Brazil
D8:British Virgin Islands|Virgin Islands, British
E9:Cayman Islands
F4:China
G7:Denmark
H9:Finland
I0:France
2M:Germany
K3:Hong Kong
K7:India
L2:Ireland
L3:Israel
L6:Italy
M0:Japan
M5:Korea, Republic of|South Korea
N4:Luxembourg
1T:Marshall Islands
O5:Mexico
P7:Netherlands
Q2:New Zealand
P8:Norway
R1:Panama
U0:Singapore
T3:South Africa
U3:Spain
V7:Sweden
V8:Switzerland
F5:Taiwan
X0:United Kingdom
Y7:Guernsey
Y8:Isle of Man
Y9:Jersey
"""
    ),
}


def _lookup(table: dict[str, str], name: str) -> Callable[[str], str]:
    def transform(arg: str) -> str:
        code = table.get(_key(arg))
        if code is None:
            raise _fail(f"ixt-sec:{name}")
        return code

    transform.__name__ = name
    return transform


countrynameen = _lookup(_COUNTRIES, "countrynameen")
stateprovnameen = _lookup(_STATES, "stateprovnameen")
edgarprovcountryen = _lookup(_EDGAR_LOCATIONS, "edgarprovcountryen")

# dei exchangeCodeItemType codes. More specific names come first.
_EXCHANGES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), code)
    for pattern, code in (
        (r"\bnyse arca\b|\barca\b", "NYSEArca"),
        (r"\bnyse american\b|\bnyse mkt\b|\bamerican stock exchange\b", "NYSEAMER"),
        (r"\bnyse national\b", "NYSENAT"),
        (r"\bnyse chicago\b|\bchicago stock exchange\b", "CHX"),
        (r"\bnasdaq phlx\b|\bphiladelphia stock exchange\b|\bphlx\b", "PHLX"),
        (r"\bnasdaq bx\b|\bboston stock exchange\b", "BX"),
        (r"\bnasdaq\b", "NASDAQ"),
        (r"\bnew york stock exchange\b|\bnyse\b", "NYSE"),
        (r"\bcboe byx\b", "CboeBYX"),
        (r"\bcboe bzx\b|\bbats\b", "CboeBZX"),
        (r"\bcboe edga\b", "CboeEDGA"),
        (r"\bcboe edgx\b", "CboeEDGX"),
        (r"\bcboe c2\b|\bc2 options\b", "C2"),
        (r"\bcboe\b|\bchicago board options exchange\b", "CBOE"),
        (r"\binvestors exchange\b|\biex\b", "IEX"),
        (r"\blong term stock exchange\b|\bltse\b", "LTSE"),
        (r"\bmembers exchange\b|\bmemx\b", "MEMX"),
        (r"\bbox options exchange\b|\bbox exchange\b", "BOX"),
    )
)


def exchnameen(arg: str) -> str:
    text = _key(arg.replace("-", " "))
    for pattern, code in _EXCHANGES:
        if pattern.search(text):
            return code
    raise _fail("ixt-sec:exchnameen")


_FILER_CATEGORIES = {
    _key(name.replace("-", " ")): name
    for name in (
        "Large Accelerated Filer",
        "Accelerated Filer",
        "Non-accelerated Filer",
        "Smaller Reporting Company",
        "Smaller Reporting Accelerated Filer",
    )
}


def entityfilercategoryen(arg: str) -> str:
    category = _FILER_CATEGORIES.get(_key(arg.replace("-", " ")))
    if category is None:
        raise _fail("ixt-sec:entityfilercategoryen")
    return category


# --- Numeric transforms ----------------------------------------------------

_SMALL = {
    word: value
    for value, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
    )
}
_TENS = {
    word: value * 10
    for value, word in enumerate(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], start=2
    )
}
_SCALES = {
    "thousand": 10**3,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
}


def _words_to_number(text: str) -> Decimal | None:
    """Parse an English cardinal (`twelve thousand`, `one hundred and five`)."""
    words = re.sub(r"[,\-]", " ", text.lower()).split()
    words = [word for word in words if word not in {"and", "a"}]
    if not words:
        return None
    if words in (["no"], ["none"]):
        return Decimal(0)
    total = Decimal(0)
    current = Decimal(0)
    seen = False
    for word in words:
        if word in _SMALL:
            current += _SMALL[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or Decimal(1)) * 100
        elif word in _SCALES:
            total += (current or Decimal(1)) * _SCALES[word]
            current = Decimal(0)
        elif re.fullmatch(r"\d+(?:\.\d+)?", word):
            current += Decimal(word)
        else:
            return None
        seen = True
    return total + current if seen else None


def numwordsen(arg: str) -> str:
    value = _words_to_number(arg)
    if value is None:
        raise _fail("ixt-sec:numwordsen")
    return str(value)


def numinf(arg: str) -> str:
    return "INF"


def numneginf(arg: str) -> str:
    return "-INF"


def numnan(arg: str) -> str:
    return "NaN"


# --- Duration transforms ---------------------------------------------------

# The SEC registry converts fractional units with an average month of
# 30.4375 days and truncates the remainder at each step.
_DAYS_PER_MONTH = Decimal("30.4375")


def _number(arg: str, name: str) -> tuple[str, Decimal]:
    text = arg.strip().replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _fail(f"ixt-sec:{name}") from None
    if not value.is_finite():
        raise _fail(f"ixt-sec:{name}")
    return ("-" if value < 0 else ""), abs(value)


def _duration(sign: str, parts: list[tuple[int, str]], time_parts: list[tuple[int, str]]) -> str:
    date_text = "".join(f"{value}{unit}" for value, unit in parts if value)
    time_text = "".join(f"{value}{unit}" for value, unit in time_parts if value)
    if not date_text and not time_text:
        return "P0D"
    return f"{sign}P{date_text}" + (f"T{time_text}" if time_text else "")


def _split(value: Decimal, factor: Decimal) -> tuple[int, Decimal]:
    whole = int(value)
    return whole, (value - whole) * factor


def duryear(arg: str) -> str:
    sign, value = _number(arg, "duryear")
    years, months = _split(value, Decimal(12))
    whole_months, days = _split(months, _DAYS_PER_MONTH)
    return _duration(sign, [(years, "Y"), (whole_months, "M"), (int(days), "D")], [])


def durmonth(arg: str) -> str:
    sign, value = _number(arg, "durmonth")
    months, days = _split(value, _DAYS_PER_MONTH)
    return _duration(sign, [(months, "M"), (int(days), "D")], [])


def durweek(arg: str) -> str:
    sign, value = _number(arg, "durweek")
    return _duration(sign, [(int(value * 7), "D")], [])


def durday(arg: str) -> str:
    sign, value = _number(arg, "durday")
    days, hours = _split(value, Decimal(24))
    whole_hours, minutes = _split(hours, Decimal(60))
    return _duration(sign, [(days, "D")], [(whole_hours, "H"), (int(minutes), "M")])


def durhour(arg: str) -> str:
    sign, value = _number(arg, "durhour")
    hours, minutes = _split(value, Decimal(60))
    return _duration(sign, [], [(hours, "H"), (int(minutes), "M")])


_DURATION_UNIT = re.compile(r"(years?|months?|weeks?|days?)$")


def durwordsen(arg: str) -> str:
    """`two years, six months` -> `P2Y6M`; weeks become days."""
    text = re.sub(r"\band\b|,", " ", arg.lower())
    tokens = text.split()
    totals = {"Y": 0, "M": 0, "D": 0}
    pending: list[str] = []
    seen = False
    for token in tokens:
        match = _DURATION_UNIT.match(token)
        if match is None:
            pending.append(token)
            continue
        amount = _words_to_number(" ".join(pending))
        if amount is None or amount != int(amount):
            raise _fail("ixt-sec:durwordsen")
        unit = match.group(1)[0]
        if unit == "y":
            totals["Y"] += int(amount)
        elif unit == "m":
            totals["M"] += int(amount)
        elif unit == "w":
            totals["D"] += int(amount) * 7
        else:
            totals["D"] += int(amount)
        pending = []
        seen = True
    if pending or not seen:
        raise _fail("ixt-sec:durwordsen")
    return _duration("", [(totals["Y"], "Y"), (totals["M"], "M"), (totals["D"], "D")], [])


# --- Date transforms -------------------------------------------------------

_QUARTER_WORDS = {
    "1": 1,
    "1st": 1,
    "first": 1,
    "2": 2,
    "2nd": 2,
    "second": 2,
    "3": 3,
    "3rd": 3,
    "third": 3,
    "4": 4,
    "4th": 4,
    "fourth": 4,
}
_QUARTER_ENDS = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
_QUARTER_PATTERNS = (
    re.compile(r"^q([1-4])\s*(\d{4})$"),
    re.compile(r"^(\d{4})\s*q([1-4])$"),
    re.compile(r"^(1st|2nd|3rd|4th|first|second|third|fourth|[1-4])\s*(?:quarter|qtr)\s*(\d{4})$"),
)


def datequarterend(arg: str) -> str:
    """`Q1 2025`, `2025 Q1`, `first quarter 2025` -> `2025-03-31`."""
    text = re.sub(r"[,.]|\bof\b|\bfiscal\b", " ", arg.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for index, pattern in enumerate(_QUARTER_PATTERNS):
        match = pattern.match(text)
        if match:
            quarter_text, year = (match.group(2), match.group(1)) if index == 1 else match.groups()
            return f"{year}-{_QUARTER_ENDS[_QUARTER_WORDS[quarter_text]]}"
    raise _fail("ixt-sec:datequarterend")


SEC_TRANSFORMS: dict[str, Callable[[str], str]] = {
    "boolballotbox": boolballotbox,
    "boolfalse": boolfalse,
    "booltrue": booltrue,
    "countrynameen": countrynameen,
    "stateprovnameen": stateprovnameen,
    "edgarprovcountryen": edgarprovcountryen,
    "exchnameen": exchnameen,
    "entityfilercategoryen": entityfilercategoryen,
    "numwordsen": numwordsen,
    "numinf": numinf,
    "numneginf": numneginf,
    "numnan": numnan,
    "duryear": duryear,
    "durmonth": durmonth,
    "durweek": durweek,
    "durday": durday,
    "durhour": durhour,
    "durwordsen": durwordsen,
    "datequarterend": datequarterend,
}


def load_custom_transforms(custom_transforms: dict[Any, Callable[[str], str]]) -> None:
    """`ModelManager.LoadCustomTransforms` hook: register every `ixt-sec` function."""
    from arelle.ModelValue import qname

    for name, function in SEC_TRANSFORMS.items():
        custom_transforms[qname(SEC_TRANSFORM_NAMESPACE, f"ixt-sec:{name}")] = function


__pluginInfo__ = {
    "name": "SEC inline XBRL transformation registry (sec-xbrl-warehouse)",
    "version": SEC_TRANSFORM_REGISTRY_VERSION,
    "description": "Registers ixt-sec transforms for EDGAR inline XBRL filings.",
    "license": "Same as sec-xbrl-warehouse",
    "author": "sec-xbrl-warehouse",
    "ModelManager.LoadCustomTransforms": load_custom_transforms,
}
