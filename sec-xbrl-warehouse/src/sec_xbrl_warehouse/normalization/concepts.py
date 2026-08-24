from __future__ import annotations

from urllib.parse import urlparse


def concept_key(namespace: str, concept_name: str, taxonomy_version: str | None = None) -> str:
    return f"{namespace}|{concept_name}|{taxonomy_version or ''}"


def namespace_family(namespace: str) -> str:
    """Return a stable hint such as `us-gaap`, `dei`, `ifrs-full`, or extension.

    This helper is for candidate rules and display only. Namespace URI remains the
    identity and callers must not map concepts solely from this hint.
    """
    lowered = namespace.lower()
    for family in ("us-gaap", "dei", "ifrs-full", "srt", "country"):
        if family in lowered:
            return family
    host = urlparse(namespace).hostname
    return f"extension:{host}" if host else "extension"


def split_qname(qname: str) -> tuple[str | None, str]:
    """Split a display QName without pretending its prefix is a namespace URI."""
    if ":" not in qname:
        return None, qname
    prefix, local = qname.split(":", 1)
    return prefix or None, local
