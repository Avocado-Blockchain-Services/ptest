"""Bridge recording and deselect twins (dynamic-selection T4).

Real-subprocess tests run the bridge file (``__main__`` controller, or
``-p pytest_bridge`` xdist workers) against the fixture project in
``tests/ng/fixtures/selection_project`` with a private report dir and
binding env, launched like ``test_stall_bridge.py``. Serial and ``-n 2``
runs assert F/M/X/D/opaque attribution and outcomes; the deselect
binding deselects exactly the listed ids; invalid bindings run everything;
and argv/outcomes/exit/coverage are identical with and without recording.

Every twin carries an explicit timeout of at most 60 s. File polls use a
watchdog deadline; no test synchronises on a bare sleep.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys

from pathlib import Path

import pytest

from ptest.runtime import pytest_bridge as bridge

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "src" / "ptest" / "runtime" / "pytest_bridge.py"
PROTOCOL = REPO_ROOT / "src" / "ptest" / "runtime" / "protocol-v1.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "selection_project"

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
    "PTEST_SELECTION_RECORD", "PTEST_SELECTION_DESELECT",
)

_HAS_BARRIER = hasattr(__import__("ptest.contracts", fromlist=["x"]), "SELECTION_PROTOCOL")
_NEEDS_BARRIER = pytest.mark.skipif(
    not _HAS_BARRIER, reason="awaiting contracts barrier (T5)")

_INGEST_SPEC = importlib.util.find_spec("ptest.selection_ingest")
_NEEDS_INGEST = pytest.mark.skipif(
    _INGEST_SPEC is None, reason="awaiting selection_ingest (T3)")

_XDIST_SPEC = importlib.util.find_spec("xdist")
_NEEDS_XDIST = pytest.mark.skipif(
    _XDIST_SPEC is None, reason="pytest-xdist is not installed")

_COV_SPEC = importlib.util.find_spec("pytest_cov")
_NEEDS_COV = pytest.mark.skipif(
    _COV_SPEC is None, reason="pytest-cov is not installed")


def _hex(n: int) -> str:
    return secrets.token_hex(n // 2)


class TwinResult:
    def __init__(self, returncode, stdout, stderr, report, report_path,
                 run_id):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.report = report
        self.report_path = report_path
        self.run_id = run_id


def _copy_fixture_project(target: Path) -> Path:
    """Copy the .txt-suffixed fixture tree, stripping the suffix."""
    for source in sorted(FIXTURES.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(FIXTURES)
        name = relative.name
        assert name.endswith(".txt") or relative.suffix == ".json", relative
        if name.endswith(".txt"):
            relative = relative.with_name(name[:-4])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    assert (target / "pkg" / "core.py").exists()
    assert (target / "tests" / "test_core.py").exists()
    return target


def _run_bridge(root: Path, argv: list[str], *, workers: int = 1,
                extra_env: dict | None = None, timeout: float = 60,
                record: bool = False, bridge_path: Path = BRIDGE,
                run_id: str | None = None) -> TwinResult:
    """Run the bridge file directly with a valid executor binding."""
    assert timeout <= 60
    root = root.resolve()
    reports = root / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    os.chmod(reports, 0o700)
    run_id = run_id or _hex(32)
    nonce = _hex(64)
    attempt = "a001"
    report_path = reports / f"native-{attempt}-{run_id}.json"
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
    if record:
        child_env["PTEST_SELECTION_RECORD"] = "1"
    for key, value in (extra_env or {}).items():
        child_env[key] = value
    completed = subprocess.run(
        [sys.executable, str(bridge_path), *argv],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    report = None
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    return TwinResult(completed.returncode,
                      completed.stdout.decode("utf-8", errors="replace"),
                      completed.stderr.decode("utf-8", errors="replace"),
                      report, report_path, run_id)


def _read_deps(report_path: Path) -> list[tuple[Path, dict]]:
    found = []
    for path in sorted(report_path.parent.glob(report_path.name + ".deps-*")):
        found.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return found


def _write_binding(report_path: Path, run_id: str, nodeids,
                   *, raw: bytes | None = None, mode: int = 0o600) -> Path:
    """Write the private deselect binding like operations/T3 would."""
    path = Path(str(report_path) + ".deselect")
    if raw is None:
        raw = json.dumps(
            {"format": "ptest-selection-deselect-v1",
             "run_id": run_id, "nodeids": list(nodeids)},
            ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)
    return path


def _summary_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in re.finditer(r"(\d+) (failed|passed|skipped|xfailed|xpassed|error|deselected)",
                             stdout):
        counts[match.group(2)] = int(match.group(1))
    return counts


# ---------------------------------------------------------------------------
# Duplicated literals.
# ---------------------------------------------------------------------------

def test_bridge_selection_literals_match_recorder():
    from ptest.runtime import selection_recorder as recorder

    assert bridge._SELECTION_DEPS_INFIX == recorder._DEPS_INFIX
    assert bridge._SELECTION_DEPS_FORMAT == recorder._DEPS_FORMAT
    assert bridge._SELECTION_DESELECT_SUFFIX == recorder._DESELECT_SUFFIX
    assert bridge._SELECTION_DESELECT_FORMAT == recorder._DESELECT_FORMAT
    assert bridge._SELECTION_DESELECT_MAX_BYTES == recorder._DESELECT_MAX_BYTES
    assert bridge._SELECTION_RECORD_ENV == recorder._RECORD_ENV
    assert bridge._SELECTION_DESELECT_ENV == recorder._DESELECT_ENV
    assert bridge._SELECTION_TOOL_NAME == recorder._TOOL_NAME


@_NEEDS_BARRIER
def test_bridge_selection_literals_match_contracts():
    from ptest import contracts as C

    assert bridge._SELECTION_DEPS_INFIX == C.SELECTION_DEPS_INFIX
    assert bridge._SELECTION_DEPS_FORMAT == C.SELECTION_DEPS_FORMAT
    assert bridge._SELECTION_DESELECT_SUFFIX == C.SELECTION_DESELECT_SUFFIX
    assert bridge._SELECTION_DESELECT_FORMAT == C.SELECTION_DESELECT_FORMAT
    assert bridge._SELECTION_DESELECT_MAX_BYTES == C.SELECTION_DESELECT_MAX_BYTES
    assert bridge._SELECTION_DESELECT_MAX_IDS == C.SELECTION_DESELECT_MAX_IDS
    assert bridge._SELECTION_RECORD_ENV == C.SELECTION_RECORD_ENV
    assert bridge._SELECTION_DESELECT_ENV == C.SELECTION_DESELECT_ENV
    assert bridge._SELECTION_TOOL_NAME == C.SELECTION_TOOL_NAME


def test_deselect_binding_refused_outside_scoped(tmp_path, monkeypatch):
    """The binding never applies outside scoped execution (unit)."""
    report = tmp_path / "rep.json"
    _write_binding(report, "a" * 32, ["t.py::t1"])
    monkeypatch.setenv("PTEST_SELECTION_DESELECT", str(report) + ".deselect")
    monkeypatch.setenv("PTEST_EXECUTION", "full")
    nodeids, status = bridge._read_deselect_binding(report, "a" * 32)
    assert (nodeids, status) == (None, "ignored")
    monkeypatch.delenv("PTEST_SELECTION_DESELECT")
    nodeids, status = bridge._read_deselect_binding(report, "a" * 32)
    assert (nodeids, status) == (None, "none")


# ---------------------------------------------------------------------------
# Serial recording: F/M/X/D/opaque and outcomes.
# ---------------------------------------------------------------------------

# pytest escapes non-ASCII and newlines in node ids (literal backslash
# sequences), so the recorded and bound ids use the escaped forms.
_PARAM_UNICODE = "tests/test_core.py::test_parametrized[\\xfcn\\xefcode-\\xdf]"
_NEWLINE_ID = "tests/test_core.py::test_newline_id[line1\\nline2]"

_ALL_NODEIDS = [
    "tests/test_core.py::test_add",
    "tests/test_core.py::test_lazy_import",
    "tests/test_core.py::test_reads_data",
    "tests/test_core.py::test_spawns_python",
    "tests/test_core.py::test_runs_true",
    "tests/test_core.py::test_fails",
    "tests/test_core.py::test_setup_error",
    "tests/test_core.py::test_skips",
    "tests/test_core.py::test_xfail",
    "tests/test_core.py::test_parametrized[plain[0]]",
    _PARAM_UNICODE,
    _NEWLINE_ID,
]

_EXPECTED_OUTCOMES = {
    "tests/test_core.py::test_add": "passed",
    "tests/test_core.py::test_lazy_import": "passed",
    "tests/test_core.py::test_reads_data": "passed",
    "tests/test_core.py::test_spawns_python": "passed",
    "tests/test_core.py::test_runs_true": "passed",
    "tests/test_core.py::test_fails": "failed",
    "tests/test_core.py::test_setup_error": "error",
    "tests/test_core.py::test_skips": "skipped",
    "tests/test_core.py::test_xfail": "xfailed",
    "tests/test_core.py::test_parametrized[plain[0]]": "passed",
    _PARAM_UNICODE: "passed",
    _NEWLINE_ID: "passed",
}


def _decode(payload: dict) -> dict[str, dict]:
    """Decode one deps payload into name-based node records."""
    paths = payload["paths"]
    functions = [(paths[entry[0]], entry[1], entry[2])
                 for entry in payload["functions"]]
    fixture_list = payload["fixtures"]
    decoded = {}
    for node in payload["nodes"]:
        decoded[node["nodeid"]] = {
            "outcome": node["outcome"],
            "opaque": node["opaque"],
            "functions": {functions[index] for index in node["functions"]},
            "modules": {paths[index] for index in node["modules"]},
            "data": {paths[index] for index in node["data"]},
            # Frozen §2.6: node fixture refs are indexes into the
            # top-level fixtures array.
            "fixtures": [tuple(fixture_list[index]["key"])
                         for index in node["fixtures"]],
        }
    fixtures = {}
    for entry in fixture_list:
        fixtures[tuple(entry["key"])] = {
            "functions": {functions[index] for index in entry["functions"]},
            "modules": {paths[index] for index in entry["modules"]},
            "data": {paths[index] for index in entry["data"]},
            "opaque": entry["opaque"],
        }
    return {"nodes": decoded, "fixtures": fixtures}


def test_serial_recording_covers_kinds_and_outcomes(tmp_path):
    root = _copy_fixture_project(tmp_path / "proj")
    twin = _run_bridge(root, ["tests/test_core.py"], record=True)
    assert twin.returncode == 1, twin.stderr
    assert twin.report is not None
    deps = _read_deps(twin.report_path)
    assert len(deps) == 1
    path, payload = deps[0]
    assert path.name == twin.report_path.name + f".deps-{payload['pid']}"
    assert payload["format"] == "ptest-selection-deps-v1"
    assert payload["run_id"] == twin.run_id
    assert payload["role"] == "controller"
    assert payload["worker_id"] is None
    assert payload["recording"] is True
    assert payload["inactive_reason"] is None
    assert payload["overflow"] is False
    assert payload["tamper"] is False
    assert payload["deselect"] == "none"
    assert payload["deselected"] == 0
    assert payload["python"] == list(sys.version_info[:2])
    decoded = _decode(payload)
    assert set(decoded["nodes"]) == set(_ALL_NODEIDS), twin.stdout
    for nodeid, outcome in _EXPECTED_OUTCOMES.items():
        assert decoded["nodes"][nodeid]["outcome"] == outcome, nodeid
    # F(T): direct calls.
    assert ("pkg/core.py", "add", 10) in decoded["nodes"][
        "tests/test_core.py::test_add"]["functions"]
    assert ("pkg/core.py", "greet", 15) in decoded["nodes"][
        "tests/test_core.py::test_fails"]["functions"]
    # M(T): the lazy module body executed in the test context.
    assert "pkg/lazy.py" in decoded["nodes"][
        "tests/test_core.py::test_lazy_import"]["modules"]
    # D(T): the data file was opened in the test context.
    assert "data/config.json" in decoded["nodes"][
        "tests/test_core.py::test_reads_data"]["data"]
    # Opaque: spawning this interpreter; clean: an outside binary.
    assert decoded["nodes"][
        "tests/test_core.py::test_spawns_python"]["opaque"] is True
    assert decoded["nodes"][
        "tests/test_core.py::test_runs_true"]["opaque"] is False
    # The clean spawn leaves no project functions in the test context
    # beyond the test body itself.
    assert {entry[1] for entry in decoded["nodes"][
        "tests/test_core.py::test_runs_true"]["functions"]} == {"test_runs_true"}


def test_session_fixture_call_belongs_to_fixture_key(tmp_path):
    """X(T): boot() is attributed to the fixture key, not the first test."""
    root = _copy_fixture_project(tmp_path / "proj")
    twin = _run_bridge(root, ["tests/test_core.py"], record=True)
    assert twin.returncode == 1, twin.stderr
    (_, payload), = _read_deps(twin.report_path)
    decoded = _decode(payload)
    keys = [key for key in decoded["fixtures"] if key[1] == "booted"]
    assert len(keys) == 1
    key = keys[0]
    assert key[2] == "session"
    assert ("pkg/core.py", "boot", 5) in decoded["fixtures"][key]["functions"]
    assert key in decoded["nodes"]["tests/test_core.py::test_add"]["fixtures"]
    assert ("pkg/core.py", "boot", 5) not in decoded["nodes"][
        "tests/test_core.py::test_add"]["functions"]


# ---------------------------------------------------------------------------
# Deselect: exact ids out, reported as deselected, never as passed.
# ---------------------------------------------------------------------------

def _run_with_binding(root: Path, nodeids, *, raw=None, env_path=None,
                      record=True):
    """Write the binding first (like operations does), then run the bridge."""
    run_id = _hex(32)
    report_path = root / "reports" / f"native-a001-{run_id}.json"
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(report_path.parent, 0o700)
    binding = _write_binding(report_path, run_id, nodeids, raw=raw)
    target = env_path if env_path is not None else binding
    twin = _run_bridge(root, ["tests/test_core.py"], record=record,
                       extra_env={"PTEST_SELECTION_DESELECT": str(target)},
                       run_id=run_id)
    assert twin.report_path.name == report_path.name
    return twin, binding


def test_deselect_binding_deselects_exactly(tmp_path):
    root = _copy_fixture_project(tmp_path / "proj")
    dropped = ["tests/test_core.py::test_fails",
               "tests/test_core.py::test_setup_error",
               _PARAM_UNICODE,
               _NEWLINE_ID,
               "tests/test_core.py::test_does_not_exist"]
    twin, _ = _run_with_binding(root, dropped)
    assert twin.returncode == 0, twin.stdout + twin.stderr
    assert "4 deselected" in twin.stdout, twin.stdout
    (_, payload), = _read_deps(twin.report_path)
    assert payload["run_id"] == twin.run_id
    assert payload["deselect"] == "applied"
    assert payload["deselected"] == 4
    decoded = _decode(payload)
    for nodeid in dropped[:4]:
        assert nodeid not in decoded["nodes"], nodeid
    assert (set(decoded["nodes"])
            == set(_ALL_NODEIDS) - set(dropped[:4])), twin.stdout


@pytest.mark.parametrize("variant", [
    "wrong-format", "wrong-run", "not-a-list", "truncated",
    "symlink", "mode-0644", "env-mismatch",
])
def test_invalid_binding_runs_everything(tmp_path, variant):
    root = _copy_fixture_project(tmp_path / "proj")
    run_id = _hex(32)
    report = root / "reports" / f"native-a001-{run_id}.json"
    report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(report.parent, 0o700)
    binding = Path(str(report) + ".deselect")
    nodeids = ["tests/test_core.py::test_fails"]
    if variant == "wrong-format":
        _write_binding(report, run_id, nodeids,
                       raw=json.dumps({"format": "nope", "run_id": run_id,
                                       "nodeids": nodeids}).encode())
    elif variant == "wrong-run":
        _write_binding(report, "0" * 32, nodeids)
    elif variant == "not-a-list":
        _write_binding(report, run_id, nodeids,
                       raw=json.dumps({"format": "ptest-selection-deselect-v1",
                                       "run_id": run_id,
                                       "nodeids": {"a": 1}}).encode())
    elif variant == "truncated":
        _write_binding(report, run_id, nodeids,
                       raw=b'{"format": "ptest-selection-dese')
    elif variant == "symlink":
        target = tmp_path / "evil.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(target, binding)
    elif variant == "mode-0644":
        _write_binding(report, run_id, nodeids, mode=0o644)
    elif variant == "env-mismatch":
        _write_binding(report, run_id, nodeids)
        binding = tmp_path / "elsewhere.deselect"
    second = _run_bridge(
        root, ["tests/test_core.py"], record=True,
        extra_env={"PTEST_SELECTION_DESELECT": str(binding)},
        run_id=run_id)
    assert second.returncode == 1, second.stdout + second.stderr
    assert "deselected" not in second.stdout
    (_, payload), = _read_deps(second.report_path)
    assert payload["deselect"] == "ignored"
    assert payload["deselected"] == 0
    assert set(_decode(payload)["nodes"]) == set(_ALL_NODEIDS)


# ---------------------------------------------------------------------------
# N6: argv, outcomes, exit status and output are unchanged by recording.
# ---------------------------------------------------------------------------

def test_outcome_parity_with_and_without_recording(tmp_path):
    root = _copy_fixture_project(tmp_path / "proj")
    recorded = _run_bridge(root, ["tests/test_core.py"], record=True)
    plain = _run_bridge(root, ["tests/test_core.py"], record=False)
    assert recorded.returncode == plain.returncode == 1
    assert _summary_counts(recorded.stdout) == _summary_counts(plain.stdout)
    assert _summary_counts(recorded.stdout) != {}
    assert recorded.report is not None and plain.report is not None
    # Without the record env there is no dependency file at all.
    assert _read_deps(plain.report_path) == []
    assert _read_deps(recorded.report_path) != []


def _total_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("TOTAL"):
            return line
    return ""


@_NEEDS_XDIST
@_NEEDS_COV
def test_coverage_totals_identical_with_recording(tmp_path):
    """N6: --cov totals match with and without the recorder (xdist)."""
    root = _copy_fixture_project(tmp_path / "proj")
    argv = ["-n", "2", "--cov=pkg", "--cov-report=term",
            "tests/test_core.py"]
    recorded = _run_bridge(root, argv, workers=2, record=True)
    plain = _run_bridge(root, argv, workers=2, record=False)
    assert recorded.report is not None, recorded.stderr
    assert plain.report is not None, plain.stderr
    assert recorded.returncode == plain.returncode
    assert _total_line(recorded.stdout) != ""
    assert _total_line(recorded.stdout) == _total_line(plain.stdout)


# ---------------------------------------------------------------------------
# xdist: controller plus one file per worker.
# ---------------------------------------------------------------------------

@_NEEDS_XDIST
def test_xdist_recording_merges_workers(tmp_path):
    root = _copy_fixture_project(tmp_path / "proj")
    twin = _run_bridge(root, ["-n", "2", "tests/test_core.py"],
                       workers=2, record=True)
    assert twin.returncode == 1, twin.stderr
    deps = _read_deps(twin.report_path)
    assert len(deps) == 3, [path.name for path, _ in deps]
    by_role = {}
    for _, payload in deps:
        assert payload["run_id"] == twin.run_id
        assert payload["recording"] is True
        by_role.setdefault(payload["role"], []).append(payload)
    assert set(by_role) == {"controller", "worker"}
    (controller,) = by_role["controller"]
    assert controller["worker_id"] is None
    assert sorted(controller["workers"]) == ["gw0", "gw1"]
    assert controller["nodes"] == []
    merged: dict[str, dict] = {}
    for payload in by_role["worker"]:
        assert payload["worker_id"] in ("gw0", "gw1")
        assert payload["workers"] == []
        for node in payload["nodes"]:
            assert node["nodeid"] not in merged
            merged[node["nodeid"]] = node
    assert set(merged) == set(_ALL_NODEIDS), twin.stdout
    for nodeid, outcome in _EXPECTED_OUTCOMES.items():
        assert merged[nodeid]["outcome"] == outcome, nodeid
    opaque = {nodeid for nodeid, node in merged.items() if node["opaque"]}
    assert "tests/test_core.py::test_spawns_python" in opaque
    assert "tests/test_core.py::test_runs_true" not in opaque
    # A session-fixture record exists on the workers that set it up, and
    # at least one node refs it by fixtures-array index (frozen §2.6).
    seen_booted_ref = False
    for worker_payload in by_role["worker"]:
        keys = [tuple(entry["key"]) for entry in worker_payload["fixtures"]]
        for node in worker_payload["nodes"]:
            for index in node["fixtures"]:
                assert isinstance(index, int)
                assert 0 <= index < len(keys)
                if keys[index][1] == "booted" and keys[index][2] == "session":
                    seen_booted_ref = True
    assert seen_booted_ref


# ---------------------------------------------------------------------------
# Shadowed bridge: without its sibling the run still passes unrecorded.
# ---------------------------------------------------------------------------

def test_shadowed_bridge_runs_unrecorded(tmp_path):
    root = _copy_fixture_project(tmp_path / "proj")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    # The bridge file plus its frozen descriptor, but not the recorder
    # sibling: recording must degrade, the run must not.
    shutil.copyfile(BRIDGE, shadow / "pytest_bridge.py")
    shutil.copyfile(PROTOCOL, shadow / "protocol-v1.json")
    twin = _run_bridge(root, ["tests/test_core.py"], record=True,
                       bridge_path=shadow / "pytest_bridge.py")
    assert twin.returncode == 1, twin.stderr
    deps = _read_deps(twin.report_path)
    assert len(deps) == 1
    _, payload = deps[0]
    assert payload["recording"] is False
    assert payload["inactive_reason"] == "recorder could not start"
    assert payload["nodes"] == []


@_NEEDS_INGEST
def test_ingest_accepts_real_serial_output(tmp_path):
    """Cross-check: T3 ingest reads the real serial file as complete."""
    from ptest import selection_ingest
    root = _copy_fixture_project(tmp_path / "proj")
    twin = _run_bridge(root, ["tests/test_core.py"], record=True)
    assert twin.returncode == 1, twin.stderr
    run = selection_ingest.read_run(twin.report_path, run_id=twin.run_id,
                                    expected_workers=1)
    assert run.complete is True
    assert run.recording is True


@_NEEDS_INGEST
@_NEEDS_XDIST
def test_ingest_accepts_real_xdist_output(tmp_path):
    """Cross-check: T3 ingest reads the real xdist files as complete."""
    from ptest import selection_ingest
    root = _copy_fixture_project(tmp_path / "proj")
    twin = _run_bridge(root, ["-n", "2", "tests/test_core.py"],
                       workers=2, record=True)
    assert twin.returncode == 1, twin.stderr
    run = selection_ingest.read_run(twin.report_path, run_id=twin.run_id,
                                    expected_workers=2)
    assert run.complete is True
    assert run.recording is True
