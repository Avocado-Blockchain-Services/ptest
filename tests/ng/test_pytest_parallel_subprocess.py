"""Real-xdist subprocess twins for the parallel pytest bridge (T1).

Each twin runs ``src/ptest/runtime/pytest_bridge.py`` directly as a
subprocess (``sys.executable``, scrubbed env, ``PTEST_BRIDGE_PROTOCOL``,
the adapter-to-bridge env, a private 0700 report dir with a valid report
binding) against real pytest-xdist 3.8.0 from the test environment, then
parses the JSON report and the ``ptest-bridge-refusal`` stderr marker.
Every twin carries an explicit timeout of at most 60 s.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "src" / "ptest" / "runtime" / "pytest_bridge.py"
PROTOCOL = REPO_ROOT / "src" / "ptest" / "runtime" / "protocol-v1.json"
PARALLEL_FIXTURES = Path(__file__).parent / "fixtures" / "pytest" / "parallel"

WORKERS = 4

# Control variables the harness purges so a twin never inherits
# orchestrator state (mirrors the serial twins' scrub list, extended
# with the parallel identity variables the bridge itself manages).
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


class TwinResult(NamedTuple):
    code: int
    stdout: bytes
    stderr: bytes
    report: dict | None
    refusals: list[dict]


def _refusals(stderr: bytes) -> list[dict]:
    found = []
    for line in stderr.decode(errors="replace").splitlines():
        if line.startswith(_REFUSAL_PREFIX):
            try:
                found.append(json.loads(line[len(_REFUSAL_PREFIX):]))
            except ValueError:
                pass
    return found


def _run_bridge(root: Path, argv: list[str], *, execution: str,
                workers: int = WORKERS, extra_env: dict | None = None,
                timeout: float = 60) -> TwinResult:
    """Run the bridge file directly with a valid executor binding."""
    assert timeout <= 60
    root = root.resolve()
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    os.chmod(reports, 0o700)
    markers = root / "markers"
    markers.mkdir(exist_ok=True)
    run_id = _hex(32)
    nonce = _hex(64)
    attempt = "a001"
    report_path = reports / f"native-{attempt}-{run_id}.json"

    child_env = {
        key: value for key, value in os.environ.items()
        if key not in _SCRUB
    }
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": str(workers),
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_{attempt}_w000",
        "PTEST_EXECUTION": execution,
        "PTEST_TEST_ROOTS": '["tests"]' if execution == "full" else "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": nonce,
        "PTEST_PYTEST_ATTEMPT": attempt,
        "PTEST_PYTEST_EXECUTION": execution,
        "PTEST_PARALLEL_MARKERS": str(markers),
    })
    for key, value in (extra_env or {}).items():
        child_env[key] = value

    # Same process group as the caller: a detached session made the
    # scheduler count twin descendants as escaped under a concurrent ptest
    # admission and ended that run incomplete (exit 70).
    completed = subprocess.run(
        [sys.executable, str(BRIDGE), *argv],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    report = None
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return TwinResult(
        code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        report=report,
        refusals=_refusals(completed.stderr),
    )


_IDENTITY_TEST = (
    "import os\n"
    "MARKERS = os.environ.get('PTEST_PARALLEL_MARKERS', '')\n"
    "def _record(name):\n"
    "    with open(os.path.join(MARKERS, 'workers.log'), 'a',"
    " encoding='utf-8') as handle:\n"
    "        handle.write(os.environ.get('PYTEST_XDIST_WORKER', '-') + ':'\n"
    "            + os.environ.get('PTEST_WORKER_ID', '-') + ':'\n"
    "            + os.environ.get('PTEST_RESOURCE_PREFIX', '-') + ':'\n"
    "            + name + '\\n')\n"
)


def _identity_tests(count: int) -> str:
    body = _IDENTITY_TEST
    for index in range(count):
        body += (
            f"\ndef test_identity_{index:02d}():\n"
            f"    _record('identity_{index:02d}')\n"
            "    assert True\n"
        )
    return body


def _worker_lines(root: Path) -> list[tuple[str, str, str, str]]:
    lines = (root / "markers" / "workers.log").read_text(
        encoding="utf-8").splitlines()
    return [tuple(line.split(":")) for line in lines]  # type: ignore[misc]


def test_parallel_pass_four_workers_observed(tmp_path, fake_pytest_project):
    """Twin (a): a 4-worker pass completes with per-worker identity."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(8)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None

    lines = _worker_lines(root)
    assert len(lines) == 8
    xdist_workers = {line[0] for line in lines}
    ptest_workers = {line[1] for line in lines}
    prefixes = {line[2] for line in lines}
    assert xdist_workers == {"gw0", "gw1", "gw2", "gw3"}
    assert ptest_workers == {"w000", "w001", "w002", "w003"}
    assert len(prefixes) == 4
    for xdist_id, ptest_id, prefix, _ in lines:
        number = int(xdist_id[2:])
        assert ptest_id == f"w{number:03d}"
        assert prefix.startswith("pt_") and prefix.endswith(ptest_id)


def test_parallel_fail_reports_native_failure(tmp_path, fake_pytest_project):
    """Twin (b): a 4-worker failure is complete with native-failure."""
    root = fake_pytest_project(
        tests={
        "tests/test_bad.py": (
_IDENTITY_TEST
           + "\ndef test_ok():\n    _record('ok')\n    assert True\n"
           + "\ndef test_broken():\n    _record('broken')\n    assert False\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_bad.py"],
        execution="scoped", timeout=60)

    assert twin.code == 1, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 1
    assert twin.report["problem"] == "native-failure"


def test_parallel_worker_crash_is_never_a_pass(tmp_path, fake_pytest_project):
    """Twin (c): a SIGKILLed worker means incomplete or failed, never 0."""
    root = fake_pytest_project(
        tests={
        "tests/test_crash.py": (
"import os, signal\n"
           + _IDENTITY_TEST
           + "\ndef test_kill_worker():\n"
           "    _record('kill')\n"
           "    os.kill(os.getpid(), signal.SIGKILL)\n"
           + "\ndef test_ok():\n    _record('ok')\n    assert True\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_crash.py"],
        execution="scoped", timeout=60)

    assert twin.code != 0
    assert twin.report is None or (
        twin.report["terminal_complete"] is False
        or twin.report.get("native_exit_code") not in (None, 0))


def test_parallel_loadgroup_keeps_xdist_group_together(tmp_path, fake_pytest_project):
    """Twin (d): loadgroup runs each xdist_group on one worker."""
    body = _IDENTITY_TEST
    for name in ("g1_a", "g1_b"):
        body += (f"\nimport pytest\n@pytest.mark.xdist_group('g1')\n"
                 f"def test_{name}():\n    _record('{name}')\n    assert True\n")
    for name in ("g2_a", "g2_b"):
        body += (f"\nimport pytest\n@pytest.mark.xdist_group('g2')\n"
                 f"def test_{name}():\n    _record('{name}')\n    assert True\n")
    for name in ("plain_a", "plain_b"):
        body += (f"\ndef test_{name}():\n    _record('{name}')\n"
                 "    assert True\n")
    root = fake_pytest_project(tests={"tests/test_groups.py": body}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "loadgroup", "-q", "-p", "no:cacheprovider",
               "tests/test_groups.py"],
        execution="scoped", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    lines = _worker_lines(root)
    assert len(lines) == 6
    by_test = {line[3]: line[1] for line in lines}
    assert by_test["g1_a"] == by_test["g1_b"]
    assert by_test["g2_a"] == by_test["g2_b"]


def test_parallel_ctrl_c_leaves_no_survivors(tmp_path, fake_pytest_project):
    """Twin (e): SIGINT the bridge PID; exit fast, nothing survives.

    The bridge runs in the caller's process group (no new session), so only
    its own PID is signalled. Survivors are proven absent by a
    descendant-held fifo reaching EOF: every worker holds the write end from
    test-module import until death.
    """
    import select

    body = (
        "import os, time\n"
        "MARKERS = os.environ.get('PTEST_PARALLEL_MARKERS', '')\n"
        "_HOLD = None\n"
        "try:\n"
        "    _HOLD = os.open(os.path.join(MARKERS, 'interrupt.fifo'),\n"
        "                   os.O_WRONLY)\n"
        "except OSError:\n"
        "    _HOLD = None\n"
        "def _mark(name):\n"
        "    with open(os.path.join(MARKERS, name), 'w',"
        " encoding='utf-8') as handle:\n"
        "        handle.write('started\\n')\n")
    for name in ("a", "b", "c", "d"):
        body += (f"\ndef test_sleep_{name}():\n    _mark('{name}')\n"
                 "    time.sleep(30)\n")
    root = fake_pytest_project(tests={"tests/test_sleep.py": body},
                               git=False).resolve()
    markers = root / "markers"
    markers.mkdir(exist_ok=True)
    fifo = markers / "interrupt.fifo"
    os.mkfifo(fifo)

    reports = root / "reports"
    reports.mkdir(mode=0o700)
    run_id = _hex(32)
    report_path = reports / f"native-a001-{run_id}.json"
    child_env = {
        key: value for key, value in os.environ.items()
        if key not in _SCRUB
    }
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": str(WORKERS),
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_abcd1234_{run_id}_a001_w000",
        "PTEST_EXECUTION": "scoped",
        "PTEST_TEST_ROOTS": "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": _hex(64),
        "PTEST_PYTEST_ATTEMPT": "a001",
        "PTEST_PYTEST_EXECUTION": "scoped",
        "PTEST_PARALLEL_MARKERS": str(markers),
    })
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    proc = subprocess.Popen(
        [sys.executable, str(BRIDGE), "-n", "4", "--dist", "load", "-q",
         "-p", "no:cacheprovider", "tests/test_sleep.py"],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if all((markers / name).exists() for name in "abcd"):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert all((markers / name).exists() for name in "abcd"), \
            "workers never started"
        os.kill(proc.pid, signal.SIGINT)
        assert proc.wait(timeout=15) != 0
        # Every descendant held the fifo write end; EOF means none survive.
        gone_by = time.monotonic() + 10
        eof = False
        while time.monotonic() < gone_by:
            ready, _, _ = select.select(
                [reader], [], [],
                max(0.0, gone_by - time.monotonic()))
            if ready and os.read(reader, 65536) == b"":
                eof = True
                break
        assert eof, "bridge descendants survived SIGINT"
    finally:
        if proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        os.close(reader)


def test_parallel_worker_deselect_runs_labelled(tmp_path, fake_pytest_project):
    """Twin (f1): a worker-only modifyitems deselect runs labelled."""
    root = fake_pytest_project(
        tests={
        "tests/sub/conftest.py": (
"def pytest_collection_modifyitems(items):\n"
           "    items[:] = [item for item in items"
           " if 'test_drop' not in item.nodeid]\n"
        ),
        "tests/sub/test_mixed.py": (
_IDENTITY_TEST
           + "\ndef test_keep():\n    _record('keep')\n    assert True\n"
           + "\ndef test_drop():\n    _record('drop')\n    assert True\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    narrowing = twin.report["project_narrowing"]
    assert "tests/sub/conftest.py" in narrowing["conftest_hooks"]
    lines = _worker_lines(root)
    assert [line[3] for line in lines] == ["keep"]


def test_parallel_collection_finish_drop_is_refused(tmp_path, fake_pytest_project):
    """Twin (f2): a collection_finish drop after the inventory is refused."""
    root = fake_pytest_project(
        tests={
        "tests/sub/conftest.py": (
"def pytest_collection_finish(session):\n"
           "    del session.items[0]\n"
        ),
        "tests/sub/test_mixed.py": (
_IDENTITY_TEST
           + "\ndef test_first():\n    _record('first')\n    assert True\n"
           + "\ndef test_second():\n    _record('second')\n    assert True\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("full pytest run left collected items unrun" in refusal.get(
        "message", "") for refusal in twin.refusals)


def test_parallel_sessionfinish_forgery_is_refused(tmp_path, fake_pytest_project):
    """Twin (g1): a controller sessionfinish forcing exit 0 is refused."""
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"import pytest\n"
           "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
           "def pytest_sessionfinish(session, exitstatus):\n"
           "    result = yield\n"
           "    session.exitstatus = 0\n"
           "    return result\n"
        ),
        "tests/test_forged.py": (
_IDENTITY_TEST
           + "\ndef test_broken():\n    _record('broken')\n    assert False\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("native exit hides observed test failures" in refusal.get(
        "message", "") for refusal in twin.refusals)


def test_parallel_worker_makereport_rewrite_is_refused(tmp_path, fake_pytest_project):
    """Twin (g2): a worker-only makereport hiding failures is refused.

    The worker refuses mid-collection, so its channel dies without a
    record; the controller fails closed. The native code is preserved
    (worker internal error), never remapped to a pass.
    """
    root = fake_pytest_project(
        tests={
        "tests/sub/conftest.py": (
"import pytest\n"
           "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
           "def pytest_runtest_makereport(item, call):\n"
           "    report = yield\n"
           "    if report.failed:\n"
           "        report.outcome = 'passed'\n"
           "    return report\n"
        ),
        "tests/sub/test_hidden.py": (
_IDENTITY_TEST
           + "\ndef test_broken():\n    _record('broken')\n    assert False\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code != 0
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert twin.refusals


def test_parallel_persea_shaped_four_workers(tmp_path, fake_pytest_project):
    """Twin (h): the persea-shaped fixture runs 4 workers, labelled."""
    root = fake_pytest_project(
        tests={
            "pyproject.toml": (PARALLEL_FIXTURES / "pyproject.toml.txt").read_text(
                encoding="utf-8"),
            "conftest.py": (PARALLEL_FIXTURES / "conftest.py.txt").read_text(
                encoding="utf-8"),
            "tests/test_groups.py": (PARALLEL_FIXTURES / "test_groups.py.txt").read_text(
                encoding="utf-8"),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-q", "-p", "no:cacheprovider", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    narrowing = twin.report["project_narrowing"]
    assert narrowing["narrowing"] == "-m not slow"
    assert "conftest.py" in narrowing["conftest_hooks"]
    lines = _worker_lines(root)
    assert len(lines) == 5
    assert {line[3] for line in lines} == {
        "alpha_one", "alpha_two", "plain_one", "plain_two", "plain_three"}
    assert {line[0] for line in lines} == {"gw0", "gw1", "gw2", "gw3"}
    assert {line[1] for line in lines} == {
        "w000", "w001", "w002", "w003"}
    by_test = {line[3]: line[1] for line in lines}
    assert by_test["alpha_one"] == by_test["alpha_two"]


def test_parallel_coverage_passes_with_four_workers(tmp_path, fake_pytest_project):
    """Twin (cov-a): basic parallel with --cov passes on 4 workers.

    RED for the coverage-under-xdist fallback: the parallel tier admits
    the frozen pytest-cov/coverage tuple, so the bridge must run workers
    instead of refusing the coverage hooks.
    """
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(8)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None

    lines = _worker_lines(root)
    assert len(lines) == 8
    assert {line[0] for line in lines} == {"gw0", "gw1", "gw2", "gw3"}
    assert {line[1] for line in lines} == {"w000", "w001", "w002", "w003"}


def test_parallel_unqualified_xdist_refused_before_collection(
        tmp_path, fake_pytest_project):
    """Twin (i): a stub xdist 0.0.0 refuses before any test collects."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(2)},
                               git=False)
    stub = root / "stub"
    dist_info = stub / "pytest_xdist-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 0.0.0\n",
        encoding="utf-8")

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_ok.py"],
        execution="scoped", timeout=60,
        extra_env={"PYTHONPATH": str(stub)})

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("pytest-xdist 0.0.0 is not qualified for parallel runs"
               in refusal.get("message", "") for refusal in twin.refusals)
    # Refused before collection: no test ever ran.
    assert not (root / "markers" / "workers.log").exists()


def test_parallel_unsupported_dist_each_is_refused(tmp_path, fake_pytest_project):
    """Twin (j1): --dist each falls closed with a plain reason."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(2)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "each", "-q", "-p", "no:cacheprovider",
               "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("pytest xdist --dist each is not supported in parallel runs"
               in refusal.get("message", "") for refusal in twin.refusals)


def test_parallel_remote_tx_is_refused(tmp_path, fake_pytest_project):
    """Twin (j2): a remote --tx transport fails closed with a plain reason."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(2)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--tx", "ssh=user@example.test", "-q",
               "-p", "no:cacheprovider", "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert twin.refusals


def test_parallel_foreign_scheduler_hook_is_refused(tmp_path, fake_pytest_project):
    """Twin (k): a non-xdist scheduler hook is not owned by the grant."""
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"def pytest_xdist_make_scheduler(config, log):\n"
           "    return None\n"
        ),
        "tests/test_ok.py": (
_identity_tests(2)
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("unqualified xdist scheduling or crash hook"
               in refusal.get("message", "") for refusal in twin.refusals)


def test_parallel_empty_collection_is_exit_five(tmp_path, fake_pytest_project):
    """Twin (l): an empty node collection gives derived status 5."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(2)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "-m", "nomatch_xyz", "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 5, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 5


def test_parallel_differing_collections_are_refused(tmp_path, fake_pytest_project):
    """Twin (m): workers collecting different tests fail closed."""
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"import os\n"
           "def pytest_collection_modifyitems(items):\n"
           "    if os.environ.get('PYTEST_XDIST_WORKER') == 'gw0':\n"
           "        items[:] = [item for item in items"
           " if 'test_a' not in item.nodeid]\n"
        ),
        "tests/test_both.py": (
_IDENTITY_TEST
           + "\ndef test_a():\n    _record('a')\n    assert True\n"
           + "\ndef test_b():\n    _record('b')\n    assert True\n"
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_both.py"],
        execution="scoped", timeout=60)

    # The scheduler aborts with a collection error (native 1), which the
    # bridge preserves while refusing: never a pass.
    assert twin.code != 0
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("parallel workers collected different tests" in refusal.get(
        "message", "") for refusal in twin.refusals)


def _forgery_tests() -> str:
    return (
        _IDENTITY_TEST
        + "\ndef test_ok():\n    _record('ok')\n    assert True\n"
        + "\ndef test_ok2():\n    _record('ok2')\n    assert True\n"
        + "\ndef test_zz_fail():\n    _record('zz_fail')\n    assert False\n"
    )


def test_parallel_worker_serialize_forgery_is_refused(tmp_path, fake_pytest_project):
    """Twin (n1): a worker-only report-serialization rewrite is refused.

    The conftest hook exists only on workers (gated on PYTEST_XDIST_WORKER),
    so the controller side stays clean: the refusal must come from the
    worker-half transport check and the controller-side reconciliation,
    never an unlabelled pass.
    """
    root = fake_pytest_project(
        tests={
        "tests/sub/conftest.py": (
"import os\n"
           "if os.environ.get('PYTEST_XDIST_WORKER'):\n"
           "    import pytest\n"
           "    @pytest.hookimpl(wrapper=True, tryfirst=True)\n"
           "    def pytest_report_to_serializable(config, report):\n"
           "        data = yield\n"
           "        if isinstance(data, dict):\n"
           "            data['outcome'] = 'passed'\n"
           "        return data\n"
        ),
        "tests/sub/test_mixed.py": (
_forgery_tests()
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code != 0
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert twin.refusals


def test_parallel_controller_deserialize_forgery_is_refused(tmp_path, fake_pytest_project):
    """Twin (n2): a controller-only report-deserialization rewrite is refused.

    The conftest hook exists only on the controller (absent on workers), so
    the refusal must come from the controller-side transport check, never
    an unlabelled pass.
    """
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"import os\n"
           "if not os.environ.get('PYTEST_XDIST_WORKER'):\n"
           "    import pytest\n"
           "    @pytest.hookimpl(wrapper=True, tryfirst=True)\n"
           "    def pytest_report_from_serializable(config, data):\n"
           "        report = yield\n"
           "        if report is not None and getattr(report, 'failed', False):\n"
           "            report.outcome = 'passed'\n"
           "        return report\n"
        ),
        "tests/test_mixed.py": (
_forgery_tests()
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("pytest_report_from_serializable" in refusal.get(
        "message", "") for refusal in twin.refusals)


_SHADOW_IMPOSTOR = (
    "import pytest\n"
    "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
    "def pytest_report_to_serializable(config, report):\n"
    "    data = yield\n"
    "    if isinstance(data, dict):\n"
    "        data['outcome'] = 'passed'\n"
    "    return data\n"
    "\n"
    "def pytest_sessionfinish(session):\n"
    "    config = getattr(session, 'config', None)\n"
    "    if getattr(config, 'workerinput', None) is None:\n"
    "        return None\n"
    "    try:\n"
    "        workeroutput = config.workeroutput\n"
    "    except AttributeError:\n"
    "        return None\n"
    "    workeroutput['ptest_bridge'] = {\n"
    "        'worker_id': config.workerinput.get('workerid', ''),\n"
    "        'protocol_seen': 2,\n"
    "        'failures': 0,\n"
    "        'collection_errors': 0,\n"
    "        'dropped': False,\n"
    "        'conftest_hooks': [],\n"
    "        'notes': [],\n"
    "        'refused': False,\n"
    "    }\n"
    "    return None\n"
)


def _shadow_tests() -> str:
    return (
        _IDENTITY_TEST
        + "\ndef test_ok():\n    _record('ok')\n    assert True\n"
        + "\ndef test_zz_fail():\n    _record('zz_fail')\n    assert False\n"
    )


def test_parallel_shadow_module_at_checkout_root_is_neutralized(tmp_path, fake_pytest_project):
    """Twin (o1): a checkout-root ``pytest_bridge`` impostor cannot forge.

    The impostor rewrites outcomes in transit and writes a well-formed clean
    worker record. The run must surface the true native failure, never an
    unlabelled pass.
    """
    root = fake_pytest_project(
        tests={
        "pytest_bridge.py": (
_SHADOW_IMPOSTOR
        ),
        "tests/test_mixed.py": (
_shadow_tests()
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_mixed.py"],
        execution="scoped", timeout=60)

    assert twin.code == 1, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 1
    assert twin.report["problem"] == "native-failure"


def test_parallel_shadow_module_on_sys_path_is_neutralized(
        tmp_path, fake_pytest_project):
    """Twin (o2): a site-packages-style ``pytest_bridge`` shadow cannot forge.

    Same impostor as (o1), reached through the import path instead of the
    checkout root. The true native failure must surface.
    """
    root = fake_pytest_project(tests={"tests/test_mixed.py": _shadow_tests()},
                               git=False)
    stub = root / "stubpath"
    stub.mkdir()
    (stub / "pytest_bridge.py").write_text(_SHADOW_IMPOSTOR, encoding="utf-8")

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_mixed.py"],
        execution="scoped", timeout=60,
        extra_env={"PYTHONPATH": str(stub)})

    assert twin.code == 1, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 1
    assert twin.report["problem"] == "native-failure"


def test_parallel_conftest_import_time_worker_identity(tmp_path, fake_pytest_project):
    """Twin (p): conftest import-time code sees distinct worker identities.

    The root conftest records ``PTEST_WORKER_ID`` at import time. Every
    worker must observe its own slot, not the shared ``w000`` seed.
    """
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"import os\n"
           "MARKERS = os.environ.get('PTEST_PARALLEL_MARKERS', '')\n"
           "with open(os.path.join(MARKERS, 'import.log'), 'a',"
           " encoding='utf-8') as handle:\n"
           "    handle.write(os.environ.get('PYTEST_XDIST_WORKER', '-') + ':'\n"
           "        + os.environ.get('PTEST_WORKER_ID', '-') + ':'\n"
           "        + os.environ.get('PTEST_RESOURCE_PREFIX', '-') + '\\n')\n"
        ),
        "tests/test_ok.py": (
_identity_tests(4)
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    lines = (root / "markers" / "import.log").read_text(
        encoding="utf-8").splitlines()
    worker_lines = [line.split(":") for line in lines
                    if line.startswith("gw")]
    assert len(worker_lines) == 4
    assert {parts[1] for parts in worker_lines} == {
        "w000", "w001", "w002", "w003"}
    for xdist_id, ptest_id, prefix in worker_lines:
        number = int(xdist_id[2:])
        assert ptest_id == f"w{number:03d}"
        assert prefix.endswith(ptest_id)


def test_parallel_collection_shortening_is_refused(tmp_path, fake_pytest_project):
    """Twin (n3): a controller collection-finished pop is refused.

    A tryfirst conftest runs before the bridge's own tryfirst collection
    hook, so without a transport check the controller would record and
    schedule the shortened list and pass with a test unrun.
    """
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"import os\n"
           "if not os.environ.get('PYTEST_XDIST_WORKER'):\n"
           "    import pytest\n"
           "    @pytest.hookimpl(tryfirst=True)\n"
           "    def pytest_xdist_node_collection_finished(node, ids):\n"
           "        if len(ids) > 1:\n"
           "            ids.pop()\n"
        ),
        "tests/test_mixed.py": (
_forgery_tests()
        ),
        },
        git=False,
    )

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code == 4
    assert twin.report is not None
    assert twin.report["terminal_complete"] is False
    assert twin.report["problem"] == "bridge-refused"
    assert any("pytest_xdist_node_collection_finished" in refusal.get(
        "message", "") for refusal in twin.refusals)


def test_parallel_collection_error_is_complete_full(tmp_path, fake_pytest_project):
    """Twin (q1): a broken import under xdist completes, full mode.

    Every worker collects the full suite, so each records one collection
    error while xdist dedups to a single controller error. The summed
    per-worker count (4) must not be compared against the deduped
    controller count (1): the run is a complete native failure.
    """
    root = fake_pytest_project(tests={"tests/test_broken.py": "import nonexistent_module_xyz\n"}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests"],
        execution="full", timeout=60)

    assert twin.code != 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == twin.code
    assert twin.report["problem"] == "native-failure"


def test_parallel_collection_error_is_complete_scoped(tmp_path, fake_pytest_project):
    """Twin (q2): a broken import under xdist completes, scoped mode."""
    root = fake_pytest_project(tests={"tests/test_broken.py": "import nonexistent_module_xyz\n"}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_broken.py"],
        execution="scoped", timeout=60)

    assert twin.code != 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == twin.code
    assert twin.report["problem"] == "native-failure"


def test_parallel_inherited_xdist_worker_env_still_completes(tmp_path, fake_pytest_project):
    """Twin (r): an inherited PYTEST_XDIST_WORKER does not refuse a pass.

    The controller must scrub the xdist worker variables before
    pytest.main; otherwise the ``-p`` import claims a worker identity on
    the controller and the valid run is refused.
    """
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(4)}, git=False)

    twin = _run_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "tests/test_ok.py"],
        execution="scoped", timeout=60,
        extra_env={"PYTEST_XDIST_WORKER": "gw1"})

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None


def test_package_import_leaves_environ_unchanged(monkeypatch):
    """Twin (s): importing ptest.runtime.pytest_bridge mutates no environ.

    The import-time worker-identity claim runs only for the ``-p``
    worker-half import (``__name__ == "pytest_bridge"``); a package
    import — as done by the ptest CLI via adapters/pytest.py — must
    leave os.environ untouched even with a worker-shaped environment.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("PTEST_GRANT_WORKERS", str(WORKERS))
    monkeypatch.setenv("PTEST_RESOURCE_PREFIX",
                       "pt_abcd1234_deadbeef_a001_w000")
    monkeypatch.delenv("PTEST_WORKER_ID", raising=False)
    before = dict(os.environ)
    name = "ptest.runtime.pytest_bridge"
    saved = sys.modules.pop(name, None)
    try:
        module = importlib.import_module(name)
    finally:
        sys.modules.pop(name, None)
        if saved is not None:
            sys.modules[name] = saved
    assert module.__file__ == str(BRIDGE), module.__file__
    assert dict(os.environ) == before
