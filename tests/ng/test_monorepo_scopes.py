from __future__ import annotations

from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import monorepo


def _children():
    return tuple(monorepo.ChildTarget(declaration=name, directory=Path(name), config=None)
                 for name in ("api", "web"))


@pytest.mark.parametrize(("scope", "rebased"), [
    ("api/tests", "tests"),
    ("api/tests/", "tests"),
    ("./api/tests/", "tests"),
    ("api/tests/test_a.py", "tests/test_a.py"),
    ("api/tests/test_a.py::test_x", "tests/test_a.py::test_x"),
])
def test_scope_forms_route_to_the_child(scope, rebased):
    routed = monorepo.route_scopes((scope,), _children())
    assert routed.target.declaration == "api"
    assert routed.scopes == (rebased,)


@pytest.mark.parametrize(("scope", "needle"), [
    ("/abs/api/tests", "relative to the repository root"),
    ("api/../web/x", "relative to the repository root"),
    ("docs/x.py", 'inside a project: "api/..." or "web/..."'),
    ("docs", 'or a whole project: "api" or "web"'),
])
def test_bad_scopes_say_what_to_type(scope, needle):
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes((scope,), _children())
    assert needle in exc.value.message
    assert "ptest --full" in exc.value.message or "relative" in needle


def _pytest_children(case):
    """Children carrying real configs: bare names expand to test roots."""
    return tuple(monorepo.ChildTarget(
        declaration=name, directory=Path(name),
        config=case.config(runner_kind="pytest",
                           project_id="ab" * 16 if name == "api" else "cd" * 16))
        for name in ("api", "web"))


def test_bare_child_name_runs_child_test_roots(case):
    routed = monorepo.route_scopes(("web",), _pytest_children(case))
    assert routed.target.declaration == "web"
    assert routed.scopes == ("tests",)


def test_bare_child_name_with_trailing_slash_runs_child_test_roots(case):
    routed = monorepo.route_scopes(("web/",), _pytest_children(case))
    assert routed.target.declaration == "web"
    assert routed.scopes == ("tests",)


def test_bare_command_child_name_routes_with_empty_test_roots():
    routed = monorepo.route_scopes(("web",), _children())
    assert routed.target.declaration == "web"
    assert routed.scopes == ()


def test_unknown_child_name_is_rejected_with_child_list():
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes(("docs/tests",), _children())
    assert '"api/..."' in exc.value.message
    assert '"api"' in exc.value.message
    assert "ptest --full" in exc.value.message


def test_full_bare_child_name_selects_that_childs_gate(case):
    target = monorepo.resolve_full_child(("web",), _pytest_children(case))
    assert target.declaration == "web"


def test_full_child_name_with_trailing_slash_selects_gate(case):
    target = monorepo.resolve_full_child(("web/",), _pytest_children(case))
    assert target.declaration == "web"


def test_full_exact_test_root_selects_that_childs_gate(case):
    target = monorepo.resolve_full_child(("api/tests",), _pytest_children(case))
    assert target.declaration == "api"


def test_full_command_child_with_path_is_rejected_plainly():
    with pytest.raises(C.Problem) as exc:
        monorepo.resolve_full_child(("web/tests",), _children())
    assert "--full runs a whole project" in exc.value.message
    assert "ptest api/tests/test_x.py" in exc.value.message


@pytest.mark.parametrize("scopes", [
    ("api", "web"),
    ("api/tests", "web"),
    ("web/", "api"),
])
def test_full_two_children_are_rejected_as_one_project(case, scopes):
    with pytest.raises(C.Problem) as exc:
        monorepo.resolve_full_child(scopes, _pytest_children(case))
    assert "one project at a time" in exc.value.message


def test_full_repeated_same_child_runs_that_gate_once(case):
    target = monorepo.resolve_full_child(
        ("web", "web/"), _pytest_children(case))
    assert target.declaration == "web"


@pytest.mark.parametrize("scope", [
    "api/tests/test_x.py",
    "docs",
    "-k",
])
def test_full_partial_scope_is_rejected_plainly(case, scope):
    with pytest.raises(C.Problem) as exc:
        monorepo.resolve_full_child((scope,), _pytest_children(case))
    assert "--full runs a whole project" in exc.value.message
    assert "ptest api/tests/test_x.py" in exc.value.message


def test_scopes_across_children_are_refused_plainly():
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes(("api/tests", "web/src"), _children())
    assert "one project at a time" in exc.value.message
