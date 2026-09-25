"""Real scoped guard/bridge/pytest/report acceptance.

The controller interpreter is already provisioned. Other candidate interpreters
can be supplied explicitly with PTEST_TEST_PYTHON_<version with underscores>.
Unavailable tuples are skipped and remain unqualified. The one cataloged
pytest-cov tuple is supplied through the explicit
PTEST_TEST_PYTHON_9_1_1_COV override; the child launcher is a preprovisioned
interpreter and never synchronizes dependencies.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tomllib

import pytest

import support
from ptest import config as config_api, contracts as C, operations, reports, scheduler


VERSIONS = ("8.4.2", "9.0.3", "9.1.0", "9.1.1")
FIXTURES = Path(__file__).parent / "fixtures" / "pytest"


def _interpreter(version="9.1.1"):
    supplied = os.environ.get("PTEST_TEST_PYTHON_" + version.replace(".", "_"))
    if supplied is None:
        if pytest.__version__ != version:
            pytest.skip(f"unqualified: no preprovisioned pytest {version} interpreter")
        supplied = sys.executable
    observed = subprocess.run(
        [supplied, "-c", "import pytest; print(pytest.__version__)"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert observed.stdout.strip() == version
    return supplied


def _coverage_launcher():
    supplied = os.environ.get("PTEST_TEST_PYTHON_9_1_1_COV")
    if supplied:
        candidates = [supplied]
    else:
        # The dev environment itself carries the frozen tuple (test extra),
        # so the suite interpreter qualifies when no override is set. An
        # explicitly set override keeps its strict probe (a mismatch skips
        # rather than silently falling back).
        candidates = [sys.executable]
    for candidate in candidates:
        try:
            checked = subprocess.run(
                [candidate, "-c", (
                    "import pytest, pytest_cov, coverage; "
                    "print(pytest.__version__, pytest_cov.__version__, coverage.__version__)"
                )], capture_output=True, text=True, timeout=5, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            pytest.skip(f"unqualified: frozen pytest-cov fixture unavailable ({exc})")
        if checked.returncode == 0 and checked.stdout.strip() == "9.1.1 7.1.0 7.15.0":
            return (candidate,)
    if supplied:
        detail = (checked.stderr.strip() or checked.stdout.strip() or "tuple probe failed")[-240:]
        pytest.skip(f"unqualified: frozen pytest-cov fixture unavailable ({detail})")
    pytest.skip("unqualified: set PTEST_TEST_PYTHON_9_1_1_COV to a preprovisioned frozen coverage tuple")


def _parallel_coverage_launcher():
    """A frozen-tuple interpreter that also carries qualified xdist."""
    launcher = _coverage_launcher()
    try:
        checked = subprocess.run(
            [launcher[0], "-c", (
                "import xdist; print(xdist.__version__)"
            )], capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"unqualified: pytest-xdist fixture unavailable ({exc})")
    if checked.returncode != 0 or checked.stdout.strip() != "3.8.0":
        detail = (checked.stderr.strip() or checked.stdout.strip() or "xdist probe failed")[-240:]
        pytest.skip(f"unqualified: qualified pytest-xdist unavailable ({detail})")
    return launcher


def _project(case, domain, *, version="9.1.1", args=(), conftest="",
             allow_xdist=False, launcher=None):
    root = case.project(domain, kind="pytest")
    config_path = root / ".ptest.toml"
    project_id = tomllib.loads(config_path.read_text())["project_id"]
    selected_launcher = (_interpreter(version) if launcher is None else launcher)
    config_path.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(selected_launcher) if isinstance(selected_launcher, tuple) else [selected_launcher])}\n'
        f'args = {json.dumps(["-s", *(args if allow_xdist else ("-p", "no:xdist", *args))])}\nfull_args = ["--invalid-full-only"]\n'
        'test_roots = ["tests"]\nworkers = 8\n'
    )
    (root / "tests").mkdir()
    (root / "project_module.py").write_text("VALUE = 7\n")
    shutil.copyfile(FIXTURES / "suite.py.txt", root / "tests/test_native.py")
    if conftest:
        (root / "conftest.py").write_text(conftest)
    return root


def _data(result):
    assert result.result is not None, result.stderr.decode()
    assert result.result["error"] is None
    return result.result["data"]


def _no_claims(data):
    assert data["source_valid"] is False
    assert data["full_gate_eligible"] is False
    assert data["baseline_published"] is False
    assert data["counts"] is None
    assert data["plan"]["execution"] == "scoped"


def _released(domain, count=1):
    leases = scheduler.reconcile(domain)
    assert len(leases) == count
    assert all(lease.state is C.LeaseState.RELEASED for lease in leases)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("failure", [False, True], ids=["pass", "fail"])
def test_real_scoped_native_tuple_preserves_stream_exit_and_consumes_report(case, version, failure):
    domain = case.domain(slots=2, jobs=2)
    root = _project(case, domain, version=version)
    result = case.invoke(domain, root, "--workers", "8", "--", "tests",
                         env={"FIXTURE_FAILURE": str(int(failure))}, timeout=10)
    assert result.code == int(failure), result.stderr.decode()
    assert b"native-output:quoted [x];$HOME" in result.stdout
    assert (root / "tests-ran").read_text() == "yes"
    data = _data(result)
    assert data["runner_exit_code"] == int(failure)
    assert data["status"] == ("failed" if failure else "passed")
    assert data["granted_workers"] == 1
    assert not any(r["code"] in {"report-invalid", "state-unavailable"} for r in data["reasons"])
    _no_claims(data)
    _released(domain)
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    assert not list((domain.root / "checkouts").glob("*/history.sqlite3"))


def test_real_scoped_literal_suffix_is_not_reparsed_or_shell_expanded(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import json\nfrom pathlib import Path\n"
        "def pytest_addoption(parser):\n    parser.addoption('--literal')\n"
        "def pytest_configure(config):\n"
        "    Path('literal.json').write_text(json.dumps(config.getoption('--literal')))\n"
    ))
    value = "--workers 64; $(touch injected) '界' $HOME"
    result = case.invoke(domain, root, "--", "--literal", value, "tests", timeout=10)
    assert result.code == 0, result.stderr.decode()
    assert json.loads((root / "literal.json").read_text()) == value
    assert not (root / "injected").exists()
    _no_claims(_data(result))


def test_real_blocked_xdist_is_not_an_active_plugin(case):
    domain = case.domain()
    root = _project(case, domain)
    result = case.invoke(domain, root, "--", "-p", "no:xdist", "tests", timeout=10)
    assert result.code == 0, result.stderr.decode()
    assert (root / "tests-ran").exists()


def _nocov_launcher():
    """A pytest 9.1.1 interpreter WITHOUT pytest-cov (missing-tuple fixture).

    Provisioned outside the checkout (the dev environment itself now
    carries the frozen tuple)::

        nocov_env=$(mktemp -d /tmp/ptest-qpy-nocov-XXXXXX)
        uv venv "$nocov_env" --python 3.14
        uv pip install --python "$nocov_env/bin/python" "pytest==9.1.1"
        PTEST_TEST_PYTHON_9_1_1_NOCOV="$nocov_env/bin/python" \
          ptest tests/ng/test_pytest_scoped_subprocess.py \
          -k test_cataloged_advanced_tuple_reaches_bridge_but_missing_cov_fails_closed -q
    """
    supplied = os.environ.get("PTEST_TEST_PYTHON_9_1_1_NOCOV")
    if not supplied:
        pytest.skip("unqualified: set PTEST_TEST_PYTHON_9_1_1_NOCOV to a pytest 9.1.1 interpreter without pytest-cov")
    try:
        checked = subprocess.run(
            [supplied, "-c", (
                "import pytest; print(pytest.__version__); "
                "import pytest_cov"
            )], capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"unqualified: no-cov fixture unavailable ({exc})")
    if checked.returncode == 0 or "No module named 'pytest_cov'" not in checked.stderr:
        pytest.skip("unqualified: no-cov fixture carries pytest-cov")
    if checked.stdout.strip() != "9.1.1":
        pytest.skip(f"unqualified: no-cov fixture is not pytest 9.1.1 ({checked.stdout.strip()})")
    return (supplied,)


def test_cataloged_advanced_tuple_reaches_bridge_but_missing_cov_fails_closed(case):
    """Catalog admission is reachable; absent pytest-cov is not qualification evidence."""
    domain = case.domain()
    root = _project(case, domain, launcher=_nocov_launcher(),
                    args=("--cov=project_module", "--cov-report=term"))
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text().replace("workers = 8", "workers = 1"))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert result.code == 4
    assert data["status"] == "incomplete"
    assert data["source_valid"] is False
    assert data["baseline_published"] is False
    assert b"unsupported-capability" in result.stderr


def test_real_off_table_coverage_tuple_refuses_before_tests(case):
    interpreter = os.environ.get("PTEST_TEST_PYTHON_OFFTABLE_COV")
    if interpreter is None:
        pytest.skip("unqualified: no preprovisioned off-table coverage interpreter supplied")
    probe = subprocess.run(
        [interpreter, "-c", (
            "import pytest, pytest_cov, coverage; "
            "print(pytest.__version__, pytest_cov.__version__, coverage.__version__)"
        )], check=True, capture_output=True, text=True, timeout=5,
    )
    assert probe.stdout.strip() == "9.1.1 7.0.0 7.16.1"
    domain = case.domain()
    root = _project(
        case, domain, launcher=interpreter,
        args=("--cov=project_module", "--cov-report=term"),
    )
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text().replace("workers = 8", "workers = 1"))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert result.code == 4
    assert data["status"] == "incomplete"
    assert data["source_valid"] is False
    assert data["baseline_published"] is False
    assert b"unsupported-capability" in result.stderr
    assert not (root / "tests-ran").exists()


def test_q_py_select_real_coverage_baseline_then_exact_selected_file(case):
    """Real locked pytest-cov tuple proves baseline inventory and exact SELECT."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, launcher=_coverage_launcher())
    # Keep the baseline inventory larger than the selected group so the
    # planner proves exact-file selection rather than treating one-file
    # selection as a full run.
    (root / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")
    # The helper's native fixture command is replaced by the locked coverage
    # tuple and an explicitly closed group mapping for exact selection.
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(_coverage_launcher()))}\n'
        'args = ["-s", "-p", "no:xdist", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py", "tests/test_extra.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", "tests/__pycache__", "ptest-result-q-py-select.json", "ptest-result-q-py-select-selected.json", "ptest-result-q-py-select-no-profile.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], tests = ["tests/test_native.py"] }]\n'
    )
    support.init_git_repo(root)
    result_path = "ptest-result-q-py-select.json"
    baseline = case.invoke(domain, root, "--result-json", result_path, "--full", timeout=30)
    baseline_data = _data(baseline)
    assert baseline.code == 0, baseline.stderr.decode()
    assert baseline_data["status"] == "passed"
    assert baseline_data["baseline_published"] is True
    assert baseline_data["full_gate_eligible"] is True
    (root / "project_module.py").write_text("VALUE = 7\n# changed source digest\n")
    selected = case.invoke(domain, root, "--result-json", "ptest-result-q-py-select-selected.json", "--changed", timeout=30)
    selected_data = _data(selected)
    assert selected.code == 0, selected.stderr.decode()
    assert selected_data["status"] == "passed"
    assert selected_data["plan"]["execution"] == "selected"
    assert selected_data["plan"]["files"] == ["tests/test_native.py"]
    assert selected_data["counts"]["collected"] == 2
    assert selected_data["source_valid"] is True
    profile = next((domain.root / "checkouts").glob("*/qualified-native-profile.json"))
    profile.unlink()
    fallback = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-select-no-profile.json", "--changed",
        timeout=30)
    fallback_data = _data(fallback)
    assert fallback.code == 4, fallback.stderr.decode()
    assert fallback_data["plan"]["execution"] == "full"
    assert fallback_data["baseline_published"] is False


def test_q_py_select_parallel_coverage_baseline_then_parallel_selected(case):
    """A parallel coverage baseline qualifies selection that selects in parallel.

    The fixture enables xdist (-n 4) with the frozen pytest-cov/coverage
    tuple: the --full baseline runs on 4 workers with complete coverage,
    publishes a parallel-identity profile, and the later --changed run
    selects exact files on 4 workers.
    """
    domain = case.domain(slots=4, jobs=4)
    launcher = _parallel_coverage_launcher()
    root = _project(case, domain, launcher=launcher)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4 --dist=load'\n",
        encoding="utf-8")
    (root / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(launcher))}\n'
        'args = ["-s", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py", "tests/test_extra.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", "tests/__pycache__", "ptest-result-q-py-select-parallel.json", "ptest-result-q-py-select-parallel-selected.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], tests = ["tests/test_native.py"] }]\n'
    )
    support.init_git_repo(root)
    result_path = "ptest-result-q-py-select-parallel.json"
    baseline = case.invoke(domain, root, "--result-json", result_path, "--full", timeout=60)
    baseline_data = _data(baseline)
    assert baseline.code == 0, baseline.stderr.decode()
    assert baseline_data["status"] == "passed"
    assert baseline_data["baseline_published"] is True
    assert baseline_data["full_gate_eligible"] is True
    assert baseline_data["granted_workers"] == 4
    (root / "project_module.py").write_text("VALUE = 7\n# changed source digest\n")
    selected = case.invoke(domain, root, "--result-json", "ptest-result-q-py-select-parallel-selected.json", "--changed", timeout=60)
    selected_data = _data(selected)
    assert selected.code == 0, selected.stderr.decode()
    assert selected_data["status"] == "passed"
    assert selected_data["plan"]["execution"] == "selected"
    assert selected_data["plan"]["files"] == ["tests/test_native.py"]
    assert selected_data["granted_workers"] == 4
    assert selected_data["source_valid"] is True


def test_q_py_select_without_parallel_profile_runs_serial_with_baseline_reason(case):
    """A selected run without a parallel profile says why it goes serial.

    The baseline is forced serial (--workers 1), so it publishes a
    serial-only profile; the later selected run names the missing parallel
    coverage baseline instead of going serial silently.
    """
    domain = case.domain(slots=4, jobs=4)
    launcher = _parallel_coverage_launcher()
    root = _project(case, domain, launcher=launcher)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4 --dist=load'\n",
        encoding="utf-8")
    (root / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(launcher))}\n'
        'args = ["-s", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py", "tests/test_extra.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", "tests/__pycache__", "ptest-result-q-py-select-serial.json", "ptest-result-q-py-select-serial-selected.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], tests = ["tests/test_native.py"] }]\n'
    )
    support.init_git_repo(root)
    baseline = case.invoke(domain, root, "--result-json", "ptest-result-q-py-select-serial.json",
                           "--full", "--workers", "1", timeout=60)
    baseline_data = _data(baseline)
    assert baseline.code == 0, baseline.stderr.decode()
    assert baseline_data["granted_workers"] == 1
    assert baseline_data["baseline_published"] is True
    (root / "project_module.py").write_text("VALUE = 7\n# changed source digest\n")
    selected = case.invoke(domain, root, "--result-json", "ptest-result-q-py-select-serial-selected.json",
                           "--changed", timeout=60)
    selected_data = _data(selected)
    assert selected.code == 0, selected.stderr.decode()
    assert selected_data["plan"]["execution"] == "selected"
    assert selected_data["plan"]["files"] == ["tests/test_native.py"]
    assert selected_data["granted_workers"] == 1
    assert selected_data["command"]["workers"] == 1
    assert ["parallel-workers",
            "serial: parallel selection needs a parallel coverage baseline "
            "— run ptest --full once"] in [
                [reason["code"], reason["message"]]
                for reason in selected_data["reasons"]]


def test_q_py_scoped_coverage_uses_parallel_workers_without_baseline(case):
    """A scoped coverage run with no stored profile still uses xdist workers.

    Adding --cov must never make scoped runs serial: the tier admits the
    frozen pair, and coverage completeness only gates qualification, never
    the scoped verdict.
    """
    domain = case.domain(slots=4, jobs=4)
    launcher = _parallel_coverage_launcher()
    root = _project(case, domain, launcher=launcher, allow_xdist=True,
                    args=("--cov=project_module", "--cov-report=term"))
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4 --dist=load'\n",
        encoding="utf-8")
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(launcher))}\n'
        'args = ["-s", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", "tests/__pycache__", "ptest-result-q-py-scoped-cov.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], tests = ["tests/test_native.py"] }]\n'
    )
    support.init_git_repo(root)
    scoped = case.invoke(domain, root, "--result-json", "ptest-result-q-py-scoped-cov.json",
                         "--", "tests/test_native.py", timeout=60)
    scoped_data = _data(scoped)
    assert scoped.code == 0, scoped.stderr.decode()
    assert scoped_data["plan"]["execution"] == "scoped"
    assert scoped_data["granted_workers"] == 4


def test_q_py_scoped_after_full_baseline_normalizes_owned_scope_identity(case):
    """A qualified full baseline must not make an explicit native scope stale."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, launcher=_coverage_launcher())
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(_coverage_launcher()))}\n'
        'args = ["-s", "-p", "no:xdist", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py", "tests/test_extra.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", '
        '"tests/__pycache__", "ptest-result-q-py-scoped.json", '
        '"ptest-result-q-py-scoped-selected.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], '
        'tests = ["tests/test_native.py"] }]\n'
    )
    (root / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")
    support.init_git_repo(root)
    baseline = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-scoped.json", "--full", timeout=30)
    baseline_data = _data(baseline)
    assert baseline.code == 0, baseline.stderr.decode()
    assert baseline_data["full_gate_eligible"] is True
    scoped = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-scoped-selected.json",
        "--", "tests/test_native.py", timeout=30)
    scoped_data = _data(scoped)
    assert scoped.code == 0, scoped.stderr.decode()
    assert scoped_data["status"] == "passed"
    assert scoped_data["plan"]["execution"] == "scoped"
    assert scoped_data["source_valid"] is True
    assert scoped_data["granted_workers"] == 1


def test_q_py_selected_runtime_drift_refuses_then_full_rebaselines(case):
    """A child plugin drift refuses SELECT before tests; FULL can rebaseline it."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, launcher=_coverage_launcher())
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(_coverage_launcher()))}\n'
        'args = ["-s", "-p", "no:xdist", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py", "tests/test_extra.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", '
        '"tests/__pycache__", "conftest.py", "drift_plugin.py", '
        '"ptest-result-q-py-drift.json", "ptest-result-q-py-drift-selected.json", '
        '"ptest-result-q-py-drift-full.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], '
        'tests = ["tests/test_native.py"] }]\n'
    )
    (root / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\ntests-ran\nptest-result-*\n"
        "conftest.py\ndrift_plugin.py\n"
    )
    support.init_git_repo(root)
    baseline = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-drift.json", "--full", timeout=30)
    baseline_data = _data(baseline)
    assert baseline.code == 0, baseline.stderr.decode()
    assert baseline_data["baseline_published"] is True
    (root / "tests-ran").unlink()

    (root / "project_module.py").write_text("VALUE = 7\n# selected runtime drift\n")
    (root / "conftest.py").write_text("pytest_plugins = ['drift_plugin']\n")
    (root / "drift_plugin.py").write_text("RUNTIME_DRIFT = True\n")
    selected = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-drift-selected.json",
        "--changed", timeout=30)
    selected_data = _data(selected)
    assert selected.code != 0, (selected_data, selected.stderr.decode())
    assert selected_data["status"] == "incomplete"
    assert selected_data["baseline_published"] is False
    assert not (root / "tests-ran").exists()
    assert any(reason["code"] in {"report-invalid", "unsupported-capability"}
               for reason in selected_data["reasons"])

    support.init_git_repo(root)
    full = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-drift-full.json", "--full", timeout=30)
    full_data = _data(full)
    assert full.code == 0, full.stderr.decode()
    assert full_data["status"] == "passed"
    assert full_data["baseline_published"] is True


def test_advanced_full_runtime_change_rebaselines_without_stale_expected_digest(case):
    """An explicit full gate may observe a changed runtime and publish anew."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, launcher=_coverage_launcher())
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(_coverage_launcher()))}\n'
        'args = ["-s", "-p", "no:xdist", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", '
        '"tests/__pycache__", "ptest-result-q-py-runtime.json", '
        '"ptest-result-q-py-runtime-changed.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], '
        'tests = ["tests/test_native.py"] }]\n'
    )
    support.init_git_repo(root)
    first = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-runtime.json", "--full", timeout=30)
    first_data = _data(first)
    assert first.code == 0, first.stderr.decode()
    assert first_data["baseline_published"] is True
    config.write_text(config.read_text().replace("--cov-report=term", "--cov-report=term-missing"))
    support.init_git_repo(root)
    second = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-runtime-changed.json", "--full", timeout=30)
    second_data = _data(second)
    assert second.code == 0, second.stderr.decode()
    assert second_data["status"] == "passed"
    assert second_data["full_gate_eligible"] is True
    assert second_data["baseline_published"] is True


@pytest.mark.parametrize("hook", ["pytest_runtestloop", "pytest_runtest_protocol", "pytest_cmdline_main"])
def test_real_unowned_execution_hook_is_refused_before_collection(case, hook):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "from pathlib import Path\nPath('conftest-loaded').write_text('yes')\n"
        f"def {hook}():\n    Path('unowned-hook-ran').write_text('yes')\n    return True\n"
    ))
    (root / "tests/test_native.py").write_text("from pathlib import Path\nPath('collected').touch()\ndef test_one(): pass\n")
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert data["runner_exit_code"] == 4
    assert any(r["code"] == "unsupported-capability" for r in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "conftest-loaded").exists()  # Qualification is before collection, not conftest import.
    assert not (root / "collected").exists()
    assert not (root / "unowned-hook-ran").exists()
    _released(domain)


def test_real_pytest_cov_lookalike_hook_is_not_approved(case):
    domain = case.domain()
    root = _project(case, domain)
    (root / "pytest_cov_shim.py").write_text(
        "from pathlib import Path\n"
        "def pytest_runtest_call(item):\n"
        "    Path('lookalike-hook-ran').touch()\n"
    )
    (root / "conftest.py").write_text("pytest_plugins = ['pytest_cov_shim']\n")
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert result.code == data["runner_exit_code"] == 4
    assert data["status"] == "incomplete"
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert not (root / "lookalike-hook-ran").exists()
    _released(domain)


@pytest.mark.parametrize("hook", ["pytest_runtestloop", "pytest_runtest_protocol", "pytest_pyfunc_call"])
def test_real_nested_execution_hook_is_refused_before_tests(case, hook):
    domain = case.domain()
    root = _project(case, domain)
    nested = root / "tests/unit"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "from pathlib import Path\nPath('nested-conftest-loaded').touch()\n"
        f"def {hook}():\n    Path('nested-executor-ran').touch()\n    return True\n"
    )
    (nested / "test_nested.py").write_text(
        "from pathlib import Path\nPath('nested-collected').touch()\n"
        "def test_nested():\n    Path('nested-test-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "nested-conftest-loaded").exists()
    assert (root / "nested-collected").exists()
    assert not (root / "nested-executor-ran").exists()
    assert not (root / "nested-test-ran").exists()
    assert not (root / "tests-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


def test_real_collection_finish_cannot_install_the_test_loop(case):
    domain = case.domain()
    root = _project(case, domain)
    nested = root / "tests/unit"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "class Executor:\n"
        "    def pytest_runtestloop(self, session):\n"
        "        Path('collection-finish-executor-ran').touch()\n"
        "        return True\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_collection_finish(session):\n"
        "    Path('collection-finish-ran').touch()\n"
        "    session.config.pluginmanager.register(Executor(), 'collection-finish-executor')\n"
    )
    (nested / "test_nested.py").write_text(
        "from pathlib import Path\nPath('collection-finish-collected').touch()\n"
        "def test_nested():\n    Path('collection-finish-test-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert (
        data["status"],
        (root / "collection-finish-executor-ran").exists(),
        (root / "collection-finish-test-ran").exists(),
    ) == ("incomplete", False, False)
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "collection-finish-collected").exists()
    assert (root / "collection-finish-ran").exists()
    assert not (root / "tests-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


def test_real_outer_collection_finish_wrapper_cannot_install_the_test_loop(case):
    domain = case.domain()
    root = _project(case, domain)
    nested = root / "tests/unit"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "class Executor:\n"
        "    def pytest_runtestloop(self, session):\n"
        "        Path('outer-collection-finish-executor-ran').touch()\n"
        "        return True\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_collection_finish(session):\n"
        "    yield\n"
        "    Path('outer-collection-finish-ran').touch()\n"
        "    session.config.pluginmanager.register(Executor(), 'outer-collection-finish-executor')\n"
    )
    (nested / "test_nested.py").write_text(
        "from pathlib import Path\nPath('outer-collection-finish-collected').touch()\n"
        "def test_nested():\n    Path('outer-collection-finish-test-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "outer-collection-finish-collected").exists()
    assert (root / "outer-collection-finish-ran").exists()
    assert not (root / "outer-collection-finish-executor-ran").exists()
    assert not (root / "outer-collection-finish-test-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


def test_real_yield_fixture_teardown_cannot_install_protocol_suppression(case):
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests/test_native.py").unlink()
    (root / "tests/test_late_protocol.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "class Executor:\n"
        "    @pytest.hookimpl(tryfirst=True)\n"
        "    def pytest_runtest_protocol(self, item, nextitem):\n"
        "        Path('teardown-protocol-executor-ran').touch()\n"
        "        return True\n"
        "@pytest.fixture(autouse=True)\n"
        "def register_after_call(request):\n"
        "    yield\n"
        "    if not request.config.pluginmanager.hasplugin('teardown-protocol-executor'):\n"
        "        Path('teardown-protocol-registered').touch()\n"
        "        request.config.pluginmanager.register(Executor(), 'teardown-protocol-executor')\n"
        "def test_first():\n    Path('teardown-first-test-ran').touch()\n"
        "def test_later():\n    Path('teardown-later-test-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "teardown-first-test-ran").exists()
    assert (root / "teardown-protocol-registered").exists()
    assert not (root / "teardown-protocol-executor-ran").exists()
    assert not (root / "teardown-later-test-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


def test_real_setup_skip_registration_cannot_install_protocol_suppression(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\nfrom pathlib import Path\n"
        "CONFIG = None\n"
        "class Executor:\n"
        "    @pytest.hookimpl(tryfirst=True)\n"
        "    def pytest_runtest_protocol(self, item, nextitem):\n"
        "        Path('skip-protocol-executor-ran').touch()\n"
        "        return True\n"
        "def pytest_configure(config):\n"
        "    global CONFIG\n    CONFIG = config\n"
        "def pytest_runtest_setup(item):\n"
        "    if item.name == 'test_skip':\n        pytest.skip('ordinary setup skip')\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'setup' and report.outcome == 'skipped' and not CONFIG.pluginmanager.hasplugin('skip-protocol-executor'):\n"
        "        Path('skip-protocol-registered').touch()\n"
        "        CONFIG.pluginmanager.register(Executor(), 'skip-protocol-executor')\n"
    ))
    (root / "tests/test_native.py").unlink()
    (root / "tests/test_skip_protocol.py").write_text(
        "from pathlib import Path\n"
        "def test_skip():\n    Path('skip-body-ran').touch()\n"
        "def test_later():\n    Path('skip-later-body-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "skip-protocol-registered").exists()
    assert not (root / "skip-protocol-executor-ran").exists()
    assert not (root / "skip-body-ran").exists()
    assert not (root / "skip-later-body-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


@pytest.mark.parametrize("failure", [False, True], ids=["pass", "native-fail"])
def test_real_last_item_teardown_registration_cannot_certify_completion(case, failure):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\nfrom pathlib import Path\n"
        "class Executor:\n"
        "    @pytest.hookimpl(tryfirst=True)\n"
        "    def pytest_runtest_protocol(self, item, nextitem):\n"
        "        Path('last-item-executor-ran').touch()\n"
        "        return True\n"
        "@pytest.fixture(autouse=True)\n"
        "def register_after_last_item(request):\n"
        "    yield\n"
        "    if not request.config.pluginmanager.hasplugin('last-item-executor'):\n"
        "        Path('last-item-protocol-registered').touch()\n"
        "        request.config.pluginmanager.register(Executor(), 'last-item-executor')\n"
    ))
    (root / "tests/test_native.py").unlink()
    (root / "tests/test_last_item.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "def test_last_item():\n"
        "    Path('last-item-body-ran').touch()\n"
        "    assert os.environ.get('FIXTURE_FAILURE') != '1'\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10,
                         env={"FIXTURE_FAILURE": str(int(failure))})

    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    expected_exit = 1 if failure else 4
    assert result.code == data["runner_exit_code"] == expected_exit
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "last-item-body-ran").exists()
    assert (root / "last-item-protocol-registered").exists()
    assert not (root / "last-item-executor-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


@pytest.mark.parametrize(
    ("registration", "registered_marker"),
    [
        (
            "def pytest_runtest_setup(item):\n"
            "    if not item.config.pluginmanager.hasplugin('setup-hook-executor'):\n"
            "        Path('setup-hook-registered').touch()\n"
            "        item.config.pluginmanager.register(Executor(), 'setup-hook-executor')\n",
            "setup-hook-registered",
        ),
        (
            "@pytest.fixture(autouse=True)\n"
            "def register_executor(request):\n"
            "    if not request.config.pluginmanager.hasplugin('fixture-executor'):\n"
            "        Path('autouse-fixture-registered').touch()\n"
            "        request.config.pluginmanager.register(Executor(), 'fixture-executor')\n",
            "autouse-fixture-registered",
        ),
    ],
    ids=["setup-hook", "autouse-fixture"],
)
def test_real_per_item_setup_cannot_replace_the_test_body(case, registration, registered_marker):
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests/test_native.py").unlink()
    nested = root / "tests/unit"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "class Executor:\n"
        "    def pytest_pyfunc_call(self, pyfuncitem):\n"
        "        Path('per-item-executor-ran').touch()\n"
        "        return True\n"
        + registration
    )
    (nested / "test_nested.py").write_text(
        "from pathlib import Path\nPath('per-item-collected').touch()\n"
        "def test_first():\n    Path('per-item-first-test-ran').touch()\n"
        "def test_second():\n    Path('per-item-second-test-ran').touch()\n"
    )

    result = case.invoke(domain, root, "--", "tests", timeout=10)

    data = _data(result)
    assert (
        data["status"],
        (root / "per-item-executor-ran").exists(),
        (root / "per-item-first-test-ran").exists(),
        (root / "per-item-second-test-ran").exists(),
    ) == ("incomplete", False, False, False)
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert any(reason["code"] == "unsupported-capability" for reason in data["reasons"])
    assert b"native-config-invalid" in result.stderr
    assert (root / "per-item-collected").exists()
    assert (root / registered_marker).exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _no_claims(data)
    _released(domain)


def test_real_native_usage_error_is_not_a_bridge_refusal(case):
    domain = case.domain()
    root = _project(case, domain)
    result = case.invoke(domain, root, "--", "--unknown-native-option", timeout=10)
    data = _data(result)
    assert result.code == data["runner_exit_code"] == 4
    assert data["status"] == "failed"
    assert data["exit_origin"] == "runner"
    assert not any(r["code"] in {"report-invalid", "native-config-invalid"} for r in data["reasons"])
    assert not (root / "tests-ran").exists()
    _released(domain)


_TAMPER = """
import atexit, json, os
from pathlib import Path
def alter_report():
    path = Path(os.environ['PTEST_PYTEST_REPORT_PATH'])
    value = json.loads(path.read_text())
    mode = os.environ['REPORT_FAULT']
    if mode == 'missing':
        path.unlink()
        return
    if mode == 'malformed':
        path.write_text('not-json')
        return
    if mode == 'nonce': value['nonce'] = '0' * 64
    if mode == 'attempt': value['attempt_id'] = 'a002'
    if mode == 'exit':
        value.update(native_exit_code=23, bridge_exit_code=23, problem='native-failure')
    if mode == 'refused':
        value.update(terminal_complete=False, native_exit_code=None, bridge_exit_code=4, problem='bridge-refused')
    if mode == 'save': Path('previous-report.json').write_text(json.dumps(value))
    if mode == 'replay': value = json.loads(Path('previous-report.json').read_text())
    path.write_text(json.dumps(value))
atexit.register(alter_report)
"""


@pytest.mark.parametrize("fault", ["missing", "malformed", "nonce", "attempt", "exit"])
@pytest.mark.parametrize("failure", [False, True], ids=["zero", "nonzero"])
def test_real_missing_forged_mismatched_report_never_passes(case, fault, failure):
    domain = case.domain()
    root = _project(case, domain, conftest=_TAMPER)
    result = case.invoke(domain, root, "--", "tests", timeout=10,
                         env={"REPORT_FAULT": fault, "FIXTURE_FAILURE": str(int(failure))})
    data = _data(result)
    assert result.code == (1 if failure else 70)
    assert data["runner_exit_code"] == int(failure)
    assert data["status"] == "incomplete"
    assert any(r["code"] in {"report-invalid", "state-unavailable"} for r in data["reasons"])
    assert (root / "tests-ran").exists()
    _no_claims(data)
    _released(domain)


def test_real_report_from_previous_grant_cannot_be_replayed(case):
    domain = case.domain()
    root = _project(case, domain, conftest=_TAMPER)
    first = case.invoke(domain, root, "--", "tests", timeout=10, env={"REPORT_FAULT": "save"})
    assert first.code == 0
    second = case.invoke(domain, root, "--", "tests", timeout=10, env={"REPORT_FAULT": "replay"})
    assert second.code == 70
    assert _data(second)["status"] == "incomplete"
    assert any(r["code"] == "report-invalid" for r in _data(second)["reasons"])
    _released(domain, 2)


def test_real_disagreeing_refusal_report_keeps_observed_native_exit_origin(case):
    domain = case.domain()
    root = _project(case, domain, conftest=_TAMPER)
    result = case.invoke(domain, root, "--", "tests", timeout=10,
                         env={"REPORT_FAULT": "refused", "FIXTURE_FAILURE": "1"})
    data = _data(result)
    assert result.code == data["runner_exit_code"] == 1
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "runner"
    assert any(reason["code"] == "report-invalid" for reason in data["reasons"])
    _released(domain)


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "fixture condition did not arrive"
        time.sleep(0.01)


_BLOCK = """
import os, time
from pathlib import Path
def test_block():
    label = os.environ.get('BLOCK_LABEL', 'first')
    Path(label + '-started').touch()
    deadline = time.monotonic() + 5
    while not Path(label + '-release').exists():
        assert time.monotonic() < deadline, 'fixture release timed out'
        time.sleep(0.01)
"""


@pytest.mark.parametrize("change", ["args", "runner", "invalid", "removed"])
def test_real_queued_config_change_cancels_without_launch(case, change):
    domain = case.domain(slots=2, jobs=2)
    root = _project(case, domain)
    (root / "tests/test_native.py").write_text(_BLOCK)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(case.invoke, domain, root, "--", "tests", timeout=12)
        _wait_for(lambda: (root / "first-started").exists())
        second = pool.submit(case.invoke, domain, root, "--", "tests", timeout=12,
                             env={"BLOCK_LABEL": "second"})
        _wait_for(lambda: any(l.state is C.LeaseState.QUEUED for l in scheduler.reconcile(domain)))
        path = root / ".ptest.toml"
        original = path.read_text()
        if change == "removed": path.unlink()
        elif change == "invalid": path.write_text("invalid toml !")
        elif change == "runner": path.write_text(original.replace('kind = "pytest"', 'kind = "command"'))
        else: path.write_text(original.replace(
            'args = ["-s", "-p", "no:xdist"]', 'args = ["-q"]'))
        (root / "first-release").touch()
        # Prevent a buggy stale run from delaying RED for the fixture deadline.
        (root / "second-release").touch()
        first.result()
        refused = second.result()
    assert refused.code == 2
    assert b"changed-during-run" in refused.stderr
    assert not (root / "second-started").exists()
    states = [l.state for l in scheduler.reconcile(domain)]
    assert sorted(s.value for s in states) == ["CANCELLED", "RELEASED"]


@pytest.mark.parametrize("same_checkout", [True, False], ids=["serialize", "overlap"])
def test_real_checkout_admission_serializes_or_overlaps(case, same_checkout):
    domain = case.domain(slots=2, jobs=2)
    first_root = _project(case, domain)
    second_root = first_root if same_checkout else _project(case, domain)
    for root in {first_root, second_root}:
        (root / "tests/test_native.py").write_text(_BLOCK)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(case.invoke, domain, first_root, "--", "tests", timeout=12)
        _wait_for(lambda: (first_root / "first-started").exists())
        second = pool.submit(case.invoke, domain, second_root, "--", "tests", timeout=12,
                             env={"BLOCK_LABEL": "second"})
        if same_checkout:
            _wait_for(lambda: any(l.state is C.LeaseState.QUEUED for l in scheduler.reconcile(domain)))
            assert not (second_root / "second-started").exists()
        else:
            _wait_for(lambda: (second_root / "second-started").exists())
            assert len([l for l in scheduler.reconcile(domain) if l.state is C.LeaseState.RUNNING]) == 2
        (first_root / "first-release").touch()
        (second_root / "second-release").touch()
        assert first.result().code == second.result().code == 0
    _released(domain, 2)


def test_real_cancellation_reaps_guard_and_releases_checkout(case):
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests/test_native.py").write_text(_BLOCK)
    def cancel_when_started():
        _wait_for(lambda: (root / "first-started").exists())
        os.kill(os.getpid(), signal.SIGINT)
    with ThreadPoolExecutor(max_workers=1) as pool:
        cancel = pool.submit(cancel_when_started)
        result = operations.execute(domain, config_api.resolve_config(root).config,
                                    C.RunRequest(mode=C.Mode.SCOPED, argv=("tests",)))
        cancel.result(timeout=5)
    data = C.serialize_run_result(result)
    assert result.exit_code == result.runner_exit_code == 2
    assert data["status"] == "failed"  # Genuine pytest KeyboardInterrupt exit 2 wins.
    assert data["exit_origin"] == "runner"
    _no_claims(data)
    _released(domain)
    (root / "first-release").touch()
    assert case.invoke(domain, root, "--", "tests", timeout=10).code == 0


def test_real_report_is_consumed_after_quiescence_and_only_once(case, monkeypatch):
    domain = case.domain()
    root = _project(case, domain)
    consume = reports.consume_report
    seen = []
    def observe(binding):
        assert scheduler.reconcile(domain)[0].state is C.LeaseState.FINALIZING
        native = consume(binding)
        seen.append((binding, native))
        return native
    monkeypatch.setattr(reports, "consume_report", observe)
    result = operations.execute(domain, config_api.resolve_config(root).config,
                                C.RunRequest(mode=C.Mode.SCOPED, argv=("tests",)))
    assert result.status is C.Status.PASSED
    assert len(seen) == 1
    binding, native = seen[0]
    assert native.observed_runtime_version == "9.1.1"
    assert not binding.path.exists()
    with pytest.raises(C.Problem):
        consume(binding)
    _released(domain)


def test_real_early_bridge_refusal_is_not_a_native_test_failure(case, monkeypatch):
    domain = case.domain()
    root = _project(case, domain)
    launch = operations._launch_guard
    def invalid_grant(domain, grant, prepared, setup=None):
        prepared = replace(prepared, env_updates=prepared.env_updates + (("PTEST_GRANT_WORKERS", "0"),))
        return launch(domain, grant, prepared, setup)
    monkeypatch.setattr(operations, "_launch_guard", invalid_grant)
    result = operations.execute(domain, config_api.resolve_config(root).config,
                                C.RunRequest(mode=C.Mode.SCOPED, argv=("tests",)))
    assert result.status is C.Status.INCOMPLETE
    assert result.runner_exit_code == result.exit_code == 4
    assert result.exit_origin == "ptest"
    assert any(reason.code == "unsupported-capability" for reason in result.reasons)
    assert not (root / "tests-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _released(domain)


def test_real_guard_report_name_mismatch_is_incomplete(case, monkeypatch):
    domain = case.domain()
    root = _project(case, domain)
    launch = operations._launch_guard
    def mismatch(*args):
        child, control, frames = launch(*args)
        frames.expected_report_name = "native-a001-" + "0" * 32 + ".json"
        return child, control, frames
    monkeypatch.setattr(operations, "_launch_guard", mismatch)
    result = operations.execute(domain, config_api.resolve_config(root).config,
                                C.RunRequest(mode=C.Mode.SCOPED, argv=("tests",)))
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert any(reason.code == "protocol-mismatch" for reason in result.reasons)
    assert result.source_valid is result.full_gate_eligible is result.baseline_published is False


def test_real_late_execution_plugin_is_refused_before_collection(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "from pathlib import Path\n"
        "class Executor:\n"
        "    def pytest_runtestloop(self):\n        Path('executor-ran').touch()\n        return True\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Executor(), 'late-executor')\n"
    ))
    (root / "tests/test_native.py").write_text("from pathlib import Path\nPath('collected').touch()\ndef test_one(): pass\n")
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert _data(result)["status"] == "incomplete"
    assert not (root / "collected").exists()
    assert not (root / "executor-ran").exists()
    _released(domain)


def test_real_repeat_plugin_cannot_bypass_gate_without_execution_hooks(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "from pathlib import Path\n"
        "class Repeat:\n"
        "    def pytest_generate_tests(self, metafunc):\n        Path('repeat-ran').touch()\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Repeat(), 'repeat')\n"
    ))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert _data(result)["status"] == "incomplete"
    assert not (root / "repeat-ran").exists()
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_real_execution_hook_alias_cannot_bypass_plugin_qualification(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.hookimpl(specname='pytest_runtestloop')\n"
        "def pytest_aliased_executor():\n    Path('alias-ran').touch()\n    return True\n"
    ))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert _data(result)["status"] == "incomplete"
    assert not (root / "alias-ran").exists()
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_real_custom_reporter_and_cleanup_hooks_are_preserved(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "from pathlib import Path\n"
        "def pytest_terminal_summary(terminalreporter):\n    terminalreporter.write_line('custom-report-preserved')\n"
        "def pytest_unconfigure(config):\n    Path('cleanup-ran').touch()\n"
    ))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert result.code == 0, result.stderr.decode()
    assert b"custom-report-preserved" in result.stdout
    assert (root / "cleanup-ran").exists()
    assert not list((domain.root / "checkouts").glob("*/reports/*"))
    _released(domain)


@pytest.mark.parametrize("failure", [False, True], ids=["zero", "nonzero"])
def test_real_report_write_collision_is_incomplete_without_overwrite(case, failure):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import os\nfrom pathlib import Path\n"
        "path = Path(os.environ['PTEST_PYTEST_REPORT_PATH'])\n"
        "path.write_text('existing report sentinel')\npath.chmod(0o600)\n"
    ))
    result = case.invoke(domain, root, "--", "tests", timeout=10,
                         env={"FIXTURE_FAILURE": str(int(failure))})
    assert result.code == (1 if failure else 70)
    assert _data(result)["status"] == "incomplete"
    assert _data(result)["runner_exit_code"] == int(failure)
    paths = list((domain.root / "checkouts").glob("*/reports/*"))
    assert len(paths) == 1
    assert paths[0].read_text() == "existing report sentinel"
    _released(domain)


def test_real_source_change_retains_reason_without_source_valid_claim(case):
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "from pathlib import Path\n"
        "def pytest_sessionfinish(session, exitstatus):\n    Path('tracked-input.txt').write_text('changed')\n"
    ))
    (root / "tracked-input.txt").write_text("original")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\ntests-ran\nptest-result-*\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")
    for args in (("init",), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args],
                       cwd=root, env=env, capture_output=True, check=True, timeout=5)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert result.code == 0
    assert any(reason["code"] == "changed-during-run" for reason in data["reasons"])
    assert (root / "tracked-input.txt").read_text() == "changed"
    _no_claims(data)
    _released(domain)


def test_real_xdist_plugin_is_refused_when_preprovisioned(case):
    interpreter = os.environ.get("PTEST_TEST_XDIST_PYTHON")
    if interpreter is None:
        pytest.skip("unqualified: no preprovisioned xdist interpreter supplied")
    probe = subprocess.run([interpreter, "-c", "import pytest, xdist; print(pytest.__version__)"],
                           check=True, capture_output=True, text=True, timeout=5)
    assert probe.stdout.strip() in VERSIONS
    domain = case.domain()
    root = _project(case, domain, allow_xdist=True)
    path = root / ".ptest.toml"
    path.write_text(path.read_text().replace(json.dumps([_interpreter()]), json.dumps([interpreter])))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert _data(result)["status"] == "incomplete"
    assert _data(result)["exit_origin"] == "ptest"
    assert b"xdist is not owned" in result.stderr
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_real_renamed_xdist_controller_is_refused(case):
    """A real xdist module remains owned-bound even under a project alias."""
    interpreter = os.environ.get("PTEST_TEST_XDIST_PYTHON")
    if interpreter is None:
        pytest.skip("unqualified: no preprovisioned xdist interpreter supplied")
    probe = subprocess.run(
        [interpreter, "-c", "import pytest, xdist; print(pytest.__version__)"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert probe.stdout.strip() in VERSIONS
    domain = case.domain()
    root = _project(case, domain, allow_xdist=True, conftest=(
        "import xdist.plugin as _renamed_xdist\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(_renamed_xdist, 'renamed-xdist-controller')\n"
    ))
    path = root / ".ptest.toml"
    path.write_text(path.read_text().replace(
        json.dumps([_interpreter()]), json.dumps([interpreter])))
    result = case.invoke(
        domain, root, "--", "tests", timeout=10,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert _data(result)["status"] == "incomplete"
    assert _data(result)["exit_origin"] == "ptest"
    assert b"xdist is not owned" in result.stderr
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_foreign_object_under_blocked_looponfail_name_is_refused(case):
    """The blocked loop-on-fail name never authenticates an arbitrary object."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "class ForeignController:\n"
        "    def pytest_runtest_call(self, item):\n"
        "        open('foreign-hook-ran', 'w').close()\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.unregister(name='xdist.looponfail')\n"
        "    config.pluginmanager.register(ForeignController(), 'xdist.looponfail')\n"
    ))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert b"unqualified pytest execution hook" in result.stderr
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_real_unsupported_runtime_is_refused_when_preprovisioned(case):
    interpreter = os.environ.get("PTEST_TEST_UNSUPPORTED_PYTEST_PYTHON")
    if interpreter is None:
        pytest.skip("unqualified: no preprovisioned unsupported pytest interpreter supplied")
    probe = subprocess.run([interpreter, "-c", "import pytest; print(pytest.__version__)"],
                           check=True, capture_output=True, text=True, timeout=5)
    assert probe.stdout.strip() not in VERSIONS
    domain = case.domain()
    root = _project(case, domain)
    path = root / ".ptest.toml"
    path.write_text(path.read_text().replace(json.dumps([_interpreter()]), json.dumps([interpreter])))
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert result.code == data["runner_exit_code"] == 4
    assert b"unsupported-capability" in result.stderr
    assert not (root / "tests-ran").exists()
    _released(domain)


def test_q_py_full_with_conftest_hook_runs_labelled(case):
    """Advanced-profile full path twin: a conftest hook runs labelled."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, launcher=_coverage_launcher())
    config = root / ".ptest.toml"
    project_id = tomllib.loads(config.read_text())["project_id"]
    config.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps(list(_coverage_launcher()))}\n'
        'args = ["-s", "-p", "no:xdist", "--cov=project_module", "--cov-report=term"]\n'
        'full_args = []\ntest_roots = ["tests/test_native.py"]\nworkers = 8\n'
        'lifecycle = "cooperative-process-group"\n'
        '[selection]\nenabled = true\nclosed_inputs = true\n'
        'non_input_outputs = ["tests-ran", ".coverage", ".pytest_cache", "__pycache__", '
        '"tests/__pycache__", "ptest-result-q-py-full-hook.json"]\n'
        'input_roots = ["project_module.py"]\n'
        'groups = [{ name = "native", sources = ["project_module.py"], '
        'tests = ["tests/test_native.py"] }]\n'
    )
    (root / "tests" / "conftest.py").write_text(
        "def pytest_pycollect_makeitem():\n    pass\n")
    support.init_git_repo(root)
    completed = case.invoke(
        domain, root, "--result-json", "ptest-result-q-py-full-hook.json", "--full", timeout=30)
    data = _data(completed)
    label = "full (project-filtered: conftest collection hook)"
    assert completed.code == 0, completed.stderr.decode()
    # workers = 8 is ignored for pytest without a tier admission: the
    # unadmitted advanced full run requests a single serial slot.
    assert data["granted_workers"] == 1
    assert data["command"]["workers"] == 1
    assert label in completed.stderr.decode()
    assert data["status"] == "passed"
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


def _round16_scoped_failing(project_root):
    (project_root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_fails():\n    Path('fail.marker').write_text('ran')\n"
        "    assert False\n")


def _round16_scoped_refused(result, root):
    assert result.code == 4, result.stderr.decode()
    assert b"ptest-bridge-refusal" in result.stderr
    data = _data(result)
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert (root / "fail.marker").read_text() == "ran"


def test_real_scoped_sessionfinish_exit_call_forces_zero_is_refused(case):
    """Round 16 (sf_exit): pytest.exit(returncode=0) in sessionfinish hides nothing."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    pytest.exit('forced', returncode=0)\n"))
    _round16_scoped_failing(root)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    _round16_scoped_refused(result, root)


def test_real_scoped_sessionfinish_wrapper_rewrite_is_refused(case):
    """Round 16 (sf_wraptf): a tryfirst wrapper clearing exitstatus hides nothing."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    result = yield\n"
        "    session.exitstatus = 0\n"
        "    return result\n"))
    _round16_scoped_failing(root)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    _round16_scoped_refused(result, root)


def test_real_scoped_sessionfinish_old_wrapper_rewrite_is_refused(case):
    """Round 16 (sf_oldwrap): a hookwrapper clearing exitstatus hides nothing."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    outcome = yield\n"
        "    session.exitstatus = 0\n"))
    _round16_scoped_failing(root)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    _round16_scoped_refused(result, root)


def test_real_scoped_unconfigure_exitstatus_reset_is_refused(case):
    """Round 16 (unconf_set): pytest_unconfigure clearing exitstatus hides nothing."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "SEEN = {}\n"
        "def pytest_sessionstart(session):\n"
        "    SEEN['session'] = session\n"
        "def pytest_unconfigure(config):\n"
        "    SEEN['session'].exitstatus = 0\n"))
    _round16_scoped_failing(root)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    _round16_scoped_refused(result, root)


def test_real_scoped_add_cleanup_exitstatus_reset_is_refused(case):
    """Round 16 (cleanup_set): config.add_cleanup clearing exitstatus hides nothing."""
    domain = case.domain()
    root = _project(case, domain, conftest=(
        "def pytest_sessionstart(session):\n"
        "    session.config.add_cleanup(lambda: setattr(session, 'exitstatus', 0))\n"))
    _round16_scoped_failing(root)
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    _round16_scoped_refused(result, root)


def test_real_scoped_xfail_and_skip_stay_passing(case):
    """Round 16: outcome counting leaves xfail/skip green (exit 0)."""
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests" / "test_native.py").write_text(
        "import pytest\n"
        "@pytest.mark.xfail(reason='known', strict=False)\n"
        "def test_known():\n    assert False\n"
        "@pytest.mark.skip(reason='skipped')\n"
        "def test_skipped():\n    assert False\n"
        "def test_ok():\n    assert True\n")
    result = case.invoke(domain, root, "--", "tests", timeout=10)
    assert result.code == 0, result.stderr.decode()
    assert b"ptest-bridge-refusal" not in result.stderr
    data = _data(result)
    assert data["status"] == "passed"
    assert data["runner_exit_code"] == 0
    assert data["exit_origin"] == "runner"
    _no_claims(data)
    _released(domain)


def test_real_scoped_deselect_all_exit_five_passes_through(case):
    """Round 16: exit 5 (nothing collected) keeps its native outcome."""
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests" / "test_native.py").write_text(
        "def test_only():\n    assert True\n")
    result = case.invoke(domain, root, "--", "tests",
                         "--deselect", "tests/test_native.py::test_only", timeout=10)
    assert result.code == 5, result.stderr.decode()
    assert b"ptest-bridge-refusal" not in result.stderr
    data = _data(result)
    assert data["runner_exit_code"] == 5
    assert data["exit_origin"] == "runner"
    _released(domain)


def test_real_scoped_maxfail_stop_after_failure_stays_failure(case):
    """Round 16: -x stopping after a failure stays a native failure."""
    domain = case.domain()
    root = _project(case, domain)
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    assert False\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    result = case.invoke(domain, root, "--", "tests", "-x", timeout=10)
    assert result.code == 1, result.stderr.decode()
    assert b"ptest-bridge-refusal" not in result.stderr
    data = _data(result)
    assert data["status"] == "failed"
    assert data["runner_exit_code"] == 1
    assert data["exit_origin"] == "runner"
    assert not (root / "b.marker").exists()
    _released(domain)
