# Heavy Scoped Remote Routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ptest tests/api` (221 of 334 test files) run on the Cloud Run backend instead of locally, while `ptest tests/api/test_x.py` and any `-k` filter stay local.

**Architecture:** A third routing tier between "scoped → always local" and "`--full` → backend". A pure function counts how many of the repo's test files the caller's path arguments cover; at or above a configurable threshold the *scoped* command (no coverage gate) is sent to the existing `run_cloudrun`, built with container parallelism instead of the local worker cap.

**Tech Stack:** Python 3.11+ single-file script (`ptest`, no `.py` extension, stdlib only). Tests: pytest, loaded via `importlib` because the target is an extensionless script.

## Global Constraints

- **Design rules in the module docstring must not break:** exit code is always the test run's exit code; test output reaches stdout verbatim; the command blocks until finished; an unreachable backend falls back to local with one warning, never hangs.
- **Stdlib only.** ptest has no dependencies and must keep none. Test-only use of pytest is fine.
- **Never introduce parallelism, only reduce it** — except on the remote container, which is dedicated.
- **`--full` behaviour is untouched by this work**, including its coverage gate.
- **Existing refusals stay refusals.** `ptest tests`, `ptest .`, bare `ptest` keep exiting 2.
- **Exit 75 means "nothing ran, retry later"** and must never be returned for a real test failure.
- Threshold config key: `remote_scoped_min_files`, default `40`, `0` disables.
- Spec: `docs/superpowers/specs/2026-07-26-heavy-scoped-remote-routing-design.md`

## File Structure

| File | Responsibility |
|---|---|
| `ptest` (modify) | The whole tool. New helpers go beside the existing scoping helpers, ~line 400; wiring goes in `main()` |
| `tests/conftest.py` (create) | Loads the extensionless script as a module; builds fake repo trees |
| `tests/test_routing.py` (create) | Unit tests for arg parsing, counting, and the routing verdict |
| `tests/test_main_wiring.py` (create) | Tests that `main()` dispatches to the right runner with the right command |
| `config.example.toml` (modify) | Document the new key |
| `README.md` (modify) | Document the third tier |
| `~/.config/ptest/config.toml` (modify) | Register the ptest repo itself; add the default |

---

### Task 1: Commit the drift so the repo is source of truth again

`~/.local/bin/ptest` is 873 lines; this repo's copy is 725. The installed copy holds five uncommitted functions — `all_test_files`, `refuse_disguised_full`, `full_suite_targets`, `root_targeting_args`, `has_narrowing_filter` — the entire refuse-disguised-full feature. Every later task builds on them. Nothing else in this task; it is a verbatim import of live code so that the diff for the actual feature is readable.

**Files:**
- Modify: `ptest` (replace wholesale with the installed copy)

**Interfaces:**
- Consumes: nothing
- Produces: `all_test_files(root: Path) -> list[Path]`, `has_narrowing_filter(passthrough: list[str]) -> bool`, `root_targeting_args(passthrough: list[str], root: Path, full_targets: set[Path]) -> list[str]`, `refuse_disguised_full(what: str, why: str, root: Path, workers: int) -> int`, `full_suite_targets(full_cmd: str, root: Path) -> set[Path]`

- [ ] **Step 1: Confirm the direction of the drift before copying**

Run:
```bash
cd /home/ingmar/code/tools/ptest
git status --short          # must be clean
diff <(grep -c '' ptest) <(grep -c '' ~/.local/bin/ptest)
diff <(grep -oE '^def [a-z_]+' ptest) <(grep -oE '^def [a-z_]+' ~/.local/bin/ptest)
```
Expected: working tree clean; the installed copy is longer; the only `>` lines are the five function names listed above. If anything appears with a `<` prefix, the installed copy is MISSING something this repo has — stop and report, do not copy.

- [ ] **Step 2: Copy the installed script over the repo copy**

```bash
cd /home/ingmar/code/tools/ptest
cp ~/.local/bin/ptest ptest
chmod +x ptest
```

- [ ] **Step 3: Verify they are now byte-identical**

Run: `diff -q ~/.local/bin/ptest /home/ingmar/code/tools/ptest/ptest && echo IN_SYNC`
Expected: `IN_SYNC`

- [ ] **Step 4: Verify the script still runs**

Run: `cd /home/ingmar/code/tools/ptest && python3 ptest doctor`
Expected: prints config/backend lines, exits 0. (It reports on the ptest repo itself, which is unregistered — `project ✗ unregistered` is the correct output here, not a failure.)

- [ ] **Step 5: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest
git commit -m "feat: refuse disguised full-suite runs (import live drift)

~/.local/bin/ptest had drifted 148 lines ahead of this repo with the
entire refuse-disguised-full feature: all_test_files, full_suite_targets,
root_targeting_args, has_narrowing_filter and refuse_disguised_full.

It makes 'ptest tests', 'ptest .' and bare 'ptest' exit 2 instead of
quietly collecting the whole suite locally at no coverage gate. It has
been live on this machine since 2026-07-25 and never committed.

Imported verbatim so the follow-up feature diff is readable."
```

---

### Task 2: Test harness that can import an extensionless script

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_routing.py`
- Modify: `~/.config/ptest/config.toml` (register this repo)

**Interfaces:**
- Consumes: `all_test_files`, `root_targeting_args`, `full_suite_targets` from Task 1
- Produces: pytest fixtures `ptest` (the loaded module) and `repo` (factory `repo(["tests/api/test_a.py", ...]) -> Path`)

- [ ] **Step 1: Write the harness and a test that pins EXISTING behaviour**

`ptest` has no `.py` extension, so a plain import cannot find it. Load it by path. Its `if __name__ == "__main__"` guard means `exec_module` does not run `main()`.

Create `tests/conftest.py`:

```python
"""Load the extensionless `ptest` script as an importable module."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ptest"


@pytest.fixture(scope="session")
def ptest():
    loader = importlib.machinery.SourceFileLoader("ptest_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ptest_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path):
    """Build a fake repo tree from a list of relative paths."""
    def make(paths):
        for rel in paths:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
        return tmp_path
    return make


@pytest.fixture
def persea_shaped(repo):
    """A tree with persea-api's proportions: 221 api / 61 db / 23 crawler / 3 unit."""
    paths = (
        [f"tests/api/test_a{i}.py" for i in range(221)]
        + [f"tests/db/test_d{i}.py" for i in range(61)]
        + [f"tests/crawler/test_c{i}.py" for i in range(23)]
        + [f"tests/unit/test_u{i}.py" for i in range(3)]
    )
    return repo(paths)
```

Create `tests/test_routing.py`:

```python
"""Routing decisions: which invocations run where."""


def test_all_test_files_finds_every_test_file(ptest, persea_shaped):
    assert len(ptest.all_test_files(persea_shaped)) == 221 + 61 + 23 + 3


def test_naming_the_whole_tree_is_still_a_refusal(ptest, persea_shaped, monkeypatch):
    """Pin existing behaviour before refactoring root_targeting_args."""
    monkeypatch.chdir(persea_shaped)
    hits = ptest.root_targeting_args(["tests"], persea_shaped, set())
    assert hits == ["tests"], "`ptest tests` must still be seen as the full suite"


def test_a_real_subdir_is_not_a_refusal(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.root_targeting_args(["tests/api"], persea_shaped, set()) == []
```

- [ ] **Step 2: Register this repo so tests run through ptest, not raw pytest**

The repo has no `pyproject.toml`, so `default_commands` would return the empty `unknown` kind. Add an explicit stanza. Append to `~/.config/ptest/config.toml`:

```toml
[projects.ptest]
root    = "/home/ingmar/code/tools/ptest"
kind    = "pytest"
workers = 2
scoped  = "uv run --with pytest pytest"
full    = "uv run --with pytest pytest tests"
backend = "local"
```

- [ ] **Step 3: Run the tests and verify they pass**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -v`
Expected: 3 passed. If ptest refuses the path, the stanza in Step 2 is wrong — check with `ptest where`.

- [ ] **Step 4: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add tests/conftest.py tests/test_routing.py
git commit -m "test: harness for loading ptest as a module, pin refusal behaviour"
```

---

### Task 3: `path_like_args` — one implementation of "which args are paths"

`root_targeting_args` skips only tokens starting with `-`, so a flag's *value* that happens to exist on disk is read as a selected path: `pytest --rootdir tests` is seen as naming `tests` and gets refused. Extract the correct logic once and route both callers through it.

**Files:**
- Modify: `ptest` (add `VALUE_FLAGS` + `path_like_args` above `root_targeting_args`; change `root_targeting_args`'s loop)
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `path_like_args(passthrough: list[str]) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
def test_path_like_args_keeps_bare_paths(ptest):
    assert ptest.path_like_args(["tests/api", "tests/db"]) == ["tests/api", "tests/db"]


def test_path_like_args_drops_flags(ptest):
    assert ptest.path_like_args(["-q", "--no-cov", "tests/api"]) == ["tests/api"]


def test_path_like_args_drops_a_flags_separate_value(ptest):
    assert ptest.path_like_args(["-k", "tests/api"]) == []
    assert ptest.path_like_args(["--rootdir", "tests", "tests/api"]) == ["tests/api"]
    assert ptest.path_like_args(["-n", "2", "tests/api"]) == ["tests/api"]


def test_path_like_args_keeps_inline_values_out_of_the_way(ptest):
    """`--cov=x` carries its own value, so the NEXT token is still a path."""
    assert ptest.path_like_args(["--cov=content_maker_api", "tests/api"]) == ["tests/api"]


def test_rootdir_value_is_no_longer_refused_as_the_whole_suite(ptest, persea_shaped,
                                                              monkeypatch):
    monkeypatch.chdir(persea_shaped)
    hits = ptest.root_targeting_args(["--rootdir", "tests", "tests/api"],
                                     persea_shaped, set())
    assert hits == [], "`tests` here is --rootdir's value, not a selected path"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -v`
Expected: FAIL — `AttributeError: module 'ptest_mod' has no attribute 'path_like_args'` on the first four, and the last one fails with `hits == ['tests']`.

- [ ] **Step 3: Implement**

In `ptest`, immediately above `def root_targeting_args(`, insert:

```python
# Flags whose value is a SEPARATE token. Without this list the value is read as
# a selected path whenever it happens to exist on disk — `--rootdir tests` is
# the case that bites, since `tests` is a real directory but is not selected.
# Inline forms (`--cov=x`, `-k=expr`) need no entry: they start with `-`, so the
# token that follows them is genuinely the next argument.
VALUE_FLAGS = {
    "-k", "-m", "--deselect", "--ignore", "--rootdir", "-p",   # pytest
    "-n", "--numprocesses",                                    # xdist
    "-t", "--testNamePattern", "--reporter",                   # vitest / jest
    "--maxWorkers", "--minWorkers", "--concurrency", "--workers", "--jobs",
}


def path_like_args(passthrough: list[str]) -> list[str]:
    """Caller arguments that could name a test path — flags and their values removed."""
    out: list[str] = []
    skip_next = False
    for a in passthrough:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            skip_next = a in VALUE_FLAGS
            continue
        out.append(a)
    return out
```

Then in `root_targeting_args`, replace these three lines:

```python
    for a in passthrough:
        if a.startswith("-"):
            continue
        if a in WHOLE_TREE_TOKENS:
```

with:

```python
    for a in path_like_args(passthrough):
        if a in WHOLE_TREE_TOKENS:
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -v`
Expected: all pass, including the two pinned refusal tests from Task 2 — the refactor must not change them.

- [ ] **Step 5: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest tests/test_routing.py
git commit -m "fix: don't mistake a flag's value for a selected test path

root_targeting_args skipped only tokens starting with '-', so
'pytest --rootdir tests' was read as naming 'tests' and refused as a
disguised full suite. Extract path_like_args as the single definition of
'which arguments are paths' and route root_targeting_args through it."
```

---

### Task 4: `heavy_scoped_files` — count what a scoped run actually covers

**Files:**
- Modify: `ptest` (add below `has_narrowing_filter`)
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes: `path_like_args` (Task 3), `all_test_files` (Task 1)
- Produces: `heavy_scoped_files(passthrough: list[str], root: Path) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
def test_counts_files_under_a_directory_arg(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/api"], persea_shaped) == 221
    assert ptest.heavy_scoped_files(["tests/db"], persea_shaped) == 61
    assert ptest.heavy_scoped_files(["tests/unit"], persea_shaped) == 3


def test_counts_a_single_file_arg_as_one(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/api/test_a0.py"], persea_shaped) == 1


def test_two_args_are_a_union_not_a_sum(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    both = ptest.heavy_scoped_files(["tests/api", "tests/api/test_a0.py"], persea_shaped)
    assert both == 221, "the file is already inside the directory"
    assert ptest.heavy_scoped_files(["tests/api", "tests/db"], persea_shaped) == 282


def test_a_nonexistent_path_counts_zero(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/typo"], persea_shaped) == 0


def test_no_path_args_counts_zero(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["-q", "--no-cov"], persea_shaped) == 0


def test_absolute_paths_are_counted(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files([str(persea_shaped / "tests/db")], persea_shaped) == 61
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -k heavy -v`
Expected: FAIL — `AttributeError: module 'ptest_mod' has no attribute 'heavy_scoped_files'`

- [ ] **Step 3: Implement**

In `ptest`, immediately after `has_narrowing_filter`, insert:

```python
def heavy_scoped_files(passthrough: list[str], root: Path) -> int:
    """How many of the repo's test files the caller's paths actually cover.

    This is the size signal the remote-routing tier turns on. A scoped run that
    covers most of the suite costs the same as `--full` on this machine while
    the dedicated container sits idle; one that covers a handful is finished
    before a remote round trip has finished packing.

    Union, not sum: `tests/api tests/api/test_x.py` covers 221 files, not 222.
    An argument that resolves to nothing contributes zero and is NOT an error —
    ptest is not a path validator, and the runner reports a bad path better than
    ptest could. A typo therefore routes local, which is the safe direction.
    """
    args = path_like_args(passthrough)
    if not args:
        return 0
    tests = [t.resolve() for t in all_test_files(root)]
    if not tests:
        return 0
    covered: set[Path] = set()
    for a in args:
        # Relative args are checked against BOTH cwd and root for the same
        # reason root_targeting_args does: run_local() executes in the root.
        candidates = [Path(a)] if os.path.isabs(a) else [Path.cwd() / a, root / a]
        for p in candidates:
            if not p.exists():
                continue
            r = p.resolve()
            covered.update(t for t in tests if t.is_relative_to(r))
            break
    return len(covered)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest tests/test_routing.py
git commit -m "feat: heavy_scoped_files — how much of the suite a scoped run covers"
```

---

### Task 5: The routing verdict and container parallelism

**Files:**
- Modify: `ptest` (add both functions after `heavy_scoped_files`)
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes: `heavy_scoped_files` (Task 4), `has_narrowing_filter` (Task 1)
- Produces:
  - `remote_worker_flags(kind: str) -> str`
  - `should_route_remote(passthrough: list[str], root: Path, pcfg: dict, cfg: dict, force_local: bool) -> tuple[bool, int]` — `(route?, files covered)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
CLOUD = {"backend": "cloudrun"}
LOCAL = {"backend": "local"}
CFG = {"defaults": {"remote_scoped_min_files": 40}}


def test_big_directory_routes_remote(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, n = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, CFG, False)
    assert (route, n) == (True, 221)


def test_small_directory_stays_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, n = ptest.should_route_remote(["tests/crawler"], persea_shaped, CLOUD, CFG, False)
    assert (route, n) == (False, 23)


def test_single_file_stays_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api/test_a0.py"], persea_shaped,
                                         CLOUD, CFG, False)
    assert route is False


def test_a_narrowing_filter_keeps_a_big_path_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api", "-k", "foo"], persea_shaped,
                                         CLOUD, CFG, False)
    assert route is False, "a filter means the caller wants the fast loop"


def test_force_local_wins(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, CFG, True)
    assert route is False


def test_local_backend_never_routes(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, LOCAL, CFG, False)
    assert route is False


def test_threshold_zero_disables_the_tier(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    cfg = {"defaults": {"remote_scoped_min_files": 0}}
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, cfg, False)
    assert route is False


def test_threshold_boundary_is_inclusive(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    at = {"defaults": {"remote_scoped_min_files": 61}}
    below = {"defaults": {"remote_scoped_min_files": 62}}
    assert ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, at, False)[0]
    assert not ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, below, False)[0]


def test_project_threshold_overrides_the_default(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    pcfg = {"backend": "cloudrun", "remote_scoped_min_files": 500}
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, pcfg, CFG, False)
    assert route is False


def test_default_threshold_when_config_is_silent(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, {}, False)[0]
    assert not ptest.should_route_remote(["tests/crawler"], persea_shaped, CLOUD, {}, False)[0]


def test_only_pytest_needs_an_explicit_parallel_flag(ptest):
    assert ptest.remote_worker_flags("pytest") == "-n auto"
    for kind in ("vitest", "npm", "go", "cargo", "unknown"):
        assert ptest.remote_worker_flags(kind) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -k "route or remote_worker" -v`
Expected: FAIL — `AttributeError: module 'ptest_mod' has no attribute 'should_route_remote'`

- [ ] **Step 3: Implement**

In `ptest`, immediately after `heavy_scoped_files`, insert:

```python
# Default only. A project stanza may override it, and 0 disables the tier.
DEFAULT_REMOTE_SCOPED_MIN_FILES = 40


def remote_worker_flags(kind: str) -> str:
    """Parallelism for a run that owns its container.

    The inverse of worker_flags: that one exists to stop a runner grabbing a
    machine shared with five other agents, and this one exists because the
    container is dedicated and capping it would discard the entire benefit.

    Only pytest needs a flag. xdist is opt-in, so `uv run pytest tests/api` in
    the container runs single-threaded and would be SLOWER than the local run it
    replaced. vitest, go and cargo already use every core by default.
    """
    return "-n auto" if kind == "pytest" else ""


def should_route_remote(passthrough: list[str], root: Path, pcfg: dict, cfg: dict,
                        force_local: bool) -> tuple[bool, int]:
    """Is this scoped run big enough to be worth shipping off-box?

    Returns (route?, files covered) — the count comes back so the caller can say
    what it decided without walking the tree a second time.
    """
    if force_local or pcfg.get("backend") != "cloudrun":
        return False, 0
    # A filter means the caller is hunting one failure and wants the fastest
    # loop, not the biggest machine. Never trade their latency for our cores.
    if has_narrowing_filter(passthrough):
        return False, 0
    threshold = int(pcfg.get(
        "remote_scoped_min_files",
        cfg.get("defaults", {}).get("remote_scoped_min_files",
                                    DEFAULT_REMOTE_SCOPED_MIN_FILES)))
    if threshold <= 0:
        return False, 0
    covered = heavy_scoped_files(passthrough, root)
    return covered >= threshold, covered
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_routing.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest tests/test_routing.py
git commit -m "feat: should_route_remote + remote_worker_flags"
```

---

### Task 6: Wire the tier into `main()`

**Files:**
- Modify: `ptest` (`main()`, and `run_cloudrun`'s concurrency-refusal message)
- Create: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `should_route_remote`, `remote_worker_flags` (Task 5), `run_cloudrun`, `cap_workers_for_local` (existing)
- Produces: no new public names. `run_cloudrun` gains a keyword argument `what: str = "--full"` used only in the refusal message.

- [ ] **Step 1: Write the failing tests**

`CONFIG` and `STATE` are module-level constants read at import time, so tests monkeypatch `mod.CONFIG` rather than setting env. `run_cloudrun` and `run_local` are monkeypatched, so no test reaches gcloud, the network, or the real daily-run state file.

Create `tests/test_main_wiring.py`:

```python
"""What main() dispatches to, and with what command."""
import pytest

CONFIG_TOML = """
[defaults]
workers = 2
remote_scoped_min_files = 40

[projects.fake]
root    = "{root}"
kind    = "pytest"
workers = 2
scoped  = "uv run pytest"
full    = "uv run pytest --cov-fail-under=85 tests"
backend = "cloudrun"
job     = "fake-job"
"""


@pytest.fixture
def wired(ptest, persea_shaped, monkeypatch, tmp_path):
    """main() with the runners replaced by recorders."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG_TOML.format(root=persea_shaped))
    monkeypatch.setattr(ptest, "CONFIG", cfg)
    monkeypatch.chdir(persea_shaped)
    monkeypatch.setattr(ptest, "ensure_node_deps", lambda *a, **k: 0)

    calls = {"remote": [], "local": []}

    def fake_remote(pcfg, cfg_, project, root, cmd, **kw):
        calls["remote"].append(cmd)
        return 0

    def fake_local(cmd, cwd, env):
        calls["local"].append(cmd)
        return 0

    monkeypatch.setattr(ptest, "run_cloudrun", fake_remote)
    monkeypatch.setattr(ptest, "run_local", fake_local)
    return ptest, calls


def test_big_path_goes_remote_with_container_parallelism(wired):
    ptest, calls = wired
    assert ptest.main(["tests/api"]) == 0
    assert len(calls["remote"]) == 1 and not calls["local"]
    cmd = calls["remote"][0]
    assert "-n auto" in cmd
    assert "tests/api" in cmd
    assert "--cov-fail-under" not in cmd, "a scoped run keeps no coverage gate"


def test_small_path_runs_local_at_the_cap(wired):
    ptest, calls = wired
    assert ptest.main(["tests/crawler"]) == 0
    assert len(calls["local"]) == 1 and not calls["remote"]
    assert "-n 2" in calls["local"][0]
    assert "-n auto" not in calls["local"][0]


def test_force_local_keeps_a_big_path_here(wired):
    ptest, calls = wired
    assert ptest.main(["--local", "tests/api"]) == 0
    assert len(calls["local"]) == 1 and not calls["remote"]
    assert "-n 2" in calls["local"][0]


def test_callers_own_flag_wins_over_container_parallelism(wired):
    ptest, calls = wired
    assert ptest.main(["tests/api", "-n", "0"]) == 0
    cmd = calls["remote"][0]
    assert cmd.index("-n auto") < cmd.index("-n 0"), "caller's flag must come last"


def test_busy_backend_refuses_with_75_and_runs_nothing(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: 75)
    assert ptest.main(["tests/api"]) == 75
    assert not calls["local"], "exit 75 means nothing ran"


def test_broken_backend_degrades_to_a_capped_local_run(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: None)
    assert ptest.main(["tests/api"]) == 0
    assert len(calls["local"]) == 1
    assert "-n 2" in calls["local"][0], "the fallback must be capped, not -n auto"
    assert "-n auto" not in calls["local"][0]


def test_remote_test_failure_is_returned_verbatim(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: 1)
    assert ptest.main(["tests/api"]) == 1
    assert not calls["local"]


def test_naming_the_whole_tree_is_still_refused_not_routed(wired):
    ptest, calls = wired
    assert ptest.main(["tests"]) == 2
    assert not calls["remote"] and not calls["local"]


def test_full_still_sends_the_full_command(wired):
    ptest, calls = wired
    assert ptest.main(["--full"]) == 0
    assert "--cov-fail-under=85" in calls["remote"][0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_main_wiring.py -v`
Expected: FAIL — `test_big_path_goes_remote_with_container_parallelism` finds the run in `calls["local"]`, not `calls["remote"]`.

- [ ] **Step 3: Implement the wiring**

In `main()`, replace this block:

```python
    parts = [base]
    if not full or force_local:
        # Cap workers on anything running on this machine. A remote job owns its
        # whole container, so it uses the project's own full-suite command as-is.
        flags = worker_flags(kind, workers, root)
        if flags:
            parts.append(flags)
```

with:

```python
    # A scoped run big enough to be worth the container goes there too — the
    # third tier between "scoped, always local" and "--full, always remote".
    route_remote, covered = (
        should_route_remote(passthrough, root, pcfg, cfg, force_local)
        if not full else (False, 0)
    )

    parts = [base]
    if route_remote:
        # Dedicated container: use its cores, not this machine's cap.
        flags = remote_worker_flags(kind)
    elif not full or force_local:
        # Cap workers on anything running on this machine. A remote job owns its
        # whole container, so it uses the project's own full-suite command as-is.
        flags = worker_flags(kind, workers, root)
    else:
        flags = ""
    if flags:
        parts.append(flags)
```

Then, immediately before the existing `if full and not force_local and pcfg.get("backend") == "cloudrun":` block, insert:

```python
    if route_remote:
        total = len(all_test_files(root))
        print(f"[ptest] covers {covered}/{total} test files — routing to the "
              f"remote backend (scoped, no coverage gate)",
              file=sys.stderr, flush=True)
        rc = run_cloudrun(pcfg, cfg, name or root.name, root, cmd,
                          what=f"`ptest {' '.join(path_like_args(passthrough))}`")
        if rc is not None:
            return rc  # includes 75 (busy: nothing ran) and real test failures
        warn("remote unavailable — running this scoped run locally at the cap")
```

Finally, replace the last three lines of `main()`:

```python
    local_cmd = cap_workers_for_local(cmd, kind, workers, root) if full else cmd
    if full and local_cmd != cmd:
        warn(f"running --full LOCALLY — capped to {workers} workers "
             "(the remote command's parallelism is for a dedicated container)")
    return run_local(local_cmd, root, env)
```

with:

```python
    remote_shaped = full or route_remote
    local_cmd = cap_workers_for_local(cmd, kind, workers, root) if remote_shaped else cmd
    if remote_shaped and local_cmd != cmd:
        warn(f"running {'--full' if full else 'this scoped run'} LOCALLY — capped "
             f"to {workers} workers (the remote command's parallelism is for a "
             "dedicated container)")
    return run_local(local_cmd, root, env)
```

- [ ] **Step 4: Fix `run_cloudrun`'s refusal message for the scoped case**

Its concurrency refusal currently advises "use a scoped `ptest <path>`", which is useless when the scoped path is what got refused. Change the signature:

```python
def run_cloudrun(pcfg: dict, cfg: dict, project: str, root: Path, cmd: str,
                 what: str = "--full") -> int | None:
```

and replace the two warn lines in the `at_concurrency_limit` branch:

```python
        warn(f"NOT falling back to a local {what} run (that would hammer this "
             "machine). Re-run when one finishes, or scope it tighter with a "
             "path or a -k filter.")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/ingmar/code/tools/ptest && ptest tests/test_main_wiring.py tests/test_routing.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest tests/test_main_wiring.py
git commit -m "feat: route heavy scoped runs to the remote backend

A scoped run covering >= remote_scoped_min_files (default 40) test files
now goes to the cloudrun backend instead of running locally at 2 workers.
It sends the SCOPED command -- no coverage gate -- with -n auto instead of
the local cap, because the container is dedicated.

Busy refuses (exit 75, nothing runs); broken degrades to a capped local
run. -k/-m/--lf, --local, and small paths all stay local. Refusals still
refuse: 'ptest tests' is not silently upgraded to a remote full run."
```

---

### Task 7: Document, install, and prove it end to end

**Files:**
- Modify: `ptest` (module docstring usage block)
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `~/.config/ptest/config.toml`
- Install: `~/.local/bin/ptest`

**Interfaces:**
- Consumes: everything above
- Produces: the installed tool

- [ ] **Step 1: Update the usage block in `ptest`'s module docstring**

Replace the `ptest PATH [ARGS...]` paragraph with:

```
  ptest PATH [ARGS...]      scoped run, workers capped. PATH must really scope:
                            bare `ptest`, `ptest .`, and any directory the whole
                            suite lives under (`tests`, `src`) are `--full` in
                            disguise and are REFUSED, for every runner alike.
                            A `-k`/`-t`/`--lf` filter counts as scoping.
                            A path covering >= remote_scoped_min_files test
                            files goes to the backend instead of running here.
```

- [ ] **Step 2: Document the key in `config.example.toml`**

Add under `[defaults]`, next to `daily_remote_runs`:

```toml
# A scoped run covering at least this many test files goes to the backend
# instead of running here: `ptest tests/api` is 221 of persea-api's 334 test
# files, which is --full-sized load wearing a scoped command's clothes. Narrow
# paths and any -k/-m/--lf filter always stay local, where a ~2 min remote
# round trip would dominate. Set 0 to disable the tier. Overridable per project.
remote_scoped_min_files = 40
```

- [ ] **Step 3: Document the tier in `README.md`**

Add after the existing description of scoped vs full:

```markdown
### Three tiers, not two

| invocation | where |
|---|---|
| `ptest tests/api/test_x.py`, `ptest -k foo` | local, capped to `workers` |
| `ptest tests/api` (≥ `remote_scoped_min_files` files) | backend, scoped command, no coverage gate |
| `ptest --full` | backend, full command, coverage gate |

The middle tier exists because a scoped run can be full-suite-sized: `tests/api`
is 221 of persea-api's 334 test files. A `-k`/`-m`/`--lf` filter always keeps a
run local — a filter means you are hunting one failure and want the fast loop.
`ptest --local <path>` forces local for anything.
```

- [ ] **Step 4: Add the default to the live config**

In `~/.config/ptest/config.toml`, under `[defaults]`, add:

```toml
remote_scoped_min_files = 40
```

- [ ] **Step 5: Run the whole test suite**

Run: `cd /home/ingmar/code/tools/ptest && ptest --full`
Expected: all tests pass. (This project's stanza is `backend = "local"`, so `--full` runs here — it is a few dozen fast unit tests, not a load concern.)

- [ ] **Step 6: Install**

```bash
cp /home/ingmar/code/tools/ptest/ptest ~/.local/bin/ptest
chmod +x ~/.local/bin/ptest
diff -q /home/ingmar/code/tools/ptest/ptest ~/.local/bin/ptest && echo IN_SYNC
```

- [ ] **Step 7: Prove the routing decision against the real repo, without spending a remote run**

Run:
```bash
cd /home/ingmar/code/persea_content_maker/persea_content_maker_api
python3 - <<'PY'
import importlib.machinery, importlib.util
from pathlib import Path
ldr = importlib.machinery.SourceFileLoader("p", str(Path.home()/".local/bin/ptest"))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("p", ldr))
ldr.exec_module(m)
root = Path.cwd(); cfg = m.load_config()
name, pcfg = m.resolve_project(cfg, root)
for arg in ("tests/api", "tests/db", "tests/crawler", "tests/llm", "tests/unit"):
    print(arg, m.should_route_remote([arg], root, pcfg, cfg, False))
print("-k", m.should_route_remote(["tests/api", "-k", "x"], root, pcfg, cfg, False))
PY
```
Expected exactly:
```
tests/api (True, 221)
tests/db (True, 61)
tests/crawler (False, 23)
tests/llm (False, 17)
tests/unit (False, 3)
-k (False, 0)
```

- [ ] **Step 8: One real remote scoped run**

Run: `cd /home/ingmar/code/persea_content_maker/persea_content_maker_api && ptest tests/db`
Expected: prints `[ptest] covers 61/334 test files — routing to the remote backend`, then `[ptest] packaged …MB → gs://…`, then the remote job's test output. Exit code is the suite's. If it prints exit 75, the job is busy — that is a correct outcome, not a failure; retry later.

- [ ] **Step 9: Commit**

```bash
cd /home/ingmar/code/tools/ptest
git add ptest README.md config.example.toml
git commit -m "docs: document the heavy-scoped remote tier"
```

---

## Notes for the implementer

**`cap_workers_for_local` strips the caller's `-n 0` on a fallback.** It removes every `-n …` and appends `-n {workers}`, so `ptest tests/api -n 0` that falls back to local runs at `-n 2`, not serially. This is pre-existing behaviour for `--full` fallbacks and is left alone: locally, the cap outranks the caller's preference. Do not "fix" it as part of this work.

**Do not reuse `full_suite_targets` for counting.** It reads directories out of the configured `full` command string, which persea-front spells as `npm test --` — no path at all. Counting must go through `all_test_files`, which is why that function exists.

**The two walks.** `main()` calls `heavy_scoped_files` (inside `should_route_remote`) and then `all_test_files` again for the log line's denominator. Both are name-only `os.walk`s that open nothing, and the second happens only on the routing path, immediately before a ~2-minute remote round trip. Leave it simple.
