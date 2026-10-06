"""Post-test-stall end to end: serial/xdist stall, deadline dumps, Ctrl-C, healthy.

Readiness-gated on the T1 bridge marker and the T3 guard stall watcher;
skips until both integrate, then must pass with zero skips.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ptest import config as config_api, contracts as C, operations

_WATCHDOG_S = 60

_HANG_TEARDOWN_CONFTEST = """\
import threading

import pytest


@pytest.fixture(scope="session", autouse=True)
def _hang_in_teardown():
    yield
    threading.Event().wait()
"""

_SLOW_TEARDOWN_CONFTEST = """\
import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def _slow_teardown():
    yield
    time.sleep(3)
"""

_PASS_SUITE = "def test_ok():\n    pass\n"

_BLOCK_SUITE = """\
import threading


def test_block():
    from pathlib import Path
    Path("started").touch()
    threading.Event().wait()
"""


def _project(case, domain, *, suite=_PASS_SUITE, conftest="",
             args=("-s",), workers=8, addopts=None):
    root = case.project(domain, kind="pytest")
    config_path = root / ".ptest.toml"
    project_id = tomllib.loads(config_path.read_text())["project_id"]
    config_path.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps([sys.executable])}\n'
        f'args = {json.dumps(list(args))}\nfull_args = ["--invalid-full-only"]\n'
        'test_roots = ["tests"]\n'
        f"workers = {workers}\n"
    )
    if addopts is not None:
        # The parallel tier is qualified by the project's own pytest
        # config (executability.parallel_request reads ini addopts);
        # ptest [runner] args must never carry -n (reject_unowned_controls).
        (root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '" + addopts + "'\n")
    (root / "tests").mkdir()
    (root / "tests/test_native.py").write_text(suite)
    if conftest:
        (root / "tests/conftest.py").write_text(conftest)
    return root


def _fast_guard(monkeypatch):
    # Real guard plus real pytest startup: compress the stall poll cadence
    # so the 1 s window fires promptly without wall-clock flake.
    monkeypatch.setattr(
        operations, "_GUARD_SCRIPT",
        "import ptest.guard as g; g._STALL_POLL_S = 0.2; g._DUMP_WAIT_S = 0.3; "
        + operations._GUARD_SCRIPT)
    monkeypatch.setattr(operations, "_stall_timeout_s", lambda _config: 1.0)


def _execute(root, domain, workers=None):
    config = config_api.resolve_config(root).config
    assert config is not None
    return operations.execute(
        domain, config,
        C.RunRequest(mode=C.Mode.SCOPED, argv=("tests",), workers=workers))


def _stall_leftovers(domain):
    return [path for path in (domain.root / "checkouts").glob("*/reports/*")
            if path.name.endswith(".done") or ".stack-" in path.name]


def _wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "fixture condition did not arrive"
        time.sleep(0.01)


def test_serial_stall_is_incomplete_70_with_controller_dump(case, monkeypatch, capsys):
    # Real guard plus real pytest startup: the session-fixture teardown hang
    # must end 70 with a dump naming the blocking fixture.
    domain = case.domain()
    root = _project(case, domain, conftest=_HANG_TEARDOWN_CONFTEST)
    _fast_guard(monkeypatch)
    result = _execute(root, domain)
    err = capsys.readouterr().err
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert result.exit_origin == "ptest"
    assert result.full_gate_eligible is False
    stall = [reason for reason in result.reasons
             if reason.code == "post-test-stall"]
    assert len(stall) == 1
    assert stall[0].message.endswith(
        "; stack dumps above; rerun once alone, report a repeat")
    assert "ptest: stack dumps (" in err
    # The run-plan line is always stderr line 0 (see test_changed_explain),
    # so the dump header is matched anywhere, with its exact reason suffix.
    assert any(line.endswith("— post-test-stall") for line in err.splitlines())
    assert "_hang_in_teardown" in err
    assert _stall_leftovers(domain) == []
    payload = json.dumps(C.serialize_run_result(result))
    assert "_hang_in_teardown" not in payload


def test_xdist_stall_includes_worker_stacks(case, monkeypatch, capsys):
    # Real guard plus real pytest/xdist startup with two workers.
    import xdist  # noqa: F401  (the xdist variant needs the real plugin)
    domain = case.domain(slots=2)
    # Parallelism is qualified by the project's own pytest addopts; a -n
    # in ptest [runner] args is rejected (reject_unowned_controls).
    root = _project(case, domain, conftest=_HANG_TEARDOWN_CONFTEST,
                    args=("-s",), workers=2, addopts="-n 2")
    _fast_guard(monkeypatch)
    result = _execute(root, domain, workers=2)
    err = capsys.readouterr().err
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70)
    assert "ptest: stack dumps (3 processes) — post-test-stall" in err
    assert "role=worker" in err
    assert _stall_leftovers(domain) == []


def test_compound_deadline_kill_prints_dumps(case, monkeypatch, capsys):
    # Real guard plus real pytest startup: a blocked test call trips the
    # compound deadline, which still prints dumps with the timeout reason.
    domain = case.domain()
    root = _project(case, domain, suite=_BLOCK_SUITE)
    monkeypatch.setattr(operations, "resolve_compound_timeout",
                        lambda *args, **kwargs: (3.0, "cli"))
    result = _execute(root, domain)
    err = capsys.readouterr().err
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70)
    assert any(reason.code == "execution-timeout" for reason in result.reasons)
    assert "ptest: stack dumps (" in err
    # Same stderr ordering note as the serial stall test: the plan line is
    # first, so the dump header is matched anywhere, with its reason suffix.
    assert any(line.endswith("— execution-timeout") for line in err.splitlines())
    assert "test_block" in err
    assert _stall_leftovers(domain) == []


def test_ctrl_c_prints_no_dumps(case, capsys):
    # Real guard plus real pytest startup: a user interrupt keeps cancel
    # semantics and prints no dump lines.
    domain = case.domain()
    root = _project(case, domain, suite=_BLOCK_SUITE)

    def cancel_when_started():
        _wait_for(lambda: (root / "started").exists())
        os.kill(os.getpid(), signal.SIGINT)

    with ThreadPoolExecutor(max_workers=1) as pool:
        cancel = pool.submit(cancel_when_started)
        result = _execute(root, domain)
        cancel.result(timeout=_WATCHDOG_S)
    assert "ptest: stack dumps" not in capsys.readouterr().err
    # Cancel mapping is unchanged by the stall feature: the runner's own
    # exit wins over an observed user cancel once the bridge certified a
    # terminal report, so the interrupted call reads FAILED/exit-2/runner
    # (the guard still forwards SIGINT for a fast kill, with no dump).
    assert (result.status, result.exit_code, result.exit_origin) == (
        C.Status.FAILED, 2, "runner")
    assert _stall_leftovers(domain) == []


def test_healthy_run_prints_no_dumps_and_cleans_up(case, capsys):
    # Real guard plus real pytest startup: a passing run is byte-quiet about
    # dumps and leaves no marker or dump files behind.
    domain = case.domain()
    root = _project(case, domain)
    result = _execute(root, domain)
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    assert "stack dump" not in capsys.readouterr().err
    assert _stall_leftovers(domain) == []


def test_disabled_stall_timeout_survives_slow_teardown(case, monkeypatch, capsys):
    # Real guard plus real pytest startup: with stall detection disabled a
    # 3 s teardown (past the 1 s e2e window) still passes with no stall kill.
    domain = case.domain()
    root = _project(case, domain, conftest=_SLOW_TEARDOWN_CONFTEST)
    monkeypatch.setattr(operations, "_stall_timeout_s", lambda _config: None)
    result = _execute(root, domain)
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    assert not any("stall" in reason.code for reason in result.reasons)
    assert "stack dump" not in capsys.readouterr().err
    assert _stall_leftovers(domain) == []
