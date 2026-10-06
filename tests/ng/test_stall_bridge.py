"""Bridge stall marker and SIGWINCH dump registration (post-test-stall T1).

Unit tests exercise the controller-only arm helper (``_StallArm``), the
marker/dump path rules and the silent-skip refusal paths in process.
Subprocess twins run the real bridge file (``__main__`` controller, or
``-p pytest_bridge`` xdist workers) against real pytest and assert the
marker timing, the faulthandler dump contents and the no-behaviour-change
guarantees (N6/N10/N11/D11).

Every twin carries an explicit timeout of at most 60 s. File polls use a
watchdog deadline; no test synchronises on a bare sleep.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time

from pathlib import Path
from types import SimpleNamespace

import pytest

from ptest.runtime import pytest_bridge as bridge

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "src" / "ptest" / "runtime" / "pytest_bridge.py"
PROTOCOL = REPO_ROOT / "src" / "ptest" / "runtime" / "protocol-v1.json"

_SCRUB = (
    "PYTEST_ADDOPTS", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PTEST_EXECUTION", "PTEST_RUN_ID", "PTEST_GRANT_NONCE",
    "PTEST_PYTEST_REPORT_PATH", "PTEST_PYTEST_ATTEMPT",
    "PTEST_PYTEST_EXECUTION", "PTEST_PYTEST_CHECKOUT_ROOT",
    "PTEST_PYTEST_CONFIG_PATH", "PTEST_GRANT_WORKERS", "PTEST_WORKER_ID",
    "PTEST_RESOURCE_PREFIX", "PTEST_CHECKOUT_ID", "PTEST_PYTEST_PROFILE",
    "PTEST_PYTEST_SELECTED_FILES", "PTEST_TEST_ROOTS",
    "PTEST_PARALLEL_MARKERS", "T5_WORKER_MARKERS",
    "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID",
    "PYTEST_XDIST_WORKER_COUNT",
)

_REFUSAL_PREFIX = "ptest-bridge-refusal: "


def _hex(n: int) -> str:
    return secrets.token_hex(n // 2)


def _report(name: str, when: str, outcome: str = "",
            failed: bool = False, skipped: bool = False):
    """A minimal pytest-report stand-in (nodeid/when/outcome/failed/skipped)."""
    return SimpleNamespace(nodeid=name, when=when, outcome=outcome,
                           failed=failed, skipped=skipped)


def _arm_in(directory: Path, name: str = "native-a001-"
             "0123456789abcdef0123456789abcdef.json"):
    """A fresh controller-only arm bound to a report path in ``directory``."""
    return bridge._StallArm(directory / name)


# ---------------------------------------------------------------------------
# Frozen literals (design D2): the bridge duplicates the contracts values so
# the bridge file stays dependency-light (no ptest imports at runtime).
# ---------------------------------------------------------------------------

def test_stall_constants_have_frozen_values():
    assert bridge._STALL_MARKER_SUFFIX == ".done"
    assert bridge._STACK_DUMP_INFIX == ".stack-"
    assert bridge._STACK_DUMP_HEADER_PREFIX == "ptest stack dump: "


def test_stall_constants_match_contracts():
    """Pin bridge literals to contracts.py once the shared barrier lands.

    The contracts names are owned by T2; until they exist this asserts the
    frozen literal values above, and afterwards it asserts equality too.
    """
    import ptest.contracts as contracts

    assert bridge._STALL_MARKER_SUFFIX == ".done"
    assert bridge._STACK_DUMP_INFIX == ".stack-"
    assert bridge._STACK_DUMP_HEADER_PREFIX == "ptest stack dump: "
    for name in ("STALL_MARKER_SUFFIX", "STACK_DUMP_INFIX",
                 "STACK_DUMP_HEADER_PREFIX"):
        if hasattr(contracts, name):
            assert getattr(bridge, "_" + name) == getattr(contracts, name)


def test_stall_path_rules_match_frozen_formulas(tmp_path):
    report = tmp_path / "native-a001-0123456789abcdef0123456789abcdef.json"
    assert bridge._stall_marker_path(report) == Path(str(report) + ".done")
    pid = os.getpid()
    assert pid > 0
    assert (bridge._stack_dump_path(report, pid)
            == Path(f"{report}.stack-{pid}"))
    with pytest.raises(ValueError):
        bridge._stack_dump_path(report, 0)
    with pytest.raises(ValueError):
        bridge._stack_dump_path(report, -3)


# ---------------------------------------------------------------------------
# Arm condition (a): every expected item has a final outcome.
# ---------------------------------------------------------------------------

def test_arm_after_all_call_outcomes(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1", "a.py::t2", "a.py::t3"])
    arm.observe(_report("a.py::t1", "setup", failed=False))
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.observe(_report("a.py::t2", "setup", failed=False))
    arm.observe(_report("a.py::t2", "call", outcome="failed"))
    arm.observe(_report("a.py::t3", "setup", failed=False))
    arm.observe(_report("a.py::t3", "call", outcome="skipped"))
    marker = tmp_path / (arm._report_name + ".done")
    assert marker.is_file()
    assert marker.stat().st_size == 0
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_setup_failure_counts_as_final(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1", "a.py::t2"])
    arm.observe(_report("a.py::t1", "setup", failed=True))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.observe(_report("a.py::t2", "setup", skipped=True))
    assert (tmp_path / (arm._report_name + ".done")).is_file()


def test_setup_pass_alone_is_not_final(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "setup", failed=False))
    assert not (tmp_path / (arm._report_name + ".done")).exists()


def test_teardown_reports_never_arm(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "setup", failed=False))
    arm.observe(_report("a.py::t1", "teardown", failed=True))
    assert not (tmp_path / (arm._report_name + ".done")).exists()


def test_rerun_outcome_is_not_final(tmp_path):
    """A rerunfailures-style ``rerun`` call report must not count."""
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "call", outcome="rerun"))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    assert (tmp_path / (arm._report_name + ".done")).is_file()


def test_unknown_nodeids_do_not_arm_early(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(_report("other.py::zzz", "call", outcome="passed"))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    assert (tmp_path / (arm._report_name + ".done")).is_file()


# ---------------------------------------------------------------------------
# Arm condition (b): the native loop returned or raised.
# ---------------------------------------------------------------------------

def test_loop_return_arms_with_missing_outcomes(tmp_path):
    """-x / maxfail / Interrupt: items without outcomes still arm via (b)."""
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1", "a.py::t2", "a.py::t3"])
    arm.observe(_report("a.py::t1", "call", outcome="failed"))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.loop_returned()
    assert (tmp_path / (arm._report_name + ".done")).is_file()


def test_empty_expectation_never_arms_through_outcomes(tmp_path):
    """Zero items: outcomes alone never arm; only the loop return does."""
    arm = _arm_in(tmp_path)
    arm.expect([])
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    assert not (tmp_path / (arm._report_name + ".done")).exists()
    arm.loop_returned()
    assert (tmp_path / (arm._report_name + ".done")).is_file()


def test_marker_created_at_most_once(tmp_path):
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    marker = tmp_path / (arm._report_name + ".done")
    first = marker.stat().st_mtime_ns
    arm.loop_returned()
    arm.observe(_report("a.py::t1", "call", outcome="passed"))
    assert marker.stat().st_mtime_ns == first


def test_observe_with_garbage_reports_never_raises(tmp_path):
    """N11: hostile report shapes are swallowed, never raised into pytest."""
    arm = _arm_in(tmp_path)
    arm.expect(["a.py::t1"])
    arm.observe(SimpleNamespace())
    arm.observe(_report("", "call", outcome="passed"))
    arm.observe(_report(None, None, outcome=None))  # type: ignore[arg-type]
    arm.observe("not-a-report")  # type: ignore[arg-type]
    arm.loop_returned()  # still arms: arming itself must keep working
    assert (tmp_path / (arm._report_name + ".done")).is_file()


# ---------------------------------------------------------------------------
# N6/N11: symlinks refused, OSErrors swallowed, runs unaffected.
# ---------------------------------------------------------------------------

def test_symlinked_marker_is_never_written_through(tmp_path):
    """N6: O_NOFOLLOW refuses the link; the link target is untouched."""
    target = tmp_path / "user-data.txt"
    target.write_text("user-data", encoding="utf-8")
    name = "native-a001-0123456789abcdef0123456789abcdef.json"
    link = tmp_path / (name + ".done")
    link.symlink_to(target)
    arm = bridge._StallArm(tmp_path / name)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "call", outcome="passed"))  # must not raise
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "user-data"


def test_unwritable_reports_dir_leaves_run_unaffected(tmp_path):
    """N11: marker creation failure is silent; the suite outcome is intact."""
    missing = tmp_path / "no-such-dir" / "native-a001-aaaaaaaa000000000000000000000000.json"
    arm = bridge._StallArm(missing)
    arm.expect(["a.py::t1"])
    arm.observe(_report("a.py::t1", "call", outcome="passed"))  # must not raise


def test_controller_dump_header_lines_are_exact(tmp_path):
    import faulthandler

    report = tmp_path / "native-a001-0123456789abcdef0123456789abcdef.json"
    try:
        assert bridge._register_stack_dump(report, role="controller") is True
        dump = tmp_path / f"{report.name}.stack-{os.getpid()}"
        first = dump.read_bytes().splitlines(keepends=True)[0]
        assert first == (f"ptest stack dump: role=controller pid={os.getpid()}\n").encode()
    finally:
        faulthandler.unregister(signal.SIGWINCH)


def test_worker_dump_header_names_gateway(tmp_path):
    import faulthandler

    report = tmp_path / "native-a001-0123456789abcdef0123456789abcdef.json"
    try:
        assert bridge._register_stack_dump(report, role="worker", worker_id="gw3") is True
        dump = tmp_path / f"{report.name}.stack-{os.getpid()}"
        first = dump.read_bytes().splitlines(keepends=True)[0]
        assert first == (f"ptest stack dump: role=worker id=gw3 pid={os.getpid()}\n").encode()
    finally:
        faulthandler.unregister(signal.SIGWINCH)


def test_symlinked_dump_path_is_never_written_through(tmp_path):
    """N6: a pre-created symlink at the dump path refuses registration."""
    import faulthandler

    target = tmp_path / "victim.txt"
    target.write_text("untouched", encoding="utf-8")
    report = tmp_path / "native-a001-0123456789abcdef0123456789abcdef.json"
    link = tmp_path / f"{report.name}.stack-{os.getpid()}"
    link.symlink_to(target)
    before = signal.getsignal(signal.SIGWINCH)
    before_enabled = faulthandler.is_enabled()
    assert bridge._register_stack_dump(report, role="controller") is False
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "untouched"
    assert signal.getsignal(signal.SIGWINCH) is before
    assert faulthandler.is_enabled() is before_enabled


def test_dump_registration_failure_skips_silently(tmp_path):
    """N11: an unavailable reports dir means no registration, no raise."""
    report = (tmp_path / "no-such-dir"
              / "native-a001-aaaaaaaa000000000000000000000000.json")
    assert bridge._register_stack_dump(report, role="controller") is False


def test_inprocess_run_registers_no_sigwinch_and_keeps_argv(tmp_path, monkeypatch):
    """D11/N10: in-process ``run()`` never registers SIGWINCH (the outer
    run owns it) and passes argv through untouched."""
    import faulthandler

    import pytest as real_pytest

    reports = tmp_path / "reports"
    reports.mkdir(mode=0o700)
    run_id = "ab" * 16
    report = reports / f"native-a001-{run_id}.json"
    monkeypatch.setenv("PTEST_BRIDGE_PROTOCOL", str(PROTOCOL))
    monkeypatch.setenv("PTEST_GRANT_WORKERS", "1")
    monkeypatch.setenv("PTEST_EXECUTION", "scoped")
    monkeypatch.setenv("PTEST_TEST_ROOTS", "[]")
    monkeypatch.setenv("PTEST_PYTEST_CHECKOUT_ROOT", str(tmp_path))
    monkeypatch.setenv("PTEST_PYTEST_CONFIG_PATH", "")
    monkeypatch.setenv("PTEST_PYTEST_REPORT_PATH", str(report))
    monkeypatch.setenv("PTEST_RUN_ID", run_id)
    monkeypatch.setenv("PTEST_GRANT_NONCE", "cd" * 32)
    monkeypatch.setenv("PTEST_PYTEST_ATTEMPT", "a001")
    monkeypatch.setenv("PTEST_PYTEST_EXECUTION", "scoped")
    seen: list[list[str]] = []

    def _fake_main(argv, plugins=None):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(real_pytest, "main", _fake_main)
    before_handler = signal.getsignal(signal.SIGWINCH)
    before_enabled = faulthandler.is_enabled()
    monkeypatch.chdir(tmp_path)
    assert bridge.run(["tests/test_x.py"]) == 0
    assert seen == [["tests/test_x.py"]]
    assert signal.getsignal(signal.SIGWINCH) is before_handler
    assert faulthandler.is_enabled() is before_enabled
    assert list(reports.glob("*.stack-*")) == []
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["terminal_complete"] is True


# ---------------------------------------------------------------------------
# Subprocess twins: the real bridge file as __main__ (serial controller)
# or -p pytest_bridge (xdist workers), against real pytest.
# ---------------------------------------------------------------------------

class TwinResult:
    def __init__(self, code, stdout, stderr, report, report_path):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.report = report
        self.report_path = report_path
        self.refusals = [
            json.loads(line[len(_REFUSAL_PREFIX):])
            for line in stderr.decode(errors="replace").splitlines()
            if line.startswith(_REFUSAL_PREFIX)
        ]


def _wait_for(path: Path, timeout: float, *, present: bool = True) -> bool:
    """Poll for a path to appear (or disappear) with a watchdog deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() is present:
            return True
        time.sleep(0.05)
    return path.exists() is present


def _run_bridge(root: Path, argv: list[str], *, workers: int = 1,
                extra_env: dict | None = None, timeout: float = 60,
                report_name: str | None = None) -> TwinResult:
    """Run the bridge file directly with a valid executor binding."""
    assert timeout <= 60
    root = root.resolve()
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    os.chmod(reports, 0o700)
    run_id = _hex(32)
    nonce = _hex(64)
    attempt = "a001"
    report_path = reports / (report_name or f"native-{attempt}-{run_id}.json")
    child_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": str(workers),
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_{attempt}_w000",
        "PTEST_EXECUTION": "scoped",
        "PTEST_TEST_ROOTS": "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": nonce,
        "PTEST_PYTEST_ATTEMPT": attempt,
        "PTEST_PYTEST_EXECUTION": "scoped",
    })
    for key, value in (extra_env or {}).items():
        child_env[key] = value
    completed = subprocess.run(
        [sys.executable, str(BRIDGE), *argv],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    report = None
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return TwinResult(completed.returncode, completed.stdout,
                      completed.stderr, report, report_path)


def _write(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_POLL_LOOP = (
    "import time\n"
    "def _wait(path, timeout=30.0):\n"
    "    from pathlib import Path\n"
    "    deadline = time.monotonic() + timeout\n"
    "    while not Path(path).exists():\n"
    "        if time.monotonic() > deadline:\n"
    "            raise RuntimeError('watchdog: ' + path)\n"
    "        time.sleep(0.05)\n"
)


def test_serial_marker_arms_while_session_teardown_blocked(tmp_path):
    """The marker appears once every call outcome is final, even though a
    session-fixture teardown is still blocked (teardown never gates arming)."""
    root = tmp_path / "proj"
    root.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    marker = "MARKER = Path(os.environ['PTEST_PYTEST_REPORT_PATH'] + '.done')"
    _write(root, "conftest.py",
           "import os, time\n"
           "from pathlib import Path\n"
           "import pytest\n"
           f"GATE = Path({str(gate)!r})\n"
           "@pytest.fixture(scope='session', autouse=True)\n"
           "def _stall_gate():\n"
           "    yield\n"
           "    (GATE / 'teardown-entered').write_text('x')\n"
           "    deadline = time.monotonic() + 30\n"
           "    while not (GATE / 'release').exists():\n"
           "        if time.monotonic() > deadline:\n"
           "            raise RuntimeError('watchdog: no release')\n"
           "        time.sleep(0.05)\n")
    _write(root, "tests/test_two.py",
           "import os\n"
           "from pathlib import Path\n"
           + marker + "\n"
           "def test_first():\n"
           "    assert not MARKER.exists()\n"
           "def test_last():\n"
           "    assert not MARKER.exists()\n")
    proc = None
    try:
        reports = root / "reports"
        reports.mkdir(mode=0o700, exist_ok=True)
        run_id, nonce = _hex(32), _hex(64)
        report_path = reports / f"native-a001-{run_id}.json"
        child_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        child_env.update({
            "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
            "PTEST_GRANT_WORKERS": "1",
            "PTEST_WORKER_ID": "w000",
            "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_a001_w000",
            "PTEST_EXECUTION": "scoped",
            "PTEST_TEST_ROOTS": "[]",
            "PTEST_PYTEST_CHECKOUT_ROOT": str(root.resolve()),
            "PTEST_PYTEST_CONFIG_PATH": "",
            "PTEST_PYTEST_REPORT_PATH": str(report_path),
            "PTEST_RUN_ID": run_id,
            "PTEST_GRANT_NONCE": nonce,
            "PTEST_PYTEST_ATTEMPT": "a001",
            "PTEST_PYTEST_EXECUTION": "scoped",
        })
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "-q", "-p", "no:cacheprovider",
             "tests/test_two.py"],
            cwd=str(root), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        marker_path = Path(str(report_path) + ".done")
        # Integration boundary: real bridge + real pytest startup.
        assert _wait_for(gate / "teardown-entered", 45), "teardown never blocked"
        assert _wait_for(marker_path, 15), "marker missing after final outcome"
        assert marker_path.stat().st_size == 0
    finally:
        (gate / "release").write_text("x")
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr.decode()
    assert marker_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["terminal_complete"] is True
    assert payload["native_exit_code"] == 0


def test_no_marker_while_last_call_blocked(tmp_path):
    """N1: no marker exists while the last test is still inside its call."""
    root = tmp_path / "proj"
    root.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    _write(root, "tests/test_blocked.py",
           "import os\n"
           "from pathlib import Path\n"
           + _POLL_LOOP +
           "MARKER = Path(os.environ['PTEST_PYTEST_REPORT_PATH'] + '.done')\n"
           f"GATE = Path({str(gate)!r})\n"
           "def test_first():\n"
           "    assert True\n"
           "def test_blocked_last():\n"
           "    assert not MARKER.exists()\n"
           "    (GATE / 'started').write_text('x')\n"
           f"    _wait(str(GATE / 'go'))\n")
    proc = None
    try:
        reports = root / "reports"
        reports.mkdir(mode=0o700, exist_ok=True)
        run_id, nonce = _hex(32), _hex(64)
        report_path = reports / f"native-a001-{run_id}.json"
        child_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        child_env.update({
            "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
            "PTEST_GRANT_WORKERS": "1",
            "PTEST_WORKER_ID": "w000",
            "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_a001_w000",
            "PTEST_EXECUTION": "scoped",
            "PTEST_TEST_ROOTS": "[]",
            "PTEST_PYTEST_CHECKOUT_ROOT": str(root.resolve()),
            "PTEST_PYTEST_CONFIG_PATH": "",
            "PTEST_PYTEST_REPORT_PATH": str(report_path),
            "PTEST_RUN_ID": run_id,
            "PTEST_GRANT_NONCE": nonce,
            "PTEST_PYTEST_ATTEMPT": "a001",
            "PTEST_PYTEST_EXECUTION": "scoped",
        })
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "-q", "-p", "no:cacheprovider",
             "tests/test_blocked.py"],
            cwd=str(root), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        marker_path = Path(str(report_path) + ".done")
        # Integration boundary: real bridge + real pytest startup.
        assert _wait_for(gate / "started", 45), "blocked test never started"
        for _ in range(20):  # ~1 s of polls: still inside the call
            assert not marker_path.exists(), "marker armed mid-call"
            time.sleep(0.05)
        (gate / "go").write_text("x")
        assert _wait_for(marker_path, 15), "marker missing after release"
    finally:
        (gate / "go").write_text("x") if (gate.exists()
                                          and not (gate / "go").exists()) else None
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr.decode()


def test_dash_x_arms_through_loop_return(tmp_path):
    """-x: items that never ran still arm, via the runtestloop return."""
    root = tmp_path / "proj"
    root.mkdir()
    ran = tmp_path / "ran"
    ran.mkdir()
    _write(root, "tests/test_x.py",
           "import os\n"
           "from pathlib import Path\n"
           f"RAN = Path({str(ran)!r})\n"
           "def test_fails():\n"
           "    assert False\n"
           "def test_never_runs():\n"
           "    (RAN / 'never').write_text('x')\n")
    # Integration boundary: real bridge + real pytest startup.
    twin = _run_bridge(root, ["-x", "-q", "-p", "no:cacheprovider",
                              "tests/test_x.py"], timeout=60)
    assert twin.code == 1, twin.stderr.decode()
    assert not (ran / "never").exists()
    assert Path(str(twin.report_path) + ".done").is_file()
    assert twin.report is not None
    assert twin.report["native_exit_code"] == 1


def test_collection_error_with_zero_items_arms_through_loop(tmp_path):
    """Zero collected items plus a collection error arms only via (b)."""
    root = tmp_path / "proj"
    root.mkdir()
    _write(root, "tests/test_broken.py", "def broken(:\n")
    # Integration boundary: real bridge + real pytest startup.
    twin = _run_bridge(root, ["-q", "-p", "no:cacheprovider",
                              "tests/test_broken.py"], timeout=60)
    assert twin.code != 0
    assert Path(str(twin.report_path) + ".done").is_file()


def test_symlinked_marker_leaves_subprocess_run_unaffected(tmp_path):
    """N6/N11 end to end: a pre-created symlink marker is refused and the
    run's exit code and report are unchanged."""
    root = tmp_path / "proj"
    root.mkdir()
    _write(root, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    run_id = _hex(32)
    target = tmp_path / "user-data.txt"
    target.write_text("user-data", encoding="utf-8")
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    report_path = reports / f"native-a001-{run_id}.json"
    link = Path(str(report_path) + ".done")
    link.symlink_to(target)
    # Integration boundary: real bridge + real pytest startup.
    twin = _run_bridge(root, ["-q", "-p", "no:cacheprovider", "tests/test_ok.py"],
                       timeout=60,
                       report_name=f"native-a001-{run_id}.json")
    assert twin.code == 0, twin.stderr.decode()
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "user-data"
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0


def test_healthy_run_has_no_new_output(tmp_path):
    """N10: a healthy run's stdout/stderr carry no bridge stall lines."""
    root = tmp_path / "proj"
    root.mkdir()
    _write(root, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    # Integration boundary: real bridge + real pytest startup.
    twin = _run_bridge(root, ["-q", "-p", "no:cacheprovider", "tests/test_ok.py"],
                       timeout=60)
    assert twin.code == 0, twin.stderr.decode()
    assert twin.refusals == []
    assert b"stack dump" not in twin.stdout + twin.stderr
    assert twin.report is not None and twin.report["problem"] is None


def test_xdist_sigwinch_writes_controller_and_worker_dumps(tmp_path):
    """Real xdist: SIGWINCH to the controller and each worker appends
    all-thread stacks to their own dump files, naming the blocking test."""
    pytest.importorskip("xdist")
    root = tmp_path / "proj"
    root.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    _write(root, "tests/test_stall.py",
           "import os\n"
           "from pathlib import Path\n"
           + _POLL_LOOP +
           f"GATE = Path({str(gate)!r})\n"
           # NOTE: the blocked test is last. xdist --dist load deals
           # collected tests round-robin, so a leading blocked test would
           # park later tests in its own worker queue behind it.
           "def test_quick_a():\n"
           f"    (GATE / 'quick-a').write_text('x')\n"
           "def test_quick_b():\n"
           f"    (GATE / 'quick-b').write_text('x')\n"
           "def test_stall_blocked_call():\n"
           "    (GATE / 'started').write_text('x')\n"
           f"    _wait(str(GATE / 'go'))\n")
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    run_id, nonce = _hex(32), _hex(64)
    report_path = reports / f"native-a001-{run_id}.json"
    child_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": "2",
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_a001_w000",
        "PTEST_EXECUTION": "scoped",
        "PTEST_TEST_ROOTS": "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root.resolve()),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": nonce,
        "PTEST_PYTEST_ATTEMPT": "a001",
        "PTEST_PYTEST_EXECUTION": "scoped",
    })
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "-n", "2", "--dist", "load",
         "-q", "-p", "no:cacheprovider", "tests/test_stall.py"],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Integration boundary: real bridge + real xdist startup.
        assert _wait_for(gate / "started", 45), "blocked test never started"
        import psutil

        controller = proc.pid
        workers = [child.pid for child in
                   psutil.Process(controller).children(recursive=True)]
        assert workers, "no xdist workers observed"
        for pid in [controller, *workers]:
            os.kill(pid, signal.SIGWINCH)
        deadline = time.monotonic() + 15
        dumps: dict = {}
        while time.monotonic() < deadline:
            dumps = {path: path.read_bytes()
                     for path in reports.glob(f"{report_path.name}.stack-*")}
            if (len(dumps) >= 3
                    and all(len(data.splitlines()) > 1 for data in dumps.values())):
                break
            time.sleep(0.1)
        assert len(dumps) >= 3, f"expected controller + 2 worker dumps: {sorted(str(p) for p in dumps)}"
        by_role: dict[bytes, list[bytes]] = {}
        for path, data in dumps.items():
            lines = data.splitlines(keepends=True)
            assert lines[0].startswith(b"ptest stack dump: role=")
            role = lines[0].split(b"role=")[1].split()[0]
            by_role.setdefault(role, []).append(data)
        # The controller names itself; each worker names its gateway.
        assert b"controller" in by_role, sorted(by_role)
        assert len(by_role.get(b"worker", [])) >= 2
        # The worker executing the blocked test names it. An idle worker
        # legitimately shows its xdist serve loop instead (faulthandler
        # dumps all threads truthfully), so only one worker must name it.
        assert any(b"test_stall_blocked_call" in data
                   for data in by_role[b"worker"])
        # xdist arm path (a): with the blocked call still running, the two
        # forwarded quick outcomes are not enough, so no marker exists yet.
        marker_path = Path(str(report_path) + ".done")
        assert _wait_for(gate / "quick-a", 20), "quick test A never ran"
        assert _wait_for(gate / "quick-b", 20), "quick test B never ran"
        assert not marker_path.exists(), "controller armed before all outcomes"
        (gate / "go").write_text("x")
        assert _wait_for(marker_path, 15), "marker missing after last outcome"
        assert marker_path.stat().st_size == 0
    finally:
        (gate / "go").write_text("x")
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == 0, stderr.decode()
    assert marker_path.exists()


def test_xdist_worker_crash_arms_only_through_loop_return(tmp_path):
    """A SIGKILLed worker leaves items without outcomes: no arm happens
    while the run is in flight; the marker appears only when the native
    loop returns (condition (b))."""
    pytest.importorskip("xdist")
    root = tmp_path / "proj"
    root.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    _write(root, "tests/test_crash.py",
           "import os\n"
           "from pathlib import Path\n"
           + _POLL_LOOP +
           f"GATE = Path({str(gate)!r})\n"
           # NOTE: the victim is last (see the round-robin note above), so
           # the quick tests always finish while the victim is blocked.
           "def test_quick_a():\n"
           f"    (GATE / 'quick-a').write_text('x')\n"
           "def test_quick_b():\n"
           f"    (GATE / 'quick-b').write_text('x')\n"
           "def test_crash_victim():\n"
           "    (GATE / 'started').write_text(str(os.getpid()))\n"
           f"    _wait(str(GATE / 'go'))\n")
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    run_id, nonce = _hex(32), _hex(64)
    report_path = reports / f"native-a001-{run_id}.json"
    child_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": "2",
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_a001_w000",
        "PTEST_EXECUTION": "scoped",
        "PTEST_TEST_ROOTS": "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root.resolve()),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": nonce,
        "PTEST_PYTEST_ATTEMPT": "a001",
        "PTEST_PYTEST_EXECUTION": "scoped",
    })
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "-n", "2", "--dist", "load",
         "-q", "-p", "no:cacheprovider", "tests/test_crash.py"],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Integration boundary: real bridge + real xdist startup.
        assert _wait_for(gate / "started", 45), "victim test never started"
        assert _wait_for(gate / "quick-a", 20), "quick test A never ran"
        assert _wait_for(gate / "quick-b", 20), "quick test B never ran"
        marker_path = Path(str(report_path) + ".done")
        assert not marker_path.exists(), "armed with outcomes still missing"
        victim = int((gate / "started").read_text(encoding="utf-8").strip())
        assert victim > 0
        os.kill(victim, signal.SIGKILL)
        assert _wait_for(marker_path, 20), "no arm after the loop returned"
    finally:
        (gate / "go").write_text("x")
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode != 0
    assert marker_path.exists()

