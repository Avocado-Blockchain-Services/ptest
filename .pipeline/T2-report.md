# T2 report — Planner v2: changed units, name propagation, per-test decision, self-audit

- status: DONE
- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T2
- taskBranch: feature/dynamic-selection-T2
- commit: 3bdb623 (T2: selection planner v2 with decision-table tests)
- filesOwned:
  - src/ptest/selection_planner.py (new, 657 lines)
  - tests/ng/test_selection_planner.py (new, ~1860 lines)

## Result

Implemented `plan`, `needed_versions`, `needed_data_paths`,
`is_selected` and `audit_misses` per design section 4 (normative).
`ptest tests/ng/test_selection_planner.py` → **80 tests pass**
(79 passed, 1 skipped — the gated `TestWithSourceIndex`, awaiting T1;
orchestrator must confirm 0 skipped after the merge).
Strict TDD was followed: the file was written first and failed at
collection (`ImportError: selection_planner`), then the planner was
built until green. No test or assertion was weakened to get green; three
test-side expectations that contradicted the literal spec were corrected
to the spec (formatting-only edits still count as `reached`; an
incompatible-run node's file still runs whole via 4.6; a name seed puts
the path in SK so module dependents rerun via clause e).

## Three-pass review (dan-jefferies-agent)

- Pass 1 (re-read bytes): two findings, both fixed — corrupt ambient
  *function* ids were silently skipped in clause g instead of tainting
  the run opaque (now in `ambient_bad`, covered by
  `test_corrupt_ambient_ids_taint_nodes_opaque`); an unused unpack and a
  dead test helper were removed.
- Pass 2 (acceptance anchoring): every 4.3 row and 4.5 clause has a
  positive test and a negative twin; propagation fixpoint (const body,
  decorator/default, subclass, two-hop re-export, wildcard, effect→AM,
  parametrize→WF), per-run baselines incl. the 128-signature cap,
  fixtures, ambient (function reach / data `full_reason` with the exact
  string / opaque ambient), N3 static-only clause b with demotion
  clearing, 4.6 unrecorded files, N4 outcomes, and output shape
  (`files`/`deselect`/`whole_files`, `reached`, `recorded`, `coverage`,
  unit ordering by count then label, the six exact fallback reasons).
  Added during review: doc-statement tests ("doc" ignored) and an
  empty-snapshot edge test.
- Pass 3 (smell sweep): planner imports only `ptest.contracts` (no I/O,
  no clock, no project code); all 9 `C.*` names used exist verbatim in
  the frozen barrier; no TODOs/stubs; no other repo file references the
  new module (T5 wires it post-merge); positives fail against a no-op
  planner by construction. Security/math/UX n/a beyond the pure
  function and the guarded `coverage` division.

## Key decisions (spec-literal readings, pinned by tests)

- Clause b is **definitive** for opaque/demoted nodes: later precise
  clauses never apply ("Otherwise it is NOT selected", N3). A demoted
  node with a precise function hit but a static miss is skipped
  (`test_demoted_node_ignores_precise_hit_on_static_miss`).
- Fallback reason precedence: demoted > fixture-missing > opaque.
- Clause-b skips still count toward `reached` when deps touch the change.
- Units come from CF/seeds/AM/D_r/WF/4.6 only (AF selections attribute
  to no unit); propagated names never become units.
- Removed test files follow 4.8 literally (WF entry keeps them in
  `files`); T5 should drop missing paths from argv.

## Integration notes / seams

- **Barrier missing**: the frozen `contracts.py` block (design s12) is
  applied in NEITHER the chain worktree nor this task worktree (both at
  f478189, `SELECTION_PROTOCOL` absent). Per the task brief I did NOT
  touch `contracts.py`; the planner uses only `C.<name>` attribute
  reads, and the **test file carries a marked BARRIER SHIM** declaring
  the missing shapes verbatim from the frozen spec (deleted after the
  merge; nothing else changes meaning). Re-run this file post-merge.
- T5 consumes: `plan`, `needed_versions`, `needed_data_paths`,
  `is_selected`, `audit_misses` with the exact frozen signatures.
- Never pushed, merged, or deployed. Staged explicit paths only
  (2 files, +2518). No migration generated.

## Fix report — audit findings round 2 (3 items, all fixed)

- fixWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T2
- filesOwned: same 2 files
  (`src/ptest/selection_planner.py`, `tests/ng/test_selection_planner.py`).

### 1. Removed test file leaked into `files`/`whole_files` [HIGH][CONFIRMED]
- Fix: `_diff_path` no longer consults store history for removed paths —
  `is_test = path in index.test_files`, so a deleted test file never
  enters WF; the now-unused `historical` set/parameter is removed from
  `plan`/`_analyze`/`_diff_path`. Belt and braces at output assembly:
  `wf_all`, `files`, and `whole` are intersected with the current
  test-file set (5.8 lists runnable test files).
- Regression test `test_removed_test_file_selects_nothing` mirrors the
  finding's repro (baseline {lib, test_lib, test_gone}, index without
  test_gone, changed=(test_gone,)) and asserts `files == ()`,
  `whole_files == ()`, `selected == 0`, no `file` unit.

### 2. Per-node `_reach`/ambient recomputation [HIGH][CONFIRMED]
- Fix: once per run — clause-b reach set (`reach_b`), stale/changed path
  union (`touch`) — and once per analysed run — clause-g ambient paths,
  their reach, and changed-function labels (`ambient_pre`) — built up
  front; per node only membership tests remain. `_reach` no longer
  copies `index.test_files` per call (shared `_test_set` helper). Same
  seed sets, same outcomes (full file green, untouched tests unmodified).
- No timing test pinned (fixture-scale perf is not unit-testable here);
  evidence is the structural O(nodes)→O(runs+signatures) change plus the
  unchanged 80-pass suite.

### 3. Clause g counted units for unselected nodes [MEDIUM][CONFIRMED]
- Fix: `_count("function", ...)` for ambient CF hits runs only after the
  clause-g reach check selects the node (4.8: each unit carries the count
  of nodes it selected).
- Tests: negative twin `test_ambient_function_change_static_miss_skips`
  now asserts `selected == 0` and `units == {"function":
  {LIB+"::serve": 0}}`; positive twin locks the selected path with
  `{LIB+"::serve": 1}`.

## Verification (task worktree root, runner `ptest`)

- `ptest tests/ng/test_selection_planner.py` (exit 0) → 80 passed,
  1 skipped (pre-existing gated `TestWithSourceIndex`, awaiting T1).
- Pre-fix proof (planner stashed to HEAD, tests kept): same command
  (exit 1) → `test_removed_test_file_selects_nothing` and the extended
  negative twin FAILED, 78 passed — both regression tests catch their
  bug; the positive twin passed pre- and post-fix.
- Self-review: full `git diff` re-read caught one fresh issue (duplicated
  frozen-test-set idiom → `_test_set` helper); changed privates are
  module-private and tests use only the public API; no test weakened;
  no TODOs/stubs. Not verified: A3 wall-clock on a 20k-test store (no
  such fixture here). Discovered but not fixed: a removed path can still
  yield a zero-test `module` unit via `am_all` (harmless for argv/D8;
  left minimal).
