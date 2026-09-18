# ptest NG Vitest Basic-Serial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or an equivalent isolated-worktree, test-first workflow. Every test runs through `ptest`; never invoke a raw test runner.

**Goal:** Add one truthful, local, explicitly scoped native Vitest path that can certify only a guarded `basic_serial` run.

**Architecture:** The adapter admits only `runner.kind=vitest`, `Mode.SCOPED`, and `Plan.execution="scoped"`; it launches a project-local Node/Vitest bridge with literal arguments and a single scheduler slot. The bridge replaces the prototype event record with `NativeTerminalReport`'s private protocol and only reports completion after the native run, owned reporter, revalidation, close, and guard quiescence contract have succeeded. `operations.execute` consumes the same report allocation/finalization lifecycle used by scoped Pytest.

**Tech Stack:** Python 3.13, Node/Vitest v3 candidates, private JSON terminal reports, ptest guard/scheduler, `ptest` bootstrap runner.

**Spec:** `docs/designs/2026-09-17-ptest-ng-design.md` plus the approved 2026-09-18 local-first product direction.

## Global Constraints

- Local only. Do not add a remote backend, API, TUI, dependency, lockfile, installer, public schema, or deployment change.
- Support only explicit scoped Vitest execution. Full, automatic, selected, setup/shadow/probe modes, identity/selection/history/baseline publication, and native worker parallelism remain unavailable.
- Force exactly one nonexclusive scheduler slot/job and a `basic_serial` bridge profile. Never infer support or an advanced profile from a Vitest version string.
- Use a validated Node launcher and literal argv only; reject package-manager/shell launchers, launcher flags, and unsafe Node environment injection rather than stripping it.
- The target project is executable local code, not a sandbox. Prevent ordinary foreign execution-control configuration from certifying a serial run; document that config/plugin/report code can have side effects before refusal.
- Candidate Node/Vitest tuples are unqualified until a real preprovisioned subprocess run records evidence. Never install fixtures as part of tests.

---

### Task 1: Scoped adapter admission and prepared-run contract

**Files:**
- Modify: `src/ptest/adapters/vitest.py`
- Modify: `tests/ng/test_vitest_adapter.py`

**Produces:** `prepare(config, plan, grant, attempt)` refuses every non-scoped/non-Vitest plan, reserves one serial capability, preserves configured/request literal arguments exactly once, validates a Node-only launcher, and binds only bridge identity/environment values required by the executor.

- [ ] Write failing unit tests for scoped-only admission, requested worker downgrade to one, npm/npx/shell/launcher-flag refusal, literal suffix handling, and unavailable capability flags.
- [ ] Run the focused Vitest adapter tests through `scripts/ptest-bootstrap` and record RED.
- [ ] Implement the minimal adapter contract and run the same focused scope GREEN.
- [ ] Commit adapter/test changes.

### Task 2: Replace bridge terminal evidence with the shared private protocol

**Files:**
- Modify: `src/ptest/runtime/vitest_bridge.mjs`
- Modify: `tests/ng/test_vitest_adapter.py`
- Modify: `tests/ng/fixtures/vitest/NATIVE_MATRIX.md`

**Produces:** a project-local bridge that accepts only scoped/basic-serial control, creates an exclusive 0600 report at the allocated path, writes the exact `NativeTerminalReport` key set, authenticates exit agreement, and rejects incompatible pool/workspace/watch/browser/API/custom-runner/sequencer/plugin/Node-environment controls without executing test bodies.

- [ ] Write failing bridge tests for protocol shape, missing/replayed/colliding report, literal argv, rejection of owned-control override, and successful report only after owned terminal/close handling.
- [ ] Run focused bridge tests RED through `ptest`.
- [ ] Implement the smallest bridge rewrite; do not retain or translate the old event/profile/status/files record.
- [ ] Run focused bridge tests GREEN and commit.

### Task 3: Guarded executor lifecycle and qualified real-subprocess evidence

**Files:**
- Modify: `src/ptest/operations.py` only where a small shared native-report seam is necessary
- Create: `tests/ng/test_vitest_scoped_subprocess.py`
- Modify: `tests/ng/fixtures/vitest/NATIVE_MATRIX.md`
- Modify: `tests/ng/test_vitest_adapter.py` only for public adapter contract regressions

**Consumes:** Task 1's serial `PreparedRun` and Task 2's report protocol.

**Produces:** grant → post-queue configuration recheck → report allocation → guard launch → DRAINING/quiescence → report consumption → export → release sequence, with scoped claims permanently false. Real preprovisioned candidates prove native pass/failure, same-checkout serialization, separate-checkout overlap, queued config mutation, cancellation, report replay/mismatch, and observed serial limits; unavailable tuples skip explicitly.

- [ ] Write real subprocess tests before lifecycle code, observing a meaningful RED in a preprovisioned native fixture or explicitly recording why a tuple is unqualified.
- [ ] Implement only the required lifecycle connection and test it through scoped `ptest`.
- [ ] Record the exact platform/Node/Vitest/Vite tuple, pass/fail result, skips, and residual risks.
- [ ] Commit code, tests, and evidence.

### Task 4: Mechanical gate and independent audit

- [ ] Run all Task 11E scoped tests with worker-capped `ptest`, inspect durations, run `graphify update .`, `git diff --check`, JSON validation, and applicable checked-in security gates.
- [ ] Have Claude Opus high audit the integrated range and every abuse case above. Any rejection receives a focused repair before integration.
- [ ] Run `ptest --full` only after the final remaining feature slice is integrated; do not run it during this task alone.

## Self-review

- Scope coverage: Tasks 1–3 cover admission, bridge/report semantics, lifecycle, and qualification; Task 4 covers integration and audit.
- No public config/schema/dependency/remote work is included.
- The only supported completion claim is a real, qualified scoped `basic_serial` tuple.
