# ADR-003: Arelle as XBRL Semantic Source of Truth

## Status

Accepted.

## Decision

Use Arelle for XBRL/iXBRL document-set loading, contexts, units, facts, taxonomy concepts, relationships, and validation. EdgarTools remains an optional convenience adapter.

## Consequences

- Higher standards fidelity than custom parsing.
- Arelle API changes are isolated behind one adapter.
- Tests must pin structural expectations across Arelle upgrades.
- Filing packages must be retained intact so taxonomy and document references resolve.
