"""Selection engine unit tests (T5 integration glue).

The engine reaches T1-T3 only through module-level lazy accessors
(``_impact``, ``_source_index``, ``_planner``, ``_store``, ``_ingest``),
so every test injects fakes there. Nothing here touches a real store,
the network, or project code: the integration boundary with the real
T1-T4 modules is covered by ``test_selection_e2e.py`` (readiness-gated).
"""
from __future__ import annotations

import contextvars
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

import support
from ptest import contracts as C
from ptest.selection_planner import GateAudit


ENGINE = "ptest.selection_engine"
T1, T2 = C.SELECTION_TIER_RUN_NOW, C.SELECTION_TIER_DEFERRED


# --- fakes ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FakeImpact:
    """Mirrors the frozen impact.Impact shape (design 2.2)."""

    kind: str
    changed: tuple = ()
    files: tuple = ()
    direct: int = 0
    via: int = 0
    total: int = 0
    reason: str = ""
    ignored: int = 0
    engine: str = ""
    dynamic_ok: bool = False
    relevant: tuple = ()
    static_reason: str = ""
    deselect: tuple = ()
    tests: int = 0
    reached: int = 0
    units: tuple = ()
    details: tuple = ()
    selection: object = None
    conditional: tuple = ()
    decision: object = None
    observed: frozenset = frozenset()
    project_index: object = None


@dataclass(frozen=True, slots=True)
class FakeFile:
    digest: str


@dataclass(frozen=True, slots=True)
class FakeIndex:
    files: dict
    test_files: frozenset = frozenset()
    reverse: dict = None
    complete: bool = True

    def __post_init__(self):
        object.__setattr__(self, "reverse",
                           {} if self.reverse is None else self.reverse)


def _context(functions=(), modules=(), data=(), opaque=False):
    return C.ContextDeps(functions=C.selection_ids(functions),
                         modules=C.selection_ids(modules),
                         data=C.selection_ids(data), opaque=opaque)


def _snapshot(*, run_id="ab" * 16, compatibility="compat",
              nodes=(), fixtures=(), ambient=None, demotions=None,
              gate=None, observers=None, fingerprint=None):
    vocab = C.DepVocabulary(paths=("tests/test_a.py",),
                            functions=((0, "test_body"),),
                            fixtures=(("base", "arg", "session"),))
    runs = {run_id: C.RunBaseline(
        run_id=run_id, recorded_at=1.0, compatibility=compatibility,
        digests={}, ambient=ambient or C.selection_empty_context(),
        fingerprint=fingerprint)}
    node_map = {}
    for i, (nodeid, outcome) in enumerate(nodes):
        node_map[nodeid] = C.NodeRecord(
            nodeid=nodeid, test_file=C.selection_test_file(nodeid),
            outcome=outcome, run_id=run_id, deps=_context(),
            fixtures=C.selection_ids(()))
    return C.DependencySnapshot(vocabulary=vocab, runs=runs, nodes=node_map,
                                fixtures={}, demotions=demotions or {},
                                gate=gate, observers=observers or {})


def _meta(*, nodes=3, runs=1, size=4096, newest=100.0, python=(3, 12),
          inactive=None, observers=0):
    return C.SelectionStoreMeta(
        size_bytes=size, nodes=nodes, runs=runs,
        newest_recorded_at=newest, python=python, inactive_reason=inactive,
        audit_checked=0, audit_misses=0, demoted=0, observers=observers)


class FakeStore:
    """In-memory SelectionStore double with a call log."""

    def __init__(self, snapshot=None, meta=None):
        self._snapshot = snapshot
        self._meta = meta or _meta()
        self.calls = []
        self.updates = []
        self.evicted = 0
        self.update_error = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True

    def get_many(self, digests):
        self.calls.append(("get_many", tuple(sorted(digests))))
        return {}

    def put_many(self, blobs):
        self.calls.append(("put_many", len(blobs)))

    def snapshot(self):
        self.calls.append(("snapshot",))
        return self._snapshot

    def meta(self):
        self.calls.append(("meta",))
        return self._meta

    def update(self, run, **kwargs):
        self.calls.append(("update", kwargs.get("run_id")))
        if self.update_error is not None:
            raise self.update_error
        self.updates.append(dict(kwargs, run=run))
        return self.evicted

    def mark_outcomes(self, outcomes):
        self.calls.append(("mark_outcomes", dict(outcomes)))

    def invalidate(self, test_files):
        self.calls.append(("invalidate", None if test_files is None
                           else tuple(test_files)))

    def demote(self, nodeids):
        if not all(isinstance(d, str) and d for d in dict(nodeids).values()):
            raise ValueError("demotion digests must be nonempty str")
        self.calls.append(("demote", dict(nodeids)))

    def record_audit(self, checked, misses):
        self.calls.append(("record_audit", checked, misses))

    def note_inactive(self, reason, python):
        self.calls.append(("note_inactive", reason, python))


class FakeStoreModule:
    def __init__(self, store=None, error=None):
        self._store = store
        self._error = error
        self.opened = []

    def open_store(self, domain, project_id, *, create):
        self.opened.append((project_id, create))
        if self._error is not None:
            raise self._error
        return self._store


class FakeImpactModule:
    def __init__(self, static=None):
        self.PLANNING = contextvars.ContextVar("ptest_impact_planning",
                                               default=None)
        self.MAX_SELECTED = 200
        self._static = static
        self.plans = 0

    def plan(self, top, project_root, config, repo_changed, *,
             key=None, cache=None, conftest_edges=True):
        self.plans += 1
        return self._static

    def git_top(self, start):
        from ptest import impact
        return impact.git_top(start)

    def relevant_paths(self, top, project_root, config, paths):
        from ptest import impact
        return impact.relevant_paths(top, project_root, config, paths)


class FakeSourceIndex:
    def __init__(self, index=None, error=None):
        self._index = index
        self._error = error
        self.builds = []

    def build_project_index(self, project_root, config, *, key, cache,
                            conftest_edges=True):
        self.builds.append((key, cache))
        if self._error is not None:
            raise self._error
        return self._index

    def decode_index(self, blob):
        return None

    def ensure_cached(self, project_root, digests, *, key, cache):
        self.builds.append(("ensure_cached", cache))
        return 0

    def iter_python_files(self, project_root):
        return iter(())

    def read_source(self, project_root, rel):
        return None


class FakePlanner:
    def __init__(self, decision=None, error=None, misses=()):
        self._decision = decision
        self._error = error
        self._misses = misses
        self._audit = None
        self.inputs_seen = []
        self.audit_calls = []

    def needed_versions(self, index, deps, compatibility):
        return frozenset()

    def needed_data_paths(self, deps, compatibility):
        return frozenset()

    def plan(self, inputs):
        self.inputs_seen.append(inputs)
        if self._error is not None:
            raise self._error
        return self._decision

    def audit_misses(self, decision, deps, failed):
        self.audit_calls.append((decision, deps, tuple(failed)))
        return self._misses

    def audit_classify(self, decision, deps, failed, ran):
        self.audit_calls.append((decision, deps, tuple(failed)))
        if self._audit is not None:
            return self._audit
        return GateAudit(previously_deferred=0, failed=0, learned={},
                         unlearned=(), misses=tuple(self._misses))


def _run_deps(*, complete=True, recording=True, nodes=(),
              inactive=None, python=(3, 12)):
    vocab = C.DepVocabulary(paths=(), functions=(), fixtures=())
    node_map = {}
    for nodeid, outcome in nodes:
        node_map[nodeid] = C.RecordedNode(
            nodeid=nodeid, outcome=outcome, deps=C.selection_empty_context(),
            fixtures=C.selection_ids(()))
    return C.RunDependencies(
        vocabulary=vocab, nodes=node_map, fixtures={},
        ambient=C.selection_empty_context(), complete=complete,
        recording=recording, python=python, inactive_reason=inactive, notes=())


class FakeIngest:
    def __init__(self, run=None):
        self._run = run if run is not None else _run_deps()
        self.cleaned = []
        self.bindings = []
        self.rewrites = []

    def read_run(self, report_path, *, run_id, expected_workers):
        return self._run

    def write_deselect_binding(self, report_path, run_id, nodeids):
        self.bindings.append(tuple(nodeids))
        return Path(str(report_path) + ".deselect")

    rewrite_error = None

    def rewrite_deselect_binding(self, report_path, run_id, nodeids):
        if self.rewrite_error is not None:
            raise self.rewrite_error
        self.rewrites.append((Path(report_path), run_id, tuple(nodeids)))
        return Path(str(report_path) + ".deselect")

    def cleanup(self, report_path):
        self.cleaned.append(report_path)


def _config(tmp_path=None, *, runner="pytest", dynamic=True, enabled=True,
            full_ratio=0.70, observers=(), ambient_fingerprint=()):
    return C.Config(
        runner=C.RunnerConfig(
            kind=C.RunnerKind.PYTEST if runner == "pytest"
            else C.RunnerKind.COMMAND,
            launcher=("python",) if runner == "pytest" else ("echo",),
            args=(), full_args=(), test_roots=("tests",), workers=1,
            lifecycle="cooperative-process-group"),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=enabled, closed_inputs=False,
                                    full_ratio=full_ratio, dynamic=dynamic,
                                    observers=observers,
                                    ambient_fingerprint=ambient_fingerprint),
        project_id="ab" * 16,
    )


def _domain(tmp_path):
    root = Path(tmp_path) / "state"
    root.mkdir(exist_ok=True)
    return C.DomainPaths(root=root, machine_config=root / "m",
                         ledger=root / "l", marker=root / "k",
                         fixture=True, domain_id=None)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    engine = pytest.importorskip(ENGINE)
    store = FakeStore(snapshot=_snapshot())
    store_mod = FakeStoreModule(store=store)
    impact_mod = FakeImpactModule()
    source_mod = FakeSourceIndex(index=FakeIndex(files={}))
    planner_mod = FakePlanner()
    ingest_mod = FakeIngest()
    monkeypatch.setattr(engine, "_impact", lambda: impact_mod)
    monkeypatch.setattr(engine, "_source_index", lambda: source_mod)
    monkeypatch.setattr(engine, "_planner", lambda: planner_mod)
    monkeypatch.setattr(engine, "_store", lambda: store_mod)
    monkeypatch.setattr(engine, "_ingest", lambda: ingest_mod)
    monkeypatch.setattr(engine, "_source_key", lambda domain: b"k" * 64)
    monkeypatch.setattr(engine, "compatibility_fingerprint",
                        lambda key, config, root: "compat")
    return types.SimpleNamespace(
        engine=engine, store=store, store_mod=store_mod,
        impact_mod=impact_mod, source_mod=source_mod, planner_mod=planner_mod,
        ingest_mod=ingest_mod)


def _decision(*, files=("tests/test_a.py",), deselect=(), selected=2,
              reached=0, recorded=2, total_files=4, units=(), fallbacks=(),
              coverage=0.5, tiers=None, **extra):
    """A planner decision; without ``tiers`` the T1 nodes are synthesised
    from ``selected`` (the engine counts tests from the tier map)."""
    if tiers is None:
        tiers = {f"{files[0]}::t{i}": (T1, "d") for i in range(selected)} \
            if files and selected else {}
    return C.SelectionDecision(
        files=tuple(files), deselect=tuple(deselect),
        whole_files=tuple(files), selected=selected, reached=reached,
        recorded=recorded, total_files=total_files, units=tuple(units),
        fallbacks=tuple(fallbacks), coverage=coverage, tiers=tiers, **extra)


def _static_ok(**overrides):
    fields = {
        "kind": "selected",
        "changed": ("pkg/core.py",),
        "files": ("tests/test_a.py", "tests/test_b.py"),
        "direct": 1, "via": 1, "total": 10,
        "engine": "static", "dynamic_ok": True,
        "relevant": ("pkg/core.py",),
        "project_index": FakeIndex(files={"pkg/core.py": FakeFile("d1")}),
    }
    fields.update(overrides)
    return FakeImpact(**fields)


# --- gating ------------------------------------------------------------

def test_non_dynamic_verdict_passes_through_untouched(fakes, tmp_path):
    static = FakeImpact(kind="selected", files=("tests/test_a.py",),
                        total=10)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (), static)
    # The verdict itself is untouched; only the R34 report is attached.
    assert (out.kind, out.files, out.total, out.static_reason,
            out.details) == ("selected", ("tests/test_a.py",), 10, "", ())
    assert out.selection.engine == "static"
    assert (out.selection.run_now, out.selection.deferred) == (None, None)
    assert fakes.planner_mod.inputs_seen == []
    assert fakes.store_mod.opened == []


def test_non_pytest_runner_passes_through(fakes, tmp_path):
    out = fakes.engine.refine(None, None, tmp_path,
                              _config(runner="command"), (),
                              _static_ok())
    assert out is not None
    assert out.kind == "selected"
    assert out.engine == "static"
    assert out.static_reason == ""
    assert fakes.planner_mod.inputs_seen == []


def test_dynamic_false_gives_static_reason(fakes, tmp_path):
    out = fakes.engine.refine(None, None, tmp_path,
                              _config(dynamic=False), (),
                              _static_ok())
    assert out.static_reason == "dynamic = false in .ptest.toml"
    assert out.kind == "selected"
    assert out.files == ("tests/test_a.py", "tests/test_b.py")
    assert out.details == ("engine static: dynamic = false in .ptest.toml",)


# --- static reasons ----------------------------------------------------

def test_no_key_means_no_records_yet(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(fakes.engine, "_source_key", lambda domain: None)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "no dependency records yet — any run records them"
    assert fakes.store_mod.opened == []


def test_missing_store_is_unavailable(fakes, tmp_path):
    fakes.store_mod._error = C.Problem(code="state-unavailable", message="gone",
                                      phase="test")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == "dependency store unavailable"
    assert out.kind == "selected"


def test_missing_store_module_is_unavailable(fakes, tmp_path, monkeypatch):
    def boom():
        raise ImportError("no selection_store yet")
    monkeypatch.setattr(fakes.engine, "_store", boom)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == "dependency store unavailable"


def test_empty_snapshot_means_no_records_yet(fakes, tmp_path):
    empty = C.DependencySnapshot(
        vocabulary=C.DepVocabulary(paths=(), functions=(), fixtures=()),
        runs={}, nodes={}, fixtures={}, demotions={})
    fakes.store._snapshot = empty
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "no dependency records yet — any run records them"


def test_incompatible_runs_mean_another_configuration(fakes, tmp_path,
                                                      monkeypatch):
    fakes.store._snapshot = _snapshot(compatibility="other")
    monkeypatch.setattr(fakes.engine, "compatibility_fingerprint",
                        lambda key, config, root: "compat")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "dependency records are from another configuration"


def test_old_python_means_cannot_record(fakes, tmp_path):
    empty = C.DependencySnapshot(
        vocabulary=C.DepVocabulary(paths=(), functions=(), fixtures=()),
        runs={}, nodes={}, fixtures={}, demotions={})
    fakes.store._snapshot = empty
    fakes.store._meta = _meta(python=(3, 11))
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "test Python 3.11 cannot record dependencies (needs 3.12+)"


def test_inactive_recorder_reason(fakes, tmp_path):
    empty = C.DependencySnapshot(
        vocabulary=C.DepVocabulary(paths=(), functions=(), fixtures=()),
        runs={}, nodes={}, fixtures={}, demotions={})
    fakes.store._snapshot = empty
    fakes.store._meta = _meta(inactive="no free sys.monitoring tool id")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "recording was unavailable: no free sys.monitoring tool id"


def test_planner_exception_means_dynamic_planner_failed(fakes, tmp_path):
    fakes.planner_mod._error = RuntimeError("boom")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == "dynamic planner failed"
    assert out.kind == "selected"
    assert out.files == ("tests/test_a.py", "tests/test_b.py")


# --- decision mapping --------------------------------------------------

def _refine_selected(fakes, tmp_path, **kw):
    units = (C.ChangedUnit(kind="function", label="pkg/core.py:boot",
                           tests=2),)
    fakes.planner_mod._decision = _decision(units=units, **kw)
    return fakes.engine.refine(None, None, tmp_path, _config(), (),
                               _static_ok())


def test_dynamic_selected_maps_files_deselect_and_tests(fakes, tmp_path):
    out = _refine_selected(
        fakes, tmp_path, files=("tests/test_a.py",),
        deselect=("tests/test_a.py::test_other",), selected=2, recorded=3,
        total_files=4)
    assert out.kind == "selected"
    assert out.engine == "dynamic"
    assert out.files == ("tests/test_a.py",)
    assert out.deselect == ("tests/test_a.py::test_other",)
    assert out.tests == 2
    assert out.total == 4
    assert out.static_reason == ""


def _scoped_request(impact):
    return C.RunRequest(mode=C.Mode.SCOPED, argv=tuple(impact.files),
                        base=None, deselect=impact.deselect)


def test_unsafe_deselect_id_keeps_its_whole_file(fakes, tmp_path):
    """G2/D7: an id the binding cannot hold widens its file, never crashes.

    A parametrized id over SELECTION_NODEID_MAX_BYTES next to a selected
    test made RunRequest raise ValueError (traceback, exit 1) before the fix.
    """
    long_id = "tests/test_a.py::test_p[" + "x" * 5000 + "]"
    out = _refine_selected(
        fakes, tmp_path, files=("tests/test_a.py", "tests/test_b.py"),
        deselect=(long_id, "tests/test_a.py::test_q",
                  "tests/test_b.py::test_r"),
        selected=2, recorded=6, total_files=4)
    assert out.deselect == ("tests/test_b.py::test_r",)
    assert out.tests == 4
    assert _scoped_request(out).deselect == ("tests/test_b.py::test_r",)


def test_deselect_over_id_cap_runs_whole_files(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SELECTION_DESELECT_MAX_IDS", 2)
    out = _refine_selected(
        fakes, tmp_path, files=("tests/test_a.py",),
        deselect=tuple(f"tests/test_a.py::u{i}" for i in range(3)),
        selected=1, recorded=4, total_files=4)
    assert out.deselect == ()
    assert out.tests == 4
    assert _scoped_request(out).deselect == ()


def test_dynamic_none_with_reach_reports_reached(fakes, tmp_path):
    fakes.planner_mod._decision = _decision(files=(), deselect=(),
                                            selected=0, reached=5,
                                            recorded=8, total_files=4)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok(kind="none"))
    assert out.kind == "none"
    assert out.engine == "dynamic"
    assert out.files == ()
    assert out.reached == 5


def test_dynamic_none_without_reach_keeps_static_text(fakes, tmp_path):
    fakes.planner_mod._decision = _decision(files=(), deselect=(),
                                            selected=0, reached=0,
                                            recorded=8, total_files=4)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok(kind="none"))
    assert out.kind == "none"
    assert out.reached == 0


# Spec R6 - neither full_ratio, MAX_SELECTED nor an ambient data change
# promotes a dynamic iteration plan to the full suite any more (oracle change
# of the four 0.6.8 tests test_dynamic_full_on_recorded_ratio,
# test_dynamic_full_on_file_ratio, test_dynamic_full_over_argv_limit and
# test_dynamic_full_on_planner_reason).

def _node_tiers(file, count, code="d"):
    return {f"{file}::t{i}": (T1, code) for i in range(count)}


@pytest.mark.parametrize("case", ["recorded-ratio", "file-ratio",
                                  "argv-limit", "ambient-data"])
def test_iteration_never_promoted_by_removed_caps(fakes, tmp_path, case):
    """300 files, 90% of recorded tests, an ambient data change: all scoped."""
    if case == "recorded-ratio":
        files = ("tests/test_a.py",)
        decision = _decision(files=files, selected=9, recorded=10,
                             total_files=4)
        expected = 9
    elif case == "file-ratio":
        files = tuple(f"tests/test_{i}.py" for i in range(4))
        tiers = {f"{f}::t": (T1, "d") for f in files}
        decision = _decision(files=files, selected=4, recorded=10,
                             total_files=4, tiers=tiers)
        expected = 4
    elif case == "argv-limit":
        files = tuple(f"tests/test_{i}.py" for i in range(300))
        tiers = {f"{f}::t": (T1, "d") for f in files}
        decision = _decision(files=files, selected=300, recorded=1000,
                             total_files=1000, tiers=tiers)
        expected = 300
    else:
        files = ("tests/test_a.py",)
        tiers = {"tests/test_a.py::t0": (T1, "d"),
                 "tests/test_b.py::t0": (T2, "g-data"),
                 "tests/test_b.py::t1": (T2, "g-data")}
        decision = _decision(
            files=files, selected=1, recorded=3, total_files=2,
            tiers=tiers, via={"tests/test_b.py::t0": "data/config.json",
                              "tests/test_b.py::t1": "data/config.json"})
        expected = 1
    fakes.planner_mod._decision = decision
    out = fakes.engine.refine(None, None, tmp_path,
                              _config(full_ratio=0.5), (), _static_ok())
    assert out.kind == "selected"
    assert out.engine == "dynamic"
    assert out.files == files
    assert out.tests == expected
    assert out.reason == ""
    assert out.selection is not None and out.selection.engine == "dynamic"
    assert out.selection.run_now == expected
    if case == "ambient-data":
        assert out.selection.deferred == 2
        assert out.selection.deferred_by_reason == (
            ("g-data", "data/config.json", 2),)


def test_dynamic_details_carry_v_lines(fakes, tmp_path):
    out = _refine_selected(fakes, tmp_path)
    assert out.details[0].startswith("engine dynamic · 2 tests recorded · ")
    assert "changed function pkg/core.py:boot → 2 tests" in out.details
    fakes.planner_mod._decision = _decision(
        fallbacks=(("tests/test_a.py::test_x", "no dependency record"),))
    out2 = fakes.engine.refine(None, None, tmp_path, _config(), (),
                               _static_ok())
    assert "fallback tests/test_a.py::test_x: no dependency record" in \
        out2.details


def test_dynamic_details_use_singular_counts(fakes, tmp_path):
    units = (C.ChangedUnit(kind="function", label="pkg/core.py:boot",
                           tests=1),)
    fakes.planner_mod._decision = _decision(
        files=("tests/test_a.py",), selected=1, recorded=3, units=units)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.kind == "selected"
    assert out.details[0].startswith("engine dynamic · 3 tests recorded · ")
    assert "changed function pkg/core.py:boot → 1 test" in out.details


def test_progress_selection_formats_use_singular_and_plural():
    from ptest import progress

    assert progress.format_selection_store(None, 12, 2048, 5.0) == (
        "selection store: 12 tests recorded · 2.0 KB · newest 5.0s ago")
    assert progress.format_selection_store(None, 1, 4, 1.0) == (
        "selection store: 1 test recorded · 4 B · newest 1.0s ago")
    assert progress.format_selection_recorded(1, 1) == (
        "ptest: -v selection: recorded 1 test from 1 process")
    assert progress.format_selection_recorded(7, 2) == (
        "ptest: -v selection: recorded 7 tests from 2 processes")
    assert progress.format_selection_vaudit(1, 1) == (
        "ptest: -v selection audit: 1 failing test checked · 1 miss")
    assert progress.format_selection_vaudit(0, 2) == (
        "ptest: -v selection audit: 0 failing tests checked · 2 misses")

    def unit(kind):
        return types.SimpleNamespace(kind=kind, label=kind, tests=1)

    assert progress.summarize_units(
        [unit("function"), unit("unrecorded")]) == (
        "1 function changed, 1 unrecorded test file")
    assert progress.summarize_units(
        [unit("unrecorded"), unit("unrecorded")]) == (
        "2 unrecorded test files")


def test_dynamic_summary_counts_units_by_kind(fakes, tmp_path):
    units = (C.ChangedUnit(kind="function", label="a", tests=1),
             C.ChangedUnit(kind="function", label="b", tests=1),
             C.ChangedUnit(kind="name", label="c", tests=1),
             C.ChangedUnit(kind="module", label="d", tests=1),
             C.ChangedUnit(kind="data", label="e", tests=1),
             C.ChangedUnit(kind="file", label="f", tests=1),
             C.ChangedUnit(kind="unrecorded", label="g", tests=1))
    fakes.planner_mod._decision = _decision(units=units)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    kinds = [unit.kind for unit in out.units]
    assert kinds == ["function", "function", "name", "module", "data",
                     "file", "unrecorded"]


# --- index rule ------------------------------------------------------

def _selected_decision(fakes):
    fakes.planner_mod._decision = _decision()


def test_given_complete_index_is_reused(fakes, tmp_path):
    index = FakeIndex(files={"pkg/core.py": FakeFile("d1")})
    _selected_decision(fakes)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok(project_index=index))
    assert out.engine == "dynamic"
    assert fakes.source_mod.builds == []
    assert fakes.planner_mod.inputs_seen[0].index is index


@pytest.mark.parametrize("kind", ["selected", "none", "full"])
def test_missing_index_is_built_once_with_planning_key(fakes, tmp_path,
                                                       kind):
    built = FakeIndex(files={})
    fakes.source_mod._index = built
    _selected_decision(fakes)
    static = _static_ok(kind=kind, project_index=None,
                        relevant=("pkg/core.py",))
    if kind == "full":
        static = _static_ok(kind="full", reason="is outside the import graph",
                            project_index=None,
                            relevant=("data/config.json",))
    out = fakes.engine.refine(None, None, tmp_path, _config(),
                              ("pkg/core.py",), static)
    assert out.engine == "dynamic"
    assert len(fakes.source_mod.builds) == 1
    key, cache = fakes.source_mod.builds[0]
    assert key == b"k" * 64
    assert fakes.planner_mod.inputs_seen[0].index is built


def test_incomplete_index_means_dynamic_planner_failed(fakes, tmp_path):
    fakes.source_mod._index = FakeIndex(files={}, complete=False)
    _selected_decision(fakes)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok(project_index=None))
    assert out.static_reason == "dynamic planner failed"


def test_raising_index_build_means_dynamic_planner_failed(fakes, tmp_path):
    fakes.source_mod._error = RuntimeError("walk failed")
    _selected_decision(fakes)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok(project_index=None))
    assert out.static_reason == "dynamic planner failed"


# --- compatibility ---------------------------------------------------

def test_compatibility_is_stable_and_order_insensitive(tmp_path):
    engine = pytest.importorskip(ENGINE)
    root = Path(tmp_path) / "proj"
    root.mkdir()
    (root / "uv.lock").write_bytes(b"lock-bytes")
    (root / "pyproject.toml").write_bytes(b"[project]")
    config = _config()
    first = engine.compatibility_fingerprint(b"k" * 64, config, root)
    (root / "uv.lock").write_bytes(b"lock-bytes")
    assert engine.compatibility_fingerprint(b"k" * 64, config, root) == first
    assert len(first) == 64


def test_compatibility_changes_with_lockfile_bytes(tmp_path):
    engine = pytest.importorskip(ENGINE)
    root = Path(tmp_path) / "proj"
    root.mkdir()
    (root / "uv.lock").write_bytes(b"v1")
    config = _config()
    before = engine.compatibility_fingerprint(b"k" * 64, config, root)
    (root / "uv.lock").write_bytes(b"v2")
    assert engine.compatibility_fingerprint(b"k" * 64, config, root) != before


def test_compatibility_changes_with_policy_and_runner(tmp_path):
    engine = pytest.importorskip(ENGINE)
    root = Path(tmp_path) / "proj"
    root.mkdir()
    config = _config()
    base = engine.compatibility_fingerprint(b"k" * 64, config, root)
    other = engine.compatibility_fingerprint(
        b"k" * 64, _config(full_ratio=0.5), root)
    assert other != base


def test_data_digest_never_covers_only_a_prefix(tmp_path, monkeypatch):
    """N1: an edit past the read cap must not keep the digest equal.

    Before the fix both versions hashed the same 8-byte prefix.
    """
    engine = pytest.importorskip(ENGINE)
    monkeypatch.setattr(C, "SELECTION_DATA_MAX_BYTES", 8)
    data = tmp_path / "blob.bin"
    data.write_bytes(b"12345678-tail-one")
    first = engine._data_digest(b"k" * 64, tmp_path, "blob.bin")
    data.write_bytes(b"12345678-tail-two")
    assert first == "?"
    assert engine._data_digest(b"k" * 64, tmp_path, "blob.bin") == "?"
    data.write_bytes(b"small")
    assert engine._data_digest(b"k" * 64, tmp_path, "blob.bin") \
        == C.selection_file_digest(b"k" * 64, b"small")


def test_oversize_data_baseline_is_none_so_contexts_go_opaque(
        tmp_path, monkeypatch):
    engine = pytest.importorskip(ENGINE)
    monkeypatch.setattr(C, "SELECTION_DATA_MAX_BYTES", 8)
    (tmp_path / "big.bin").write_bytes(b"x" * 9)
    (tmp_path / "ok.bin").write_bytes(b"x" * 8)
    monkeypatch.setattr(engine, "_source_index", lambda: None)
    monkeypatch.setattr(engine, "_run_data_paths",
                        lambda run: ("big.bin", "ok.bin", "gone.bin"))
    digests = engine._current_digests(b"k" * 64, tmp_path, object())
    assert digests["big.bin"] is None
    assert digests["ok.bin"] == C.selection_file_digest(b"k" * 64, b"x" * 8)
    assert digests["gone.bin"] == ""


def test_compatibility_changes_with_bytes_past_any_prefix(tmp_path,
                                                         monkeypatch):
    engine = pytest.importorskip(ENGINE)
    monkeypatch.setattr(engine, "_FILE_READ_CAP", 8)
    root = Path(tmp_path) / "proj"
    root.mkdir()
    (root / "uv.lock").write_bytes(b"12345678-a")
    config = _config()
    before = engine.compatibility_fingerprint(b"k" * 64, config, root)
    assert engine.compatibility_fingerprint(b"k" * 64, config, root) != before


# --- preview ---------------------------------------------------------

def test_preview_writes_no_records_and_prints_nothing(fakes, tmp_path,
                                                      capsys):
    _selected_decision(fakes)
    fakes.impact_mod._static = _static_ok()
    out = fakes.engine.preview(_domain(tmp_path), None, tmp_path, _config(),
                               ("pkg/core.py",))
    assert out.engine == "dynamic"
    assert fakes.impact_mod.plans == 1
    # Snapshot for records, meta for the -v line; no version fetch is
    # needed when the planner wants no old versions.
    assert [call[0] for call in fakes.store.calls] == ["snapshot", "meta"]
    assert capsys.readouterr() == ("", "")


def test_preview_never_raises_for_state_problems(fakes, tmp_path, capsys):
    fakes.store_mod._error = C.Problem(code="state-unavailable", message="gone",
                                      phase="test")
    fakes.impact_mod._static = _static_ok()
    out = fakes.engine.preview(_domain(tmp_path), None, tmp_path, _config(),
                               ("pkg/core.py",))
    assert out.static_reason == "dependency store unavailable"
    assert capsys.readouterr() == ("", "")


# --- planning context ------------------------------------------------

def test_planning_sets_key_and_store_cache(fakes, tmp_path):
    domain = _domain(tmp_path)
    with fakes.engine.planning(domain, _config()) as ctx:
        assert ctx is not None
        assert ctx.key == b"k" * 64
        assert ctx.cache is fakes.store
        assert fakes.impact_mod.PLANNING.get() is ctx
    assert fakes.impact_mod.PLANNING.get() is None
    assert fakes.store.closed
    # The parse cache must persist from the first plan, or every plan
    # without records re-parses the whole project (A3).
    assert fakes.store_mod.opened == [(_config().project_id, True)]


def test_planning_without_key_uses_no_cache(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(fakes.engine, "_source_key", lambda domain: None)
    with fakes.engine.planning(_domain(tmp_path), _config()) as ctx:
        assert ctx is not None
        assert ctx.key is None
        assert ctx.cache is None


def test_planning_without_planning_var_is_a_noop(fakes, tmp_path,
                                                 monkeypatch):
    fakes.impact_mod.PLANNING = None
    with fakes.engine.planning(_domain(tmp_path), _config()) as ctx:
        assert ctx is None
    assert fakes.store_mod.opened == []


# --- prepare_run -----------------------------------------------------

def _request(*, mode=C.Mode.SCOPED, deselect=(), shadow=False, probe=None):
    return C.RunRequest(mode=mode, deselect=tuple(deselect), shadow=shadow,
                        probe=probe)


def test_prepare_run_sets_record_env_for_pytest_scoped(fakes, tmp_path):
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), _config(), _request(),
        Path(tmp_path) / "report.json", "ab" * 16)
    assert (C.SELECTION_RECORD_ENV, "1") in env
    assert binding is None
    assert C.SELECTION_DESELECT_ENV not in dict(env)


def test_prepare_run_writes_binding_for_deselect(fakes, tmp_path):
    report = Path(tmp_path) / "report.json"
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), _config(),
        _request(deselect=("tests/test_a.py::test_x",)), report, "ab" * 16)
    assert dict(env)[C.SELECTION_DESELECT_ENV] == str(binding)
    assert fakes.ingest_mod.bindings == [("tests/test_a.py::test_x",)]


def test_prepare_run_drops_unsafe_ids(fakes, tmp_path, monkeypatch):
    request = _request(deselect=("tests/test_a.py::test_x",
                                 "tests/test_a.py::test_y"))
    monkeypatch.setattr(
        C, "selection_nodeid_safe",
        lambda nid: nid == "tests/test_a.py::test_x")
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), _config(), request,
        Path(tmp_path) / "report.json", "ab" * 16)
    assert binding is not None
    assert fakes.ingest_mod.bindings == [("tests/test_a.py::test_x",)]


@pytest.mark.parametrize("config, req", [
    (_config(dynamic=False), _request()),
    (_config(enabled=False), _request()),
    (_config(runner="command"), _request()),
])
def test_prepare_run_stays_silent_otherwise(fakes, tmp_path, config, req):
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), config, req, Path(tmp_path) / "report.json",
        "ab" * 16)
    assert env == ()
    assert binding is None


def test_prepare_run_binding_failure_runs_everything(fakes, tmp_path,
                                                     monkeypatch):
    def boom(report_path, run_id, nodeids):
        raise C.Problem(code="state-unavailable", message="disk gone",
                       phase="test")
    monkeypatch.setattr(fakes.ingest_mod, "write_deselect_binding", boom)
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), _config(),
        _request(deselect=("tests/test_a.py::test_x",)),
        Path(tmp_path) / "report.json", "ab" * 16)
    assert (C.SELECTION_RECORD_ENV, "1") in env
    assert binding is None
    assert C.SELECTION_DESELECT_ENV not in dict(env)


def test_prepare_run_never_raises(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(fakes.engine, "_ingest",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    env, binding = fakes.engine.prepare_run(
        _domain(tmp_path), _config(),
        _request(deselect=("tests/test_a.py::test_x",)),
        Path(tmp_path) / "report.json", "ab" * 16)
    assert (C.SELECTION_RECORD_ENV, "1") in env
    assert binding is None


# --- after_run -------------------------------------------------------

def _after(fakes, tmp_path, capsys=None, **overrides):
    params = {
        "domain": _domain(tmp_path),
        "config": _config(),
        "project_id": "ab" * 16,
        "run_id": "cd" * 16,
        "report_path": Path(tmp_path) / "native-a001.json",
        "project_root": Path(tmp_path),
        "expected_workers": 1,
        "execution": "scoped",
        "valid_handoff": True,
        "unchanged_inputs": True,
        "cancelled": False,
        "guard_failed": False,
        "setup_failed": False,
        "argv_files": ("tests/test_a.py",),
        "verbose": False,
        "quiet": False,
    }
    params.update(overrides)
    return fakes.engine.after_run(**params)


def _kinds(calls):
    return [call[0] for call in calls]


def test_cleanup_removes_ingest_files(fakes, tmp_path):
    report = Path(tmp_path) / "native-a001.json"
    fakes.engine.cleanup(report)
    assert fakes.ingest_mod.cleaned == [report]


def test_cleanup_without_ingest_or_report_never_raises(fakes, tmp_path,
                                                       monkeypatch):
    fakes.engine.cleanup(None)
    assert fakes.ingest_mod.cleaned == []
    monkeypatch.setattr(fakes.engine, "_ingest",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    fakes.engine.cleanup(Path(tmp_path) / "native-a001.json")


def test_after_run_valid_scoped_updates_without_cleaning_up(
        fakes, tmp_path):
    # Ingest-file cleanup is owned by operations' outer finally
    # (engine.cleanup), not by after_run.
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed")])
    _after(fakes, tmp_path)
    assert ("update", "cd" * 16) in fakes.store.calls
    assert fakes.ingest_mod.cleaned == []
    assert "mark_outcomes" not in _kinds(fakes.store.calls)
    assert "invalidate" not in _kinds(fakes.store.calls)
    # The recorded versions are made diffable for later plans.
    assert ("ensure_cached", fakes.store) in fakes.source_mod.builds


def test_after_run_valid_scoped_verbose_reports_recorded(fakes, tmp_path,
                                                         capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed"),
               ("tests/test_a.py::test_y", "passed")])
    _after(fakes, tmp_path, verbose=True)
    err = capsys.readouterr().err
    assert "ptest: -v selection: recorded 2 tests from 1 process" in err


def test_after_run_cancelled_keeps_failures_selected(fakes, tmp_path,
                                                    capsys):
    """D10/N4: a failure seen before the cancel keeps its test selected;
    nothing is recorded and nothing is invalidated."""
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed"),
               ("tests/test_a.py::test_y", "passed")])
    _after(fakes, tmp_path, cancelled=True, verbose=True, argv_files=None)
    kinds = _kinds(fakes.store.calls)
    assert "update" not in kinds
    assert "invalidate" not in kinds
    assert ("mark_outcomes",
            {"tests/test_a.py::test_x": "failed"}) in fakes.store.calls
    assert fakes.ingest_mod.cleaned == []
    assert "not recorded: the run was cancelled" in capsys.readouterr().err


def test_after_run_changed_inputs_marks_failed_only(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed"),
               ("tests/test_a.py::test_y", "passed")])
    _after(fakes, tmp_path, unchanged_inputs=False,
           argv_files=("tests/test_a.py",))
    kinds = _kinds(fakes.store.calls)
    assert "update" not in kinds
    assert "invalidate" not in kinds
    assert ("mark_outcomes",
            {"tests/test_a.py::test_x": "failed"}) in fakes.store.calls


def test_after_run_report_invalid_marks_without_update(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "error")])
    _after(fakes, tmp_path, valid_handoff=False,
           argv_files=("tests/test_a.py",))
    assert ("mark_outcomes",
            {"tests/test_a.py::test_x": "error"}) in fakes.store.calls
    assert "invalidate" not in _kinds(fakes.store.calls)
    assert ("update", "cd" * 16) not in fakes.store.calls


def test_after_run_incomplete_files_mark_and_invalidate(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        complete=False,
        nodes=[("tests/test_a.py::test_x", "failed")])
    _after(fakes, tmp_path, argv_files=("tests/test_a.py",))
    assert ("mark_outcomes",
            {"tests/test_a.py::test_x": "failed"}) in fakes.store.calls
    assert ("invalidate", ("tests/test_a.py",)) in fakes.store.calls
    assert ("update", "cd" * 16) not in fakes.store.calls


@pytest.mark.parametrize("flag", ["guard_failed", "setup_failed"])
def test_after_run_guard_or_setup_failure_writes_no_records(fakes, tmp_path,
                                                             flag):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed")])
    _after(fakes, tmp_path, **{flag: True})
    assert ("update", "cd" * 16) not in fakes.store.calls


def test_after_run_incomplete_run_invalidates_full(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(complete=False, nodes=[])
    _after(fakes, tmp_path, execution="full", argv_files=None)
    assert ("invalidate", None) in fakes.store.calls
    assert ("update", "cd" * 16) not in fakes.store.calls


def test_after_run_recording_disabled_notes_inactive(fakes, tmp_path,
                                                       capsys):
    # The bridge inactivity report is persisted so the frozen
    # ``test Python 3.N ...`` / ``recording was unavailable: ...``
    # static reasons can print on later runs. Cleanup is owned by
    # operations' outer finally, not by after_run.
    fakes.ingest_mod._run = _run_deps(
        recording=False, inactive="Python 3.11 has no sys.monitoring",
        python=(3, 11), nodes=[])
    _after(fakes, tmp_path, verbose=True)
    assert ("update", "cd" * 16) not in fakes.store.calls
    assert ("note_inactive", "Python 3.11 has no sys.monitoring",
            (3, 11)) in fakes.store.calls
    assert fakes.ingest_mod.cleaned == []
    assert "not recorded" in capsys.readouterr().err


def test_after_run_success_clears_stale_inactive(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed")])
    _after(fakes, tmp_path)
    assert ("update", "cd" * 16) in fakes.store.calls
    assert ("note_inactive", None, None) in fakes.store.calls


def test_missing_store_means_no_records_yet(fakes, tmp_path, monkeypatch):
    def missing(domain, project_id, *, create):
        raise C.Problem(code="state-unavailable",
                        message="selection store does not exist",
                        phase="test")

    monkeypatch.setattr(fakes.store_mod, "open_store", missing)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "no dependency records yet — any run records them"
    assert out.kind == "selected"


def test_broken_store_means_store_unavailable(fakes, tmp_path, monkeypatch):
    def broken(domain, project_id, *, create):
        raise C.Problem(code="coordinator-corrupt",
                        message="selection store has an unknown schema",
                        phase="test")

    monkeypatch.setattr(fakes.store_mod, "open_store", broken)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == "dependency store unavailable"


def test_after_run_never_raises(fakes, tmp_path, monkeypatch, capsys):
    fakes.store_mod._error = C.Problem(code="coordinator-corrupt", message="bad schema",
                                      phase="test")
    _after(fakes, tmp_path, verbose=True)
    assert "not recorded: dependency store unavailable" in \
        capsys.readouterr().err
    monkeypatch.setattr(fakes.engine, "_ingest",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    _after(fakes, tmp_path, verbose=True)
    assert "not recorded: dependency ingest is unavailable" in \
        capsys.readouterr().err


# --- self-audit ------------------------------------------------------

def test_self_audit_order_and_miss_line(fakes, tmp_path, capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ("tests/test_a.py::test_x",)
    fakes.source_mod._index = FakeIndex(
        files={"tests/test_a.py": FakeFile("digest-a")})
    _after(fakes, tmp_path, execution="full", argv_files=None)
    kinds = _kinds(fakes.store.calls)
    assert kinds.index("snapshot") < kinds.index("demote") < \
        kinds.index("update")
    assert ("demote",
            {"tests/test_a.py::test_x": "digest-a"}) in fakes.store.calls
    assert ("record_audit", 1, 1) in fakes.store.calls
    err = capsys.readouterr().err
    assert "ptest: selection audit: 1 failing test would not have been " \
        "selected — they now run whenever a change statically reaches " \
        "them" in err


def test_self_audit_miss_plural(fakes, tmp_path, capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed"),
               ("tests/test_a.py::test_y", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ("tests/test_a.py::test_x",
                                 "tests/test_a.py::test_y")
    fakes.source_mod._index = FakeIndex(
        files={"tests/test_a.py": FakeFile("digest-a")})
    _after(fakes, tmp_path, execution="full", argv_files=None)
    assert "ptest: selection audit: 2 failing tests would not have been " \
        "selected" in capsys.readouterr().err


def test_self_audit_unindexed_miss_is_not_claimed(fakes, tmp_path, capsys):
    """A miss with no indexed test file (a src doctest) cannot be pinned.

    Before the fix its '' digest made store.demote raise, which also lost
    every other demotion and the audit counters, while the line still
    claimed the tests were pinned.
    """
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed"),
               ("pkg/mod.py::pkg.mod.helper", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ("pkg/mod.py::pkg.mod.helper",
                                 "tests/test_a.py::test_x")
    fakes.source_mod._index = FakeIndex(
        files={"tests/test_a.py": FakeFile("digest-a")})
    _after(fakes, tmp_path, execution="full", argv_files=None)
    assert ("demote",
            {"tests/test_a.py::test_x": "digest-a"}) in fakes.store.calls
    assert ("record_audit", 2, 2) in fakes.store.calls
    assert "ptest: selection audit: 1 failing test would not have been " \
        "selected" in capsys.readouterr().err


def test_self_audit_only_unindexed_misses_prints_no_claim(fakes, tmp_path,
                                                          capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("pkg/mod.py::pkg.mod.helper", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ("pkg/mod.py::pkg.mod.helper",)
    fakes.source_mod._index = FakeIndex(files={})
    _after(fakes, tmp_path, execution="full", argv_files=None)
    assert "demote" not in _kinds(fakes.store.calls)
    assert ("record_audit", 1, 1) in fakes.store.calls
    assert "ptest: selection audit:" not in capsys.readouterr().err


def test_self_audit_verbose_summary_without_misses(fakes, tmp_path, capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ()
    fakes.source_mod._index = FakeIndex(files={})
    _after(fakes, tmp_path, execution="full", argv_files=None, verbose=True)
    err = capsys.readouterr().err
    assert "ptest: selection audit:" not in err
    assert "ptest: -v selection audit: 1 failing test checked · 0 misses" \
        in err
    assert ("record_audit", 1, 0) in fakes.store.calls


def test_self_audit_scoped_runs_skip_audit(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed")])
    fakes.planner_mod._misses = ("tests/test_a.py::test_x",)
    _after(fakes, tmp_path, execution="scoped",
           argv_files=("tests/test_a.py",))
    assert "demote" not in _kinds(fakes.store.calls)
    assert "record_audit" not in _kinds(fakes.store.calls)


# --- status_lines --------------------------------------------------------

def test_status_lines_name_existing_stores_only(fakes, tmp_path):
    domain = _domain(tmp_path)
    fakes.store._meta = _meta(nodes=12, size=2048, newest=None)
    lines = fakes.engine.status_lines(
        domain, [(None, _config()), ("child", _config())])
    assert lines == [
        "selection store: 12 tests recorded · 2.0 KB · newest unknown ago",
        "selection store child: 12 tests recorded · 2.0 KB · "
        "newest unknown ago",
    ]


def test_status_lines_skip_missing_stores(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(fakes.engine, "_store", lambda: (_ for _ in (
    )).throw(ImportError("gone")))
    assert fakes.engine.status_lines(
        _domain(tmp_path), [(None, _config())]) == []


# === 0.7.0 tiered iteration selection ======================================

RUN = "ab" * 16
KEY = b"k" * 64


def _tiered(fakes, tmp_path, tiers, *, files, deselect=(), via=None,
            weights=None, config=None, static=None, **extra):
    """Refine one static verdict against a decision built from ``tiers``."""
    selected = sum(1 for key, (tier, _) in tiers.items()
                   if tier == T1 and "::" in key)
    fakes.planner_mod._decision = _decision(
        files=files, deselect=deselect, tiers=tiers, selected=selected,
        via=via or {}, weights=weights or {}, **extra)
    top = Path(tmp_path) if (Path(tmp_path) / ".git").exists() else None
    return fakes.engine.refine(None, top, tmp_path, config or _config(),
                               (), static or _static_ok())


# --- the report (R30, R34) ---------------------------------------------------

def test_report_counts_run_now_and_deferred_with_complete_aggregates(
        fakes, tmp_path):
    tiers = {
        "tests/test_a.py::t0": (T1, "d"),
        "tests/test_a.py::t1": (T2, "g-shared"),
        "tests/test_b.py::t0": (T2, "b-static"),
        "tests/test_c.py": (T2, "u-static"),
    }
    out = _tiered(
        fakes, tmp_path, tiers, files=("tests/test_a.py",),
        deselect=("tests/test_a.py::t1",),
        via={"tests/test_a.py::t1": "pkg/models.py",
             "tests/test_b.py::t0": "data/x.json",
             "tests/test_c.py": "pkg/new.py"},
        weights={"tests/test_c.py": 3})
    report = out.selection
    assert report.engine == "dynamic" and report.iteration is True
    assert (report.run_now, report.deferred) == (1, 5)
    assert report.deferred_by_reason == (
        ("g-shared", "pkg/models.py", 1), ("b-static", "data/x.json", 1),
        ("u-static", "pkg/new.py", 3))
    assert report.deferred_by_file == (
        ("tests/test_a.py", 1), ("tests/test_b.py", 1),
        ("tests/test_c.py", 3))
    assert report.full_reason is None and report.gate is None
    assert out.kind == "selected" and out.tests == 1
    assert out.deselect == ("tests/test_a.py::t1",)


def test_unrecorded_file_key_weighs_at_least_one(fakes, tmp_path):
    out = _tiered(fakes, tmp_path,
                  {"tests/test_a.py::t0": (T1, "d"),
                   "tests/test_new.py": (T1, "n")},
                  files=("tests/test_a.py", "tests/test_new.py"),
                  weights={"tests/test_new.py": 4})
    assert out.selection.run_now == 5
    assert out.tests == 5


def test_restored_deselect_nodes_count_as_run(fakes, tmp_path, monkeypatch):
    """R5: T2 nodes the request cannot keep deselected run and leave the
    deferred count (an unsafe id restores its file; over the id cap
    nothing is deselected)."""
    long_id = "tests/test_a.py::test_p[" + "x" * 5000 + "]"
    tiers = {
        "tests/test_a.py::t0": (T1, "d"),
        long_id: (T2, "b-static"),
        "tests/test_a.py::t1": (T2, "b-static"),
        "tests/test_b.py::t0": (T2, "b-static"),
    }
    via = {long_id: "data/x.json", "tests/test_a.py::t1": "data/x.json",
           "tests/test_b.py::t0": "data/x.json"}
    out = _tiered(
        fakes, tmp_path, tiers, files=("tests/test_a.py",),
        deselect=(long_id, "tests/test_a.py::t1", "tests/test_a.py::t9"),
        via=via)
    report = out.selection
    assert out.deselect == ()
    # t0 + the unsafe id + its sibling t1 + the unaffected t9 all run now.
    assert report.run_now == 4 == out.tests
    assert report.deferred == 1
    assert report.deferred_by_reason == (("b-static", "data/x.json", 1),)
    assert report.deferred_by_file == (("tests/test_b.py", 1),)

    monkeypatch.setattr(C, "SELECTION_DESELECT_MAX_IDS", 1)
    safe_tiers = {"tests/test_a.py::t0": (T1, "d"),
                  "tests/test_a.py::t1": (T2, "b-static"),
                  "tests/test_a.py::t2": (T2, "b-static")}
    out = _tiered(fakes, tmp_path, safe_tiers, files=("tests/test_a.py",),
                  deselect=("tests/test_a.py::t1", "tests/test_a.py::t2"),
                  via={"tests/test_a.py::t1": "p.json",
                       "tests/test_a.py::t2": "p.json"})
    assert out.deselect == ()
    assert (out.selection.run_now, out.selection.deferred) == (3, 0)


def test_unbindable_conditional_node_runs(fakes, tmp_path):
    """R44: a conditional node whose id cannot be bound is promoted
    unconditionally: it runs, never drops."""
    long_id = "tests/test_a.py::test_p[" + "x" * 5000 + "]"
    fakes.store._snapshot = _snapshot(observers={long_id: frozenset({"*"})})
    tiers = {long_id: (T2, "g-shared"),
             "tests/test_a.py::t1": (T2, "g-shared")}
    out = _tiered(
        fakes, tmp_path, tiers, files=("tests/test_a.py",),
        deselect=(long_id, "tests/test_a.py::t1"),
        via={long_id: "pkg/m.py", "tests/test_a.py::t1": "pkg/m.py"},
        conditional=((long_id, RUN),),
        config=_config(ambient_fingerprint=("echo", "x")))
    report = out.selection
    assert out.conditional == () and report.conditional == ()
    assert out.kind == "selected"
    assert (report.run_now, report.deferred) == (2, 0)
    assert report.observers_promoted == 1 and report.observers_deferred == 0


def test_conditional_entries_carry_the_chosen_runs_fingerprint(fakes,
                                                               tmp_path):
    node = "tests/test_a.py::t0"
    fakes.store._snapshot = _snapshot(
        fingerprint="cd" * 32, observers={node: frozenset({"pkg/m.py"})})
    out = _tiered(
        fakes, tmp_path,
        {node: (T2, "g-shared"), "tests/test_a.py::t1": (T2, "g-shared")},
        files=("tests/test_a.py",),
        deselect=(node, "tests/test_a.py::t1"),
        via={node: "pkg/m.py", "tests/test_a.py::t1": "pkg/m.py"},
        conditional=((node, RUN),),
        config=_config(ambient_fingerprint=("echo", "x")))
    # Nothing is certain to run: the plan keeps the observer's file so the
    # run can promote it, but its kind is "none".
    assert out.kind == "none" and out.tests == 0
    assert out.files == ("tests/test_a.py",)
    assert out.deselect == (node, "tests/test_a.py::t1")
    assert out.conditional == ((node, "cd" * 32),)
    report = out.selection
    assert report.conditional == ((node, "g-shared", "pkg/m.py"),)
    assert (report.run_now, report.deferred) == (0, 2)
    assert report.observers_deferred == 1
    assert fakes.planner_mod.inputs_seen[0].conditional is True


def test_no_conditional_flag_without_a_declared_fingerprint(fakes, tmp_path):
    _tiered(fakes, tmp_path, {"tests/test_a.py::t0": (T1, "d")},
            files=("tests/test_a.py",))
    inputs = fakes.planner_mod.inputs_seen[0]
    assert inputs.conditional is False and inputs.audit is False


def test_config_without_new_keys_plans_identically(fakes, tmp_path):
    """R46: the same config bytes plan the same way as 0.6.8 - no merged
    observers, no since-gate paths, no conditional entries - and the
    request carries exactly the decision's files and deselect."""
    tiers = {"tests/test_a.py::t0": (T1, "d"),
             "tests/test_a.py::t1": (T2, "b-static")}
    out = _tiered(fakes, tmp_path, tiers, files=("tests/test_a.py",),
                  deselect=("tests/test_a.py::t1",),
                  via={"tests/test_a.py::t1": "p.json"})
    inputs = fakes.planner_mod.inputs_seen[0]
    assert dict(inputs.observers) == {}
    assert inputs.since_gate == frozenset()
    assert inputs.conditional is False
    assert (out.files, out.deselect, out.conditional) == (
        ("tests/test_a.py",), ("tests/test_a.py::t1",), ())
    assert repr(_config().selection) == repr(C.SelectionPolicy(
        enabled=True, closed_inputs=False, full_ratio=0.70, dynamic=True))


# --- observers (R13, R16) ----------------------------------------------------

@pytest.mark.parametrize("declared, expected", [
    # Literal prefixes only: a glob metacharacter is a plain character, a
    # name stops at a path boundary, and "tests" covers everything under it.
    (("tests/test_*",), set()),
    (("tests/test_a",), set()),
    (("tests/test_a.py",), {"tests/test_a.py::t0"}),
    (("tests",), {"tests/test_a.py::t0", "tests/test_ab.py::t0"}),
    (("other",), set()),
    ((), set()),
])
def test_observer_prefixes_match_literally(fakes, tmp_path, declared,
                                           expected):
    fakes.store._snapshot = _snapshot(nodes=[
        ("tests/test_a.py::t0", "passed"),
        ("tests/test_ab.py::t0", "passed")])
    _tiered(fakes, tmp_path, {"tests/test_a.py::t0": (T1, "d")},
            files=("tests/test_a.py",),
            config=_config(observers=declared))
    merged = fakes.planner_mod.inputs_seen[0].observers
    assert set(merged) == expected
    assert all(paths == frozenset({"*"}) for paths in merged.values())


def test_declared_prefixes_merge_with_learned_rows(fakes, tmp_path):
    node = "tests/test_a.py::t0"
    fakes.store._snapshot = _snapshot(
        nodes=[(node, "passed"), ("tests/test_b.py::t0", "passed")],
        observers={node: frozenset({"pkg/x.py"}),
                   "tests/test_b.py::t0": frozenset({"pkg/y.py"})})
    _tiered(fakes, tmp_path, {node: (T1, "d")}, files=("tests/test_a.py",),
            config=_config(observers=("tests/test_a.py",)))
    merged = fakes.planner_mod.inputs_seen[0].observers
    assert merged[node] == frozenset({"pkg/x.py", "*"})
    assert merged["tests/test_b.py::t0"] == frozenset({"pkg/y.py"})


# --- verdicts the dynamic planner did not decide (R34 matrix) -----------------

def test_trigger_full_verdict_reports_r8a_without_an_engine(fakes, tmp_path):
    static = FakeImpact(kind="full", reason="uv.lock is a full trigger")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (), static)
    assert out.kind == "full"
    report = out.selection
    assert report.engine is None and report.iteration is True
    assert (report.run_now, report.deferred) == (None, 0)
    assert report.full_reason == "uv.lock is a full trigger"
    assert fakes.store_mod.opened == []


def test_static_engine_full_verdict_reports_the_static_engine(fakes,
                                                               tmp_path):
    fakes.planner_mod._error = RuntimeError("boom")
    static = _static_ok(kind="full",
                        reason="10 of 10 test files reaches full_ratio 0.7")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (), static)
    report = out.selection
    assert out.static_reason == "dynamic planner failed"
    assert report.engine == "static"
    assert (report.run_now, report.deferred) == (None, 0)
    assert report.full_reason == "10 of 10 test files reaches full_ratio 0.7"


def test_static_selection_reports_unknowable_counts(fakes, tmp_path):
    fakes.store._snapshot = _snapshot(compatibility="other")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.static_reason == \
        "dependency records are from another configuration"
    report = out.selection
    assert report.engine == "static"
    assert (report.run_now, report.deferred) == (None, None)
    assert report.gate_recorded is False


def test_non_pytest_runner_has_no_selection_report(fakes, tmp_path):
    out = fakes.engine.refine(None, None, tmp_path,
                              _config(runner="command"), (), _static_ok())
    assert out.selection is None


# --- empty diffs still plan (R10) -------------------------------------------

def _empty_static(**over):
    fields = {"kind": "none", "dynamic_ok": False, "relevant": ()}
    fields.update(over)
    return FakeImpact(**fields)


def test_empty_diff_runs_t1_nodes(fakes, tmp_path):
    fakes.planner_mod._decision = _decision(
        files=("tests/test_a.py",), selected=2)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _empty_static())
    assert out.kind == "selected" and out.engine == "dynamic"
    assert out.files == ("tests/test_a.py",) and out.tests == 2
    assert fakes.planner_mod.inputs_seen[0].changed == frozenset()


def test_empty_diff_with_only_deferred_tests_keeps_the_count(fakes,
                                                            tmp_path):
    out = _tiered(fakes, tmp_path,
                  {"tests/test_b.py::t0": (T2, "b-static")}, files=(),
                  via={"tests/test_b.py::t0": "tests/data/cfg.json"},
                  static=_empty_static())
    assert out.kind == "none" and out.files == ()
    assert out.selection.deferred == 1
    assert out.selection.deferred_by_reason == (
        ("b-static", "tests/data/cfg.json", 1),)


def test_empty_diff_without_records_keeps_the_verdict_silently(fakes,
                                                              tmp_path):
    fakes.store._snapshot = C.DependencySnapshot(
        vocabulary=C.DepVocabulary(paths=(), functions=(), fixtures=()),
        runs={}, nodes={}, fixtures={}, demotions={})
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _empty_static())
    assert out.kind == "none"
    assert out.static_reason == "" and out.details == ()
    assert out.selection.engine == "static"


@pytest.mark.parametrize("kind", ["b-static", "u-static"])
def test_deferral_survives_green_point_move(fakes, tmp_path, kind):
    """A deferral is measured since the gate, not since the green point: an
    empty diff (the green point moved past the change) still plans with the
    gate's since-gate paths and reports the deferred tests."""
    repo = _repo(tmp_path)
    head = support.git(repo, "rev-parse", "HEAD")
    gate = C.GateMarker(commit=head, dirty={}, recorded_at=1.0)
    (repo / "tests" / "data" / "cfg.json").write_text('{"a": 1}\n')
    fakes.store._snapshot = _snapshot(gate=gate)
    code = kind
    key = ("tests/test_a.py::t0" if kind == "b-static"
           else "tests/test_new.py")
    out = _tiered(
        fakes, repo, {key: (T2, code)}, files=(),
        via={key: "tests/data/cfg.json"}, static=_empty_static(),
        weights={"tests/test_new.py": 2})
    # The planner got the path although the diff handed to refine is empty.
    inputs = fakes.planner_mod.inputs_seen[0]
    assert inputs.changed == frozenset()
    assert inputs.since_gate == frozenset({"tests/data/cfg.json"})
    assert out.kind == "none"
    assert out.selection.deferred == (1 if kind == "b-static" else 2)
    assert out.selection.gate_recorded is True


# --- since the gate (R9, R27) ------------------------------------------------

def _repo(tmp_path, name="repo"):
    root = Path(tmp_path) / name
    support.init_git_repo(root, files={
        "pkg/__init__.py": "", "pkg/core.py": "A = 1\n",
        "tests/test_a.py": "def test_a():\n    pass\n",
        "tests/data/cfg.json": "{}\n"})
    return root.resolve()


def _since(engine, repo, gate, config=None):
    return engine._since_gate(KEY, repo, repo, config or _config(), gate)


def test_since_gate_lists_committed_dirty_and_untracked_project_paths(
        fakes, tmp_path):
    repo = _repo(tmp_path)
    gate = C.GateMarker(commit=support.git(repo, "rev-parse", "HEAD"),
                        dirty={}, recorded_at=1.0)
    assert _since(fakes.engine, repo, gate) == (frozenset(), True)
    (repo / "pkg" / "core.py").write_text("A = 2\n")
    support.git_commit_all(repo, "change core")
    (repo / "pkg" / "new.py").write_text("B = 1\n")
    (repo / "tests" / "data" / "cfg.json").write_text('{"k": 1}\n')
    (repo / "notes.md").write_text("ignored class\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"out")
    paths, recorded = _since(fakes.engine, repo, gate)
    assert recorded is True
    # Filtered exactly like the changed set: docs and tool output are gone.
    assert paths == frozenset({"pkg/core.py", "pkg/new.py",
                               "tests/data/cfg.json"})


@pytest.mark.parametrize("case", ["missing", "unknown-commit", "no-key",
                                  "no-top"])
def test_unusable_gate_marker_means_no_gate_recorded(fakes, tmp_path, case):
    repo = _repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("A = 2\n")
    gate = C.GateMarker(commit=support.git(repo, "rev-parse", "HEAD"),
                        dirty={}, recorded_at=1.0)
    key, top = KEY, repo
    if case == "missing":
        gate = None
    elif case == "unknown-commit":
        gate = C.GateMarker(commit="0" * 40, dirty={}, recorded_at=1.0)
    elif case == "no-key":
        key = None
    else:
        top = None
    assert fakes.engine._since_gate(key, top, repo, _config(), gate) == (
        frozenset(), False)


def test_marker_paths_that_leave_the_checkout_are_never_read(fakes, tmp_path):
    """The marker lives in a store file: hostile paths in it are ignored,
    not digested and not turned into planner inputs."""
    repo = _repo(tmp_path)
    outside = Path(tmp_path) / "outside.py"
    outside.write_text("SECRET = 1\n")
    gate = C.GateMarker(
        commit=support.git(repo, "rev-parse", "HEAD"),
        dirty={"../outside.py": "x", str(outside): "x", "/etc/hostname": "x",
               "pkg/../../outside.py": None},
        recorded_at=1.0)
    assert _since(fakes.engine, repo, gate) == (frozenset(), True)


def test_unusable_gate_marker_is_reported_by_refine(fakes, tmp_path):
    out = _tiered(fakes, tmp_path, {"tests/test_a.py::t0": (T1, "d")},
                  files=("tests/test_a.py",))
    assert out.selection.gate_recorded is False
    assert "since gate: no gate recorded" in out.details


def test_marker_with_too_many_dirty_paths_is_cleared(fakes, tmp_path,
                                                     monkeypatch):
    from ptest import lastgreen

    repo = _repo(tmp_path)
    monkeypatch.setattr(lastgreen, "MAX_GREEN_FILES", 2)
    for name in ("a", "b", "c"):
        (repo / "pkg" / f"{name}.py").write_text("X = 1\n")
    assert fakes.engine._gate_marker(KEY, repo) == (None, True)
    # Without git the marker is cleared too, never invented.
    assert fakes.engine._gate_marker(KEY, tmp_path) == (None, True)


def test_gate_dirty_digest_is_keyed_and_oversize_counts_changed(
        fakes, tmp_path, monkeypatch):
    import hashlib
    import hmac

    engine = fakes.engine
    repo = _repo(tmp_path)
    text = "A = 'dirty at the gate'\n"
    (repo / "pkg" / "core.py").write_text(text)
    marker, clear = engine._gate_marker(KEY, repo)
    assert clear is False
    assert marker.commit == support.git(repo, "rev-parse", "HEAD")
    digest = marker.dirty["pkg/core.py"]
    # Keyed under the domain key; never lastgreen's plain sha256.
    assert digest == hmac.new(KEY, text.encode(),
                              hashlib.sha256).hexdigest()
    assert digest != hashlib.sha256(text.encode()).hexdigest()
    # Unchanged since the gate: not in since_gate.
    assert _since(engine, repo, marker) == (frozenset(), True)
    # A change after the gate is.
    (repo / "pkg" / "core.py").write_text(text + "# more\n")
    assert _since(engine, repo, marker)[0] == frozenset({"pkg/core.py"})
    # Deleted after the gate digests to None: changed.
    (repo / "pkg" / "core.py").unlink()
    assert _since(engine, repo, marker)[0] == frozenset({"pkg/core.py"})

    # Over lastgreen's 2 MiB hash cap the keyed digest still sees a byte
    # change at the end (the plain content hash was None == None here).
    big = Path(repo) / "pkg" / "big.py"
    big.write_text("#" * (3 * 1024 * 1024) + "\nX = 1\n")
    marker, _ = engine._gate_marker(KEY, repo)
    assert marker.dirty["pkg/big.py"] not in (None, "?")
    assert "pkg/big.py" not in _since(engine, repo, marker)[0]
    big.write_text("#" * (3 * 1024 * 1024) + "\nX = 2\n")
    assert "pkg/big.py" in _since(engine, repo, marker)[0]

    # Over the read cap the digest is "?": it compares as changed even
    # though the bytes did not move.
    monkeypatch.setattr(engine, "_FILE_READ_CAP", 8)
    marker, _ = engine._gate_marker(KEY, repo)
    assert marker.dirty["pkg/big.py"] == "?"
    assert "pkg/big.py" in _since(engine, repo, marker)[0]


# --- R32 detail lines ----------------------------------------------------------

def test_detail_lines_name_tiers_deferred_top_observers_and_gate(
        fakes, tmp_path):
    repo = _repo(tmp_path)
    gate = C.GateMarker(commit=support.git(repo, "rev-parse", "HEAD"),
                        dirty={}, recorded_at=time_ago(90))
    (repo / "pkg" / "core.py").write_text("A = 3\n")
    fakes.store._snapshot = _snapshot(
        gate=gate, nodes=[("tests/test_a.py::t0", "passed")],
        observers={"tests/test_a.py::t0": frozenset({"pkg/x.py"})})
    tiers = {"tests/test_a.py::t0": (T1, "d"),
             "tests/test_a.py::t1": (T1, "a"),
             "tests/test_b.py::t0": (T2, "g-shared"),
             "tests/test_b.py::t1": (T2, "g-shared"),
             "tests/test_c.py": (T2, "u-static")}
    out = _tiered(fakes, repo, tiers, files=("tests/test_a.py",),
                  via={"tests/test_b.py::t0": "pkg/m.py",
                       "tests/test_b.py::t1": "pkg/m.py",
                       "tests/test_c.py": "pkg/new.py"},
                  weights={"tests/test_c.py": 1},
                  config=_config(observers=("tests/test_a.py",)))
    details = out.details
    assert details[0].startswith("engine dynamic · ")
    assert details[1] == ("tiers: 2 run now (a 1, d 1) · 3 deferred "
                          "(g-shared 2, u-static 1)")
    assert details[2] == ("deferred top: tests/test_b.py 2 · "
                          "tests/test_c.py 1")
    assert details[3] == ("observers: 1 declared · 1 learned · 0 promoted"
                          " · 0 deferred")
    assert details[4].startswith("since gate: 1 path · gate 1m")
    assert details[4].endswith(" ago")


def time_ago(seconds):
    import time

    return time.time() - seconds


def test_detail_lines_without_deferred_have_no_top_line(fakes, tmp_path):
    out = _tiered(fakes, tmp_path, {"tests/test_a.py::t0": (T1, "d")},
                  files=("tests/test_a.py",))
    assert out.details[1] == "tiers: 1 run now (d 1) · 0 deferred (none)"
    assert not any(line.startswith("deferred top:") for line in out.details)


def test_deferred_top_is_capped_at_twenty_files(fakes, tmp_path):
    tiers = {f"tests/test_{i:02d}.py::t": (T2, "b-static")
             for i in range(25)}
    out = _tiered(fakes, tmp_path, tiers, files=(),
                  via={key: "p.json" for key in tiers},
                  static=_empty_static())
    top = next(line for line in out.details
               if line.startswith("deferred top:"))
    assert top.count(" · ") == 19


# --- narrow (R40) ----------------------------------------------------------------

def test_narrow_recounts_the_report_for_the_folders(fakes, tmp_path):
    tiers = {"tests/api/test_a.py::t0": (T1, "d"),
             "tests/api/test_a.py::t1": (T2, "b-static"),
             "tests/web/test_w.py::t0": (T1, "d"),
             "tests/web/test_w.py::t1": (T2, "g-shared")}
    out = _tiered(
        fakes, tmp_path, tiers,
        files=("tests/api/test_a.py", "tests/web/test_w.py"),
        deselect=("tests/api/test_a.py::t1", "tests/web/test_w.py::t1"),
        via={"tests/api/test_a.py::t1": "p.json",
             "tests/web/test_w.py::t1": "pkg/m.py"})
    narrowed = fakes.engine.narrow(out, ("tests/api",))
    assert narrowed.files == ("tests/api/test_a.py",)
    assert narrowed.deselect == ("tests/api/test_a.py::t1",)
    assert narrowed.tests == 1 and narrowed.kind == "selected"
    report = narrowed.selection
    assert (report.run_now, report.deferred) == (1, 1)
    assert report.deferred_by_reason == (("b-static", "p.json", 1),)
    assert report.deferred_by_file == (("tests/api/test_a.py", 1),)
    # "" is the whole project; a folder with nothing leaves nothing to run.
    whole = fakes.engine.narrow(out, ("",))
    assert whole.selection.run_now == 2 and whole.selection.deferred == 2
    empty = fakes.engine.narrow(out, ("tests/none",))
    assert empty.kind == "none" and empty.files == ()
    assert (empty.selection.run_now, empty.selection.deferred) == (0, 0)


def test_narrow_never_raises_and_leaves_undecided_verdicts_alone(fakes):
    static = FakeImpact(kind="selected",
                        files=("tests/api/test_a.py", "tests/web/test_w.py"))
    out = fakes.engine.narrow(static, ("tests/api",))
    assert out.files == ("tests/api/test_a.py",)
    assert fakes.engine.narrow(object(), ("x",)) is not None


# --- promote_conditional (R44) -----------------------------------------------

def _conditional_report(fakes, tmp_path):
    nodes = ["tests/test_a.py::t0", "tests/test_a.py::t1"]
    fakes.store._snapshot = _snapshot(
        fingerprint="cd" * 32,
        observers={nodes[0]: frozenset({"*"}), nodes[1]: frozenset({"*"})})
    tiers = {nodes[0]: (T2, "g-shared"), nodes[1]: (T2, "g-shared"),
             "tests/test_a.py::t2": (T2, "b-static")}
    out = _tiered(
        fakes, tmp_path, tiers, files=("tests/test_a.py",),
        deselect=tuple(tiers),
        via={nodes[0]: "pkg/m.py", nodes[1]: "pkg/m.py",
             "tests/test_a.py::t2": "p.json"},
        conditional=tuple((node, RUN) for node in nodes),
        config=_config(ambient_fingerprint=("echo", "x")))
    return nodes, out.selection


def test_promote_conditional_moves_observers_from_deferred_to_run(fakes,
                                                                  tmp_path):
    nodes, report = _conditional_report(fakes, tmp_path)
    assert (report.run_now, report.deferred) == (0, 3)
    assert report.observers_deferred == 2
    promoted = fakes.engine.promote_conditional(
        report, [nodes[0]], "ef" * 32)
    assert (promoted.run_now, promoted.deferred) == (1, 2)
    assert promoted.deferred_by_reason == (
        ("g-shared", "pkg/m.py", 1), ("b-static", "p.json", 1))
    assert promoted.deferred_by_file == (("tests/test_a.py", 2),)
    assert promoted.observers_promoted == 1
    assert promoted.observers_deferred == 1
    assert promoted.conditional == ((nodes[1], "g-shared", "pkg/m.py"),)
    assert promoted.fingerprint == "changed"
    # The input is a frozen value: the original report is untouched.
    assert report.run_now == 0


def test_promote_conditional_outcome_words(fakes, tmp_path):
    nodes, report = _conditional_report(fakes, tmp_path)
    same = fakes.engine.promote_conditional(report, [], "ef" * 32)
    assert same.fingerprint == "unchanged"
    assert (same.run_now, same.deferred) == (0, 3)
    absent = fakes.engine.promote_conditional(
        report, nodes, None, reason="exit 3")
    assert absent.fingerprint == "absent"
    assert absent.fingerprint_reason == "exit 3"
    assert (absent.run_now, absent.deferred) == (2, 1)
    assert absent.conditional == ()
    word = fakes.engine.promote_conditional(report, [], "changed")
    assert word.fingerprint == "changed"


def test_promote_conditional_ignores_ids_that_are_not_conditional(
        fakes, tmp_path):
    _, report = _conditional_report(fakes, tmp_path)
    out = fakes.engine.promote_conditional(
        report, ["tests/test_a.py::t2", "tests/other.py::x"], "ef" * 32)
    assert (out.run_now, out.deferred) == (0, 3)


# --- rewrite_binding ------------------------------------------------------------

def test_rewrite_binding_uses_the_ingest_writer(fakes, tmp_path):
    report = Path(tmp_path) / "native-a001.json"
    assert fakes.engine.rewrite_binding(
        report, RUN, ["tests/test_a.py::t1"]) is True
    assert fakes.ingest_mod.rewrites == [
        (report, RUN, ("tests/test_a.py::t1",))]


def test_rewrite_binding_reports_failure_and_never_raises(fakes, tmp_path,
                                                          monkeypatch):
    fakes.ingest_mod.rewrite_error = OSError("rename refused")
    assert fakes.engine.rewrite_binding(
        Path(tmp_path) / "r.json", RUN, []) is False
    monkeypatch.setattr(fakes.engine, "_ingest",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    assert fakes.engine.rewrite_binding(
        Path(tmp_path) / "r.json", RUN, []) is False


# --- status (R19) --------------------------------------------------------------

def test_status_line_counts_learned_observers(fakes, tmp_path):
    fakes.store._meta = _meta(nodes=12, size=2048, newest=None, observers=3)
    assert fakes.engine.status_lines(_domain(tmp_path),
                                     [(None, _config())]) == [
        "selection store: 12 tests recorded · 2.0 KB · newest unknown ago"
        " · 3 learned observers"]
    fakes.store._meta = _meta(nodes=12, size=2048, newest=None, observers=1)
    assert fakes.engine.status_lines(_domain(tmp_path),
                                     [(None, _config())])[0].endswith(
        " · 1 learned observer")


# --- after_run: gate, observers, marker, fingerprint --------------------------

def _gate_audit(**over):
    fields = dict(previously_deferred=0, failed=0, learned={}, unlearned=(),
                  misses=())
    fields.update(over)
    return GateAudit(**fields)


def _gate(fakes, tmp_path, audit=None, nodes=(), **overrides):
    fakes.ingest_mod._run = _run_deps(nodes=list(nodes))
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._audit = audit or _gate_audit()
    fakes.source_mod._index = FakeIndex(
        files={"tests/test_a.py": FakeFile("digest-a")})
    params = {"execution": "full", "argv_files": None}
    params.update(overrides)
    return _after(fakes, tmp_path, **params)


def test_cancelled_run_writes_no_tier_state(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "failed")])
    out = _after(fakes, tmp_path, cancelled=True, execution="full",
                 argv_files=None, fingerprint="ab" * 32)
    assert out is None
    assert fakes.store.updates == []
    assert "update" not in _kinds(fakes.store.calls)
    # A failure seen before the cancel still widens selection.
    assert ("mark_outcomes",
            {"tests/test_a.py::t0": "failed"}) in fakes.store.calls


@pytest.mark.parametrize("case", ["rejected-update", "incomplete",
                                  "invalid-handoff", "guard-failed",
                                  "setup-failed", "inputs-changed"])
def test_rejected_or_incomplete_ingest_writes_no_observers_or_marker(
        fakes, tmp_path, capsys, case):
    audit = _gate_audit(previously_deferred=1, failed=1, learned={
        "tests/test_a.py::t1": frozenset({"pkg/m.py"})})
    overrides = {}
    nodes = [("tests/test_a.py::t1", "failed")]
    complete = True
    if case == "rejected-update":
        fakes.store.update_error = C.Problem(
            code="capacity-exceeded", message="full", phase="test")
    elif case == "incomplete":
        complete = False
    elif case == "invalid-handoff":
        overrides["valid_handoff"] = False
    elif case == "guard-failed":
        overrides["guard_failed"] = True
    elif case == "setup-failed":
        overrides["setup_failed"] = True
    else:
        overrides["unchanged_inputs"] = False
    fakes.ingest_mod._run = _run_deps(nodes=nodes, complete=complete)
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._audit = audit
    fakes.source_mod._index = FakeIndex(files={})
    out = _after(fakes, tmp_path, execution="full", argv_files=None,
                 fingerprint="ab" * 32, verbose=True, **overrides)
    # No gate report, no learned rows, no marker, no gate line.
    assert out is None
    assert fakes.store.updates == []
    assert "selection gate:" not in capsys.readouterr().err


def test_failed_tests_still_record_with_failed_outcome(fakes, tmp_path):
    """Regression of the corrected fact: a run whose tests failed is
    ingested, with the failed outcome on its record."""
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "failed"),
               ("tests/test_a.py::t1", "passed")])
    _after(fakes, tmp_path, fingerprint="ab" * 32)
    assert len(fakes.store.updates) == 1
    update = fakes.store.updates[0]
    assert update["run"].nodes["tests/test_a.py::t0"].outcome == "failed"
    assert "mark_outcomes" not in _kinds(fakes.store.calls)


@pytest.mark.parametrize("given, expected", [
    ("ab" * 32, "ab" * 32), (None, None), ("short", None),
    ("AB" * 32, None), ("zz" * 32, None)])
def test_fingerprint_is_recorded_on_every_ingest_when_valid(
        fakes, tmp_path, given, expected):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "passed")])
    _after(fakes, tmp_path, fingerprint=given)
    assert fakes.store.updates[0]["fingerprint"] == expected


def test_ambient_fingerprint_is_an_accepted_alias_of_fingerprint(fakes,
                                                                 tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "passed")])
    _after(fakes, tmp_path, ambient_fingerprint="cd" * 32)
    assert fakes.store.updates[0]["fingerprint"] == "cd" * 32


def test_iteration_ingest_writes_no_observers_and_no_marker(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "passed")])
    assert _after(fakes, tmp_path) is None
    update = fakes.store.updates[0]
    assert update["observers"] is None and update["gate"] is None
    assert update["clear_gate"] is False and update["full"] is False


def test_gate_ingest_writes_the_marker_and_returns_the_gate_report(
        fakes, tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("A = 9\n")
    report = _gate(fakes, tmp_path, project_root=repo, verbose=True,
                   nodes=[("tests/test_a.py::t0", "passed")])
    update = fakes.store.updates[0]
    assert update["full"] is True and update["clear_gate"] is False
    marker = update["gate"]
    assert marker.commit == support.git(repo, "rev-parse", "HEAD")
    assert set(marker.dirty) == {"pkg/core.py"}
    assert report == C.SelectionGateReport(
        previously_deferred=0, failed=0, observers_learned=0,
        observers_evicted=0, unlearned=0, demoted=0)
    assert "ptest: -v selection gate: nothing was deferred" in \
        capsys.readouterr().err


def test_gate_without_git_clears_the_marker(fakes, tmp_path):
    _gate(fakes, tmp_path, nodes=[("tests/test_a.py::t0", "passed")])
    update = fakes.store.updates[0]
    assert update["gate"] is None and update["clear_gate"] is True


def test_gate_audit_plans_with_the_iteration_rules(fakes, tmp_path):
    repo = _repo(tmp_path)
    gate = C.GateMarker(commit=support.git(repo, "rev-parse", "HEAD"),
                        dirty={}, recorded_at=1.0)
    (repo / "pkg" / "core.py").write_text("A = 5\n")
    fakes.store._snapshot = _snapshot(
        gate=gate, nodes=[("tests/test_a.py::t0", "passed")],
        observers={"tests/test_a.py::t0": frozenset({"pkg/x.py"})})
    _gate(fakes, tmp_path, project_root=repo,
          nodes=[("tests/test_a.py::t0", "failed")])
    inputs = fakes.planner_mod.inputs_seen[0]
    assert inputs.audit is True and inputs.conditional is False
    assert inputs.changed == frozenset()
    assert inputs.since_gate == frozenset({"pkg/core.py"})
    assert inputs.observers["tests/test_a.py::t0"] == frozenset(
        {"pkg/x.py"})


def test_deferred_failure_learns_or_counts_unlearned(fakes, tmp_path,
                                                     capsys):
    """R15: (a) learn without demoting, (b) only count, (c) demote, and
    observers_learned counts tests, not rows."""
    audit = _gate_audit(
        previously_deferred=5, failed=4,
        learned={"tests/test_a.py::t1": frozenset({"pkg/m.py", "pkg/n.py"}),
                 "tests/test_a.py::t2": frozenset({"pkg/m.py"})},
        unlearned=("tests/test_b.py::t3",),
        misses=("tests/test_a.py::t4",))
    report = _gate(
        fakes, tmp_path, audit=audit,
        nodes=[("tests/test_a.py::t1", "failed"),
               ("tests/test_a.py::t2", "failed"),
               ("tests/test_b.py::t3", "failed"),
               ("tests/test_a.py::t4", "failed")])
    update = fakes.store.updates[0]
    assert update["observers"] == {
        "tests/test_a.py::t1": frozenset({"pkg/m.py", "pkg/n.py"}),
        "tests/test_a.py::t2": frozenset({"pkg/m.py"})}
    # Only the miss is demoted; learned and unlearned nodes never are.
    assert ("demote", {"tests/test_a.py::t4": "digest-a"}) in \
        fakes.store.calls
    assert report == C.SelectionGateReport(
        previously_deferred=5, failed=4, observers_learned=2,
        observers_evicted=0, unlearned=1, demoted=1)
    err = capsys.readouterr().err
    assert ("ptest: selection gate: ran 5 previously deferred tests · "
            "4 failed · 2 observers learned · 1 failed without a "
            "learnable path") in err
    assert "ptest: selection audit: 1 failing test would not have been " \
        "selected" in err


def test_gate_line_uses_singular_counts_and_skips_zero_unlearned(
        fakes, tmp_path, capsys):
    audit = _gate_audit(previously_deferred=1, failed=1, learned={
        "tests/test_a.py::t1": frozenset({"pkg/m.py"})})
    _gate(fakes, tmp_path, audit=audit,
          nodes=[("tests/test_a.py::t1", "failed")])
    err = capsys.readouterr().err
    assert ("ptest: selection gate: ran 1 previously deferred test · "
            "1 failed · 1 observer learned\n") in err
    assert "learnable path" not in err


def test_unlearned_only_gate_prints_the_unlearned_clause(fakes, tmp_path,
                                                          capsys):
    audit = _gate_audit(previously_deferred=2, failed=1,
                        unlearned=("tests/test_b.py::t3",))
    report = _gate(fakes, tmp_path, audit=audit,
                   nodes=[("tests/test_b.py::t3", "failed")])
    assert report.unlearned == 1 and report.observers_learned == 0
    assert fakes.store.updates[0]["observers"] is None
    assert ("2 previously deferred tests · 1 failed · 0 observers learned"
            " · 1 failed without a learnable path") in \
        capsys.readouterr().err


def test_known_observer_rows_are_not_relearned(fakes, tmp_path):
    node = "tests/test_a.py::t1"
    fakes.store._snapshot = _snapshot(
        observers={node: frozenset({"*"}),
                   "tests/test_a.py::t2": frozenset({"pkg/m.py"})})
    audit = _gate_audit(
        previously_deferred=2, failed=2,
        learned={node: frozenset({"pkg/m.py"}),
                 "tests/test_a.py::t2": frozenset({"pkg/m.py", "pkg/n.py"})})
    report = _gate(fakes, tmp_path, audit=audit,
                   nodes=[(node, "failed"),
                          ("tests/test_a.py::t2", "failed")])
    # t1 already observes every path; t2 only gains the new path.
    assert fakes.store.updates[0]["observers"] == {
        "tests/test_a.py::t2": frozenset({"pkg/n.py"})}
    assert report.observers_learned == 1


def test_observer_cap_eviction_is_reported_not_silent(fakes, tmp_path,
                                                      capsys):
    fakes.store.evicted = 7
    report = _gate(fakes, tmp_path, verbose=True,
                   nodes=[("tests/test_a.py::t0", "passed")])
    assert report.observers_evicted == 7
    err = capsys.readouterr().err
    assert ("ptest: -v selection gate: 7 observer rows evicted "
            "(store cap 100000)") in err
    # An evicted row only removes a promotion; it demotes nothing.
    assert "demote" not in _kinds(fakes.store.calls)
    fakes.store.evicted = 1
    _gate(fakes, tmp_path, verbose=True,
          nodes=[("tests/test_a.py::t0", "passed")])
    assert "1 observer row evicted" in capsys.readouterr().err


def test_eviction_line_needs_verbose(fakes, tmp_path, capsys):
    fakes.store.evicted = 7
    _gate(fakes, tmp_path, nodes=[("tests/test_a.py::t0", "passed")])
    assert "evicted" not in capsys.readouterr().err


def test_gate_report_when_the_audit_could_not_run(fakes, tmp_path, capsys):
    fakes.source_mod._error = RuntimeError("walk failed")
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::t0", "passed")])
    report = _after(fakes, tmp_path, execution="full", argv_files=None,
                    verbose=True)
    # The gate still recorded (marker, records) but claims nothing learned.
    assert report == C.SelectionGateReport(
        previously_deferred=0, failed=0, observers_learned=0,
        observers_evicted=0, unlearned=0, demoted=0)
    assert "selection gate:" not in capsys.readouterr().err


def test_planning_never_runs_the_fingerprint_command(fakes, tmp_path,
                                                     monkeypatch):
    """R42: refine, previews and status never execute the declared command."""
    import subprocess

    marker = Path(tmp_path) / "ran"
    spawned = []

    def forbidden(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("planning must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    config = _config(ambient_fingerprint=("touch", str(marker)))
    fakes.planner_mod._decision = _decision()
    fakes.impact_mod._static = _static_ok()
    out = fakes.engine.preview(_domain(tmp_path), None, tmp_path, config,
                               ("pkg/core.py",))
    assert out.engine == "dynamic"
    fakes.engine.status_lines(_domain(tmp_path), [(None, config)])
    assert spawned == [] and not marker.exists()


# --- lastgreen helpers (R27) --------------------------------------------------

def test_lastgreen_head_commit_and_dirty_files(tmp_path):
    from ptest import lastgreen

    repo = _repo(tmp_path)
    head = lastgreen.head_commit(repo)
    assert head == support.git(repo, "rev-parse", "HEAD")
    assert lastgreen.dirty_files(repo, "") == ()
    (repo / "pkg" / "core.py").write_text("A = 2\n")
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    (repo / "tests" / "data" / "cfg.json").write_text('{"x": 1}\n')
    assert lastgreen.dirty_files(repo, "") == (
        "pkg/core.py", "pkg/new.py", "tests/data/cfg.json")
    # Only paths under the project, still git-top-relative.
    assert lastgreen.dirty_files(repo, "pkg") == (
        "pkg/core.py", "pkg/new.py")
    assert lastgreen.dirty_files(repo, "pk") == ()


def test_lastgreen_helpers_report_unavailable_git_as_none(tmp_path):
    from ptest import lastgreen

    plain = Path(tmp_path) / "plain"
    plain.mkdir()
    assert lastgreen.head_commit(plain) is None
    assert lastgreen.dirty_files(plain, "") is None


def test_record_pass_still_records_through_the_shared_dirty_helper(tmp_path):
    from ptest import lastgreen

    repo = _repo(tmp_path)
    (repo / "pkg" / "core.py").write_text("A = 7\n")
    domain_root = Path(tmp_path) / "domain"
    domain_root.mkdir()
    lastgreen.record_pass(domain_root, str(repo), "", repo)
    record = lastgreen.load(domain_root, lastgreen._norm_root(repo), "")
    assert record is not None
    assert record.commit == support.git(repo, "rev-parse", "HEAD")
    assert set(record.files) == {"pkg/core.py"}
