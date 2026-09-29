"""Init commit reminder lists every uncommitted ptest file (T3)."""
from __future__ import annotations

import json

import pytest

from ptest.config import init_project
from ptest.contracts import InitAction, InitOptions, RunnerKind
from support import git, init_git_repo, write_file, write_ptest_toml

_V2_MANIFEST = 'version = 2\n[monorepo]\nchildren = ["api", "web"]\n'
_SKILL = "# ptest skill\n"


def _options(**kwargs):
    return InitOptions(runner=kwargs.pop("runner", None),
                       dry_run=kwargs.pop("dry_run", False),
                       reveal_command=False, **kwargs)


def _untracked_v2_repo(root):
    """Git repo with committed README and untracked ptest files only."""
    repo = init_git_repo(root, files={"README.md": "x\n"})
    (repo / ".ptest.toml").write_text(_V2_MANIFEST, encoding="utf-8")
    (repo / "api").mkdir()
    (repo / "web").mkdir()
    write_ptest_toml(repo / "api")
    write_ptest_toml(repo / "web")
    write_file(repo / ".claude" / "skills" / "ptest" / "SKILL.md", _SKILL)
    return repo


_EXPECTED = (".ptest.toml", "api/.ptest.toml", "web/.ptest.toml",
             ".claude/skills/ptest/SKILL.md")


def test_unchanged_config_init_project_lists_every_uncommitted_ptest_file(tmp_path):
    repo = _untracked_v2_repo(tmp_path / "repo")

    result = init_project(repo, _options())

    assert result.action is InitAction.EXISTING
    assert result.commit_paths == _EXPECTED


def test_unchanged_config_text_init_names_all_files(tmp_path, monkeypatch, capsys):
    from ptest import cli

    repo = _untracked_v2_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    assert cli.main(("init", "--agents", "none", "--no-doctor")) == 0
    out = capsys.readouterr().out

    assert "Commit these files:" in out
    for name in _EXPECTED:
        assert name in out


def test_unchanged_config_json_commit_paths_match(tmp_path, monkeypatch, capsys):
    from ptest import cli

    repo = _untracked_v2_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    assert cli.main(("init", "--json", "--agents", "none")) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["data"]["commit_paths"] == list(_EXPECTED)


def test_committed_everything_clears_reminder(tmp_path, monkeypatch, capsys):
    from ptest import cli

    repo = _untracked_v2_repo(tmp_path / "repo")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ptest config")
    monkeypatch.chdir(repo)

    assert cli.main(("init", "--json", "--agents", "none")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["commit_paths"] == []

    assert cli.main(("init", "--agents", "none", "--no-doctor")) == 0
    assert "Commit these files:" not in capsys.readouterr().out


def test_fresh_standalone_create_orders_written_first_deduped(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    write_file(repo / "docs" / "ptest-agent.md", "# guide\n")

    result = init_project(repo, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert result.commit_paths == (".ptest.toml", "docs/ptest-agent.md")


def test_agent_rule_files_created_this_run_appended_once(tmp_path, monkeypatch, capsys):
    from ptest import cli
    from ptest.agent_rules import _legacy_provider_text

    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    skill = repo / ".claude" / "skills" / "ptest" / "SKILL.md"
    write_file(skill, _legacy_provider_text("claude").decode("utf-8"))
    monkeypatch.chdir(repo)

    assert cli.main(("init", "--json", "--runner", "pytest",
                     "--agents", "claude", "--no-doctor")) == 0
    payload = json.loads(capsys.readouterr().out)

    paths = payload["data"]["commit_paths"]
    assert len(paths) == len(set(paths))
    assert paths[0] == ".ptest.toml"
    assert paths.count(".claude/skills/ptest/SKILL.md") == 1
    assert "docs/ptest-agent.md" in paths


def test_untracked_non_ptest_files_never_listed(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    write_file(repo / "notes.toml", "x = 1\n")
    write_file(repo / "TODO.md", "todo\n")
    write_ptest_toml(repo)

    result = init_project(repo, _options())

    assert result.action is InitAction.EXISTING
    assert result.commit_paths == (".ptest.toml",)


def test_non_git_directory_gives_empty(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    result = init_project(plain, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert result.commit_paths == ()


def test_dry_run_gives_no_reminder(tmp_path, monkeypatch, capsys):
    from ptest import cli

    repo = _untracked_v2_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    assert cli.main(("init", "--dry-run", "--agents", "none",
                     "--no-doctor")) == 0
    assert "Commit these files:" not in capsys.readouterr().out

    fresh = init_git_repo(tmp_path / "fresh", files={"README.md": "x\n"})
    result = init_project(fresh, _options(runner=RunnerKind.PYTEST,
                                          dry_run=True))
    assert result.action is InitAction.PREVIEW
    assert result.commit_paths == ()


def test_nested_config_root_lists_boundary_relative_path(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    sub = repo / "sub"
    sub.mkdir()

    result = init_project(sub, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert result.target == sub / ".ptest.toml"
    assert result.commit_paths == ("sub/.ptest.toml",)


def test_scan_failure_falls_back_to_written_list(tmp_path, monkeypatch):
    import ptest.worktree as worktree

    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    monkeypatch.setattr(worktree, "uncommitted_config_files", lambda *a, **k: ())

    result = init_project(repo, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert result.commit_paths == (".ptest.toml",)


def test_control_characters_render_sanitized(tmp_path):
    from ptest import contracts as C
    from ptest.init_render import _commit_reminder_lines

    result = C.InitResult(action=C.InitAction.EXISTING,
                          target=tmp_path / ".ptest.toml", exists=True,
                          config=None,
                          commit_paths=(".ptest.toml", "a\x07b",))
    lines = _commit_reminder_lines(result, 80, dry_run=False)

    text = "\n".join(lines)
    assert "\x07" not in text
    assert ".ptest.toml" in text
