# T4 report — Operations: stall plumbing, dump printing/cleanup, verdict

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T4
- taskBranch: feature/post-test-stall-T4
- commit: e6ead20 ("T4 operations: stall plumbing, stack-dump print/cleanup, post-test-stall verdict")
- status: DONE (boxed below; e2e live verification deferred to post-merge by design)

## What shipped (5 files, +927/−4, all T4-owned)

- `src/ptest/stack_dumps.py` (new) — `collect` / `emit` / `cleanup` plus
  `Dump`. Caps: 32 files, 400 lines + 32 KiB per file, 128 KiB total,
  4096 dir entries. Every printed line via `render.terminal_text`.
  Controller (header `role=controller`) first, then PID ascending;
  header-only files skipped; other attempts' names ignored. Refusal:
  `files.validate_private_file` + `files.read_regular` (symlink, foreign
  owner, non-regular, wrong mode, hard link never read nor deleted);
  cleanup unlinks only lstat-regular user-owned files via
  `files.unlink_if_same`. Header
  `ptest: stack dumps (<n> processes) — <reason>`; truncation notes start
  `ptest: stack dump truncated` / `ptest: stack dumps truncated`.
  stderr only, `print` direct (never `-q`-suppressed), nothing returned
  into results (N7 structural).
- `src/ptest/operations.py` — `_stall_timeout_s` seam (non-pytest None;
  unset 120; 0 None; else value), `_STALL_TIMEOUT_S` ContextVar set/reset
  around `_run_guard` on the standard path (`effective`) and `_execute_shadow`
  (`config`), `_launch_guard` passes it into `LaunchManifest` (signature
  unchanged). `_outcome`: post-test-stall + no user cancel →
  `(INCOMPLETE, 70, "ptest", None)` ahead of every raw-code branch; cancel
  falls through to the existing mapping. Frozen rerun suffix via
  `_stall_reason_message` on standard + shadow reason assembly. Dump
  printing after reap: kill codes per attempt (a001, a002 in shadow),
  `guard handoff incomplete` on the early-return path only when nothing
  printed yet and no user cancel. Cleanup in the outer `finally` (standard)
  and alongside both `reports.cleanup_report` loops (shadow).
- `tests/ng/test_stack_dumps.py` (new, 14 tests), `tests/ng/test_run_deadline.py`
  (+19 tests), `tests/ng/test_post_test_stall_e2e.py` (new, 6 tests,
  readiness-gated).
- `progress.py`, `reports.py`, `history.py`: intentionally untouched
  (optional touch points; header formatting lives in `stack_dumps`,
  `_compound_killed` already keys on execution-timeout only — pinned by a
  new regression test). No migration.

## Verification (all `ptest` from the task worktree root, exit 0)

- `ptest tests/ng/test_stack_dumps.py` → `14 passed`, `ptest: passed · 14 tests`
- `ptest tests/ng/test_run_deadline.py -k "stall or compound_killed or post_test_stall or launch_guard_manifest"` → `24 passed`
- `ptest tests/ng/test_post_test_stall_e2e.py tests/ng/test_stack_dumps.py tests/ng/test_run_deadline.py` → `127 passed, 6 skipped`
  (the 6 skips are the e2e readiness gate: `guard._STALL_POLL_S` and
  `pytest_bridge._STALL_MARKER_SUFFIX` are both absent until T1+T3 land)
- `ptest tests/ng/test_operations.py tests/ng/test_shadow.py` → `130 passed, 16 skipped`
  (skips are interpreter-qualification skips in existing tests; my diff adds no skip markers)
- `ptest tests/ng/test_cli.py tests/ng/test_run_output.py` → `416 passed`
- Strict TDD red steps observed: new module first failed collection
  (`ImportError`); deadline additions failed 18/24 pre-implementation
  (seam/Outcome/suffix/manifest/fault); two `stack_dumps` failures were
  test-side mistakes I fixed in the tests (single 30 KiB line is
  `terminal_text`-capped to 1 KiB so the total-cap never tripped; exact
  pull-count assertion replaced with a 10⁶-entry fake proving ≤4096 names).

## Acceptance mapping (design §4 T4)

- Manifest carries the effective value for pytest, None otherwise:
  seam unit tests + `_launch_guard` encode-spy test (`test_run_deadline.py`).
- Printer bounds/escaping/ordering/refusal/cleanup: `test_stack_dumps.py`.
- Verdict 70 + rerun suffix + never-promoted raw codes (0 and 23 via
  `TEST_GUARD_FAULT=problem:post-test-stall:<raw>` real-guard fault test),
  `full_gate_eligible` False, no dump text in `serialize_run_result` (N7),
  no `history._compound_killed` feed: `test_run_deadline.py`.
- E2E serial/xdist stall, deadline dump (G1), Ctrl-C no-dump (N8), healthy
  run (N10), disabled-timeout slow teardown (N9): `test_post_test_stall_e2e.py`,
  skipped here by the mandated gate; orchestrator must confirm 0 skipped
  after T1+T3 merge.
- End line format unchanged (`progress.format_end` untouched); cause rides
  the existing `cli._emit_reasons` reason line per D8, so no `cli.py` change.

## Integration notes (for the merge; all pre-T2-barrier shims, identical bytes)

- `contracts.py` barrier is absent in every worktree (chain base included),
  and `contracts.py` is T2-owned, so this diff never touches it. Three
  guarded fallbacks, all no-ops after the barrier lands: `stack_dumps`
  name constants + local `stack_dump_pid` mirror (`stack_dumps.py:24-46`),
  `getattr(config.runner, "stall_timeout_s", None)` (`operations.py:66-83`),
  conditional `stall_timeout_s` manifest kwarg (`operations.py:1304`).
  Deadline tests pin behavior, not the barrier, via `_stall_reason_code()`.
- Needs from siblings (unchanged frozen shapes assumed): T1 bridge marker +
  dump files + `_STALL_MARKER_SUFFIX`; T3 `guard._STALL_POLL_S`,
  `guard._DUMP_WAIT_S`, stall `Problem`, SIGWINCH-before-kill; T2
  `RunnerConfig.stall_timeout_s` parsing (e2e deliberately monkeypatches
  the seam instead of writing `stall_timeout` in TOML).

## Honest closing (dan-jefferies passes 1–3)

- Re-read the full diff twice; one non-obvious check confirmed:
  shadow `bindings` are built pre-launch with `.path` per attempt and the
  emit loop reuses the file's own `a{index:03d}` convention.
- Claimed-vs-shipped delta: none. The e2e module is written to the frozen
  interfaces but its live assertions (stall kill, worker stacks, deadline
  dumps) are NOT verified here — they cannot run until T1+T3 merge.
  That is the design-mandated gate, not a deferral of my own logic.
- Stubbed/TODO/deferred: none (grep clean). Owned-vs-touched: identical
  5-file set; no other file touched.
- Out-of-scope smells (not fixed): `stack_dumps._parse_identity` detects
  the controller by substring (`role=controller`); a hand-crafted
  user-owned dump could mislabel ordering only — harmless, noted.
- implemented: seam, ContextVar, manifest, `_outcome`, suffixes, printing
  (3 paths), cleanup (3 sites), printer module, 3 test files.
- verified: all green runs above with exact commands/exit codes.
- not verified: live e2e stall behavior (needs T1+T3; 6 gated skips here).
- deferred: removal of the three pre-barrier fallbacks after T2 lands
  (optional cleanup; they are inert post-merge).
- discovered-but-not-fixed: none in scope.
- Confidence: high for unit/plumbing verdict behavior (red→green observed,
  400+ adjacent tests green); medium for post-merge e2e count assertions
  (`(3 processes)` assumes controller + 2 worker dumps all non-empty —
  re-check this one number when the gate lifts).
- never pushed, never merged, no live database touched, no `.pipeline/`
  files staged; worktree clean at `e6ead20`.
