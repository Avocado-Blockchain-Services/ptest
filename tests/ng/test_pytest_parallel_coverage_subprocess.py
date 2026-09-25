"""Real-xdist subprocess twins for parallel pytest runs with coverage.

Each twin runs ``src/ptest/runtime/pytest_bridge.py`` directly as a
subprocess (``sys.executable``, scrubbed env, ``PTEST_BRIDGE_PROTOCOL``,
the adapter-to-bridge env with ``PTEST_PYTEST_PROFILE=advanced``, a
private 0700 report dir with a valid report binding) against real
pytest-xdist 3.8.0 and the frozen pytest-cov/coverage tuple from the test
environment, then parses the JSON report and the
``ptest-bridge-refusal`` stderr marker. Every twin carries an explicit
timeout of at most 60 s.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "src" / "ptest" / "runtime" / "pytest_bridge.py"
PROTOCOL = REPO_ROOT / "src" / "ptest" / "runtime" / "protocol-v1.json"
PARALLEL_FIXTURES = Path(__file__).parent / "fixtures" / "pytest" / "parallel"

WORKERS = 4

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
    "PTEST_PYTEST_COMMAND_VARIANTS", "PTEST_EXPECTED_RUNTIME_IDENTITY",
    "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID",
    "PYTEST_XDIST_WORKER_COUNT",
)

_REFUSAL_PREFIX = "ptest-bridge-refusal: "


def _hex(n: int) -> str:
    return secrets.token_hex(n // 2)


class TwinResult:
    def __init__(self, code, stdout, stderr, report, refusals):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.report = report
        self.refusals = refusals


def _refusals(stderr: bytes) -> list[dict]:
    found = []
    for line in stderr.decode(errors="replace").splitlines():
        if line.startswith(_REFUSAL_PREFIX):
            try:
                found.append(json.loads(line[len(_REFUSAL_PREFIX):]))
            except ValueError:
                pass
    return found


def _run_advanced_bridge(root: Path, argv: list[str], *, execution: str,
                         workers: int = WORKERS,
                         extra_env: dict | None = None,
                         timeout: float = 60) -> TwinResult:
    """Run the bridge file directly with a valid advanced executor binding."""
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
    checkout_id = _hex(32)
    report_path = reports / f"native-{attempt}-{run_id}.json"

    child_env = {
        key: value for key, value in os.environ.items()
        if key not in _SCRUB
    }
    child_env.update({
        "PTEST_BRIDGE_PROTOCOL": str(PROTOCOL),
        "PTEST_GRANT_WORKERS": str(workers),
        "PTEST_WORKER_ID": "w000",
        "PTEST_RESOURCE_PREFIX": f"pt_{checkout_id[:8]}_{run_id}_{attempt}_w000",
        "PTEST_CHECKOUT_ID": checkout_id,
        "PTEST_EXECUTION": execution,
        "PTEST_TEST_ROOTS": '["tests"]' if execution == "full" else "[]",
        "PTEST_PYTEST_CHECKOUT_ROOT": str(root),
        "PTEST_PYTEST_CONFIG_PATH": "",
        "PTEST_PYTEST_REPORT_PATH": str(report_path),
        "PTEST_RUN_ID": run_id,
        "PTEST_GRANT_NONCE": nonce,
        "PTEST_PYTEST_ATTEMPT": attempt,
        "PTEST_PYTEST_EXECUTION": execution,
        "PTEST_PYTEST_PROFILE": "advanced",
        "PTEST_PYTEST_COMMAND_VARIANTS": json.dumps(
            [["python", "bridge"], ["python", "bridge"]]),
        "PTEST_PARALLEL_MARKERS": str(markers),
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
    return TwinResult(
        completed.returncode, completed.stdout, completed.stderr,
        report, _refusals(completed.stderr))


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


def test_parallel_coverage_complete_with_four_workers(tmp_path, fake_pytest_project):
    """Advanced parallel with --cov: pass, 4 worker identities, coverage complete."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(8)}, git=False)

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None
    assert twin.report["coverage"] == {"complete": True}
    assert twin.report["reporters"] == {"complete": True}
    assert twin.report["inventory"]["complete"] is True
    assert len(twin.report["inventory"]["tests"]) == 8
    assert {item["outcome"] for item in twin.report["inventory"]["tests"]} == {"passed"}
    workers = twin.report["workers"]
    assert len(workers) == 4
    assert {item["worker_id"] for item in workers} == {
        "w000", "w001", "w002", "w003"}
    for item in workers:
        assert item["resource_prefix"].endswith(item["worker_id"])

    lines = _worker_lines(root)
    assert len(lines) == 8
    assert {line[0] for line in lines} == {"gw0", "gw1", "gw2", "gw3"}


def test_parallel_coverage_idle_workers_still_complete(tmp_path, fake_pytest_project):
    """One test on four workers: idle workers still contribute coverage."""
    root = fake_pytest_project(tests={"tests/test_one.py": "def test_only():\n    assert True\n"}, git=False)

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["coverage"] == {"complete": True}
    assert len(twin.report["workers"]) == 4


def test_parallel_coverage_empty_collection_exits_five_complete(tmp_path, fake_pytest_project):
    """All-deselected advanced parallel run: exit 5, terminal complete."""
    root = fake_pytest_project(tests={"tests/test_ok.py": _identity_tests(2)}, git=False)

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "-m", "nomatch_xyz",
               "tests/test_ok.py"],
        execution="scoped", timeout=60)

    assert twin.code == 5, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 5


def test_parallel_coverage_killed_worker_is_not_passed_and_incomplete(tmp_path, fake_pytest_project):
    """A SIGKILLed worker under coverage: never passed, coverage incomplete."""
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

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code != 0
    assert twin.report is None or (
        twin.report["terminal_complete"] is False
        or twin.report.get("native_exit_code") not in (None, 0))
    assert twin.report is None or twin.report["coverage"] == {"complete": False}


def test_parallel_coverage_suppressed_worker_is_incomplete_but_passes(tmp_path, fake_pytest_project):
    """One worker's coverage data suppressed: verdict passes, coverage incomplete."""
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"def pytest_sessionfinish(session):\n"
           "    config = session.config\n"
           "    if getattr(config, 'workerinput', None) is None:\n"
           "        return\n"
           "    if config.workerinput.get('workerid') != 'gw1':\n"
           "        return\n"
           "    try:\n"
           "        config.workeroutput.pop('cov_worker_node_id', None)\n"
           "    except AttributeError:\n"
           "        pass\n"
        ),
        "tests/test_ok.py": (
_identity_tests(8)
        ),
        },
        git=False,
    )

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None
    # Coverage completeness never changes the test verdict itself.
    assert twin.report["coverage"] == {"complete": False}


def test_parallel_coverage_forged_worker_key_is_incomplete(tmp_path, fake_pytest_project):
    """A worker forging its coverage key: verdict passes, coverage incomplete."""
    root = fake_pytest_project(
        tests={
        "conftest.py": (
"def pytest_sessionfinish(session):\n"
           "    config = session.config\n"
           "    if getattr(config, 'workerinput', None) is None:\n"
           "        return\n"
           "    if config.workerinput.get('workerid') != 'gw1':\n"
           "        return\n"
           "    try:\n"
           "        config.workeroutput['cov_worker_node_id'] = 'gw9'\n"
           "    except AttributeError:\n"
           "        pass\n"
        ),
        "tests/test_ok.py": (
_identity_tests(8)
        ),
        },
        git=False,
    )

    twin = _run_advanced_bridge(
        root, ["-n", "4", "--dist", "load", "-q", "-p", "no:cacheprovider",
               "--cov=tests", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["native_exit_code"] == 0
    assert twin.report["problem"] is None
    assert twin.report["coverage"] == {"complete": False}


def test_parallel_persea_shaped_coverage_runs_four_workers_labelled(
        tmp_path, fake_pytest_project):
    """Persea-shaped addopts plus --cov: 4 workers and the project label."""
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

    twin = _run_advanced_bridge(
        root, ["-q", "-p", "no:cacheprovider",
               "--cov=.", "--cov-report=", "tests"],
        execution="full", timeout=60)

    assert twin.code == 0, twin.stderr.decode()
    assert twin.report is not None
    assert twin.report["terminal_complete"] is True
    assert twin.report["coverage"] == {"complete": True}
    narrowing = twin.report["project_narrowing"]
    assert narrowing["narrowing"] == "-m not slow"
    assert "conftest.py" in narrowing["conftest_hooks"]
    assert len(twin.report["workers"]) == 4
    lines = _worker_lines(root)
    assert len(lines) == 5
    assert {line[0] for line in lines} == {"gw0", "gw1", "gw2", "gw3"}
