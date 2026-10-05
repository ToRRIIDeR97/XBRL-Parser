# 04 — Pinned local taxonomy packages and offline parsing

Status: plan only. Code follows after this plan is reviewed.

## Problem

Arelle resolves US-GAAP, dei, srt and other standard taxonomies over the network,
through the allowlisted web cache in `data/cache/arelle`.
`config/taxonomy_packages.yml` lists no packages, and `data/taxonomy-packages` is
empty. Consequences:

- **Speed and SEC load.** Each new taxonomy year is fetched during extraction,
  inside the per-filing time budget.
- **Reproducibility.** Results depend on whatever the cache held at the time.
- **Fragility.** The 2 October load logged 588 `webCache:retryingOperation` errors
  fetching `srt-2018-01-31.xsd`, and missing schema definitions
  (`ix11.12.1.2:missingReferences`, `ExplicitMemberUndefinedQNameError`) on 26 of 43
  filings in revision 1.
- **Packages can't be used at all today (found while writing this plan).**
  Registering a valid taxonomy package fails with
  `XbrlExtractionError: Arelle rejected taxonomy package`. Arelle validates
  `META-INF/taxonomyPackage.xml` against a schema that imports
  `http://www.w3.org/2001/03/xml.xsd`. `www.w3.org` isn't an allowed taxonomy host,
  and offline mode blocks it as well. The same blocked fetch
  (`http://www.w3.org/2001/xml.xsd`) causes the `lxml.schemaError` logged on every
  extraction.

### Before / after

| | Before | After |
|---|---|---|
| Registered package, offline mode | rejected | used; no network access |
| Standard taxonomies in production | fetched during extraction | loaded from pinned, checksum-verified local packages |
| Getting packages onto a host | manual, undocumented | `xbrl-warehouse taxonomy sync` |
| Seeing what's missing | nothing | `xbrl-warehouse taxonomy status`, plus a `doctor` check |

## Scope

Likely files:

- New `src/sec_xbrl_warehouse/infrastructure/xbrl/taxonomy_packages.py`, the public
  interface below.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py`: resolve the W3C
  `xml.xsd` schemas (both `2001/xml.xsd` and `2001/03/xml.xsd`) from local copies
  shipped with the application, never from the network. Reuse the registry/status
  code instead of the inline loop in `_create_controller`.
- `src/sec_xbrl_warehouse/cli.py`: `taxonomy status`, `taxonomy sync`, and a
  "Taxonomy packages" `doctor` check.
- `config/taxonomy_packages.yml`: each entry gains `url`. Populate real entries for
  the years the corpus needs: `us-gaap`, `dei`, `srt`, `ecd`, `country`, `currency`,
  `exch`, `naics`, `sic`, `stpr`, plus `ifrs-full` for foreign filers. Checksums are
  recorded from the published files.
- `docs/OPERATIONS.md`, `docs/DEPLOYMENT.md`, `docs/ARELLE_IMPLEMENTATION.md`,
  `docs/SECURITY.md` (new outbound fetch path for packages).

Public interface:

```python
@dataclass(frozen=True)
class TaxonomyPackageStatus:
    package_id: str
    filename: str
    state: Literal["ok", "missing", "checksum_mismatch", "disabled", "unsafe_path"]
    detail: str = ""

def package_status(settings: Settings) -> list[TaxonomyPackageStatus]: ...
async def sync_taxonomy_packages(settings: Settings, http) -> list[TaxonomyPackageStatus]: ...
```

`http` is any object with the `SecHttpClient.download(url, destination, max_bytes=...)`
signature. The CLI passes the real client, so the user agent, rate limit and byte
limits apply.

Exclusions:

- Turning offline mode on by default for development is out of scope.
  `doctor` reports `arelle_offline=false` as a failure only when `env=production`.
- Deciding which years to pin is a data step recorded in the registry, not code.

Invariants:

- Packages are immutable files verified by SHA-256 before every use. A mismatch
  stops extraction (existing behavior in `_create_controller`).
- The taxonomy host allowlist and HTTPS-only policy stay as they are. Package
  downloads use the same policy.
- A failed or partial download never leaves a file under the final package name.

## Acceptance criteria

- **AC-1 Offline extraction through packages.** With a registered, checksum-matching
  package and `arelle_offline=True`, extracting a filing that imports the package's
  schema succeeds with no network access. No issue mentions `xml.xsd`.
- **AC-2 Offline without a package fails fast.** If the taxonomy isn't packaged,
  extraction raises `XbrlExtractionError` naming the missing URL. This is existing
  behavior and must be kept.
- **AC-3 Sync.**
  - `sync_taxonomy_packages` downloads each enabled package that's missing, verifies
    its SHA-256, and moves it into place atomically.
  - A mismatch raises `ArtifactIntegrityError` and leaves no file.
  - Already-verified packages aren't downloaded again.
  - URLs that aren't HTTPS, aren't on `allowed_taxonomy_hosts`, or carry credentials
    raise `ConfigurationError` before any request.
- **AC-4 Status.** `package_status` reports `ok`, `missing`, `checksum_mismatch`,
  `disabled` and `unsafe_path` (a filename escaping the packages directory) without
  network access. `doctor` fails its "Taxonomy packages" check when any enabled
  package isn't `ok`, or when `env=production` and `arelle_offline` is false.
- **AC-5 Pinned registry.** The repository registry lists real `us-gaap-*`, `dei-*`
  and `srt-*` packages (at least), each with an HTTPS `url` and a 64-character
  `sha256`.

## Implementation sequence

1. Ship local copies of the W3C `xml.xsd` schemas and resolve them locally
   (unblocks AC-1).
2. `taxonomy_packages.py`: registry parsing, `package_status`,
   `sync_taxonomy_packages` (AC-3, AC-4).
3. Make `_create_controller` use the shared registry code. Add CLI commands and the
   `doctor` check (AC-4).
4. Populate `config/taxonomy_packages.yml` (AC-5): download each package once,
   record URL and SHA-256, and check the license terms for redistribution. Packages
   aren't committed; only the registry is.
5. Docs and a runbook section on adding a new taxonomy year. Run the Validate suite.
6. Operator step: `taxonomy sync`, set `XBRL_ARELLE_OFFLINE=true`, re-extract one
   AAPL filing, and confirm no `webCache:*` issues.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/04-local-taxonomy-packages/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/04-local-taxonomy-packages/verification
```

`fixtures/fixture-taxonomy-2025.zip` is a minimal standards-shaped taxonomy package
(`META-INF/taxonomyPackage.xml` and `catalog.xml` rewriting
`https://xbrl.fasb.org/fixture-taxonomy/2025/`), built from `fixtures/package-src/`.
Tests compute its checksum at run time, so rebuilding the zip doesn't break them.

Manual checks: `xbrl-warehouse doctor` output with a missing package and with
`XBRL_ENV=production` plus `XBRL_ARELLE_OFFLINE=false`.

### Baseline (main @ 8f16ca0, 5 October 2026)

9 failed, 1 passed:

- AC-1 fails with `Arelle rejected taxonomy package`, the real defect.
- AC-3, AC-4 and AC-5 fail because `taxonomy_packages` doesn't exist and the registry
  is empty.
- AC-2 passes and pins existing behavior.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
