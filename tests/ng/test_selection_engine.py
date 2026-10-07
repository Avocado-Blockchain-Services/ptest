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

from ptest import contracts as C


ENGINE = "ptest.selection_engine"


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
              nodes=(), fixtures=(), ambient=None, demotions=None):
    vocab = C.DepVocabulary(paths=("tests/test_a.py",),
                            functions=((0, "test_body"),),
                            fixtures=(("base", "arg", "session"),))
    runs = {run_id: C.RunBaseline(
        run_id=run_id, recorded_at=1.0, compatibility=compatibility,
        digests={}, ambient=ambient or C.selection_empty_context())}
    node_map = {}
    for i, (nodeid, outcome) in enumerate(nodes):
        node_map[nodeid] = C.NodeRecord(
            nodeid=nodeid, test_file=C.selection_test_file(nodeid),
            outcome=outcome, run_id=run_id, deps=_context(),
            fixtures=C.selection_ids(()))
    return C.DependencySnapshot(vocabulary=vocab, runs=runs, nodes=node_map,
                                fixtures={}, demotions=demotions or {})


def _meta(*, nodes=3, runs=1, size=4096, newest=100.0, python=(3, 12),
          inactive=None):
    return C.SelectionStoreMeta(
        size_bytes=size, nodes=nodes, runs=runs,
        newest_recorded_at=newest, python=python, inactive_reason=inactive,
        audit_checked=0, audit_misses=0, demoted=0)


class FakeStore:
    """In-memory SelectionStore double with a call log."""

    def __init__(self, snapshot=None, meta=None):
        self._snapshot = snapshot
        self._meta = meta or _meta()
        self.calls = []
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

    def mark_outcomes(self, outcomes):
        self.calls.append(("mark_outcomes", dict(outcomes)))

    def invalidate(self, test_files):
        self.calls.append(("invalidate", None if test_files is None
                           else tuple(test_files)))

    def demote(self, nodeids):
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

    def iter_python_files(self, project_root):
        return iter(())

    def read_source(self, project_root, rel):
        return None


class FakePlanner:
    def __init__(self, decision=None, error=None, misses=()):
        self._decision = decision
        self._error = error
        self._misses = misses
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

    def read_run(self, report_path, *, run_id, expected_workers):
        return self._run

    def write_deselect_binding(self, report_path, run_id, nodeids):
        self.bindings.append(tuple(nodeids))
        return Path(str(report_path) + ".deselect")

    def cleanup(self, report_path):
        self.cleaned.append(report_path)


def _config(tmp_path=None, *, runner="pytest", dynamic=True, enabled=True,
            full_ratio=0.70):
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
                                    full_ratio=full_ratio, dynamic=dynamic),
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
              full_reason=None, coverage=0.5):
    return C.SelectionDecision(
        full_reason=full_reason, files=tuple(files),
        deselect=tuple(deselect),
        whole_files=tuple(files), selected=selected, reached=reached,
        recorded=recorded, total_files=total_files, units=tuple(units),
        fallbacks=tuple(fallbacks), coverage=coverage)


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
    assert out is static
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


def test_dynamic_full_on_recorded_ratio(fakes, tmp_path):
    fakes.planner_mod._decision = _decision(files=("tests/test_a.py",),
                                            selected=7, recorded=10,
                                            total_files=4)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.kind == "full"
    assert out.engine == "dynamic"
    assert out.reason == \
        "7 of 10 recorded tests reach full_ratio 0.7 (dynamic)"


def test_dynamic_full_on_file_ratio(fakes, tmp_path):
    files = tuple(f"tests/test_{i}.py" for i in range(3))
    fakes.planner_mod._decision = _decision(files=files, selected=1,
                                            recorded=10, total_files=4)
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.kind == "full"
    assert out.reason == \
        "3 of 4 test files reach full_ratio 0.7 (dynamic)"


def test_dynamic_full_over_argv_limit(fakes, tmp_path):
    files = tuple(f"tests/test_{i}.py" for i in range(201))
    fakes.planner_mod._decision = _decision(files=files, selected=1,
                                            recorded=1000, total_files=1000)
    out = fakes.engine.refine(None, None, tmp_path,
                              _config(full_ratio=1.0), (), _static_ok())
    assert out.kind == "full"
    assert out.reason == \
        "201 test files exceed the 200-file scoped limit (dynamic)"


def test_dynamic_full_on_planner_reason(fakes, tmp_path):
    fakes.planner_mod._decision = _decision(
        full_reason="ambient data file data/config.json changed")
    out = fakes.engine.refine(None, None, tmp_path, _config(), (),
                              _static_ok())
    assert out.kind == "full"
    assert out.reason == \
        "ambient data file data/config.json changed (dynamic)"


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


def test_after_run_valid_scoped_updates_and_cleans_up(fakes, tmp_path):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed")])
    _after(fakes, tmp_path)
    assert ("update", "cd" * 16) in fakes.store.calls
    assert fakes.ingest_mod.cleaned == [Path(tmp_path) / "native-a001.json"]
    assert "mark_outcomes" not in _kinds(fakes.store.calls)
    assert "invalidate" not in _kinds(fakes.store.calls)


def test_after_run_valid_scoped_verbose_reports_recorded(fakes, tmp_path,
                                                         capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "passed"),
               ("tests/test_a.py::test_y", "passed")])
    _after(fakes, tmp_path, verbose=True)
    err = capsys.readouterr().err
    assert "ptest: -v selection: recorded 2 tests from 1 processes" in err


def test_after_run_cancelled_writes_nothing(fakes, tmp_path, capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed")])
    _after(fakes, tmp_path, cancelled=True, verbose=True)
    assert fakes.store.calls == []
    assert fakes.ingest_mod.cleaned == [Path(tmp_path) / "native-a001.json"]
    assert "not recorded" in capsys.readouterr().err


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


def test_after_run_recording_disabled_cleans_up_only(fakes, tmp_path,
                                                     capsys):
    fakes.ingest_mod._run = _run_deps(recording=False,
                                     inactive="Python 3.11 has no sys.monitoring",
                                     nodes=[])
    _after(fakes, tmp_path, verbose=True)
    assert ("update", "cd" * 16) not in fakes.store.calls
    assert fakes.ingest_mod.cleaned != []
    assert "not recorded" in capsys.readouterr().err


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


def test_self_audit_verbose_summary_without_misses(fakes, tmp_path, capsys):
    fakes.ingest_mod._run = _run_deps(
        nodes=[("tests/test_a.py::test_x", "failed")])
    fakes.planner_mod._decision = _decision()
    fakes.planner_mod._misses = ()
    fakes.source_mod._index = FakeIndex(files={})
    _after(fakes, tmp_path, execution="full", argv_files=None, verbose=True)
    err = capsys.readouterr().err
    assert "ptest: selection audit:" not in err
    assert "ptest: -v selection audit: 1 failing tests checked · 0 misses" \
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
