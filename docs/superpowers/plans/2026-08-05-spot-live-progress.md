# Spot Live Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user follow a running durable Spot request and see elapsed time, parsed test progress, and recent output without affecting the run.

**Architecture:** `LocalProcessAdapter` will stream its child process output into a bounded buffer and notify a worker-owned reporter.  The reporter writes a generation-safe `SpotProgress` record under the request key while the lease remains live.  The CLI reads that record and the terminal result, rendering a polling `ptest follow KEY` view.

**Tech Stack:** Python 3 standard library, existing GCS JSON REST adapter, pytest through `ptest`.

## Global Constraints

- Progress is advisory; leases and terminal results remain authoritative.
- `follow` is read-only: no publish, acknowledgement, claim, lease update, or worker scaling.
- All project tests run through `ptest`; no raw test runners.
- Bounded excerpts only; never write unbounded test output to a durable record.

---

### Task 1: Durable progress record and storage API

**Files:**
- Modify: `spot_queue.py`
- Test: `tests/test_spot_queue.py`

**Interfaces:**
- Produces `SpotProgress(request_key, worker_id, observed_at, elapsed_seconds, completed_tests, total_tests, output_tail)`.
- Produces `GcloudSpotQueueStore.publish_progress(progress, lease) -> bool`, `read_progress(request_key) -> SpotProgress | None`, and `clear_progress(request_key, lease) -> bool`.

- [ ] **Step 1: Write the failing record/storage tests**

```python
def test_progress_round_trips_and_is_bound_to_its_request_key():
    progress = SpotProgress(KEY, "worker-a", NOW, 12, 4, 10, ".... 40%")
    store.publish_progress(progress, lease)
    assert store.read_progress(KEY) == progress

def test_progress_write_from_an_expired_or_wrong_lease_is_rejected():
    assert store.publish_progress(progress, other_lease) is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `ptest tests/test_spot_queue.py -k progress`
Expected: failure because `SpotProgress` and its storage operations do not exist.

- [ ] **Step 3: Implement the minimal record and GCS operations**

```python
PROGRESS_SCHEMA = "ptest-spot-progress-v1"
PROGRESS_OUTPUT_LIMIT = 8_000

@dataclass(frozen=True)
class SpotProgress: ...

def publish_progress(self, progress: SpotProgress, lease: Lease) -> bool: ...
def read_progress(self, request_key: str) -> SpotProgress | None: ...
```

Use `spot/v1/progress/<request-key>.json`; require a current matching lease before each write.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `ptest tests/test_spot_queue.py -k progress`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add spot_queue.py tests/test_spot_queue.py
git commit -m "feat: add durable Spot progress records"
```

### Task 2: Worker streaming reporter

**Files:**
- Modify: `spot_worker.py`
- Test: `tests/test_spot_worker.py`

**Interfaces:**
- Consumes `SpotProgress` and `publish_progress` from Task 1.
- Produces `LocalProcessAdapter.run(command, cwd, on_output=None) -> tuple[int, str]` and `parse_test_progress(output: str) -> tuple[int | None, int | None]`.

- [ ] **Step 1: Write the failing streaming tests**

```python
def test_runner_reports_incremental_output_before_process_exits(tmp_path):
    events = []
    code, output = adapter.run("printf '3 passed, 7 total\\n'", tmp_path, events.append)
    assert code == 0
    assert events == ["3 passed, 7 total\\n"]

def test_worker_publishes_parsed_progress_while_a_claim_is_running():
    worker.run_claimed_request(request, lease)
    assert progress_records[-1].completed_tests == 3
    assert progress_records[-1].total_tests == 7
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `ptest tests/test_spot_worker.py -k 'incremental or progress'`
Expected: failure because the callback/reporting behavior does not exist.

- [ ] **Step 3: Implement bounded stream capture and periodic reporting**

```python
def parse_test_progress(output: str) -> tuple[int | None, int | None]: ...

def run(self, command, cwd, on_output=None):
    # read stdout line-by-line, retain bounded tail, invoke on_output
```

The worker writes every ten seconds at most, emits immediately on recognizable progress, and stops publishing before terminal result publication.  Heartbeats continue independently.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `ptest tests/test_spot_worker.py -k 'incremental or progress'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add spot_worker.py tests/test_spot_worker.py
git commit -m "feat: publish live Spot worker progress"
```

### Task 3: Read-only `ptest follow` command

**Files:**
- Modify: `ptest`, `spot_queue.py`
- Test: `tests/test_main_wiring.py`, `tests/test_spot_queue.py`

**Interfaces:**
- Consumes `read_progress(key)` and `read_result(key)`.
- Produces `main(["follow", KEY]) -> int`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_follow_renders_percentage_and_recent_output(wired, capsys):
    wired.progress = SpotProgress(KEY, "worker-a", NOW, 30, 6, 10, "... 6 passed")
    assert ptest.main(["follow", KEY, "--once"]) == 0
    assert "60%" in capsys.readouterr().out

def test_follow_prints_terminal_output_and_returns_terminal_exit_code(wired, capsys):
    wired.result = SpotResult(KEY, "failed", 1, "1 failed", NOW)
    assert ptest.main(["follow", KEY, "--once"]) == 1
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `ptest tests/test_main_wiring.py -k follow`
Expected: failure because `follow` is not a recognized command.

- [ ] **Step 3: Implement command routing and rendering**

```python
def follow_spot_result(store, request_key: str, once: bool = False) -> int: ...
```

Validate the key exactly as `result` does.  Poll every two seconds, display unknown percentages as elapsed only, classify stale progress from the live lease, and print terminal output verbatim before returning its exit code.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `ptest tests/test_main_wiring.py -k follow tests/test_spot_queue.py -k progress`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ptest spot_queue.py tests/test_main_wiring.py tests/test_spot_queue.py
git commit -m "feat: follow live Spot test progress"
```

### Task 4: Packaging, permission, and live verification

**Files:**
- Modify: `terraform/spot_queue.tf`, `README.md`
- Test: `tests/test_spot_terraform.py`, `tests/test_main_wiring.py`, `tests/test_spot_worker.py`, `tests/test_spot_queue.py`

**Interfaces:**
- Worker service account can write `spot/v1/progress/`.
- Client/operator can read `spot/v1/progress/`.

- [ ] **Step 1: Write failing IAM/template tests**

```python
def test_progress_prefix_is_granted_to_worker_and_operator_readers(terraform):
    assert 'spot/v1/progress/' in terraform
```

- [ ] **Step 2: Run focused test and verify RED**

Run: `ptest tests/test_spot_terraform.py -k progress`
Expected: failure because the prefix is absent.

- [ ] **Step 3: Add the least-privilege progress object permissions and docs**

Document `ptest follow <key>` under the existing result/status command help.

- [ ] **Step 4: Run complete targeted verification**

Run: `ptest tests/test_spot_queue.py tests/test_spot_worker.py tests/test_main_wiring.py tests/test_spot_terraform.py`
Expected: PASS.

- [ ] **Step 5: Rebuild/deploy worker and install CLI, then live-check**

Build the Spot worker image, update the worker template, run one new `ptest --full --fresh`, and verify `ptest follow <key>` reports a running record without changing the queue state.

- [ ] **Step 6: Commit**

```bash
git add terraform/spot_queue.tf README.md tests/test_spot_terraform.py
git commit -m "docs: expose Spot progress follow command"
```
