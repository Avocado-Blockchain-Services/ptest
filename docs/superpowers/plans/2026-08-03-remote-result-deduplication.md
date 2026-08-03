# ptest Remote Result Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make identical remote `ptest` calls share an in-flight Cloud Run execution and reuse its passing result for exactly one hour.

**Architecture:** The standalone dispatcher derives one canonical source manifest for packing and hashing, coordinates owners and joiners through generation-guarded GCS JSON objects, and submits Cloud Run Jobs asynchronously so joiners can discover running executions. Remote test code remains read-only; authenticated local operators own coordination state.

**Tech Stack:** Python 3.11+ standard library, `gcloud storage`, Cloud Run Jobs v2, GCS generation preconditions, pytest through `ptest`.

## Global Constraints

- Run tests through `ptest`; never invoke pytest directly.
- Passing results are fresh only while `now < completed_at + 3600`.
- Never cache failures, infrastructure errors, interruptions, exit 2, or exit 75.
- Cache hits and joiners bypass budget and concurrency checks; only owners consume them.
- Preserve local caps, fallbacks, dotenv stripping, worktrees, summaries, and exit codes 0/1/2/75.
- Do not create warm compute or grant remote test code GCS write access.
- Local timings are correctness-only.

---

### Task 1: Canonical shipped-source identity

**Files:**
- Modify: `ptest`
- Create: `tests/test_source_identity.py`

**Interfaces:**
- Produces `source_manifest(root: Path) -> list[SourceEntry]`.
- Produces `tree_digest(entries: Sequence[SourceEntry]) -> str`.
- Changes `pack_tree` to consume the same entries.

- [ ] **Step 1: Write failing source-identity tests**

Cover identical bytes under different worktree paths/mtimes, tracked edits, untracked files, ignored lockfiles, staged deletion, executable-bit changes, symlinks, secret dotenv exclusion, and non-Git fallback exclusions:

```python
assert ptest.tree_digest(ptest.source_manifest(repo_a)) == ptest.tree_digest(
    ptest.source_manifest(repo_b)
)
```

- [ ] **Step 2: Run red test**

Run: `ptest tests/test_source_identity.py`

Expected: FAIL on missing interfaces.

- [ ] **Step 3: Implement one ordered manifest and framed SHA-256 digest**

Hash normalized path, kind, executable bit, size, bytes/link target with length framing. Feed the exact entries to `pack_tree`; preserve symlinks and skip vanished staged paths.

- [ ] **Step 4: Verify and commit**

Run: `ptest tests/test_source_identity.py tests/test_routing.py`

Expected: PASS.

```bash
git add ptest tests/test_source_identity.py
git commit -m "feat: derive remote identity from shipped source"
```

### Task 2: Request keys and record freshness

**Files:**
- Modify: `ptest`
- Modify: `config.example.toml`
- Create: `tests/test_remote_cache_records.py`

**Interfaces:**
- Consumes Task 1 tree digest.
- Produces `remote_request_key(fields: Mapping[str, str]) -> str`.
- Produces `passing_record_is_fresh(record: dict, now: datetime) -> bool`.
- Produces `coordination_paths(key, tree_digest) -> CoordinationPaths`.

- [ ] **Step 1: Write failing key/freshness tests**

Assert every identity field changes the key; assert freshness at 3599.999 seconds and expiry exactly at 3600. Reject malformed schema/timestamps and non-success records.

- [ ] **Step 2: Run red test**

Run: `ptest tests/test_remote_cache_records.py`

Expected: FAIL.

- [ ] **Step 3: Implement canonical compact JSON hashing and config**

```python
payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
return hashlib.sha256(payload).hexdigest()
```

Add `remote_cache_ttl_seconds = 3600`, `remote_claim_ttl_seconds = 2100`, and `runner_namespace = "v1"`; validate positive TTLs and namespace syntax.

- [ ] **Step 4: Verify and commit**

Run: `ptest tests/test_remote_cache_records.py tests/test_source_identity.py`

```bash
git add ptest config.example.toml tests/test_remote_cache_records.py
git commit -m "feat: define remote request cache identity"
```

### Task 3: Atomic GCS coordination adapter

**Files:**
- Modify: `ptest`
- Create: `tests/test_gcs_coordination.py`

**Interfaces:**
- Produces `GcsCoordination.read_json/create_json/replace_json/delete_if_generation/object_exists`.
- Reads return `StoredJson(value, generation)`; create-only collision is distinct from outage.

- [ ] **Step 1: Write failing subprocess-contract tests**

Pin create with `--if-generation-match=0`, describe/read, generation-safe replacement/deletion, 404, 412, malformed JSON, and transient failures. All subprocess calls use argument arrays, never shell strings.

- [ ] **Step 2: Run red test**

Run: `ptest tests/test_gcs_coordination.py`

- [ ] **Step 3: Implement adapter using explicit-account `gcloud_base`**

Write temporary JSON with standard library APIs. Classify only known not-found and precondition failures; raise `CoordinationUnavailable` for ambiguity. Never print payload contents.

- [ ] **Step 4: Verify and commit**

Run: `ptest tests/test_gcs_coordination.py tests/test_source_identity.py`

```bash
git add ptest tests/test_gcs_coordination.py
git commit -m "feat: add atomic GCS coordination primitives"
```

### Task 4: Asynchronous Cloud Run lifecycle

**Files:**
- Modify: `ptest`
- Create: `tests/test_cloudrun_lifecycle.py`

**Interfaces:**
- Produces `submit_execution(base: list[str], job: str, region: str, source_uri: str, command: str) -> str`.
- Produces `describe_execution(base: list[str], execution: str, region: str) -> ExecutionState`.
- Produces `wait_for_execution(base: list[str], execution: str, region: str) -> ExecutionResult` with name, terminal category, logs, summary, and exit code.

- [ ] **Step 1: Write failing lifecycle tests**

Cover async submission/name parsing, reconciling-to-terminal polling, success, test failure, infrastructure failure, missing logs, and transient describe retry.

- [ ] **Step 2: Run red test**

Run: `ptest tests/test_cloudrun_lifecycle.py`

- [ ] **Step 3: Extract lifecycle from `run_cloudrun`**

Submit asynchronously, poll with bounded backoff no longer than ten seconds, reuse log fetching/summarization, and preserve failure versus fallback classification.

- [ ] **Step 4: Verify and commit**

Run: `ptest tests/test_cloudrun_lifecycle.py tests/test_main_wiring.py`

```bash
git add ptest tests/test_cloudrun_lifecycle.py
git commit -m "refactor: expose asynchronous Cloud Run lifecycle"
```

### Task 5: Owner/joiner/reuse state machine

**Files:**
- Modify: `ptest`
- Create: `tests/test_remote_deduplication.py`

**Interfaces:**
- Consumes Tasks 1–4.
- Changes `run_cloudrun` to implement fresh-pass hit, live join, atomic ownership, stale recovery, and safe uncached fallback.

- [ ] **Step 1: Write state-machine tests with fake clock and in-memory coordination**

Cover one submission for two logical callers, one owner-only budget/concurrency call, fresh hit bypass, TTL boundary, claim-to-execution race, stale claim with/without live execution, owner disappearance after submit, shared red result, forbidden cache outcomes, and generation-safe cleanup.

- [ ] **Step 2: Run red test**

Run: `ptest tests/test_remote_deduplication.py`

- [ ] **Step 3: Implement exact operation ordering**

Manifest/digest → fresh pass → live execution → atomic claim → owner-only budget/concurrency → content-addressed upload → async submit → execution manifest → wait → pass record on exit 0 only.

- [ ] **Step 4: Preserve optimization-only fallback**

On coordination outage, warn once and use the existing uncached remote path with its concurrency ceiling. Never turn it into an uncapped local full run.

- [ ] **Step 5: Add an explicit benchmark bypass**

Reserve `--fresh` as a ptest-only remote flag. It adds a random nonce to request identity, bypasses pass lookup/write, still obeys budget/concurrency, and never reaches the underlying runner. Unit tests prove ordinary invocations cannot bypass caching accidentally.

- [ ] **Step 6: Verify and commit**

Run: `ptest tests/test_remote_deduplication.py tests/test_main_wiring.py tests/test_routing.py tests/test_cloudrun_lifecycle.py`

```bash
git add ptest tests/test_remote_deduplication.py
git commit -m "feat: coalesce identical remote test executions"
```

### Task 6: Diagnostics and batching documentation

**Files:**
- Modify: `ptest`
- Modify: `README.md`
- Modify: `config.example.toml`
- Modify: `outsource_tests.md`
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- Adds cache state/namespace/TTL to `where` and health classification to `doctor`.

- [ ] **Step 1: Write failing CLI-output tests**

Assert enabled, disabled, healthy, and unavailable states without credential leakage.

- [ ] **Step 2: Run red tests**

Run: `ptest tests/test_main_wiring.py -k 'cache or doctor or where'`

- [ ] **Step 3: Implement diagnostics and docs**

Document scoped TDD, one final coverage-enabled `ptest --full`, in-flight joining, one-hour passing reuse, namespace bumps, and no warm compute.

- [ ] **Step 4: Verify and commit**

Run: `ptest tests/test_main_wiring.py tests/test_remote_cache_records.py`

```bash
git add ptest README.md config.example.toml outsource_tests.md tests/test_main_wiring.py
git commit -m "docs: explain remote result reuse and batching"
```

### Task 7: Install and live concurrency smoke

**Files:**
- Modify: `smoke.sh`
- Modify: `README.md`

- [ ] **Step 1: Add fixture-tested smoke parsing**

Make one-execution and cache-hit assertions testable without GCP, then run their focused tests through `ptest`.

- [ ] **Step 2: Add opt-in live smoke**

Two processes request the same small remote command; collect both exit codes and count executions by emitted request key. A third request must report a passing cache hit.

- [ ] **Step 3: Verify all unit tests**

Run: `ptest tests/test_source_identity.py tests/test_remote_cache_records.py tests/test_gcs_coordination.py tests/test_cloudrun_lifecycle.py tests/test_remote_deduplication.py tests/test_main_wiring.py tests/test_routing.py`

- [ ] **Step 4: Install safely and run live smoke once**

Compare repository and installed scripts, back up `~/.local/bin/ptest`, copy only after green unit tests, add reviewed namespace/TTL config, then run `PTEST_LIVE_DEDUP_SMOKE=1 ./smoke.sh`.

Expected: two passing callers, one new execution, then one cache hit.

- [ ] **Step 5: Run final gate and commit**

Run: `ptest --full`

```bash
git add smoke.sh README.md
git commit -m "test: prove remote execution deduplication end to end"
```
