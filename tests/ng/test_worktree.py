"""Static linked-worktree detection and committed-state checks."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ptest import worktree as W
from support import git, init_git_repo, write_ptest_toml


def _linked_worktree(tmp_path, *, commit_config=False, **toml):
    """main checkout (committed README) + linked worktree at tmp_path/wt."""
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main, **toml)
    if commit_config:
        git(main, "add", ".ptest.toml")
        git(main, "commit", "-q", "-m", "config")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    return main, wt


def test_linked_worktree_detects_real_git_worktree(tmp_path):
    main, wt = _linked_worktree(tmp_path)

    link = W.linked_worktree(wt)

    assert link is not None
    assert link.root == wt
    assert link.main_root == main


def test_linked_worktree_main_checkout_is_none(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})

    assert W.linked_worktree(main) is None


def test_linked_worktree_non_git_dir_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert W.linked_worktree(plain) is None


def test_linked_worktree_bare_repo_worktree_is_none(tmp_path):
    src = init_git_repo(tmp_path / "src", files={"README.md": "x\n"})
    git(tmp_path, "clone", "-q", "--bare", str(src), "m.git")
    bare_wt = tmp_path / "bare-wt"
    git(tmp_path / "m.git", "worktree", "add", "--detach", str(bare_wt))

    assert W.linked_worktree(bare_wt) is None


def test_linked_worktree_submodule_style_gitfile_is_none(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    sub = tmp_path / "sub"
    sub.mkdir()
    # A submodule-style .git file points at a gitdir with no commondir.
    (sub / ".git").write_text(f"gitdir: {main}/.git\n", encoding="utf-8")

    assert W.linked_worktree(sub) is None


def test_linked_worktree_git_symlink_is_none(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    dotgit = wt / ".git"
    content = dotgit.read_bytes()
    dotgit.unlink()
    dotgit.symlink_to(tmp_path / "elsewhere")

    assert W.linked_worktree(wt) is None
    # Restore so the tmp tree stays consistent (not strictly required).
    dotgit.unlink()
    dotgit.write_bytes(content)


def test_linked_worktree_oversize_gitfile_is_none(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    (wt / ".git").write_bytes(b"gitdir: " + b"x" * 4096 + b"\n")

    assert W.linked_worktree(wt) is None


@pytest.mark.parametrize("content", [
    b"garbage\n",
    b"gitdir: a\nsecond: b\n",
    b"gitdir:\n",
    b"gitdir: \n",
    b"notgitdir: x\n",
    b"gitdir: has\0nul\n",
    b"gitdir: has\x01control\n",
    b"",
])
def test_linked_worktree_garbage_gitfile_is_none(tmp_path, content):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".git").write_bytes(content)

    assert W.linked_worktree(plain) is None


def test_linked_worktree_back_link_mismatch_is_none(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    fake_gitdir = main / ".git" / "worktrees" / "fake"
    fake_gitdir.mkdir(parents=True)
    (fake_gitdir / "commondir").write_text(str(main / ".git") + "\n",
                                           encoding="utf-8")
    (fake_gitdir / "gitdir").write_text("/some/other/place/.git\n",
                                         encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {fake_gitdir}\n", encoding="utf-8")

    assert W.linked_worktree(wt) is None


def test_linked_worktree_gitdir_across_symlink_is_none(tmp_path):
    main = init_git_repo(tmp_path / "cm", files={"README.md": "x\n"})
    real = tmp_path / "a" / "gd"
    real.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (real / "commondir").write_text(str(main / ".git") + "\n",
                                    encoding="utf-8")
    (real / "gitdir").write_text(str(wt / ".git") + "\n", encoding="utf-8")
    (tmp_path / "sym").symlink_to(tmp_path / "a", target_is_directory=True)
    (wt / ".git").write_text(f"gitdir: {tmp_path}/sym/gd\n",
                             encoding="utf-8")

    assert W.linked_worktree(wt) is None


def test_linked_worktree_and_main_config_use_no_subprocess(tmp_path, monkeypatch):
    main, wt = _linked_worktree(tmp_path)

    def _fail(*args, **kwargs):
        raise AssertionError("subprocess must not be used for detection")

    monkeypatch.setattr(subprocess, "Popen", _fail)

    link = W.linked_worktree(wt)
    assert link is not None and link.main_root == main
    assert W.main_config(wt, wt) is not None


def test_main_config_nearest_first_wins(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main)
    api = main / "api"
    api.mkdir()
    write_ptest_toml(api)
    (main / "api" / "x").mkdir()
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    found = W.main_config(wt / "api" / "x", wt)

    assert found is not None
    assert found.relative == "api"
    assert found.path == main / "api" / ".ptest.toml"
    assert found.children == ()


def test_main_config_lists_v2_children(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8",
    )
    for child in ("api", "web"):
        (main / child).mkdir()
        write_ptest_toml(main / child)
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    found = W.main_config(wt, wt)

    assert found is not None
    assert found.relative == "."
    assert found.path == main / ".ptest.toml"
    assert found.children == ("api", "web")


def test_main_config_ignores_symlinked_main_config(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    write_ptest_toml(scratch, kind="command")
    real = tmp_path / "real.toml"
    real.write_bytes((scratch / ".ptest.toml").read_bytes())
    (main / ".ptest.toml").symlink_to(real)
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    assert W.main_config(wt, wt) is None


def test_main_config_outside_root_is_none(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    assert W.main_config(other, wt) is None
    assert W.main_config(wt, other) is None


def test_uncommitted_config_files_untracked_then_committed(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    # An uncommitted main-checkout config never reaches the worktree, so a
    # worktree-local config stands in for the untracked/staged states.
    write_ptest_toml(wt)

    assert W.uncommitted_config_files(wt) == (".ptest.toml",)

    git(wt, "add", ".ptest.toml")
    assert W.uncommitted_config_files(wt) == (".ptest.toml",)

    git(wt, "commit", "-q", "-m", "config")
    assert W.uncommitted_config_files(wt) == ()


def test_uncommitted_config_files_non_git_is_empty(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".ptest.toml").write_text("version = 1\n", encoding="utf-8")

    assert W.uncommitted_config_files(plain) == ()


def test_uncommitted_config_files_non_git_spawns_no_subprocess(
        tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".ptest.toml").write_text("version = 1\n", encoding="utf-8")
    (plain / "docs").mkdir()
    (plain / "docs" / "ptest-agent.md").write_text(
        "<!-- ptest-agent-rules:start -->\nold\n", encoding="utf-8")

    def _fail(*args, **kwargs):
        # BaseException on purpose: uncommitted_config_files swallows
        # Exception, so AssertionError would pass vacuously.
        pytest.fail("uncommitted check must not spawn git outside git")

    monkeypatch.setattr(subprocess, "Popen", _fail)

    assert W.uncommitted_config_files(plain) == ()
    assert W.uncommitted_config_files(plain, include_agent_rules=True) == ()


def test_uncommitted_config_files_unborn_head_is_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".ptest.toml").write_text("version = 1\n", encoding="utf-8")

    assert W.uncommitted_config_files(repo) == ()


def test_uncommitted_config_files_lists_monorepo_children(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api"]\n', encoding="utf-8")
    (main / "api").mkdir()
    write_ptest_toml(main / "api")

    assert W.uncommitted_config_files(main) == (
        ".ptest.toml", "api/.ptest.toml")

    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "all")
    assert W.uncommitted_config_files(main) == ()


def test_uncommitted_config_files_agent_rules_opt_in(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main)
    (main / "AGENTS.md").write_text("# plain notes\n", encoding="utf-8")
    docs = main / "docs"
    docs.mkdir()
    (docs / "ptest-agent.md").write_text(
        "<!-- ptest-agent-rules:start -->\n", encoding="utf-8")

    assert W.uncommitted_config_files(main) == (".ptest.toml",)
    assert W.uncommitted_config_files(main, include_agent_rules=True) == (
        ".ptest.toml", "docs/ptest-agent.md")

    (main / "AGENTS.md").write_text(
        "<!-- ptest-agent-rules:start -->\n# managed\n", encoding="utf-8")
    assert W.uncommitted_config_files(main, include_agent_rules=True) == (
        ".ptest.toml", "docs/ptest-agent.md", "AGENTS.md")


def test_uncommitted_config_files_without_git_binary_is_empty(
        tmp_path, monkeypatch):
    main, wt = _linked_worktree(tmp_path)
    write_ptest_toml(wt)
    assert W.uncommitted_config_files(wt) == (".ptest.toml",)

    monkeypatch.setenv("PATH", "")
    assert W.uncommitted_config_files(wt) == ()


# --- fail-closed branches ---

def _gitdir_of(wt):
    text = (wt / ".git").read_text(encoding="utf-8")
    assert text.startswith("gitdir: ")
    return Path(text[len("gitdir: "):].strip())


def test_linked_worktree_non_utf8_and_crlf_gitfiles_are_none(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    (wt / ".git").write_bytes(b"gitdir: \xff\xfe\n")
    assert W.linked_worktree(wt) is None

    (wt / ".git").write_bytes(
        f"gitdir: {main}/.git/worktrees/x\r\n".encode())
    assert W.linked_worktree(wt) is None


def test_linked_worktree_tolerates_crlf_gitfile(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    gitdir = _gitdir_of(wt)
    (wt / ".git").write_bytes(f"gitdir: {gitdir}\r\n".encode())

    link = W.linked_worktree(wt)

    assert link is not None
    assert link.main_root == main


def test_linked_worktree_never_raises_on_unexpected_input(tmp_path):
    assert W.linked_worktree(None) is None
    assert W.main_config(None, tmp_path) is None
    assert W.uncommitted_config_files(None) == ()


def test_linked_worktree_unreadable_gitfile_is_none(tmp_path, monkeypatch):
    from ptest import contracts as _C
    from ptest.worktree import read_regular as _real_read
    main, wt = _linked_worktree(tmp_path)

    def _boom(root, relative, limit):
        if relative == ".git":
            raise _C.Problem(code="state-unavailable",
                             message="unavailable", phase="files",
                             retryable=False)
        return _real_read(root, relative, limit)

    monkeypatch.setattr("ptest.worktree.read_regular", _boom)
    assert W.linked_worktree(wt) is None


def test_linked_worktree_corrupt_gitdir_admin_files_are_none(tmp_path):
    main, wt = _linked_worktree(tmp_path)
    gitdir = _gitdir_of(wt)
    commondir_text = (gitdir / "commondir").read_text(encoding="utf-8")
    gitdir_text = (gitdir / "gitdir").read_text(encoding="utf-8")

    (gitdir / "commondir").write_bytes(b"x" * 5000)
    assert W.linked_worktree(wt) is None

    (gitdir / "commondir").write_bytes(b"one\ntwo\n")
    assert W.linked_worktree(wt) is None

    (gitdir / "commondir").write_bytes(b"\n")
    assert W.linked_worktree(wt) is None

    (gitdir / "commondir").write_text(commondir_text, encoding="utf-8")
    (gitdir / "gitdir").unlink()
    assert W.linked_worktree(wt) is None

    (gitdir / "gitdir").write_bytes(b"x" * 5000)
    assert W.linked_worktree(wt) is None

    (gitdir / "gitdir").write_bytes(b"a\nb\n")
    assert W.linked_worktree(wt) is None

    (gitdir / "gitdir").write_bytes(b"\n")
    assert W.linked_worktree(wt) is None

    (gitdir / "gitdir").write_text(gitdir_text, encoding="utf-8")
    assert W.linked_worktree(wt) is not None


def _manual_layout(tmp_path, name, *, commondir_text, back_text=None):
    root = tmp_path / name
    root.mkdir()
    gd = root / "gd"
    gd.mkdir()
    (gd / "commondir").write_text(commondir_text, encoding="utf-8")
    if back_text is not None:
        (gd / "gitdir").write_text(back_text, encoding="utf-8")
    (root / ".git").write_text(f"gitdir: {gd}\n", encoding="utf-8")
    return root


def test_linked_worktree_common_dir_edge_cases_are_none(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})

    missing = _manual_layout(
        tmp_path, "wt-missing",
        commondir_text=str(tmp_path / "nope" / ".git") + "\n",
        back_text=str(tmp_path / "wt-missing" / ".git") + "\n")
    assert W.linked_worktree(missing) is None

    # A common dir named exactly `.git` that is a regular file, not a dir.
    fakeroot = tmp_path / "fakeroot"
    fakeroot.mkdir()
    (fakeroot / ".git").write_text("not a directory\n", encoding="utf-8")
    notdir = _manual_layout(
        tmp_path, "wt-notdir",
        commondir_text=str(fakeroot / ".git") + "\n",
        back_text=str(tmp_path / "wt-notdir" / ".git") + "\n")
    assert W.linked_worktree(notdir) is None


def test_main_config_on_plain_checkout_and_oversize_main_config(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    write_ptest_toml(main)

    assert W.main_config(main, main) is None

    big = tmp_path / "bigmain"
    big.mkdir()
    init_git_repo(big, files={"README.md": "x\n"})
    (big / ".ptest.toml").write_bytes(b"x = 1\n" + b"#" * (300 * 1024))
    big_wt = tmp_path / "bigwt"
    git(big, "worktree", "add", "-q", "-b", "wt", str(big_wt))
    assert W.main_config(big_wt, big_wt) is None


def test_main_config_invalid_main_toml_lists_no_children(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_bytes(b"\xff\xfe invalid \x00\n")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    found = W.main_config(wt, wt)
    assert found is not None
    assert found.relative == "."
    assert found.children == ()


def test_main_config_version_without_manifest_lists_no_children(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_text("x = 1\n", encoding="utf-8")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    found = W.main_config(wt, wt)
    assert found is not None
    assert found.children == ()


def test_main_config_broken_v2_manifest_lists_no_children(tmp_path):
    main = init_git_repo(tmp_path / "main", files={"README.md": "x\n"})
    (main / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = []\n", encoding="utf-8")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "wt", str(wt))

    found = W.main_config(wt, wt)
    assert found is not None
    assert found.children == ()


def test_uncommitted_config_files_local_config_edge_cases(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    assert W.uncommitted_config_files(repo) == ()
    assert W.uncommitted_config_files(repo, include_agent_rules=True) == ()

    (repo / ".ptest.toml").write_bytes(b"x" * (300 * 1024))
    assert W.uncommitted_config_files(repo) == (".ptest.toml",)

    (repo / ".ptest.toml").write_bytes(b"\xff\xfe binary \x00\n")
    assert W.uncommitted_config_files(repo) == (".ptest.toml",)

    (repo / ".ptest.toml").write_text("x = 1\n", encoding="utf-8")
    assert W.uncommitted_config_files(repo) == (".ptest.toml",)

    (repo / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = []\n", encoding="utf-8")
    assert W.uncommitted_config_files(repo) == (".ptest.toml",)


def test_uncommitted_config_files_skips_oversize_agent_rules(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    write_ptest_toml(repo)
    docs = repo / "docs"
    docs.mkdir()
    (docs / "ptest-agent.md").write_bytes(b"#" * (300 * 1024))

    assert W.uncommitted_config_files(
        repo, include_agent_rules=True) == (".ptest.toml",)


def test_uncommitted_config_files_through_symlinked_root(tmp_path):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    write_ptest_toml(repo)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)

    assert W.uncommitted_config_files(link) == (".ptest.toml",)

    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "all")
    assert W.uncommitted_config_files(link) == ()


def test_uncommitted_config_files_undecodable_git_output_is_empty(
        tmp_path, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", files={"README.md": "x\n"})
    write_ptest_toml(repo)

    def _bad_git(root, scan, *argv, **kwargs):
        return b"\xff\xfe\x00"

    monkeypatch.setattr("ptest.source._git", _bad_git)
    assert W.uncommitted_config_files(repo) == ()
