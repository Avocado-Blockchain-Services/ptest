"""Section A twins: monorepo root `ptest --changed` via impact routing.

Covers spec section A (cli.py monorepo branch, monorepo.py):
A.1 root --changed runs impacted children with the mapped request
    (SCOPED files / FULL / vitest `--changed <sha>`), skips clean ones
    with `ptest: <child> · no changes`, --base reaches resolve_base;
A.2 vitest child delegates to vitest's own `--changed <base>`,
    falling back to HEAD when the base has no sha;
A.3 one end line per child plus the total line, exit = first nonzero;
A.4 a root lockfile full trigger runs only the triggered child.
A clean tree with nothing changed anywhere prints the nothing-changed
line with no per-child lines and no total.

The graph itself (``ptest.impact``) is stubbed here via ``sys.modules``;
these tests pin the CLI mapping, not the selection.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from ptest import contracts as C
from ptest import monorepo as monorepo_api
from ptest.cli import main
from support import git, init_git_repo


def _child_specs(web_kind: str = "command") -> dict:
    """Child specs for the api/web twin dispatcher (api always command)."""
    if web_kind == "vitest":
        web: dict = {"kind": "vitest", "launcher": ("node",),
                     "test_roots": ("src",)}
    else:
        web = {"kind": "command", "launcher": ("true",)}
    return {"api": {"kind": "command", "launcher": ("true",)}, "web": web}


def _result(exit_code: int = 0):
    return type("R", (), {"reasons": (), "exit_code": exit_code,
                          "status": C.Status.PASSED, "counts": None})()


def _install_impact(monkeypatch, plans, *, sha="abc123", label="origin/dev",
                    explicit_seen=None, top=None, repo_changed=()):
    """Stub ``ptest.impact``; plans maps child declaration -> Impact."""
    from dataclasses import dataclass

    mod = types.ModuleType("ptest.impact")

    @dataclass(frozen=True, slots=True)
    class Base:
        sha: str | None
        label: str

    @dataclass(frozen=True, slots=True)
    class Impact:
        kind: str
        changed: tuple = ()
        files: tuple = ()
        direct: int = 0
        via: int = 0
        total: int = 0
        reason: str = ""

    base = Base(sha, label)
    mod.Base = Base
    mod.Impact = Impact
    mod.git_top = lambda start: top
    mod.resolve_base = lambda top_arg, explicit: (
        explicit_seen.__setitem__("explicit", explicit) or base
        if explicit_seen is not None else base)
    mod.changed_files = lambda top_arg, resolved: repo_changed
    mod.plan = lambda top_arg, project_root, config, changed: (
        plans(mod)[Path(project_root).name])
    monkeypatch.setitem(sys.modules, "ptest.impact", mod)
    return mod


def _selected(mod, changed=("api/a.py",),
              files=("tests/test_a.py",), direct=1, via=0, total=4):
    return mod.Impact(kind="selected", changed=tuple(changed),
                      files=tuple(files), direct=direct, via=via, total=total)


def _none(mod, changed=()):
    return mod.Impact(kind="none", changed=tuple(changed))


def _capture(monkeypatch, result=None):
    calls = []
    outcome = result if result is not None else _result()

    def fake_execute(domain, config, request):
        calls.append((config, request))
        return outcome

    monkeypatch.setattr("ptest.operations.execute", fake_execute)
    return calls


def _root(tmp_path, monkeypatch, monorepo, web_kind="command"):
    monorepo(_child_specs(web_kind), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)


# --- worktree_changed_files (kept helper) ------------------------------------

def test_changed_files_sees_uncommitted_and_untracked(tmp_path):
    init_git_repo(tmp_path, files={"api/a.py": "1\n"}, message="base")
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    (tmp_path / "api" / "new.py").write_text("new\n", encoding="utf-8")

    changed = monorepo_api.worktree_changed_files(tmp_path, None)

    assert "api/a.py" in changed
    assert "api/new.py" in changed


def test_changed_files_limits_committed_range_to_base(tmp_path):
    init_git_repo(tmp_path, files={"api/a.py": "1\n", "web/w.js": "1\n"}, message="base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    git(tmp_path, "commit", "-am", "touch api")

    assert monorepo_api.worktree_changed_files(tmp_path, base) == ("api/a.py",)


# --- A.1: routing ------------------------------------------------------------

def test_root_changed_runs_touched_child_and_skips_clean_one(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _install_impact(monkeypatch, lambda mod: {
        "api": _selected(mod), "web": _none(mod)}, top=tmp_path,
        repo_changed=("api/extra.py",))
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0
    assert len(calls) == 1
    assert calls[0][1].mode is C.Mode.SCOPED
    assert calls[0][1].argv == ("tests/test_a.py",)
    assert calls[0][1].base is None
    assert calls[0][1].next_hint is False
    assert calls[0][0].config_path.parent.name == "api"
    err = capsys.readouterr().err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err


def test_root_changed_clean_tree_prints_nothing_changed(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    _install_impact(monkeypatch, lambda mod: {
        "api": _none(mod), "web": _none(mod)}, top=tmp_path, repo_changed=())
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev — nothing to test · "
            "ptest --full runs everything") in err
    assert "ptest: total" not in err


def test_root_changed_outside_git_runs_full_gate_per_child(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    _install_impact(monkeypatch, lambda mod: {
        "api": mod.Impact(kind="full", reason="git changes are unavailable"),
        "web": mod.Impact(kind="full", reason="git changes are unavailable")},
        top=None, repo_changed=None)
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0
    assert [call[1].mode for call in calls] == [C.Mode.FULL] * 2
    assert all(call[1].changed_note
               == "changed → full suite: git changes are unavailable"
               for call in calls)


def test_root_changed_base_ref_reaches_resolve(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "touch api")
    seen: dict = {}
    _install_impact(monkeypatch, lambda mod: {
        "api": _selected(mod), "web": _none(mod)}, top=tmp_path,
        repo_changed=("api/extra.py",), explicit_seen=seen)
    calls = _capture(monkeypatch)

    assert main(("--changed", "--base", base)) == 0
    assert seen["explicit"] == base
    assert len(calls) == 1
    assert calls[0][1].base is None
    assert "ptest: web · no changes" in capsys.readouterr().err


def test_root_changed_returns_first_failure_after_all_children_finish(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    (tmp_path / "api" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "web" / "w.js").write_text("y = 1\n", encoding="utf-8")
    _install_impact(monkeypatch, lambda mod: {
        "api": _selected(mod, changed=("api/a.py",)),
        "web": _selected(mod, changed=("web/w.js",))}, top=tmp_path,
        repo_changed=("api/a.py", "web/w.js"))
    codes = iter((9, 3))
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: _result(next(codes)),
    )

    assert main(("--changed",)) == 9
    assert "ptest: total" in capsys.readouterr().err


# --- A.4: root files that are full triggers -----------------------------------

def test_root_lockfile_full_trigger_runs_only_the_triggered_child(
        tmp_path, monkeypatch, monorepo):
    _root(tmp_path, monkeypatch, monorepo)
    (tmp_path / "uv.lock").write_text("v2\n", encoding="utf-8")
    _install_impact(monkeypatch, lambda mod: {
        "api": mod.Impact(kind="full", changed=("uv.lock",),
                          reason="uv.lock is a full trigger"),
        "web": _none(mod)}, top=tmp_path, repo_changed=("uv.lock",))
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0
    assert len(calls) == 1
    assert calls[0][1].mode is C.Mode.FULL
    assert (calls[0][1].changed_note
            == "changed → full suite: uv.lock is a full trigger")


# --- A.2: vitest delegation ---------------------------------------------------

def test_root_changed_vitest_child_gets_changed_argv(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo, web_kind="vitest")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "web" / "src" / "app.test.ts").parent.mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "app.test.ts").write_text(
        "test\n", encoding="utf-8")
    _install_impact(monkeypatch, lambda mod: {
        "api": _none(mod),
        "web": mod.Impact(kind="vitest", changed=("web/src/app.ts",))},
        top=tmp_path, repo_changed=("web/src/app.ts",))
    calls = _capture(monkeypatch)

    assert main(("--changed", "--base", base)) == 0
    assert len(calls) == 1
    assert calls[0][1].argv == ("--changed", "abc123")
    assert calls[0][1].base is None
    assert "ptest: api · no changes" in capsys.readouterr().err


def test_root_changed_vitest_child_without_sha_uses_head(
        tmp_path, monkeypatch, capsys, monorepo):
    _root(tmp_path, monkeypatch, monorepo, web_kind="vitest")
    (tmp_path / "web" / "src" / "app.test.ts").parent.mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "app.test.ts").write_text(
        "test\n", encoding="utf-8")
    _install_impact(monkeypatch, lambda mod: {
        "api": _none(mod),
        "web": mod.Impact(kind="vitest", changed=("web/src/app.ts",))},
        top=tmp_path, sha=None, label="HEAD",
        repo_changed=("web/src/app.ts",))
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0
    assert len(calls) == 1
    assert calls[0][1].mode is C.Mode.SCOPED
    assert calls[0][1].argv == ("--changed", "HEAD")


# --- skip-line styling (kept helper) ------------------------------------------

def test_no_changes_line_styles_prefix_and_project_on_tty():
    from ptest import progress

    line = progress.format_no_changes("web", color=True)
    assert "\x1b[2mptest:\x1b[0m" in line
    assert "\x1b[1mweb\x1b[0m" in line
    assert line.endswith("· no changes")


def test_no_changes_line_plain_without_tty(monkeypatch):
    from ptest import progress

    assert progress.format_no_changes("web") == "ptest: web · no changes"
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in progress.format_no_changes("web", color=True)
