"""Per-project SQLite dependency store (T3).

Covers the section 2.5 store contract: update/snapshot round trip, parse
cache, outcomes/invalidate/demote/audit/meta, concurrent upserts, private
file refusals, hot-journal recovery, corruption rebuild, oldest-first
eviction, N7 isolation and the never-store-contents rule.

Integration boundary: ``test_store_holds_20k_nodes_under_cap`` builds a
synthetic 20k-node store (A5). It asserts on-disk size only and never calls
``snapshot`` over the full 20k rows, so it stays within the fast unit
boundary.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
import stat
import threading
import time
from array import array
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import selection_store as S

PROJ = "ab" * 16
OTHER_PROJ = "cd" * 16
RUN_A = "11" * 16
RUN_B = "22" * 16
COMPAT = "compat-v1"


def _ctx(funcs=(), modules=(), data=(), opaque=False):
    return S.ContextDeps(
        functions=array("I", sorted(set(funcs))),
        modules=array("I", sorted(set(modules))),
        data=array("I", sorted(set(data))),
        opaque=opaque,
    )


def _vocab(paths, funcs, fixtures=()):
    return S.DepVocabulary(
        paths=tuple(paths),
        functions=tuple(funcs),
        fixtures=tuple(fixtures),
    )


def _node(nodeid, outcome="passed", deps=None, fixtures=()):
    return S.RecordedNode(
        nodeid=nodeid,
        outcome=outcome,
        deps=deps if deps is not None else _ctx(),
        fixtures=array("I", sorted(set(fixtures))),
    )


def _run(nodes, *, paths=("tests/test_a.py", "pkg/a.py"),
         funcs=((1, "f"),), fixtures=None, ambient=None,
         complete=True, recording=True):
    nodes = dict(nodes)
    return S.RunDependencies(
        vocabulary=_vocab(paths, funcs, fixtures or ()),
        nodes=nodes,
        fixtures=dict(fixtures or {}),
        ambient=ambient if ambient is not None else _ctx(),
        complete=complete,
        recording=recording,
        python=(3, 12),
        inactive_reason=None,
        notes=(),
    )


def _digests(digest="aa" * 32, paths=("tests/test_a.py", "pkg/a.py")):
    return {path: digest for path in paths}


def _open(domain, project_id=PROJ):
    return S.open_store(domain, project_id, create=True)


def _db_path(domain, project_id=PROJ):
    return domain.root / "projects" / project_id / "selection.db"


# --- basics ---------------------------------------------------------------

def test_update_snapshot_round_trip(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        run = _run({
            "tests/test_a.py::test_one": _node(
                "tests/test_a.py::test_one", "passed", _ctx(funcs=(0,))),
            "tests/test_a.py::test_two": _node(
                "tests/test_a.py::test_two", "failed", _ctx(modules=(1,))),
        })
        store.update(run, run_id=RUN_A, recorded_at=100.0,
                     compatibility=COMPAT, digests=_digests(), full=True)
        snap = store.snapshot()
        assert set(snap.nodes) == {
            "tests/test_a.py::test_one", "tests/test_a.py::test_two"}
        node = snap.nodes["tests/test_a.py::test_one"]
        assert node.test_file == "tests/test_a.py"
        assert node.outcome == "passed"
        assert node.run_id == RUN_A
        assert isinstance(node.deps.functions, array)
        assert list(node.deps.functions) == [0]
        assert snap.runs[RUN_A].compatibility == COMPAT
        assert snap.runs[RUN_A].recorded_at == 100.0
        assert set(snap.runs[RUN_A].digests.values()) == {"aa" * 32}
        assert snap.vocabulary.paths == ("pkg/a.py", "tests/test_a.py")
        assert snap.vocabulary.functions == ((0, "f"),)
        assert snap.demotions == {}
    finally:
        store.close()


def test_update_replaces_only_ran_nodes(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({
            "tests/test_a.py::test_one": _node("tests/test_a.py::test_one"),
            "tests/test_a.py::test_two": _node("tests/test_a.py::test_two"),
        }), run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        store.update(_run({
            "tests/test_a.py::test_three": _node("tests/test_a.py::test_three"),
        }), run_id=RUN_B, recorded_at=2.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        # Re-record run A with only test_one: test_two (same run) is
        # replaced away, run B's node is kept.
        store.update(_run({
            "tests/test_a.py::test_one": _node("tests/test_a.py::test_one"),
        }), run_id=RUN_A, recorded_at=3.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        snap = store.snapshot()
        assert set(snap.nodes) == {
            "tests/test_a.py::test_one", "tests/test_a.py::test_three"}
    finally:
        store.close()


def test_fixture_records_replaced_per_run(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        vocab_fixtures = (("tests/conftest.py::fix", "fix", "session"),)
        run = _run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one", fixtures=(0,))},
            fixtures={0: _ctx(funcs=(0,))})
        run = S.RunDependencies(
            vocabulary=_vocab(("tests/test_a.py", "pkg/a.py"), ((1, "f"),),
                               vocab_fixtures),
            nodes=run.nodes, fixtures={0: _ctx(funcs=(0,))},
            ambient=_ctx(), complete=True, recording=True,
            python=(3, 12), inactive_reason=None, notes=())
        store.update(run, run_id=RUN_A, recorded_at=1.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        assert len(store.snapshot().fixtures) == 1
        # Re-record without fixtures: the old fixture record is gone.
        run2 = _run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")})
        store.update(run2, run_id=RUN_A, recorded_at=2.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        snap = store.snapshot()
        assert snap.fixtures == {}
        assert set(snap.nodes) == {"tests/test_a.py::test_one"}
    finally:
        store.close()


def test_runs_with_no_rows_left_are_pruned(domain_factory):
    """Oracle change (R21): a re-recorded node keeps one row per run up to
    K, so a run is unreferenced only once K newer runs took its last row."""
    domain = domain_factory()
    store = _open(domain)
    try:
        runs = [RUN_A, RUN_B, "33" * 16, "44" * 16]
        for index, run_id in enumerate(runs):
            store.update(_run({"tests/test_a.py::test_one": _node(
                "tests/test_a.py::test_one",
                "failed" if index == 3 else "passed")}),
                run_id=run_id, recorded_at=float(index + 1),
                compatibility=COMPAT, digests=_digests(), full=False)
            if index == 1:
                assert set(store.snapshot().runs) == {RUN_A, RUN_B}
        snap = store.snapshot()
        assert set(snap.runs) == set(runs[1:])
        assert snap.nodes["tests/test_a.py::test_one"].outcome == "failed"
    finally:
        store.close()


def test_full_prunes_nodes_absent_from_two_inventories(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        nodes_v1 = {f"tests/test_a.py::test_{i}": _node(
            f"tests/test_a.py::test_{i}") for i in ("one", "two", "old")}
        store.update(_run(nodes_v1), run_id=RUN_A, recorded_at=1.0,
                     compatibility=COMPAT, digests=_digests(), full=True)
        # Second full run drops "old" but the previous inventory still
        # names it, so it survives one generation.
        nodes_v2 = {key: value for key, value in nodes_v1.items()
                    if not key.endswith("old")}
        store.update(_run(nodes_v2), run_id=RUN_B, recorded_at=2.0,
                     compatibility=COMPAT, digests=_digests(), full=True)
        assert "tests/test_a.py::test_old" in store.snapshot().nodes
        # Third full run: absent from the last two inventories -> pruned.
        store.update(_run(nodes_v2), run_id="33" * 16, recorded_at=3.0,
                     compatibility=COMPAT, digests=_digests(), full=True)
        snap = store.snapshot()
        assert "tests/test_a.py::test_old" not in snap.nodes
        assert set(snap.nodes) == set(nodes_v2)
    finally:
        store.close()


def test_missing_digest_makes_referencing_contexts_opaque(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        run = _run({
            "tests/test_a.py::test_one": _node(
                "tests/test_a.py::test_one", deps=_ctx(funcs=(0,))),
        })
        digests = {"tests/test_a.py": "bb" * 32, "pkg/a.py": None}
        store.update(run, run_id=RUN_A, recorded_at=1.0,
                     compatibility=COMPAT, digests=digests, full=False)
        snap = store.snapshot()
        assert snap.nodes["tests/test_a.py::test_one"].deps.opaque is True
    finally:
        store.close()


# --- outcomes / invalidate / demote / audit / meta -------------------------

def test_mark_outcomes_existing_only(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({
            "tests/test_a.py::test_one": _node("tests/test_a.py::test_one"),
        }), run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        store.mark_outcomes({"tests/test_a.py::test_one": "failed",
                             "tests/test_a.py::test_ghost": "failed"})
        snap = store.snapshot()
        assert snap.nodes["tests/test_a.py::test_one"].outcome == "failed"
        assert set(snap.nodes) == {"tests/test_a.py::test_one"}
    finally:
        store.close()


def test_invalidate_subset_and_all(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({
            "tests/test_a.py::test_one": _node("tests/test_a.py::test_one"),
            "tests/test_b.py::test_two": _node(
                "tests/test_b.py::test_two", deps=_ctx(),
                fixtures=array("I")),
        }, paths=("tests/test_a.py", "tests/test_b.py", "pkg/a.py")),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(paths=("tests/test_a.py", "tests/test_b.py",
                                     "pkg/a.py")),
            full=False)
        store.invalidate(("tests/test_a.py",))
        snap = store.snapshot()
        assert snap.nodes["tests/test_a.py::test_one"].outcome == "unknown"
        assert snap.nodes["tests/test_b.py::test_two"].outcome == "passed"
        store.invalidate(None)
        snap = store.snapshot()
        assert {node.outcome for node in snap.nodes.values()} == {"unknown"}
    finally:
        store.close()


def test_demote_record_audit_note_inactive_meta(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({
            "tests/test_a.py::test_one": _node("tests/test_a.py::test_one"),
        }), run_id=RUN_A, recorded_at=42.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        store.demote({"tests/test_a.py::test_one": "ff" * 32})
        store.record_audit(checked=10, misses=2)
        store.record_audit(checked=5, misses=1)
        store.note_inactive("recorder could not start", (3, 12))
        meta = store.meta()
        assert meta.nodes == 1
        assert meta.runs == 1
        assert meta.newest_recorded_at == 42.0
        assert meta.audit_checked == 15
        assert meta.audit_misses == 3
        assert meta.demoted == 1
        assert meta.python == (3, 12)
        assert meta.inactive_reason == "recorder could not start"
        assert meta.size_bytes == _db_path(domain).stat().st_size
        assert store.snapshot().demotions == {
            "tests/test_a.py::test_one": "ff" * 32}
        store.note_inactive(None, None)
        assert store.meta().inactive_reason is None
        assert store.meta().python is None
    finally:
        store.close()


# --- parse cache -----------------------------------------------------------

def test_parse_cache_round_trip_and_version_gate(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        assert store.get_many(["nope"]) == {}
        store.put_many({"d1": b"blob-one", "d2": b"blob-two"})
        assert store.get_many(["d1", "d2", "missing"]) == {
            "d1": b"blob-one", "d2": b"blob-two"}
        # A row written by another index version is invisible.
        conn = store._conn
        conn.execute("UPDATE cache SET version = 0 WHERE digest = 'd1'")
        conn.commit()
        assert store.get_many(["d1", "d2"]) == {"d2": b"blob-two"}
    finally:
        store.close()


def test_cache_lru_prune(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.put_many({"old-unref": b"x", "old-ref": b"y"})
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests={"tests/test_a.py": "bb" * 32, "pkg/a.py": "old-ref"},
            full=False)
        # Age every cache row; only the baseline-referenced digest survives.
        store._conn.execute("UPDATE cache SET used_at = 1.0")
        store.update(_run({"tests/test_a.py::test_two": _node(
            "tests/test_a.py::test_two")}),
            run_id=RUN_B, recorded_at=2.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        assert store.get_many(["old-unref"]) == {}
        assert store.get_many(["old-ref"]) == {"old-ref": b"y"}
    finally:
        store.close()


# --- concurrency -----------------------------------------------------------

def test_concurrent_updates_from_two_connections(domain_factory):
    domain = domain_factory()
    errors: list[BaseException] = []

    def worker(index: int):
        try:
            store = S.open_store(domain, PROJ, create=False)
            try:
                for round_no in range(25):
                    run_id = f"{index:02x}" + f"{round_no:02x}" * 15
                    store.update(_run({
                        f"tests/test_a.py::test_{index}_{round_no}": _node(
                            f"tests/test_a.py::test_{index}_{round_no}",
                            "failed" if round_no % 2 else "passed"),
                        "tests/test_a.py::test_shared": _node(
                            "tests/test_a.py::test_shared",
                            "failed" if index else "passed"),
                    }), run_id=run_id, recorded_at=float(round_no),
                        compatibility=COMPAT, digests=_digests(), full=False)
            finally:
                store.close()
        except BaseException as exc:  # never leak; the test asserts below
            errors.append(exc)

    seed = _open(domain)
    seed.close()
    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    store = S.open_store(domain, PROJ, create=False)
    try:
        snap = store.snapshot()
        # Every row parses: nothing torn; the shared node keeps rows from
        # both writers (up to K), the newest one is the node's record.
        assert snap.nodes["tests/test_a.py::test_shared"].outcome in {
            "passed", "failed"}
        assert len(snap.nodes) == 51
        assert len(snap.runs) == 50
    finally:
        store.close()


NODE_ONE = "tests/test_a.py::test_one"


def _record(store, run_id, recorded_at, nodes=(NODE_ONE,), outcome="passed",
            **extra):
    return store.update(
        _run({nodeid: _node(nodeid, outcome) for nodeid in nodes}),
        run_id=run_id, recorded_at=recorded_at, compatibility=COMPAT,
        digests=_digests(), full=extra.pop("full", False), **extra)


def test_same_node_keeps_one_row_per_run_up_to_k(domain_factory):
    """R21, R26: two runs recording one node leave two rows (the old
    last-writer-wins replaced the first); a fourth run trims the oldest."""
    domain = domain_factory()
    store = _open(domain)
    try:
        _record(store, RUN_A, 1.0, outcome="passed")
        _record(store, RUN_B, 2.0, outcome="failed")
        snap = store.snapshot()
        assert [r.run_id for r in snap.records[NODE_ONE]] == [RUN_B, RUN_A]
        assert snap.nodes[NODE_ONE] is snap.records[NODE_ONE][0]
        assert snap.nodes[NODE_ONE].outcome == "failed"
        assert snap.records[NODE_ONE][1].outcome == "passed"
        _record(store, "33" * 16, 3.0)
        _record(store, "44" * 16, 4.0)
        snap = store.snapshot()
        assert [r.run_id for r in snap.records[NODE_ONE]] == [
            "44" * 16, "33" * 16, RUN_B]
        assert RUN_A not in snap.runs
        assert S.SELECTION_RECORDS_PER_NODE == 3
    finally:
        store.close()


def test_backdated_written_run_is_kept_and_the_oldest_other_row_goes(
        domain_factory):
    """R21: "never the row being written", even when its run is the oldest."""
    domain = domain_factory()
    store = _open(domain)
    try:
        for run_id, at in ((RUN_B, 2.0), ("33" * 16, 3.0), ("44" * 16, 4.0)):
            _record(store, run_id, at)
        _record(store, RUN_A, 0.5)
        ids = [r.run_id for r in store.snapshot().records[NODE_ONE]]
        assert ids == ["44" * 16, "33" * 16, RUN_A]
    finally:
        store.close()


def test_store_concurrent_updates_keep_at_most_k_rows_per_node(
        domain_factory):
    domain = domain_factory()
    errors: list[BaseException] = []
    seed = _open(domain)
    seed.close()

    def worker(index: int):
        try:
            store = S.open_store(domain, PROJ, create=False)
            try:
                for round_no in range(12):
                    _record(store, f"{index:02x}" + f"{round_no:02x}" * 15,
                            float(round_no))
            finally:
                store.close()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    store = S.open_store(domain, PROJ, create=False)
    try:
        rows = store._conn.execute(
            "SELECT run_id FROM nodes WHERE nodeid = ?",
            (NODE_ONE,)).fetchall()
        assert len(rows) == S.SELECTION_RECORDS_PER_NODE
        snap = store.snapshot()
        assert {r.run_id for r in snap.records[NODE_ONE]} <= set(snap.runs)
        assert len(snap.records[NODE_ONE]) == 3
    finally:
        store.close()


def test_store_nodes_delete_keys_on_run_id(domain_factory):
    """R21: re-recording run A without a node deletes only A's row of it."""
    domain = domain_factory()
    store = _open(domain)
    try:
        two = "tests/test_a.py::test_two"
        _record(store, RUN_A, 1.0, nodes=(NODE_ONE, two))
        _record(store, RUN_B, 2.0, nodes=(NODE_ONE, two))
        _record(store, RUN_A, 3.0, nodes=(NODE_ONE,))
        snap = store.snapshot()
        assert [r.run_id for r in snap.records[two]] == [RUN_B]
        assert {r.run_id for r in snap.records[NODE_ONE]} == {RUN_A, RUN_B}
    finally:
        store.close()


def test_outcome_updates_cover_all_k_rows(domain_factory):
    """R21: mark_outcomes and invalidate touch every record, not the newest."""
    domain = domain_factory()
    store = _open(domain)
    try:
        for index, run_id in enumerate((RUN_A, RUN_B, "33" * 16)):
            _record(store, run_id, float(index + 1))
        store.mark_outcomes({NODE_ONE: "failed"})
        records = store.snapshot().records[NODE_ONE]
        assert len(records) == 3
        assert {r.outcome for r in records} == {"failed"}
        store.invalidate(("tests/test_a.py",))
        assert {r.outcome for r in store.snapshot().records[NODE_ONE]} == {
            "unknown"}
        store.mark_outcomes({NODE_ONE: "passed"})
        store.invalidate(None)
        assert {r.outcome for r in store.snapshot().records[NODE_ONE]} == {
            "unknown"}
    finally:
        store.close()


# --- private files ---------------------------------------------------------

def _uid_plus_one(monkeypatch):
    real_uid = os.getuid()
    other = real_uid + 1 if real_uid < 60000 else real_uid - 1
    monkeypatch.setattr(os, "getuid", lambda: other)


def test_private_file_refusals(domain_factory, tmp_path, monkeypatch):
    domain = domain_factory()
    _open(domain).close()
    db = _db_path(domain)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    project_dir = db.parent

    with pytest.raises(C.Problem) as exc:
        os.chmod(db, 0o644)
        S.open_store(domain, PROJ, create=False)
    assert exc.value.code == "unsafe-path"
    os.chmod(db, 0o600)

    os.link(db, tmp_path / "hard.db")
    try:
        with pytest.raises(C.Problem) as exc:
            S.open_store(domain, PROJ, create=False)
        assert exc.value.code == "unsafe-path"
    finally:
        os.unlink(tmp_path / "hard.db")

    _uid_plus_one(monkeypatch)
    with pytest.raises(C.Problem) as exc:
        S.open_store(domain, PROJ, create=False)
    assert exc.value.code == "unsafe-path"


def test_symlinked_db_and_project_dir_refused(domain_factory, tmp_path):
    domain = domain_factory()
    _open(domain).close()
    db = _db_path(domain)
    real = tmp_path / "real.db"
    os.rename(db, real)
    try:
        os.symlink(real, db)
        with pytest.raises(C.Problem) as exc:
            S.open_store(domain, PROJ, create=False)
        assert exc.value.code == "unsafe-path"
    finally:
        os.unlink(db)
        os.rename(real, db)

    project_dir = db.parent
    moved = tmp_path / "projects-evil"
    os.rename(project_dir, moved)
    try:
        os.symlink(moved, project_dir)
        with pytest.raises(C.Problem) as exc:
            S.open_store(domain, PROJ, create=False)
        assert exc.value.code == "unsafe-path"
    finally:
        os.unlink(project_dir)
        os.rename(moved, project_dir)


def test_create_false_never_creates(domain_factory):
    domain = domain_factory()
    with pytest.raises(C.Problem) as exc:
        S.open_store(domain, PROJ, create=False)
    assert exc.value.code == "state-unavailable"
    assert not (domain.root / "projects").exists()


def test_bad_project_id_is_a_problem(domain_factory):
    domain = domain_factory()
    with pytest.raises(C.Problem):
        S.open_store(domain, "../evil", create=True)


def test_hot_journal_recovers(domain_factory):
    from support import leave_hot_journal
    domain = domain_factory()
    store = _open(domain)
    store.update(_run({"tests/test_a.py::test_one": _node(
        "tests/test_a.py::test_one")}),
        run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
        digests=_digests(), full=False)
    store.close()
    leave_hot_journal(_db_path(domain))
    recovered = S.open_store(domain, PROJ, create=False)
    try:
        assert set(recovered.snapshot().nodes) == {
            "tests/test_a.py::test_one"}
    finally:
        recovered.close()


def test_corrupt_store_rebuilds_after_remove(domain_factory):
    domain = domain_factory()
    _open(domain).close()
    db = _db_path(domain)
    with open(db, "r+b") as handle:
        handle.write(b"not a database at all" * 64)
        handle.truncate()
    with pytest.raises(C.Problem) as exc:
        S.open_store(domain, PROJ, create=False)
    assert exc.value.code == "coordinator-corrupt"
    assert S.remove_store(domain, PROJ) is True
    assert S.remove_store(domain, PROJ) is False
    fresh = S.open_store(domain, PROJ, create=True)
    try:
        assert fresh.snapshot().nodes == {}
        assert fresh.meta().nodes == 0
    finally:
        fresh.close()


def test_ambient_functions_round_trip(domain_factory):
    """Collection-time code that calls project functions lands in the
    ambient context. Before the fix every such run raised KeyError inside
    update and was silently never recorded."""
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(
            _run({"tests/test_a.py::test_one": _node(
                "tests/test_a.py::test_one", deps=_ctx(funcs=[0]))},
                funcs=((1, "f"), (1, "g")), ambient=_ctx(funcs=[1])),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        snap = store.snapshot()
        names = {snap.vocabulary.functions[f][1]
                 for f in snap.runs[RUN_A].ambient.functions}
        assert names == {"g"}
        assert set(snap.nodes) == {"tests/test_a.py::test_one"}
    finally:
        store.close()


def test_successive_scoped_updates_keep_every_id_resolvable(domain_factory):
    """Two scoped runs with different vocabularies: every id in the merged
    snapshot must resolve to the names each run recorded."""
    from ptest import selection_planner as PL
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(
            _run({"tests/test_a.py::test_one": _node(
                "tests/test_a.py::test_one", deps=_ctx(funcs=[0], data=[2]))},
                paths=("tests/test_a.py", "pkg/a.py", "data/a.json"),
                funcs=((1, "f"),), ambient=_ctx(funcs=[0])),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(paths=("tests/test_a.py", "pkg/a.py",
                                    "data/a.json")), full=False)
        store.update(
            _run({"tests/test_b.py::test_two": _node(
                "tests/test_b.py::test_two", deps=_ctx(funcs=[0, 1],
                                                      modules=[3]))},
                paths=("pkg/b.py", "tests/test_b.py", "pkg/a.py",
                       "pkg/c.py"),
                funcs=((0, "g"), (2, "h")), ambient=_ctx(funcs=[1])),
            run_id=RUN_B, recorded_at=2.0, compatibility=COMPAT,
            digests=_digests(paths=("pkg/b.py", "tests/test_b.py",
                                    "pkg/a.py", "pkg/c.py")), full=False)
        snap = store.snapshot()
        resolved = {}
        for nodeid, node in snap.nodes.items():
            paths = PL._context_paths(snap.vocabulary, snap.fixtures,
                                      node.deps, list(node.fixtures))
            assert paths is not None, nodeid
            resolved[nodeid] = paths
        assert resolved["tests/test_a.py::test_one"][0] == {("pkg/a.py", "f")}
        assert resolved["tests/test_a.py::test_one"][2] == {"data/a.json"}
        assert resolved["tests/test_b.py::test_two"][0] == {
            ("pkg/b.py", "g"), ("pkg/a.py", "h")}
        assert resolved["tests/test_b.py::test_two"][1] == {"pkg/c.py"}
        for run_id, expected in ((RUN_A, {("pkg/a.py", "f")}),
                                 (RUN_B, {("pkg/a.py", "h")})):
            ambient = {PL._resolve_function(snap.vocabulary, fid)
                       for fid in snap.runs[run_id].ambient.functions}
            assert ambient == expected
    finally:
        store.close()


def test_engine_rebuilds_damaged_store_on_write_open(domain_factory):
    """A damaged store would pin every later run to the static rule."""
    from ptest import selection_engine as E
    domain = domain_factory()
    _open(domain).close()
    db = _db_path(domain)
    with open(db, "r+b") as handle:
        handle.write(b"not a database at all" * 64)
        handle.truncate()
    assert E._open_store(domain, PROJ, create=False) is None
    store = E._open_store(domain, PROJ, create=True)
    assert store is not None
    try:
        assert store.snapshot().nodes == {}
    finally:
        store.close()


def test_engine_keeps_store_with_unknown_schema(domain_factory):
    """Another ptest version's store is not damaged; never remove it."""
    import sqlite3
    from ptest import selection_engine as E
    domain = domain_factory()
    _open(domain).close()
    db = _db_path(domain)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE kv SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    before = os.stat(db).st_ino
    assert E._open_store(domain, PROJ, create=True) is None
    assert os.stat(db).st_ino == before


def test_remove_store_never_follows_links(domain_factory, tmp_path):
    domain = domain_factory()
    assert S.remove_store(domain, PROJ) is False
    _open(domain).close()
    db = _db_path(domain)
    target = tmp_path / "target.txt"
    target.write_bytes(b"precious")
    os.rename(db, tmp_path / "moved.db")
    try:
        os.symlink(target, db)
        assert S.remove_store(domain, PROJ) is False
        assert target.read_bytes() == b"precious"
    finally:
        os.unlink(db)
        os.rename(tmp_path / "moved.db", db)


# --- size / eviction / N7 / never-store ------------------------------------

def _big_run(count=20000, funcs_per_node=150, pool=1500):
    paths = ("pkg/big.py", "tests/test_big.py")
    vocab_funcs = tuple((0, f"f{n}") for n in range(pool))
    nodes = {}
    for index in range(count):
        base = (index * 7) % pool
        refs = [(base + slot) % pool for slot in range(funcs_per_node)]
        nodes[f"tests/test_big.py::test_{index}"] = _node(
            f"tests/test_big.py::test_{index}", "passed",
            _ctx(funcs=refs))
    run = S.RunDependencies(
        vocabulary=_vocab(paths, vocab_funcs),
        nodes=nodes, fixtures={}, ambient=_ctx(), complete=True,
        recording=True, python=(3, 12), inactive_reason=None, notes=())
    return run


def test_store_holds_20k_nodes_under_cap(domain_factory):
    """R25: 20k nodes x K=3 records stay under the 128 MiB hard cap."""
    domain = domain_factory()
    store = _open(domain)
    try:
        for index, run_id in enumerate((RUN_A, RUN_B, "33" * 16)):
            store.update(_big_run(), run_id=run_id,
                         recorded_at=float(index + 1), compatibility=COMPAT,
                         digests={"pkg/big.py": "aa" * 32,
                                  "tests/test_big.py": "bb" * 32},
                         full=True)
        size = _db_path(domain).stat().st_size
        assert size <= S.SELECTION_STORE_MAX_BYTES
        rows = store._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert rows == 3 * 20000
        assert S.SELECTION_STORE_MAX_BYTES == 128 * 1024 * 1024
        assert S.SELECTION_STORE_TARGET_BYTES == 96 * 1024 * 1024
    finally:
        store.close()


def test_eviction_is_oldest_first_and_never_fails(domain_factory,
                                                  monkeypatch):
    domain = domain_factory()
    store = _open(domain)
    try:
        vocab_funcs = tuple((1, f"f{n}") for n in range(20))

        def big(run_id, index):
            nodes = {f"tests/test_a.py::t{index}_{n}": _node(
                f"tests/test_a.py::t{index}_{n}",
                deps=_ctx(funcs=range(20))) for n in range(150)}
            return S.RunDependencies(
                vocabulary=_vocab(("tests/test_a.py", "pkg/a.py"),
                                  vocab_funcs),
                nodes=nodes, fixtures={}, ambient=_ctx(), complete=True,
                recording=True, python=(3, 12), inactive_reason=None,
                notes=())

        peak = 0
        for index, run_id in enumerate((RUN_A, RUN_B, "33" * 16)):
            store.update(big(run_id, index), run_id=run_id,
                         recorded_at=float(index), compatibility=COMPAT,
                         digests=_digests(), full=False)
            peak = max(peak, _db_path(domain).stat().st_size)
        assert len(store.snapshot().runs) == 3
        # Drop the target: the next run evicts all three old runs and the
        # file compacts below the accumulated peak.
        monkeypatch.setattr(S, "SELECTION_STORE_TARGET_BYTES", 30_000)
        store.update(big("44" * 16, 3), run_id="44" * 16,
                     recorded_at=3.0, compatibility=COMPAT,
                     digests=_digests(), full=False)
        snap = store.snapshot()
        assert set(snap.runs) == {"44" * 16}
        assert len(snap.nodes) == 150
        assert _db_path(domain).stat().st_size < peak
    finally:
        store.close()


def test_capacity_exceeded_when_newest_run_cannot_fit(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        # Pin this connection at its current size: any growth is SQLITE_FULL.
        pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
        store._conn.execute(f"PRAGMA max_page_count={pages}")
        nodes = {f"tests/test_a.py::test_{index}": _node(
            f"tests/test_a.py::test_{index}",
            deps=_ctx(funcs=(0,))) for index in range(300)}
        with pytest.raises(C.Problem) as exc:
            store.update(_run(nodes), run_id=RUN_A, recorded_at=1.0,
                         compatibility=COMPAT, digests=_digests(),
                         full=False)
        assert exc.value.code == "capacity-exceeded"
    finally:
        store.close()


def test_project_isolation_n7(domain_factory):
    domain = domain_factory()
    first = _open(domain, PROJ)
    try:
        first.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
    finally:
        first.close()
    second = _open(domain, OTHER_PROJ)
    try:
        assert second.snapshot().nodes == {}
        assert second.meta().nodes == 0
    finally:
        second.close()


def test_never_stores_contents_or_unkeyed_digests(domain_factory):
    domain = domain_factory()
    sentinel = "sentinel-contents-9f3c7a-marshmallow"
    raw_source = f"# {sentinel}\ndef f():\n    return 1\n".encode()
    raw_data = f'{{"key": "{sentinel}"}}'.encode()
    keyed_source = hashlib.sha256(b"key" + raw_source).hexdigest()
    keyed_data = hashlib.sha256(b"key" + raw_data).hexdigest()
    store = _open(domain)
    try:
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one", deps=_ctx(funcs=(0,), data=(1,)))},
            paths=("pkg/a.py", "data/conf.json"),
            funcs=((0, "f"),)),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests={"pkg/a.py": keyed_source,
                     "data/conf.json": keyed_data},
            full=False)
    finally:
        store.close()
    blob = _db_path(domain).read_bytes()
    assert sentinel.encode() not in blob
    assert raw_source not in blob
    assert raw_data not in blob
    assert hashlib.sha256(raw_source).hexdigest().encode() not in blob
    assert hashlib.sha256(raw_data).hexdigest().encode() not in blob


def test_context_manager_and_idempotent_close(domain_factory):
    domain = domain_factory()
    with S.open_store(domain, PROJ, create=True) as store:
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        store.close()
        store.close()
    with S.open_store(domain, PROJ, create=False) as store:
        assert set(store.snapshot().nodes) == {"tests/test_a.py::test_one"}
    with pytest.raises(C.Problem):
        store.snapshot()


def test_eviction_removes_only_oldest_run(domain_factory, monkeypatch):
    """A target that fits all-but-one run evicts exactly the oldest."""
    domain = domain_factory()
    store = _open(domain)
    try:
        vocab_funcs = tuple((1, f"f{n}") for n in range(20))

        def big(index):
            nodes = {f"tests/test_a.py::t{index}_{n}": _node(
                f"tests/test_a.py::t{index}_{n}",
                deps=_ctx(funcs=range(20))) for n in range(150)}
            return S.RunDependencies(
                vocabulary=_vocab(("tests/test_a.py", "pkg/a.py"),
                                  vocab_funcs),
                nodes=nodes, fixtures={}, ambient=_ctx(), complete=True,
                recording=True, python=(3, 12), inactive_reason=None,
                notes=())

        store.update(big(0), run_id=RUN_A, recorded_at=0.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        store.update(big(1), run_id=RUN_B, recorded_at=1.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        size_two = store._db_size(store._conn)
        store.update(big(2), run_id="33" * 16, recorded_at=2.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        size_three = store._db_size(store._conn)
        assert len(store.snapshot().runs) == 3
        # One equal-shaped run's measured growth. The target leaves room
        # for that growth minus three pages: a drained eviction frees a
        # whole run (many pages) and stops after the oldest, while an
        # undrained one frees a single page per eviction and must keep
        # going until only the newest run is left.
        growth = size_three - size_two
        page = store._conn.execute("PRAGMA page_size").fetchone()[0]
        assert growth > 4 * page
        monkeypatch.setattr(S, "SELECTION_STORE_TARGET_BYTES",
                            size_three + growth - 3 * page)
        newest = "44" * 16
        store.update(big(3), run_id=newest, recorded_at=3.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        snap = store.snapshot()
        assert set(snap.runs) == {RUN_B, "33" * 16, newest}
        assert len(snap.nodes) == 450
    finally:
        store.close()


def _sized_run(prefix, count):
    vocab_funcs = tuple((1, f"f{n}") for n in range(20))
    nodes = {f"tests/test_a.py::{prefix}_{n}": _node(
        f"tests/test_a.py::{prefix}_{n}",
        deps=_ctx(funcs=range(20))) for n in range(count)}
    return S.RunDependencies(
        vocabulary=_vocab(("tests/test_a.py", "pkg/a.py"), vocab_funcs),
        nodes=nodes, fixtures={}, ambient=_ctx(), complete=True,
        recording=True, python=(3, 12), inactive_reason=None, notes=())


def test_small_run_never_evicts_a_larger_full_run(domain_factory,
                                                  monkeypatch):
    """Bug (persea, 0.6.7): after a 14k-test full run the next one-test
    scoped run evicted it oldest-first, so selection fell back to the
    static rule and ran the whole suite again on every change. A run
    holding more records than the incoming one is never evicted for the
    size target."""
    domain = domain_factory()
    store = _open(domain)
    try:
        full = "aa" * 16
        store.update(_sized_run("full", 600), run_id=full, recorded_at=0.0,
                     compatibility=COMPAT, digests=_digests(), full=True)
        monkeypatch.setattr(S, "SELECTION_STORE_TARGET_BYTES", 30_000)
        for index, run_id in enumerate((RUN_A, RUN_B)):
            store.update(_sized_run(f"scoped{index}", 1), run_id=run_id,
                         recorded_at=1.0 + index, compatibility=COMPAT,
                         digests=_digests(), full=False)
        snap = store.snapshot()
        assert full in snap.runs
        assert sum(1 for node in snap.nodes.values()
                   if node.run_id == full) == 600
        # The older small run made room for the newer one.
        assert set(snap.runs) == {full, RUN_B}
    finally:
        store.close()


def test_size_target_drops_unreferenced_cache_before_any_run(
        domain_factory, monkeypatch):
    """Spec 2.5 pruning: file versions no record references go before any
    record does. Same persea bug: 21 MB of young unreferenced parse cache
    pushed the store over its target and records were evicted instead."""
    domain = domain_factory()
    store = _open(domain)
    try:
        referenced = "aa" * 32  # the digest _digests() records
        store.put_many({referenced: b"r" * 50_000})
        store.put_many({f"{n:064x}": os.urandom(50_000) for n in range(8)})
        store.update(_sized_run("full", 300), run_id=RUN_A,
                     recorded_at=0.0, compatibility=COMPAT,
                     digests=_digests(), full=True)
        size_with_cache = store._db_size(store._conn)
        monkeypatch.setattr(S, "SELECTION_STORE_TARGET_BYTES",
                            size_with_cache - 200_000)
        store.update(_sized_run("big", 400), run_id=RUN_B, recorded_at=1.0,
                     compatibility=COMPAT, digests=_digests(), full=False)
        snap = store.snapshot()
        assert set(snap.runs) == {RUN_A, RUN_B}
        assert set(store.get_many([referenced])) == {referenced}
        assert store.get_many([f"{n:064x}" for n in range(8)]) == {}
        assert _db_path(domain).stat().st_size <= S.SELECTION_STORE_MAX_BYTES
    finally:
        store.close()


def test_concurrent_create_from_two_connections(domain_factory):
    """Two racers creating the same fresh store never see corruption."""
    domain = domain_factory()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def opener(project_id: str):
        barrier.wait(timeout=30)
        try:
            store = S.open_store(domain, project_id, create=True)
        except BaseException as exc:  # never leak; asserted below
            errors.append(exc)
        else:
            try:
                store.update(_run({"tests/test_a.py::test_one": _node(
                    "tests/test_a.py::test_one")}),
                    run_id=RUN_A, recorded_at=1.0,
                    compatibility=COMPAT, digests=_digests(), full=False)
            except BaseException as exc:
                errors.append(exc)
            finally:
                store.close()

    for iteration in range(25):
        project_id = f"{iteration:032x}"
        errors.clear()
        threads = [threading.Thread(target=opener, args=(project_id,))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(isinstance(exc, C.Problem)
                       and exc.code == "coordinator-corrupt"
                       for exc in errors), errors
        assert errors == [], errors
        store = S.open_store(domain, project_id, create=False)
        try:
            assert RUN_A in store.snapshot().runs
        finally:
            store.close()


def test_concurrent_update_while_lock_held(domain_factory):
    """An update blocked past busy_timeout waits instead of failing raw."""
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        # Shrink this connection's busy wait so the test stays fast; the
        # bounded retry in _begin_immediate is independent of it.
        store._conn.execute("PRAGMA busy_timeout=50")
        holder = sqlite3.connect(str(_db_path(domain)), timeout=5.0)
        try:
            holder.execute("PRAGMA busy_timeout=5000")
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("CREATE TABLE IF NOT EXISTS _hold(x)")
            errors: list[BaseException] = []

            def writer():
                try:
                    second = S.open_store(domain, PROJ, create=False)
                    # Same shrink as the main handle: without the
                    # bounded retry, one 50 ms busy wait cannot outlast
                    # the 0.5 s hold below.
                    second._conn.execute("PRAGMA busy_timeout=50")
                except BaseException as exc:  # asserted below
                    errors.append(exc)
                    return
                try:
                    second.update(_run({"tests/test_a.py::test_two": _node(
                        "tests/test_a.py::test_two")}),
                        run_id=RUN_B, recorded_at=2.0,
                        compatibility=COMPAT, digests=_digests(),
                        full=False)
                except BaseException as exc:  # asserted below
                    errors.append(exc)
                finally:
                    second.close()

            thread = threading.Thread(target=writer)
            thread.start()
            time.sleep(0.5)  # hold the lock past the 50 ms busy_timeout
            holder.execute("COMMIT")
            thread.join(timeout=60)
            assert errors == [], errors
            assert not any(isinstance(exc, sqlite3.Error)
                           for exc in errors)
            assert RUN_B in store.snapshot().runs
        finally:
            holder.close()
    finally:
        store.close()


def test_update_error_mapping_is_typed():
    """Lock and I/O failures map to C.Problem, nothing else changes."""
    with pytest.raises(C.Problem) as busy:
        S._map_update_error(sqlite3.OperationalError("database is locked"))
    assert busy.value.code == "coordinator-unavailable"
    assert busy.value.retryable
    with pytest.raises(C.Problem) as io_error:
        S._map_update_error(sqlite3.OperationalError("disk I/O error"))
    assert io_error.value.code == "state-unavailable"
    with pytest.raises(sqlite3.OperationalError):
        S._map_update_error(sqlite3.OperationalError("no such table: nope"))


def test_snapshot_unions_fixture_rows_across_runs(domain_factory):
    """N1: a narrowed re-record must not drop another run's fixture deps.

    R1 records a module-scoped fixture executing both setup_sqlite and
    setup_pg; a later scoped run R2 re-records the same fixture with only
    setup_sqlite. The snapshot must carry the union, so nodes from R1
    still resolve the setup_pg dependency. Would fail before the fix:
    the last row scanned won and the union assertion below missed index 1.
    """
    domain = domain_factory()
    store = _open(domain)
    try:
        paths = ("tests/test_x.py", "pkg/db.py")
        funcs = ((1, "setup_sqlite"), (1, "setup_pg"))
        fx_vocab = (("tests/test_x.py", "backend", "module"),)
        digests = {path: "bb" * 32 for path in paths}
        full_run = _run(
            {
                "tests/test_x.py::test_x[sqlite]": _node(
                    "tests/test_x.py::test_x[sqlite]",
                    "passed", _ctx(), fixtures=(0,)),
                "tests/test_x.py::test_x[pg]": _node(
                    "tests/test_x.py::test_x[pg]",
                    "passed", _ctx(), fixtures=(0,)),
            },
            paths=paths, funcs=funcs,
            fixtures={0: _ctx(funcs=(0, 1))},
        )
        full_run = dataclasses.replace(
            full_run, vocabulary=_vocab(paths, funcs, fx_vocab))
        store.update(full_run, run_id=RUN_A, recorded_at=100.0,
                     compatibility=COMPAT, digests=digests, full=True)
        scoped_run = _run(
            {
                "tests/test_x.py::test_x[sqlite]": _node(
                    "tests/test_x.py::test_x[sqlite]",
                    "passed", _ctx(), fixtures=(0,)),
            },
            paths=paths, funcs=funcs,
            fixtures={0: _ctx(funcs=(0,))},
        )
        scoped_run = dataclasses.replace(
            scoped_run, vocabulary=_vocab(paths, funcs, fx_vocab))
        store.update(scoped_run, run_id=RUN_B, recorded_at=200.0,
                     compatibility=COMPAT, digests=digests, full=False)
        snap = store.snapshot()
        assert len(snap.fixtures) == 1
        record = snap.fixtures[0]
        assert sorted(record.deps.functions) == [0, 1]
        assert record.run_id == RUN_B
        assert snap.nodes["tests/test_x.py::test_x[pg]"].run_id == RUN_A
    finally:
        store.close()


# --- R24 eviction order ----------------------------------------------------

def _wide(prefix, count=150, funcs=20):
    vocab_funcs = tuple((1, f"f{n}") for n in range(funcs))
    nodes = {f"tests/test_a.py::{prefix}_{n}": _node(
        f"tests/test_a.py::{prefix}_{n}", deps=_ctx(funcs=range(funcs)))
        for n in range(count)}
    return S.RunDependencies(
        vocabulary=_vocab(("tests/test_a.py", "pkg/a.py"), vocab_funcs),
        nodes=nodes, fixtures={}, ambient=_ctx(), complete=True,
        recording=True, python=(3, 12), inactive_reason=None, notes=())


RUN_C = "33" * 16
RUN_D = "44" * 16


def _three_records_and_a_newcomer(store, **kw):
    for index, run_id in enumerate((RUN_A, RUN_B, RUN_C)):
        store.update(_wide("same"), run_id=run_id,
                     recorded_at=float(index + 1), compatibility=COMPAT,
                     digests=_digests(), full=False)
    store.update(_wide("other", count=10), run_id=RUN_D, recorded_at=4.0,
                 compatibility=COMPAT, digests=_digests(), full=False, **kw)


def _rows_of(store, run_id):
    return store._conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE run_id = ?",
        (run_id,)).fetchone()[0]


def test_eviction_never_drops_newest_row_or_written_run(domain_factory,
                                                         monkeypatch):
    """R24: under an unreachable target, surplus records go (oldest runs
    first) while every node keeps its newest row and the written run keeps
    all of its rows; whole-run eviction never trades the larger run."""
    domain = domain_factory()
    store = _open(domain)
    try:
        _three_records_and_a_newcomer(store)
        monkeypatch.setattr(S, "SELECTION_STORE_TARGET_BYTES", 1)
        store.update(_wide("written", count=20), run_id="55" * 16,
                     recorded_at=5.0, compatibility=COMPAT,
                     digests=_digests(), full=False)
        snap = store.snapshot()
        same = [n for n in snap.nodes if "::same_" in n]
        assert len(same) == 150
        for nodeid in same:
            assert [r.run_id for r in snap.records[nodeid]] == [RUN_C]
        assert _rows_of(store, "55" * 16) == 20
        assert RUN_A not in snap.runs and RUN_B not in snap.runs
        assert "55" * 16 in snap.runs
    finally:
        store.close()


def test_surplus_records_are_evicted_oldest_run_first(domain_factory,
                                                       monkeypatch):
    domain = domain_factory()
    store = _open(domain)
    try:
        _three_records_and_a_newcomer(store)
        order = S.SelectionStore._surplus_rows(store._conn, RUN_D)
        runs = [run for _, run in order]
        assert runs == [RUN_A] * 150 + [RUN_B] * 150
        assert not any(run in (RUN_C, RUN_D) for run in runs)
        calls = []
        real = S.SelectionStore._drop_rows

        def spy(conn, rowids):
            calls.append(len(rowids))
            real(conn, rowids)

        monkeypatch.setattr(S.SelectionStore, "_drop_rows",
                            staticmethod(spy))
        monkeypatch.setattr(S, "_SURPLUS_BATCH", 100)
        monkeypatch.setattr(
            S.SelectionStore, "_db_size",
            lambda self, conn: (S.SELECTION_STORE_TARGET_BYTES + 1
                                if not calls else 0))
        store.update(_wide("extra", count=2), run_id="55" * 16,
                     recorded_at=5.0, compatibility=COMPAT,
                     digests=_digests(), full=False)
        assert calls == [100]
        assert _rows_of(store, RUN_A) == 50
        assert _rows_of(store, RUN_B) == 150
        assert _rows_of(store, RUN_C) == 150
    finally:
        store.close()


def test_hard_cap_retry_uses_the_same_order(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        _three_records_and_a_newcomer(store)
        conn = store._conn
        store._evict_oldest(conn, exclude=RUN_D)
        assert _rows_of(store, RUN_A) == 0 and _rows_of(store, RUN_B) == 150
        store._evict_oldest(conn, exclude=RUN_D)
        assert _rows_of(store, RUN_B) == 0 and _rows_of(store, RUN_C) == 150
        # No surplus left: the fewest-records run goes, never the written one.
        store._evict_oldest(conn, exclude=RUN_D)
        assert _rows_of(store, RUN_D) == 10
        assert _rows_of(store, RUN_C) == 0
        assert set(store.snapshot().runs) == {RUN_D}
    finally:
        store.close()


# --- schema v2: migration and old readers -----------------------------------

_V1_SCHEMA = """
CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE paths(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
CREATE TABLE funcs(id INTEGER PRIMARY KEY, path_id INTEGER NOT NULL
    REFERENCES paths(id), qualname TEXT NOT NULL,
    UNIQUE(path_id, qualname));
CREATE TABLE fixtures(id INTEGER PRIMARY KEY, baseid TEXT NOT NULL,
    argname TEXT NOT NULL, scope TEXT NOT NULL,
    UNIQUE(baseid, argname, scope));
CREATE TABLE cache(digest TEXT PRIMARY KEY, version INTEGER NOT NULL,
    blob BLOB NOT NULL, used_at REAL NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, recorded_at REAL NOT NULL,
    compatibility TEXT NOT NULL, ambient BLOB NOT NULL);
CREATE TABLE run_digests(run_id TEXT NOT NULL REFERENCES runs(run_id)
    ON DELETE CASCADE, path_id INTEGER NOT NULL REFERENCES paths(id),
    digest TEXT NOT NULL, PRIMARY KEY(run_id, path_id));
CREATE TABLE nodes(nodeid TEXT PRIMARY KEY, test_file TEXT NOT NULL,
    outcome TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id),
    deps BLOB NOT NULL, fixtures BLOB NOT NULL);
CREATE TABLE fixture_deps(fixture_id INTEGER NOT NULL
    REFERENCES fixtures(id), run_id TEXT NOT NULL REFERENCES runs(run_id)
    ON DELETE CASCADE, deps BLOB NOT NULL,
    PRIMARY KEY(fixture_id, run_id));
CREATE TABLE demotions(nodeid TEXT PRIMARY KEY, digest TEXT NOT NULL);
"""


def _make_v1_store(domain, project_id=PROJ):
    """A 0.6.x-shaped store written with the old DDL and real rows."""
    project = S._project_dir(domain, project_id, create=True)
    db = project / "selection.db"
    conn = sqlite3.connect(db)
    conn.executescript(_V1_SCHEMA)
    ctx = S._pack_ctx(_ctx(funcs=(0,), modules=(1,)))
    conn.executemany("INSERT INTO paths(id, path) VALUES(?, ?)",
                     [(1, "pkg/a.py"), (2, "tests/test_a.py")])
    conn.execute("INSERT INTO funcs(id, path_id, qualname) VALUES(1,1,'f')")
    conn.execute("INSERT INTO fixtures(id, baseid, argname, scope)"
                 " VALUES(1, 'tests/conftest.py::fx', 'fx', 'session')")
    conn.executemany(
        "INSERT INTO kv(key, value) VALUES(?, ?)",
        [("schema_version", "1"), ("audit_checked", "7"),
         ("audit_misses", "1"), ("last_full_inv", '["tests/test_a.py::t0"]')])
    for run_id, at in ((RUN_A, 1.0), (RUN_B, 2.0)):
        conn.execute("INSERT INTO runs(run_id, recorded_at, compatibility,"
                     " ambient) VALUES(?, ?, ?, ?)",
                     (run_id, at, COMPAT, ctx))
        conn.execute("INSERT INTO run_digests(run_id, path_id, digest)"
                     " VALUES(?, 1, ?)", (run_id, "aa" * 32))
        conn.execute("INSERT INTO fixture_deps(fixture_id, run_id, deps)"
                     " VALUES(1, ?, ?)", (run_id, ctx))
    for index in range(5):
        conn.execute(
            "INSERT INTO nodes(nodeid, test_file, outcome, run_id, deps,"
            " fixtures) VALUES(?, 'tests/test_a.py', ?, ?, ?, ?)",
            (f"tests/test_a.py::t{index}", "failed" if index else "passed",
             RUN_A if index % 2 else RUN_B, ctx, S._pack_fixture_ids((1,))))
    conn.execute("INSERT INTO demotions(nodeid, digest) VALUES('x', 'dd')")
    conn.commit()
    conn.close()
    os.chmod(db, 0o600)
    return db


def _dump(db):
    conn = sqlite3.connect(db)
    try:
        return {
            "nodes": conn.execute(
                "SELECT nodeid, test_file, outcome, run_id, deps, fixtures"
                " FROM nodes ORDER BY nodeid, run_id").fetchall(),
            "runs": conn.execute(
                "SELECT run_id, recorded_at, compatibility, ambient"
                " FROM runs ORDER BY run_id").fetchall(),
            "digests": conn.execute(
                "SELECT * FROM run_digests ORDER BY 1, 2").fetchall(),
            "fixture_deps": conn.execute(
                "SELECT * FROM fixture_deps ORDER BY 1, 2").fetchall(),
            "paths": conn.execute("SELECT * FROM paths ORDER BY 1").fetchall(),
            "demotions": conn.execute("SELECT * FROM demotions").fetchall(),
            "kv": conn.execute(
                "SELECT * FROM kv WHERE key != 'schema_version'"
                " ORDER BY 1").fetchall(),
        }
    finally:
        conn.close()


def _schema_dump(db):
    conn = sqlite3.connect(db)
    try:
        return (conn.execute("SELECT type, name, sql FROM sqlite_master"
                             " ORDER BY name").fetchall(),
                conn.execute("SELECT * FROM kv ORDER BY 1").fetchall(),
                conn.execute("SELECT * FROM runs ORDER BY 1").fetchall())
    finally:
        conn.close()


def test_v1_store_migrates_in_place_idempotently(domain_factory):
    domain = domain_factory()
    db = _make_v1_store(domain)
    before = _dump(db)
    ino = os.stat(db).st_ino
    store = S.open_store(domain, PROJ, create=False)
    try:
        assert os.stat(db).st_ino == ino
        after = _dump(db)
        assert after == before          # no row lost or changed
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT value FROM kv WHERE key ="
                                " 'schema_version'").fetchone() == ("2",)
            pk = sorted(row[1] for row in conn.execute(
                "PRAGMA table_info(nodes)") if row[5])
            assert pk == ["nodeid", "run_id"]
            assert "fingerprint" in [row[1] for row in conn.execute(
                "PRAGMA table_info(runs)")]
            assert conn.execute("SELECT COUNT(*) FROM runs WHERE fingerprint"
                                " IS NOT NULL").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM observers"
                                ).fetchone() == (0,)
            index_sql = [row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'nodes' AND sql IS NOT NULL")]
            assert any("(nodeid)" in sql for sql in index_sql)
            assert conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE"
                                " 'last_gate_%'").fetchone() == (0,)
        finally:
            conn.close()
        snap = store.snapshot()
        assert len(snap.nodes) == 5 and snap.gate is None
        assert snap.observers == {} and snap.runs[RUN_A].fingerprint is None
        # The migrated store takes a second row for an existing node.
        store.update(_run({"tests/test_a.py::t0": _node(
            "tests/test_a.py::t0")}), run_id=RUN_C, recorded_at=3.0,
            compatibility=COMPAT, digests=_digests(), full=False)
        assert len(store.snapshot().records["tests/test_a.py::t0"]) == 2
    finally:
        store.close()
    # Re-opening, and re-running the migration itself, change nothing.
    settled = _schema_dump(db)
    S.open_store(domain, PROJ, create=False).close()
    assert _schema_dump(db) == settled
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        S._migrate_v1(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert _schema_dump(db) == settled


def test_v1_reader_refuses_v2_store_without_touching_it(domain_factory,
                                                         monkeypatch):
    """Spec Rollout: a 0.6.x reader (schema "1") gets the unknown-schema
    refusal and the v2 file stays byte-identical."""
    domain = domain_factory()
    store = _open(domain)
    _record(store, RUN_A, 1.0)
    store.close()
    db = _db_path(domain)
    before = db.read_bytes()
    monkeypatch.setattr(S, "_SCHEMA_VERSION", "1")
    with pytest.raises(C.Problem) as exc:
        S.open_store(domain, PROJ, create=True)
    assert exc.value.code == "coordinator-corrupt"
    assert "unknown schema" in exc.value.message
    assert db.read_bytes() == before
    assert not db.with_name("selection.db-journal").exists()


def test_v1_reader_leaves_large_v2_store_untouched(domain_factory,
                                                    monkeypatch):
    """An older reader's smaller cap refuses an over-cap v2 store with
    capacity-exceeded; the engine plans static and never removes it."""
    from ptest import selection_engine as E
    domain = domain_factory()
    store = _open(domain)
    _record(store, RUN_A, 1.0)
    store.close()
    db = _db_path(domain)
    old_cap = 1024 * 1024
    with open(db, "r+b") as handle:
        handle.truncate(old_cap + 4096)
    before = (db.read_bytes(), os.stat(db).st_ino)
    monkeypatch.setattr(S, "SELECTION_STORE_MAX_BYTES", old_cap)
    with pytest.raises(C.Problem) as exc:
        S.open_store(domain, PROJ, create=True)
    assert exc.value.code == "capacity-exceeded"
    assert E._open_store(domain, PROJ, create=True) is None
    assert (db.read_bytes(), os.stat(db).st_ino) == before


# --- observers, gate marker, fingerprint ------------------------------------

GATE_COMMIT = "ab" * 20


def _gate(dirty=None, at=50.0, commit=GATE_COMMIT):
    return C.GateMarker(commit=commit, dirty=dict(dirty or {}),
                        recorded_at=at)


def test_observers_gate_and_fingerprint_round_trip(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        fingerprint = "ef" * 32
        lost = _record(
            store, RUN_A, 1.0, full=True, fingerprint=fingerprint,
            observers={NODE_ONE: frozenset({"pkg/a.py", "data/x.json"})},
            gate=_gate({"pkg/a.py": "aa" * 32, "gone.py": None,
                        "big.bin": "?"}))
        assert lost == 0
        snap = store.snapshot()
        assert snap.observers == {NODE_ONE: frozenset({"pkg/a.py",
                                                       "data/x.json"})}
        assert snap.runs[RUN_A].fingerprint == fingerprint
        assert snap.gate == _gate({"pkg/a.py": "aa" * 32, "gone.py": None,
                                   "big.bin": "?"})
        assert snap.inventory == frozenset({NODE_ONE})
        meta = store.meta()
        assert (meta.nodes, meta.observers) == (1, 1)
        # A later run without them keeps the learned rows and the marker.
        _record(store, RUN_B, 2.0)
        snap = store.snapshot()
        assert snap.gate is not None and NODE_ONE in snap.observers
        assert snap.runs[RUN_B].fingerprint is None
    finally:
        store.close()


def test_observer_rows_merge_and_collapse_at_the_per_node_cap(
        domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        _record(store, RUN_A, 1.0, observers={NODE_ONE: {"a.py"}})
        _record(store, RUN_B, 2.0, observers={NODE_ONE: {"b.py", "a.py"}})
        assert store.snapshot().observers[NODE_ONE] == {"a.py", "b.py"}
        cap = S.SELECTION_OBSERVER_ROWS_PER_NODE
        assert cap == 64
        many = {f"p{n}.py" for n in range(cap - 2)}      # 62 + 2 = 64: fits
        _record(store, RUN_C, 3.0, observers={NODE_ONE: many})
        assert len(store.snapshot().observers[NODE_ONE]) == 64
        lost = _record(store, RUN_D, 4.0, observers={NODE_ONE: {"extra.py"}})
        assert store.snapshot().observers[NODE_ONE] == {"*"}
        assert lost == 0                  # widening is not eviction
        _record(store, "55" * 16, 5.0, observers={NODE_ONE: {"z.py"}})
        assert store.snapshot().observers[NODE_ONE] == {"*"}
    finally:
        store.close()


def test_observer_cap_eviction_is_reported_not_silent(domain_factory,
                                                       monkeypatch):
    """R14: the store cap drops oldest learned_at rows first, the count is
    returned, and no node record is demoted or removed by it."""
    domain = domain_factory()
    store = _open(domain)
    try:
        monkeypatch.setattr(S, "SELECTION_OBSERVER_ROWS_MAX", 5)
        nodes = [f"tests/test_a.py::t{n}" for n in range(4)]
        _record(store, RUN_A, 1.0, nodes=nodes, observers={
            nodes[0]: {"a.py", "b.py"}, nodes[1]: {"c.py"}})
        assert store.snapshot().observers[nodes[0]] == {"a.py", "b.py"}
        lost = _record(store, RUN_B, 2.0, nodes=nodes, observers={
            nodes[2]: {"d.py", "e.py", "f.py"}, nodes[3]: {"g.py"}})
        assert lost == 2                         # 7 rows, cap 5
        snap = store.snapshot()
        assert snap.observers == {nodes[1]: {"c.py"},
                                  nodes[2]: {"d.py", "e.py", "f.py"},
                                  nodes[3]: {"g.py"}}
        assert set(snap.nodes) == set(nodes)
        assert snap.demotions == {}
        assert {n: snap.nodes[n].outcome for n in nodes} == {
            n: "passed" for n in nodes}
    finally:
        store.close()


def test_observer_rows_are_pruned_only_with_their_node(domain_factory):
    """R17: dropped from neither scoped runs nor eviction; gone once the
    node id is absent from the last two full inventories."""
    domain = domain_factory()
    store = _open(domain)
    try:
        keep, drop = NODE_ONE, "tests/test_a.py::test_old"
        _record(store, RUN_A, 1.0, nodes=(keep, drop), full=True,
                observers={keep: {"a.py"}, drop: {"b.py"}})
        _record(store, RUN_B, 2.0, nodes=(keep,), full=False)
        assert set(store.snapshot().observers) == {keep, drop}
        _record(store, RUN_B, 3.0, nodes=(keep,), full=True)
        assert set(store.snapshot().observers) == {keep, drop}  # one gen
        _record(store, RUN_C, 4.0, nodes=(keep,), full=True)
        assert set(store.snapshot().observers) == {keep}
    finally:
        store.close()


def test_gate_marker_clears_or_validates(domain_factory):
    from ptest import lastgreen
    domain = domain_factory()
    store = _open(domain)
    try:
        _record(store, RUN_A, 1.0, gate=_gate({"a.py": "aa" * 32}))
        assert store.snapshot().gate is not None
        _record(store, RUN_B, 2.0, clear_gate=True)
        assert store.snapshot().gate is None
        _record(store, RUN_C, 3.0, gate=_gate({"a.py": "aa" * 32}))
        over = {f"f{n}.py": "bb" * 32
                for n in range(lastgreen.MAX_GREEN_FILES + 1)}
        _record(store, RUN_D, 4.0, gate=_gate(over))
        assert store.snapshot().gate is None
        keys = store._conn.execute(
            "SELECT COUNT(*) FROM kv WHERE key LIKE 'last_gate_%'"
        ).fetchone()[0]
        assert keys == 0
        exactly = {f"f{n}.py": "bb" * 32
                   for n in range(lastgreen.MAX_GREEN_FILES)}
        _record(store, "55" * 16, 5.0, gate=_gate(exactly))
        assert len(store.snapshot().gate.dirty) == lastgreen.MAX_GREEN_FILES
    finally:
        store.close()


@pytest.mark.parametrize("kwargs", [
    {"gate": _gate(), "clear_gate": True},
    {"fingerprint": "xyz"},
    {"fingerprint": "AB" * 32},
    {"fingerprint": "ab" * 31},
    {"observers": {"": {"a.py"}}},
    {"observers": {NODE_ONE: {""}}},
    {"observers": {NODE_ONE: "a.py"}},
    {"observers": [NODE_ONE]},
    {"gate": _gate(commit="not-hex")},
    {"gate": _gate({"a.py": 5})},
    {"gate": _gate({"": "aa"})},
])
def test_update_rejects_malformed_tier_state(domain_factory, kwargs):
    domain = domain_factory()
    store = _open(domain)
    try:
        with pytest.raises(ValueError):
            _record(store, RUN_A, 1.0, **kwargs)
        assert store.snapshot().nodes == {}
    finally:
        store.close()


@pytest.mark.parametrize("damage", [
    ("DELETE FROM kv WHERE key = 'last_gate_dirty'"),
    ("UPDATE kv SET value = 'nope' WHERE key = 'last_gate_commit'"),
    ("UPDATE kv SET value = '[1]' WHERE key = 'last_gate_dirty'"),
    ("UPDATE kv SET value = '{bad' WHERE key = 'last_gate_dirty'"),
    ("UPDATE kv SET value = 'x' WHERE key = 'last_gate_recorded_at'"),
])
def test_unreadable_gate_marker_reads_as_none(domain_factory, damage):
    domain = domain_factory()
    store = _open(domain)
    try:
        _record(store, RUN_A, 1.0, gate=_gate({"a.py": "aa" * 32}))
        assert store.snapshot().gate is not None
        store._conn.execute(damage)
        assert store.snapshot().gate is None
    finally:
        store.close()


def test_rejected_or_incomplete_ingest_writes_no_observers_or_marker(
        domain_factory):
    """R14/R27 (store half): the observers, marker and fingerprint ride the
    update transaction, so a rejected update leaves none of them."""
    domain = domain_factory()
    store = _open(domain)
    try:
        pages = store._conn.execute("PRAGMA page_count").fetchone()[0]
        store._conn.execute(f"PRAGMA max_page_count={pages}")
        nodes = {f"tests/test_a.py::test_{index}": _node(
            f"tests/test_a.py::test_{index}",
            deps=_ctx(funcs=(0,))) for index in range(300)}
        with pytest.raises(C.Problem) as exc:
            store.update(_run(nodes), run_id=RUN_A, recorded_at=1.0,
                         compatibility=COMPAT, digests=_digests(),
                         full=True, fingerprint="ab" * 32,
                         observers={NODE_ONE: {"a.py"}},
                         gate=_gate({"a.py": "aa" * 32}))
        assert exc.value.code == "capacity-exceeded"
        snap = store.snapshot()
        assert snap.observers == {} and snap.gate is None
        assert snap.runs == {} and snap.nodes == {}
        assert store._conn.execute(
            "SELECT COUNT(*) FROM kv WHERE key LIKE 'last_gate_%'"
        ).fetchone()[0] == 0
    finally:
        store.close()
