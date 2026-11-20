"""0.7.4 G1: parallelism is decided after setup (abuse rows A6-A10, A15).

Oracle: spec docs/specs/2026-11-20-ptest-0.7.4-fresh-worktree-visibility.md
goal G1 and abuse rows A6-A10/A15. A fresh worktree has no xdist or
pytest-cov dist-info until setup installs it, so the first run used to ask
for one slot and print ``serial: ... until setup installs it``.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from ptest import contracts as C, config as config_api, operations

SITE = "vv/lib/python3.12/site-packages"

# Runs as the declared setup: appends to setup.log, creates the dist-infos
# named in argv[1:], and may interrupt the ptest process (argv marker).
_SETUP_SCRIPT = """\
import os, signal, sys
from pathlib import Path
Path("setup.log").open("a").write("setup\\n")
interrupt = Path("interrupt")
if interrupt.exists():
    pid = int(interrupt.read_text())
    interrupt.unlink()
    os.kill(pid, signal.SIGINT)
    import time
    time.sleep(30)
if Path("fail-setup").exists():
    sys.exit(3)
site = Path("%s")
site.mkdir(parents=True, exist_ok=True)
for name in sys.argv[1:]:
    (site / (name + ".dist-info")).mkdir(exist_ok=True)
""" % SITE


def _project(case, domain, *, addopts="-n 4", dists=("pytest_xdist-3.8.0",)):
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n\n"
        "def test_body():\n"
        "    Path(__file__).parents[1].joinpath('ran.log').open('a').write('x')\n",
        encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"%s\"\n" % addopts,
        encoding="utf-8")
    (root / "vv" / "bin").mkdir(parents=True)
    (root / "vv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    shim = root / "vv" / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    (root / "setup.py").write_text(_SETUP_SCRIPT, encoding="utf-8")
    argv = [sys.executable, "setup.py", *dists]
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([str(shim)])}\n"
        "args = []\nfull_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\n"
        f"argv = {json.dumps(argv)}\n"
        f'required_paths = ["{SITE}"]\n'
        "network = false\nlifecycle_scripts = false\n",
        encoding="utf-8")
    resolved = config_api.resolve_config(root).config
    assert resolved is not None
    return root, resolved


def _reasons(result):
    return [(r.code, r.message) for r in result.reasons]


def _serial_reasons(result):
    return [m for c, m in _reasons(result)
            if c == "parallel-workers" and m.startswith("serial:")]


def _lines(root, name):
    path = root / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = operations.run_setup_only

    def wrapper(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(operations, "run_setup_only", wrapper)
    return calls


def test_first_run_presetup_requests_parallel_slots(case, spy):
    """G1: setup creating the qualified xdist makes the first run parallel."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.status is C.Status.PASSED
    assert result.command.workers == 4
    assert result.granted_workers == 2
    assert _serial_reasons(result) == []
    assert len(spy) == 1
    assert _lines(root, "setup.log") == ["setup"]  # in-guard setup is a no-op
    assert (root / "ran.log").exists()


def test_no_setup_twin_stays_serial_with_todays_text(case, spy):
    """G1 twin: with setup current but xdist absent, nothing pre-setups."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain, dists=())
    first = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))
    assert first.status is C.Status.PASSED
    spy.clear()

    again = operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.SCOPED, no_setup=True))

    assert spy == []
    assert _serial_reasons(again) == [
        "serial: pytest-xdist is not installed in the project environment "
        "yet; ptest runs serially until setup installs it"]
    assert _lines(root, "setup.log") == ["setup"]


def test_no_setup_with_setup_required_never_runs_setup(case, spy):
    """A9: --no-setup never triggers the pre-run setup, even when owed."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain)

    with pytest.raises(C.Problem):
        operations.execute(
            domain, config, C.RunRequest(mode=C.Mode.SCOPED, no_setup=True))

    assert spy == []
    assert _lines(root, "setup.log") == []


@pytest.mark.parametrize("addopts", [
    "-n 4 --dist=each", "-n 1", "-n 4 --maxprocesses=2", "-n 0"])
def test_config_reasons_never_presetup(case, spy, addopts):
    """A9: config reasons (-n 1, -n 0, --dist each, --maxprocesses) never do."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain, addopts=addopts)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.granted_workers == 1
    assert spy == []
    assert _lines(root, "setup.log") == ["setup"]  # the in-guard setup only


def test_presetup_failure_ends_with_setup_failed(case, spy, capsys):
    """A10: a failed pre-run setup never runs the tests."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain)
    (root / "fail-setup").write_text("1", encoding="utf-8")

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.status is not C.Status.PASSED
    assert result.exit_code != 0
    assert "setup failed" in capsys.readouterr().err
    assert _lines(root, "ran.log") == []
    assert _lines(root, "setup.log") == ["setup"]
    assert operations._required_setup_state(
        config, operations._checkout(config), domain) is not None


def test_cancel_during_presetup_records_nothing(case, spy):
    """A7: cancel while pre-setup runs: 130, no fingerprint, no held slot."""
    domain = case.domain(slots=1, jobs=1)
    root, config = _project(case, domain)
    (root / "interrupt").write_text(str(os.getpid()), encoding="utf-8")
    previous = signal.getsignal(signal.SIGINT)
    try:
        result = operations.execute(
            domain, config, C.RunRequest(mode=C.Mode.SCOPED))
    finally:
        signal.signal(signal.SIGINT, previous)

    assert result.exit_code == 130
    assert _lines(root, "ran.log") == []
    assert operations._stored_setup_fingerprint(
        domain, operations._checkout(config)) is None
    # The single slot is free again: a follow-up run is granted at once.
    follow = operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.SCOPED, queue_timeout_s=5))
    assert follow.status is C.Status.PASSED


@pytest.mark.parametrize("dists", [
    ("pytest_xdist-2.0.0",),
    ("pytest_xdist-3.8.0", "pytest_xdist-3.7.0")])
def test_presetup_unqualified_xdist_stays_serial_with_reason(case, spy, dists):
    """A8: setup installing an unqualified or duplicate xdist stays serial."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain, dists=dists)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert len(spy) == 1
    assert result.granted_workers == 1
    assert len(_serial_reasons(result)) == 1
    assert result.status is C.Status.PASSED


def test_presetup_cov_pair_enables_parallel(case, spy):
    """G1 for the pytest-cov/coverage pair under --cov."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(
        case, domain, addopts="-n 4 --cov",
        dists=("pytest_xdist-3.8.0", "pytest_cov-6.0.0", "coverage-7.6.0"))

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert len(spy) == 1
    assert result.command.workers == 4
    assert _serial_reasons(result) == []


def test_presetup_unsupported_cov_pair_stays_serial(case, spy):
    """A8 for the cov pair: an unsupported pair after setup stays serial."""
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(
        case, domain, addopts="-n 4 --cov",
        dists=("pytest_xdist-3.8.0", "pytest_cov-2.0.0", "coverage-7.6.0"))

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert len(spy) == 1
    assert result.granted_workers == 1
    assert len(_serial_reasons(result)) == 1


def test_concurrent_first_runs_set_up_once(case, monkeypatch):
    """A6: a pre-setup that finds setup current after admission is a no-op.

    The race is injected deterministically: when this run's admission is
    granted, "another run" has just finished the same setup.
    """
    domain = case.domain(slots=2, jobs=2)
    root, config = _project(case, domain)
    checkout = operations._checkout(config)
    real_poll = operations._poll_with_retry
    state = {"done": False}

    def poll(dom, ticket, admission):
        outcome = real_poll(dom, ticket, admission)
        if outcome.state is C.LeaseState.GRANTED and not state["done"]:
            state["done"] = True
            site = root / SITE
            site.mkdir(parents=True)
            (site / "pytest_xdist-3.8.0.dist-info").mkdir()
            operations._record_setup_fingerprint(
                domain, checkout, operations._setup_fingerprint(config, checkout))
        return outcome

    monkeypatch.setattr(operations, "_poll_with_retry", poll)

    assert operations.run_setup_only(domain, config, queue_timeout_s=5) is None
    assert _lines(root, "setup.log") == []
    # And the slot was released: another exclusive admission is immediate.
    monkeypatch.undo()
    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))
    assert result.status is C.Status.PASSED
    assert _lines(root, "setup.log") == []
