from __future__ import annotations

from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import monorepo


def _children():
    return tuple(monorepo.ChildTarget(declaration=name, directory=Path(name), config=None)
                 for name in ("api", "web"))


def _pytest_children(case):
    """Children carrying real configs: bare names expand to test roots."""
    return tuple(monorepo.ChildTarget(
        declaration=name, directory=Path(name),
        config=case.config(runner_kind="pytest",
                           project_id="ab" * 16 if name == "api" else "cd" * 16))
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


def test_bare_child_name_splits_as_whole_child_folder(tmp_path):
    children = tuple(monorepo.ChildTarget(declaration=name, directory=tmp_path / name, config=None)
                     for name in ("api", "web"))
    split = monorepo.split_scopes(("web", "web/"), children)
    assert split.target.declaration == "web"
    assert split.files == ()
    assert [(item.typed, item.local) for item in split.folders] == [
        ("web", ""), ("web", "")]


def test_existing_directory_splits_as_folder(tmp_path):
    (tmp_path / "api" / "tests" / "unit").mkdir(parents=True)
    children = tuple(monorepo.ChildTarget(declaration=name, directory=tmp_path / name, config=None)
                     for name in ("api", "web"))
    split = monorepo.split_scopes(("api/tests/unit",), children)
    assert split.target.declaration == "api"
    assert split.files == ()
    assert [(item.typed, item.local) for item in split.folders] == [
        ("api/tests/unit", "tests/unit")]


def test_named_file_and_node_id_split_as_files(tmp_path):
    children = tuple(monorepo.ChildTarget(declaration=name, directory=tmp_path / name, config=None)
                     for name in ("api", "web"))
    split = monorepo.split_scopes(
        ("api/tests/test_x.py", "api/tests/test_x.py::test_q"), children)
    assert split.target.declaration == "api"
    assert split.folders == ()
    assert [(item.typed, item.local) for item in split.files] == [
        ("api/tests/test_x.py", "tests/test_x.py"),
        ("api/tests/test_x.py::test_q", "tests/test_x.py::test_q")]


def test_missing_path_splits_as_explicit_file(tmp_path):
    children = tuple(monorepo.ChildTarget(declaration=name, directory=tmp_path / name, config=None)
                     for name in ("api", "web"))
    split = monorepo.split_scopes(("api/tests/nope.py",), children)
    assert [(item.typed, item.local) for item in split.files] == [
        ("api/tests/nope.py", "tests/nope.py")]


def test_split_uses_first_declared_child_in_examples():
    children = tuple(monorepo.ChildTarget(declaration=name, directory=Path(name), config=None)
                     for name in ("web", "srv"))
    with pytest.raises(C.Problem) as exc:
        monorepo.split_scopes(("/abs/web/tests",), children)
    assert "web/tests" in exc.value.message
    assert "api/" not in exc.value.message
    with pytest.raises(C.Problem) as exc:
        monorepo.split_scopes(("oops/tests",), children)
    assert '"web/..."' in exc.value.message
    assert "api/" not in exc.value.message


def test_full_file_message_echoes_the_named_scope():
    message = monorepo.full_scope_message(
        _children(), typed="api/tests/test_x.py")
    assert "--full" in message
    assert "ptest api/tests/test_x.py" in message


def test_full_folder_message_names_the_first_declared_child():
    children = tuple(monorepo.ChildTarget(declaration=name, directory=Path(name), config=None)
                     for name in ("web", "srv"))
    message = monorepo.full_scope_message(children)
    assert "--full" in message
    assert "web/tests" in message
    assert "api/" not in message


def test_scopes_across_children_are_refused_plainly():
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes(("api/tests", "web/src"), _children())
    assert "one project at a time" in exc.value.message
