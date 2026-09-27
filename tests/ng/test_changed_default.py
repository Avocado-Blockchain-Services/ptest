"""T1 decision layer for bare ptest / ptest --changed routing.

End-to-end through the frozen interfaces the CLI wiring (design.md 4.6)
consumes: resolve_base -> changed_files -> plan, per child in a monorepo.
No cli.py import: the request mapping and output lines belong to the wiring
task; this file pins the decisions that mapping reads.
"""
from __future__ import annotations

from ptest import contracts as C
from ptest import impact as I
from support import git, init_git_repo, write_file

PID = "cd" * 16


def _config(kind=C.RunnerKind.PYTEST, test_roots=("tests",), **selection_over):
    sel = {"enabled": True, "closed_inputs": False}
    sel.update(selection_over)
    return C.Config(
        runner=C.RunnerConfig(kind=kind, launcher=("uv",),
                              test_roots=test_roots),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(**sel),
        project_id=PID,
    )


def _api_repo(root):
    init_git_repo(root, files={
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": "from pkg.b import VALUE\n",
        "tests/test_a.py": "from pkg.a import VALUE\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    }, branch="main")
    return root


def _route(top, project_root, config):
    """The exact three calls the CLI wiring makes per project (4.6)."""
    base = I.resolve_base(I.git_top(project_root), None)
    repo_changed = I.changed_files(I.git_top(project_root), base)
    return base, I.plan(top, project_root, config, repo_changed)


def test_bare_ptest_on_feature_branch_selects_reached_tests(tmp_path):
    _api_repo(tmp_path)
    git(tmp_path, "checkout", "-qb", "feat")
    write_file(tmp_path / "pkg" / "b.py", "VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "bump b")

    base, impact = _route(tmp_path, tmp_path, _config())

    assert base.label == "main"
    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)
    assert (impact.direct, impact.via, impact.total) == (0, 1, 4)


def test_bare_ptest_on_default_branch_sees_uncommitted_only(tmp_path):
    _api_repo(tmp_path)
    committed = git(tmp_path, "rev-parse", "HEAD")
    write_file(tmp_path / "pkg" / "b.py", "VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "committed on main")

    base, impact = _route(tmp_path, tmp_path, _config())

    # On the default branch only uncommitted work counts: the committed
    # change is invisible, so nothing is selected.
    assert base == I.Base(sha=None, label="HEAD")
    assert impact.kind == "none"
    assert committed


def test_bare_ptest_clean_tree_is_nothing_changed(tmp_path):
    _api_repo(tmp_path)

    base, impact = _route(tmp_path, tmp_path, _config())

    assert base == I.Base(sha=None, label="HEAD")
    assert impact.kind == "none"
    assert impact.changed == ()


def test_monorepo_children_plan_against_same_repo_changed(tmp_path):
    init_git_repo(tmp_path, files={
        "api/pkg/__init__.py": "",
        "api/pkg/b.py": "VALUE = 1\n",
        "api/tests/test_a.py": "from pkg.b import VALUE\ndef test_a():\n assert VALUE\n",
        "api/tests/test_b.py": "def test_b():\n assert True\n",
        "api/tests/test_c.py": "def test_c():\n assert True\n",
        "api/tests/test_d.py": "def test_d():\n assert True\n",
        "web/src/a.ts": "1\n",
    }, branch="main")
    git(tmp_path, "checkout", "-qb", "feat")
    write_file(tmp_path / "api" / "pkg" / "b.py", "VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "touch api")

    top = I.git_top(tmp_path)
    base = I.resolve_base(top, None)
    repo_changed = I.changed_files(top, base)
    assert repo_changed == ("api/pkg/b.py",)
    api = I.plan(top, tmp_path / "api", _config(), repo_changed)
    web_config = C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.VITEST, launcher=("node",),
                              test_roots=("src",)),
        setup=None, resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id=PID)
    web = I.plan(top, tmp_path / "web", web_config, repo_changed)

    assert api.kind == "selected"
    assert api.files == ("tests/test_a.py",)
    assert web.kind == "none"


def test_explicit_base_routes_against_merge_base(tmp_path):
    _api_repo(tmp_path)
    git(tmp_path, "checkout", "-qb", "feat")
    fork = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    write_file(tmp_path / "main-only.py", "x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "advance main")
    git(tmp_path, "checkout", "-q", "feat")
    write_file(tmp_path / "pkg" / "b.py", "VALUE = 2\n")
    git(tmp_path, "commit", "-qam", "bump b")

    top = I.git_top(tmp_path)
    base = I.resolve_base(top, "main")
    repo_changed = I.changed_files(top, base)

    assert base == I.Base(sha=fork, label="main")
    # main's own commit is not attributed to the feature branch.
    assert repo_changed == ("pkg/b.py",)
