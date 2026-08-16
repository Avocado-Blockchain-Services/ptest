"""`ptest status` — situational awareness for whoever is about to run tests.

Several agents share this machine, and the failure mode ptest exists to prevent
is invisible from inside any one session: your run is polite, load average is
40, and nothing tells you why. This surfaces the missing half — who else is
running, what is already off-box, and what is about to go wrong.
"""

import json
import os
import time

import pytest


def _proc(root, pid, cmdline, ppid=1):
    """A fake /proc entry: NUL-separated cmdline plus a PPid line."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")
    (d / "status").write_text(f"Name:\t{cmdline[0]}\nPid:\t{pid}\nPPid:\t{ppid}\n")
    return d


# ── the registry of in-flight runs ──────────────────────────────────────────

def test_a_registered_run_is_visible_to_other_sessions(ptest, tmp_path, monkeypatch):
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "active")
    with ptest.registered_run("persea-api", tmp_path, "uv run pytest tests/api", "local"):
        runs = ptest.active_runs()
    assert len(runs) == 1
    assert runs[0]["project"] == "persea-api"
    assert runs[0]["where"] == "local"
    assert runs[0]["pid"] == os.getpid()


def test_the_registration_is_removed_when_the_run_ends(ptest, tmp_path, monkeypatch):
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "active")
    with ptest.registered_run("p", tmp_path, "cmd", "local"):
        pass
    assert ptest.active_runs() == []


def test_a_crashed_run_does_not_haunt_the_registry(ptest, tmp_path, monkeypatch):
    """A killed ptest never runs its finally block; the reader prunes instead."""
    active = tmp_path / "active"
    monkeypatch.setattr(ptest, "ACTIVE", active)
    active.mkdir(parents=True)
    dead = 999_999_999
    (active / f"{dead}.json").write_text(json.dumps(
        {"pid": dead, "project": "ghost", "root": "/x", "cmd": "c",
         "where": "local", "started": time.time()}))

    assert ptest.active_runs() == []
    assert not (active / f"{dead}.json").exists(), "the stale record is cleaned up"


def test_a_corrupt_record_is_ignored_not_fatal(ptest, tmp_path, monkeypatch):
    active = tmp_path / "active"
    monkeypatch.setattr(ptest, "ACTIVE", active)
    active.mkdir(parents=True)
    (active / "1.json").write_text("{not json")
    assert ptest.active_runs() == []


def test_registration_never_breaks_a_test_run(ptest, tmp_path, monkeypatch):
    """Bookkeeping is not worth failing a suite over."""
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "nope" / "active")
    monkeypatch.setattr(ptest.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    with ptest.registered_run("p", tmp_path, "cmd", "local"):
        pass  # must not raise


# ── runners nobody registered ───────────────────────────────────────────────

def test_a_bare_pytest_is_reported_as_a_stray(ptest, tmp_path):
    _proc(tmp_path, 4001, ["python", "-m", "pytest", "-n", "16", "tests"])
    strays = ptest.stray_runners(proc=tmp_path, registered=frozenset())
    assert len(strays) == 1
    assert strays[0]["pid"] == 4001
    assert strays[0]["workers"] == 16


def test_a_registered_run_is_not_a_stray(ptest, tmp_path):
    _proc(tmp_path, 4002, ["python", "-m", "pytest", "tests"])
    assert ptest.stray_runners(proc=tmp_path, registered=frozenset({4002})) == []


def test_a_child_of_a_registered_run_is_not_a_stray(ptest, tmp_path):
    """ptest runs the suite through a shell, so the runner is always a grandchild.

    Worktree names and cwd both lie about who started a process; ancestry does
    not. Without this, every legitimate ptest run reports itself as a stray.
    """
    _proc(tmp_path, 5000, ["ptest", "tests/api"])
    _proc(tmp_path, 5001, ["/bin/sh", "-c", "uv run pytest"], ppid=5000)
    _proc(tmp_path, 5002, ["python", "-m", "pytest", "tests"], ppid=5001)
    assert ptest.stray_runners(proc=tmp_path, registered=frozenset({5000})) == []


def test_vitest_and_go_and_cargo_count_too(ptest, tmp_path):
    _proc(tmp_path, 6001, ["node", "/x/node_modules/.bin/vitest", "run"])
    _proc(tmp_path, 6002, ["go", "test", "./..."])
    _proc(tmp_path, 6003, ["cargo", "test"])
    found = {s["pid"] for s in ptest.stray_runners(proc=tmp_path, registered=frozenset())}
    assert found == {6001, 6002, 6003}


def test_ptest_itself_is_not_a_test_runner(ptest, tmp_path):
    _proc(tmp_path, 7001, ["/home/x/.local/bin/ptest", "status"])
    assert ptest.stray_runners(proc=tmp_path, registered=frozenset()) == []


def test_a_vanishing_process_is_survivable(ptest, tmp_path):
    """/proc races: the pid can exit between listdir and read."""
    (tmp_path / "8001").mkdir()          # no cmdline, no status
    assert ptest.stray_runners(proc=tmp_path, registered=frozenset()) == []


def test_a_missing_proc_is_survivable(ptest, tmp_path):
    assert ptest.stray_runners(proc=tmp_path / "absent", registered=frozenset()) == []


# ── config that will fail at run time ───────────────────────────────────────

def test_a_full_command_pointing_at_nothing_is_reported(ptest, tmp_path):
    (tmp_path / "tests").mkdir()
    pcfg = {"full": "uv run pytest visual_stuff/tests"}
    assert ptest.missing_full_paths(pcfg, tmp_path) == ["visual_stuff/tests"]


def test_a_full_command_that_resolves_is_quiet(ptest, tmp_path):
    (tmp_path / "tests").mkdir()
    assert ptest.missing_full_paths({"full": "uv run pytest tests"}, tmp_path) == []


def test_flags_and_their_values_are_not_paths(ptest, tmp_path):
    pcfg = {"full": "uv run pytest -n auto --cov=pkg --cov-fail-under=85 -k slow"}
    assert ptest.missing_full_paths(pcfg, tmp_path) == []


def test_a_full_command_with_no_paths_at_all_is_quiet(ptest, tmp_path):
    """`npm test --` names no path; that is normal, not a broken config."""
    assert ptest.missing_full_paths({"full": "npm run test:coverage --"}, tmp_path) == []


# ── the warnings an agent should read before running anything ───────────────

def test_saturated_machine_warns(ptest):
    warnings = ptest.status_warnings(
        load=14.0, cores=16, runs=[{"project": "a"}, {"project": "b"}],
        strays=[], counts={}, cap=60, missing_paths={})
    assert any("load" in w.lower() for w in warnings)


def test_a_stray_runner_warns_loudest(ptest):
    warnings = ptest.status_warnings(
        load=1.0, cores=16, runs=[], cap=60, counts={}, missing_paths={},
        strays=[{"pid": 9, "workers": 16, "cmd": "pytest -n 16"}])
    assert warnings, "an unregistered runner is exactly what ptest exists to catch"
    assert "9" in warnings[0]


def test_approaching_the_daily_cap_warns(ptest):
    warnings = ptest.status_warnings(
        load=1.0, cores=16, runs=[], strays=[], cap=60,
        counts={"persea-front": 57}, missing_paths={})
    assert any("persea-front" in w and "57" in w for w in warnings)


def test_a_quiet_machine_has_nothing_to_say(ptest):
    assert ptest.status_warnings(load=0.5, cores=16, runs=[], strays=[],
                                 counts={"p": 2}, cap=60, missing_paths={}) == []


def test_a_broken_full_path_warns(ptest):
    warnings = ptest.status_warnings(
        load=0.5, cores=16, runs=[], strays=[], counts={}, cap=60,
        missing_paths={"assets": ["visual_stuff/tests"]})
    assert any("visual_stuff/tests" in w for w in warnings)


# ── the command ─────────────────────────────────────────────────────────────

def test_status_reports_a_quiet_machine(ptest, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(ptest, "stray_runners", lambda **k: [])
    monkeypatch.setattr(ptest, "machine_load", lambda: (0.4, 16))
    monkeypatch.setattr(ptest, "remote_counts", lambda: ("2026-08-16", {}))

    assert ptest.cmd_status({"projects": {}}, tmp_path) == 0

    out = capsys.readouterr().out
    assert "no other ptest runs" in out
    assert "all clear" in out


def test_status_shows_another_agents_run(ptest, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(ptest, "stray_runners", lambda **k: [])
    monkeypatch.setattr(ptest, "machine_load", lambda: (2.0, 16))
    monkeypatch.setattr(ptest, "remote_counts", lambda: ("2026-08-16", {}))

    with ptest.registered_run("persea-api", tmp_path, "uv run pytest tests/api", "local"):
        assert ptest.cmd_status({"projects": {}}, tmp_path) == 0

    out = capsys.readouterr().out
    assert "persea-api" in out
    assert "tests/api" in out


def test_status_exits_nonzero_when_something_is_wrong(ptest, tmp_path, monkeypatch,
                                                      capsys):
    """An agent that only checks the exit code still learns something is off."""
    monkeypatch.setattr(ptest, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(ptest, "machine_load", lambda: (0.4, 16))
    monkeypatch.setattr(ptest, "remote_counts", lambda: ("2026-08-16", {}))
    monkeypatch.setattr(ptest, "stray_runners",
                        lambda **k: [{"pid": 9, "workers": 16, "cmd": "pytest -n 16"}])

    assert ptest.cmd_status({"projects": {}}, tmp_path) == 1
    assert "⚠" in capsys.readouterr().out
