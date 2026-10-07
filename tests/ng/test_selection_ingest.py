"""Dependency-file ingest, deselect binding writer and cleanup (T3).

Covers the section 2.6 bridge file contract from the reading side:
serial/xdist merges, completeness, malformed-file abuse cases (never a
crash, never a wrong skip), qualname normalisation, duplicate-node merge,
the O_EXCL deselect writer and report-scoped cleanup.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import selection_ingest as I

RUN_ID = "ab" * 16
OTHER_RUN = "cd" * 16
REPORT = "native-a001-" + "ef" * 16 + ".json"


def _report(tmp_path: Path, name: str = REPORT) -> Path:
    reports = tmp_path / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    return reports / name


def _ctx(funcs=(), modules=(), data=(), opaque=False):
    return {"functions": list(funcs), "modules": list(modules),
            "data": list(data), "opaque": opaque}


def _node(nodeid, outcome="passed", fixtures=(), **ctx):
    entry = {"nodeid": nodeid, "outcome": outcome,
             "fixtures": list(fixtures)}
    entry.update(_ctx(**ctx))
    return entry


def _fixture(baseid, argname, scope="session", **ctx):
    entry = {"key": [baseid, argname, scope]}
    entry.update(_ctx(**ctx))
    return entry


def _payload(pid, run_id=RUN_ID, role="controller", worker_id=None,
             workers=(), paths=(), functions=(), fixtures=(), nodes=(),
             ambient=None, recording=True, inactive_reason=None,
             python=(3, 12), overflow=False, tamper=False):
    return {
        "format": I.DEPS_FORMAT, "run_id": run_id, "role": role,
        "worker_id": worker_id, "pid": pid, "python": list(python),
        "recording": recording, "inactive_reason": inactive_reason,
        "workers": list(workers), "overflow": overflow, "tamper": tamper,
        "deselect": "none", "deselected": 0,
        "paths": list(paths), "functions": [list(item) for item in functions],
        "ambient": ambient if ambient is not None else _ctx(),
        "fixtures": fixtures, "nodes": nodes,
    }


def _write_deps(report: Path, pid: int, payload, *, mode=0o600,
                raw: bytes | None = None) -> Path:
    path = Path(f"{report}{I.DEPS_INFIX}{pid}")
    data = raw if raw is not None else json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode)
    return path


def _serial(report, pid=100, **over):
    paths = ["tests/test_a.py", "pkg/a.py"]
    functions = [[1, "f", 10]]
    nodes = [_node("tests/test_a.py::test_one", "passed", funcs=[0])]
    payload = _payload(pid, paths=paths, functions=functions, nodes=nodes,
                       **over)
    return _write_deps(report, pid, payload)


# --- valid runs ------------------------------------------------------------

def test_serial_run_merges_complete(tmp_path):
    report = _report(tmp_path)
    _serial(report)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True
    assert run.recording is True
    assert run.python == (3, 12)
    assert set(run.nodes) == {"tests/test_a.py::test_one"}
    node = run.nodes["tests/test_a.py::test_one"]
    assert node.outcome == "passed"
    assert run.vocabulary.paths == ("pkg/a.py",)
    assert run.vocabulary.functions == ((0, "f"),)
    assert list(node.deps.functions) == [0]
    assert run.notes == ()


def test_xdist_run_merges_workers(tmp_path):
    report = _report(tmp_path)
    controller = _payload(100, role="controller", workers=["gw0", "gw1"],
                          paths=["tests/test_a.py"], functions=[],
                          nodes=[])
    _write_deps(report, 100, controller)
    worker0 = _payload(101, role="worker", worker_id="gw0",
                       paths=["tests/test_a.py", "pkg/a.py"],
                       functions=[[1, "f", 3]],
                       nodes=[_node("tests/test_a.py::test_one", "passed",
                                    funcs=[0])])
    _write_deps(report, 101, worker0)
    worker1 = _payload(102, role="worker", worker_id="gw1",
                       paths=["tests/test_a.py"],
                       functions=[],
                       nodes=[_node("tests/test_b.py::test_two", "failed",
                                    modules=[0])])
    _write_deps(report, 102, worker1)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=2)
    assert run.complete is True
    assert set(run.nodes) == {"tests/test_a.py::test_one",
                              "tests/test_b.py::test_two"}
    assert run.nodes["tests/test_b.py::test_two"].outcome == "failed"
    # Shared path strings share one vocabulary id.
    assert run.vocabulary.paths.count("tests/test_a.py") == 1


def test_qualname_normalisation_to_module_body(tmp_path):
    report = _report(tmp_path)
    payload = _payload(
        100, paths=["pkg/a.py"],
        functions=[[0, "<module>", 0], [0, "<lambda>", 5], [0, "f", 9]],
        nodes=[_node("tests/test_a.py::t", funcs=[0, 1, 2])])
    _write_deps(report, 100, payload)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True
    node = run.nodes["tests/test_a.py::t"]
    # Module-level records fold into the module-body set.
    assert list(node.deps.functions) == [0]
    assert list(node.deps.modules) == [0]


def test_duplicate_node_keeps_worst_outcome_and_union(tmp_path):
    report = _report(tmp_path)
    controller = _payload(100, role="controller", workers=["gw0", "gw1"],
                          paths=[], functions=[], nodes=[])
    _write_deps(report, 100, controller)
    _write_deps(report, 101, _payload(
        101, role="worker", worker_id="gw0", paths=["pkg/a.py", "pkg/b.py"],
        functions=[[0, "f", 1], [1, "g", 2]],
        nodes=[_node("tests/test_a.py::t", "passed", funcs=[0])]))
    _write_deps(report, 102, _payload(
        102, role="worker", worker_id="gw1", paths=["pkg/a.py", "pkg/b.py"],
        functions=[[0, "f", 1], [1, "g", 2]],
        nodes=[_node("tests/test_a.py::t", "failed", funcs=[1])]))
    run = I.read_run(report, run_id=RUN_ID, expected_workers=2)
    assert run.complete is True
    node = run.nodes["tests/test_a.py::t"]
    assert node.outcome == "failed"
    assert sorted(node.deps.functions) == [0, 1]


def test_fixture_keys_and_ambient_merge(tmp_path):
    report = _report(tmp_path)
    payload = _payload(
        100, paths=["pkg/a.py"],
        functions=[[0, "boot", 4]],
        fixtures=[_fixture("tests/conftest.py::boot", "boot", "session",
                            funcs=[0])],
        ambient=_ctx(modules=[0]),
        nodes=[_node("tests/test_a.py::t", fixtures=[0], funcs=[0])])
    _write_deps(report, 100, payload)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True
    assert run.vocabulary.fixtures == (
        ("tests/conftest.py::boot", "boot", "session"),)
    assert list(run.nodes["tests/test_a.py::t"].fixtures) == [0]
    assert list(run.ambient.modules) == [0]


# --- completeness ----------------------------------------------------------

def test_completeness_matrix(tmp_path):
    report = _report(tmp_path)
    # No files at all.
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is False
    _serial(report, pid=100)
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is True
    # A second valid controller breaks the serial "exactly one" rule.
    _serial(report, pid=101)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is False
    assert run.notes != ()


def test_xdist_missing_worker_wrong_count_overflow(tmp_path):
    report = _report(tmp_path)
    _write_deps(report, 100, _payload(
        100, role="controller", workers=["gw0", "gw1"]))
    _write_deps(report, 101, _payload(
        101, role="worker", worker_id="gw0"))
    run = I.read_run(report, run_id=RUN_ID, expected_workers=2)
    assert run.complete is False  # gw1 missing
    run = I.read_run(report, run_id=RUN_ID, expected_workers=3)
    assert run.complete is False  # wrong count
    _write_deps(report, 102, _payload(
        102, role="worker", worker_id="gw1", overflow=True))
    run = I.read_run(report, run_id=RUN_ID, expected_workers=2)
    assert run.complete is False  # overflow drops data


# --- malformed input: never raises, never a wrong skip ---------------------

def _serial_cases(report, pid=100, **over):
    return _payload(pid, paths=["tests/test_a.py"], functions=[],
                    nodes=[_node("tests/test_a.py::t")], **over)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(format="nope"),
    lambda p: p.update(run_id=OTHER_RUN),
    lambda p: p.update(pid=999),
    lambda p: p.update(role="librarian"),
    lambda p: p.pop("nodes"),
    lambda p: p.update(paths="not-a-list"),
    lambda p: p.update(nodes=[{"nodeid": 42}]),
    lambda p: p.update(nodes=[_node("tests/test_a.py::t", outcome="melted")]),
    lambda p: p.update(functions=[[0]]),
    lambda p: p.update(fixtures=[{"key": ["a"]}]),
])
def test_malformed_files_never_raise_and_never_complete(tmp_path, mutate):
    report = _report(tmp_path)
    payload = _serial_cases(report)
    mutate(payload)
    _write_deps(report, 100, payload)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is False
    assert run.notes != ()
    # Crucially no phantom selection data escapes a rejected file.
    assert run.nodes == {}


def test_truncated_json_is_incomplete(tmp_path):
    report = _report(tmp_path)
    payload = _serial_cases(report)
    raw = json.dumps(payload).encode("utf-8")[:len(json.dumps(payload)) // 2]
    _write_deps(report, 100, payload, raw=raw)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is False
    assert run.nodes == {}


def test_hostile_paths_make_contexts_opaque(tmp_path):
    report = _report(tmp_path)
    hostile = ["../evil.py", "/abs.py", "bad\x00.py", ".venv/lib/x.py",
               "tests/test_a.py"]
    payload = _payload(
        100, paths=hostile,
        functions=[[0, "f", 1], [1, "g", 2], [2, "h", 3], [3, "i", 4],
                   [4, "j", 5]],
        ambient=_ctx(data=[3]),
        nodes=[_node("tests/test_a.py::t", funcs=[4], data=[3]),
               _node("../evil.py::t", funcs=[0])])
    _write_deps(report, 100, payload)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True
    # Nothing dropped: both nodes present, hostile contexts opaque.
    assert set(run.nodes) == {"tests/test_a.py::t", "../evil.py::t"}
    assert run.nodes["tests/test_a.py::t"].deps.opaque is True
    assert run.nodes["../evil.py::t"].deps.opaque is True
    assert run.ambient.opaque is True


def test_private_file_abuse_refused(tmp_path, monkeypatch):
    report = _report(tmp_path)
    good = _write_deps(report, 100, _serial_cases(report))
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is True

    os.chmod(good, 0o644)
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is False
    os.chmod(good, 0o600)

    alias = tmp_path / "reports" / "alias.json"
    os.link(good, alias)
    try:
        assert I.read_run(report, run_id=RUN_ID,
                          expected_workers=1).complete is False
    finally:
        os.unlink(alias)

    real_uid = os.getuid()
    other = real_uid + 1 if real_uid < 60000 else real_uid - 1
    monkeypatch.setattr(os, "getuid", lambda: other)
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is False


def test_symlink_and_oversize_refused(tmp_path, monkeypatch):
    report = _report(tmp_path)
    target = tmp_path / "evil.json"
    _write_deps(report, 100, _serial_cases(report))
    real = Path(f"{report}{I.DEPS_INFIX}100")
    data = real.read_bytes()
    real.unlink()
    target.write_bytes(data)
    os.chmod(target, 0o600)
    os.symlink(target, real)
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is False
    os.unlink(real)

    monkeypatch.setattr(I, "DEPS_MAX_BYTES", 16)
    _write_deps(report, 200, _serial_cases(report, pid=200))
    assert I.read_run(report, run_id=RUN_ID,
                      expected_workers=1).complete is False


def test_other_report_files_ignored(tmp_path):
    report = _report(tmp_path)
    other = _report(tmp_path, REPORT.replace("ef", "aa"))
    _write_deps(other, 100, _payload(100, run_id=OTHER_RUN))
    _serial(report)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True
    assert set(run.nodes) == {"tests/test_a.py::test_one"}


def test_dir_scan_bounded(tmp_path, monkeypatch):
    report = _report(tmp_path)
    _serial(report)
    seen: list[str] = []
    real_scandir = os.scandir

    class FakeEntry:
        def __init__(self, name):
            self.name = name

    def fake_scandir(path):
        with real_scandir(path) as iterator:
            real = [entry.name for entry in iterator]
        for name in real:
            seen.append(name)
            yield FakeEntry(name)
        for index in range(6000):
            name = f"junk-{index}.json"
            seen.append(name)
            yield FakeEntry(name)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    run = I.read_run(report, run_id=RUN_ID, expected_workers=1)
    assert run.complete is True  # the real file is yielded first
    assert len(seen) <= I.MAX_DIR_ENTRIES  # the scan stops at the bound


# --- deselect writer --------------------------------------------------------

def test_write_deselect_binding_round_trip(tmp_path):
    report = _report(tmp_path)
    nodeids = ["tests/test_a.py::test[x-1]",
               "tests/test_b.py::Test::test_ünïcode",
               "tests/test_c.py::test_line\nbreak"]
    path = I.write_deselect_binding(report, RUN_ID, nodeids)
    assert path == Path(f"{report}{I.DESELECT_SUFFIX}")
    stamp = os.lstat(path)
    assert stat.S_IMODE(stamp.st_mode) == 0o600
    assert stamp.st_nlink == 1
    data = json.loads(path.read_bytes().decode("utf-8"))
    assert data == {"format": I.DESELECT_FORMAT, "run_id": RUN_ID,
                    "nodeids": nodeids}


def test_write_deselect_binding_refusals(tmp_path):
    report = _report(tmp_path)
    with pytest.raises(ValueError):
        I.write_deselect_binding(report, RUN_ID, ["tests/test_a.py::t\x00"])
    with pytest.raises(ValueError):
        I.write_deselect_binding(report, RUN_ID, ["no-colons-here"])
    with pytest.raises(ValueError):
        I.write_deselect_binding(report, RUN_ID, ["../evil.py::t"])
    with pytest.raises(ValueError):
        I.write_deselect_binding(report, "not-hex", ["tests/test_a.py::t"])
    assert not Path(f"{report}{I.DESELECT_SUFFIX}").exists()

    I.write_deselect_binding(report, RUN_ID, ["tests/test_a.py::t"])
    with pytest.raises(OSError):
        I.write_deselect_binding(report, RUN_ID, ["tests/test_a.py::t"])

    report2 = _report(tmp_path, REPORT.replace("ef", "bb"))
    link_path = Path(f"{report2}{I.DESELECT_SUFFIX}")
    link_path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        I.write_deselect_binding(report2, RUN_ID, ["tests/test_a.py::t"])


# --- cleanup ----------------------------------------------------------------

def test_cleanup_removes_only_own_files(tmp_path):
    report = _report(tmp_path)
    other = _report(tmp_path, REPORT.replace("ef", "aa"))
    _serial(report, pid=100)
    _serial(other, pid=100)
    keep = tmp_path / "reports" / "keep.json"
    keep.write_bytes(b"keep")
    binding = I.write_deselect_binding(report, RUN_ID,
                                       ["tests/test_a.py::test_one"])
    linked = Path(f"{report}{I.DEPS_INFIX}200")
    linked.symlink_to(tmp_path / "reports" / "keep.json")
    stray = Path(f"{report}{I.DEPS_INFIX}201")
    stray.write_bytes(b"x")
    os.chmod(stray, 0o600)
    I.cleanup(report)
    assert not Path(f"{report}{I.DEPS_INFIX}100").exists()
    assert not binding.exists()
    assert Path(f"{other}{I.DEPS_INFIX}100").exists()
    assert keep.read_bytes() == b"keep"
    assert linked.is_symlink()  # never unlinked through links
    assert not stray.exists()  # our name, regular, owned: removed
    I.cleanup(report)  # idempotent, never raises
