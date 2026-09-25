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
    ("api", 'inside a project: "api/..." or "web/..."'),
    ("api/", 'inside a project: "api/..." or "web/..."'),
])
def test_bad_scopes_say_what_to_type(scope, needle):
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes((scope,), _children())
    assert needle in exc.value.message
    assert "ptest --full" in exc.value.message or "relative" in needle


def test_scopes_across_children_are_refused_plainly():
    with pytest.raises(C.Problem) as exc:
        monorepo.route_scopes(("api/tests", "web/src"), _children())
    assert "one project at a time" in exc.value.message
