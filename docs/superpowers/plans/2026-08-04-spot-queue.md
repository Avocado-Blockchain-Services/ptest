# Snapshot Cache and Spot Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add source-archive cache fast paths and an opt-in Spot-VM queue backend without applying or merging infrastructure.

**Architecture:** GCS content-addressed source artifacts and existing result deduplication stay authoritative. Pub/Sub transports request keys; a worker writes generation-safe leases/results, while a controller only scales a regional Spot MIG.

**Tech Stack:** Python standard library, gcloud CLI adapters, Pub/Sub/Storage/Compute/Cloud Run Terraform, Docker.

## Global Constraints

- Use `ptest` for every test command; never raw pytest/Vitest/npm test.
- Preserve dirty-tree source digest semantics and GCS generation-precondition safety.
- `spot_queue` is opt-in; existing `cloudrun` behavior stays unchanged.
- Queue work is at-least-once safe; each worker handles one suite in a new temporary directory.
- Do not run `terraform apply`, alter deployed infrastructure, alter user config, push, or merge.
- Configure a regional Spot MIG with max workers defaulting to 5 and idle timeout defaulting to 3600 seconds; excess backlog remains in Pub/Sub.

---

### Task 1: Source archive cache fast path

**Files:** Modify `ptest`, `tests/test_source_identity.py`, `tests/test_remote_deduplication.py`.

**Interfaces:** `source_object_exists(base, bucket, object_name) -> bool | None`; `prepare_source_archive(root, digest, manifest, base, store, bucket) -> Path | None`.

- [ ] Write failing tests: an existing `sources/<digest>.tar.gz` must not call `pack_tree` or `upload_source_once`; a missing object must pack and upload exactly once.
- [ ] Run `ptest tests/test_source_identity.py tests/test_remote_deduplication.py` and confirm RED.
- [ ] Implement a read-only GCS object probe. Empty/404 is a miss; ambiguous probe failure preserves the current safe local fallback. On hit skip packing; on miss package, revalidate the source digest, and retain generation-match upload.
- [ ] Add separate `source digest`, `source cache hit/miss`, `source packaging`, and `source upload` timing logs.
- [ ] Re-run the same tests, then commit `perf: skip packaging cached remote sources`.

### Task 2: Durable queue protocol and ptest backend

**Files:** Modify `ptest`, `config.example.toml`, `tests/test_main_wiring.py`; create `spot_queue.py`, `tests/test_spot_queue.py`.

**Interfaces:** versioned `SpotRequest`, `Lease`, and `SpotResult`; `SpotQueueStore.claim(request_key, worker_id, now, lease_seconds) -> Lease | None`; `publish_result(result, lease) -> bool`.

- [ ] Write failing tests for exclusive generation-safe claims, expired-lease reacquisition, idempotent terminal result publication, malformed record rejection, and `backend = "spot_queue"` routing.
- [ ] Run `ptest tests/test_spot_queue.py tests/test_main_wiring.py` and confirm RED.
- [ ] Implement a production gcloud/GCS/Pub/Sub adapter and an in-memory test adapter. Request records include exact command, source URI, request key, schema version, and timestamps. ptest reuses passing results, publishes a request/message, waits for durable terminal result, and preserves `--fresh`.
- [ ] Re-run `ptest tests/test_spot_queue.py tests/test_main_wiring.py tests/test_remote_cache_records.py`, then commit `feat: add durable spot queue backend`.

### Task 3: Worker, controller, and Terraform

**Files:** Create `spot_worker.py`, `spot_controller.py`, `Dockerfile.spot-worker`, `Dockerfile.spot-controller`, `terraform/spot_queue.tf`, `tests/test_spot_worker.py`, `tests/test_spot_controller.py`; modify `terraform/variables.tf`, `terraform/outputs.tf`, `README.md`.

**Interfaces:** `scale_target(backlog, active_workers, idle_seconds, max_workers, active_leases=0) -> int`; `run_claimed_request(request, workspace_root, adapter) -> SpotResult`.

- [ ] Write failing tests: keep one worker at 3599 idle seconds, zero at 3600; cap backlog at five; never scale in an active lease; worker event order is download, unpack, run, publish result, then acknowledge.
- [ ] Run `ptest tests/test_spot_worker.py tests/test_spot_controller.py` and confirm RED.
- [ ] Implement a worker pull/lease/heartbeat/cleanup loop with SIGTERM/preemption leaving work unacknowledged. Implement a pure controller scale policy and authenticated reconcile process; it never executes tests or owns result state.
- [ ] Add Docker images with baked Node/Vitest dependencies. Add non-applied Terraform for Pub/Sub pull subscription, service accounts/IAM, regional Spot MIG/instance template/health check, controller service, scheduler, variables `spot_max_workers=5`, `spot_idle_seconds=3600`, images, and machine type.
- [ ] Run `ptest tests/test_spot_worker.py tests/test_spot_controller.py tests/test_spot_queue.py`; run `terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate`; document the manual-only deployment procedure; commit `feat: add spot queue worker infrastructure`.

### Task 4: Whole-branch verification

**Files:** Only audited-finding fixes.

- [ ] Run `ptest --full`.
- [ ] Run `python -m py_compile ptest spot_queue.py spot_worker.py spot_controller.py` and `terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate`.
- [ ] Audit: no apply/config mutation/default-backend change; validated records only; result before acknowledgement; fresh worker directories; capacity/backlog behavior.
- [ ] Commit only review fixes. Leave the branch unmerged and the worktree intact.
