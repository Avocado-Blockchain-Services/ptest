# Progress T3 — repo/config/monorepo/venv factories; init, selection, CLI, shadow

Worktree: `/home/ingmar/worktrees/ptest/cc-ptest-parallel-suite/ptest-T3`
Branch: `feature/ptest-parallel-suite-T3`
Base: `fadb2ab` + shared bundle commit (`shared: apply frozen parallel-suite bundle`).

Parallel header observed: `ptest: ptest-T3 · pytest · 4 workers · ...` with
coverage TOTAL printed.

## Per-file test counts (collect-only ids; before == after)

Collected via `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider`
(base files from `git show HEAD:` vs worktree files); the sorted id lists are
identical (828 ids; the only diff line is a scratch-dir artifact
`fixtures/doctor/db_per_test.py::test_db` collected from the copied fixtures
directory, not a real test).

| File | Before | After | ptest run |
|------|--------|-------|-----------|
| test_changed_explain.py | 33 | 33 | 78 passed (chunk with scopes/facts/compound) |
| test_cli.py | 313 | 313 | 313 passed |
| test_compound_profiles.py | 26 | 26 | 78 passed (chunk) |
| test_doctor_init_integration.py | 8 | 8 | 8 passed (after explicit-state fix) |
| test_executability.py | 134 | 134 | 134 passed |
| test_init.py | 59 | 59 | 80 passed (chunk with init_render) |
| test_init_changed.py | 21 | 21 | 21 passed |
| test_init_render.py | 21 | 21 | 80 passed (chunk with init) |
| test_init_smoke.py | 42 | 42 | 42 passed |
| test_monorepo_changed.py | 24 | 24 | 24 passed |
| test_monorepo_scopes.py | 11 | 11 | 78 passed (chunk) |
| test_project_facts.py | 8 | 8 | 78 passed (chunk) |
| test_selection.py | 26 | 26 | 26 passed ×3 consecutive runs |
| test_shadow.py | 37 | 37 | 37 passed (21 passed + 16 pre-existing env skips for the unprovisioned frozen-cov python) |
| test_source.py | 65 | 65 | 65 passed (clean wrapper rerun) |

No skips, xfails, deletions, or weakened assertions. No `os.chdir` without
monkeypatch, no `os.environ[...] =`, no `Path.home()`/`expanduser`, no fixed
ports, no fixed `/tmp` filesystem paths in owned files (remaining `/tmp`
literals are payload-only with comments: `ProviderResult.scratch` in
test_cli via `_REVIEW_SCRATCH`, `CheckoutIdentity.root` in
test_changed_explain, `Config.config_path` in test_compound_profiles).

## Helpers deleted (all callers now use factories or support builders)

- test_monorepo_changed.py: `_git`, `_repo`, `_child_toml`, `_monorepo`
  → `support.git` / `support.init_git_repo` / `monorepo` fixture
  (`_child_specs` composes child kwargs; `ptest.monorepo` used as
  `monorepo_api`).
- test_source.py: `_git`, `_repository` → `support.git` /
  `support.init_git_repo` (raw `merge`/`clone`/`uv` subprocess calls stay;
  they are not builders).
- test_init_smoke.py: `_git` (fake marker) → `fake_git_marker`.
- test_init_changed.py: `_stub_cov_venv` → `venv_stub` fixture
  (`_pytest_repo` threads it through).
- test_executability.py: `_write` → `support.write_file`; `_stub_venv` →
  `venv_stub` fixture (`xdist=` takes one version or a tuple for the
  duplicate-install fallback).
- test_shadow.py: `_commit` → `support.init_git_repo(root,
  message="fixture")` (init is idempotent, covers re-commits);
  `_shadow_project` toml → `support.write_ptest_toml` with `tail=`
  for the `[selection]` block.
- test_doctor_init_integration.py: `_write_child_config` deleted;
  `_write_persea_shaped_monorepo` / `_write_db_standalone_repo` now compose
  `monorepo` / `ptest_project`; two inline pytest tomls →
  `support.write_ptest_toml`.
- test_cli.py: `_monorepo_cli_root` / `_review_project` now compose the
  `monorepo` fixture (payload files stay explicit); inline command/monorepo
  tomls → `monorepo` / `support.write_ptest_toml`; `inspection_project`
  overwrite → `write_ptest_toml`; fake `.git` markers → `fake_git_marker`;
  nine `scratch="/tmp/ptest-review-test"` payloads → `_REVIEW_SCRATCH`.
- test_init.py: directory-variant fake marker → `fake_git_marker`
  (worktree-file variant stays inline: distinct shape).

Kept deliberately (out of T3 scope, seams recorded below):
- `_fake_reviewer`, `_fake_qualified_profiles`, `_fake_cli_executable`,
  `_fake_version`, `_fake_entries`, `_ok_item_launches`, `_one_row_reply`
  (test_cli), `_install_fake_claude` (test_doctor_init_integration),
  `_fake_python_on_path` (test_init_smoke): provider/executable fakes owned
  by T4 (`fake_bin`/`fake_exec`) and T5 (`fake_provider`); those modules do
  not exist on this branch yet.
- `_config` object builders (test_executability, test_compound_profiles,
  test_source): they build `C.Config`, never `.ptest.toml`; no factory
  covers them.
- `_child_root` (test_init), `_pytest_repo` (test_init_smoke,
  test_init_changed): runner-marker/test-file payload builders, not
  git/toml/monorepo/venv builders.
- `version = 999` / `not valid configuration` / drift-mutation tomls and
  the manifest-only rejection test (test_cli): assertion subjects.
- Raw `git merge`/`clone`/`uv` subprocess calls (test_source): behaviour
  under test, not builders.

## Factories (`tests/ng/factories_repo.py`, new)

Function-scoped fixtures `git_repo(name="repo", *, parent=None, files=None,
commit=True, branch="main")`, `ptest_project(name="proj", *, parent=None,
git=False, files=None, branch="main", **toml)`, `monorepo(children, *,
parent=None, name="mono", git=False, branch="main", root_toml=None)`
(children accept dict kwargs → `ptest_toml_text`, literal TOML strings, or
`None`; `name=None` uses `parent` itself as the root), and
`venv_stub(root, *, pytest=None, pytest_cov=None, coverage=None,
xdist=None)` (`xdist` also accepts an iterable for duplicate-install
cases). Unknown kwargs raise `TypeError` via explicit signatures and the
`ptest_toml_text` passthrough. Module helpers `ptest_toml(root, **kwargs)`
and `fake_git_marker(root)` (detection-only `.git`; change-classification
tests must use `git_repo`). Everything lands under `tmp_path` (or an
explicit `parent` under tmp); no fixed paths, no HOME/state use.
A factories module imports only support, ptest and the stdlib.

## Verification notes

- `test_selection.py::test_choose_plan_is_pure_without_subprocess_or_test_imports`
  crashed xdist workers (INTERNALERROR, crashitem) when scheduled first on a
  worker: its `builtins.__import__` guard blocked the ptest bridge's lazy
  `from pytest import UsageError` in `pytest_runtest_logreport`
  (`pytest_bridge.py:2836` → `_worker_bootstrap`, `:2720`). Serial runs
  passed only by luck of ordering (an earlier test bootstrapped the worker
  first). Fixed inside the owned file: the guard exempts already-imported
  roots and a post-hoc `sys.modules` diff fails with the same message on
  genuinely new `pytest`/`conftest`/`test_*` imports. 26 passed after the
  fix. T1 may want to harden the bridge's lazy import; no src change was
  needed here.
- `changed-during-run` (pycache) after edits: rerun once, per context pack.
- `coordinator-unavailable` / `protocol-mismatch` / `guard handoff was
  incomplete`: slot contention with sibling task worktrees (T1/T4/T5 hold
  all 4 slots for minutes). test_source once showed `65 passed` at pytest
  level with the wrapper handoff failing afterwards; a later clean rerun
  gave `ptest: passed · 65 tests`.
- `test_doctor_init_integration.py::test_doctor_review_runs_one_haiku_call_per_item`
  failed identically on the base file under the bundle (verified via a
  temporary copy): the isolated domain root does not exist yet, so history
  validation fails closed (`_answers_for` maps any `_timing_answer`
  exception to "ptest history is unavailable"). Fixed with explicit empty
  state (create `platform.domain_paths(None).root` at 0700) in the owned
  file; the real account domain is never touched.
