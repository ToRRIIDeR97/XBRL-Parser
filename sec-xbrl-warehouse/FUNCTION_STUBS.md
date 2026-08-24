# Function Stub and Interface Index

This generated index tells the takeover agent which symbols still require implementation or production hardening. The full docstring beside each symbol and `HANDOFF.md` are authoritative for invariants, failure behavior, and acceptance tests.

- Implementation/hardening symbols: **0**
- Protocol/interface methods: **24**
- Regenerate with: `python scripts/generate_stub_index.py`

## Implementation and hardening points

No symbols found.

## Port and protocol contracts

### `src/sec_xbrl_warehouse/domain/ports.py`

- **`ObjectStore.ensure_bucket`** — Interface contract (line 23)
  - Signature: `def ensure_bucket(self) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ObjectStore.exists`** — Interface contract (line 25)
  - Signature: `def exists(self, key: str) -> bool`
  - Contract: No docstring; inspect the surrounding interface.
- **`ObjectStore.put_file`** — Interface contract (line 27)
  - Signature: `def put_file(self, source: Path, key: str, *, expected_sha256: str | None=None, metadata: dict[str, str] | None=None) -> StoredObject`
  - Contract: No docstring; inspect the surrounding interface.
- **`ObjectStore.get_file`** — Interface contract (line 36)
  - Signature: `def get_file(self, key: str, destination: Path) -> StoredObject`
  - Contract: No docstring; inspect the surrounding interface.
- **`ObjectStore.put_bytes`** — Interface contract (line 38)
  - Signature: `def put_bytes(self, data: bytes, key: str, *, metadata: dict[str, str] | None=None) -> StoredObject`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.upsert_source_artifact`** — Interface contract (line 49)
  - Signature: `async def upsert_source_artifact(self, artifact: DatasetArtifact, *, checksum_sha256: str | None=None, object_key: str | None=None) -> UUID`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.mark_artifact_state`** — Interface contract (line 57)
  - Signature: `async def mark_artifact_state(self, artifact_id: UUID, state: ArtifactState, *, details: dict[str, Any] | None=None) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.enqueue_job`** — Interface contract (line 65)
  - Signature: `async def enqueue_job(self, *, job_type: str, idempotency_key: str, payload: dict[str, Any], priority: int=100, max_attempts: int=5) -> UUID`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.claim_job`** — Interface contract (line 75)
  - Signature: `async def claim_job(self, worker_id: str, lock_timeout_seconds: int) -> JobRecord | None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.complete_job`** — Interface contract (line 77)
  - Signature: `async def complete_job(self, job_id: UUID, result: dict[str, Any] | None=None) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.fail_job`** — Interface contract (line 79)
  - Signature: `async def fail_job(self, job_id: UUID, error: str, retryable: bool) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.upsert_filing`** — Interface contract (line 81)
  - Signature: `async def upsert_filing(self, filing: FilingRef) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`ControlRepository.record_validation_issues`** — Interface contract (line 83)
  - Signature: `async def record_validation_issues(self, issues: Sequence[ValidationIssue]) -> None`
  - Contract: No docstring; inspect the surrounding interface.
- **`Warehouse.ping`** — Interface contract (line 88)
  - Signature: `def ping(self) -> bool`
  - Contract: No docstring; inspect the surrounding interface.
- **`Warehouse.insert_sec_batch`** — Interface contract (line 90)
  - Signature: `def insert_sec_batch(self, table: SecTable, batch: Any) -> int`
  - Contract: No docstring; inspect the surrounding interface.
- **`Warehouse.insert_xbrl_bundle`** — Interface contract (line 92)
  - Signature: `def insert_xbrl_bundle(self, bundle: XbrlExtractionBundle) -> dict[str, int]`
  - Contract: No docstring; inspect the surrounding interface.
- **`Warehouse.insert_canonical_facts`** — Interface contract (line 94)
  - Signature: `def insert_canonical_facts(self, facts: Sequence[CanonicalFact]) -> int`
  - Contract: No docstring; inspect the surrounding interface.
- **`Warehouse.query`** — Interface contract (line 96)
  - Signature: `def query(self, sql: str, parameters: dict[str, Any] | None=None) -> list[dict[str, Any]]`
  - Contract: No docstring; inspect the surrounding interface.
- **`XbrlExtractor.extract`** — Interface contract (line 101)
  - Signature: `def extract(self, entrypoint: Path, filing: FilingRef, *, extraction_revision: int=1) -> XbrlExtractionBundle`
  - Contract: No docstring; inspect the surrounding interface.
- **`FilingDiscovery.discover`** — Interface contract (line 112)
  - Signature: `async def discover(self, *args: Any, **kwargs: Any) -> Sequence[FilingRef]`
  - Contract: No docstring; inspect the surrounding interface.
- **`FilingPackageStore.download_package`** — Interface contract (line 117)
  - Signature: `async def download_package(self, filing: FilingRef) -> FilingPackageManifest`
  - Contract: No docstring; inspect the surrounding interface.
- **`BatchWriter.write_batches`** — Interface contract (line 122)
  - Signature: `def write_batches(self, batches: Iterable[Any], *, destination_prefix: str, partition_values: dict[str, str]) -> tuple[StoredObject, ...]`
  - Contract: No docstring; inspect the surrounding interface.
### `src/sec_xbrl_warehouse/mapping/canonical_mapper.py`

- **`MappingRepository.approved_mappings_for_concept`** — Interface contract (line 22)
  - Signature: `async def approved_mappings_for_concept(self, namespace: str, concept_name: str, taxonomy_version: str | None, cik: int, filed_date: date) -> Sequence[ConceptMapping]`
  - Contract: No docstring; inspect the surrounding interface.
- **`MappingRepository.create_review`** — Interface contract (line 31)
  - Signature: `async def create_review(self, *, filing: FilingRef, fact: XbrlFact, candidates: Sequence[str], evidence: dict[str, object]) -> UUID`
  - Contract: No docstring; inspect the surrounding interface.
