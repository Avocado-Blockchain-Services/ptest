"""Section A twins: monorepo root `ptest --changed`.

Covers spec section A (cli.py monorepo branch, monorepo.py):
A.1 root --changed runs touched children in CHANGED mode, skips clean ones
    with `ptest: <child> · no changes`, --base passes through;
A.2 vitest child with changes uses vitest's own `--changed <base>` when a
    base names the comparison, else its full suite;
A.3 one end line per child plus the total line, exit = first nonzero;
A.4 root files outside every child that are full triggers run that child.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ptest import contracts as C
from ptest import monorepo
from ptest.cli import main


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args),
        cwd=root, check=True, capture_output=True, text=True,
        timeout=60,
    )
    return proc.stdout.strip()


def _repo(root: Path, files: dict[str, str]) -> None:
    _git(root, "init", "-b", "main")
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")


def _child_toml(project_id: str, kind: str = "command") -> str:
    if kind == "vitest":
        return ('version = 1\nproject_id = "' + project_id + '"\n'
                '[runner]\nkind = "vitest"\nlauncher = ["node"]\n'
                'test_roots = ["src"]\n')
    return ('version = 1\nproject_id = "' + project_id + '"\n'
            '[runner]\nkind = "command"\nlauncher = ["true"]\n')


def _monorepo(root: Path, web_kind: str = "command") -> None:
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8")
    for child, seed in (("api", "ab"), ("web", "cd")):
        child_dir = root / child
        child_dir.mkdir(exist_ok=True)
        (child_dir / ".ptest.toml").write_text(
            _child_toml(seed * 16, kind=web_kind if child == "web" else "command"),
            encoding="utf-8")


def _result(exit_code: int = 0):
    return type("R", (), {"reasons": (), "exit_code": exit_code,
                          "status": C.Status.PASSED, "counts": None})()


# --- A.1: unit twins for change classification ---

def test_changed_files_sees_uncommitted_and_untracked(tmp_path):
    _repo(tmp_path, {"api/a.py": "1\n"})
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    (tmp_path / "api" / "new.py").write_text("new\n", encoding="utf-8")

    changed = monorepo.worktree_changed_files(tmp_path, None)

    assert "api/a.py" in changed
    assert "api/new.py" in changed


def test_changed_files_limits_committed_range_to_base(tmp_path):
    _repo(tmp_path, {"api/a.py": "1\n", "web/w.js": "1\n"})
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "touch api")

    assert monorepo.worktree_changed_files(tmp_path, base) == ("api/a.py",)


def _preflight(tmp_path):
    return monorepo.preflight_children(
        tmp_path, monorepo.parse_monorepo_manifest(
            b'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
            tmp_path / ".ptest.toml"))


def test_select_changed_runs_only_the_touched_child(tmp_path):
    _monorepo(tmp_path)
    _repo(tmp_path, {"api/a.py": "1\n", "web/w.js": "1\n"})
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    children = _preflight(tmp_path)

    selected = monorepo.select_changed_children(tmp_path, children, None)

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_select_changed_clean_tree_runs_nothing(tmp_path):
    _monorepo(tmp_path)
    _repo(tmp_path, {"api/a.py": "1\n", "web/w.js": "1\n"})
    children = _preflight(tmp_path)

    selected = monorepo.select_changed_children(tmp_path, children, None)

    assert [item.run for item in selected] == [False, False]


def test_select_changed_outside_git_runs_every_child(tmp_path):
    children = tuple(monorepo.ChildTarget(
        declaration=name, directory=tmp_path / name, config=None)
        for name in ("api", "web"))

    selected = monorepo.select_changed_children(tmp_path, children, None)

    assert [item.run for item in selected] == [True, True]


# --- A.4: root files that are full triggers ---

def test_select_changed_root_lockfile_runs_only_the_triggered_child(case):
    web_config = case.config(runner_kind="command")
    api_selection = case.config(runner_kind="command").selection
    from dataclasses import replace
    api_config = replace(case.config(runner_kind="command"),
                         selection=replace(api_selection, full_triggers=("uv.lock",)))
    children = (
        monorepo.ChildTarget(declaration="api", directory=case.base / "api",
                             config=api_config),
        monorepo.ChildTarget(declaration="web", directory=case.base / "web",
                             config=web_config),
    )
    _repo(case.base, {"api/a.py": "1\n", "web/w.js": "1\n", "uv.lock": "v1\n"})
    (case.base / "uv.lock").write_text("v2\n", encoding="utf-8")

    selected = monorepo.select_changed_children(case.base, children, None)

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_select_changed_root_manifest_runs_every_child(case):
    children = tuple(monorepo.ChildTarget(
        declaration=name, directory=case.base / name,
        config=case.config(runner_kind="command")) for name in ("api", "web"))
    _repo(case.base, {"api/a.py": "1\n", ".ptest.toml": "root\n"})
    (case.base / ".ptest.toml").write_text("root-changed\n", encoding="utf-8")

    selected = monorepo.select_changed_children(case.base, children, None)

    assert [item.run for item in selected] == [True, True]


# --- A.2: vitest request shape ---

def test_vitest_child_with_base_uses_vitest_changed_argv(case):
    child = monorepo.ChildTarget(
        declaration="web", directory=case.base / "web",
        config=case.config(runner_kind="vitest"))

    request = monorepo.child_changed_request(child, base="abc123")

    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("--changed", "abc123")


def test_vitest_child_without_base_falls_back_to_changed_mode(case):
    child = monorepo.ChildTarget(
        declaration="web", directory=case.base / "web",
        config=case.config(runner_kind="vitest"))

    request = monorepo.child_changed_request(child, base=None)

    assert request.mode is C.Mode.AUTOMATIC
    assert request.argv == ()


def test_command_child_uses_changed_mode_with_base_passthrough(case):
    child = monorepo.ChildTarget(
        declaration="api", directory=case.base / "api",
        config=case.config(runner_kind="command"))

    request = monorepo.child_changed_request(child, base="abc123")

    assert request.mode is C.Mode.AUTOMATIC
    assert request.base == "abc123"


# --- cli.py monorepo branch twins ---

def test_root_changed_runs_touched_child_and_skips_clean_one(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed",)) == 0
    assert [call[1].mode for call in calls] == [C.Mode.AUTOMATIC]
    assert calls[0][0].config_path.parent.name == "api"
    err = capsys.readouterr().err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err


def test_root_changed_clean_tree_skips_every_child(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed",)) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "ptest: api · no changes" in err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err


def test_root_changed_passes_base_through_to_children(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "touch api")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed", "--base", base)) == 0
    assert len(calls) == 1
    assert calls[0][1].base == base
    assert "ptest: web · no changes" in capsys.readouterr().err


def test_root_changed_returns_first_failure_after_all_children_finish(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "api" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "web" / "w.js").write_text("y = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    codes = iter((9, 3))
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: _result(next(codes)),
    )

    assert main(("--changed",)) == 9
    assert "ptest: total" in capsys.readouterr().err


def test_root_changed_vitest_child_gets_changed_argv(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path, web_kind="vitest")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "web" / "src" / "app.test.ts").parent.mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "app.test.ts").write_text(
        "test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed", "--base", base)) == 0
    assert len(calls) == 1
    assert calls[0][1].argv == ("--changed", base)
    assert "ptest: api · no changes" in capsys.readouterr().err


def test_root_changed_vitest_child_without_base_runs_full_gate(
        tmp_path, monkeypatch, capsys):
    _monorepo(tmp_path, web_kind="vitest")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "web" / "src" / "app.test.ts").parent.mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "app.test.ts").write_text(
        "test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed",)) == 0
    assert len(calls) == 1
    assert calls[0][1].mode is C.Mode.AUTOMATIC
    assert calls[0][1].argv == ()
