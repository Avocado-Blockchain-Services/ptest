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


# --- A.1: unit twins for change classification ---

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


def _preflight(tmp_path):
    return monorepo_api.preflight_children(
        tmp_path, monorepo_api.parse_monorepo_manifest(
            b'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
            tmp_path / ".ptest.toml"))


def test_select_changed_runs_only_the_touched_child(tmp_path, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, files={"api/a.py": "1\n", "web/w.js": "1\n"}, message="base")
    (tmp_path / "api" / "a.py").write_text("2\n", encoding="utf-8")
    children = _preflight(tmp_path)
    heads = _covering_heads(tmp_path)

    selected = monorepo_api.select_changed_children(
        tmp_path, children, None, heads)

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_select_changed_clean_tree_skips_only_baseline_covered_children(tmp_path, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, files={"api/a.py": "1\n", "web/w.js": "1\n"}, message="base")
    children = _preflight(tmp_path)
    heads = _covering_heads(tmp_path)

    selected = monorepo_api.select_changed_children(
        tmp_path, children, None, heads)

    assert [item.run for item in selected] == [False, False]


def test_select_changed_clean_tree_without_baselines_runs_every_child(tmp_path, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, files={"api/a.py": "1\n", "web/w.js": "1\n"}, message="base")
    children = _preflight(tmp_path)

    selected = monorepo_api.select_changed_children(
        tmp_path, children, None, {"api": None, "web": None})

    assert [item.run for item in selected] == [True, True]


def _covering_heads(root: Path) -> dict[str, str]:
    head = git(root, "rev-parse", "HEAD")
    return {"api": head, "web": head}


def _publish_baseline(case, domain, checkout, head: str) -> None:
    from ptest import history as history_api

    digest = "11" * 32
    snapshot = case.snapshot(head=head, digest=digest,
                             compatibility="compat-v1")
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=digest, compatibility="compat-v1")
    result = case.result(sequence=1, plan=plan,
                         project_id=checkout.project_id,
                         checkout_id=checkout.checkout_id,
                         input_before=snapshot, input_after=snapshot,
                         policy_digest="33" * 32)
    inventory = case.inventory(("tests/test_a.py",))
    published = history_api.publish_outcome(domain, checkout, result, inventory)
    assert published.baseline_published is True


def _checkouts(children):
    from ptest import operations

    return {child.declaration: operations._checkout(child.config)
            for child in children}


def _storerepo(case, monorepo):
    """Git monorepo inside the fixture domain (history requires it)."""
    domain = case.domain()
    repo = monorepo(_child_specs(), parent=domain.root, name="repo")
    init_git_repo(repo, files={"api/a.py": "1\n", "web/w.js": "1\n"}, message="base")
    return domain, repo


def test_child_baseline_heads_reads_recorded_baselines(case, monorepo):
    from ptest import history as history_api

    domain, repo = _storerepo(case, monorepo)
    children = _preflight(repo)
    head = git(repo, "rev-parse", "HEAD")
    checkouts = _checkouts(children)
    _publish_baseline(case, domain, checkouts["api"], head)

    heads = monorepo_api.child_baseline_heads(domain, children)

    assert heads == {"api": head, "web": None}
    reread = history_api.read_history(domain, checkouts["api"])
    assert reread.baseline is not None and reread.baseline.head == head


def test_no_base_runs_child_with_committed_change_since_baseline(case, monorepo):
    domain, repo = _storerepo(case, monorepo)
    children = _preflight(repo)
    checkouts = _checkouts(children)
    base = git(repo, "rev-parse", "HEAD")
    _publish_baseline(case, domain, checkouts["api"], base)
    _publish_baseline(case, domain, checkouts["web"], base)
    (repo / "api" / "a.py").write_text("2\n", encoding="utf-8")
    git(repo, "commit", "-am", "touch api")

    heads = monorepo_api.child_baseline_heads(domain, children)
    assert heads == {"api": base, "web": base}
    selected = monorepo_api.select_changed_children(repo, children, None, heads)

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_no_base_uncommitted_change_runs_despite_covering_baseline(case, monorepo):
    domain, repo = _storerepo(case, monorepo)
    children = _preflight(repo)
    checkouts = _checkouts(children)
    head = git(repo, "rev-parse", "HEAD")
    _publish_baseline(case, domain, checkouts["api"], head)
    _publish_baseline(case, domain, checkouts["web"], head)
    (repo / "api" / "a.py").write_text("2\n", encoding="utf-8")

    heads = monorepo_api.child_baseline_heads(domain, children)
    selected = monorepo_api.select_changed_children(repo, children, None, heads)

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_root_changed_uses_baselines_when_base_is_absent(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "touch api")
    head = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monorepo_api, "child_baseline_heads",
                        lambda domain, children: {"api": base, "web": head})
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed",)) == 0
    assert [call[0].config_path.parent.name for call in calls] == ["api"]
    assert calls[0][1].mode is C.Mode.AUTOMATIC
    err = capsys.readouterr().err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err


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


def test_select_changed_outside_git_runs_every_child(tmp_path):
    children = tuple(monorepo_api.ChildTarget(
        declaration=name, directory=tmp_path / name, config=None)
        for name in ("api", "web"))

    selected = monorepo_api.select_changed_children(tmp_path, children, None)

    assert [item.run for item in selected] == [True, True]


# --- A.4: root files that are full triggers ---

def test_select_changed_root_lockfile_runs_only_the_triggered_child(case):
    web_config = case.config(runner_kind="command")
    api_selection = case.config(runner_kind="command").selection
    from dataclasses import replace
    api_config = replace(case.config(runner_kind="command"),
                         selection=replace(api_selection, full_triggers=("uv.lock",)))
    children = (
        monorepo_api.ChildTarget(declaration="api", directory=case.base / "api",
                             config=api_config),
        monorepo_api.ChildTarget(declaration="web", directory=case.base / "web",
                             config=web_config),
    )
    init_git_repo(case.base, files={"api/a.py": "1\n", "web/w.js": "1\n", "uv.lock": "v1\n"}, message="base")
    (case.base / "uv.lock").write_text("v2\n", encoding="utf-8")

    selected = monorepo_api.select_changed_children(
        case.base, children, None, _covering_heads(case.base))

    assert {item.target.declaration: item.run for item in selected} == {
        "api": True, "web": False}


def test_select_changed_root_manifest_runs_every_child(case):
    children = tuple(monorepo_api.ChildTarget(
        declaration=name, directory=case.base / name,
        config=case.config(runner_kind="command")) for name in ("api", "web"))
    init_git_repo(case.base, files={"api/a.py": "1\n", ".ptest.toml": "root\n"}, message="base")
    (case.base / ".ptest.toml").write_text("root-changed\n", encoding="utf-8")

    selected = monorepo_api.select_changed_children(
        case.base, children, None, _covering_heads(case.base))

    assert [item.run for item in selected] == [True, True]


# --- A.2: vitest request shape ---

def test_vitest_child_with_base_uses_vitest_changed_argv(case):
    child = monorepo_api.ChildTarget(
        declaration="web", directory=case.base / "web",
        config=case.config(runner_kind="vitest"))

    request = monorepo_api.child_changed_request(child, base="abc123")

    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("--changed", "abc123")


def test_vitest_child_without_base_falls_back_to_changed_mode(case):
    child = monorepo_api.ChildTarget(
        declaration="web", directory=case.base / "web",
        config=case.config(runner_kind="vitest"))

    request = monorepo_api.child_changed_request(child, base=None)

    assert request.mode is C.Mode.AUTOMATIC
    assert request.argv == ()


def test_command_child_uses_changed_mode_with_base_passthrough(case):
    child = monorepo_api.ChildTarget(
        declaration="api", directory=case.base / "api",
        config=case.config(runner_kind="command"))

    request = monorepo_api.child_changed_request(child, base="abc123")

    assert request.mode is C.Mode.AUTOMATIC
    assert request.base == "abc123"


# --- cli.py monorepo branch twins ---

def test_root_changed_runs_touched_child_and_skips_clean_one(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    head = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monorepo_api, "child_baseline_heads",
                        lambda domain, children: {"api": head, "web": head})
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


def test_root_changed_clean_tree_skips_baseline_covered_children(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    head = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monorepo_api, "child_baseline_heads",
                        lambda domain, children: {"api": head, "web": head})
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
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "touch api")
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
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
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
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs("vitest"), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    base = git(tmp_path, "rev-parse", "HEAD")
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
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs("vitest"), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    (tmp_path / "web" / "src" / "app.test.ts").parent.mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "app.test.ts").write_text(
        "test\n", encoding="utf-8")
    head = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monorepo_api, "child_baseline_heads",
                        lambda domain, children: {"api": head, "web": head})
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


def test_root_changed_clean_tree_without_baselines_runs_every_child(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo(_child_specs(), parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(monorepo_api, "child_baseline_heads",
                        lambda domain, children: {"api": None, "web": None})
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or _result(),
    )

    assert main(("--changed",)) == 0
    assert [call[0].config_path.parent.name for call in calls] == ["api", "web"]
    assert "ptest: total" in capsys.readouterr().err
