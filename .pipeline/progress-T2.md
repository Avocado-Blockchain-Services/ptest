# Progress — T2 (isolation foundation, parallel config, domain/state factories)

Worktree: `/home/ingmar/worktrees/ptest/cc-ptest-parallel-suite/ptest-T2`
(branch `feature/ptest-parallel-suite-T2`, base `aa34d2a`).

## Bundle

- First commit `e08735c` (`shared: apply frozen parallel-suite bundle`)
  applied `.pipeline/shared-bundle.patch` verbatim.
- Bundle amendments afterwards: **none**. `git diff e08735c -- {pyproject.toml,
  .ptest.toml, tests/ng/conftest.py, tests/ng/support.py}` is empty.

## Parallel header evidence

`.venv/bin/ptest tests/ng/test_config.py`:

```text
ptest: ptest-T2 · pytest · 4 workers · tests/ng/test_config.py
============================= 169 passed in 9.41s ==============================
ptest: passed · 169 tests · 17.4s
```

Coverage `TOTAL` line prints on every scoped run
(`--cov=ptest --cov-report=term` in `.ptest.toml [runner] args`).
Multi-worker distribution lines observed, e.g.
`created: 4/4 workers` / `4 workers [28 items]`.
In-test xdist proof: `test_isolation.py::
test_session_guard_registered_on_each_worker` asserts
`worker_id.startswith("gw")` (passes).

## `--dist loadgroup` vs `loadfile` (same chunk, `test_resources` + `test_storage`, 28 tests)

| dist | result |
|------|--------|
| `loadfile` (temporary local edit, reverted; `pyproject.toml` clean afterwards) | 28 passed in 9.16s |
| `loadgroup` (committed config) | 28 passed in 7.29s |

`loadgroup` kept: behaves like `load` for ungrouped tests, honours
`xdist_group`, and was faster on the measured chunk. Single measurement
each way (shared machine, sibling tasks running); indicative, not a
benchmark.

## Full-mode anchor (collection only, no tests run)

```text
$ .venv/bin/python -m pytest --collect-only -q -p no:cacheprovider tests
3933 tests collected in 17.71s   (exit 0; base 3920 + 13 new test_isolation tests)
$ .venv/bin/python -m pytest --fixtures -q -p no:cacheprovider tests | grep -E "domain_factory|account_home|state_dir_factory"
domain_factory / account_home / state_dir_factory — all listed from tests/ng/factories_domain.py
$ grep -rnE "^[[:space:]]*pytest_plugins[[:space:]]*=" tests/ --include="*.py"
(no matches)
```

## Per-file counts (`.venv/bin/ptest` output; before = base+bundle, after = migrated)

| file | before | after |
|------|--------|-------|
| test_uninstall.py | 65 passed + 1 failed (`test_self_plans_only_the_argv_fixture_root_never_real_paths`, the known probe failure) | **66 passed** |
| test_state_directory.py | 30 passed | 30 passed |
| test_install.py | 16 passed | 16 passed |
| test_platform.py | 53 passed | 53 passed |
| test_files.py | 69 passed | 69 passed |
| test_scheduler.py | 165 passed | 165 passed |
| test_resources.py | 18 passed | 18 passed |
| test_storage.py | 10 passed | 10 passed |
| test_history.py | 143 passed | 143 passed |
| test_isolation.py (new) | — | 13 passed |
| test_config.py (reference, unowned) | 169 passed | 169 passed |

Chunked after-runs: A (uninstall+state_directory+install+platform) 165
passed; B (files+scheduler+resources+storage) 262 passed; C
(history+isolation+config) 325 passed.

## Migrations

- New `tests/ng/factories_domain.py` (plugin via `pytest_configure`):
  `domain_factory(slots=1, jobs=1)`, `account_home(name="home")`,
  `state_dir_factory(name="state")`; unknown kwargs → `TypeError`.
- New `tests/ng/test_isolation.py` (13 tests): env redirection,
  normal-domain-under-home + env-selected state, root freshness (×2) +
  distinct-builds, control-var strip (+ strip-predicate unit check),
  `_snapshot_path` creation/mtime/relink, `ptest_toml_text` parse for
  command+pytest, `init_git_repo` one commit on main with fixture
  identity, factory delegation/`TypeError`s, per-worker guard
  registration.
- `test_platform.py`: `_make_account_home` deleted (→ `account_home`);
  `_account_home` keeps only the platform-specific `_filesystem_type`
  patch and delegates the passwd home to `support.patch_account_home`.
  `test_explicit_state_directory…` uses `state_dir_factory` + `rmdir`
  to keep proving laziness. `test_normal_domain_rejects_foreign…`
  keeps a documented uid-agnostic override (the factory only fakes the
  current uid; the foreign uid is the subject).
- `test_state_directory.py`: `account` fixture delegates to
  `account_home`; `_git_init_fixture` deleted (→ `support.init_git_repo`);
  valid state dirs via `state_dir_factory`; direct `setenv` kept only
  where the env value/placement/non-creation is the subject
  (invalid values, symlinks, missing parents, writable parents,
  in-checkout placement, laziness probes), each with a subject comment.
- `test_uninstall.py`: `_fake_home`/`_fake_account`/`_workspace_domain`
  deleted; redundant fakes dropped (isolated_env covers), path-needing
  sites use `account_home()`, state via `state_dir_factory`;
  `_isolate_self_discovery` keeps argv/package/PATH fakes, passwd home
  left to `isolated_env`. Probe failure fixed: the self-root assertion
  rejoins renderer-wrapped lines (same idiom as
  `test_self_render_never_truncates_*`), no weakening. Symlinked-home
  test keeps a documented direct patch (factory builds real dirs only);
  `/tmp/` render payloads commented as non-filesystem.
- `test_scheduler.py`: `_normal_domain` deleted (→
  `account_home(name="account")` + `platform.domain_paths(None)` at all
  13 sites).
- `test_files.py`: `MINI_PREFIX_MAIN.WRAPPER_VALUE_OPTS` gains
  `"--timeout"`, matching `support._WRAPPER_VALUE_OPTS`.
- `test_install.py` / `test_resources.py` / `test_storage.py` /
  `test_history.py`: unchanged (all resources already tmp-local,
  ephemeral ports, faked kernels; green under isolation).
- No `xdist_group` marks: no owned test scans or signals processes it
  did not spawn; sockets are ephemeral; the real-`uv` install test
  shares only the concurrency-safe `REAL_TOOL_ENV` caches (D5).
- No skips, xfails, deletions, or weakened assertions. All test ids kept.

## Isolation-grep status (owned files)

- `os.environ[...] =`: none. `os.chdir`: none. `Path.home()`/
  `expanduser`: one assertion about the function itself
  (`test_history.py:1863`). Literal `/tmp/` filesystem paths: none
  (payload strings commented). Fixed non-zero ports: none (ephemeral
  `0` + proxy-to-nowhere `127.0.0.1:1` in install test).
- Remaining direct `PTEST_STATE_DIR` setenvs are value/placement
  subjects (see above); every valid-dir setup uses `state_dir_factory`.
- `test_state_directory.py` imports one T3-owned helper
  (`test_doctor_init_integration._write_db_standalone_repo`,
  `_prepare_review`); pre-existing, left for T3.

## Integration notes (for the orchestrator)

- `test_history.py::_normal_home` untouched: under isolation its
  `real_home` guard now watches the iso home instead of the real home;
  suite passes (143). If T1/T4 touch history-adjacent behaviour, that
  guard's meaning shifted silently — worth a look at merge time.
- `_write_trivial_command_project` / `_write_marker_command_project` /
  `_write_monorepo` (state_directory) and `_git` / `_v1` (uninstall)
  stay: config-subject literals / static boundary markers; T3's
  `factories_repo` is the natural next owner, not T2.
