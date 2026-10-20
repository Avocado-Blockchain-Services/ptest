"""Tiered iteration selection: end-to-end smoke rows S1-S17 (serial).

Integration boundary: the real ptest CLI as a subprocess
(``case.invoke``), the pattern of ``test_selection_e2e``. Every row runs
against a committed, hub-shaped fixture repository materialised from
``tests/ng/fixtures/tiered`` (``.py.txt`` files): twenty tests in ten
files, every test appends its node id to ``ran.txt`` (an ignored file at
the project root). Store setup and store checks go through SQLite on the
fixture domain's own ``selection.db``; nothing in ptest is monkeypatched.

Spec: ``docs/specs/2026-10-19-tiered-iteration-selection.md`` (Smoke
tests). Row ids below are the spec's ids. With the spec's fixture
``.ptest.toml`` a serial pytest run stays on the basic tier, whose end
line carries no test counts (only the advanced tier, admitted by declared
coverage or ``[runner] reuse = true``, spec 2026-10-12 R2, reads verified
native counts), so those rows take their counts from the JSON
``selection`` block. ``test_spec_counted_end_line`` asserts the spec's
counted end lines exactly on the same fixture plus ``reuse = true`` and
``non_input_outputs = ["ran.txt"]`` (the full mode fingerprints ignored
files, and every test writes ``ran.txt``); that is the one deviation
from the spec's fixture config. The xdist twin asserts the counted
wording through the controller-reconciled counts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import support
from support import CONTROL_VARS

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tiered"
SERIAL_ARGS = ("-q", "-p", "no:xdist")
FULL_TIMEOUT = 120
CHANGED_TIMEOUT = 90

ALL_TESTS = frozenset(
    {f"tests/test_users.py::{n}"
     for n in ("test_users_listed", "test_users_count")}
    | {f"tests/test_orders.py::{n}"
       for n in ("test_orders_total", "test_orders_empty")}
    | {f"tests/test_cache.py::{n}"
       for n in ("test_cache_engine_is_shared", "test_cache_engine_kind")}
    | {f"tests/test_snapshot.py::{n}"
       for n in ("test_manifest_snapshot", "test_manifest_file_is_json")}
    | {f"tests/test_mixed.py::{n}"
       for n in ("test_mixed_users", "test_mixed_hub")}
    | {f"tests/test_hub_{i}.py::test_hub_{i}_{s}"
       for i in range(1, 6) for s in ("a", "b")})
USERS_TESTS = frozenset({
    "tests/test_users.py::test_users_listed",
    "tests/test_users.py::test_users_count",
    "tests/test_mixed.py::test_mixed_users"})
ORDERS_TESTS = frozenset({
    "tests/test_orders.py::test_orders_total",
    "tests/test_orders.py::test_orders_empty"})
CACHE_TESTS = frozenset({
    "tests/test_cache.py::test_cache_engine_is_shared",
    "tests/test_cache.py::test_cache_engine_kind"})
OBSERVER = "tests/test_snapshot.py::test_manifest_snapshot"
NEIGHBOUR = "tests/test_snapshot.py::test_manifest_file_is_json"
MODELS = "app/models.py"
SHARED_MODELS = "shared import-time state via app/models.py"
GATE_HINT = "gate at spec end"
HOLD_FLAG = "hold.flag"
SPEC_DELTA = ("spec/product delta awaiting an orchestrator decision "
              "(amend the spec row or close the product gap); ")

_GITIGNORE = ("__pycache__/\n.pytest_cache/\nran.txt\n" + HOLD_FLAG
              + "\nptest-result-*.json\nr.json\n")
_TOP_LINE = re.compile(
    r"^ptest: (passed|failed|incomplete|cancelled|no tests needed)\b")


# ---------------------------------------------------------------- project

def materialise(target: Path) -> Path:
    """Copy the ``.txt``-suffixed fixture tree, stripping the suffix."""
    for source in sorted(FIXTURE.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(FIXTURE)
        assert relative.name.endswith(".txt"), relative
        relative = relative.with_name(relative.name[:-4])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return target


def config_text(project_id: str, args=SERIAL_ARGS,
                fingerprint: list | None = None, workers: int = 1,
                reuse: bool = False) -> str:
    text = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        f"args = {json.dumps(list(args))}\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        f"workers = {workers}\n"
        'lifecycle = "cooperative-process-group"\n'
        + ("reuse = true\n" if reuse else "")
        + "\n[selection]\n"
        "enabled = true\n")
    if reuse:
        # The full mode fingerprints ignored files, and every test writes
        # ran.txt mid-run, so the counted variant declares it an output.
        text += 'non_input_outputs = ["ran.txt"]\n'
    if fingerprint is not None:
        text += f"ambient_fingerprint = {json.dumps(fingerprint)}\n"
    return text


class Project:
    """One committed fixture repository inside a fixture domain."""

    def __init__(self, case, domain, root: Path, args=SERIAL_ARGS,
                 workers: int = 1, reuse: bool = False):
        self.case = case
        self.domain = domain
        self.root = root
        self.args = tuple(args)
        self.workers = workers
        self.reuse = reuse
        self.project_id = tomllib.loads(
            (root / ".ptest.toml").read_text())["project_id"]

    # -- construction
    @classmethod
    def create(cls, case, domain, args=SERIAL_ARGS, workers: int = 1,
               files: dict | None = None,
               reuse: bool = False) -> "Project":
        root = case.project(domain, kind="pytest")
        project_id = tomllib.loads(
            (root / ".ptest.toml").read_text())["project_id"]
        materialise(root)
        (root / ".gitignore").write_text(_GITIGNORE)
        for name, text in (files or {}).items():
            (root / name).write_text(text)
        (root / ".ptest.toml").write_text(
            config_text(project_id, args, None, workers, reuse))
        support.init_git_repo(root, message="base")
        return cls(case, domain, root, args, workers, reuse)

    @classmethod
    def recorded(cls, case, args=SERIAL_ARGS, workers: int = 1,
                 files: dict | None = None,
                 reuse: bool = False) -> "Project":
        """Fixture repo after one green ``ptest --full`` (``ran.txt`` gone)."""
        domain = case.domain(slots=workers, jobs=workers)
        project = cls.create(case, domain, args, workers, files, reuse)
        full = project.run("--full")
        assert full.code == 0, full.err[-2000:]
        assert project.ran() == ALL_TESTS, project.ran()
        project.clear_ran()
        return project

    # -- running
    def run(self, *args: str, root: Path | None = None,
            timeout: float = FULL_TIMEOUT) -> "Run":
        target = root or self.root
        done = self.case.invoke(self.domain, target, *args, timeout=timeout)
        return Run(done, target)

    def ran(self, root: Path | None = None) -> set:
        path = (root or self.root) / "ran.txt"
        return set(path.read_text().split()) if path.exists() else set()

    def clear_ran(self, root: Path | None = None) -> None:
        path = (root or self.root) / "ran.txt"
        if path.exists():
            path.unlink()

    # -- git
    def git(self, *args: str, root: Path | None = None) -> str:
        return support.git(root or self.root, *args)

    def commit(self, message: str, root: Path | None = None) -> str:
        return support.git_commit_all(root or self.root, message)

    def worktree(self, name: str, branch: str) -> Path:
        path = self.domain.root / name
        self.git("worktree", "add", "-q", "-b", branch, str(path), "main")
        return path

    # -- edits (spec setup phrases)
    def edit(self, relative: str, old: str, new: str,
             root: Path | None = None) -> None:
        path = (root or self.root) / relative
        text = path.read_text()
        assert old in text, (relative, old)
        path.write_text(text.replace(old, new, 1))

    def edit_users_body(self, root: Path | None = None) -> None:
        self.edit("app/svc/users.py", '["ada", "grace"]',
                  'sorted(["grace", "ada"])', root)

    def edit_total_body(self, root: Path | None = None) -> None:
        self.edit("app/svc/orders.py", "sum(items)",
                  "sum(item for item in items)", root)

    def edit_engine_body(self) -> None:
        self.edit("app/cache.py", '"pool": []}', '"pool": [], "v": 2}')

    def add_field(self, line: str, root: Path | None = None) -> None:
        path = (root or self.root) / MODELS
        path.write_text(path.read_text() + f"    {line}\n")

    def add_static_norm(self) -> None:
        path = self.root / MODELS
        path.write_text(
            path.read_text()
            + "\n    @staticmethod\n    def _norm(v):\n        return v\n")

    def write_snapshot(self, fields: list, root: Path | None = None) -> None:
        data = {"routes": ["/audit", "/users"],
                "schemas": {"User": sorted(fields)}}
        (root or self.root).joinpath(
            "tests", "data", "manifest.json").write_text(
            json.dumps(data, sort_keys=True) + "\n")

    def set_fingerprint(self, argv: list | None) -> None:
        (self.root / ".ptest.toml").write_text(
            config_text(self.project_id, self.args, argv, self.workers,
                        self.reuse))

    # -- store (the fixture domain's own selection.db)
    @property
    def store_path(self) -> Path:
        return self.domain.root / "projects" / self.project_id / "selection.db"

    def store_digest(self) -> str:
        return hashlib.sha256(self.store_path.read_bytes()).hexdigest()

    def sql(self, statement: str, params=()) -> list:
        conn = sqlite3.connect(str(self.store_path), timeout=10)
        try:
            return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def sql_write(self, statement: str, params=()) -> None:
        conn = sqlite3.connect(str(self.store_path), timeout=10)
        try:
            with conn:
                conn.execute(statement, params)
        finally:
            conn.close()

    def store_state(self) -> dict:
        """Every state table's rows; the derived source-index cache keyed
        by content digest is reduced to its key set (planning may add a
        row for an edited file, never alter or drop one)."""
        conn = sqlite3.connect(str(self.store_path), timeout=10)
        try:
            tables = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))
            state = {}
            for table in tables:
                if table == "cache":
                    state[table] = frozenset(
                        row[0] for row in conn.execute(
                            "SELECT digest FROM cache"))
                else:
                    state[table] = sorted(conn.execute(
                        f"SELECT * FROM {table}").fetchall(), key=repr)
            return state
        finally:
            conn.close()

    def assert_state_unchanged(self, before: dict) -> None:
        after = self.store_state()
        assert after.keys() == before.keys()
        for table, rows in before.items():
            if table == "cache":
                assert rows <= after[table], "a cache row was dropped"
            else:
                assert after[table] == rows, f"{table} rows changed"

    def kv(self, key: str):
        rows = self.sql("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    # -- composite rows
    def after_s2(self) -> "Run":
        """S2: ``age`` added to ``User``; nothing runs now."""
        self.add_field("age: int")
        return self.run("--changed", timeout=CHANGED_TIMEOUT)

    def learned(self) -> "Run":
        """S3: the gate after S2; learns the snapshot observer."""
        self.after_s2()
        gate = self.run("--full")
        self.clear_ran()
        return gate

    def reseed(self, fields: list, fingerprint: list | None) -> "Run":
        """Commit the snapshot (and fingerprint), one green full run."""
        self.write_snapshot(fields)
        self.set_fingerprint(fingerprint)
        self.commit("reseed")
        green = self.run("--full")
        assert green.code == 0, green.err[-2000:]
        self.clear_ran()
        return green


class Run:
    """A completed CLI invocation with its stderr and JSON selection."""

    def __init__(self, done, root: Path):
        self.done = done
        self.root = root
        self.code = done.code
        self.err = done.stderr.decode("utf-8", "replace")
        self.out = done.stdout.decode("utf-8", "replace")

    @property
    def data(self) -> dict:
        assert self.done.result is not None, self.err[-2000:]
        assert self.done.result["kind"] == "run"
        assert self.done.result["error"] is None
        return self.done.result["data"]

    @property
    def selection(self) -> dict | None:
        return self.data["selection"]

    @property
    def lines(self) -> list:
        return [line for line in self.err.splitlines()
                if line.startswith("ptest: ")]

    @property
    def start(self) -> str:
        return self.lines[0]

    @property
    def end(self) -> str:
        ends = [line for line in self.lines if _TOP_LINE.match(line)]
        assert ends, self.err[-2000:]
        return ends[-1]


# ------------------------------------------------------------------ rows

@pytest.fixture
def recorded(case):
    return Project.recorded(case)


def test_s1_body_edit_in_a_partial_file_runs_three_tests(recorded):
    recorded.edit_users_body()

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert re.fullmatch(r"ptest: passed · [\d.]+m?s", run.end), run.end
    assert GATE_HINT not in run.end and "deferred" not in run.end
    # The two users tests and test_mixed_users run; test_mixed_hub is
    # deselected from the partially selected file by the binding.
    assert recorded.ran() == USERS_TESTS
    selection = run.selection
    assert selection["run_now"] == 3
    assert selection["deferred"] == 0
    assert selection["gate"] is None


def test_s2_import_time_edit_defers_everything_and_runs_nothing(recorded):
    recorded.add_field("age: int")

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert run.start.endswith(
        f"{MODELS} → nothing to run now · deferred 20 ({SHARED_MODELS}) · "
        f"{GATE_HINT}"), run.start
    assert recorded.ran() == set()
    assert run.done.result is None  # R31: no result document


def test_s3_gate_learns_the_observer(recorded):
    recorded.after_s2()

    gate = recorded.run("--full")

    assert gate.code == 1, gate.err[-2000:]
    assert ("ptest: selection gate: ran 20 previously deferred tests · "
            "1 failed · 1 observer learned") in gate.lines
    assert not any("selection audit:" in line for line in gate.lines)
    assert recorded.ran() == ALL_TESTS
    assert recorded.sql("SELECT nodeid, path FROM observers") == [
        (OBSERVER, MODELS)]
    learned = gate.selection["gate"]
    assert learned["observers_learned"] == 1
    assert learned["unlearned"] == 0
    assert learned["demoted"] == 0


def test_s4_learned_observer_runs_and_its_neighbour_stays_deferred(recorded):
    recorded.learned()
    recorded.reseed(["age", "email", "name"], None)
    recorded.add_field("phone: str")

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 1, run.err[-2000:]
    assert f"deferred 19 ({SHARED_MODELS})" in run.end, run.end
    assert recorded.ran() == {OBSERVER}
    assert NEIGHBOUR not in recorded.ran()
    selection = run.selection
    assert selection["observers"]["promoted"] == 1
    assert selection["run_now"] == 1
    assert selection["deferred"] == 19


def test_s5_sibling_worktree_chooses_the_closest_record(recorded):
    tree_a = recorded.worktree("wt-a", "branch-a")
    recorded.add_field("age: int", tree_a)
    recorded.edit_users_body(tree_a)
    run_a = recorded.run("--base", "main", root=tree_a,
                         timeout=CHANGED_TIMEOUT)
    assert run_a.code == 0, run_a.err[-2000:]
    assert run_a.selection["run_now"] == 3
    assert run_a.selection["deferred"] == 17
    tree_b = recorded.worktree("wt-b", "branch-b")
    recorded.edit_total_body(tree_b)

    run_b = recorded.run("--base", "main", root=tree_b,
                         timeout=CHANGED_TIMEOUT)

    assert run_b.code == 0, run_b.err[-2000:]
    assert re.fullmatch(r"ptest: passed · [\d.]+m?s", run_b.end), run_b.end
    assert recorded.ran(tree_b) == ORDERS_TESTS
    assert run_b.selection["deferred"] == 0
    # The store holds two rows (the recorded run's and A's) for each
    # users test, and B did not touch them; the orders tests now have
    # the recorded run's row plus B's own.
    for nodeid in sorted(USERS_TESTS):
        rows = recorded.sql(
            "SELECT run_id FROM nodes WHERE nodeid = ?", (nodeid,))
        assert len({row[0] for row in rows}) == 2, (nodeid, rows)
    for nodeid in sorted(ORDERS_TESTS):
        rows = recorded.sql(
            "SELECT run_id FROM nodes WHERE nodeid = ?", (nodeid,))
        assert len({row[0] for row in rows}) == 2, (nodeid, rows)


def test_s6a_lockfile_bump_in_the_diff_runs_the_full_suite(recorded):
    recorded.git("checkout", "-q", "-b", "bump")
    (recorded.root / "uv.lock").write_text(
        (recorded.root / "uv.lock").read_text() + "# bump\n")
    head = recorded.commit("bump the lockfile")

    run = recorded.run("--base", "main", timeout=FULL_TIMEOUT)

    reason = "uv.lock is a full trigger"
    assert run.code == 0, run.err[-2000:]
    assert run.start.endswith(f"changed vs main → full suite: {reason}"), (
        run.start)
    assert re.fullmatch(
        r"ptest: passed · full suite \(uv\.lock is a full trigger\) · "
        r"[\d.]+m?s", run.end), run.end
    assert recorded.ran() == ALL_TESTS
    selection = run.selection
    assert selection["full_reason"] == reason
    assert selection["deferred"] == 0
    assert selection["gate"] is not None
    assert recorded.kv("last_gate_commit") == head  # R8a


def _bumped_on_main(case, reuse: bool = False) -> tuple:
    """S6b setup: a recorded fixture whose main carries a lockfile bump.

    The spec's ``age`` field breaks the snapshot test unless the snapshot
    already lists it, which a green S6b/S6c run needs; main therefore
    also carries the pre-staged snapshot (no run happens on main, so no
    record exists for it).
    """
    project = Project.recorded(case, reuse=reuse)
    (project.root / "uv.lock").write_text(
        (project.root / "uv.lock").read_text() + "# bump\n")
    project.write_snapshot(["age", "email", "name"])
    project.commit("bump the lockfile on main, stage the snapshot")
    tree = project.worktree("wt-age", "branch-age")
    project.add_field("age: int", tree)
    return project, tree


def test_s6b_lockfile_bump_already_on_the_base_falls_back_to_static(case):
    project, tree = _bumped_on_main(case)

    run = project.run("--base", "main", root=tree, timeout=FULL_TIMEOUT)

    reason = "10 of 10 test files reaches full_ratio 0.7"
    assert run.code == 0, run.err[-2000:]
    assert run.start.endswith(f"changed vs main → full suite: {reason}"), (
        run.start)
    assert re.fullmatch(
        rf"ptest: passed · full suite \({re.escape(reason)}\) · [\d.]+m?s",
        run.end), run.end
    assert project.ran(tree) == ALL_TESTS
    selection = run.selection
    assert selection["engine"] == "static"
    assert selection["full_reason"] == reason
    project.clear_ran(tree)

    project.edit_total_body(tree)
    again = project.run("--base", "main", root=tree,
                        timeout=CHANGED_TIMEOUT)

    assert again.code == 0, again.err[-2000:]
    assert project.ran(tree) == ORDERS_TESTS
    assert again.selection["engine"] == "dynamic"
    assert "deferred" not in again.end and "full suite" not in again.end


def test_s6c_second_worktree_after_the_bump_defers_honestly(case):
    project, tree = _bumped_on_main(case)
    seeded = project.run("--base", "main", root=tree, timeout=FULL_TIMEOUT)
    assert seeded.code == 0, seeded.err[-2000:]
    other = project.worktree("wt-total", "branch-total")
    project.edit_total_body(other)

    run = project.run("--base", "main", root=other,
                      timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert re.fullmatch(
        rf"ptest: passed · deferred 18 \({SHARED_MODELS}\) · [\d.]+m?s · "
        rf"{GATE_HINT}", run.end), run.end
    assert "full suite" not in run.err
    assert project.ran(other) == ORDERS_TESTS
    selection = run.selection
    assert selection["engine"] == "dynamic"
    assert selection["full_reason"] is None
    assert selection["deferred"] == 18


def test_skipped_launch_leaves_store_untouched(recorded):
    """S7 (spec row and named negative contract): a no-tests-needed stop
    changes no state row (D9) and, once the parse cache is warm, an
    immediate repeat leaves the whole store file byte-identical."""
    recorded.learned()
    recorded.reseed(["age", "email", "name"],
                    [sys.executable, "-m", "app.manifest"])
    recorded.add_static_norm()
    before = recorded.store_state()

    run = recorded.run("--result-json", "r.json", "-v", "--changed",
                       timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert "fingerprint: unchanged · 1 observer kept deferred" in run.err
    assert re.fullmatch(
        rf"ptest: no tests needed · deferred 20 \({SHARED_MODELS}\) · "
        rf"[\d.]+m?s · {GATE_HINT}", run.end), run.end
    assert recorded.ran() == set()  # the runner was never launched
    assert run.data["status"] == "no-tests-needed"
    selection = run.selection
    assert selection["observers"]["fingerprint"] == "unchanged"
    assert selection["observers"]["deferred"] == 1
    assert selection["run_now"] == 0
    assert selection["deferred"] == 20
    # No record, outcome, observer, fingerprint or gate marker changed.
    recorded.assert_state_unchanged(before)
    settled = recorded.store_digest()
    (recorded.root / "r.json").unlink()  # a result export is create-only

    repeat = recorded.run("--result-json", "r.json", "-v", "--changed",
                          timeout=CHANGED_TIMEOUT)

    assert repeat.code == 0, repeat.err[-2000:]
    assert repeat.end.startswith("ptest: no tests needed")
    assert recorded.ran() == set()
    assert recorded.store_digest() == settled  # byte-identical once warm


def test_s8_changed_fingerprint_promotes_the_observer(recorded):
    recorded.learned()
    recorded.reseed(["age", "email", "name"],
                    [sys.executable, "-m", "app.manifest"])
    recorded.add_field("phone: str")

    run = recorded.run("-v", "--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 1, run.err[-2000:]
    assert "fingerprint: changed · 1 observer promoted" in run.err
    assert f"deferred 19 ({SHARED_MODELS})" in run.end, run.end
    assert recorded.ran() == {OBSERVER}  # binding rewritten for this id
    assert run.selection["observers"]["promoted"] == 1


def test_s9_failing_fingerprint_command_counts_as_changed(recorded):
    recorded.learned()
    recorded.reseed(["age", "email", "name"],
                    [sys.executable, "-c", "import sys; sys.exit(3)"])
    recorded.add_static_norm()

    run = recorded.run("-v", "--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert ("fingerprint: absent (exit 3) · treated as changed · "
            "1 observer promoted") in run.err
    assert re.fullmatch(
        rf"ptest: passed · deferred 19 \({SHARED_MODELS}\) · [\d.]+m?s "
        rf"· {GATE_HINT}", run.end), run.end
    assert recorded.ran() == {OBSERVER}
    assert run.selection["observers"]["fingerprint"] == "absent"


def test_s11_importlib_router_and_lazy_state(recorded):
    recorded.edit("app/plugins/audit.py", '@route("/audit")',
                  '@route("/audit/v2")')
    recorded.edit_engine_body()

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert re.fullmatch(
        r"ptest: passed · deferred 18 \(shared import-time "
        rf"state via app/plugins/audit\.py\) · [\d.]+m?s · {GATE_HINT}",
        run.end), run.end
    assert recorded.ran() == CACHE_TESTS
    reasons = run.selection["deferred_reasons"]
    assert "app/plugins/audit.py" in reasons[0]["paths"]


def _spawn(project: Project, *args: str) -> subprocess.Popen:
    """The CLI as ``case.invoke`` starts it, but left running for a signal."""
    cmd = [sys.executable, "-m", "ptest",
           "--fixture-domain", str(project.domain.root), *args]
    env = {key: value for key, value in os.environ.items()
           if key not in CONTROL_VARS}
    return subprocess.Popen(
        cmd, cwd=str(project.root), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)


def _cancel_run(recorded: Project, mode: str) -> tuple:
    """S13 setup: S1 and S2 edits, ``ptest --changed`` held in its first
    test (``hold.flag`` mode), SIGINT once ``ran.txt`` shows it started.
    Returns ``(returncode, stderr, store state before)``."""
    recorded.edit_users_body()
    recorded.add_field("age: int")
    hold = recorded.root / HOLD_FLAG
    hold.write_text(mode + "\n")
    before = recorded.store_state()
    proc = _spawn(recorded, "--changed")
    try:
        deadline = time.monotonic() + 60
        while not recorded.ran() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert recorded.ran(), "the first test never started"
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=30)
        hold.unlink(missing_ok=True)
    return proc.returncode, stderr.decode("utf-8", "replace"), before


def _next_changed_run_is_intact(recorded: Project, before: dict) -> None:
    recorded.assert_state_unchanged(before)
    recorded.clear_ran()

    again = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert again.code == 0, again.err[-2000:]
    assert recorded.ran() == USERS_TESTS
    assert again.selection["run_now"] == 3
    assert again.selection["deferred"] == 17


def test_s13_cancelled_run_leaves_the_store_alone_runner_ignoring_sigint(
        recorded):
    """A runner that ignores SIGINT is killed after the grace: ptest's own
    `cancelled` (exit 130); the records stay untouched."""
    code, err, before = _cancel_run(recorded, "stuck")

    assert code == 130, err[-2000:]
    assert "ptest: cancelled" in err
    _next_changed_run_is_intact(recorded, before)


def test_s13_cancelled_run_leaves_the_store_alone_cooperative_runner(
        recorded):
    """Pinned existing behaviour, not the spec's row: a pytest that leaves
    on its own KeyboardInterrupt ends with its runner status (exit 2,
    `test_real_cancellation_reaps_guard_and_releases_checkout`); the
    records stay untouched. The spec's `cancelled (exit 130)` for this
    plain run is the xfail row below."""
    code, err, before = _cancel_run(recorded, "cooperative")

    assert code == 2, err[-2000:]
    _next_changed_run_is_intact(recorded, before)


@pytest.mark.xfail(strict=True, reason=SPEC_DELTA + "S13: the spec ends an "
                   "ordinary SIGINT'd run `cancelled` (exit 130); the "
                   "product lets the runner's own exit 2 win")
def test_s13_spec_plain_sigint_run_ends_cancelled_130(recorded):
    code, err, _ = _cancel_run(recorded, "cooperative")

    assert code == 130, err[-2000:]
    assert "ptest: cancelled" in err


def test_s14_unknown_schema_falls_back_to_static_and_never_writes(recorded):
    recorded.sql_write(
        "UPDATE kv SET value = '99' WHERE key = 'schema_version'")
    recorded.edit_users_body()
    before = recorded.store_digest()

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert ("→ 2 of 10 test files (static: dependency store unavailable"
            in run.start), run.start
    assert run.end.endswith(f"· {GATE_HINT} (no gate recorded)"), run.end
    assert recorded.store_digest() == before


def test_s15a_empty_diff_after_a_green_run_has_nothing_to_test(recorded):
    run = recorded.run(timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert ("ptest: no changes since last green run — nothing to test"
            in run.lines), run.lines
    assert not any(GATE_HINT in line for line in run.lines)
    assert recorded.ran() == set()


def test_s15b_empty_diff_still_reports_the_deferred_tests(recorded):
    recorded.edit_users_body()
    recorded.add_field("age: int")
    first = recorded.run(timeout=CHANGED_TIMEOUT)
    assert first.code == 0, first.err[-2000:]
    assert recorded.ran() == USERS_TESTS
    assert first.selection["deferred"] == 17
    recorded.clear_ran()

    second = recorded.run(timeout=CHANGED_TIMEOUT)

    assert second.code == 0, second.err[-2000:]
    assert (f"ptest: no changes since last green run — nothing to run now "
            f"· deferred 17 ({SHARED_MODELS}) · {GATE_HINT}"
            in second.lines), second.lines
    assert recorded.ran() == set()


def test_s16_vitest_delegation_reports_the_engine(case, fake_exec_node):
    from test_operations import _run_data
    from test_vitest_full_reuse import _invoke, _project

    domain = case.domain()
    root = _project(case, domain, fake_exec_node)
    first = _invoke(case, domain, root, "--full", timeout=20)
    assert first.code == 0, first.stderr.decode()
    (root / "sample.test.ts").write_text(
        'test("one", () => { /* edit */ })\n')
    head = support.git(root, "rev-parse", "HEAD")
    support.git_commit_all(root, "edit the sample test")

    changed = _invoke(case, domain, root, "--changed", timeout=20)

    err = changed.stderr.decode()
    assert changed.code == 0, err
    assert re.search(r"→ vitest --changed \S+", err), err
    ends = [line for line in err.splitlines()
            if line.startswith("ptest: passed")]
    assert len(ends) == 1 and ends[0].endswith(f" · {GATE_HINT}"), ends
    assert "no gate recorded" not in ends[0]
    record = json.loads((root / "node-record.json").read_text())
    assert any(token == "--changed" for token in record["argv"]), record
    assert head in record["argv"] or any(
        token.startswith(head[:7]) for token in record["argv"]), record
    selection = _run_data(changed)["selection"]
    assert selection["engine"] == "vitest-changed"
    assert selection["deferred"] is None


def test_s17_deferral_since_the_gate_survives_a_moved_green_point(recorded):
    recorded.sql_write("DELETE FROM nodes WHERE test_file = ?",
                       ("tests/test_orders.py",))
    recorded.edit_total_body()
    recorded.edit_users_body()
    note = "deferred 2 (unrecorded since gate via app/svc/orders.py)"

    first = recorded.run(timeout=CHANGED_TIMEOUT)

    assert first.code == 0, first.err[-2000:]
    assert re.fullmatch(
        rf"ptest: passed · {re.escape(note)} · [\d.]+m?s · {GATE_HINT}",
        first.end), first.end
    assert recorded.ran() == USERS_TESTS
    assert not recorded.ran() & ORDERS_TESTS
    recorded.clear_ran()

    second = recorded.run(timeout=CHANGED_TIMEOUT)

    assert second.code == 0, second.err[-2000:]
    assert (f"ptest: no changes since last green run — nothing to run now "
            f"· {note} · {GATE_HINT}" in second.lines), second.lines
    assert recorded.ran() == set()


# ------------------------------------------- spec end-line wording (S rows)
# The spec writes counted serial end lines (`passed · 3 tests`, `1 failed,
# 19 passed`). Counts come from the advanced tier's verified native
# evidence, so these rows build the fixture with `reuse = true` (see the
# module docstring) and assert each spec wording exactly.

def _row_s1(case):
    project = Project.recorded(case, reuse=True)
    project.edit_users_body()
    return project.run("--changed", timeout=CHANGED_TIMEOUT)


def _row_s3(case):
    project = Project.recorded(case, reuse=True)
    project.after_s2()
    return project.run("--full")


def _row_s4(case):
    project = Project.recorded(case, reuse=True)
    project.learned()
    project.reseed(["age", "email", "name"], None)
    project.add_field("phone: str")
    return project.run("--changed", timeout=CHANGED_TIMEOUT)


def _row_s5(case):
    project = Project.recorded(case, reuse=True)
    tree_a = project.worktree("wt-a", "branch-a")
    project.add_field("age: int", tree_a)
    project.edit_users_body(tree_a)
    project.run("--base", "main", root=tree_a, timeout=CHANGED_TIMEOUT)
    tree_b = project.worktree("wt-b", "branch-b")
    project.edit_total_body(tree_b)
    return project.run("--base", "main", root=tree_b,
                       timeout=CHANGED_TIMEOUT)


def _row_s6a(case):
    project = Project.recorded(case, reuse=True)
    project.git("checkout", "-q", "-b", "bump")
    (project.root / "uv.lock").write_text(
        (project.root / "uv.lock").read_text() + "# bump\n")
    project.commit("bump the lockfile")
    return project.run("--base", "main", timeout=FULL_TIMEOUT)


def _row_s6c(case):
    project, tree = _bumped_on_main(case, reuse=True)
    project.run("--base", "main", root=tree, timeout=FULL_TIMEOUT)
    other = project.worktree("wt-total", "branch-total")
    project.edit_total_body(other)
    return project.run("--base", "main", root=other,
                       timeout=CHANGED_TIMEOUT)


def _row_s8(case):
    project = Project.recorded(case, reuse=True)
    project.learned()
    project.reseed(["age", "email", "name"],
                   [sys.executable, "-m", "app.manifest"])
    project.add_field("phone: str")
    return project.run("-v", "--changed", timeout=CHANGED_TIMEOUT)


def _row_s9(case):
    project = Project.recorded(case, reuse=True)
    project.learned()
    project.reseed(["age", "email", "name"],
                   [sys.executable, "-c", "import sys; sys.exit(3)"])
    project.add_static_norm()
    return project.run("-v", "--changed", timeout=CHANGED_TIMEOUT)


def _row_s11(case):
    project = Project.recorded(case, reuse=True)
    project.edit("app/plugins/audit.py", '@route("/audit")',
                 '@route("/audit/v2")')
    project.edit_engine_body()
    return project.run("--changed", timeout=CHANGED_TIMEOUT)


def _row_s17(case):
    project = Project.recorded(case, reuse=True)
    project.sql_write("DELETE FROM nodes WHERE test_file = ?",
                      ("tests/test_orders.py",))
    project.edit_total_body()
    project.edit_users_body()
    return project.run(timeout=CHANGED_TIMEOUT)


_T = r"[\d.]+m?s"
_SPEC_END_LINES = {
    "S1": (_row_s1, rf"ptest: passed · 3 tests · {_T}"),
    "S3": (_row_s3, r"ptest: failed · 1 failed, 19 passed\b.*"),
    "S4": (_row_s4, rf"ptest: failed · 1 failed, 0 passed · deferred 19 "
                    rf"\({SHARED_MODELS}\) · {_T} \(exit 1\) · fix the code "
                    rf"under test, then rerun ptest"),
    "S5": (_row_s5, rf"ptest: passed · 2 tests · {_T}"),
    "S6a": (_row_s6a, rf"ptest: passed · 20 tests · full suite "
                      rf"\(uv\.lock is a full trigger\) · {_T}"),
    "S6c": (_row_s6c, rf"ptest: passed · 2 tests · deferred 18 "
                      rf"\({SHARED_MODELS}\) · {_T} · {GATE_HINT}"),
    "S8": (_row_s8, rf"ptest: failed · 1 failed, 0 passed · deferred 19 "
                    rf"\({SHARED_MODELS}\).*"),
    "S9": (_row_s9, rf"ptest: passed · 1 test · deferred 19 "
                    rf"\({SHARED_MODELS}\) · {_T} · {GATE_HINT}"),
    "S11": (_row_s11, rf"ptest: passed · 2 tests · deferred 18 \(shared "
                      rf"import-time state via app/plugins/audit\.py\) · "
                      rf"{_T} · {GATE_HINT}"),
    "S17": (_row_s17, rf"ptest: passed · 3 tests · deferred 2 \(unrecorded "
                      rf"since gate via app/svc/orders\.py\) · {_T} · "
                      rf"{GATE_HINT}"),
}


@pytest.mark.parametrize("row", sorted(_SPEC_END_LINES))
def test_spec_counted_end_line(case, row):
    setup, pattern = _SPEC_END_LINES[row]

    run = setup(case)

    assert re.fullmatch(pattern, run.end), (row, run.end)


# ------------------------------------------------- named negative contracts

MARKER = "FPMARKER-5c1e9a"


def _store_files_bytes(project: Project) -> bytes:
    """Every byte of the project's store directory (db, journal, wal)."""
    parts = []
    for path in sorted(project.store_path.parent.rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes())
    return b"".join(parts)


def test_writing_fingerprint_command_fails_closed(recorded, tmp_path):
    """R43 (named negative contract): a fingerprint command that writes an
    input (`app/settings.py`) changes the checkout under the run: the run
    ends `incomplete` with `changed-during-run` (exit 70), never
    `no tests needed` or green.

    The selective path with a kept-deferred conditional observer, where
    the R44 no-launch stop would otherwise end the run at exit 0 and no
    runner-side guard can trip. The writer is committed by the reseed (an
    uncommitted `.ptest.toml` is a full trigger, R46) and only writes once
    its flag, outside the checkout, exists; its stdout is constant, so the
    fingerprint stays unchanged and the stop is reached."""
    recorded.learned()
    flag = tmp_path / "fp-write.flag"
    writer = [sys.executable, "-c",
              "import os, sys; sys.stdout.write('fp\\n'); "
              f"os.path.exists({str(flag)!r}) and "
              "open('app/settings.py', 'a').write('# touched\\n')"]
    recorded.reseed(["age", "email", "name"], writer)  # flag absent: green
    assert "# touched" not in (
        recorded.root / "app" / "settings.py").read_text()
    flag.write_text("")
    recorded.add_static_norm()

    run = recorded.run("--result-json", "r.json", "-v", "--changed",
                       timeout=CHANGED_TIMEOUT)

    assert "full suite" not in run.err, run.err[-2000:]
    assert "fingerprint: unchanged · 1 observer kept deferred" in run.err, (
        run.err[-2000:])  # the R44 stop path was reached
    assert run.code == 70, run.err[-2000:]
    assert "changed-during-run" in run.err, run.err[-2000:]
    assert re.search(r"^ptest: incomplete\b", run.err, re.M), run.err[-2000:]
    assert "ptest: no tests needed" not in run.err
    assert not re.search(r"^ptest: passed\b", run.err, re.M)
    assert "# touched" in (
        recorded.root / "app" / "settings.py").read_text()  # it wrote
    result = json.loads((recorded.root / "r.json").read_text())
    assert result["data"]["status"] == "incomplete", result["data"]["status"]
    assert any(reason["code"] == "changed-during-run"
               for reason in result["data"]["reasons"]), result["data"]


def test_fingerprint_output_never_printed_or_stored(recorded):
    """The fingerprint command's stdout and stderr are digested, never
    echoed: a marker it prints appears in no CLI stream, no result JSON
    and no stored byte."""
    recorded.learned()
    emitter = [sys.executable, "-c",
               f"import sys; print('{MARKER}-out'); "
               f"print('{MARKER}-err', file=sys.stderr)"]
    recorded.reseed(["age", "email", "name"], emitter)
    recorded.add_static_norm()

    run = recorded.run("--result-json", "r.json", "-v", "--changed",
                       timeout=CHANGED_TIMEOUT)

    assert "fingerprint:" in run.err, run.err[-2000:]  # it did run
    assert MARKER not in run.err
    assert MARKER not in run.out
    result_bytes = (recorded.root / "r.json").read_bytes()
    assert MARKER.encode() not in result_bytes
    assert MARKER.encode() not in _store_files_bytes(recorded)
