"""0.7.4 G2-G4: a refused baseline stays visible; dynamic lines name their reference.

Oracle: docs/specs/2026-11-20-ptest-0.7.4-fresh-worktree-visibility.md goals
G3 (refusal record in selection.db kv, one line on later runs), G4 (dynamic
reference) and abuse rows A1-A5 (paths only, hostile kv, tamper, project
isolation, stale refusal).
"""
from __future__ import annotations

import json
import sqlite3
import types
from array import array
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import operations, progress
from ptest import selection_engine as E
from ptest import selection_store as S

PROJ = "ab" * 16
OTHER = "cd" * 16
RUN_A = "11" * 16
RUN_B = "22" * 16
COMMIT = "ab" * 20
NODE = "tests/test_a.py::test_one"
NOW = 1_000_000.0


def _run():
    return S.RunDependencies(
        vocabulary=S.DepVocabulary(paths=("tests/test_a.py", "pkg/a.py"),
                                   functions=((1, "f"),), fixtures=()),
        nodes={NODE: S.RecordedNode(
            nodeid=NODE, outcome="passed",
            deps=S.ContextDeps(functions=array("I"), modules=array("I"),
                               data=array("I"), opaque=False),
            fixtures=array("I"))},
        fixtures={}, ambient=S.ContextDeps(
            functions=array("I"), modules=array("I"), data=array("I"),
            opaque=False),
        complete=True, recording=True, python=(3, 12), inactive_reason=None,
        notes=())


def _record(domain, project=PROJ, run_id=RUN_A, **extra):
    store = S.open_store(domain, project, create=True)
    try:
        store.update(_run(), run_id=run_id, recorded_at=NOW - 10,
                     compatibility="c", full=True,
                     digests={"tests/test_a.py": "aa" * 32,
                              "pkg/a.py": "aa" * 32}, **extra)
    finally:
        store.close()


def _marker(at=NOW - 3 * 86400):
    return C.GateMarker(commit=COMMIT, dirty={}, recorded_at=at)


def _refusal(paths=(".env",), total=2):
    return S.GateRefusal(code="unknown-input", paths=tuple(paths),
                         total=total)


def _db(domain, project=PROJ) -> Path:
    return domain.root / "projects" / project / "selection.db"


def _raw_set(domain, value, project=PROJ):
    conn = sqlite3.connect(_db(domain, project))
    try:
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES"
                     "('last_gate_refusal', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _record_json(**over):
    record = {"v": 1, "code": "unknown-input", "paths": [".env"],
              "total": 2, "commit": COMMIT, "at": NOW - 3 * 86400}
    record.update(over)
    return json.dumps(record)


LINE = ("ptest: last gate at abababababab (3d ago) recorded no baseline: "
        "gitignored files count as test inputs: .env (+1 more) · "
        "fix: ptest doctor --fix")


# --- G3 ---------------------------------------------------------------------

def test_refusal_written_with_the_gate_marker_and_line_names_it(domain_factory):
    domain = domain_factory()
    _record(domain, gate=_marker(), refusal=_refusal())
    assert E.gate_refusal_line(domain, PROJ, now=NOW) == LINE
    store = S.open_store(domain, PROJ, create=False)
    try:
        # Old readers/marker are unaffected by the extra key.
        assert store.snapshot().gate == _marker()
    finally:
        store.close()


def test_refusal_needs_a_marker_to_carry_commit_and_time(domain_factory):
    domain = domain_factory()
    _record(domain, clear_gate=True, refusal=_refusal())
    assert E.gate_refusal_line(domain, PROJ, now=NOW) is None


def test_refusal_record_is_per_project(domain_factory):
    """A4: project A's refusal never shows for project B."""
    domain = domain_factory()
    _record(domain, PROJ, gate=_marker(), refusal=_refusal())
    _record(domain, OTHER, gate=_marker())
    assert E.gate_refusal_line(domain, PROJ, now=NOW) == LINE
    assert E.gate_refusal_line(domain, OTHER, now=NOW) is None


def test_published_baseline_clears_refusal(domain_factory):
    """A5: a later gate that published a baseline deletes the record."""
    domain = domain_factory()
    _record(domain, gate=_marker(), refusal=_refusal())
    _record(domain, run_id=RUN_B, gate=_marker(), clear_refusal=True)
    assert E.gate_refusal_line(domain, PROJ, now=NOW) is None


def test_gate_without_verdict_keeps_the_refusal(domain_factory):
    domain = domain_factory()
    _record(domain, gate=_marker(), refusal=_refusal())
    _record(domain, run_id=RUN_B, gate=_marker())
    assert E.gate_refusal_line(domain, PROJ, now=NOW) == LINE


def test_no_store_means_no_line(domain_factory):
    assert E.gate_refusal_line(domain_factory(), PROJ, now=NOW) is None


HOSTILE = [
    ("escape-bytes", _record_json(paths=["a\x1b[31mred"])),
    ("bidi-control", _record_json(paths=["a‮b"])),
    ("traversal", _record_json(paths=["../outside"])),
    ("absolute", _record_json(paths=["/etc/passwd"])),
    ("oversized-path", _record_json(paths=["a" * 5000])),
    ("too-many-paths", _record_json(paths=["a", "b", "c", "d"])),
    ("no-paths", _record_json(paths=[])),
    ("paths-not-list", _record_json(paths=".env")),
    ("path-not-str", _record_json(paths=[7])),
    ("total-str", _record_json(total="9")),
    ("total-bool", _record_json(total=True)),
    ("total-below-paths", _record_json(paths=["a", "b"], total=1)),
    ("future-version", _record_json(v=2)),
    ("version-missing-type", _record_json(v="1")),
    ("unknown-code", _record_json(code="state-unavailable")),
    ("bad-commit", _record_json(commit="xyz")),
    ("at-nan", '{"v":1,"code":"unknown-input","paths":[".env"],'
               '"total":1,"commit":"%s","at":NaN}' % COMMIT),
    ("at-bool", _record_json(at=True)),
    ("not-json", "{{{"),
    ("json-list", "[1, 2]"),
    ("huge-value", _record_json(paths=[".env"]) + " " * 70000),
    ("deep-json", "[" * 50000),
]


@pytest.mark.parametrize("row,value", HOSTILE, ids=[r for r, _ in HOSTILE])
def test_malformed_refusal_record_is_ignored(domain_factory, row, value):
    """A2/A3: a tampered record shows no line and never crashes the run."""
    domain = domain_factory()
    _record(domain, gate=_marker())
    _raw_set(domain, value)
    assert E.gate_refusal_line(domain, PROJ, now=NOW) is None


def test_refusal_record_paths_are_validated_and_escaped(domain_factory):
    """A2 positive control: the same table's good row renders, and the
    stored path text reaches the line only through terminal_text."""
    domain = domain_factory()
    _record(domain, gate=_marker())
    _raw_set(domain, _record_json(paths=["café/.env"], total=1))
    line = E.gate_refusal_line(domain, PROJ, now=NOW)
    assert line is not None and "café/.env" in line
    assert "(+" not in line
    assert all(ch.isprintable() for ch in line)


def test_baseline_refusal_prints_paths_never_content(domain_factory, tmp_path):
    """A1: the note, the later line and the kv hold paths, not file text."""
    secret = "TOP-SECRET-TOKEN-0123"
    env = tmp_path / ".env"
    env.write_text(f"KEY={secret}\n")
    changes = (C.Change(old=None, new=".env", kind="ignored"),)
    paths = progress.ignored_input_paths(_snap(False, changes), _snap(False, changes))
    assert paths == [".env"]
    domain = domain_factory()
    _record(domain, gate=_marker(),
            refusal=S.GateRefusal(code="unknown-input", paths=tuple(paths),
                                  total=len(paths)))
    line = E.gate_refusal_line(domain, PROJ, now=NOW)
    assert line is not None and secret not in line
    assert secret.encode() not in _db(domain).read_bytes()


# --- operations seam --------------------------------------------------------

def _snap(clean, changes=(), digest="aa" * 32):
    return types.SimpleNamespace(clean=clean, changes=tuple(changes),
                                 digest=digest)


def _plan(execution="full"):
    return types.SimpleNamespace(execution=execution)


IGN = (C.Change(old=None, new=".env", kind="ignored"),
       C.Change(old=None, new="build/a.py", kind="ignored"))


def test_gate_verdict_refuses_when_only_ignored_paths_made_it_unclean():
    refusal, clear = operations._gate_refusal(
        _plan(), C.Status.PASSED, _snap(False, IGN), _snap(False, IGN))
    assert clear is False
    assert refusal == S.GateRefusal(code="unknown-input",
                                    paths=(".env", "build/a.py"), total=2)


def test_gate_verdict_caps_paths_at_three_and_counts_all():
    many = tuple(C.Change(old=None, new=f"b/{i}.py", kind="ignored")
                 for i in range(7))
    refusal, _ = operations._gate_refusal(
        _plan(), C.Status.PASSED, _snap(False, many), _snap(False, many))
    assert len(refusal.paths) == 3 and refusal.total == 7


def test_gate_verdict_clears_on_a_clean_passing_gate():
    assert operations._gate_refusal(
        _plan(), C.Status.PASSED, _snap(True), _snap(True)) == (None, True)


@pytest.mark.parametrize("execution,status,before,after", [
    ("selected", C.Status.PASSED, _snap(True), _snap(True)),
    ("full", C.Status.FAILED, _snap(True), _snap(True)),
    ("full", C.Status.PASSED, _snap(True), _snap(True, digest="bb" * 32)),
    ("full", C.Status.PASSED, _snap(False, (C.Change(
        old="a.py", new="a.py", kind="modified"),)), _snap(False)),
    ("full", C.Status.PASSED, None, None),
])
def test_gate_verdict_is_silent_otherwise(execution, status, before, after):
    assert operations._gate_refusal(
        _plan(execution), status, before, after) == (None, False)


def test_later_runs_print_the_refusal_line(domain_factory, capsys):
    domain = domain_factory()
    _record(domain, gate=_marker(), refusal=_refusal())
    checkout = C.CheckoutIdentity(project_id=PROJ, checkout_id="cd" * 16,
                                  root=Path("/tmp/proj"))
    request = types.SimpleNamespace(quiet=False, setup_only=False)
    operations._emit_last_gate_refusal(domain, checkout, request)
    err = capsys.readouterr().err
    assert err.startswith("ptest: last gate at abababababab (")
    assert err.endswith("fix: ptest doctor --fix\n")
    quiet = types.SimpleNamespace(quiet=True, setup_only=False)
    operations._emit_last_gate_refusal(domain, checkout, quiet)
    assert capsys.readouterr().err == ""
    other = C.CheckoutIdentity(project_id=OTHER, checkout_id="cd" * 16,
                               root=Path("/tmp/proj"))
    operations._emit_last_gate_refusal(domain, other, request)
    assert capsys.readouterr().err == ""


# --- G4 ---------------------------------------------------------------------

def _units(n_funcs, n_modules_extra=0):
    units = [C.ChangedUnit(kind="function",
                           label=f"src/pkg/m{i % 5}.py::f{i}", tests=100 - i)
             for i in range(n_funcs)]
    units += [C.ChangedUnit(kind="module", label=f"src/pkg/x{i}.py", tests=1)
              for i in range(n_modules_extra)]
    return tuple(units)


def test_reference_names_age_modules_and_gate():
    text = E._reference_text(_units(29, 3), newest_age=3 * 86400.0,
                             gate_commit=COMMIT, gate_age=2 * 3600.0)
    assert text == (
        "since recorded runs, newest 3d ago: src/pkg/m0.py, src/pkg/m1.py, "
        "src/pkg/m2.py, +5 more · last gate abababababab 2h ago")


def test_reference_without_gate_or_age_says_so():
    text = E._reference_text(_units(1), newest_age=None, gate_commit=None,
                             gate_age=None)
    assert text == ("since recorded runs: src/pkg/m0.py · no gate recorded")


def test_reference_escapes_paths():
    units = (C.ChangedUnit(kind="module", label="a\x1b[31m.py", tests=1),)
    text = E._reference_text(units, newest_age=5.0, gate_commit=COMMIT,
                             gate_age=5.0)
    assert "\x1b" not in text


def test_changed_line_carries_the_reference():
    from ptest import cli
    from ptest.impact import Impact

    impact = Impact(
        kind="selected", engine="dynamic", changed=(), tests=1940,
        files=("tests/test_a.py",), total=9, units=_units(29),
        reference="since recorded runs, newest 3d ago: a.py, +1 more "
                  "· last gate abababababab 2h ago")
    note = cli._impact_note(impact, "x", reference="vs main")
    assert note == (
        "vs main: no changes → 1940 tests in 1 of 9 files "
        "(dynamic · 29 functions changed since recorded runs, newest 3d "
        "ago: a.py, +1 more · last gate abababababab 2h ago)")


def test_age_words():
    assert [progress.format_age(s) for s in (0, 59, 60, 3599, 3600, 86399,
                                             86400, 3 * 86400 + 5, -4)] == [
        "0s", "59s", "1m", "59m", "1h", "23h", "1d", "3d", "0s"]
