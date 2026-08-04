# Spot Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `ptest status` command that reports live queued jobs, working jobs, and powered-on Spot worker VMs.

**Architecture:** Reuse `GcloudControllerAdapter`, which already owns the authenticated definitions of Pub/Sub backlog, durable worker leases, and RUNNING MIG instances. Add a small CLI-facing status method and command dispatcher, keeping queue mutation APIs entirely out of the path.

**Tech Stack:** Python 3.11+, stdlib CLI, Google Cloud CLI/Monitoring/Compute/Storage APIs, `ptest` test wrapper.

## Global Constraints

- All tests run through `ptest`; never invoke a raw test runner.
- `ptest status` is observational: it must not pull, acknowledge, publish, cancel, create, or resize any GCP resource.
- The displayed values are Pub/Sub backlog, unexpired durable leases, and RUNNING instances respectively.

---

### Task 1: Command behavior tests

**Files:** `tests/test_main_wiring.py`, `tests/test_spot_controller.py`.

**Interfaces:** Consumes `ptest.main(argv)` and `GcloudControllerAdapter.authenticated_metrics()`. Produces command regression coverage and `status_metrics() -> tuple[int, int, int]`.

- [ ] Write failing tests for `ptest status` printing `waiting jobs`, `working jobs`, and `servers on`; add a controller test proving `status_metrics()` returns backlog, leases, and running workers.
- [ ] Run `ptest tests/test_main_wiring.py tests/test_spot_controller.py -k status`; expect failure because the command and method do not exist.
- [ ] Commit the red tests with `test: define Spot status command`.

### Task 2: Read-only status implementation

**Files:** `spot_controller.py`, `ptest`, `tests/test_main_wiring.py`, `tests/test_spot_controller.py`.

**Interfaces:** `status_metrics()` returns `(backlog, leases, workers)`. `spot_status(pcfg, cfg)` validates Spot config, creates the GCP adapter, and returns the same tuple.

- [ ] Add `status_metrics()` by reusing `authenticated_metrics()` and returning backlog, leases, and running workers only.
- [ ] Add `spot_status()` and `status` CLI dispatch. Print exactly `Spot queue`, `waiting jobs`, `working jobs`, and `servers on`.
- [ ] Reject non-Spot configuration and propagate GCP read errors without substituting zeros.
- [ ] Run `ptest tests/test_main_wiring.py tests/test_spot_controller.py -k status`; expect all selected status tests to pass.
- [ ] Commit implementation with `feat: add Spot queue status command`.

### Task 3: Full verification and live read-only check

**Files:** verify all Spot test modules and the deployed queue.

- [ ] Run `ptest tests/test_main_wiring.py tests/test_spot_controller.py tests/test_spot_queue.py tests/test_spot_worker.py tests/test_spot_bootstrap.py tests/test_spot_deployment.py`.
- [ ] Run `PTEST_CONFIG=/tmp/ptest-spot-persea-live.toml ptest status` and compare its counts with direct read-only GCP observations.
- [ ] Confirm the worktree is clean and both status commits are present.
