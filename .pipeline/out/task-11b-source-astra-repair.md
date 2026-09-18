# Task 11B2 source repair evidence — 2026-09-18

Implementation commit: `bc230e7a7a950c60ab7c77552b0186668ecdc94d`.
Base: `bea3f5e` on branch `cx-ng-product-task-11b-source-astra`.
Checkout: `/home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra`.

This is implementation and verification evidence, not an independent audit.
The orchestrator owns the next review and final integrated full-suite gate.

## Authority and inspected contracts

- Worktree `AGENTS.md`, `.pipeline/context.md`, original Task 11B2 source brief
  in the original `task-11b-source` checkout, and the supplied repair brief.
- Product specification T3/C3/A5 and design source/operations API contracts.
- Raw Opus audit record:
  `/home/ingmar/.claude/projects/-home-ingmar-worktrees-ptest-cx-ng-product-task-11b-source/1b5fb15d-ac27-4efc-95ba-b09f4ea4f449.jsonl`,
  including its StructuredOutput verdict, blocking B1/B2/B3, and notes.
- Source capture implementation, operations lifecycle, relevant snapshot,
  reason, result and serialization contracts, and existing operations/source
  tests and command fixture.
- Applied secure-by-spec, systematic debugging, TDD, and verification before
  completion. Graphify had no existing graph in this checkout; its required
  post-change update was AST-only.

## Changes

Only `src/ptest/operations.py`, `tests/ng/test_operations.py`, and the command
test fixture changed in the implementation commit. No public API/schema changes.

- B1: source invalidation alters outcome/status only for a full execution plan.
  Scoped execution retains raw-zero PASSED/0 and raw-23 FAILED/23, records both
  actual snapshots and changed-during-run, and makes no source/gate/baseline claim.
- B2: a known initial digest followed by unavailable final identity produces
  a stable `unknown-input` reason. Full raw-zero becomes INCOMPLETE/70 with ptest
  origin; raw-23 becomes INCOMPLETE/23 with runner origin. The raw runner status
  is retained. Unknown-to-unknown still executes with visible limitations and
  all source/gate/baseline claims false.
- B3: removed mock-only source acceptance. Git-initialized fixtures use isolated
  local init/add/commit with hooks/global config/signing disabled, the actual
  private-key API, actual source scanning, and real guarded subprocesses.
  Tracked-input modifications prove the recorded pre/post digests differ.
  A chmod accompanying a write causes real known-to-unknown source evidence.
- Recheck pending signals immediately after pre-launch capture. Cancellation
  revokes the pending grant without launching a guard or the marker workload.
- Unexpected key/snapshot failures, including KeyboardInterrupt, revoke the
  unregistered grant before propagating, matching adapter-preparation cleanup.
- Derive bounded path-class messages from existing fingerprint/change evidence.
  Typed reasons disclose only `tracked`, `untracked`, `ignored`, or
  `unclassified`; concrete paths and contents are not copied to these reasons.
  Only fingerprints that differ between captures contribute class evidence, so
  unchanged pre-existing dirty files do not contaminate the class report.
- Consolidated duplicate reason de-duplication in the affected helper area.
  `_source_changed` had only the replaced operations caller and was removed.

## Negative contracts established before implementation

The progress update identified these negative twins before production edits:
scoped observations must not become full gates; missing post-run evidence must
not imply equality; cancellation during capture must not launch; unexpected
capture exceptions must not strand an unregistered grant; reasons must not
reveal paths/content or grow with the number of changed paths.

| Axis | Scope and evidence |
|---|---|
| Identity | Existing authenticated guard facts, DRAINING, EOF and reap precede final capture. Ordering test observes real frames and lease states. No new credential scheme. |
| Authorization | Key creation/capture occur after exclusive grant; capture-time cancellation prevents guard launch. |
| Tenancy | Every acceptance fixture uses its own domain and repository. No normal-state lookup or shared mutable fixture resource added. Existing scheduler scope exercises ownership boundaries. |
| Input | Real Git tracked, untracked, ignored and mode-change cases; snapshots remain typed. No shell or extra source scan introduced in operations. |
| State | Scoped/full exit precedence, known-to-unknown invalidation, unknown-to-unknown uncertainty, pending-cancel cleanup, and BaseException cleanup have executable evidence. |
| Exposure | Changed reason paths are empty; filenames and source contents absent; class messages bounded below 256 characters in acceptance cases and use a fixed four-class vocabulary. |
| Availability | Class derivation is linear in the already bounded snapshots. Existing source scan limits are unchanged; no new load/fuzz campaign performed. |
| Dependencies | Existing typed source API and local Git test fixtures only. No new runtime/development dependencies or network operations. |

These contracts come from T3/C3/A5 and the explicit repair/audit notes; no new
domain policy or speculative security infrastructure was introduced.

## Exact test commands and results

All commands below ran with cwd
`/home/ingmar/worktrees/ptest/cx-ng-product/ptest` through the controller's ptest
bootstrap. The bootstrap reported local backend, one worker, dispatcher
`/home/ingmar/.local/bin/.ptest-bundles/bundle.U1mnf3/ptest`, SHA256
`3cab209115a4f7e62be18996db77448288c1bec32218ba10f6d55bf4842fde68`.
No raw test runner was invoked directly.

### RED: real source and outcome contracts

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_operations.py -k 'source or path_class'
```

Exit 1: `9 failed, 2 passed, 48 deselected in 3.58s`.

- Full raw-0/raw-23 and automatic raw-0 reached the expected exit but failed
  because changed reasons did not disclose the tracked class.
- Scoped raw-0 and raw-23 failed because status was INCOMPLETE (zero became 70).
- Ignored/untracked reason cases failed because no class was disclosed.
- Known-to-unknown raw-0/raw-23 failed because status stayed PASSED/FAILED.
- Unchanged Git source and real non-Git unknown-to-unknown were passing
  regression controls, not claimed RED cases.

### RED: pending lease hardening

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_operations.py -k 'source_capture'
```

Exit 1: `5 failed, 1 passed, 59 deselected in 1.07s`.

- A SIGTERM during the actual pre-run capture entered the guard path and
  returned INCOMPLETE instead of pending CANCELLED.
- RuntimeError and KeyboardInterrupt at both key creation and snapshot capture
  left GRANTED leases rather than CANCELLED.
- The ordering test passed on existing code: it is preservation evidence, not
  newly implemented ordering or claimed RED evidence.

### GREEN: focused repair

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_operations.py -k 'source or path_class' --durations=10
```

Exit 0: `17 passed, 48 deselected in 4.02s`.

### GREEN: operations/source/scheduler integration

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_operations.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_source.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11b-source-astra/tests/ng/test_scheduler.py --durations=15
```

Exit 0: `284 passed in 24.78s`.

Slowest: new queue-ordering test 0.55s (real admission wait plus two Git scans
and guarded execution); existing native-signal/cancellation case 0.54s.
All remaining displayed cases were at most 0.46s; no test exceeded 2s/3s.

The ordering acceptance proves a queued file edit enters the *initial* snapshot,
the key does not exist while queued, grant precedes key creation, capture
precedes launch, authenticated guard EOF/reap and DRAINING precede final capture,
and both captures precede begin_finalization. The finish observer reads the
already-written export and compares its source_valid with the Finalization
argument. Changed acceptance also verifies attempt status/native/final exits,
exported outcomes/non-claims, and terminal lease release.

## Other checks and limits

- `git diff --check`: exit 0 before source commit.
- `graphify update .`: exit 0, AST only; 4,393 nodes, 9,527 edges, 299 communities.
  Warning: seven JSON files produced zero nodes, including implementation.json,
  review.json, protocol-v1.json and matrix fixtures. No semantic provider used.
  Graph output is ignored/local; no wiki/semantic metadata was used as fresh
  evidence. No pre-existing graph was available for the initial query.
- Security gate inventory found no installed repository gate scripts/config.
  Design/plan reserve Bandit, dependency advisory scanning, Gitleaks, and
  sensitivity fixtures for Task 13. These scanners were not run or installed;
  their absence is not reported as a passing scan.
- No `ptest --full`, independent/self audit, push, deployment, main merge,
  installed-CLI replacement, or network smoke was performed.
- Generic command execution still has no verified native runtime identity;
  source_valid is therefore false even for unchanged known content. No native
  setup, selection, baseline, history publication, probe, shadow, or remote
  execution capability was added. OS evidence is this Linux fixture environment.
- InputSnapshot has no exhaustive per-file Git-class field. Declared ignored
  files or non-file influences without Change records are conservatively
  reported as `unclassified`; this avoids guessed classes and API expansion.
- Ignored runner caches remain relevant unless narrowly declared non-input
  outputs by reviewed policy. No default cache exemption was added.
- Public run serialization still allowlists its existing fields and does not
  expose internal input_before/input_after snapshots. The typed RunResult
  retains both; this task did not expand the public schema.
- Unexpected post-launch programmer exceptions, external edits after the final
  capture, and failures of the scheduler cancellation operation itself are not
  newly fault-tested here. Existing scheduler authority/recovery remains in use.
- The supplied untracked repair brief was preserved and excluded from commits.
