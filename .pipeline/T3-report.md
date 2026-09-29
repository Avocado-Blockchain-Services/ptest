# T3 report — doctor uncommitted-files finding and init terminal commit reminder

- taskWorktree: /home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T3
- taskBranch: feature/worktree-safe-config-T3
- commit: 9e42ff0 ("T3: config.uncommitted doctor findings and init commit reminder")
- status: done — scoped suite fully green (179 passed)

## What changed (owned files only, 4 files, +463/-0)

- `src/ptest/doctor.py` (+98) — behavior as designed; untouched since review:
  - `_config_findings(root)` (new): one `C.Finding` per path from
    `worktree.uncommitted_config_files(root, include_agent_rules=True)` with the
    frozen shape (`config.uncommitted` / medium / high / `line=None` /
    `git-tree` + frozen consequence/remediation/verification). Best-effort:
    lazy `from . import worktree` plus broad `except Exception -> ()`, so doctor
    never fails — including when the T1 module is absent.
  - `_admit_config_findings(scan, root)` (new): admits config findings FIRST,
    under the same bounds as `scan.source` (finding count + output bytes, with
    `scan.limit("Doctor finding limit reached.")` / `_hit_output_limit()`).
  - `_config_uncommitted_readiness(problem)` (new): blocked execution/selection
    readiness naming the problem's own `config-uncommitted` code + message
    (parallel/timing mirror the unconfigured shape); None when the reason code
    is unavailable, keeping the default.
  - `inspect()`: admits config findings only when scope is None; computes
    readiness from the NON-config findings so readiness is identical
    with/without them; a `config-uncommitted` resolution (config None) gets the
    override readiness. Falls through to the untouched legacy
    `_finalize_report` path whenever neither condition holds.
  - `inspect_workspace()`: monorepo branch admits `_config_findings(root)`
    into the ledger via `_admit_finding` before child source findings, only
    when scope is None. Standalone path untouched (handled in `inspect()`).
- `src/ptest/init_render.py` (+28) — untouched since review:
  - `_commit_reminder_lines(result, width, *, dry_run)` (new): last-block tail
    shown only when not `dry_run`, action is not PREVIEW, and `commit_paths`
    is non-empty. Line 1 `Commit these files: <, -joined>` via
    `wrap_words(width, indent="", hang="  ")`, line 2
    `Worktrees and clones only get committed config.` Paths through
    `terminal_text`. No new renderer parameters; `render_init` unchanged.
  - `render_init_footer()`: appends the block last (after restart, one blank
    line). Reads `result.commit_paths` via tolerant `getattr(..., ())`.
- `tests/ng/test_doctor.py` (+262): 7 behavior tests (exact finding list +
  frozen fields; absent when committed/non-git; absent when scoped; bound
  admits first + limitation; readiness equal to committed twin; resolution
  readiness names code+message; monorepo root+child) PLUS a conditional T1
  seam block (see below).
- `tests/ng/test_init_render.py` (+75): separate `_committed_result` helper
  with `commit_paths` seam fallback (existing `_result` untouched); 4 tests
  (exact last block after restart; absent for empty/PREVIEW/dry-run; wrap at
  width 40 over the 60-column floor; control-char sanitization).

## T1 seam (declared locally, conditional, removable after T1 integrates)

T1 (`src/ptest/worktree.py`, contracts codes/fields) is not integrated on this
base, so the three frozen shapes are declared inside the owned test files only
— shipped `doctor.py`/`init_render.py` behavior is unchanged:

- `test_doctor.py` top block: unions `config.uncommitted` /
  `config-uncommitted` into `C.FINDING_CODES` / `C.REASON_CODES` (guarded, a
  no-op once T1 adds them to the literals), and installs a faithful
  `ptest.worktree` double in `sys.modules` ONLY if the real import fails. The
  double follows design §3.1 verbatim (candidate order, marker rule, single
  `ls-tree -r -z --name-only HEAD`, any-error → `()`), using real git.
- `test_init_render.py` `_committed_result`: tries the real
  `InitResult(..., commit_paths=...)` kwarg first (exercising T1 validation
  post-integration), falls back to `object.__setattr__` pre-T1.
- Post-T1 every declaration dissolves (real module/codes/kwarg shadow it) and
  the committed tests exercise the real T1 shapes with zero T3 changes. Delete
  the marked blocks once T1 is integrated.

## Verification (observed)

- TDD red first: 9 failed / 170 passed before implementation (each failure
  mapped to a missing T1 shape: `ModuleNotFoundError: ptest.worktree`,
  `ValueError: unknown finding/reason code`, `TypeError: commit_paths`).
- Logic proof: `/tmp/t3_probe.py` (throwaway, not committed) drove the REAL
  repo code with a spec-faithful T1 simulation → 19/19 PASS (finding
  list/fields, readiness equality, bound order + limitation, scoped-none,
  resolution codes, monorepo root+child, footer tail/block/absent/wrap/sanitize).
- Final scoped run, same command from the T3 worktree root:
  `ptest --workers 2 --queue-timeout 1800 tests/ng/test_doctor.py tests/ng/test_init_render.py`
  → **179 passed in 35.42s** (`ptest: passed · 179 tests · 6m50s`, exit 0).
- Coverage gate: none (per context pack). No migration generated. No test uses
  timeouts above default; no live/external-API tests. Committed inside the
  worktree only; never pushed; default branch untouched; no deploy.

## Honest closing (dan-jefferies passes)

- Pass 1 (re-read diff): found and fixed one thing before committing — the
  `inspect()` branch initially took the explicit `_evict_to_fit` path whenever
  a config-uncommitted problem was present even with override None;
  restructured to `if admitted_config or override is not None` so the legacy
  path is taken whenever there is nothing new to express. Seeded a probe
  scenario bug of my own (modified-but-tracked file is NOT uncommitted) and
  fixed the scenario, not the code.
- Pass 2 (acceptance): every T3 checkbox in design §T3 plus the task-level
  criteria is implemented and now green, including the `--from-main` footer
  behavior (stopgap warning travels via existing `result.warnings` header
  rendering — no footer work needed).
- Pass 3 (smell sweep): no duplicated concepts; no signatures changed
  (`render_init_footer`, `inspect`, `inspect_workspace` intact, so T2's CLI
  wiring is unaffected); broad `except`s fenced to best-effort helpers and
  backstopped by tests asserting non-empty output; finding construction mirrors
  the existing `scan.source` site. The test double duplicates T1's candidate
  logic by necessity — it is conditional, documented, and marked for removal.
- implemented: all T3 source + tests. verified: 179/179 scoped green plus
  19/19 probe checks on real code paths. deferred: none.
  discovered-but-not-fixed: none in owned files.
- Confidence: high — green on the real suite; post-T1 the seam blocks dissolve
  and the same assertions pin the real integration.

## Fix report (integration repair, 2026-09-29)

Rebased T3 onto the integrated T1 tip and removed every pre-integration
workaround, then fixed the real-helper clock interaction the stub had hidden.

- Base: `git rebase --onto 32de6ed 890e1ee` (T1 tip
  `feature/worktree-safe-config-T1 @ 32de6ed`); replayed the single T3 commit
  cleanly, no conflicts. New head `4ef2e3c` at time of fix (plus this repair).
- Deleted seams/shims (net -157/+26 over the 4 owned files):
  - `tests/ng/test_doctor.py`: removed the whole T1 barrier seam block
    (local `uncommitted_config_files` double, `sys.modules` injection, global
    `C.FINDING_CODES`/`C.REASON_CODES` mutation) and its now-unused
    `stat`/`subprocess`/`sys`/`tomllib`/`types` imports.
  - `tests/ng/test_init_render.py`: `_committed_result` now constructs
    `C.InitResult(..., commit_paths=...)` directly; `object.__setattr__`
    fallback deleted.
  - `src/ptest/doctor.py`: top-level `from . import worktree as worktree_api`;
    lazy import in `_config_findings` deleted (best-effort `except -> ()`
    kept for real git failures); `_config_uncommitted_readiness` tolerance
    `try/except -> None` replaced with an explicit
    `problem.code != "config-uncommitted"` guard.
  - `src/ptest/init_render.py`: `getattr(result, "commit_paths", ())`
    replaced with direct `result.commit_paths`.
- Clock fix (`tests/ng/test_doctor.py`, deadline leg of
  `test_doctor_handles_symlink_swap_output_and_deadline_without_state_access`):
  fake clock is now `lambda: next(ticks, 9.0)` instead of bare `next(ticks)`.
  Reason: the real T1 helper reads the same patched `time.monotonic` (its own
  bounded ~2 s git deadline plus the `source._git` deadline check), so the
  scripted 3-tick sequence was exhausted inside `_run_scan` (`StopIteration`).
  The stub never touched the patched clock because its `subprocess` timeout
  clock is bound at import time — that is why the failure stayed hidden.
  Scripted values still pin scan `began` at 0.0 with "now" at 9.0 (past the
  1 s budget); the saturated 9.0 default keeps that true however many
  readings the helper takes, including zero when there are no candidates.
  Production order unchanged (config findings admitted after `_Scan`
  construction), so `elapsed_s` semantics are untouched.
- Verification (observed, from the T3 worktree root on the integrated tree):
  - `ptest --workers 2 --queue-timeout 1800 tests/ng/test_doctor.py tests/ng/test_init_render.py`
    → **179 passed** (`ptest: passed · 179 tests · 42.6s`, exit 0) — the
    previously failing pre-existing deadline test now passes against the real
    T1 helper, and T3's 7+4 behavior tests now pin the real T1 shapes over
    real git instead of the double.
  - `ptest --workers 2 --queue-timeout 1800 tests/ng/test_worktree.py tests/ng/test_config.py tests/ng/test_init.py`
    → **316 passed** (`ptest: passed · 316 tests · 40.7s`, exit 0).
- Durable coverage: no new test file — the pre-existing deadline test is now
  the regression test for the helper/clock interaction (red on the integrated
  tree before this fix, green after), and T3's existing behavior tests cover
  the real-helper finding path. No live/external-API tests; no long timeouts.
- implemented: rebase + seam/shim deletion + clock fix. verified: 179/179 and
  316/316 scoped gates on the integrated tree. deferred: none.
  discovered-but-not-fixed: `elapsed_s` still includes the bounded git lookup
  (≤2 s) because config findings are admitted after `_Scan` construction;
  reordering would shift `began` past the scripted test ticks, so this stays
  as a documented trade-off, not a defect.
- Confidence: high — both required gates are green on the T1+T3 tree, and the
  previously red pre-existing test is the proof the integration is real.
