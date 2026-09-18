"""Real scoped guard/bridge/pytest/report acceptance; no fixture installs.

The controller interpreter is already provisioned. Other candidate interpreters
can be supplied explicitly with PTEST_TEST_PYTHON_<version with underscores>.
Unavailable tuples are skipped and remain unqualified, never installed here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def _project(case, domain, *, version="9.1.1", args=(), conftest=""):
    root = case.project(domain, kind="pytest")
    config_path = root / ".ptest.toml"
    project_id = tomllib.loads(config_path.read_text())["project_id"]
    config_path.write_text(
        f'version = 1\nproject_id = "{project_id}"\n[runner]\nkind = "pytest"\n'
        f'launcher = {json.dumps([_interpreter(version)])}\n'
        f'args = {json.dumps(["-s", *args])}\nfull_args = ["--invalid-full-only"]\n'
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
    assert any(r["code"] == "native-config-invalid" for r in data["reasons"])
    assert (root / "conftest-loaded").exists()  # Qualification is before collection, not conftest import.
    assert not (root / "collected").exists()
    assert not (root / "unowned-hook-ran").exists()
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
        else: path.write_text(original.replace('args = ["-s"]', 'args = ["-q"]'))
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
    assert data["status"] != "passed"
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
