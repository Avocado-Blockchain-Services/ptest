"""T1: impact.py — base resolution, changed set, per-project Impact.

Real git repos via support.init_git_repo / support.git (tmp_path); the
config builder makes a minimal pytest/vitest/command C.Config. Every test
names the design section it pins (design.md 4.1/4.2).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import impact as I
from support import git, init_git_repo, write_file

PID = "ab" * 16


def _config(kind=C.RunnerKind.PYTEST, test_roots=("tests",), **selection_over):
    sel = {"enabled": True, "closed_inputs": False}
    sel.update(selection_over)
    launcher = ("true",) if kind is C.RunnerKind.COMMAND else ("uv",)
    roots = () if kind is C.RunnerKind.COMMAND else test_roots
    return C.Config(
        runner=C.RunnerConfig(kind=kind, launcher=launcher, test_roots=roots),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(**sel),
        project_id=PID,
    )


def _graph_files(n_extra_tests=3):
    """Flat pkg + tests layout; total test files = 1 + n_extra_tests (>= 4)."""
    files = {
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": "from pkg.b import VALUE\n",
        "tests/test_a.py": "from pkg.a import VALUE\n\ndef test_a():\n    assert VALUE == 1\n",
    }
    for i in range(n_extra_tests):
        files[f"tests/test_x{i}.py"] = "def test_nothing():\n    assert True\n"
    return files


# --- resolve_base (D2) ---

def test_resolve_base_feature_branch_returns_merge_base_with_main(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n"}, branch="main")
    main_head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "feat")
    write_file(tmp_path / "a.py", "2\n")
    git(tmp_path, "commit", "-qam", "feat change")

    base = I.resolve_base(tmp_path, None)

    assert base == I.Base(sha=main_head, label="main")


def test_resolve_base_prefers_origin_head_target(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n"}, branch="main")
    git(tmp_path, "checkout", "-qb", "feat")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(tmp_path, "symbolic-ref", "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main")

    base = I.resolve_base(tmp_path, None)

    assert base.label == "origin/main"
    assert base.sha == git(tmp_path, "merge-base", "HEAD",
                           "refs/remotes/origin/main")


def test_resolve_base_on_default_branch_compares_head_only(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n"}, branch="main")

    assert I.resolve_base(tmp_path, None) == I.Base(sha=None, label="HEAD")


def test_resolve_base_explicit_ref_wins_and_uses_merge_base(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n"}, branch="main")
    git(tmp_path, "checkout", "-qb", "feat")
    fork_point = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    write_file(tmp_path / "other.py", "x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "advance main")
    git(tmp_path, "checkout", "-q", "feat")

    base = I.resolve_base(tmp_path, "main")

    # An advanced main must not show its own commits as changed.
    assert base == I.Base(sha=fork_point, label="main")


def test_resolve_base_bad_ref_is_invalid_config(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n"}, branch="main")

    with pytest.raises(C.Problem) as exc:
        I.resolve_base(tmp_path, "no-such-ref")

    assert exc.value.code == "invalid-config"
    assert exc.value.message == "--base is not a commit in this repository"


def test_resolve_base_explicit_without_repo_is_invalid_config(tmp_path):
    with pytest.raises(C.Problem) as exc:
        I.resolve_base(None, "main")

    assert exc.value.code == "invalid-config"
    assert exc.value.message == "--base needs a git repository"


def test_resolve_base_without_repo_defaults_to_head():
    assert I.resolve_base(None, None) == I.Base(sha=None, label="HEAD")


# --- changed_files (D1) ---

def test_changed_files_sees_committed_staged_unstaged_untracked(tmp_path):
    init_git_repo(tmp_path, files={"a.py": "1\n", "b.py": "1\n"}, branch="main")
    base = git(tmp_path, "rev-parse", "HEAD")
    write_file(tmp_path / "a.py", "2\n")  # committed since base
    git(tmp_path, "commit", "-qam", "touch a")
    write_file(tmp_path / "b.py", "2\n")  # unstaged
    git(tmp_path, "add", "b.py")  # staged
    write_file(tmp_path / "c.py", "new\n")  # untracked (staged? no: add -A not run)
    git(tmp_path, "add", "c.py")  # staged new
    write_file(tmp_path / "d.py", "untracked\n")  # untracked
    (tmp_path / "b.py").write_text("3\n", encoding="utf-8")  # staged + unstaged

    changed = I.changed_files(tmp_path, I.Base(sha=base, label="main"))

    assert set(changed) == {"a.py", "b.py", "c.py", "d.py"}


def test_changed_files_none_without_git():
    assert I.changed_files(None, I.Base(sha=None, label="HEAD")) is None


# --- plan: basics ---

def _repo(tmp_path, files, branch="main"):
    init_git_repo(tmp_path, files=files, branch=branch)
    return tmp_path


def _plan_at(root, rel_paths, config=None):
    """Plan with repo_changed given explicitly (unit scope)."""
    return I.plan(root, root, config or _config(), tuple(rel_paths))


def test_plan_docs_only_is_none_with_changed(tmp_path):
    _repo(tmp_path, {"README.md": "hi\n", "tests/test_a.py": "def test_x():\n assert True\n"})

    impact = _plan_at(tmp_path, ["README.md"])

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == 1


def test_plan_hidden_only_is_none(tmp_path):
    _repo(tmp_path, {"tests/test_a.py": "def test_x():\n assert True\n"})
    write_file(tmp_path / ".cache" / "x.py", "1\n")

    impact = I.plan(tmp_path, tmp_path, _config(), (".cache/x.py",))

    assert impact.kind == "none"


def test_plan_direct_test_file_is_selected(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = _plan_at(tmp_path, ["tests/test_a.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)
    assert impact.direct == 1
    assert impact.via == 0
    assert impact.total == 4
    assert impact.changed == ("tests/test_a.py",)


def test_plan_transitive_importer_is_via(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)
    assert impact.direct == 0
    assert impact.via == 1
    assert impact.total == 4


def test_plan_relative_import_resolves(tmp_path):
    _repo(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "pkg/a.py": "from .b import VALUE\n",
        "tests/test_a.py": "from pkg.a import VALUE\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    })

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)
    assert impact.via == 1


def test_plan_src_layout_resolves(tmp_path):
    _repo(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/b.py": "VALUE = 1\n",
        "src/pkg/a.py": "from pkg.b import VALUE\n",
        "tests/test_a.py": "from pkg.a import VALUE\n\ndef test_a():\n    assert VALUE\n",
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    })

    impact = _plan_at(tmp_path, ["src/pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)


def test_plan_package_init_selects_package_importers(tmp_path):
    _repo(tmp_path, {
        "pkg/__init__.py": "VALUE = 1\n",
        "tests/test_a.py": "import pkg\n\ndef test_a():\n    assert pkg.VALUE\n",
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    })

    impact = _plan_at(tmp_path, ["pkg/__init__.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)


def test_plan_deleted_source_still_selects_importers(tmp_path):
    _repo(tmp_path, _graph_files())
    (tmp_path / "pkg" / "b.py").unlink()

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)


def test_plan_function_level_import_counts(tmp_path):
    _repo(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "tests/test_a.py": "def test_a():\n    from pkg.b import VALUE\n    assert VALUE\n",
        "tests/test_b.py": "def test_b():\n    assert True\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
    })

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)


def test_plan_via_propagates_through_direct_test_files(tmp_path):
    _repo(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/b.py": "VALUE = 1\n",
        "tests/test_t.py": "from pkg.b import VALUE\nX = VALUE\n",
        "tests/test_u.py": "from tests.test_t import X\n\ndef test_u():\n    assert X\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
        "tests/test_e.py": "def test_e():\n    assert True\n",
        "tests/test_f.py": "def test_f():\n    assert True\n",
    })

    impact = _plan_at(tmp_path, ["pkg/b.py", "tests/test_t.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_t.py", "tests/test_u.py")
    assert (impact.direct, impact.via) == (1, 1)


def test_plan_unrelated_source_is_none(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"pkg/z.py": "Z = 1\n"}))

    impact = _plan_at(tmp_path, ["pkg/z.py"])

    assert impact.kind == "none"
    assert impact.changed == ("pkg/z.py",)


# --- plan: full triggers ---

@pytest.mark.parametrize("trigger", [
    "uv.lock", "pkg/requirements-dev.txt", "pyproject.toml", "setup.cfg",
    "pytest.ini", "tox.ini", "conftest.py", "pkg/conftest.py",
    "vitest.config.ts", "package.json", ".ptest.toml",
])
def test_plan_trigger_files_run_full(tmp_path, trigger):
    _repo(tmp_path, _graph_files())
    write_file(tmp_path / trigger, "x\n")

    impact = _plan_at(tmp_path, [trigger])

    assert impact.kind == "full"
    assert impact.reason == f"{trigger} is a full trigger"


def test_plan_configured_full_trigger_runs_full(tmp_path):
    _repo(tmp_path, _graph_files())
    write_file(tmp_path / "infra" / "schema.sql", "x\n")
    config = _config(full_triggers=("infra",))

    impact = _plan_at(tmp_path, ["infra/schema.sql"], config)

    assert impact.kind == "full"
    assert impact.reason == "infra/schema.sql is a full trigger"


def test_plan_ancestor_root_trigger_runs_child_full(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"uv.lock": "x\n"}))
    child = tmp_path / "api"
    # Move the graph under api/ so the child prefix owns it.
    import shutil
    child.mkdir(exist_ok=True)
    for name in ("pkg", "tests"):
        shutil.move(str(tmp_path / name), str(child / name))

    impact = I.plan(tmp_path, child, _config(), ("uv.lock",))

    assert impact.kind == "full"
    assert impact.reason == "uv.lock is a full trigger"


def test_plan_sibling_child_change_is_none(tmp_path):
    _repo(tmp_path, {"api/a.py": "1\n", "web/w.js": "1\n"})

    impact = I.plan(tmp_path, tmp_path / "api", _config(), ("web/w.js",))

    assert impact.kind == "none"


def test_plan_test_support_is_full(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"tests/helpers.py": "X = 1\n"}))

    impact = _plan_at(tmp_path, ["tests/helpers.py"])

    assert impact.kind == "full"
    assert impact.reason == "tests/helpers.py is test support"


def test_plan_unparsable_changed_py_is_full(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"pkg/b.py": "def broken(:\n"}))

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "full"
    assert impact.reason == "pkg/b.py could not be parsed"


def test_plan_non_py_source_is_outside_graph(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"pkg/data.json": "{}\n"}))

    impact = _plan_at(tmp_path, ["pkg/data.json"])

    assert impact.kind == "full"
    assert impact.reason == "pkg/data.json is outside the import graph"


def test_plan_unparsable_unchanged_file_is_skipped(tmp_path):
    files = _graph_files()
    files["pkg/stale.py"] = "def broken(:\n"
    _repo(tmp_path, files)

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "selected"
    assert impact.files == ("tests/test_a.py",)


def test_plan_selection_disabled_is_full(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = _plan_at(tmp_path, ["pkg/b.py"], _config(enabled=False))

    assert impact.kind == "full"
    assert impact.reason == ("selection is off in .ptest.toml — "
                             "ptest doctor --fix turns it on")


def test_plan_vitest_delegates(tmp_path):
    init_git_repo(tmp_path, files={"src/a.ts": "1\n"}, branch="main")
    config = _config(kind=C.RunnerKind.VITEST, test_roots=("src",))

    impact = I.plan(tmp_path, tmp_path, config, ("src/a.ts",))

    assert impact.kind == "vitest"
    assert impact.changed == ("src/a.ts",)


def test_plan_command_runner_has_no_graph(tmp_path):
    init_git_repo(tmp_path, files={"run.sh": "true\n"}, branch="main")

    impact = I.plan(tmp_path, tmp_path, _config(kind=C.RunnerKind.COMMAND),
                    ("run.sh",))

    assert impact.kind == "full"
    assert impact.reason == "command has no import graph"


def test_plan_no_git_evidence_is_full(tmp_path):
    impact = I.plan(None, tmp_path, _config(), ("pkg/b.py"))

    assert impact.kind == "full"
    assert impact.reason == "git changes are unavailable"


def test_plan_repo_changed_none_is_full(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = I.plan(tmp_path, tmp_path, _config(), None)

    assert impact.kind == "full"
    assert impact.reason == "git changes are unavailable"


def test_plan_project_outside_top_is_full(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = I.plan(tmp_path, tmp_path.parent, _config(), ("pkg/b.py",))

    assert impact.kind == "full"
    assert impact.reason == "git changes are unavailable"


def test_plan_empty_changed_is_none(tmp_path):
    _repo(tmp_path, _graph_files())

    impact = _plan_at(tmp_path, [])

    assert impact.kind == "none"
    assert impact.changed == ()


def test_plan_full_ratio_runs_full(tmp_path):
    files = {"pkg/shared.py": "X = 1\n"}
    for i in range(8):
        files[f"tests/test_{i}.py"] = (
            "from pkg.shared import X\n\ndef test_it():\n    assert X\n")
    _repo(tmp_path, files)

    impact = _plan_at(tmp_path, ["pkg/shared.py"])

    assert impact.kind == "full"
    assert impact.reason == "8 of 8 test files reaches full_ratio 0.7"


def test_plan_max_selected_runs_full(tmp_path, monkeypatch):
    _repo(tmp_path, {
        "pkg/b.py": "VALUE = 1\n",
        "pkg/c.py": "OTHER = 2\n",
        "tests/test_a.py": "from pkg.b import VALUE\ndef test_a():\n assert VALUE\n",
        "tests/test_b.py": "from pkg.c import OTHER\ndef test_b():\n assert OTHER\n",
        "tests/test_c.py": "def test_c():\n assert True\n",
        "tests/test_d.py": "def test_d():\n assert True\n",
    })
    monkeypatch.setattr(I, "MAX_SELECTED", 1)

    impact = _plan_at(tmp_path, ["pkg/b.py", "pkg/c.py"])

    assert impact.kind == "full"
    assert impact.reason == "2 test files exceed the 200-file scoped limit"


def test_plan_graph_bound_runs_full(tmp_path, monkeypatch):
    _repo(tmp_path, _graph_files())
    monkeypatch.setattr(I, "MAX_SCAN_FILES", 1)

    impact = _plan_at(tmp_path, ["pkg/b.py"])

    assert impact.kind == "full"
    assert impact.reason == "import graph too large"


def test_plan_changed_orders_relevant_before_ignored(tmp_path):
    _repo(tmp_path, _graph_files())
    write_file(tmp_path / "README.md", "hi\n")
    write_file(tmp_path / "docs" / "note.rst", "x\n")

    impact = _plan_at(tmp_path, ["README.md", "pkg/b.py", "docs/note.rst"])

    assert impact.kind == "selected"
    assert impact.changed == ("pkg/b.py",)
    assert impact.ignored == 2


# --- 0.3.3: build output / non-code outside packages are not inputs ---

def test_plan_build_lib_copy_is_dropped(tmp_path):
    """Report twin: untracked build/lib copy, no code change -> no changes."""
    _repo(tmp_path, {"tests/test_a.py": "def test_x():\n assert True\n"})
    write_file(tmp_path / "build" / "lib" / "ptest" / "runtime"
               / "protocol-v1.json", "{}\n")

    impact = _plan_at(tmp_path, ["build/lib/ptest/runtime/protocol-v1.json"])

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == 1


def test_plan_report_scenario_is_no_changes(tmp_path):
    """Exact report shape: build output + skills + AGENTS.md -> no changes."""
    _repo(tmp_path, {"tests/test_a.py": "def test_x():\n assert True\n"})
    changed = [
        "build/lib/ptest/runtime/protocol-v1.json",
        "build/lib/ptest/worker.py",
        "AGENTS.md",
        "docs/ptest-agent.md",
        ".agents/skills/a/SKILL.md",
        ".claude/skills/b/SKILL.md",
        ".gemini/c.md",
        ".opencode/d.md",
    ]
    for rel in changed:
        write_file(tmp_path / rel, "x\n")

    impact = _plan_at(tmp_path, changed)

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == len(changed)


@pytest.mark.parametrize("path", [
    "build/lib/pkg/b.py",
    "dist/pkg/b.py",
    "pkg.egg-info/SOURCES.txt",
    "node_modules/dep/index.js",
    ".venv/lib/x.py",
    "venv/lib/x.py",
    "pkg/__pycache__/b.pyc",
    ".pytest_cache/CACHEDIR.TAG",
    ".mypy_cache/x.data",
    ".ruff_cache/x",
    ".tox/py311/x.py",
    "htmlcov/index.html",
    ".coverage",
    ".coverage.localhost.1234",
    "api/build/lib/x.py",
])
def test_plan_tool_output_segments_are_dropped(tmp_path, path):
    _repo(tmp_path, {"tests/test_a.py": "def test_x():\n assert True\n"})

    impact = _plan_at(tmp_path, [path])

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == 1


def test_plan_docs_change_is_no_changes(tmp_path):
    _repo(tmp_path, _graph_files())
    write_file(tmp_path / "docs" / "guide.md", "x\n")

    impact = _plan_at(tmp_path, ["docs/guide.md"])

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == 1


def test_plan_json_under_tests_is_full(tmp_path):
    _repo(tmp_path, dict(_graph_files(), **{"tests/data.json": "{}\n"}))

    impact = _plan_at(tmp_path, ["tests/data.json"])

    assert impact.kind == "full"
    assert impact.reason == "tests/data.json is outside the import graph"


def test_plan_md_inside_package_keeps_old_note(tmp_path):
    """Inside non-code keeps today's ignore-note (fail safe, unchanged)."""
    _repo(tmp_path, dict(_graph_files(), **{"pkg/notes.md": "x\n"}))

    impact = _plan_at(tmp_path, ["pkg/notes.md"])

    assert impact.kind == "none"
    assert impact.changed == ("pkg/notes.md",)
    assert impact.ignored == 0


def test_plan_build_under_monorepo_child_is_dropped(tmp_path):
    """Twin: build/ under a monorepo child is ignored for that child."""
    _repo(tmp_path, {"api/pkg/b.py": "X = 1\n",
                     "api/tests/test_b.py": "def test_b():\n assert True\n"})
    write_file(tmp_path / "api" / "build" / "lib" / "x.json", "{}\n")

    impact = I.plan(tmp_path, tmp_path / "api", _config(),
                    ("api/build/lib/x.json",))

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.ignored == 1


def test_plan_py_under_build_is_not_a_seed(tmp_path):
    """A build copy of imported source must not select its importers."""
    _repo(tmp_path, _graph_files())
    write_file(tmp_path / "build" / "lib" / "pkg" / "b.py", "VALUE = 1\n")

    impact = _plan_at(tmp_path, ["build/lib/pkg/b.py"])

    assert impact.kind == "none"
    assert impact.changed == ()
    assert impact.files == ()


def test_plan_no_tests_prefix_is_ignored(tmp_path):
    files = dict(_graph_files())
    files["docs/gen.py"] = "X = 1\n"
    _repo(tmp_path, files)
    config = _config(no_tests=("docs",))

    impact = _plan_at(tmp_path, ["docs/gen.py"], config)

    assert impact.kind == "none"


def _rename_fixture_files():
    return {
        "app/__init__.py": "",
        "app/b.py": "X = 1\n",
        "tests/test_b.py": "from app.b import X\n\ndef test_b():\n    assert X == 1\n",
        "tests/test_c.py": "def test_c():\n    assert True\n",
        "tests/test_d.py": "def test_d():\n    assert True\n",
        "tests/test_e.py": "def test_e():\n    assert True\n",
    }


def test_changed_files_committed_rename_keeps_old_path(tmp_path):
    _repo(tmp_path, _rename_fixture_files())
    git(tmp_path, "checkout", "-qb", "feat")
    git(tmp_path, "mv", "app/b.py", "app/c.py")
    git(tmp_path, "commit", "-qm", "rename b to c")

    base = I.resolve_base(tmp_path, None)
    assert base.label == "main"

    changed = I.changed_files(tmp_path, base)
    assert "app/b.py" in changed
    assert "app/c.py" in changed

    impact = I.plan(tmp_path, tmp_path, _config(), changed)
    assert impact.kind == "selected"
    assert impact.files == ("tests/test_b.py",)


def test_changed_files_staged_rename_keeps_old_path(tmp_path):
    _repo(tmp_path, _rename_fixture_files())
    git(tmp_path, "mv", "app/b.py", "app/c.py")

    base = I.resolve_base(tmp_path, None)
    assert base == I.Base(sha=None, label="HEAD")

    changed = I.changed_files(tmp_path, base)
    assert "app/b.py" in changed
    assert "app/c.py" in changed

    impact = I.plan(tmp_path, tmp_path, _config(), changed)
    assert impact.kind == "selected"
    assert impact.files == ("tests/test_b.py",)
