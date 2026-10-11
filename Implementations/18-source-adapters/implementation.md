# 18 — Source adapters: per-regulator fetch, discovery, and egress

Status: draft for review (11 October 2026). Depends on plan 17 (filing keys, entity
keys, `UnsupportedSourceError`). Second of the multi-exchange plans; plans 19–22 each
add one adapter on top of this one.

## Problem

After plan 17 the warehouse can store a non-SEC filing but can't fetch, discover or
parse one. Every network path is the SEC's:

- `SecHttpClient` refuses every host except `www.sec.gov`, `sec.gov`, `data.sec.gov`,
  sends the SEC user agent, and uses the SEC rate limit.
- `SecEgressLock` (`xbrl:sec-egress`) is the only egress lock; `worker.requires_sec`
  decides whether a worker builds a client at all.
- `discover_filings` calls `SecSubmissionsDiscovery` or `SecDailyIndexDiscovery`;
  `fetch_filing` calls `FilingPackageDownloader`, which now refuses non-SEC filings.
- `parse_filing` fills fiscal fields with `derive_fiscal_focus`, which reads SEC `dei`
  facts only; `taxonomy_packages.required_prefixes` is one US list.

### Before / after

| | Before | After |
|---|---|---|
| `discover_filings {"source": "esef", …}` | ignored `source`, ran SEC discovery | runs the `esef` adapter's discovery (once plan 19 registers it) |
| `fetch_filing` for `esef-…` | `UnsupportedSourceError` | the `esef` adapter downloads the package |
| SEC worker and ESEF worker at once | impossible (one lock, one client) | each holds its own lock (`xbrl:sec-egress`, `xbrl:esef-egress`) |
| `doctor` taxonomy coverage | US prefixes only | US prefixes plus each enabled source's |

## Scope

Contract surface (names the tests use):

- `sec_xbrl_warehouse/sources/base.py`:
  - `SourcePolicy(code, allowed_hosts: frozenset[str], max_requests_per_second,
    max_concurrency, headers: dict[str, str])`.
  - `DiscoveryRequest(filed_from: date | None, filed_to: date | None, entity: str | None,
    forms: tuple[str, ...])`. `entity` is the source's issuer identifier as text
    (SEC: the CIK digits).
  - `DiscoveryResult(filings: tuple[FilingRef, ...], companies: tuple[Company, ...])`.
  - `SourceAdapter` protocol: `code: str`, `taxonomy_prefixes: tuple[str, ...]`,
    `async discover(request) -> DiscoveryResult`, `async download_package(filing) ->
    FilingPackageManifest`, `fiscal_focus(bundle) -> (bundle, issues)`.
- `sec_xbrl_warehouse/sources/registry.py`: `SourceRegistry(adapters)` with `codes`
  (sorted), `adapter(code)`, `adapter_for(filing_key)`, `download_package(filing)`,
  `fiscal_focus(bundle)`. Unknown or disabled codes raise `UnsupportedSourceError`;
  duplicate or malformed adapter codes raise `ValueError` at construction.
- `sec_xbrl_warehouse/sources/sec.py`: `SecSourceAdapter`, wrapping today's SEC
  discovery, `FilingPackageDownloader` and `derive_fiscal_focus` without behavior
  change.
- `infrastructure/http/source_client.py`: `SourceHttpClient(settings, policy, client=None,
  egress_lock=None)` with `get_json`, `get_bytes`, `get_text`, `download` like the SEC
  client: host allowlist (also on redirects), the policy's rate, concurrency and
  headers, byte limits from settings, and the egress lock taken once before the first
  allowed request. `SecHttpClient` keeps its name, `ALLOWED_HOSTS` and behavior; it may
  become a `SourceHttpClient` with the SEC policy.
- `exceptions.py`: `SourceAccessError(XbrlWarehouseError)`; `SecAccessError` becomes its
  subclass (existing `except SecAccessError` sites are unchanged).
- `infrastructure/http/egress_lock.py`: `lock_name(source)`, `SourceEgressLock(settings,
  *, source)`, `EgressBusyError`; `SecEgressLock` is the `sec` lock and still raises
  `SecEgressBusyError` (a subclass of `EgressBusyError`). `LOCK_NAME` stays
  `"xbrl:sec-egress"`.
- `config.py`: `enabled_sources: tuple[str, ...] = ("sec",)` (`XBRL_ENABLED_SOURCES`),
  each a valid source token.
- `infrastructure/xbrl/taxonomy_packages.py`: `required_prefixes` adds
  `source_required_prefixes[<code>]` for each enabled source (scheme-less, like today).
- `worker.py`: services gain `"sources": SourceRegistry`. `discover_filings` reads
  `payload["source"]` (default `sec`), calls that adapter with a `DiscoveryRequest`
  (legacy `cik` payloads become `entity`), upserts returned companies, refuses filings
  whose key belongs to another source (`ValueError`), and enqueues `fetch_filing`
  jobs as today. `runtime_services` builds the registry from `enabled_sources`, a
  client and lazy lock per source that fetch/discover job types need, and passes the
  registry to `IncrementalFilingIngestor` as its package store; parse uses
  `registry.fiscal_focus`. `requires_sec` becomes "requires network for source X".
- Docs: ARCHITECTURE (component, deployment, sequence views and coverage index),
  SECURITY (per-source trust boundary and host allowlists), OPERATIONS (one
  fetch worker per source, `XBRL_ENABLED_SOURCES`), SEC_ACCESS (unchanged SEC rules),
  `.env.example`.

Exclusions:

- No real non-SEC adapter (plans 19–22). No new job types, no ClickHouse or Alembic
  changes. No change to SEC request URLs, headers, rates, retries or limits.
- Bulk SEC datasets (`download_bulk`, `load_bulk`) stay SEC-only and keep using the
  SEC client directly.

Invariants:

- SEC behavior is identical: same requests in the same order, same user agent, same
  rate and concurrency, same lock name, same object keys, same job keys and payloads.
  Existing tests pass unchanged.
- A source's client can never reach another source's hosts; one process per source
  sends traffic for it.
- An unknown or disabled source is never retried and never falls back to SEC.

![Current single SEC path through runtime services, discovery, fetch and parse, against the proposed source registry, SEC adapter, per-source client and lock, and source-aware worker handlers, linked to AC-1 to AC-6](diagrams/source-adapters.svg)

Feature map: `cli.py` (`filing discover`, `worker run`, `doctor`, `taxonomy`) →
`worker.runtime_services` (clients, locks, services) → `build_handlers`
(`_discover`, `_fetch_filing`, `_ingest_filing`, `_parse_filing`) →
`IncrementalFilingIngestor` (`fetch` → `package_store.download_package`, `parse` →
`derive_fiscal_focus`) → `FilingPackageDownloader`, `SecSubmissionsDiscovery`,
`SecDailyIndexDiscovery` → `SecHttpClient` → `SecEgressLock`. Taxonomy:
`taxonomy_packages.required_prefixes` → `coverage_gaps` → `doctor` and offline
extraction refusal.

## Acceptance criteria

- **AC-1 Registry.** `SourceRegistry` routes by code and by filing key, dispatches
  `download_package` and `fiscal_focus` to the filing's adapter, raises
  `UnsupportedSourceError` for unknown/disabled sources, and rejects duplicate or
  malformed codes. `Settings.enabled_sources` defaults to `("sec",)` and validates
  tokens.
- **AC-2 Source client.** `SourceHttpClient` only requests hosts in its policy (also
  after redirects, which it doesn't follow off-policy), sends the policy's headers,
  holds the policy's rate, and takes its egress lock once, on the first allowed
  request. `SecHttpClient.ALLOWED_HOSTS` is unchanged and `SecAccessError` is a
  `SourceAccessError`.
- **AC-3 Locks.** `lock_name("sec") == LOCK_NAME == "xbrl:sec-egress"`; other sources
  get `xbrl:<code>-egress`. Locks of different sources don't block each other; a
  second holder of the same source gets `EgressBusyError` (`SecEgressBusyError` for
  SEC); a released lock can be retaken.
- **AC-4 Discovery jobs.** `discover_filings` uses `payload["source"]` (default `sec`;
  `cik` becomes `entity`), stores returned companies, enqueues `fetch:<key>:<revision>`
  jobs, rejects filings from another source, and fails without retry for a disabled
  source.
- **AC-5 SEC through the adapter.** Fetch, discovery and parse for SEC run through
  `SecSourceAdapter` with identical behavior (existing SEC worker, downloader,
  discovery and fiscal-focus tests pass unchanged; reviewed in the diff).
- **AC-6 Taxonomy prefixes.** `required_prefixes` is the US list plus
  `source_required_prefixes` for enabled sources only.
- **AC-7 Docs (manual).** The listed documents and diagrams describe the registry,
  per-source clients and locks, and how to run one fetch worker per source.
- **AC-8 Regression (suite).** All Validate gates pass.

| Criterion | Acceptance (visible) | Verification (held back) |
|---|---|---|
| AC-1 | routing, dispatch, disabled source, settings | duplicate/malformed codes, fiscal-focus dispatch |
| AC-2 | allowlist, headers, SEC client unchanged | lock once and not on refusal, redirect refusal, rate |
| AC-3 | names, independent sources | SEC error type, release and retake |
| AC-4 | named source, fetch keys, disabled source | default SEC + `cik`, companies stored, foreign keys rejected |
| AC-5 | suite | suite + review |
| AC-6 | enabled-only union | missing source entry, scheme stripping |
| AC-7 | manual | manual |
| AC-8 | gates | gates |

## Implementation sequence

1. `sources/base.py`, `exceptions.py`, `config.enabled_sources` (AC-1 part).
2. `SourceHttpClient`, `SourceEgressLock`, SEC client and lock on top of them (AC-2,
   AC-3).
3. `SourceRegistry`, `SecSourceAdapter` (AC-1, AC-5).
4. Worker wiring, ingestor package store, parse fiscal focus (AC-4, AC-5).
5. Taxonomy prefixes, `doctor` (AC-6). Docs (AC-7). Validate (AC-8).

## Verification

From `sec-xbrl-warehouse/` with the storage containers running and `.env` present:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/18-source-adapters/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/18-source-adapters/verification
```

The verification set imports the acceptance set's `demo_source.py` fake adapter.

### Baseline (plan 17 candidate 131a87f, 11 October 2026)

- Acceptance: 9 failed (`sec_xbrl_warehouse.sources` missing, `SourceAccessError`,
  `lock_name`, `EgressBusyError` missing, `Settings.enabled_sources` missing,
  `source_required_prefixes` ignored).
- Verification: 8 failed for the same reasons; 1 passed
  (`test_ac6_prefix_edges`: with no prefix entry for an enabled source the US list is
  already returned unchanged, which is correct behavior today).

## Completion evidence

The plan author (coordinating session) wrote both test sets. The implementer is told
not to read `verification/`; that boundary is an instruction, not enforced.

### Implementer record

### Verifier record
