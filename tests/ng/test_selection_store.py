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


def test_unreferenced_runs_pruned(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        assert set(store.snapshot().runs) == {RUN_A}
        # Run B re-records the same node: run A's row is now unreferenced.
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one", "failed")}),
            run_id=RUN_B, recorded_at=2.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        snap = store.snapshot()
        assert set(snap.runs) == {RUN_B}
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
        # Every row parses: nothing torn; the shared node has exactly one
        # committed outcome (last writer wins).
        assert snap.nodes["tests/test_a.py::test_shared"].outcome in {
            "passed", "failed"}
        assert len(snap.nodes) == 51
        assert len(snap.runs) == 50
    finally:
        store.close()


def test_same_node_last_writer_wins_sequentially(domain_factory):
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one", "passed")}),
            run_id=RUN_A, recorded_at=1.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        store.update(_run({"tests/test_a.py::test_one": _node(
            "tests/test_a.py::test_one", "failed")}),
            run_id=RUN_B, recorded_at=2.0, compatibility=COMPAT,
            digests=_digests(), full=False)
        snap = store.snapshot()
        assert snap.nodes["tests/test_a.py::test_one"].outcome == "failed"
        assert snap.nodes["tests/test_a.py::test_one"].run_id == RUN_B
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
    domain = domain_factory()
    store = _open(domain)
    try:
        store.update(_big_run(), run_id=RUN_A, recorded_at=1.0,
                     compatibility=COMPAT,
                     digests={"pkg/big.py": "aa" * 32,
                              "tests/test_big.py": "bb" * 32},
                     full=True)
        size = _db_path(domain).stat().st_size
        assert size <= S.SELECTION_STORE_MAX_BYTES
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
