# T1 progress — configurable + dynamic run deadline

Worktree: `/home/ingmar/worktrees/ptest/cc-ptest-parallel-suite/ptest-T1`
Branch: `feature/ptest-parallel-suite-T1` (from aa34d2a; first commit 57f4f5f
`shared: apply frozen parallel-suite bundle`).

## Bundle

`git apply .pipeline/shared-bundle.patch` + commit `57f4f5f`. Clean tree after.
Bundle header confirmed: `ptest: … 4 workers …` on scoped runs.

## Per-file test counts (`.venv/bin/ptest` output)

"Before" = clean-bundle worktree /tmp/t1-base @ 57f4f5f (own .venv via
`uv sync --locked --extra test`). "After" = T1 worktree with the deadline
change. Counts are inner `pytest` results; the outer ptest verdict under the
5-task parallel load is recorded separately (see notes).

| file | before | after |
|---|---|---|
| tests/ng/test_run_deadline.py (new) | n/a (0) | 67 passed |
| tests/ng/test_guard.py (alone) | not run alone | 76 passed + 2 flaky* |
| tests/ng/test_contracts.py + test_config.py | (in trio below) | 241 passed, clean |
| trio guard+contracts+config | 3 failed, 316 passed (base) | 2 failed, 317 passed* |
| test_help.py + test_cli.py | 374 passed (base) | 374 passed |
| test_operations.py + test_shadow.py | 1 failed, 116 passed (base) | 6 failed→0 on rerun, 111 passed |
| test_run_output + monorepo_scopes + source + history + init_smoke | — | 3 failed→2 rerun-green + 1 path-length pre-existing, 297 passed |
| test_pytest_scoped_subprocess + test_task_11f_pytest | — | 1 failed→rerun-green, 86 passed |
| test_task_11d_pytest | — | 1 failed→rerun-green, 32 passed |

\* guard timing flakes: failing params differ run to run on base and T1
alike (sigkill/cancel/barrier tests); every failing param passes on scoped
rerun in the T1 tree. The one deterministic failure is
test_run_output.py::test_command_runner_never_gets_forwarded_verbose, a
pre-existing path-length truncation (MAX_WIDTH=110, room 92; argv is 94 chars
in the chain worktree and 97 in ptest-T1, so it fails on base at this depth
too — verified arithmetically). Out of scope (T4 owns the file); reported,
not fixed.

## Scoped-run evidence

- `.venv/bin/ptest tests/ng/test_run_deadline.py` → `67 passed`, outer
  `ptest: passed · 67 tests` (clean, twice).
- `.venv/bin/ptest --timeout 300 tests/ng/test_contracts.py tests/ng/test_config.py`
  → `ptest: passed · 241 tests`.
- New tests fail on base: `git grep` on fadb2ab shows zero occurrences of
  `resolve_compound_timeout`, `comparable_run_evidence`,
  `_compound_timeout_message`, `_COMPOUND_TIMEOUT_S`, `full_timeout`,
  `DEFAULT_COMPOUND_TIMEOUT_S`, or `"--timeout"` outside the renamed
  MAX_COMPOUND_TIMEOUT_S lines (red state by construction).
- Live-fire: an outer verification run killed at the dynamic floor printed
  exactly `execution-timeout: compound execution deadline expired after 60s;
  raise it with --timeout SECONDS or [runner] timeout / full_timeout in
  .ptest.toml`.
- Literal acceptance command
  `.venv/bin/ptest --timeout 300 tests/ng/test_run_deadline.py
  tests/ng/test_guard.py tests/ng/test_contracts.py tests/ng/test_config.py`
  → `386 passed` inner (67+78+241); rerun 385 passed + 1 guard timing flake
  (`test_signal_while_waiting_for_attempt_decision_never_launches[pid-15]`,
  green 4/4 on scoped rerun). Outer verdict under parallel load varies
  (environmental handoff flakes, identical on base); inner counts are the
  signal.
- `git diff fadb2ab -- src/ptest/operations.py` shows no `def _launch_guard(`
  / `def _run_guard(` line changes (D12 holds).
- T4-wrapper compatibility: T1's own D12 test installs a positional-only
  `spy(*args)` on `operations._launch_guard` during `operations.execute`
  and observes the resolved value — the same shape as the T4-owned wrappers.
- Isolation greps on owned files return empty: no `os.chdir` /
  `os.environ[...] =` / `Path.home()` / `expanduser(`, no literal `/tmp/`
  filesystem paths, no fixed ports. No ptest subprocess spawns in owned
  files (guard fixtures spawn only owned fixture processes).

## Load notes (machine shared with T2–T5 + other projects, load ~8–10)

- Outer-orchestration flakes (`coordinator-unavailable`,
  `protocol-mismatch: guard handoff was incomplete`, `changed-during-run`)
  reproduce identically on the clean-bundle base worktree
  (/tmp/t1-base-reg1.log: inner 374 passed, outer incomplete with the same
  two problems) — environmental, not T1.
- Inner timing flakes in guard/operations/subprocess tests reproduce on base
  too (base trio: 3 failed/316 passed; base operations chunk: 1
  failed/116 passed) and pass on scoped rerun in T1.
- Verification runs used `--timeout 300/600/900/1200` (the feature's own
  escape hatch) once the dynamic floor (60 s) killed a saturated 319-test
  trio run.
