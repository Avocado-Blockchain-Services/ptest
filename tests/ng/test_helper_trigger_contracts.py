"""Negative contracts for test helpers that are not in ``full_triggers``.

Oracle: the helper-full-triggers design (requirement 4). A test-support
module such as ``tests/factories/users.py`` no longer sits in
``selection.full_triggers``; this file pins what must still hold:

* records absent: a helper edit stays a full run, never a narrowed one;
* records present: a helper body edit runs the tests that executed it,
  and an import-time registration defers instead of running everything;
* a deliberate trigger entry still forces the full suite;
* ``ptest --full`` ignores ``full_triggers`` and a narrowed green still
  reports the gate as owed.

Static rows call ``impact.plan`` on a real git repository; the dynamic
rows drive the real CLI as a subprocess (``case.invoke``) against a small
committed fixture repository built here. Nothing in ptest is patched
except the scan cap that forces the legacy tail.
"""
from __future__ import annotations

import json
import sys

import pytest

from ptest import contracts as C
from ptest import impact as I
from support import git_commit_all, init_git_repo, write_file

PID = "cd" * 16
HELPER = "tests/factories/users.py"
GATE_HINT = "gate at spec end"
RUN_TIMEOUT = 120


# ----------------------------------------------------------- static rows

def _config(**selection_over):
    selection = {"enabled": True, "closed_inputs": False}
    selection.update(selection_over)
    return C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.PYTEST, launcher=("uv",),
                              test_roots=("tests",)),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(**selection),
        project_id=PID,
    )


def _static_repo(tmp_path):
    files = {
        "tests/__init__.py": "",
        "tests/factories/__init__.py": "",
        HELPER: "def build_user():\n    return {'name': 'ada'}\n",
        "tests/test_users.py": (
            "from tests.factories.users import build_user\n\n\n"
            "def test_user():\n    assert build_user()\n"),
    }
    for index in range(3):
        files[f"tests/test_x{index}.py"] = "def test_x():\n    assert True\n"
    return init_git_repo(tmp_path, files=files)


def test_records_absent_helper_edit_is_full_test_support_indexed(tmp_path):
    # Oracle: with full_triggers lacking the helper, the static fallback
    # (the only planner without records) must stay full, for the reason
    # "test support" and not a trigger reason.
    _static_repo(tmp_path)
    config = _config(full_triggers=[".ptest.toml"])

    impact = I.plan(tmp_path, tmp_path, config, (HELPER,))

    assert impact.kind == "full"
    assert impact.reason == f"{HELPER} is test support"
    assert "full trigger" not in impact.reason
    assert impact.files == ()  # returned before any selection or full_ratio


def test_records_absent_helper_edit_is_full_test_support_legacy_tail(
        tmp_path, monkeypatch):
    # Oracle: the incomplete-index path (_legacy_tail, forced by a scan
    # cap of one file) gives the same verdict as the indexed path.
    _static_repo(tmp_path)
    monkeypatch.setattr(I, "MAX_SCAN_FILES", 1)
    config = _config(full_triggers=[".ptest.toml"])

    impact = I.plan(tmp_path, tmp_path, config, (HELPER,))

    assert impact.kind == "full"
    assert impact.reason == f"{HELPER} is test support"
    assert impact.engine == ""  # the legacy tail, not the indexed planner


@pytest.mark.parametrize("entry", [HELPER, "tests/factories"])
def test_declared_deliberate_helper_trigger_still_forces_full(
        tmp_path, entry):
    # Oracle: a helper kept on purpose in full_triggers (file or directory
    # entry) keeps the "<path> is a full trigger" verdict.
    _static_repo(tmp_path)
    config = _config(full_triggers=[".ptest.toml", entry])

    impact = I.plan(tmp_path, tmp_path, config, (HELPER,))

    assert impact.kind == "full"
    assert impact.reason == f"{HELPER} is a full trigger"


def test_unlisted_sibling_helper_is_not_a_trigger_reason(tmp_path):
    # Oracle: only the declared entry triggers; a different helper edit
    # keeps the test-support reason (the entry does not leak by prefix).
    _static_repo(tmp_path)
    write_file(tmp_path / "tests/helpers_other.py", "X = 1\n")
    config = _config(full_triggers=[".ptest.toml", HELPER])

    impact = I.plan(tmp_path, tmp_path, config, ("tests/helpers_other.py",))

    assert impact.kind == "full"
    assert impact.reason == "tests/helpers_other.py is test support"


# ---------------------------------------------------------- dynamic rows

_CONFTEST = (
    "from pathlib import Path\n\n"
    "import pytest\n\n"
    "_RAN = Path(__file__).resolve().parent.parent / 'ran.txt'\n\n\n"
    "@pytest.fixture(autouse=True)\n"
    "def _record_ran(request):\n"
    "    with open(_RAN, 'a') as fh:\n"
    "        fh.write(request.node.nodeid + '\\n')\n"
    "    yield\n")

_FACTORIES_INIT = (
    "from tests.factories import users\n\n"
    "REGISTRY = {}\n\n\n"
    "def register(name, builder):\n"
    "    REGISTRY[name] = builder\n\n\n"
    "register('user', users.build_user)\n")

_USERS = (
    "def build_user():\n"
    "    return {'name': 'ada'}\n")

_ORDERS = (
    "def build_order():\n"
    "    return {'total': 3}\n")

_TEST_USERS = (
    "from tests.factories import REGISTRY\n\n\n"
    "def test_user_has_name():\n"
    "    assert REGISTRY['user']()['name'] == 'ada'\n\n\n"
    "def test_user_is_dict():\n"
    "    assert isinstance(REGISTRY['user'](), dict)\n")

_TEST_OTHER = (
    "from tests.factories import REGISTRY\n\n\n"
    "def test_other_a():\n"
    "    assert 'user' in REGISTRY\n\n\n"
    "def test_other_b():\n"
    "    assert 1 + 1 == 2\n")

_TEST_ORDERS = (
    "from tests.factories import REGISTRY\n\n\n"
    "def test_order_total():\n"
    "    assert REGISTRY['order']()['total'] == 3\n")

_TEST_ORDERS_DIRECT = (
    "from tests.factories import orders\n\n\n"
    "def test_order_total():\n"
    "    assert orders.build_order()['total'] == 3\n")

ALL = frozenset({
    "tests/test_users.py::test_user_has_name",
    "tests/test_users.py::test_user_is_dict",
    "tests/test_other.py::test_other_a",
    "tests/test_other.py::test_other_b"})
USERS = frozenset(n for n in ALL if n.startswith("tests/test_users.py"))
NEW_ORDER = "tests/test_orders.py::test_order_total"


def _config_text(project_id, full_triggers=(".ptest.toml",)):
    return (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-q", "-p", "no:xdist"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n\n'
        "[selection]\n"
        "enabled = true\n"
        f"full_triggers = {json.dumps(sorted(full_triggers))}\n")


class _Fixture:
    """A committed fixture repository after one green ``--full`` run."""

    def __init__(self, case, full_triggers=(".ptest.toml",)):
        import tomllib

        self.case = case
        self.domain = case.domain(slots=1, jobs=1)
        self.root = case.project(self.domain, kind="pytest")
        project_id = tomllib.loads(
            (self.root / ".ptest.toml").read_text())["project_id"]
        files = {
            ".gitignore": ("__pycache__/\n.pytest_cache/\nran.txt\n"
                           "ptest-result-*.json\n"),
            ".ptest.toml": _config_text(project_id, full_triggers),
            "tests/__init__.py": "",
            "tests/conftest.py": _CONFTEST,
            "tests/factories/__init__.py": _FACTORIES_INIT,
            HELPER: _USERS,
            "tests/test_users.py": _TEST_USERS,
            "tests/test_other.py": _TEST_OTHER,
        }
        for relative, text in files.items():
            write_file(self.root / relative, text)
        init_git_repo(self.root, message="base")
        git_commit_all(self.root, "base")
        first = self.run("--full")
        assert first.code == 0, first.err[-2000:]
        assert self.ran() == ALL, self.ran()
        self.clear()

    def run(self, *args):
        done = self.case.invoke(self.domain, self.root, *args,
                                timeout=RUN_TIMEOUT)
        return _Run(done)

    def ran(self):
        path = self.root / "ran.txt"
        return set(path.read_text().split()) if path.exists() else set()

    def clear(self):
        path = self.root / "ran.txt"
        if path.exists():
            path.unlink()

    def edit(self, relative, old, new):
        path = self.root / relative
        text = path.read_text()
        assert old in text, (relative, old)
        path.write_text(text.replace(old, new, 1))


class _Run:
    def __init__(self, done):
        self.done = done
        self.code = done.code
        self.err = done.stderr.decode("utf-8", "replace")

    @property
    def end(self):
        lines = [line for line in self.err.splitlines()
                 if line.startswith("ptest: passed")
                 or line.startswith("ptest: failed")]
        assert lines, self.err[-2000:]
        return lines[-1]


@pytest.fixture
def recorded(case):
    return _Fixture(case)


def test_body_edit_to_helper_runs_only_the_tests_that_executed_it(recorded):
    # Oracle: with usable records and the helper absent from full_triggers,
    # a body change (no import-time effect) runs the tests that executed
    # build_user, not the whole suite and not nothing. test_other imports
    # the package but never calls the helper, so it stays out.
    recorded.edit(HELPER, "{'name': 'ada'}", "{'name': 'ada', 'v': 2}")

    run = recorded.run("--changed")

    assert run.code == 0, run.err[-2000:]
    assert recorded.ran() == USERS
    assert "full trigger" not in run.err
    assert "test support" not in run.err


def test_adding_a_factory_import_line_runs_the_new_test_and_defers_the_rest(
        recorded):
    # Oracle: the demo result (.pipeline/progress.md): one factory, one
    # import line appended to tests/factories/__init__.py, one new test that
    # imports the factory directly. The edit reaches the other tests only
    # through import-time reach (g-reach on the new factory), so only the
    # new test runs and the rest are deferred to the spec-end gate.
    write_file(recorded.root / "tests/factories/orders.py", _ORDERS)
    write_file(recorded.root / "tests/test_orders.py", _TEST_ORDERS_DIRECT)
    recorded.edit("tests/factories/__init__.py",
                  "register('user', users.build_user)\n",
                  "register('user', users.build_user)\n"
                  "from tests.factories import orders  # noqa: E402,F401\n")

    run = recorded.run("--changed")

    assert run.code == 0, run.err[-2000:]
    assert recorded.ran() == {NEW_ORDER}
    selection = run.done.result["data"]["selection"]
    assert selection["run_now"] == 1, selection
    assert selection["deferred"] == 4, selection
    assert selection["deferred_reasons"] == [
        {"code": "g-reach", "paths": ["tests/factories/orders.py"],
         "tests": 4}], selection
    assert ("deferred 4 (import-time reach via tests/factories/orders.py)"
            in run.end), run.end
    assert GATE_HINT in run.end


def test_registering_a_new_factory_runs_the_new_test_and_defers_the_rest(
        recorded):
    # Oracle: a top-level register() call in tests/factories/__init__.py
    # mutates shared import-time state, so the deferral tier is g-shared
    # (a different branch from the plain import line above): only the
    # reached new test runs, the others are deferred to the spec-end gate.
    write_file(recorded.root / "tests/factories/orders.py", _ORDERS)
    write_file(recorded.root / "tests/test_orders.py", _TEST_ORDERS)
    recorded.edit("tests/factories/__init__.py",
                  "register('user', users.build_user)\n",
                  "register('user', users.build_user)\n"
                  "from tests.factories import orders  # noqa: E402\n"
                  "register('order', orders.build_order)\n")

    run = recorded.run("--changed")

    assert run.code == 0, run.err[-2000:]
    assert recorded.ran() == {NEW_ORDER}
    assert ("deferred 4 (shared import-time state via "
            "tests/factories/__init__.py)") in run.end, run.end
    assert GATE_HINT in run.end


def test_full_ignores_full_triggers_and_runs_the_whole_suite(case):
    # Oracle: ptest --full semantics are unchanged by this feature: it runs
    # everything whatever full_triggers holds (here a deliberate helper
    # entry), so the whole suite runs after a helper edit.
    fixture = _Fixture(case, full_triggers=(".ptest.toml", HELPER))
    fixture.edit(HELPER, "{'name': 'ada'}", "{'name': 'ada', 'v': 2}")

    run = fixture.run("--full")

    assert run.code == 0, run.err[-2000:]
    assert fixture.ran() == ALL


def test_deliberate_helper_trigger_runs_the_full_suite_end_to_end(case):
    # Oracle: a declared deliberate trigger still forces the full suite
    # through the CLI, and the start line names the trigger reason.
    fixture = _Fixture(case, full_triggers=(".ptest.toml", HELPER))
    fixture.edit(HELPER, "{'name': 'ada'}", "{'name': 'ada', 'v': 2}")

    run = fixture.run("--changed")

    assert run.code == 0, run.err[-2000:]
    assert fixture.ran() == ALL
    assert f"{HELPER} is a full trigger" in run.err
