# Progress — T4: fake runner/executable + subprocess pytest project factories

Worktree: `/home/ingmar/worktrees/ptest/cc-ptest-parallel-suite/ptest-T4`
Branch: `feature/ptest-parallel-suite-T4` (base `fadb2ab` + `shared: apply frozen
parallel-suite bundle` as first commit).

## Per-file counts (collect-only before/after; sorted node IDs diffed empty)

| file | before | after | ptest run |
|---|---|---|---|
| test_pytest_scoped_subprocess.py | 77 | 77 | chunk F green with full file (122 passed, 11 skipped) |
| test_pytest_full_subprocess.py | 56 | 56 | chunk F green with scoped file |
| test_pytest_parallel_subprocess.py | 27 | 27 | 27 passed |
| test_pytest_parallel_coverage_subprocess.py | 7 | 7 | 10 passed with xdist_serial |
| test_pytest_xdist_serial_subprocess.py | 3 | 3 | 10 passed with coverage |
| test_task_11d_pytest.py | 33 | 33 | 72 passed with run_output |
| test_task_11f_pytest.py | 21 | 21 | 37 passed with acceptance |
| test_pytest_adapter.py | 378 | 378 | 725 passed with simple_adapters |
| test_pytest_bridge_unit.py | 4 | 4 | 79 passed with help + vitest_adapter |
| test_vitest_adapter.py | 14 | 14 | 79 passed with help + bridge_unit |
| test_simple_adapters.py | 347 | 347 | 725 passed with pytest_adapter |
| test_parallel_output_cli.py | 19 | 19 | 19 passed |
| test_run_output.py | 39 | 39 | 72 passed with 11d |
| test_acceptance.py | 16 | 16 | 37 passed with 11f |
| test_help.py | 61 | 61 | 79 passed with bridge_unit + vitest_adapter |
| test_operations.py | 96 | 96 | 96 passed |

Total owned: 1198 collected, all passing. No skip/xfail/deletion; every
assertion and test id preserved.

## Factories (new `tests/ng/factories_exec.py`)

`fake_bin`, `fake_exec(name, script, *, bin_dir=None)`,
`fake_pytest_project(*, tests, git=True, **toml)` (toml → `support.ptest_toml_text`,
`kind` defaults to `"pytest"`), `recording_script` builder plus
`fake_exec_vitest` / `fake_exec_go` / `fake_exec_cargo` /
`fake_exec_node` / `fake_exec_claude` (moved `FAKE_NODE_SCRIPT`,
`FAKE_CLAUDE_SCRIPT`, `FAKE_CLAUDE_DB_GAP_SCRIPT` verified byte-identical
to the originals via AST extraction). Fixture names use only the
`fake_exec` / `fake_bin` / `fake_pytest_` / `exec_` prefixes. The module
imports only the stdlib, pytest and `support`.

## Helpers deleted (all callers migrated)

- `_commit_fixture` (scoped + full subprocess) → `support.init_git_repo`
- `_commit` (parallel_output_cli, xdist_serial) → `support.init_git_repo`
- `_git_command_project` / `_git_pytest_project` git blocks (operations)
  → `support.init_git_repo` (`_git_pytest_project` stays as a composer)
- `_fake_node` + `_FAKE_NODE_SCRIPT` (operations) → `fake_exec_node`
- `_install_fake_claude` / `_install_fake_claude_with_db_gap`
  (parallel_output_cli) → `fake_exec_claude`
- `_write` + `_materialize_persea_fixture` (both twin files) →
  `fake_pytest_project` (persea fixture `.txt` files verified utf-8
  round-trip clean before switching copyfile → read_text)
- benchmark candidate stubs (acceptance, 2 tests) → `fake_exec`
- `sys.modules["pytest"] = None` windows (6 adapter tests) →
  `_hide_pytest_module` context manager (see bug note)

Kept (not duplicates of any available factory; inputs, not builders):
`_project`/`_native_project`/`_command_project`/`_pytest_project`/
`_setup_project`/`_vitest_project` (case-based, custom configs),
`_materialize` (xdist_serial fixture copies — subject content),
`_monorepo_root` (run_output; needs T3's `monorepo` factory — seam, see below),
`_install_fake_pytest` (sys.modules mock, not an executable),
`_node_record`/`_worker_lines`/`_read_launches` (assertion readers).

## Isolation fixes

- `test_parallel_output_cli.py` SIGINT test: `os.chdir` → `monkeypatch.chdir`
  (fixture added to signature).
- `test_task_11d_pytest.py::test_normal_domain_setup_bootstraps_account_coordinator`
  (the only real `uv lock`/`sync` test): `@pytest.mark.xdist_group("uv-cache")`
  with reason comment. REAL_TOOL_ENV is inherited via `isolated_env`.
- Doctor tests `test_doctor_persea_shaped_monorepo` and
  `test_doctor_det1_deterministic_rows_cite_child_config`: under isolation the
  passwd-home state domain has no `coordination/` dir, so history validation
  fails and TIMING-001 reports "history unavailable" (on base it read the real
  account domain, which exists but holds no rows for the fake project ids, so
  it reported "no timing history yet"). Both tests now build explicit empty
  state (`_empty_state`: `state_dir/coordination` 0700 + `PTEST_STATE_DIR`)
  and the original assertions hold unchanged.
- `test_run_output.py::test_command_runner_never_gets_forwarded_verbose`:
  the `-v runner:` line is clamped to 110 columns, so a worktree-qualified
  venv interpreter path (97 chars here) pushes the literal tail past the trim
  point — fails in every suite worktree, including on the pristine bundle
  commit. That one test now uses the short stable `/usr/bin/python3`
  interpreter (fixture is stdlib-only); the full literal comparison is
  unchanged, so the no-forwarding guarantee is intact.
- `/tmp/` literals: all remaining hits are refused-value/arg payloads that
  never touch disk (each annotated); scoped `mktemp` hit is a docstring recipe.
  No fixed ports (only `host:1234` / `gw*` payload strings).

## Bug: xdist worker crash from `sys.modules["pytest"] = None`

Six `test_pytest_adapter.py` tests poisoned `sys.modules["pytest"]` for the
whole test body. Under ptest the bridge's `pytest_runtest_logreport` hook is
registered in the outer xdist worker; it starts with
`from pytest import UsageError`, which raises ModuleNotFoundError while the
module is hidden — killing the worker mid-session (observed: abort at
502/725, crashitem `test_run_rejects_pypy_before_importing_pytest`).
`_hide_pytest_module` scopes the hiding to the guarded `run()` call and
restores the module before return; assertions and ids unchanged. The six
fixed tests are the regression coverage (any leak crashes the worker).
Observed authentic failure first (`/tmp/t4-chunkB5.log`), then fixed, then
725/725 green.

## Verification runs (`.venv/bin/ptest`, chunked, 4 workers, coverage on)

All chunks green (footer scheduler lines like `ownership-uncertain` /
`protocol-mismatch` on some runs are outer-coordinator contention with
sibling task worktrees, not test results; the pytest tallies below are final):

- help + bridge_unit + vitest_adapter: 79 passed
- simple_adapters + pytest_adapter: 725 passed
- acceptance + 11f: 37 passed
- xdist_serial + coverage twins: 10 passed
- parallel twins: 27 passed
- full + scoped subprocess: 122 passed, 11 skipped (version-gated tuples)
- 11d + run_output: 72 passed
- operations: 96 passed
- parallel_output_cli: 19 passed
- verbose single (after clamp fix): 1 passed

Chunk F flakiness log: five attempts failed 9/5/5/2/7 tests with varying
subsets, always nested-invoke exit 70 with empty stdout
(`guard handoff was incomplete` / `coordinator-unavailable`). The pristine
bundle commit (`9e55c6d`, separate worktree, own venv) failed the same chunk
5/117/11 with the same signature — load contention across the five
concurrent task worktrees, not this diff. Sixth attempt: 122 passed,
11 skipped.

## No-gw verification

Nested serial run (`test_xdist_addopts_init_serial_then_scoped_run`, `-n 0`)
stdout captured via a temporary file probe (reverted afterwards): serial
collection output, zero `gw[0-9]` worker ids outside the outer `popen-gw0`
tmp path. conftest D4 stripping + the twins' `_SCRUB` list hold.

## Timing-sensitive 3x evidence (under `-n auto`, one `-k` selection)

`test_parallel_ctrl_c_leaves_no_survivors`,
`test_ctrl_c_kills_parallel_workers`,
`test_normal_domain_setup_bootstraps_account_coordinator`,
`test_pytest_setup_timeout_blocks_execution_with_truthful_outcome`,
`test_parallel_coverage_killed_worker_is_not_passed_and_incomplete`:
5 passed in 14.20s / 13.64s / 13.58s, existing bounds untouched.

## Seams / integration notes

- `_monorepo_root` (test_run_output.py) still hand-builds the monorepo; it
  should adopt T3's `monorepo` factory at merge if signatures align (T3 owns
  `factories_repo.py`, absent on this branch).
- `fake_exec_vitest` / `fake_exec_go` / `fake_exec_cargo` are provided per the
  contract but have no callers yet: the go/cargo suites only assert
  preparation/deferral and never execute a binary.
- `import time` in `test_pytest_parallel_coverage_subprocess.py` is unused
  on base too; left alone.
