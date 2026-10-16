# 13 — Cut object-store usage: compressed extraction checkpoints, one bronze archive per filing

Status: implemented and merged to main. Pairs well with plan 01,
which stops downloading unneeded files.

## Problem

Measured in MinIO for 43 AAPL filings (5 October 2026), out of 1.3 GB total:

| Prefix | Size | Why |
|---|---|---|
| `silver/xbrl/extractions/` | **866 MB** (92% of silver) | Uncompressed extraction JSON per revision. The same data is also written as compressed Parquet (`silver/xbrl/{entity}`, about 80 MB). |
| `bronze/sec/filings/` | 356 MB | Every member is stored loose (about 9.9 MB for one 2017 10-Q) **and** inside the deterministic package zip (560 KB for the same filing). |

The loose copies exist because `download_package` stores each file under
`filing_object_key` ([filing_downloader.py:85](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/ingestion/filing_downloader.py)), and `materialize_package` reads
them back from there. The zip is created later by `archive_package` in the ingestor.

### Before / after (per filing, about)

| | Before | After |
|---|---|---|
| Extraction checkpoint | 20 MB `.json` | about 1–2 MB `.json.zst` (at least 4× smaller) |
| Bronze | about 10 MB loose plus 0.6 MB zip | 0.6 MB zip (lossless; per-member checksums in the manifest) |
| Projected for 20k filings | about 600 GB of object storage | about 60–80 GB |

## Scope

Likely files:

- `infrastructure/xbrl/extraction_io.py`: streaming zstd write and read. Use
  pyarrow's `CompressedOutputStream`/`CompressedInputStream`, which are already a
  dependency; `zstandard` isn't installed. Byte limits apply to the decompressed
  stream. Reading still accepts legacy `.json`.
- `ingestion/incremental.py`: write `…/sha256=<hash>.json.zst` (hash of the
  compressed object) and read either form when resuming.
- `ingestion/filing_downloader.py`:
  - `download_package` downloads to staging, builds the deterministic archive
    (existing `archive_package` logic: sorted names, fixed timestamps, manifest
    inside), and stores **only** the archive;
  - the returned manifest has `package_object_key`, `package_checksum_sha256`, and
    each member's checksum and size;
  - `materialize_package` extracts from the archive with the existing zip safety
    checks and verifies each member's SHA-256 against the manifest;
  - manifests without `package_object_key` (legacy) still read loose members.
- `docs/ARCHITECTURE.md` (bronze/silver), `docs/BACKUP_RESTORE.md`, `docs/OPERATIONS.md`.

Exclusions:

- **Deleting existing loose bronze objects or old JSON checkpoints.** Hard
  constraint 7 makes retention an explicit operational decision. The plan provides a
  dry-run inventory command (`storage report`) that lists reclaimable objects and
  bytes. Deletion stays a separate, operator-approved step.
- ClickHouse column slimming is plan 14.

Invariants:

- Raw bytes are lossless and immutable. The archive is deterministic, so identical
  inputs give the same key and checksum, and each member's SHA-256 is recorded.
- Resume semantics stay the same: a saved extraction checkpoint is reused, and Arelle
  isn't re-run.
- Bounded memory: compression and decompression stream, with no whole-bundle buffers.

## Acceptance criteria

- **AC-1 Compressed checkpoint.** The extraction checkpoint object key ends with
  `.json.zst`. For a 500-fact bundle it's at most a quarter of the plain JSON size.
  Resuming after a failure reads it without calling the extractor again.
- **AC-2 One archive.** After `download_package`, the package archive exists under
  `package_object_key`, and no member exists under its loose `filing_object_key`.
  `materialize_package` reproduces each member byte for byte. Two downloads of
  identical bytes give the same archive key and checksum.
- **AC-3 Backward compatibility.** Legacy manifests (loose members, no archive) and
  legacy `.json` checkpoints still work.
- **AC-4 Tamper detection.** An archive whose member bytes don't match the manifest
  checksums raises `ArtifactIntegrityError` on materialization.

## Implementation sequence

1. Compressed checkpoint I/O plus legacy reading (AC-1, AC-3).
2. Archive-at-download, materialize from the archive, legacy fallback (AC-2 to AC-4).
   The ingestor's `archive_package` call becomes a no-op for archived manifests.
3. `storage report` inventory (dry run only). Docs. Validate suite including
   integration (`test_s3_object_store.py` against MinIO).
4. Operator follow-up, needing a separate approval: decide retention for legacy loose
   bronze members and JSON checkpoints using the report.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/13-object-store-slimming/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/13-object-store-slimming/verification
```

After merge, re-measure with `du -sh /data/sec-xbrl/*` inside the MinIO container
after re-ingesting a few filings.

### Baseline (main @ 8f16ca0, 5 October 2026)

4 failed, 2 passed:

- AC-1: the key ends in `.json`.
- AC-2: no `package_object_key` after download.
- AC-4: there's no archive to tamper with.
- Determinism: `package_object_key` is `None`.
- Both AC-3 legacy tests pass on baseline and pin compatibility.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- `extraction_io.py`: `write_json` writes a zstd stream (pyarrow
  `CompressedOutputStream`, 1 MiB buffered writes) for `.zst` paths; `read_bundle`
  detects zstd by magic bytes and decompresses in 1 MiB chunks, enforcing
  `extraction_max_bytes` on the uncompressed size. Legacy `.json` reads unchanged. The
  ingestor writes `…/sha256=<hash of the compressed object>.json.zst`; remap reads
  either form. A 500-fact bundle: 551,561 B plain → 38,769 B compressed.
- `filing_downloader.py`: members download to staging only; `download_package`
  stores one deterministic archive (embedded `manifest.json` = filing + members only,
  so re-downloads of identical bytes share key and checksum). The `-xbrl.zip` fallback
  archives the source zip plus its expanded members (`source_archive` kept); a
  rejected `-xbrl.zip` is still stored loose for diagnosis. `materialize_package`
  extracts from the archive: archive SHA-256, `validate_zip_integrity`, no
  non-manifest entries, per-member SHA-256 and size. Cached manifests with an archive
  are reused when the archive exists; `FilingSummary.xml` is read from it. Legacy
  loose manifests materialize as before and the ingestor still archives them.
  Plan 15 can call `download_package` (fetch) and `materialize_package` (parse)
  separately: the manifest alone locates the bytes.
- `storage report`: `list_objects` on both stores and
  `infrastructure/storage/inventory.py`; dry-run only (lists, never deletes).
- Docs: ARCHITECTURE (bronze/silver), BACKUP_RESTORE (scope), OPERATIONS (inventory
  and retention caveat).
- Tests: `tests/unit/test_object_store_slimming.py` (fallback archive, archive/manifest
  mismatches, report and CLI) and a MinIO test in `test_s3_object_store.py` (temporary
  bucket).
- Operator follow-up (separate approval): run `storage report` and decide retention for
  legacy loose members and JSON checkpoints.

### Follow-up (verifier, 5 October 2026)

- `delete_source_files` is removed from `FilingPackageDownloader` and the
  `FilingPackageStore` port (it was unused and would have deleted the package archive,
  every raw byte of a filing). The application has no raw-artifact delete path.
  Plan-authored test fakes that still define the method are unaffected.
- `read_bundle` validates the decompressed `bytearray` directly (no extra full copy).
