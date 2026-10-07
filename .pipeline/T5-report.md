# T5 report — Integration: config key, CLI routing/UX, operations wiring, status, N9, E2E

- status: DONE
- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T5
- taskBranch: feature/dynamic-selection-T5
- commit: bde6adf ("T5: dynamic-selection integration (config key, CLI routing, engine, ops wiring)")
- base: f478189

## What was done (all acceptance items)

1. **Barrier (owner of record).** The shared contracts block was missing in
   every checkout (chain + task worktrees at f478189, `SELECTION_PROTOCOL`
   count 0), so as owner of record I applied design section 12 edits A–F
   verbatim (mechanical extraction for F, hand edits for A–E) and nothing
   else: `src/ptest/contracts.py` hunks are exactly the six barrier edits.
   Policy digest verified: `sha256(repr(policy))` =
   `43bc98485c25…` for default, `dynamic=True` and `dynamic=False`.
2. **Config** (`src/ptest/config.py:484`, `:489`, `:530`, `:1220`):
   `[selection] dynamic` bool, default true, rendered as `dynamic = false`
   after `full_ratio` only when false. Digest unchanged (repr-excluded).
3. **Engine** (`src/ptest/selection_engine.py`, new): `planning` (:118),
   `compatibility_fingerprint` (:207), `refine` (:367), `preview` (:522),
   `recording_enabled`/`prepare_run` (:540/:569), `after_run` (:622),
   `status_lines` (:899). T1–T3 only through lazy `_impact`,
   `_source_index`, `_planner`, `_store`, `_ingest` accessors. All seven
   2.8 static reasons, D8 full conversion (planner `full_reason` first,
   then recorded ratio, file ratio, 200-file limit), 2.7 index rule
   (given-complete reused, missing built once with planning key+cache,
   incomplete/raising → `dynamic planner failed`), D10 ingest matrix,
   D11 self-audit (snapshot → plan → demote → update, audit line always
   on misses, `-v` summary even with none). `after_run`/`refine`/`preview`
   never raise, never print except audit/`-v` lines from `after_run`.
4. **CLI** (`src/ptest/cli.py`): `_selection_api` seam (:3034); `_impact_note`
   dynamic/static branches (:3051) with byte-identical 0.4 fallthrough;
   `_impact_run_request` carries `deselect` on SCOPED (:3098);
   `_plan_and_refine` wraps all four routing paths (standalone, folder ×2,
   monorepo) with per-child configs (:3363); folder deselect filtered to
   folder files, never inside explicitly named files (:3398,
   `_run_scoped_request` deselect param); `-v` details escaped
   (:3389); human `status` shows one line per existing store, JSON
   unchanged, never creates state (:888, :2881).
5. **Operations** (`src/ptest/operations.py`): `_selection_engine` seam
   (:364); recorder env + deselect binding after report bind (:3502, D9/D7 —
   write failure still runs everything); post-report ingest/audit
   (:4039, D10/D11, audit/`-v` before the end line).
6. **Progress** (`src/ptest/progress.py`): `summarize_units`,
   `format_store_bytes`, `format_selection_store/audit/vaudit/recorded/
   not_recorded` + `__all__` entries.
7. **N9** (`tests/ng/test_selection_history_compat.py`, new):
   `REASON_CODES` and `_PERSISTED_REASON_ALIASES` pinned to frozen 0.4.10
   literals; dynamic-scoped, full and static-fallback publishes decode
   with only frozen top-level/nested keys and codes
   (`decode_public_document("run", …)` accepts each row); no selection
   token leaks into summaries. `history.py` untouched.
8. **E2E** (`tests/ng/test_selection_e2e.py`, new): 10 real cases per the
   acceptance list (function/constant/effectful/data/support edits,
   empty store, second worktree via `--base`, failing rerun, getattr
   audit→demote→static-reach, single-test deselect), subprocess-CLI
   (`case.invoke`) on an inline git project with marker files, gated by
   `_ready()` (T1–T4 modules + `impact.PLANNING`). Currently 10 skipped;
   orchestrator must confirm 0 skipped post-merge.
9. **Contracts tests**: barrier constants/helpers, `dynamic` default/bool/
   repr, `RunRequest.deselect` validation (unsafe, non-scoped, >200k).

## TDD evidence

- Red: new engine tests → 8 collection errors (no module); config
  `dynamic` tests → 3 failed with `config.py` stashed
  (`test_selection_dynamic_parses_bool[false]`,
  `renders_only_when_false`, `render_round_trips`), 11 passed;
  CLI dynamic tests → red on missing `cli._selection_api`.
- Harness probe (throwaway, deleted): full run was exit 70
  (`changed-during-run … path classes: untracked`) until test artifacts
  (`ran.txt`, `__pycache__/`, `.pytest_cache/`) were committed to
  `.gitignore`; then green. E2E fixture carries that `.gitignore`.
- Green (all from worktree root, `ptest <paths>`):
  `test_config+contracts+changed_default+selection_engine+history_compat+e2e` →
  488 passed, 10 skipped; `test_cli+test_changed_explain` → 407 passed;
  `test_operations` → 115 passed; neighbors
  `test_impact+test_monorepo_changed+test_history` → 236 passed.

## Deliberate deviations (2)

1. Non-bool `dynamic` (and unknown `[selection]` keys) **fail closed**
   (selection disabled + `policy-invalid` warning), not `invalid-config`
   exit 2. Reason: `_parse_config` documents "Invalid policy strings must
   not invalidate independently safe execution" and committed tests pin
   this for `full_ratio = true`; `dynamic` behaves identically
   (`tests/ng/test_config.py: test_selection_dynamic_non_bool_disables_selection`).
2. Setup-failed runs skip store interaction entirely (operations call site
   lives inside `if native_runner and not setup_failed:`). Reason: no test
   executed, so prior records stay valid; invalidating good records over a
   broken setup would be harmful. Guard/cancel/changed/incomplete paths
   follow D10 literally (mark failed/error, invalidate only when files are
   missing/invalid, cancelled writes nothing but still cleans up).

## Integration notes (seams for T1–T4 / orchestrator)

- Engine is fully duck-typed (`getattr` + `dataclasses.replace` guarded by
  `is_dataclass`): pre-merge it passes stub/0.4 impacts through untouched,
  which is why all existing routing tests stay byte-identical.
- `_full_text` checks the planner `full_reason` before the D8 ratios so an
  explicit planner verdict (e.g. `ambient data file … changed`) is never
  masked by a coincidental ratio; text variants otherwise match 2.8.
- `preview` is consumed by the T6 harness; `status_lines` takes
  `(declaration|None, config)` pairs so monorepo children resolve by
  child `project_id` (pinned in `test_monorepo_refinement_uses_child_project_id`).
- `ptest uninstall` removing `selection.db` is T3-owned (`uninstall.py`
  not in my file set) — not implemented here.
- E2E predictions that encode T2/T4 behavior (case 2 `{"limit"}` via the
  R2 getattr blind spot; case 3/9/10 unit kinds; case 5 reached-count
  left unpinned deliberately) need post-merge confirmation; the module
  documents its boundary and gates on `_ready()`.
- `adapters/pytest.py`, `history.py` intentionally unchanged (env/deselect
  ride `PreparedRun.env_updates`; no new persisted fields).

## Pass 1 — re-read findings (fixed)

- Dead `if cache is None: pass` and doubled `except ImportError/Exception`
  in engine planning/store open — removed.
- Engine/user duplicated byte/unit formatters — consolidated into
  `progress.format_store_bytes` / `summarize_units`.
- `contracts.py` diff verified barrier-only (6 hunks = edits A–F).

## Honest closing

- Implemented: barrier, config key, engine, CLI routing/notes/status,
  operations env+ingest, progress formats, all owned tests + N9 + gated E2E.
- Verified: all runs above, all green in-session; policy digest prefix;
  E2E skips (10) pre-merge as designed.
- Not verified: E2E dynamic assertions (need merged T1–T4; orchestrator
  gate); `ptest --full` (final integrated gate, not run per instructions).
- Deferred: uninstall store removal (T3); T6 docs/harness.
- Discovered but not fixed: none in owned scope.
- Owned vs touched: diff touches only the 16 owned files (5 existing
  sources + 1 new + 7 test files + 3 new test files); `history.py` and
  `adapters/pytest.py` owned-but-unchanged as designed.
- Confidence: high — every acceptance line maps to a passing test except
  the post-merge E2E bodies, which are written, gated, and harness-proven.

---

# T5 fix report (review findings, 2026-10-07)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T5
- taskBranch: feature/dynamic-selection-T5
- parent commit: bde6adf
- Method: reproduced each finding by merging feature/dynamic-selection-T1..T4
  onto bde6adf in a scratch worktree (/tmp/t5scratch, since removed) with its
  own `uv sync --locked --extra test` venv; iterated there; all fixes live in
  the task worktree. Scratch ran the repo's own `ptest` runner only.

## 1. [HIGH] E2E fixed and proven on the merge

- (a) Projects now created via `case.project(domain, kind="pytest")` (inside
  `domain.root`, the `test_pytest_scoped_subprocess` pattern); the second
  worktree goes to `domain.root / "wt2"`. No more `unsafe-path` setup errors.
- (b) Tests split into three files (`test_core.py`, `test_limit.py`,
  `test_misc.py`) so narrowing selections stay under the D8 `full_ratio`
  file conversion; grouped files still prove node-level deselect
  (same-file `test_beta` deselected in case 1, `test_glimit` in case 2).
  Data file moved to `tests/data/config.json` (inside the code area —
  `impact` drops `data/` files no test could reach, which was the
  "no changes since last green run" mechanism). Empty-store case edits a
  test file (a `core.py` edit yields a static *full* verdict whose note
  carries no static reason by design).
- Two assertions corrected to true merged behavior, both still proving the
  acceptance item: case 1 selects `{alpha, helper, checked}` (recorded
  transitive callers through `helper_alpha` and `checked("alpha")`; beta,
  limit, glimit, data excluded); case 3 (effectful) selects all 7 and D8
  converts to full — pinned as `→ full suite: 7 of 7 recorded tests reach
  full_ratio 0.7 (dynamic)` plus the `-v` `changed module pkg/core.py`
  mechanism line, with `-v` added to that invocation.
- Evidence (scratch, merged T1-T4): `ptest
  tests/ng/test_selection_e2e.py` → `10 passed` (twice, plus once more in
  the 994-file run below); no skips. Before the fix the same run gave
  `1 failed, 9 errors`, reproducing the report.

## 2. [HIGH] Env-matrix test is merge-aware (D7 contract)

- `test_selection_record_env_matrix` no longer asserts `binding is None`
  unconditionally: with `ptest.selection_ingest` present it asserts the
  binding equals `C.selection_deselect_path(report)` with the env var set
  and the file on disk; without it, the old no-bind assertions hold. The
  report path moved to `tmp_path` so the merged tree leaves no binding
  litter in the project.
- Evidence: worktree `ptest tests/ng/test_operations.py` → 116 passed;
  scratch (merged) owned-set run → 994 passed (includes this test taking
  the hard branch).

## 3. [MEDIUM] Cleanup runs in `finally` on every outcome

- Per design section 3 ("Its `finally` calls `selection_ingest.cleanup`")
  and the `stack_dumps.cleanup` precedent: new `selection_engine.cleanup`
  (never raises, no-op without ingest/None path), called in `execute()`'s
  outer `finally` next to `stack_dumps.cleanup`, guarded by
  `report_binding is not None`. The old `engine._after_run`-finally cleanup
  is removed (single owner).
- New `test_selection_ingest_cleanup_runs_on_setup_failure` drives a real
  setup-failing execute: `after_run` never runs, `cleanup` runs exactly
  once. Mutation check: with the operations hunk stashed the test fails;
  with it, it passes. Engine tests pin `after_run` no longer cleaning and
  `cleanup()` directly (incl. never-raises without ingest).

## 4. [MEDIUM] Static reasons reachable; first run correct

- `_open_store_reason` classifies open failures: T3 `state-unavailable`
  ending in "does not exist" (never-created project dir or db) → `no
  dependency records yet — any run records them` (spec 6.6); every other
  failure → `dependency store unavailable`. Coupling to T3's wording is
  documented at the call site; only T3 can do better (no `exists` API).
- `_after_run` now persists bridge inactivity via
  `store.note_inactive(run.inactive_reason, run.python)` (create=True,
  status-only, skipped when both are None), making the `test Python 3.N
  ...` and `recording was unavailable: ...` reasons reachable. Successful
  ingest clears stale inactivity with `note_inactive(None, None)`.
- Tests: missing-store → no-records (kind stays selected);
  corrupt-store → store-unavailable; inactive run → `note_inactive`
  recorded, no update, no engine cleanup; success → stale cleared.
- Evidence: `ptest tests/ng/test_selection_engine.py` → 66 passed.

## 5. [LOW] Frozen 2.8 strings

- `newest X ago ago` → `newest X ago` in `progress.format_selection_store`
  and `engine._detail_lines` (template keeps the single `ago`; real ages
  now pinned: `5.0s`, `1.0s`).
- `summarize_units`: `N unrecorded test files` without "changed"; other
  kinds keep it.
- Correct singular/plural via `C.plural` in the dynamic start line
  (`3 tests in 1 file of 8 files`), the `-v` engine line, per-unit
  `→ N test(s)` lines, `recorded N test(s)`, and `-v` audit checked
  counts. `C.plural` cannot form `miss(es)`/`process(es)` (would render
  `misss`/`processs`), so two documented three-line helpers cover those.
- Updated pins in `test_changed_default.py` (incl. a new singular-counts
  routing test and a corrected escaping stub) and
  `test_selection_engine.py` (incl. a direct progress-formats test).
- Checked T6 docs for verbatim buggy strings (`ago ago`, `1 tests`,
  `1 processes`, `1 misses`, `1 failing tests checked`): none present,
  so no doc drift from these fixes.

## Verification (commands from the worktree root unless noted)

- Worktree (pre-merge): `ptest tests/ng/test_config.py
  tests/ng/test_contracts.py tests/ng/test_changed_default.py
  tests/ng/test_selection_engine.py
  tests/ng/test_selection_history_compat.py tests/ng/test_selection_e2e.py
  tests/ng/test_cli.py tests/ng/test_changed_explain.py
  tests/ng/test_operations.py tests/ng/test_impact.py tests/ng/test_history.py`
  → `1243 passed, 10 skipped` (skips are the pre-merge E2E readiness
  gate, as designed), exit 0.
- Scratch (merged T1-T4 + these fixes): owned 8 files →
  `994 passed`, exit 0; T1-T4/neighbor files (`test_selection_planner`,
  `test_selection_store`, `test_selection_ingest`,
  `test_selection_recorder`, `test_selection_bridge_subprocess`,
  `test_impact`, `test_source_index`, `test_selection`, `test_history`,
  `test_changed_explain`) → `543 passed`, exit 0.
- `ptest --full` not run (final integrated gate, per instructions).

## Honest closing (fix turn)

- Implemented: all five findings (E2E in-domain multi-file fixture with
  true merged assertions; merge-aware env-matrix test; cleanup moved to
  `execute()` finally; reachable + first-run-correct static reasons with
  inactivity persistence; 2.8 singular/plural/ago strings).
- Verified: every line above with the repo's own runner, pre-merge
  (worktree) and post-merge (scratch, since removed); E2E 10/10 with
  0 skipped on the merge; mutation check on the cleanup test.
- Not verified: `ptest --full` (someone else's gate).
- Deferred: none from the findings.
- Discovered but not fixed (out of scope, for the integrator): D8
  `full_suite` lines keep frozen plural wording (`1 of 1 ... reach`) —
  changing them would diverge from the frozen 2.8 text and needs verb
  agreement, not just `C.plural`; `_open_store_reason` sniffs T3's
  "does not exist" message (no `exists` API on T3's side); E2E case 1
  documents transitive reach (`helper`, `checked`) as true T2/T4
  behavior — if the chain wants exactly `{alpha}`, the fixture (not T5
  glue) would need restructure.
- Owned vs touched: the 8 files in this diff (4 sources, 4 test files),
  all within the T5 file set from the prior turn.
- Confidence: high — each finding was reproduced on the merged tree
  before fixing and is pinned by a test that fails without its fix.

---

# T5 fix report, round 3 (dynamic start line, 2026-10-07)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T5
- taskBranch: feature/dynamic-selection-T5
- parent commit: bc151ab
- Finding: the round-2 plural fix wrapped F in `C.plural(..., 'file')`,
  rendering `3 tests in 1 file of 8 files` / `1 test in 1 file of 1 file`.
  Frozen 2.8 template and spec 6.6 require `{T} tests in {F} of {M} files`
  (e.g. `214 tests in 31 of 716 files`).
- Fix (`src/ptest/cli.py:3066-3069`, `_impact_note` dynamic selected
  branch): pluralize only T and M —
  `f"{C.plural(T,'test')} in {F} of {C.plural(M,'file')}"`.
  Renders: `214 tests in 31 of 716 files`, `3 tests in 1 of 8 files`,
  `1 test in 1 of 1 file` (sanity-checked via `uv run python -c` against
  `C.plural` directly).
- Test pins updated (`tests/ng/test_changed_default.py:690,707`); no other
  pins referenced the F-with-unit form (grep for `tests in`/`test in`
  shows only these two).
- Verification (from worktree root): `ptest
  tests/ng/test_changed_default.py tests/ng/test_selection_engine.py
  tests/ng/test_cli.py` → `480 passed`, exit 0. Bare `ptest` reports
  `no changes since last green run — nothing to test` (change is covered
  by the scoped run above). `ptest --full` not run (final integrated
  gate, per instructions).
- Honest closing: implemented — the one HIGH finding, nothing else.
  Verified — scoped ptest run green plus direct render check of all three
  count forms. Not verified — `ptest --full`. Deferred — none.
  Discovered but not fixed — none. Confidence: high — two-line change
  restoring the frozen template verbatim.
