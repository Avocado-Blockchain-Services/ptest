# T2 report — route bare ptest / --changed through impact

- taskWorktree: /home/ingmar/worktrees/ptest/cc-changed-by-default/ptest-T2
- taskBranch: feature/changed-by-default-T2
- commit: fd52674 ("Route bare ptest/--changed through impact selection")
- status: done (scoped suite green; one integration seam noted below)

## What changed (owned files only, 9 files)

- `src/ptest/contracts.py` — `RunRequest` gains trailing `changed_note: str | None = None`
  (validated with `_check_str` when not None) and `next_hint: bool = False`
  (validated with `_check_bool`), per design §4.3.
- `src/ptest/progress.py` — adds `NEXT_FULL`, `NEXT_FIX`, `next_step(status, narrowed)`,
  `format_impact(project, note)`, `format_nothing_changed(label)`; `format_end` gains
  keyword-only `next_step`, appended after `(exit N)`, suppressing the `-v` HINT.
  Old helpers (`format_changed_selected`, `format_changed_start`,
  `explain_changed_full_reason`) KEPT — still reachable via `operations.execute` AUTOMATIC.
- `src/ptest/operations.py` — `_emit_start` prints `ptest: <project> · <note>` (+ `-v plan`
  line) when `changed_note` is set; `_emit_end` computes
  `next_step(status, mode is SCOPED)` when `next_hint`, claiming the `-v` hint only
  when the next-step is None.
- `src/ptest/cli.py` — `_impact_note` (exact §4.5 wording, paths/label/reason escaped),
  `_impact_run_request` mapping (selected→SCOPED files / vitest→SCOPED
  `('--changed', sha or 'HEAD')` / full→FULL `changed → full suite: reason` /
  none→skip, all with `base=None`), `_run_impact_standalone` (own `next_hint=True`;
  none-with-changes prints the no-tests line, none-empty prints nothing-changed, exit 0,
  no execute), `_run_impact_monorepo` (base+changed computed ONCE; per-child plans;
  all-none-empty → only nothing-changed, no total; children `next_hint=False`; total
  carries `next_step(worst, narrowed)`), `_run_monorepo_automatic` fallback for
  shadow/probe at a monorepo root (keeps the AUTOMATIC engine, flags carried through).
  Routing condition standalone: `mode is AUTOMATIC and not shadow and probe is None`.
  `_run_doctor_fix` prints the §4.8.3 line; no `record a baseline` remains in cli.py.
- `src/ptest/monorepo.py` — deleted per D10: `ChangedChild`, `_COMMIT_RE`,
  `_child_baseline_head`, `child_baseline_heads`, `_committed_since`, `_run_all`,
  `select_changed_children`, `_classify`, `_vitest_base`, `child_changed_request`,
  and the now-unused `selection._matches` import. Kept `_git_blob`, `_repo_path`,
  `_nul_paths`, `worktree_changed_files`.
- `tests/ng/test_changed_default.py` (new, 28 tests) — routing pinned with a
  `sys.modules["ptest.impact"]` stub: request mapping + notes for selected/full/vitest,
  single-file counters, HEAD fallback, none/nothing lines + exit 0 + no execute,
  `--base` forwarding, bad-`--base` Problem, shadow/`--full` bypass, monorepo
  per-child lines + total hint + failure hint, `next_step` matrix, `format_end`
  hint suppression, real `_emit_end`/`_emit_start` wiring, `RunRequest` validation,
  doctor `--fix` §4.8.3 line.
- `tests/ng/test_monorepo_changed.py` — rewritten to impact routing (stubbed);
  `worktree_changed_files` + skip-line style tests kept verbatim.
- `tests/ng/test_natural_loop.py` — section 1 rewritten (bare≡--changed now means
  identical SCOPED requests; clean monorepo root → nothing-changed, no total).
- `tests/ng/test_run_output.py` — bare invokes that only needed "run the project"
  now pass `--full` (14 sites); `-v plan: full · mode automatic` pinned in-process
  via `operations.execute(AUTOMATIC)`; quiet-refusal now pins the full-gate
  `native-config-invalid` refusal (comment explains the route).
- `tests/ng/test_changed_explain.py` — untouched, still green (AUTOMATIC path kept).

## Verification (observed)

- `test_changed_default.py` red first: 24 failed / 3 passed before implementation.
- Final: `.venv/bin/ptest --workers 2 tests/ng/test_changed_default.py
  tests/ng/test_monorepo_changed.py tests/ng/test_changed_explain.py
  tests/ng/test_natural_loop.py tests/ng/test_run_output.py` → **130 passed**
  (worktree `/home/ingmar/worktrees/ptest/cc-changed-by-default/ptest-T2`, exit 0).
- `grep -rn "child_baseline_heads\|select_changed_children\|child_changed_request" src tests`
  → empty. `grep -rn "record a baseline" src/ptest/cli.py` → empty.
- Never ran `--full` suite, never pushed; no migration; no live/external tests.

## Integration note (seam for the controller / T1)

- `src/ptest/impact.py` did not exist in this worktree, so `cli.py` imports it lazily
  (`_impact_api()`: `from . import impact` at the routing boundary) against the frozen
  §4.1 interface (`git_top` / `resolve_base` / `changed_files` / `plan`, `Base`/`Impact`
  fields, reason strings). All routing tests stub `sys.modules["ptest.impact"]`; none of
  the committed tests need the real module. After merge with T1's `impact.py`, the
  `test_run_output.py` bare→`--full` migrations stay valid (they pin mode-agnostic
  output), and bare runs will additionally produce the new start/nothing lines —
  no test asserts the old bare start line anymore.
- `_run_monorepo_automatic` (shadow/probe at monorepo root) is new small behavior:
  previously those flags were silently dropped in the changed loop; now each child runs
  AUTOMATIC with the flags carried through. No test covers it (same as before).
- `RunRequest` has two new fields (`changed_note`, `next_hint`) per design §4.3/D-deviation-3,
  not one — the task bullet saying "only `changed_note`" is superseded by the frozen §4.3.
- dan-jefferies passes: re-read full diff (fixed a redundant `_impact_api()` call;
  added the bad-`--base` test); re-anchored every acceptance bullet above;
  smell sweep clean (escaping via `render.terminal_text`, fail-closed unknown kinds→FULL,
  no new deps). Confidence: high for routing/mapping/output; medium for graph interplay
  (real `impact.py` arrives via T1 — interface conformance is the controller's merge check).

## Fix report — sys.modules stub ignored after T1 merge (audit finding, 2026-09-27)

- Cause: the three routing-test stubs installed the fake via
  `monkeypatch.setitem(sys.modules, "ptest.impact", mod)`, but
  `cli._impact_api()` (`src/ptest/cli.py:2789`) does `from . import impact`,
  which reads the `impact` attribute off the `ptest` package before consulting
  `sys.modules`. T1's `tests/ng/test_impact.py` does
  `from ptest import impact as I` at module level, so on any merged run the
  real module is already bound and every stub was silently ignored (real
  `resolve_base` answered, e.g. `no changes vs HEAD` instead of
  `no changes vs origin/dev`).
- Fix (test-only, 3 files, no src change): each stub helper now also does
  `monkeypatch.setattr("ptest.cli._impact_api", lambda: mod)` —
  `tests/ng/test_changed_default.py:_install_impact`,
  `tests/ng/test_monorepo_changed.py:_install_impact`,
  `tests/ng/test_natural_loop.py:_stub_impact`. The `sys.modules` entry is
  kept for direct lookups; the setattr is the binding that matters. Module
  docstrings updated to say seam instead of sys.modules-only.
- Proof on a scratch T1+T2 merge (/tmp/t1t2-merge: T2 HEAD + T1's
  `src/ptest/impact.py`, `tests/ng/test_impact.py`, and the `--no-renames`
  `worktree_changed_files` hunk, own `.venv` via `uv sync`):
  - control with the setattr lines stripped (old stubs):
    `.venv/bin/ptest --workers 2 tests/ng/test_impact.py
    tests/ng/test_changed_default.py tests/ng/test_monorepo_changed.py
    tests/ng/test_natural_loop.py` → `23 failed, 85 passed` (matches the
    finding exactly; `2 workers [108 items]`).
  - with the fix, same command → `108 passed` (`2 workers [108 items]`,
    exit 0).
  - `.venv/bin/ptest --workers 2 tests/ng/test_impact.py` on the merged tree
    → `52 passed`, exit 0 (T1 suite unaffected).
- T2 isolation still green:
  `.venv/bin/ptest --workers 2 tests/ng/test_changed_default.py
  tests/ng/test_monorepo_changed.py tests/ng/test_changed_explain.py
  tests/ng/test_natural_loop.py tests/ng/test_run_output.py` → `130 passed`
  (worktree `ptest-T2`, exit 0).
- dan-jefferies passes: re-read full test-only diff (caught one stale comment
  saying "instead" while both bindings are kept — reworded); re-anchored
  against the finding (seam stub in all 3 files + merged 4-file run +
  test_impact.py run + report append); smell sweep — no new helpers/deps,
  double-install in `_route_standalone` harmless (second setattr wins, both
  undone at teardown), control run proves the tests are non-vacuous
  (23 failures unfixed → 0 fixed). No src/ contract drift (`_impact_api`
  signature untouched).
- Status: implemented (3 test files) / verified (108 merged + 52 impact-only
  + 130 T2-isolation, all exit 0) / not verified (full `--full` suite — never
  run, per scope) / deferred (none) / discovered-but-not-fixed (none).
  Confidence: high — the failure reproduced byte-identical pre-fix and is
  fully green post-fix.
