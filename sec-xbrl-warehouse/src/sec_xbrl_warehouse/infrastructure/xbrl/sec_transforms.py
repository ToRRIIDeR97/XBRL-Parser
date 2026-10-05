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

import calendar
import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal
from typing import Any

SEC_TRANSFORM_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
# Version of this implementation. Increment it (and the extraction revision)
# whenever a mapping or a transform's output changes.
SEC_TRANSFORM_REGISTRY_VERSION = "2015-08-31/sec-xbrl-warehouse-2"


def _fail(expected: str) -> Exception:
    from arelle.formula.XPathContext import FunctionArgType

    error: Exception = FunctionArgType(0, expected)
    return error


def _key(text: str) -> str:
    """Normalize displayed text for table lookup (case, accents, punctuation)."""
    text = unicodedata.normalize("NFKD", text.replace("\u2019", "'"))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _table(rows: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in rows.strip().splitlines():
        code, _, names = line.partition(":")
        for name in names.split("|"):
            table.setdefault(_key(name), code.strip())
    return table


# --- Ballot-box transforms -------------------------------------------------

# U+2610 ballot box, U+2611 with check, U+2612 with X.
_BALLOT_BOXES = {"\u2610": False, "\u2611": True, "\u2612": True}


def _ballot(arg: str, name: str) -> bool:
    checked = _BALLOT_BOXES.get(arg.strip())
    if checked is None:
        raise _fail(f"ixt-sec:{name}")
    return checked


def boolballotbox(arg: str) -> str:
    return "true" if _ballot(arg, "boolballotbox") else "false"


def yesnoballotbox(arg: str) -> str:
    return "Yes" if _ballot(arg, "yesnoballotbox") else "No"


# --- Name-to-code transforms -----------------------------------------------
#
# Name tables are data taken from the SEC registry: each row is
# `CODE:name|name` with names already normalized by `_key`. Codes follow the
# dei code lists (stateOrProvinceItemType, countryCodeItemType,
# edgarExchangeCodeItemType) and the SEC "EDGAR State and Country Codes".
# Matching is on the whole normalized text; anything else fails.

_STATES = _table(
    """
AL:alabama
AK:alaska
AZ:arizona
AR:arkansas
CA:california
CO:colorado
CT:connecticut
DE:delaware
FL:florida
GA:georgia
HI:hawaii
ID:idaho
IL:illinois
IN:indiana
IA:iowa
KS:kansas
KY:kentucky
LA:louisiana
ME:maine
MD:maryland
MA:massachusetts
MI:michigan
MN:minnesota
MS:mississippi
MO:missouri
MT:montana
NE:nebraska
NV:nevada
NH:new hampshire
NJ:new jersey
NM:new mexico
NY:new york
NC:north carolina
ND:north dakota
OH:ohio
OK:oklahoma
OR:oregon
PA:pennsylvania
RI:rhode island
SC:south carolina
SD:south dakota
TN:tennessee
TX:texas
UT:utah
VT:vermont
VA:virginia
WA:washington
WV:west virginia
WI:wisconsin
WY:wyoming
DC:district of columbia
AS:american samoa|samoa
GU:guam
MP:northern mariana islands
PR:puerto rico
UM:united states minor outlying islands
VI:virgin islands u s
AB:alberta
BC:british columbia
MB:manitoba
NB:new brunswick
NL:newfoundland|newfoundland and labrador
NS:nova scotia
NT:northwest territories
NU:nunavut
ON:ontario
PE:prince edward island
QC:quebec
SK:saskatchewan
YT:yukon
"""
)

_COUNTRIES = _table(
    """
AF:afghanistan
AX:aland islands
AL:albania
DZ:algeria
AS:american samoa
AD:andorra
AO:angola
AI:anguilla
AQ:antarctica
AG:antigua and barbuda
AR:argentina|republic of argentina|the republic of argentina
AM:armenia
AW:aruba
AU:australia
AT:austria
AZ:azerbaijan
BS:bahamas
BH:bahrain
BD:bangladesh
BB:barbados
BY:belarus
BE:belgium
BZ:belize
BJ:benin
BM:bermuda
BT:bhutan
BO:bolivia
BQ:bonaire sint eustatius and saba
BA:bosnia and herzegovina
BW:botswana
BV:bouvet island
BR:brasil|brazil|federative republic of brasil|federative republic of brazil|the federative republic of brasil|the federative republic of brazil
IO:british indian ocean territory
BN:brunei darussalam
BG:bulgaria
BF:burkina faso
BI:burundi
CV:cabo verde
KH:cambodia
CM:cameroon
CA:canada
KY:cayman islands
CF:central african republic
TD:chad
CL:chile
CN:china|people's republic of china|peoples republic of china|the people's republic of china|the peoples republic of china
CX:christmas island
CC:cocos islands|cocos keeling islands
CO:colombia|republic of colombia|the republic of colombia
KM:comoros
CD:democratic republic of congo|democratic republic of the congo
CG:congo
CK:cook islands
CR:costa rica
CI:cote d'ivoire
HR:croatia
CU:cuba
CW:curacao
CY:cyprus
CZ:czech republic|czechia
DK:denmark|kingdom of denmark|the kingdom of denmark
DJ:djibouti
DM:dominica
DO:dominican republic
EC:ecuador
EG:egypt
SV:el salvador
GQ:equatorial guinea
ER:eritrea
EE:estonia
SZ:eswatini
ET:ethiopia
FK:falkland islands|falkland islands malvinas|islas malvinas|islas malvinas islands|malvinas|malvinas islands|the falkland islands|the falkland islands malvinas
FO:faroe islands
FJ:fiji
FI:finland
FR:france
GF:french guiana
PF:french polynesia
TF:french southern territories
GA:gabon
GM:gambia
GE:georgia
DE:germany
GH:ghana
GI:gibraltar
GR:greece
GL:greenland
GD:grenada
GP:guadeloupe
GU:guam
GT:guatemala
GG:guernsey
GN:guinea
GW:guinea bissau
GY:guyana
HT:haiti
HM:heard island and mcdonald islands
VA:holy see|the holy see|vatican city
HN:honduras
HK:hong kong
HU:hungary
IS:iceland
IN:india
ID:indonesia
IR:iran|islamic republic of iran|the islamic republic of iran
IQ:iraq
IE:ireland
IM:isle of man
IL:israel
IT:italy
JM:jamaica
JP:japan
JE:jersey
JO:jordan
KZ:kazakhstan
KE:kenya
KI:kiribati
KP:democratic people's republic of korea|democratic peoples republic of korea|north korea
KR:republic of korea|south korea
KW:kuwait
KG:kyrgyzstan
LA:lao people's democratic republic|lao peoples democratic republic
LV:latvia
LB:lebanon
LS:lesotho
LR:liberia
LY:libya|state of libya|the state of libya
LI:liechtenstein
LT:lithuania
LU:duchy of luxembourg|grand duchy of luxembourg|luxembourg|the duchy of luxembourg|the grand duchy of luxembourg
MO:macao|macau
MG:madagascar
MW:malawi
MY:malaysia
MV:maldives
ML:mali
MT:malta
MH:marshall islands|republic of marshall islands|republic of the marshall islands|the republic of marshall islands|the republic of the marshall islands
MQ:martinique
MR:mauritania
MU:mauritius
YT:mayotte
MX:mexico|united mexican states
FM:federated states of micronesia|micronesia|the federated states of micronesia
MD:moldova|republic of moldova|the republic of moldova
MC:monaco
MN:mongolia
ME:montenegro
MS:montserrat
MA:morocco
MZ:mozambique
MM:myanmar
NA:namibia
NR:nauru
NP:nepal
NL:netherlands
NC:new caledonia
NZ:new zealand
NI:nicaragua
NE:niger
NG:nigeria
NU:niue
NF:norfolk island
MK:north macedonia|republic of north macedonia|the republic of north macedonia
MP:northern mariana islands
NO:norway
OM:oman
PK:pakistan
PW:palau
PS:palestine|state of palestine|the state of palestine
PA:panama
PG:papua new guinea
PY:paraguay
PE:peru
PH:philippines
PN:pitcairn
PL:poland
PT:portugal
PR:puerto rico
QA:qatar
RE:reunion
RO:romania
RU:russian federation
RW:rwanda
BL:saint barthelemy
SH:saint helena|saint helena ascension and tristan|saint helena ascension and tristan da cunha
KN:saint kitts and nevis
LC:saint lucia
MF:saint martin
PM:saint pierre and miquelon
VC:saint vincent|saint vincent and grenadines|saint vincent and the grenadines
WS:samoa
SM:san marino
ST:sao tome and principe
SA:saudi arabia
SN:senegal
RS:serbia
SC:seychelles
SL:sierra leone
SG:singapore
SX:sint maarten
SK:slovakia
SI:slovenia
SB:solomon islands
SO:somalia
ZA:republic of south africa|south africa|the republic of south africa
GS:south georgia and south sandwich islands|south georgia and the south sandwich islands
SS:south sudan
ES:espana|kingdom of spain|spain|the kingdom of spain
LK:sri lanka
SD:sudan
SR:suriname
SJ:svalbard and jan mayen
SE:sweden
CH:switzerland
SY:syria|syrian arab republic
TW:taiwan|taiwan province of china
TJ:tajikistan
TZ:tanzania|the united republic of tanzania|united republic of tanzania
TH:thailand
TL:timor leste
TG:togo
TK:tokelau
TO:tonga
TT:trinidad and tobago
TN:tunisia
TR:turkey|turkiye
TM:turkmenistan
TC:turks and caicos islands
TV:tuvalu
UG:uganda
UA:ukraine
AE:uae|united arab emirates
GB:britain|england|england and wales|great britain|u k|uk|united kingdom|united kingdom of great britain and northern ireland
UM:united states minor outlying islands
US:u s a|u sa|united states|united states of america|us a|usa
UY:uruguay
UZ:uzbekistan
VU:vanuatu
VE:venezuela
VN:viet nam
VG:british virgin islands
VI:u s virgin islands|us virgin islands
WF:wallis and futuna
EH:western sahar|western sahara
YE:yemen
ZM:zambia
ZW:zimbabwe
"""
)

# EDGAR codes for Canadian provinces, US territories, and countries. US states
# are not part of this registry transform (use stateprovnameen).
_EDGAR_LOCATIONS = _table(
    """
B5:american samoa
GU:guam
PR:puerto rico
2J:united states minor outlying islands
VI:u s virgin islands|us virgin islands|virgin islands u s|virgin islands us
A0:alberta|alberta canada
A1:british columbia|british columbia canada
A2:manitoba|manitoba canada
A3:new brunswick|new brunswick canada
A4:newfoundland|newfoundland and labrador|newfoundland and labrador canada|newfoundland canada
A5:nova scotia|nova scotia canada
A6:ontario|ontario canada
A7:prince edward island|prince edward island canada
A8:quebec|quebec canada
A9:saskatchewan|saskatchewan canada
B0:yukon|yukon canada
B2:afghanistan
Y6:aland islands
B3:albania
B4:algeria
B5:american samoa
B6:andorra
B7:angola
1A:anguilla
B8:antarctica
B9:antigua and barbuda
C1:argentina|republic of argentina|the republic of argentina
1B:armenia
1C:aruba
C3:australia
C4:austria
1D:azerbaijan
C5:bahamas
C6:bahrain
C7:bangladesh
C8:barbados
1F:belarus
C9:belgium
D1:belize
G6:benin
D0:bermuda
D2:bhutan
D3:bolivia
1E:bosnia and herzegovina
B1:botswana
D4:bouvet island
D5:brasil|brazil|federative republic of brasil|federative republic of brazil|the federative republic of brasil|the federative republic of brazil
D6:british indian ocean territory
D9:brunei darussalam
E0:bulgaria
X2:burkina faso
E2:burundi
E8:cabo verde|cape verde
E3:cambodia
E4:cameroon
Z4:canada
E9:cayman islands
F0:central african republic
F2:chad
F3:chile
F4:china|people's republic of china|peoples republic of china|the people's republic of china|the peoples republic of china
F6:christmas island
F7:cocos islands|cocos keeling islands
F8:colombia|republic of colombia|the republic of colombia
F9:comoros
Y3:democratic republic of congo|democratic republic of the congo
G0:congo
G1:cook islands
G2:costa rica
L7:cote d'ivoire
1M:croatia
G3:cuba
G4:cyprus
2N:czech republic|czechia
G7:denmark|kingdom of denmark|the kingdom of denmark
1G:djibouti
G9:dominica
G8:dominican republic
H1:ecuador
H2:egypt
H3:el salvador
H4:equatorial guinea
1J:eritrea
1H:estonia
H5:ethiopia
H7:falkland islands|falkland islands malvinas|islas malvinas|islas malvinas islands|malvinas|malvinas islands|the falkland islands|the falkland islands malvinas
H6:faroe islands
H8:fiji
H9:finland
I0:france
I3:french guiana
I4:french polynesia
2C:french southern territories
I5:gabon
I6:gambia
2Q:georgia
2M:germany
J0:ghana
J1:gibraltar
J3:greece
J4:greenland
J5:grenada
J6:guadeloupe
J8:guatemala
Y7:guernsey
J9:guinea
S0:guinea bissau
K0:guyana
K1:haiti
K4:heard island and mcdonald islands
X4:holy see|the holy see|vatican city
K2:honduras
K3:hong kong
K5:hungary
K6:iceland
K7:india
K8:indonesia
K9:iran|islamic republic of iran|the islamic republic of iran
L0:iraq
L2:ireland
Y8:isle of man
L3:israel
L6:italy
L8:jamaica
M0:japan
Y9:jersey
M2:jordan
1P:kazakhstan
M3:kenya
J2:kiribati
M4:democratic people's republic of korea|democratic peoples republic of korea|north korea
M5:republic of korea|south korea
M6:kuwait
1N:kyrgyzstan
M7:lao people's democratic republic|lao peoples democratic republic
1R:latvia
M8:lebanon
M9:lesotho
N0:liberia
N1:libya|state of libya|the state of libya
N2:liechtenstein
1Q:lithuania
N4:duchy of luxembourg|grand duchy of luxembourg|luxembourg|the duchy of luxembourg|the grand duchy of luxembourg
N5:macao|macau
N6:madagascar
N7:malawi
N8:malaysia
N9:maldives
O0:mali
O1:malta
1T:marshall islands|republic of marshall islands|republic of the marshall islands|the republic of marshall islands|the republic of the marshall islands
O2:martinique
O3:mauritania
O4:mauritius
2P:mayotte
O5:mexico|united mexican states
1K:federated states of micronesia|micronesia|the federated states of micronesia
1S:moldova|republic of moldova|the republic of moldova
O9:monaco
P0:mongolia
Z5:montenegro
P1:montserrat
P2:morocco
P3:mozambique
E1:myanmar
T6:namibia
P5:nauru
P6:nepal
P8:netherlands antilles
P7:netherlands
1W:new caledonia
Q2:new zealand
Q3:nicaragua
Q4:niger
Q5:nigeria
Q6:niue
Q7:norfolk island
1U:macedonia|north macedonia|republic of macedonia|republic of north macedonia|the republic of macedonia|the republic of north macedonia
1V:northern mariana islands
Q8:norway
P4:oman
R0:pakistan
1Y:palau
1X:palestine|state of palestine|the state of palestine
R1:panama
R2:papua new guinea
R4:paraguay
R5:peru
R6:philippines
R8:pitcairn
R9:poland
S1:portugal
S3:qatar
S4:reunion
S5:romania
1Z:russian federation
S6:rwanda
Z0:saint barthelemy
U8:saint helena|saint helena ascension and tristan|saint helena ascension and tristan da cunha
U7:saint kitts and nevis
U9:saint lucia
Z1:saint martin
V0:saint pierre and miquelon
V1:saint vincent|saint vincent and grenadines|saint vincent and the grenadines
Y0:samoa
S8:san marino
S9:sao tome and principe
T0:saudi arabia
T1:senegal
Z2:serbia
T2:seychelles
T8:sierra leone
U0:singapore
2B:slovakia
2A:slovenia
D7:solomon islands
U1:somalia
T3:republic of south africa|south africa|the republic of south africa
1L:south georgia and south sandwich islands|south georgia and the south sandwich islands
U3:espana|kingdom of spain|spain|the kingdom of spain
F1:sri lanka
V2:sudan
V3:suriname
L9:svalbard and jan mayen
V6:swaziland
V7:sweden
V8:switzerland
V9:syria|syrian arab republic
F5:taiwan|taiwan province of china
2D:tajikistan
W0:tanzania|the united republic of tanzania|united republic of tanzania
W1:thailand
Z3:timor leste
W2:togo
W3:tokelau
W4:tonga
W5:trinidad and tobago
W6:tunisia
W8:turkey|turkiye
2E:turkmenistan
W7:turks and caicos islands
2G:tuvalu
W9:uganda
2H:ukraine
C0:uae|united arab emirates
X0:britain|england|england and wales|great britain|u k|uk|united kingdom|united kingdom of great britain and northern ireland
2J:united states minor outlying islands
X1:u s a|u sa|united states|united states of america|us a|usa
X3:uruguay
2K:uzbekistan
2L:vanuatu
X5:venezuela
Q1:viet nam
D8:british virgin islands|virgin islands british
X8:wallis and futuna
U5:western sahar|western sahara
T7:yemen
Y4:zambia
Y5:zimbabwe
XX:unknown
"""
)

_EXCHANGES = _table(
    """
BOX:box exchange|box exchange llc|the box exchange|the box exchange llc
CboeBYX:cboe byx exchange|cboe byx exchange inc|the cboe byx exchange|the cboe byx exchange inc
CboeBZX:cboe bzx exchange|cboe bzx exchange inc|the cboe bzx exchange|the cboe bzx exchange inc
C2:cboe c2 exchange|cboe c2 exchange inc|the cboe c2 exchange|the cboe c2 exchange inc
CboeEDGA:cboe edga exchange|cboe edga exchange inc|the cboe edga exchange|the cboe edga exchange inc
CboeEDGX:cboe edgx exchange|cboe edgx exchange inc|the cboe edgx exchange|the cboe edgx exchange inc
CBOE:cboe exchange|cboe exchange inc|the cboe exchange|the cboe exchange inc
CHX:chicago stock exchange|chicago stock exchange inc|the chicago stock exchange|the chicago stock exchange inc
IEX:investors exchange|investors exchange llc|the investors exchange|the investors exchange llc
MIAX:miami international securities exchange|miami international securities exchange llc|the miami international securities exchange|the miami international securities exchange llc
PEARL:miax pearl|miax pearl llc|the miax pearl|the miax pearl llc
BX:nasdaq bx|nasdaq bx inc|the nasdaq bx|the nasdaq bx inc
GEMX:nasdaq gemx|nasdaq gemx llc|the nasdaq gemx|the nasdaq gemx llc
ISE:nasdaq ise|nasdaq ise llc|the nasdaq ise|the nasdaq ise llc
MRX:nasdaq mrx|nasdaq mrx llc|the nasdaq mrx|the nasdaq mrx llc
Phlx:nasdaq phlx|nasdaq phlx llc
NYSE:new york stock exchange|new york stock exchange llc|nyse|the new york stock exchange|the new york stock exchange llc|the nyse
NYSEAMER:nyse american|nyse american llc|the nyse american|the nyse american llc
NYSEArca:nyse arca|nyse arca inc|the nyse arca|the nyse arca inc
NYSENAT:nyse national|nyse national inc|the nyse national|the nyse national inc
NASDAQ:nasdaq|nasdaq global market|nasdaq global market llc|nasdaq global select market|nasdaq global select market llc|nasdaq stock market|nasdaq stock market llc|the nasdaq|the nasdaq global market|the nasdaq global market llc|the nasdaq global select market|the nasdaq global select market llc|the nasdaq stock market|the nasdaq stock market llc
"""
)


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
exchnameen = _lookup(_EXCHANGES, "exchnameen")

# dei filerCategoryItemType (2024 and later) has only these three values.
_FILER_CATEGORIES = {
    _key(name): name
    for name in ("Large Accelerated Filer", "Accelerated Filer", "Non-accelerated Filer")
}


def entityfilercategoryen(arg: str) -> str:
    text = arg.strip()
    # "Non" must be separated from "accelerated" ("Non-accelerated", "Non accelerated").
    if re.match(r"(?i)non\w", text):
        raise _fail("ixt-sec:entityfilercategoryen")
    category = _FILER_CATEGORIES.get(_key(text))
    if category is None:
        raise _fail("ixt-sec:entityfilercategoryen")
    return category


# --- Numeric transforms ----------------------------------------------------

_UNITS = {
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
    "quadrillion": 10**15,
    "quintillion": 10**18,
}
# Hyphen and dash characters accepted between number words.
_DASHES = "\u058a\u05be\u2010\u2011\u2012\u2013\u2014\u2015\ufe58\ufe63\uff0d-"
_NUMBER_SEPARATORS = re.compile(f"[,\\s{_DASHES}]+")


def _words_to_number(text: str) -> int | None:
    """Parse an English cardinal in words: `twelve thousand`, `one hundred and five`.

    Digits, fractions, and articles ("a hundred") are not number words.
    """
    words = [word for word in _NUMBER_SEPARATORS.split(text.strip().lower()) if word]
    if not words:
        return None
    if words in (["zero"], ["no"], ["none"], ["nil"]):
        return 0
    total = 0
    group = 0  # value below the current scale word, at most 999
    last_scale = None
    previous = ""
    for word in words:
        if word == "and":
            if previous not in {"hundred", *_SCALES}:
                return None
        elif word in _UNITS and word != "zero":
            if group % 100 and not (previous in _TENS and _UNITS[word] < 10):
                return None
            group += _UNITS[word]
        elif word in _TENS:
            if group % 100:
                return None
            group += _TENS[word]
        elif word == "hundred":
            if not 0 < group < 20:
                return None
            group *= 100
        elif word in _SCALES:
            scale = _SCALES[word]
            if group == 0 or (last_scale is not None and scale >= last_scale):
                return None
            total += group * scale
            group, last_scale = 0, scale
        else:
            return None
        previous = word
    if previous == "and":
        return None
    return total + group


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
# 30.4375 days, truncates at each step, and goes no lower than whole days
# (duryear, durmonth, durweek) or whole hours (durday, durhour).
_DAYS_PER_MONTH = Decimal("30.4375")
_DECIMAL_TEXT = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*")


def _number(arg: str, name: str) -> tuple[str, Decimal]:
    if not _DECIMAL_TEXT.fullmatch(arg):
        raise _fail(f"ixt-sec:{name}")
    value = Decimal(arg.strip())
    return ("-" if value < 0 else ""), abs(value)


def _duration(sign: str, parts: list[tuple[int, str]]) -> str:
    """Render `[(value, designator)]`; the first part is always shown, zeros after it aren't.

    Designators are `Y`, `M`, `D`, or `H` (hours go after `T`). A zero duration
    has no sign.
    """
    if all(value == 0 for value, _ in parts):
        sign = ""
    shown = [parts[0], *[(value, unit) for value, unit in parts[1:] if value]]
    date_text = "".join(f"{value}{unit}" for value, unit in shown if unit != "H")
    hours = [value for value, unit in shown if unit == "H"]
    return f"{sign}P{date_text}" + (f"T{hours[0]}H" if hours else "")


def _split(value: Decimal, factor: Decimal) -> tuple[int, Decimal]:
    whole = int(value)
    return whole, (value - whole) * factor


def duryear(arg: str) -> str:
    sign, value = _number(arg, "duryear")
    years, months = _split(value, Decimal(12))
    whole_months, days = _split(months, _DAYS_PER_MONTH)
    return _duration(sign, [(years, "Y"), (whole_months, "M"), (int(days), "D")])


def durmonth(arg: str) -> str:
    sign, value = _number(arg, "durmonth")
    months, days = _split(value, _DAYS_PER_MONTH)
    return _duration(sign, [(months, "M"), (int(days), "D")])


def durweek(arg: str) -> str:
    sign, value = _number(arg, "durweek")
    return _duration(sign, [(int(value * 7), "D")])


def durday(arg: str) -> str:
    sign, value = _number(arg, "durday")
    days, hours = _split(value, Decimal(24))
    return _duration(sign, [(days, "D"), (int(hours), "H")])


def durhour(arg: str) -> str:
    sign, value = _number(arg, "durhour")
    return _duration(sign, [(int(value), "H")])


_DURATION_PARTS = re.compile(
    r"\s*(?:(?P<Y>[^,]+?)\s+years?)?(?:\s*,\s*|\s+and\s+|\s+)?"
    r"(?:(?P<M>[^,]+?)\s+months?)?(?:\s*,\s*|\s+and\s+|\s+)?"
    r"(?:(?P<D>[^,]+?)\s+days?)?\s*",
    re.IGNORECASE,
)


def durwordsen(arg: str) -> str:
    """`two years, six months` -> `P2Y6M`; years, months, and days only."""
    match = _DURATION_PARTS.fullmatch(arg)
    if match is None or not any(match.group(unit) for unit in "YMD"):
        raise _fail("ixt-sec:durwordsen")
    designators = []
    for unit in "YMD":
        text = match.group(unit)
        if text is None:
            continue
        text = text.strip()
        amount = int(text) if text.isdigit() else _words_to_number(text)
        if amount is None:
            raise _fail("ixt-sec:durwordsen")
        if amount:
            designators.append(f"{amount}{unit}")
    return "P" + "".join(designators) if designators else "P0D"


# --- Date transforms -------------------------------------------------------

_QUARTERS = {"1": 3, "first": 3, "2": 6, "second": 6, "3": 9, "third": 9}
_QUARTERS |= {"4": 12, "fourth": 12, "last": 12}
_QUARTER = r"(1|2|3|4|first|second|third|fourth|last)"
# Quarter before year ("Q1 2025", "first quarter of fiscal 2025") or year
# before quarter ("2025 Q1", "2025, third quarter").
_QUARTER_FIRST = re.compile(rf"\s*(?:[^\W\d]\D*?)?{_QUARTER}\D*?(\d{{4}})\s*", re.IGNORECASE)
_YEAR_FIRST = re.compile(rf"\s*(\d{{4}})\D*?{_QUARTER}(?:(?!\d).*\w)?\s*", re.IGNORECASE)


def datequarterend(arg: str) -> str:
    """Calendar end date of the named quarter: `Q1 2025` -> `2025-03-31`."""
    match = _QUARTER_FIRST.fullmatch(arg)
    if match is not None:
        quarter, year = match.group(1), match.group(2)
    else:
        match = _YEAR_FIRST.fullmatch(arg)
        if match is None:
            raise _fail("ixt-sec:datequarterend")
        year, quarter = match.group(1), match.group(2)
    month = _QUARTERS[quarter.lower()]
    day = calendar.monthrange(int(year), month)[1]
    return f"{year}-{month:02d}-{day:02d}"


SEC_TRANSFORMS: dict[str, Callable[[str], str]] = {
    "boolballotbox": boolballotbox,
    "yesnoballotbox": yesnoballotbox,
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
