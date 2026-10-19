"""Ambient fingerprint command runner (R41-R43, R48). Real subprocesses only."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from ptest import ambient_fingerprint as A, contracts as C

KEY = b"k" * 32
ENV = {"PATH": os.environ.get("PATH", ""), "PTEST_FP": "yes"}


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _run(argv, tmp_path, **kwargs):
    kwargs.setdefault("env", ENV)
    kwargs.setdefault("key", KEY)
    return A.compute(argv, cwd=tmp_path, **kwargs)


def _gone(pid: int, wait_s: float = 5.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.02)
    return False


def _wait_file(path: Path, wait_s: float = 5.0) -> str:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{path} never written")


def test_digest_is_hmac_of_stdout_and_outcome_holds_no_output(tmp_path):
    # Oracle: R42 -- digest = HMAC-SHA256(domain key, stdout); stdout is never kept.
    secret = "SECRET-FINGERPRINT-BODY"
    out = _run(_py(f"import sys; sys.stdout.write({secret!r})"), tmp_path)
    assert out.digest == hmac.new(
        KEY, secret.encode(), hashlib.sha256).hexdigest()
    assert out.reason is None
    assert [f.name for f in dataclasses.fields(out)] == [
        "digest", "reason", "stderr_tail"]
    assert secret not in repr(out)
    assert hashlib.sha256(secret.encode()).hexdigest() != out.digest


def test_fingerprint_output_never_printed_or_stored(tmp_path, capfd):
    # Oracle: R43 -- stdout of the command is never echoed or retained.
    out = _run(_py("print('LEAKME-STDOUT')"), tmp_path)
    seen = capfd.readouterr()
    assert "LEAKME" not in seen.out + seen.err
    assert "LEAKME" not in repr(out)
    assert out.stderr_tail == ()


def test_command_runs_without_shell_with_given_cwd_env_and_closed_stdin(
        tmp_path):
    # Oracle: R41 -- literal argv, no shell, cwd = project root, stdin /dev/null.
    code = ("import os, sys; "
            "sys.stdout.write(repr((sys.argv[1], os.getcwd(), "
            "os.environ.get('PTEST_FP'), os.environ.get('HOME'), "
            "sys.stdin.read())))")
    out = _run(_py(code) + ("$HOME;`x`",), tmp_path)
    expected = repr(("$HOME;`x`", str(tmp_path.resolve()), "yes", None, ""))
    assert out.digest == hmac.new(
        KEY, expected.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("argv,reason", [
    (_py("raise SystemExit(3)"), "exit 3"),
    (("definitely-not-a-ptest-command",), "not found"),
    ((), "could not start"),
])
def test_fingerprint_failures_are_changed_never_fatal(tmp_path, argv, reason):
    # Oracle: R43 -- every failure is an absent digest with a reason, never an
    # exception; the caller treats absent as "changed".
    out = _run(argv, tmp_path)
    assert out.digest is None
    assert out.reason == reason


def test_unusable_cwd_is_could_not_start(tmp_path):
    out = A.compute(_py("pass"), cwd=tmp_path / "missing", env=ENV, key=KEY)
    assert out.digest is None
    assert out.reason in {"not found", "could not start"}


def test_failure_keeps_last_stderr_lines_only(tmp_path):
    code = ("import sys\n"
            "for i in range(30): sys.stderr.write(f'line {i}\\n')\n"
            "raise SystemExit(1)")
    out = _run(_py(code), tmp_path)
    assert out.reason == "exit 1"
    assert out.stderr_tail == tuple(
        f"line {i}" for i in range(30 - C.AMBIENT_FINGERPRINT_STDERR_LINES, 30))


def test_stderr_is_bounded_and_does_not_block_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "AMBIENT_FINGERPRINT_STDERR_MAX_BYTES", 1024)
    code = ("import sys; sys.stderr.write('e' * 2_000_000); "
            "sys.stderr.write('\\nlast'); raise SystemExit(2)")
    out = _run(_py(code), tmp_path, timeout_s=20)
    assert out.reason == "exit 2" and out.digest is None
    assert out.stderr_tail[-1] == "last"
    assert sum(len(line) for line in out.stderr_tail) <= 1024


def test_stdout_over_cap_is_absent_and_stops_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "AMBIENT_FINGERPRINT_STDOUT_MAX_BYTES", 4096)
    pidfile = tmp_path / "pid"
    code = (f"import os, sys, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
            "sys.stdout.write('x' * 100000); sys.stdout.flush(); time.sleep(60)")
    out = _run(_py(code), tmp_path, timeout_s=20)
    assert out.digest is None
    assert out.reason == "output over 16 MiB"
    assert _gone(int(_wait_file(pidfile)))


def test_stdout_exactly_at_cap_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "AMBIENT_FINGERPRINT_STDOUT_MAX_BYTES", 4096)
    out = _run(_py("import sys; sys.stdout.write('x' * 4096)"), tmp_path)
    assert out.reason is None and out.digest is not None


def _sleeper(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")
    return _py(code), pidfile


def test_fingerprint_timeout_and_sigint_kill_session(tmp_path):
    # Oracle: R41/R48 -- a timeout or a cancellation SIGKILLs the command's whole
    # session, grandchildren included, and returns promptly.
    argv, pidfile = _sleeper(tmp_path)
    started = time.monotonic()
    timed = _run(argv, tmp_path, timeout_s=1.0, poll_s=0.02)
    assert time.monotonic() - started < 15
    assert timed.digest is None
    assert timed.reason == "timed out after 1 s"
    assert _gone(int(_wait_file(pidfile)))

    sub = tmp_path / "second"
    sub.mkdir()
    argv, pidfile = _sleeper(sub)
    flag = {"stop": False}

    def cancelled() -> bool:
        if (sub / "child.pid").exists() and (sub / "child.pid").read_text():
            flag["stop"] = True
        return flag["stop"]

    cancelled_out = A.compute(argv, cwd=sub, env=ENV, key=KEY, timeout_s=30,
                              cancelled=cancelled, poll_s=0.02)
    assert cancelled_out.digest is None
    assert cancelled_out.reason == "cancelled"
    assert _gone(int(_wait_file(pidfile)))


def test_orphan_left_after_a_clean_exit_is_killed_too(tmp_path):
    # Oracle: R48 -- no process of the command's session outlives compute().
    pidfile = tmp_path / "orphan.pid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n")
    out = _run(_py(code), tmp_path, timeout_s=20)
    assert out.reason is None and out.digest is not None
    assert _gone(int(_wait_file(pidfile)))


def test_command_runs_in_its_own_session(tmp_path):
    code = "import os, sys; sys.stdout.write(str(os.getsid(0) == os.getpid()))"
    out = _run(_py(code), tmp_path)
    assert out.digest == hmac.new(KEY, b"True", hashlib.sha256).hexdigest()


def test_default_timeout_is_the_contract_constant():
    import inspect
    sig = inspect.signature(A.compute)
    assert sig.parameters["timeout_s"].default == C.AMBIENT_FINGERPRINT_TIMEOUT_S
    assert C.AMBIENT_FINGERPRINT_TIMEOUT_S == 60.0
    assert C.MAX_ATTEMPT_DECISION_TIMEOUT_S == 90.0
