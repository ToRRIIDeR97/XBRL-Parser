"""Standard taxonomy namespace families used by concept mappings."""

from __future__ import annotations

import re

# Standard namespace URIs per family. Extension and look-alike namespaces never match.
_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("us-gaap", re.compile(r"^http://fasb\.org/us-gaap/\d{4}(?:-\d{2}-\d{2})?$")),
    ("srt", re.compile(r"^http://fasb\.org/srt/\d{4}(?:-\d{2}-\d{2})?$")),
    ("dei", re.compile(r"^http://xbrl\.sec\.gov/dei/\d{4}(?:-\d{2}-\d{2})?$")),
    ("ifrs-full", re.compile(r"^https?://xbrl\.ifrs\.org/taxonomy/\d{4}-\d{2}-\d{2}/ifrs-full$")),
)
FAMILY_TOKENS = frozenset(name for name, _ in _FAMILIES)


def namespace_family(namespace: str) -> str | None:
    """Family token (`us-gaap`, `srt`, `dei`, `ifrs-full`) of a standard namespace URI."""
    for name, pattern in _FAMILIES:
        if pattern.match(namespace):
            return name
    return None
