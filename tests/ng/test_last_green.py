"""Last-green-run reference: bare ptest diffs since the last passing run.

Twins (each fails before the feature, passes after):
- default-branch commit without testing -> next bare ptest runs it
- green run -> next bare ptest reports no changes since the last green run
- failing test file is rerun (via the recorded last-failed set) until green
- rebase (recorded commit not an ancestor) -> fallback to the branch base
- --base overrides the green record
- monorepo children are tracked independently
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import lastgreen
from ptest.cli import main
from support import git, git_commit_all, init_git_repo, write_ptest_toml


# --- harness ---------------------------------------------------------------

def _standalone_cmd(root: Path, monkeypatch, **toml) -> None:
    write_ptest_toml(root, kind="command", launcher=("true",),
                     args=(), full_args=(), project_id="ab" * 16, **toml)
    monkeypatch.chdir(root)


def _capture(monkeypatch, outcomes):
    """Stub operations.execute; pop (status, exit_code) per call, default pass."""
    calls = []

    def fake_execute(domain, config, request):
        calls.append(request)
        if outcomes:
            status, code = outcomes.pop(0)
        else:
            status, code = C.Status.PASSED, 0
        return types.SimpleNamespace(reasons=(), exit_code=code,
                                     status=status, counts=None)

    monkeypatch.setattr("ptest.operations.execute", fake_execute)
    return calls


def _pytest_project(root: Path, monkeypatch) -> None:
    write_ptest_toml(root, kind="pytest", launcher=("true",),
                     args=(), full_args=(), project_id="ab" * 16)
    with (root / ".ptest.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n[selection]\nenabled = true\n")
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "greet.py").write_text(
        "def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / "tests" / "test_greet.py").write_text(
        "from src.greet import hello\n\n"
        "def test_hello():\n    assert hello() == 'hi'\n",
        encoding="utf-8")
    (root / "tests" / "test_other.py").write_text(
        "def test_other():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "test_third.py").write_text(
        "def test_third():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)


# --- twin 1: commit without testing on the default branch -------------------

def test_default_branch_commit_without_testing_runs_next(tmp_path, monkeypatch, capsys):
    init_git_repo(tmp_path, branch="main", files={})
    _standalone_cmd(tmp_path, monkeypatch)
    calls = _capture(monkeypatch, [])

    assert main(()) == 0
    assert len(calls) == 1  # first green run records the point

    # A commit made without running tests must be tested by the next ptest,
    # even on the default branch where the merge-base reference is HEAD.
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commit_all(tmp_path, message="unverified change")

    assert main(()) == 0
    assert len(calls) == 2
    assert "changed since last green run" in calls[1].changed_note
    err = capsys.readouterr().err
    assert "no changes" not in err


# --- twin 2: green run -> "no changes" --------------------------------------

def test_green_run_then_no_changes(tmp_path, monkeypatch, capsys):
    init_git_repo(tmp_path, branch="main", files={})
    _standalone_cmd(tmp_path, monkeypatch)
    calls = _capture(monkeypatch, [])

    assert main(()) == 0
    assert len(calls) == 1

    assert main(()) == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert ("no changes since last green run — nothing to test · "
            "ptest --full runs everything") in err


# --- twin 3: failing test file rerun until green ----------------------------

def test_failing_test_file_rerun_until_green(tmp_path, monkeypatch, capsys):
    init_git_repo(tmp_path, branch="main", files={})
    _pytest_project(tmp_path, monkeypatch)
    git_commit_all(tmp_path, message="add project")
    (tmp_path / "src" / "greet.py").write_text(
        "def hello():\n    return 'bye'\n", encoding="utf-8")
    # First outcome fails, everything after passes.
    calls = _capture(monkeypatch, [(C.Status.FAILED, 1)])

    assert main(()) == 1
    assert len(calls) == 1
    assert calls[0].argv == ("tests/test_greet.py",)

    # No new edit: the failed file must still be rerun (pytest --lf style).
    assert main(()) == 0
    assert len(calls) == 2
    assert calls[1].argv == ("tests/test_greet.py",)

    # The point moved and the failure set cleared: nothing left to test.
    assert main(()) == 0
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert "no changes since last green run" in err


# --- twin 4: rebase -> fallback ----------------------------------------------

def test_rebase_falls_back_to_branch_base(tmp_path, monkeypatch, capsys):
    init_git_repo(tmp_path, branch="main", files={})
    _standalone_cmd(tmp_path, monkeypatch)
    calls = _capture(monkeypatch, [])

    assert main(()) == 0
    assert len(calls) == 1

    # Rewrite history: the recorded commit is no longer an ancestor.
    # (The staged content change guarantees a new sha even within one
    # timestamp tick, where a bare amend would be a no-op.)
    (tmp_path / "note.txt").write_text("x\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--amend", "-q", "--no-edit")

    assert main(()) == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "(no green run yet)" in err
    assert "last green run (" not in err


# --- twin 5: --base overrides -------------------------------------------------

def test_base_overrides_green_record(tmp_path, monkeypatch, capsys):
    init_git_repo(tmp_path, branch="main", files={})
    _standalone_cmd(tmp_path, monkeypatch)
    calls = _capture(monkeypatch, [])

    assert main(()) == 0
    assert len(calls) == 1

    (tmp_path / "dirty.py").write_text("X = 1\n", encoding="utf-8")
    assert main(("--changed", "--base", "HEAD")) == 0
    assert len(calls) == 2
    assert calls[1].changed_note.startswith("changed vs HEAD")
    err = capsys.readouterr().err
    assert "last green run" not in err
    assert "(no green run yet)" not in err


# --- twin 6: monorepo children tracked independently --------------------------

def test_monorepo_children_tracked_independently(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    calls = _capture(monkeypatch, [])

    (tmp_path / "api" / "service.py").write_text("A = 1\n", encoding="utf-8")
    assert main(()) == 0
    assert len(calls) == 1
    first_err = capsys.readouterr().err
    assert "web · no changes" in first_err
    assert "api · no changes" not in first_err

    # Revert api to its recorded state, then touch web only: only web runs.
    (tmp_path / "api" / "service.py").unlink()
    (tmp_path / "web" / "app.js").write_text("x\n", encoding="utf-8")
    assert main(()) == 0
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert "api · no changes" in err


def test_monorepo_mixed_green_nothing_changed_makes_no_reference_claim(
        tmp_path, monkeypatch, capsys, monorepo):
    """api has a green record, web does not: the nothing-changed line must not
    claim 'no green run yet' (real persea smoke finding)."""
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    calls = _capture(monkeypatch, [])

    (tmp_path / "api" / "service.py").write_text("A = 1\n", encoding="utf-8")
    assert main(()) == 0          # api runs green, web untouched
    capsys.readouterr()
    assert main(()) == 0          # nothing changed anywhere now
    err = capsys.readouterr().err
    assert len(calls) == 1
    assert "no changes — nothing to test" in err
    assert "no green run yet" not in err


# --- cache unit contracts -----------------------------------------------------

def test_unreadable_record_falls_back(tmp_path):
    domain = tmp_path / "state"
    (domain / "last-green").mkdir(parents=True)
    record_path = lastgreen.record_path(domain, "root", "")
    record_path.write_text("{not json", encoding="utf-8")
    assert lastgreen.load(domain, "root", "") is None


def test_fingerprint_mismatch_marks_changed(tmp_path):
    init_git_repo(tmp_path, branch="main",
                  files={"a.py": "A = 1\n"})
    top = tmp_path
    record = lastgreen.GreenRecord(
        root=str(top), project="", commit=git(tmp_path, "rev-parse", "HEAD"),
        recorded_at=0.0, files={}, last_failed=())
    (tmp_path / "a.py").write_text("A = 2\n", encoding="utf-8")
    assert "a.py" in lastgreen.dirty_vs_fingerprint(top, "", record)


def test_record_round_trip_is_atomic_and_small(tmp_path):
    init_git_repo(tmp_path, branch="main", files={"a.py": "A = 1\n"})
    domain = tmp_path / "state"
    domain.mkdir()
    lastgreen.record_pass(domain, str(tmp_path), "", tmp_path)
    loaded = lastgreen.load(domain, str(tmp_path), "")
    assert loaded is not None and loaded.commit
    raw = lastgreen.record_path(domain, str(tmp_path), "").read_bytes()
    assert len(raw) < 65536
    leftovers = [path for path in domain.rglob("*") if ".tmp." in path.name]
    assert leftovers == []
    payload = json.loads(raw.decode("utf-8"))
    assert payload["project"] == ""


def test_failure_set_merges_and_clears(tmp_path):
    init_git_repo(tmp_path, branch="main", files={})
    domain = tmp_path / "state"
    domain.mkdir()
    lastgreen.record_failure(domain, str(tmp_path), "",
                             ("tests/test_a.py",))
    first = lastgreen.load(domain, str(tmp_path), "")
    assert first is not None and "tests/test_a.py" in first.last_failed
    lastgreen.record_failure(domain, str(tmp_path), "",
                             ("tests/test_b.py",))
    merged = lastgreen.load(domain, str(tmp_path), "")
    assert merged is not None
    assert "tests/test_a.py" in merged.last_failed
    assert "tests/test_b.py" in merged.last_failed
    lastgreen.record_pass(domain, str(tmp_path), "", tmp_path)
    cleared = lastgreen.load(domain, str(tmp_path), "")
    assert cleared is not None and cleared.last_failed == ()
