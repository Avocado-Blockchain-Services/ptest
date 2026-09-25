# Design: ptest-parallel-suite

Branch `feature/ptest-parallel-suite` (worktree `~/worktrees/ptest/cc-ptest-parallel-suite/ptest`), base `fadb2ab`.
Author: architect (Claude). Muse implements and audits. The context pack is `.pipeline/context-pack.md`.

## 0. Goals

1. **Deadline (T1).** Replace the hardcoded 600 s compound execution deadline with one that can be configured (`[runner] timeout` / `full_timeout`, CLI `--timeout`). When neither is set, derive the deadline from history within fixed bounds. The failure message states the limit and how to raise it.
2. **Parallel suite (T2 to T5).** `tests/ng` runs under pytest-xdist (`-n auto --dist loadgroup`) on ptest's parallel tier. Every test is isolated: its own HOME, passwd home, state dir, XDG dirs and git config; no fixed ports or paths; no leaks.
3. **Factories.** Move hand-rolled builders into frozen shared builders (`support.py`) and four topic factory plugins (`factories_*.py`). Delete duplicated helpers. Every test and assertion stays.

## 1. Execution model: all tasks start together

Tasks T1 to T5 start at the same moment from the same base. **Section 4 (shared-file bundle)** is frozen. It is the full set of cross-task contracts: conftest isolation, shared builders, xdist config and the `--timeout` wrapper grammar.

- **First commit of every task (mandatory):** apply the section 4 bundle verbatim, unless the base already contains it byte-for-byte. Commit message: `shared: apply frozen parallel-suite bundle`. Identical edits merge cleanly. Each task then checks its own files under the real parallel configuration and the real isolation.
- After that commit, only **T2** may edit `tests/ng/conftest.py`, `tests/ng/support.py`, `pyproject.toml` and `.ptest.toml`. T2 edits only when verification forces it, and records each change in `.pipeline/progress-T2.md`. If T3, T4 or T5 finds a defect in the bundle, it works around it inside its own files and reports the defect. It never edits a bundle file.
- A topic factory module may import `support`, `ptest.*` and the stdlib. It **never** imports another `factories_*` module or another test module. Anything two topics need lives in `support.py`, and is frozen here.
- Run tests only with `.venv/bin/ptest <files>` from the worktree root. Use chunks of a few files per call. **No task runs `--full`.** After all merges the orchestrator runs exactly one parallel `.venv/bin/ptest --full` as the final gate.
- The one exception executes no tests. It is the collection-only anchor check `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider tests` (D13). It reproduces the full-run collection anchor (`test_roots = ["tests"]`) without running anything, so it is not a `--full` run. Any task may run it. T2 must run it (T2 acceptance).
- Commit inside the worktree. Never push, merge, or touch `main`/`dev`.

## 2. Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | `addopts = "-n auto --dist loadgroup"` in `[tool.pytest.ini_options]`. | `loadgroup` behaves like `load` for ungrouped tests and still respects `xdist_group`. `loadfile` would serialise the large files (test_history, test_cli). No separate measurement task; T2 only confirms that the header shows more than one worker and `gw*` ids. |
| D2 | `.ptest.toml [runner] args = ["--cov=ptest", "--cov-report=term"]`, `workers = 8`. | Parallel qualification needs both cov args (context pack). `-n/--dist` stay out of runner args, as the ptest docs require. If 8 exceeds the machine grant, T2 may lower it. |
| D3 | Autouse `isolated_env` per test. It redirects HOME, XDG_*, `pwd.getpwuid(uid).pw_dir`, `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_NOSYSTEM` into a fresh `tmp_path_factory.mktemp("iso")` directory, a sibling of `tmp_path`. `PTEST_STATE_DIR` stays **unset**, exactly as the old `_clear_bootstrap_control_env` left it. | ptest ignores HOME and derives state from passwd, so patching the passwd home makes every in-process normal-domain resolution land in tmp. A probe that exported PTEST_STATE_DIR by default broke 11 of 165 test_platform/test_uninstall tests that assert normal-domain behaviour, so it stays opt-in (`isolated_env.state_dir` is created, not exported). Child processes: every spawned `ptest` must pass `--fixture-domain` (CaseFactory.invoke already does) or an explicit `PTEST_STATE_DIR` (per-task acceptance). |
| D4 | Strip inherited `PTEST_*`, `GIT_*`, `PYTEST_XDIST_*`, `COV_CORE_*`, `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` before setting the isolation vars. | The outer ptest/xdist/cov state must not leak into nested pytest or ptest children. |
| D5 | The only real-home paths a test may use are `support.REAL_TOOL_ENV`: the uv cache, uv-managed pythons, and the npm/cargo/rustup/go caches. They are resolved at import. | An isolated HOME must not force network downloads (test_task_11d runs real `uv sync`). These caches handle concurrent use and are read-mostly. |
| D6 | `_guard_real_install_against_self_uninstall` stays session-scoped autouse. It runs once per xdist worker, and its snapshot is taken at import. | Each worker fails its own run if the real install changed. |
| D7 | Allowed `xdist_group` names: `"process-table"`, `"uv-cache"`, `"toolchain"` (`support.XDIST_GROUPS`). No others. | Making tests independent is preferred. Each use must carry a `# xdist_group: <reason>` comment. |
| D8 | New `RunnerConfig` fields use `field(default=None, repr=False)`. | `source._compatibility_runner` hashes `repr(config.runner)`. Keeping the repr unchanged keeps existing baselines compatible, and a deadline is not a test input. |
| D9 | No public schema change. The deadline surfaces in the execution-timeout problem message. `protocol-v1.json` is unchanged because `compound_timeout_s` already exists. | "Additive only" is met trivially. |
| D10 | Per-task progress goes in `.pipeline/progress-T<n>.md`, not the shared `progress.md`. | Avoids merge conflicts. |
| D11 | `[tool.coverage.run] data_file = ".venv/.coverage"`. | Probed: with the default repo-root `.coverage`, the first run after creation reports `changed-during-run` (untracked). With it gitignored, every run reports it (ignored class). Inside `.venv` (a `non_input_outputs` path) both consecutive runs passed. |
| D12 | The resolved compound deadline reaches `_launch_guard` through the module-level contextvar `operations._COMPOUND_TIMEOUT_S`, not through a new parameter. The signatures of `_launch_guard` and `_run_guard` stay byte-identical to base. | Existing tests monkeypatch both seams with wrappers that accept and forward only positional args: test_operations.py l.494, 546, 700, 788, 828, 879; test_pytest_scoped_subprocess.py l.1124 (`invalid_grant(domain, grant, prepared, setup=None)`) and l.1143. A new keyword, required or optional, raises TypeError or is dropped by `launch(*args)`. Those files belong to T4, which starts from base and never sees T1's change. A contextvar set by the caller and read inside the real seam survives any positional forwarding wrapper that runs in the same thread, so no test file has to change and there is no ordering dependency between tasks. |
| D13 | Factory plugins are registered by `pytest_configure` in `tests/ng/conftest.py` via `config.pluginmanager.import_plugin`. **No conftest defines `pytest_plugins`.** `.ptest.toml` `test_roots` stays `["tests"]`. | In full mode the pytest adapter passes `config.runner.test_roots` (`tests`) as the positional arg (src/ptest/adapters/pytest.py:430). pytest 9.1.1 anchors initial conftests on `tests` and adds only `test*` subdirectories, and `ng` does not match. So tests/ng/conftest.py is imported during collection, and `_check_non_top_pytest_plugins` fails the run on any `pytest_plugins` attribute, even an empty list. `pytest_configure` is a historic hook, so it runs whether the conftest is initial (scoped runs) or late (full runs). Probed on base plus the revised bundle: `--collect-only tests` collects 3920 tests; a stub `factories_domain` fixture is listed by `--fixtures` under both the `tests` anchor and a `tests/ng/<file>` anchor; `ptest tests/ng/test_config.py` gives 169 passed on 4 workers. Toy layout: the old bundle fails with `tests` as the anchor, the new one passes under `tests`, `tests/ng`, `tests/ng/<file>` and no args, both with `-n auto` and with `-n 0`, and assertion rewriting stays active in the plugin modules. |

## 3. Frozen interfaces

### 3.1 Deadline (T1 only, frozen so tests and help agree)

`src/ptest/contracts.py`:
```python
DEFAULT_COMPOUND_TIMEOUT_S = 600.0        # no CLI, no config, no usable history
MIN_COMPOUND_TIMEOUT_S = 60.0             # floor for the dynamic deadline only
MAX_DYNAMIC_COMPOUND_TIMEOUT_S = 21600.0  # 6 h ceiling for the dynamic deadline
MAX_COMPOUND_TIMEOUT_S = 86400.0          # hard ceiling: explicit values + guard clamp (was 600.0)
COMPOUND_TIMEOUT_SAFETY_FACTOR = 3.0
COMPOUND_TIMEOUT_PER_TEST_S = 0.25

class RunnerConfig:            # existing, kw_only frozen dataclass; append:
    timeout_s: float | None = field(default=None, repr=False)
    full_timeout_s: float | None = field(default=None, repr=False)
    # __post_init__: None, or _check_float(..., lo=1, hi=MAX_COMPOUND_TIMEOUT_S); bool rejected

class RunRequest:              # existing; append:
    timeout_s: float | None = None   # _check_float lo=1 hi=MAX_COMPOUND_TIMEOUT_S when not None
```
`src/ptest/config.py`:
- `_runner` accepts the optional keys `timeout` and `full_timeout`. Each is an int or float, not bool, finite, 1..86400. Otherwise the existing `_fail()` path runs (invalid-config).
- `_serialize_fresh` emits `timeout = {v:g}` or `full_timeout = {v:g}` right after `lifecycle`, and **only when set**. A config without the keys parses and renders byte-identically to today.

`src/ptest/cli.py`:
- `_EXECUTION_VALUE` gains `"--timeout"`. `ParsedArgs.timeout_s: float | None = None`.
- Parse with `_number(value, lo=1, hi=C.MAX_COMPOUND_TIMEOUT_S)`. A repeat raises `invalid-config "option cannot be repeated"`.
- The flag is allowed in every execution mode, including `--full` and `--changed`. `timeout_s` is threaded into every `C.RunRequest(...)` that the CLI and `monorepo.py` build.

`src/ptest/history.py` (new public function, never raises):
```python
def comparable_run_evidence(domain: C.DomainPaths, checkout: C.CheckoutIdentity,
                            *, full: bool) -> tuple[float | None, int | None]:
    """(execution_s, total_tests) of the most recent completed comparable run.
    Comparable = same mode family (full vs non-full), status passed|failed, numeric
    timings.execution_s. Non-full falls back to the latest qualifying full run.
    Reads at most 20 summaries. Any Problem/OSError/shape error -> (None, None)."""
```
`src/ptest/operations.py`:
```python
def resolve_compound_timeout(runner: C.RunnerConfig, request: C.RunRequest,
                             evidence: tuple[float | None, int | None]) -> tuple[float, str]:
    # precedence: request.timeout_s -> "cli"
    #             request.mode is FULL and runner.full_timeout_s -> "config"
    #             runner.timeout_s -> "config"
    #             dynamic: signals = [execution_s * SAFETY_FACTOR] + [tests * PER_TEST_S if tests]
    #                      none -> (DEFAULT_COMPOUND_TIMEOUT_S, "default")
    #                      else -> (clamp(max(signals), MIN, MAX_DYNAMIC), "history")
```
```python
import contextvars
# D12. Read only by _launch_guard. The default keeps direct/test callers at today's 600 s.
_COMPOUND_TIMEOUT_S: contextvars.ContextVar[float] = contextvars.ContextVar(
    "ptest_compound_timeout_s", default=C.DEFAULT_COMPOUND_TIMEOUT_S)
```
- **The signatures of `_launch_guard` and `_run_guard` do not change.** Their parameter lists stay byte-identical to base. Add no parameter, positional or keyword (D12).
- `_launch_guard` builds its `C.LaunchManifest` with `compound_timeout_s=_COMPOUND_TIMEOUT_S.get()`. That replaces today's `C.MAX_COMPOUND_TIMEOUT_S`, which becomes 86400. Nothing else in `_launch_guard` changes.
- Both call sites (~1572 and ~2491) resolve the limit once before launch: `limit, _source = resolve_compound_timeout(runner, request, history.comparable_run_evidence(...))`. They then wrap only the existing, unchanged `_run_guard(...)` call:
  ```python
  token = _COMPOUND_TIMEOUT_S.set(limit)
  try:
      raw_guard, frames, execution_s = _run_guard(<unchanged args>)
  finally:
      _COMPOUND_TIMEOUT_S.reset(token)
  ```
  At ~2491 the set/try/reset sits inside the existing `try: ... except (C.Problem, OSError)`, so launch-failure handling is unchanged. History is read only through `comparable_run_evidence`.
- The monkeypatched wrappers in T4-owned files (D12 line list) call the real seam synchronously in the same thread, so they see the value unchanged. **No test file is edited for this.** T1 must not edit them, and T4 has no deadline-related wrapper edits.

`src/ptest/guard.py`:
- Two new module-level helpers. The limit is derived from the manifest, which every site can already reach, so **no guard function signature changes** (`_ready`, `_run_one`, `_Control.await_attempt_decision`, `run_guard`):
  ```python
  def _compound_limit_s(manifest: LaunchManifest) -> float:
      return min(manifest.compound_timeout_s or DEFAULT_COMPOUND_TIMEOUT_S,
                 MAX_COMPOUND_TIMEOUT_S)

  def _compound_timeout_message(manifest: LaunchManifest) -> str:
      return (f"compound execution deadline expired after "
              f"{_compound_limit_s(manifest):.0f}s; raise it with --timeout SECONDS "
              f"or [runner] timeout / full_timeout in .ptest.toml")
  ```
- `run_guard` computes `compound_deadline = time.monotonic() + _compound_limit_s(manifest)` (~l.461).
- The new text applies **only to compound-scope expiry**, at exactly three sites:
  - guard.py:245, `_Control.await_attempt_decision`: `_compound_timeout_message(self.manifest)`
  - guard.py:332, `_ready`: `_compound_timeout_message(control.manifest)`
  - guard.py:398, `_run_one`, **only when `timeout_scope == "compound"`**: `_compound_timeout_message(manifest)`, using `_run_one`'s existing `manifest` parameter
- Attempt and setup expiry (`timeout_scope` of `"attempt"` or `"setup"`, guard.py:380-383) keep today's text, `timeout_scope + " execution deadline expired"`, byte-for-byte. So `test_guard.py::test_timeout_fact_preserves_raw_status_and_closes_spawn[attempt]` (`scope in message`) holds unedited. `[compound]` holds because the new text starts with `compound execution deadline expired`.

`src/ptest/help.py`: the `_RUN` syntax adds `[--timeout 1..86400 (default: from history, else 600)]`. Keep the help tests (test_help.py, owned by T4) green by editing only help.py.

### 3.2 Shared test builders (in `support.py`, section 4; every task may use them)

| Name | Signature | Contract |
|------|-----------|----------|
| `XDIST_GROUPS` | `tuple[str, ...]` | The only allowed group names (D7). |
| `GIT_IDENTITY` | `Mapping[str, str]` | Fixture author and committer. |
| `REAL_TOOL_ENV` | `Mapping[str, str]` | D5. Resolved once at import. |
| `IsolatedEnv` | `NamedTuple(root, home, state_dir, environ)` | Value of the `isolated_env` fixture. |
| `build_isolated_env` | `(root: Path) -> IsolatedEnv` | Creates 0700 `home/`, `state/`, `xdg/{config,cache,data,state}` and `gitconfig` under `root`. |
| `patch_account_home` | `(monkeypatch, home: Path) -> Path` | Makes `pwd.getpwuid(os.getuid()).pw_dir == str(home)`. May be called again to repoint. |
| `git_env` | `(extra: dict \| None = None) -> dict` | os.environ minus `GIT_*`/CONTROL_VARS, plus hermetic git config and identity. |
| `git` | `(root, *args, env=None, check=True) -> str` | Runs hermetic git with a 30 s timeout. Returns stripped stdout. |
| `init_git_repo` | `(root, *, files=None, branch="main", commit=True, message="initial") -> Path` | `git init -b`, local identity config, writes `files`, and optionally makes one commit (`--allow-empty`). |
| `git_commit_all` | `(root, message="update") -> str` | `add -A` plus commit. Returns the HEAD sha. |
| `write_file` | `(path, content: str \| bytes) -> Path` | Creates parent directories. |
| `write_executable` | `(path, text: str) -> Path` | `write_file` plus mode 0755. |
| `PYTHON_SHEBANG` | `str` | `#!{sys.executable}\n` |
| `ptest_toml_text` | `(*, kind="command", launcher=("echo",), args=("hello",), full_args=(), test_roots=None, workers=1, project_id=None, runner_extra="", setup=None, tail="") -> str` | Canonical `.ptest.toml`. `test_roots=None` means `()` for command and `("tests",)` otherwise. `setup` is a dict whose keys must come from {argv, required_paths, network, lifecycle_scripts}. |
| `write_ptest_toml` | `(root, **same kwargs) -> Path` | Writes `root/.ptest.toml` and returns its path. |

### 3.3 Fixtures (conftest, frozen)

- `isolated_env` (autouse, function) → `IsolatedEnv`. A test may request it by name to learn its paths. It strips `CONTROL_VARS`, every `PTEST_*`/`GIT_*`/`PYTEST_XDIST_*`/`COV_CORE_*`, `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`. It sets HOME, XDG_{CONFIG,CACHE,DATA,STATE}_HOME, GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM and REAL_TOOL_ENV, and patches the passwd home. It replaces the old `_clear_bootstrap_control_env`.
- `case` (unchanged) → `CaseFactory(tmp_path)`.
- `_deny_non_tmp_self_roots` (autouse): a `--self` root must sit under `tmp_path` or `isolated_env.root`.
- `_guard_real_install_against_self_uninstall` (session, autouse): unchanged.
- Plugins are registered by `pytest_configure(config)`, which calls `config.pluginmanager.import_plugin(name)` for each name in `_FACTORY_PLUGINS` that is not registered yet and whose `find_spec` resolves. `find_spec` tolerates modules that are not merged yet. **No conftest anywhere defines `pytest_plugins`** (D13), not even as an empty list.

### 3.4 Topic factory modules: fixture naming (frozen, globally unique)

Every fixture defined in a module must start with one of that module's prefixes. Fixtures are function-scoped by default. Session scope is allowed only for expensive read-only artifacts built under `tmp_path_factory`, with an explicit env and no mutation after build. Every resource a fixture creates lives under `tmp_path`, `tmp_path_factory` or `isolated_env.root`. Every spawned process is reaped in teardown.

| Module | Task | Allowed prefixes | Must provide (factory callables, kw-only options, unknown kwargs → TypeError) |
|--------|------|------------------|------|
| `factories_domain.py` | T2 | `state_`, `domain_`, `account_` | `domain_factory(slots=1, jobs=1) -> C.DomainPaths` (delegates to `CaseFactory.domain`); `account_home(name="home") -> Path` (a fresh home plus `patch_account_home`); `state_dir_factory(name="state") -> Path` (0700 dir, sets `PTEST_STATE_DIR`) |
| `factories_repo.py` | T3 | `repo_`, `git_repo`, `ptest_project`, `monorepo`, `venv_stub` | `git_repo(name="repo", *, parent=None, files=None, commit=True, branch="main") -> Path`; `ptest_project(name="proj", *, parent=None, git=False, files=None, **toml) -> Path` (toml kwargs go to `ptest_toml_text`); `monorepo(children: dict[str, dict], *, git=False, root_toml=...) -> Path`; `venv_stub(root, *, pytest=None, pytest_cov=None, coverage=None, xdist=None) -> Path` |
| `factories_exec.py` | T4 | `fake_exec`, `fake_bin`, `fake_pytest_`, `exec_` | `fake_bin` (a per-test directory prepended to PATH by monkeypatch); `fake_exec(name, script: str, *, bin_dir=None) -> Path`; `fake_pytest_project(*, tests: dict[str, str], git=True, **toml) -> Path` |
| `factories_agents.py` | T5 | `fake_provider`, `agent_`, `doctor_`, `review_` | `fake_provider(name, *, stdout="", exit_code=0, script=None) -> Path` (an executable on the `fake_bin`-equivalent PATH dir owned by this module); other factories as needed |

Existing fixture names defined inside test files stay where they are unless they are migrated.

## 4. Shared-file bundle (exact content; also in the `sharedFileContent` return field)

Four files: `tests/ng/conftest.py` (full replacement), `tests/ng/support.py` (three exact edits: import `pwd` and `MappingProxyType`, add `"--timeout"` to `_WRAPPER_VALUE_OPTS`, append the frozen builders block), `pyproject.toml` (adds `addopts` and `[tool.coverage.run] data_file`), `.ptest.toml` (`args` gains the cov args, `workers = 8`). `.gitignore` is **not** changed. The text is identical to `sharedFileContent`.

The verbatim bundle is also committed as the git patch **`.pipeline/shared-bundle.patch`**. It applies cleanly to base `fadb2ab`: `git apply .pipeline/shared-bundle.patch`. The patch and the `sharedFileContent` text are identical, and either one is authoritative.

**Probe results (bundle applied temporarily in the chain worktree, then reverted):**
- `ptest tests/ng/test_config.py` → 169 passed, `4 workers`, `gw0..gw3`, coverage TOTAL printed. Two consecutive runs passed.
- test_task_11d_pytest + test_pytest_full_subprocess + test_source + test_agent_providers → 253 passed (4 workers, 56 s).
- test_cli + test_doctor + test_init + test_parallel_output_cli → 536 passed, 2 failed. **T4 fixes these:** `test_parallel_output_cli::test_doctor_persea_shaped_monorepo` and `::test_doctor_det1_deterministic_rows_cite_child_config` expect `no timing history yet`. They passed on base only because doctor read the **real** account state domain. They must build their own history or state explicitly.
- test_uninstall + test_state_directory + test_install + test_platform → 164 passed, 1 failed. **T2 fixes this:** `test_uninstall::test_self_plans_only_the_argv_fixture_root_never_real_paths` asserts `str(inst) in out`, but the render wraps the longer xdist tmp path (`popen-gwN`). Make the assertion independent of path length without weakening it: for example a shorter tmp root via `tmp_path_factory.mktemp("u")`, or comparing against the unwrapped rendering of the same line.
- The first run after any edit to a test/support file can report `changed-during-run` (pycache). Rerun once; that is not a failure.
- **Revision (D13):** the first bundle defined `pytest_plugins` in tests/ng/conftest.py, which collection under the full-mode `tests` anchor rejects. The bundle now registers plugins from `pytest_configure`. The re-probe of the revised bundle, applied and then reverted: `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider tests` collects 3920 tests. A throwaway `factories_domain.py` fixture is visible via `--fixtures` under both the `tests` and the `tests/ng/test_config.py` anchor. `ptest tests/ng/test_config.py` gives 169 passed on 4 workers with coverage TOTAL. The patch applies cleanly to base (`git apply --check`).

## 5. Tasks

Every task must:
- Record per-file test counts before and after (from `.venv/bin/ptest` output) in `.pipeline/progress-T<n>.md`. For every owned test file, "after" must be at least "before".
- Add no skip, xfail or deletion. Delete no assertion and weaken none. Keep every test id, except where a parametrisation id is unchanged in content.
- Pass every owned test file under the bundle (parallel header shows more than one worker).
- Every `subprocess` spawn of ptest in owned files (`-m ptest`, `ptest` executables) passes `--fixture-domain` or an explicit `PTEST_STATE_DIR` under tmp. No child may resolve the real account domain.
- For owned files, return empty from:
  - `grep -nE 'os\.chdir\(|os\.environ\[[^]]+\] *=|Path\.home\(\)|expanduser\(' <owned files>` (except inside `monkeypatch` usage or assertions about those functions)
  - a check for literal `/tmp/` *filesystem* paths (string payloads that never hit disk are fine; add a comment)
  - a check for fixed non-zero ports
- Delete a hand-rolled helper (`_git`, `_repository`, `_commit_fixture`, `_write_config`, toml-writing `_config`, `_domain`, `_fake_*`, `_monorepo*`, `_git_*project`) only after every caller in owned files uses a factory or support builder. **Leave literal TOML/text that is the subject of an assertion (render/parse golden strings) literal.** Migrate only builders of test *inputs*.

### T1: configurable and dynamic run deadline
**Owns:** `src/ptest/{contracts,config,operations,guard,cli,history,help,monorepo}.py`, `src/ptest/runtime/protocol-v1.json` (expected unchanged), `docs/schemas/**` (expected unchanged), `tests/ng/test_run_deadline.py` (new), `tests/ng/test_guard.py`, `tests/ng/test_contracts.py`, `tests/ng/test_config.py`, `.pipeline/progress-T1.md` (new).

**Acceptance:**
- Section 3.1 is implemented exactly.
- `test_run_deadline.py` covers:
  - precedence (cli > full_timeout in FULL > timeout > history > default); full_timeout is ignored for non-full modes
  - the dynamic clamp at MIN and MAX_DYNAMIC; empty or broken history → 600 "default"
  - `comparable_run_evidence` mode-family selection and its never-raise behaviour
  - config parse bounds (0, 86401, bool, string, nan → invalid-config); render round-trip with and without the keys (without = byte-identical to today)
  - CLI `--timeout` parse bounds and no-repeat; accepted with `--full` and `--changed`
  - `repr(RunnerConfig)` unchanged
  - the guard message text from 3.1 with a real short compound deadline (reuse the existing test_guard pattern at `compound_timeout_s=0.5`; no long timeouts). `{0.5:.0f}` renders as `0`, so assert the exact string `"compound execution deadline expired after 0s; raise it with --timeout SECONDS or [runner] timeout / full_timeout in .ptest.toml"`. Also assert that a real short **attempt** timeout still yields exactly `"attempt execution deadline expired"`.
  - D12 propagation: a positional-only wrapper `def spy(*args): return launch(*args)` installed on `operations._launch_guard` (the same shape as the T4-owned wrappers) sees `operations._COMPOUND_TIMEOUT_S.get()` equal to the resolved limit during `operations.execute`. Use a command-kind project with no nested pytest, and `C.RunRequest(timeout_s=...)` so the value is distinct from 600. After `execute` returns, the contextvar is back at `DEFAULT_COMPOUND_TIMEOUT_S`, including when the wrapper raises (reuse the `fail(*args)` launch-failure pattern).
- Existing literal `compound_timeout_s=600.0` in test_contracts stays valid.
- Read-only regression run (T1 edits only src to fix these): `tests/ng/test_cli.py test_help.py test_operations.py test_run_output.py test_monorepo_scopes.py test_source.py test_history.py test_init_smoke.py test_pytest_scoped_subprocess.py test_task_11d_pytest.py test_task_11f_pytest.py test_shadow.py`. This is satisfiable because `_launch_guard`/`_run_guard` signatures are unchanged (D12). The positional-only wrappers in test_operations.py (l.494, 546, 700, 788, 828, 879) and test_pytest_scoped_subprocess.py (l.1124, 1143) must pass **unedited**.
- `git diff fadb2ab -- src/ptest/operations.py` shows no change to the `def _launch_guard(` or `def _run_guard(` parameter lists.
- T1 does not need to migrate builders in test_config/test_contracts (their TOML literals are the subject under test), but it must obey isolation.

### T2: isolation foundation, parallel config, domain/state factories
**Owns:** the bundle files after the first commit (`pyproject.toml`, `.ptest.toml`, `tests/ng/conftest.py`, `tests/ng/support.py`), `tests/ng/factories_domain.py` (new), `tests/ng/test_isolation.py` (new), `tests/ng/test_{uninstall,state_directory,install,platform,files,scheduler,resources,storage,history}.py`, `.pipeline/progress-T2.md` (new).

**Acceptance:**
- `.venv/bin/ptest tests/ng/test_config.py` shows more than one worker (`gw*` ids or ptest's worker count) with coverage output. Record the header line.
- **Full-mode anchor check (collection only, D13):** with `factories_domain.py` in place, run `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider tests`. It exits 0 with no `pytest_plugins` / "non-top-level conftest" error, and its collected count is at least the base count (3920) plus T2's new tests. Then `.venv/bin/python -m pytest --fixtures -q -p no:cacheprovider tests | grep -E "domain_factory|account_home|state_dir_factory"` lists all three. Also `grep -rnE "^[[:space:]]*pytest_plugins[[:space:]]*=" tests/` returns nothing. Record the commands and output in progress-T2.md. This executes no tests and is not a `--full` run.
- `factories_domain.py` provides section 3.4.
- Install, uninstall, state and platform tests use `account_home`, `state_dir_factory` or `isolated_env` instead of ad-hoc pwd/HOME patches. Remove each duplicated `_fake_home`/`_domain` helper.
- `state_dir_factory` is the only way T2 tests set `PTEST_STATE_DIR`.
- Scheduler, resource and storage tests show no cross-worker contention. Use `xdist_group("process-table")` only for tests that scan or signal processes they did not spawn, and give a reason comment.
- Add `tests/ng/test_isolation.py` (new):
  - `isolated_env` redirects HOME/XDG/pwd home/GIT_CONFIG_GLOBAL, and PTEST_STATE_DIR is unset
  - `platform.domain_paths(None).root` is under `isolated_env.home`; after `monkeypatch.setenv("PTEST_STATE_DIR", str(isolated_env.state_dir))`, `platform.configured_state_directory() == isolated_env.state_dir`
  - two tests get distinct roots
  - inherited `PTEST_*`/`PYTEST_XDIST_*` vars are absent
  - `conftest._snapshot_path` detects creation, mtime change and relink on a tmp path
  - `support.ptest_toml_text()` parses through `ptest.config` for the kinds command and pytest
  - `init_git_repo` produces one commit on `main` with the fixture identity
- `test_files.py` MINI_PREFIX grammar mirror: add `"--timeout"` to its `WRAPPER_VALUE_OPTS` so it matches `support._WRAPPER_VALUE_OPTS`.
- Across all T2 files, run chunked verification in groups of 3 or 4 files per call.

### T3: repo, config, monorepo and venv factories; init, selection, CLI and shadow tests
**Owns:** `tests/ng/factories_repo.py` (new), `tests/ng/test_{cli,init,init_smoke,init_render,init_changed,monorepo_changed,monorepo_scopes,changed_explain,selection,source,project_facts,executability,shadow,doctor_init_integration,compound_profiles}.py`, `.pipeline/progress-T3.md` (new).

**Acceptance:**
- `factories_repo.py` provides section 3.4.
- Every per-file `_git`/`_repository`/`git init` sequence uses `support.git`/`init_git_repo` or `git_repo`. Every `.ptest.toml` input builder uses `ptest_toml_text`/`write_ptest_toml`/`ptest_project`. Every monorepo builder (`_monorepo_root`, `_monorepo_cli_root`, `_monorepo`) uses `monorepo`. Every venv stub uses `venv_stub`.
- test_cli's `scratch="/tmp/ptest-review-test"` stays only if it is a non-filesystem payload (verify, then comment). Otherwise use tmp_path.
- Any test that previously leaned on the real account domain (where/status/history/doctor against no fixture) now sees an empty tmp home. Give it explicit state; never point it back at the real home.

### T4: fake executables and subprocess pytest projects; adapter, subprocess and operations tests
**Owns:** `tests/ng/factories_exec.py` (new), `tests/ng/test_{pytest_scoped_subprocess,pytest_full_subprocess,pytest_parallel_subprocess,pytest_parallel_coverage_subprocess,pytest_xdist_serial_subprocess,task_11d_pytest,task_11f_pytest,pytest_adapter,pytest_bridge_unit,vitest_adapter,simple_adapters,parallel_output_cli,run_output,acceptance,help,operations}.py`, `.pipeline/progress-T4.md` (new).

**Acceptance:**
- `factories_exec.py` provides section 3.4.
- `_commit_fixture` and `_git_pytest_project`-style builders use `init_git_repo`/`fake_pytest_project`. `_fake_node`, `_fake_python_on_path` and `_fake_cli_executable` use `fake_exec`/`fake_bin`.
- `test_parallel_output_cli.py:697` `os.chdir` becomes `monkeypatch.chdir`.
- Fix the two test_parallel_output_cli doctor tests from the probe results in section 4. They must not depend on real-account history.
- test_task_11d's real `uv lock/sync` tests carry `xdist_group("uv-cache")` and run with `REAL_TOOL_ENV` (inherited through the environment). No network beyond what they do today.
- Nested pytest children must not inherit xdist/cov control vars. conftest D4 covers this; verify one subprocess test prints no `gw` worker.
- Timing-sensitive tests keep their existing bounds and must pass 3 times in a row under `-n auto`. Record this.

### T5: agent provider, doctor and review factories; agent, doctor, review and recommendation tests
**Owns:** `tests/ng/factories_agents.py` (new), `tests/ng/test_{agent_assessment,agent_assessment_contract,agent_providers,agent_rules,agent_doctor_acceptance,doctor,doctor_fix,doctor_smoke,review_context,review_protocol,recommendations,checklist,deterministic_items,reports,render,probes,security_gates}.py`, `.pipeline/progress-T5.md` (new).

**Acceptance:**
- `factories_agents.py` provides section 3.4.
- `_fake_account`, `_fake_reviewer`, `_fake_qualified_profiles`, `_fake_entries`/`_fake_version` and provider-executable writers use its factories plus `write_executable`. Configs use `ptest_toml_text`.
- Fake providers must never resolve the real `claude`/`codex`/`gemini`/`opencode` on the real PATH. Each provider test pins PATH to its fake dir and the system dirs it needs, and never includes `~/.local/bin`.
- `scratch="/tmp/..."` values in agent_doctor_acceptance and doctor_fix: verify they are payload-only. If they are real paths, move them under tmp_path.

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| The default pwd-home patch changes output of tests that silently read the real account domain. | This is intended: they were isolation bugs. Tasks see it at once because the bundle is their first commit. Fix it with explicit state in owned files. |
| Plugin registration fails under the full-mode anchor. `ptest --full` passes `test_roots = ["tests"]`, so tests/ng/conftest.py is **not** an initial conftest there, and pytest rejects any `pytest_plugins` attribute in it. | Removed at the source (D13). The bundle registers plugins through `pytest_configure` + `import_plugin`, and no conftest defines `pytest_plugins`. The failure only appears under the `tests` anchor, which no scoped task run uses, so T2 acceptance and final-gate step 3 check it by collection only before the single `--full`. |
| Coverage and xdist overhead. | Accepted: qualification requires it. T2 records the scoped chunk timings. |
| History-derived deadline too tight after a fast run. | Safety factor 3, 60 s floor, and an explicit `--timeout` escape named in the message. |
| A merge conflict on a bundle file. | Bundle commits are byte-identical, and only T2 edits them afterwards. |

## 7. Final gate (orchestrator, after merging T1 to T5)
1. `uv sync --locked --extra test`
2. Chunked smoke: 4 or 5 groups of about 12 files through `.venv/bin/ptest`.
3. Collection only, full-mode anchor: `.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider tests` exits 0, and `--fixtures ... tests` lists fixtures from all four `factories_*` modules. Do not start `--full` until this passes.
4. Exactly one `.venv/bin/ptest --full`. It must be parallel (more than one worker), all green, with total test count ≥ 3818 plus the new test_run_deadline and test_isolation tests.
