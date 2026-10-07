"""Readiness-gated CLI end-to-end selection tests (T5).

Integration boundary: subprocess CLI (``case.invoke``, the
``test_pytest_scoped_subprocess`` pattern: ``case.project`` inside the
fixture domain) on an inline tmp git project, with a fixture domain
and real pytest through the bridge. Each test records dependencies
with a green ``--full`` run, edits one thing, then asserts the
``--changed`` (or ``--base``) run's note and the exact set of
executed tests (each test appends its name to ``ran.txt``).

The seven recorded tests live in three test files on purpose: with a
single test file every narrowing selection would trip the D8
``full_ratio`` file conversion (``1 of 1 test files``) and no
narrowing case could be demonstrated. Grouped files still exercise
node-level deselect inside partially selected files.

"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

import support


_FULL_TIMEOUT = 120
_CHANGED_TIMEOUT = 90

_CORE = """\
LIMIT = 3

def alpha(n):
    return n + 1

def beta(n):
    return n * 2

def checked(name):
    import pkg.core as core
    return getattr(core, name)(1)
"""

_MARK = """\
def _mark(name):
    with open("ran.txt", "a") as fh:
        fh.write(name + "\\n")

"""

_TEST_CORE = """\
import pkg.core as core


def test_alpha():
    _mark("alpha")
    assert core.alpha(1) == 2


def test_beta():
    _mark("beta")
    assert core.beta(2) == 4
"""

_TEST_LIMIT = """\
import pkg.core as core


def test_limit():
    _mark("limit")
    assert core.LIMIT == 3


def test_glimit():
    _mark("glimit")
    assert getattr(core, "LIMIT") == 3
"""

_TEST_MISC = """\
import json
import pkg.core as core
from helpers import helper_alpha


def test_data():
    _mark("data")
    assert json.load(open("tests/data/config.json"))["mode"] == "fast"


def test_helper():
    _mark("helper")
    assert helper_alpha(1) == 2


def test_checked():
    _mark("checked")
    assert core.checked("alpha") == 2
"""

_HELPERS = """\
def helper_alpha(n):
    import pkg.core as core
    return core.alpha(n)
"""

_ALL = {"alpha", "beta", "limit", "glimit", "data", "helper", "checked"}


def _make_project(case, domain) -> Path:
    """Inline git project inside the fixture domain (containment rule).

    ``case.project`` creates the root under ``domain.root`` so the
    scheduler's fixture-containment check passes; the command-kind stub
    config is then replaced with the pytest selection project.
    """
    root = case.project(domain, kind="pytest")
    project_id = tomllib.loads(
        (root / ".ptest.toml").read_text())["project_id"]
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "data").mkdir(exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\nran.txt\nptest-result-*.json\n")
    (root / "pkg" / "core.py").write_text(_CORE)
    (root / "tests" / "data" / "config.json").write_text(
        '{"mode": "fast"}\n')
    (root / "tests" / "helpers.py").write_text(_HELPERS)
    (root / "tests" / "test_core.py").write_text(_MARK + _TEST_CORE)
    (root / "tests" / "test_limit.py").write_text(_MARK + _TEST_LIMIT)
    (root / "tests" / "test_misc.py").write_text(_MARK + _TEST_MISC)
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-q", "-p", "no:xdist"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "\n[selection]\n"
        "enabled = true\n")
    support.init_git_repo(root, message="base")
    return root


@pytest.fixture
def recorded(case):
    """Git project with a green full run (dependencies recorded)."""
    domain = case.domain()
    root = _make_project(case, domain)
    full = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)
    assert full.code == 0, full.stderr.decode()[-2000:]
    assert _markers(root) == _ALL
    (root / "ran.txt").unlink()
    return domain, root


def _markers(root: Path) -> set:
    path = root / "ran.txt"
    if not path.exists():
        return set()
    return set(path.read_text().split())


def _changed(case, domain, root, *extra):
    result = case.invoke(domain, root, "--changed", *extra,
                         timeout=_CHANGED_TIMEOUT)
    return result, result.stderr.decode()


def test_edit_function_body_runs_only_its_tests(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(
        _CORE.replace("return n + 1", "return 1 + n"))

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert "(dynamic · 1 function changed)" in err
    # Node-level reach through recorded calls: the direct test plus the
    # transitive callers (helpers.helper_alpha, checked() by name). The
    # other four tests stay out; same-file test_beta is deselected.
    assert _markers(root) == {"alpha", "helper", "checked"}


def test_edit_constant_runs_referencing_tests(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(_CORE.replace("LIMIT = 3",
                                                        "LIMIT = 1 + 2"))

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert "(dynamic · 1 name changed)" in err
    # Only the direct reader is selected; the getattr reader (test_glimit)
    # is the R2 blind spot the self-audit exists for (case 7).
    assert _markers(root) == {"limit"}


def test_effectful_change_selects_static_reach(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(
        _CORE + "\nprint('effect-marker')\n")

    result, err = _changed(case, domain, root, "-v")

    # A module-level effect taints every record stale, so the dynamic
    # decision selects all recorded tests and D8 converts to full. The
    # -v unit line proves the effectful-module mechanism behind it.
    assert result.code == 0, err[-2000:]
    assert ("→ full suite: 7 of 7 recorded tests reach "
            "full_ratio 0.7 (dynamic)") in err
    assert "changed module pkg/core.py" in err
    assert _markers(root) == _ALL


def test_empty_store_reports_static_note(case):
    domain = case.domain()
    root = _make_project(case, domain)
    # A test-file edit keeps the static verdict a selection (not a
    # full-suite verdict, which carries no static reason): with no
    # store the note names the empty-store reason.
    (root / "tests" / "test_core.py").write_text(
        (_MARK + _TEST_CORE).replace("assert core.alpha(1) == 2",
                                     "assert core.alpha(1) == 1 + 1"))

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert ("static: no dependency records yet — any run records them"
            in err)


def test_second_worktree_reports_no_tests_affected(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(
        _CORE.replace("return n + 1", "return 1 + n"))
    support.git(root, "add", "-A")
    support.git(root, "commit", "-q", "-m", "semantics-preserving edit")
    base = support.git(root, "rev-parse", "HEAD~1").strip()
    rerun = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)
    assert rerun.code == 0, rerun.stderr.decode()[-2000:]
    (root / "ran.txt").unlink()

    other = domain.root / "wt2"
    support.git(root, "worktree", "add", str(other), "HEAD")
    try:
        result = case.invoke(domain, other, "--changed", "--base", base,
                             timeout=_CHANGED_TIMEOUT)
        err = result.stderr.decode()
        assert result.code == 0, err[-2000:]
        assert "no tests affected:" in err
        assert "and already passed on this code" in err
        assert not (other / "ran.txt").exists()
    finally:
        support.git(root, "worktree", "remove", "--force", str(other))


def test_failing_test_always_reruns(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(
        _CORE.replace("return n * 2", "return n * 3"))

    result, err = _changed(case, domain, root)

    assert result.code == 1, err[-2000:]
    assert "beta" in _markers(root)


def test_self_audit_miss_demotes_then_selects(recorded, case):
    domain, root = recorded
    (root / "pkg" / "core.py").write_text(_CORE.replace("LIMIT = 3",
                                                        "LIMIT = 4"))

    first, first_err = _changed(case, domain, root)
    assert first.code == 1, first_err[-2000:]
    assert "limit" in _markers(root)
    assert "glimit" not in _markers(root)

    audit = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)
    audit_err = audit.stderr.decode()
    assert audit.code == 1, audit_err[-2000:]
    assert ("ptest: selection audit: 1 failing test would not have been "
            "selected" in audit_err)

    (root / "pkg" / "core.py").write_text(
        (root / "pkg" / "core.py").read_text().replace(
            "return n + 1", "return 1 + n"))
    second, second_err = _changed(case, domain, root)
    assert second.code == 1, second_err[-2000:]
    assert "glimit" in _markers(root)


def test_edit_single_test_function_deselects_rest(recorded, case):
    domain, root = recorded
    (root / "tests" / "test_core.py").write_text(
        (_MARK + _TEST_CORE).replace("assert core.alpha(1) == 2",
                                     "assert core.alpha(1) == 1 + 1"))

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert "(dynamic · " in err
    assert _markers(root) == {"alpha"}


def test_edit_data_file_runs_only_reader(recorded, case):
    domain, root = recorded
    # Under a test root so the change stays inside the code area
    # (impact drops data files where no test could reach them).
    (root / "tests" / "data" / "config.json").write_text(
        '{"mode": "fast" }\n')

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert "(dynamic · " in err
    assert "full suite" not in err
    assert _markers(root) == {"data"}


def test_edit_test_support_runs_only_caller(recorded, case):
    domain, root = recorded
    (root / "tests" / "helpers.py").write_text(
        _HELPERS.replace("return core.alpha(n)",
                         "return core.alpha(n + 0)"))

    result, err = _changed(case, domain, root)

    assert result.code == 0, err[-2000:]
    assert "(dynamic · " in err
    assert "full suite" not in err
    assert _markers(root) == {"helper"}
