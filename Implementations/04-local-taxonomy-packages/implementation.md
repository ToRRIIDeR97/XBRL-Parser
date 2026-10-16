# 04 — Pinned local taxonomy packages and offline parsing

Status: implemented and merged to main.

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

### Implementer record (5 October 2026)

- AC-1: W3C `xml.xsd` (2001 and 2001/03) resolve to copies shipped in
  `infrastructure/xbrl/schemas/`, so taxonomy packages load offline.
- AC-3/AC-4: new `infrastructure/xbrl/taxonomy_packages.py` with `package_status` and
  `sync_taxonomy_packages`. The registry is read with a YAML loader that keeps `off` and
  digit-only checksums as strings. Sync uses a temp file in the packages directory and
  an atomic rename. CLI `taxonomy status|sync`; the `doctor` check is "Taxonomy
  packages". `SecHttpClient(allowed_hosts=...)` lets sync use the taxonomy allowlist.
- `_create_controller` uses the shared registry code. A checksum mismatch or unsafe
  filename is always fatal. A missing package is fatal when offline or in production;
  elsewhere one `warehouse:taxonomyPackageMissing` warning is recorded (so dev and
  tests keep working before `taxonomy sync`).
- AC-5: 48 official packages pinned (URL, SHA-256, size) from xbrl.fasb.org and
  xbrl.sec.gov. 19 are enabled: us-gaap 2017–2026 and srt 2018–2026, the zips in
  Taxonomy Package format. Pinned but disabled: us-gaap 2015/2016 and every SEC
  per-family zip (dei 2014/2018–2020/2024, country, currency, exch, naics, sic, stpr),
  which have no `META-INF/taxonomyPackage.xml`; and SEC combined packages 2021–2023
  (all rewrite `https://xbrl.sec.gov/`; Arelle keeps one per prefix) and 2024–2026
  (not Taxonomy Package format). `ifrs-full` is not pinned: its zips are on
  www.ifrs.org, which isn't an allowed host.
- `xbrl.sec.gov` added to the default taxonomy host allowlist (SECURITY.md).
- Gates: compileall, ruff, format, mypy, unit (310 passed), scan_stubs (0),
  integration (24 passed), plan tests 01/02/03/04 (12/10/8/10 passed). Live
  `taxonomy sync` of srt-2026 from xbrl.fasb.org into a scratch directory verified.
- Not done: operator `taxonomy sync` on the live host, offline re-extraction of AAPL.

### Follow-up (verifier, 5 October 2026)

- Registry entry kinds: `package` (Arelle catalog), `archive` (official zip, explicit
  URI prefix -> directory in the zip), `file` (single published schema). Files stay
  byte-identical. Verified `archive`/`file` mappings and http/https twins of every
  prefix are added to Arelle's package remappings in `_create_controller`.
- 50 pinned sources, all enabled: us-gaap 2015–2026, srt 2018–2026, SEC per-family
  zips (country, currency, dei, exch, naics, sic, stpr up to 2020), SEC combined zips
  2021–2026 mapped per family/year (dei 2021–2026, ecd 2023–2026, cyd 2025–2026,
  and others), and single files for exch 2018, stpr 2018, invest 2013.
- `required_prefixes` (92 family/years) plus a coverage check: `taxonomy status`
  reports `uncovered` rows, `doctor` fails on them, and offline/production extraction
  refuses to start.
- Tests: fixture-zip archive mapping offline (http form resolved via the https
  mapping), coverage, invalid mappings, and a real dei 2024 offline extraction through
  `sec-2024.zip` (skips with instructions when the zip isn't installed; passed with
  the scratch download).
