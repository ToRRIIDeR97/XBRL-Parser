"""Source-neutral filing keys and entity keys.

A filing key is a bare SEC accession (`0000320193-25-000079`) or
`<source>-<native id>` for any other source (`esef-5493001KJTIIGC8Y1R12-2023-12-31-ESEF-GB-0`).
SEC accessions start with a digit and sources with a letter, so the forms never
overlap; `sec-...` is invalid because SEC filings keep the bare accession. The
`accession` columns and API fields hold filing keys.

An entity key is the SEC CIK for SEC issuers (every CIK is below `ENTITY_KEY_BASE`)
and a number allocated from PostgreSQL `entity_keys` (at or above it) for anything
else. The `cik` columns and API fields hold entity keys.

A key that matches `FILING_KEY_PATTERN` contains only ASCII letters, digits, `.`,
`_` and `-`: no quotes, spaces, slashes or control characters, so code that formats
it into SQL or object paths stays safe.
"""

from __future__ import annotations

import re

SEC_SOURCE = "sec"
SEC_CIK_SCHEME = "http://www.sec.gov/CIK"
ENTITY_KEY_BASE = 10_000_000_000
FILING_KEY_MAX_LENGTH = 128

# ASCII classes only: `\d` also matches non-ASCII digits.
_SEC_ACCESSION = r"[0-9]{10}-[0-9]{2}-[0-9]{6}"
# `[a-z][a-z0-9]{1,15}` except `sec`, spelled out because the Rust regex engine that
# pydantic uses has no look-ahead.
_SOURCE = (
    r"(?:[a-rt-z][a-z0-9]{1,15}|s[a-df-z0-9][a-z0-9]{0,14}|se|se[abd-z0-9][a-z0-9]{0,13}"
    r"|sec[a-z0-9]{1,13})"
)
_NATIVE_ID = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# Shape only: pair it with `FILING_KEY_MAX_LENGTH` (pydantic/FastAPI `max_length`).
FILING_KEY_PATTERN = rf"^(?:{_SEC_ACCESSION}|{_SOURCE}-{_NATIVE_ID})$"
_FILING_KEY = re.compile(FILING_KEY_PATTERN)


def filing_source(key: str) -> str:
    """Return the source of a filing key (`sec` for a bare accession).

    Raises `ValueError` for anything that isn't a valid filing key.
    """
    if (
        not isinstance(key, str)
        or len(key) > FILING_KEY_MAX_LENGTH
        or _FILING_KEY.fullmatch(key) is None
    ):
        raise ValueError(f"Invalid filing key: {key!r}")
    return SEC_SOURCE if key[0].isdigit() else key.split("-", 1)[0]


def is_filing_key(key: str) -> bool:
    try:
        filing_source(key)
    except ValueError:
        return False
    return True
