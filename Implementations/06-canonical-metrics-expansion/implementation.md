# 06 — Expand canonical metrics and make approved mappings usable

Status: implemented and merged to main.

## Problem

`canonical_facts` is empty on the local warehouse because `concept_mappings` has 0
rows. Three things stand between the seed list and real canonical facts:

1. **Too few metrics.** `config/canonical_metrics.yml` defines 13 metrics. The
   research pipeline and the statement work (plans 10–12) need about 45: SBC, D&A,
   buybacks, dividends, debt, leases, working capital, bank metrics and so on.
2. **Approved mappings only match one taxonomy year.**
   `approved_mappings_for_concept` compares `namespace` exactly
   ([postgres_repository.py:39](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/mapping/postgres_repository.py)), and facts carry year-specific namespaces such
   as `http://fasb.org/us-gaap/2025`. Approving `us-gaap:Revenues` would need one
   approval per US-GAAP year (2009–2026) per concept.
3. **No way to apply approvals to filings already loaded.** The `map_canonical`
   handler ([worker.py:283](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/worker.py)) expects every fact inline in the job payload, and
   nothing enqueues it. Approving a mapping today has no effect on loaded filings.

### Before / after

| | Before | After |
|---|---|---|
| Metric catalogue | 13 | about 45 (list in AC-1) |
| Approving `Revenues` → `revenue` | per year-specific namespace | one approval for the `us-gaap` family, every year |
| Bulk-approving the reviewed seed list | one CLI call per concept | `mapping approve-seeds --reviewer … --rationale …` (dry-run first) |
| After approving | nothing happens to loaded filings | `mapping remap` enqueues one `map_canonical` job per published filing |

## Scope

Likely files:

- `config/canonical_metrics.yml` and `config/concept_seed.yml`: new metrics and
  candidates, including `ifrs-full` candidates for core metrics. Each candidate may
  carry `priority` for plan 11's selection when a filing reports several candidate
  concepts for one metric.
- `mapping/postgres_repository.py`: namespace families.
  - A mapping whose `namespace` is a family token (`us-gaap`, `ifrs-full`, `dei`,
    `srt`) matches facts whose namespace is a *standard* namespace URI of that
    family: `http://fasb.org/us-gaap/<date>`,
    `https://xbrl.ifrs.org/taxonomy/<date>/ifrs-full`, `http://xbrl.sec.gov/dei/<year>`,
    `http://fasb.org/srt/<year>`.
  - Specificity order becomes: CIK, then taxonomy version, then exact namespace URI,
    then family.
- New `mapping/seed_approval.py`:
  `approve_seed_candidates(mapper, repository, *, reviewer, rationale, metrics=None, dry_run=False) -> list[SeedApproval]`.
  `SeedApproval` has `metric`, `namespace`, `concept_name` and `created: bool`.
- New `mapping/remap.py`:
  `enqueue_canonical_remap(control, *, epoch, accessions=None) -> int`.
- `worker.py`: `map_canonical` payload becomes `{accession, extraction_revision}`. The
  handler loads facts from the published extraction (silver) instead of the payload.
- `cli.py`: `mapping approve-seeds`, `mapping remap`.
- `docs/CANONICAL_MAPPING.md`, `docs/DATA_MODEL.md`, `docs/INGESTION_RUNBOOK.md`.

Exclusions:

- Choosing between competing concepts for the same metric and period, deriving Q4,
  TTM and formulas (`free_cash_flow`): plan 11.
- Statement-presentation layouts: plan 10.

Invariants (from `sec-xbrl-warehouse/AGENTS.md` and `CANONICAL_MAPPING.md`):

- Seeds are candidates. Nothing becomes approved without an explicit reviewer and
  rationale. `approve-seeds` is a human-triggered command that records both.
- Mappings are append-only and versioned. Approving the same mapping again doesn't
  create a new version.
- Every canonical fact points to its raw fact and mapping version.
- Extension concepts never match a family mapping.

## Acceptance criteria

- **AC-1 Catalogue.** `canonical_metrics.yml` defines at least these codes:
  `revenue, cost_of_revenue, gross_profit, research_and_development,
  selling_general_administrative, operating_income, interest_expense,
  income_tax_expense, net_income, basic_eps, diluted_eps, basic_shares,
  diluted_shares, shares_outstanding, operating_cash_flow, capital_expenditures,
  free_cash_flow, depreciation_amortization, share_based_compensation,
  share_repurchases, dividends_paid, acquisitions, debt_issued, debt_repaid,
  income_taxes_paid, cash_and_equivalents, short_term_investments,
  accounts_receivable, inventory, total_current_assets, goodwill, total_assets,
  accounts_payable, deferred_revenue, total_current_liabilities, current_debt,
  long_term_debt, operating_lease_liabilities, total_liabilities,
  stockholders_equity, dividends_per_share, net_interest_income, deposits,
  loans_net, allowance_for_credit_losses, provision_for_credit_losses`.
  Every non-formula metric has at least one `us-gaap` candidate.
- **AC-2 Seed integrity.**
  - Every candidate names a known metric and a namespace hint in
    `{us-gaap, ifrs-full, dei, srt}`.
  - No concept is a candidate for two metrics.
  - `revenue`, `net_income`, `total_assets` and `operating_cash_flow` have
    `ifrs-full` candidates.
  - Formula metrics have `statement: derived`, reference only known metrics, and
    have no concept candidates.
- **AC-3 Namespace families.**
  - A family mapping (`namespace="us-gaap"`) is returned for facts from any standard
    US-GAAP year namespace, 2009 to the present.
  - It's never returned for extension or look-alike namespaces, or for another
    family.
  - An exact-URI mapping for the same concept outranks the family mapping for that
    URI only.
- **AC-4 Bulk seed approval.** `approve_seed_candidates` approves each selected seed
  candidate as a family mapping, recording the reviewer and rationale.
  - `dry_run=True` writes nothing.
  - A second run creates nothing (`created=False` for each item).
  - `metrics=` limits the run to those metrics.
- **AC-5 Remap.** `enqueue_canonical_remap(control, epoch=…)` enqueues one
  `map_canonical` job per filing with a `latest_extraction_revision`. The payload is
  `{accession, extraction_revision}` with no inline facts. Its idempotency key
  includes the epoch: the same epoch enqueues nothing new, and a new epoch enqueues
  again. Unpublished filings are skipped. The worker handler maps facts loaded from
  that revision's extraction and inserts canonical facts.

## Implementation sequence

1. Metric catalogue and seeds (AC-1, AC-2). Review the US-GAAP concept choices
   against the current taxonomy and the AAPL concepts seen in `xbrl_facts`.
2. Namespace families in the repository, including specificity order (AC-3).
3. `approve_seed_candidates` plus CLI (AC-4).
4. `map_canonical` payload change, handler reading the silver extraction, and
   `enqueue_canonical_remap` plus CLI (AC-5).
5. Docs, Validate suite including integration.
6. Operator steps:
   - `mapping import-metrics`
   - `mapping approve-seeds --dry-run`, then the real run
   - `mapping remap --epoch <date>`
   - `worker run --job-type map_canonical`
   - Confirm `canonical_facts` has rows for AAPL.

## Verification

From `sec-xbrl-warehouse/`, with the local storage services running:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/06-canonical-metrics-expansion/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/06-canonical-metrics-expansion/verification
```

The integration tests reuse `tests/integration/conftest.py`, which creates and drops
its own PostgreSQL database and runs `alembic upgrade head`. Without
`RUN_XBRL_INTEGRATION=1` they're skipped, not passed.

### Baseline (main @ 8f16ca0, 5 October 2026)

7 failed, 5 passed:

- AC-1 fails: 34 metrics are missing.
- AC-2 fails: no `ifrs-full` candidates.
- AC-3 fails: the family mapping isn't found for any year, and the exact-versus-family
  test gets `[]` for the other year.
- AC-4 and AC-5 fail because `seed_approval` and `remap` don't exist yet.
- Five tests pass on baseline and pin invariants that must hold:
  - the existing seeds are unambiguous;
  - `free_cash_flow` is formula-only;
  - extension, look-alike and IFRS namespaces never match a US-GAAP family mapping.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- AC-1/AC-2: 46 metrics (45 reported + `free_cash_flow` formula) and 109 seed
  candidates with `priority` (us-gaap for every reported metric; ifrs-full for 17
  core metrics; dei `EntityCommonStockSharesOutstanding`). `load_rules` rejects
  unknown hints, candidates on formula metrics, and formula metrics not marked
  `derived`; priorities are exposed as `CanonicalMapper.candidate_priorities`.
  Concept choices were checked against AAPL's non-dimensional us-gaap facts.
- AC-3: `mapping/namespaces.py` (strict standard-URI families). Repository matches
  `namespace IN (uri, family)`; specificity CIK > taxonomy version > exact URI > family.
  `resolve_review` accepts a family mapping for a standard-year review.
- AC-4: `mapping/seed_approval.py` + `mapping approve-seeds`. `approve_mapping_once`
  is idempotent when scope, validity, reviewer and rationale match (fixes a bug: with
  NULL cik the old version lookup never matched, so re-approval wrote duplicate v1 rows).
- AC-5: `mapping/remap.py` + `mapping remap`; `map_canonical` payload is
  `{accession, extraction_revision}` and the worker loads the checksum-verified saved
  extraction found via the completed `ingest_filing` job's `extract` checkpoint.
- Pre-existing bug fixed: fact `taxonomy_version` was read from `concept.namespaceURI`,
  the XML Schema namespace of the element declaration (`2001`); now from the fact QName
  namespace. Regression assertion in `tests/unit/test_arelle_adapter.py` (fails on old code).
- Gates: compileall, ruff, format, mypy, unit (329 passed, 1 skipped), scan_stubs (0),
  integration (27 passed), plan tests 01–06 (12/10/8/10/8/12 passed).
