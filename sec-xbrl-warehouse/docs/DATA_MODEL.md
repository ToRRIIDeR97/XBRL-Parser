# Data Model

## Source fidelity principle

The raw layer stores what was filed or what the SEC bulk set published. The canonical layer stores an interpretation. These are separate records with explicit lineage.

## SEC FSNDS tables

The bulk loader expects eight tab-delimited tables.

TSV fields use CSV quoting: quoted tabs, escaped double quotes, and quoted
newlines are field content. The reader bounds bytes per logical record and
counts only unquoted separators before allocating CSV fields. Checkpoints
retain the physical source line where each batch begins.

### SUB — submissions

Primary key: `adsh`.

Contains accession, CIK, registrant identity/address, filer status, form, fiscal metadata, filing/acceptance dates, instance filename, co-registrant metadata, and public-float fields.

### TAG — taxonomy tags

Compound key: `(tag, version)`.

Contains standard and extension tag metadata: custom/abstract flags, datatype, instant-or-duration indicator, credit/debit balance, label, and documentation.

### DIM — dimensions

Primary key: `dimh`.

Contains the SEC-provided hash and lexical representation of axis/member pairs. `dimh = 0` represents no XBRL dimensions.

### NUM — numeric facts

Compound key: `(adsh, tag, version, ddate, qtrs, uom, dimh, iprx)`.

Preserve both the raw decimal text and a parsed decimal value. Never use binary floating point as the only representation.

NUM and TXT `ddate` remain typed dates in Arrow/Parquet. ClickHouse stores their
ISO date strings so published years outside its native Date/Date32 ranges are
retained without clamping. The adapter converts dates to strings only on the
ClickHouse transport boundary. Invalid calendar dates still fail parsing;
unusual but parseable dates are preserved, not corrected to an assumed year.

### TXT — text facts

Compound key: `(adsh, tag, version, ddate, qtrs, dimh, iprx, _row_hash)`.

The source-row hash preserves distinct contexts and other published text values
that share the SEC business fields. Replaying the same row remains idempotent.

Contains normalized plain text and metadata including language, source length, truncation information, context reference, and footnotes.

### REN — rendering groups

Compound key: `(adsh, report)`.

Describes SEC rendering/menu groups and role hierarchy.

### PRE — presentation lines

Compound key: `(adsh, report, line)`.

Describes statement location, order, indentation/parenthetical status, preferred labels, and negation behavior.

### CAL — calculation relationships

Compound key: `(adsh, grp, arc)`.

Describes parent-child calculation arcs and sign/weight direction.

Preserve the published `negative` integer: current signed weights (-1/1) and
older 0/1 encodings remain distinct source values, not normalized booleans.
Arrow and ClickHouse use signed Int16 so upgrades preserve the full former
UInt8 range. Parsing accepts only -1, 0, or 1. Interpret direction using the
source encoding; do not treat both nonzero values as a negative arc.

## Full-fidelity Arelle entities

### `xbrl_facts`

Required identity and lineage:

- `fact_id`: deterministic hash of accession, source document, inline ID or fact path, concept, context, unit, and duplicate ordinal.
- accession and filing metadata.
- concept namespace, local name, QName, taxonomy version.
- context and unit IDs.
- raw lexical value.
- typed decimal/text/date/boolean value where applicable.
- nil, decimals, precision, language.
- period start/end/instant.
- entity scheme and identifier.
- dimension JSON plus stable context/dimension hashes.
- footnote IDs and tuple ancestry.
- source document, source fact ID, extraction revision.

### `xbrl_contexts`

One row per context per extraction revision. Includes entity, period, dimensional scenario/segment content, and a canonical context hash.

### `xbrl_concepts`

One row per QName/taxonomy version used by the filing or loaded taxonomy. Includes datatype, substitution group, period type, balance, abstract/nillable flags, labels, documentation, and extension status.

### `xbrl_relationships`

One row per arc. Includes network type, link role, arc role, source/target QName, order, priority, weight, preferred label, and dimensional flags.

## Canonical layer

### `canonical_metrics` in PostgreSQL

Defines the stable metric code, expected statement, period type, unit family, and optional derived formula.

### `concept_mappings` in PostgreSQL

Append-only/versioned mapping decisions. A mapping can be:

- exact taxonomy version + QName;
- namespace + local name with validity range;
- filing-specific extension mapping;
- rule-based candidate requiring review.

### `canonical_facts` in ClickHouse

Contains:

- raw fact ID;
- canonical metric code;
- normalized decimal/text value;
- normalized unit and sign;
- mapping ID/version/confidence/explanation;
- dimensional scope;
- source and extraction revision.

## Row hashing

Every silver/raw warehouse row should have a deterministic hash over normalized field serialization. Hashing excludes ingest timestamps but includes all source-controlled values. This supports replay verification and source-revision comparison.
