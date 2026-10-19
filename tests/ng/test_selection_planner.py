"""Decision-table tests for the dynamic selection planner (T2).

Pure unit tests over hand-made contracts dataclasses covering design
section 4 (per-path diff rows, propagation fixpoint, per-test clauses,
unrecorded files, output shape) plus the self-audit miss computation.
Every clause has a positive test and a negative twin.

No I/O, no clock, no T1/T3 imports: inputs are built by the small
helpers below. The ``TestWithSourceIndex`` class is the only exception
and is gated on T1 being present.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ptest import contracts as C

from ptest import selection_planner as PL

K = "compat-fingerprint"
OTHER = "other-configuration"
LIB = "pkg/lib.py"
TEST = "tests/test_lib.py"


# ---------------------------------------------------------------------------
# Builders: hand-made index / snapshot fragments. The vocabulary builder
# VB must be shared between RUN digests and NODE deps so path ids align.
# ---------------------------------------------------------------------------

def SC(q, body="b:" + "x", skel="s:" + "x", refs=(), decorated=False,
       local=False):
    return C.ScopeIndex(qualname=q, body=body, skeleton=skel,
                        refs=tuple(tuple(r) for r in refs),
                        decorated=decorated, local=local)


def CL(q, skel="s:" + "x", body="c:" + "x", refs=(), plain=False):
    return C.ClassIndex(qualname=q, skeleton=skel, body=body,
                        refs=tuple(tuple(r) for r in refs), plain=plain)


def ST(kind, bound=(), refs=(), fp="fp"):
    return C.StatementIndex(kind=kind, bound=tuple(bound),
                            refs=tuple(tuple(r) for r in refs),
                            fingerprint=fp)


def FI(scopes=(), classes=(), stmts=(), parsed=True):
    return C.FileIndex(parsed=parsed, imports=(),
                       scopes=tuple(scopes), classes=tuple(classes),
                       statements=tuple(stmts))


def PF(path, digest, index, test=False, support=False, imports=(),
       scope_refs=None, class_refs=None, stmt_refs=None, stmt_bound=None):
    n = len(index.statements)
    return C.ProjectFile(
        path=path, digest=digest, index=index, modules=(), test=test,
        support=support, imports=frozenset(imports),
        scope_refs=dict(scope_refs or {}),
        class_refs=dict(class_refs or {}),
        statement_refs=tuple(stmt_refs if stmt_refs is not None
                             else [frozenset() for _ in range(n)]),
        statement_bound=tuple(stmt_bound if stmt_bound is not None
                               else [frozenset() for _ in range(n)]),
    )


def IDX(files, reverse=None, complete=True):
    files = dict(files)
    test_files = frozenset(p for p, pf in files.items() if pf.test)
    if reverse is None:
        rev = {}
        for p, pf in files.items():
            for imp in pf.imports:
                rev.setdefault(imp, set()).add(p)
        reverse = {k: frozenset(v) for k, v in rev.items()}
    unparsed = frozenset(p for p, pf in files.items() if not pf.index.parsed)
    return C.ProjectIndex(root=Path("."), files=files,
                          test_files=test_files, reverse=dict(reverse),
                          unparsed=unparsed, complete=complete)


class VB:
    """One run-local vocabulary; ids are dense and shared by RUN/NODE."""

    def __init__(self):
        self._paths = {}
        self._funcs = {}
        self._fixts = {}

    def p(self, path):
        return self._paths.setdefault(path, len(self._paths))

    def f(self, path, qual):
        key = (self.p(path), qual)
        return self._funcs.setdefault(key, len(self._funcs))

    def fx(self, base, arg, scope):
        key = (base, arg, scope)
        return self._fixts.setdefault(key, len(self._fixts))

    def build(self):
        paths = [None] * len(self._paths)
        for path, i in self._paths.items():
            paths[i] = path
        funcs = [None] * len(self._funcs)
        for (pid, qual), i in self._funcs.items():
            funcs[i] = (pid, qual)
        fixts = [None] * len(self._fixts)
        for key, i in self._fixts.items():
            fixts[i] = key
        return C.DepVocabulary(paths=tuple(paths), functions=tuple(funcs),
                               fixtures=tuple(fixts))


def CT(vb, funcs=(), mods=(), data=(), opaque=False):
    return C.ContextDeps(
        functions=C.selection_ids(vb.f(p, q) for p, q in funcs),
        modules=C.selection_ids(vb.p(m) for m in mods),
        data=C.selection_ids(vb.p(d) for d in data),
        opaque=opaque,
    )


def RUN(vb, rid, digests, compat=K, recorded_at=0.0, ambient=None):
    return C.RunBaseline(
        run_id=rid, recorded_at=recorded_at, compatibility=compat,
        digests={vb.p(path): dg for path, dg in digests.items()},
        ambient=ambient or C.selection_empty_context(),
    )


def NODE(vb, nodeid, test_file, run_id, outcome="passed", funcs=(),
         mods=(), data=(), opaque=False, fixtures=()):
    return C.NodeRecord(
        nodeid=nodeid, test_file=test_file, outcome=outcome, run_id=run_id,
        deps=CT(vb, funcs, mods, data, opaque),
        fixtures=C.selection_ids(fixtures),
    )


def FIX(fxid, run_id, deps):
    return C.FixtureRecord(fixture=fxid, run_id=run_id, deps=deps)


def SNAP(vb, runs, nodes, fixtures=(), demotions=None, records=None,
         inventory=()):
    """``nodes`` is one record per node id (newest); ``records`` optionally
    maps a node id to every kept record (newest first)."""
    return C.DependencySnapshot(
        vocabulary=vb.build(),
        runs={r.run_id: r for r in runs},
        nodes={n.nodeid: n for n in nodes},
        fixtures={f.fixture: f for f in fixtures},
        demotions=dict(demotions or {}),
        records=({k: tuple(v) for k, v in records.items()}
                 if records is not None else None),
        inventory=frozenset(inventory),
    )


def INP(index, deps, versions=None, data_digests=None, compat=K,
        changed=frozenset(), test_roots=("tests",), since_gate=(),
        observers=None, conditional=False, audit=False):
    return C.SelectionInputs(
        index=index, deps=deps, versions=dict(versions or {}),
        data_digests=dict(data_digests or {}), compatibility=compat,
        changed=frozenset(changed), test_roots=tuple(test_roots),
        since_gate=frozenset(since_gate),
        observers={k: frozenset(v) for k, v in (observers or {}).items()},
        conditional=conditional, audit=audit,
    )


T1 = C.SELECTION_TIER_RUN_NOW
T2 = C.SELECTION_TIER_DEFERRED


def _check_invariants(d):
    """Invariants every decision must satisfy (R1, R5, T2 and reached)."""
    t2_keys = set()
    t1_nodes = 0
    for key_, (tier, code) in d.tiers.items():
        assert tier in (T1, T2), (key_, tier)
        codes = (C.SELECTION_T1_CODES if tier == T1
                 else C.SELECTION_T2_CODES)
        assert code in codes, (key_, tier, code)
        if tier == T2:
            t2_keys.add(key_)
            assert key_ in d.via, key_
            if "::" in key_:
                assert PL.is_selected(d, key_) is False, key_
            else:
                assert key_ not in d.files, key_
        elif "::" in key_:
            t1_nodes += 1
            assert PL.is_selected(d, key_), key_
    assert set(d.via) == t2_keys
    assert d.selected == t1_nodes
    t2_nodes = sum(1 for k in t2_keys if "::" in k)
    assert d.reached <= d.recorded - d.selected - t2_nodes
    assert not set(d.deselect) & {k for k, (t, _) in d.tiers.items()
                                  if t == T1}
    for nodeid, _ in d.conditional:
        assert d.tiers[nodeid][0] == T2
    assert d.full_reason is None
    for field_ in (d.files, d.deselect, d.whole_files):
        assert list(field_) == sorted(field_)


def _plan(inputs):
    decision = PL.plan(inputs)
    _check_invariants(decision)
    return decision


def _assert_t2(d, nodeid, code, via=None):
    """The node is deferred (never run) under ``code`` (R1, R5)."""
    assert d.tiers.get(nodeid) == (T2, code)
    assert not PL.is_selected(d, nodeid)
    if via is not None:
        assert d.via[nodeid] == via


def key(path, name):
    return C.selection_name_key(path, name)


def units_by_kind(decision):
    out = {}
    for u in decision.units:
        out.setdefault(u.kind, {})[u.label] = u.tests
    return out


def pair(old_lib, new_lib, *, test_fi=None, old_test_fi=None, node_kw=None,
         run_digests=None, versions=None, changed=(LIB,), lib_digests=("do", "dn"),
         test_digest="dt", test_old_digest=None, outcome="passed",
         test_refs=None, lib_refs=None, lib_class_refs=None, run_id="r1",
         recorded_at=0.0, data_digests=None, test_imports=(LIB,), compat=K,
         extra_nodes=(), extra_runs=(), fixtures=(), demotions=None,
         seed_versions=True, lib_stmt_refs=None, lib_stmt_bound=None,
         test_stmt_refs=None, test_stmt_bound=None, run_compat=None):
    """One lib file + one test file calling it; returns (inputs, nodeid)."""
    vb = VB()
    test_fi = test_fi if test_fi is not None else FI(scopes=[SC("test_f")])
    old_test_fi = old_test_fi if old_test_fi is not None else test_fi
    test_old_digest = test_old_digest or test_digest
    index = IDX({
        LIB: PF(LIB, lib_digests[1], new_lib, scope_refs=lib_refs,
                class_refs=lib_class_refs, stmt_refs=lib_stmt_refs,
                stmt_bound=lib_stmt_bound),
        TEST: PF(TEST, test_digest, test_fi, test=True,
                 imports=test_imports, scope_refs=test_refs,
                 stmt_refs=test_stmt_refs, stmt_bound=test_stmt_bound),
    })
    node_kw = dict(node_kw or {})
    node_kw.setdefault("funcs", [(LIB, "f")])
    node = NODE(vb, TEST + "::test_f", TEST, run_id, outcome=outcome, **node_kw)
    digests = dict(run_digests or {})
    digests.setdefault(LIB, lib_digests[0])
    digests.setdefault(TEST, test_old_digest)
    run = RUN(vb, run_id, digests, compat=run_compat or compat,
              recorded_at=recorded_at)
    vers = dict(versions or {})
    if seed_versions:
        vers.setdefault(lib_digests[0], old_lib)
        vers.setdefault(test_old_digest, old_test_fi)
    deps = SNAP(vb, [run, *extra_runs], [node, *extra_nodes],
                fixtures=fixtures, demotions=demotions)
    return INP(index, deps, versions=vers, data_digests=data_digests,
               compat=compat, changed=changed), node.nodeid


# ---------------------------------------------------------------------------
# 4.3 per-path diff rows (each: positive + negative twin)
# ---------------------------------------------------------------------------

def test_body_change_selects_caller():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert d.files == (TEST,)
    assert units_by_kind(d)["function"] == {LIB + "::f": 1}


def test_identical_fingerprints_select_nothing():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b1")])  # formatting-only edit
    inputs, nid = pair(old, new, lib_digests=("do", "dn"))
    # Baseline digest differs from current, but content fingerprints match.
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.files == ()
    assert d.units == ()
    assert d.selected == 0
    # The digest changed, so the node still counts as having reached it.
    assert d.reached == 1


def test_skeleton_change_seeds_name():
    old = FI(scopes=[SC("f", skel="s1")])
    new = FI(scopes=[SC("f", skel="s2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["name"] == {LIB + ":f": 0}


def test_same_skeleton_gives_no_name_unit():
    old = FI(scopes=[SC("f", skel="s1", body="b1")])
    new = FI(scopes=[SC("f", skel="s1", body="b2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)  # body change still selects via CF
    assert "name" not in units_by_kind(d)


def test_new_scope_in_test_file_selects_whole():
    lib = FI(scopes=[SC("f")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f"), SC("test_g")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert TEST in d.whole_files


def test_new_scope_in_lib_is_not_whole_file():
    new_stmts = [ST("def", bound=("f",), fp="s1"), ST("def", bound=("g",), fp="s2")]
    old_stmts = [ST("def", bound=("f",), fp="s1")]
    old = FI(scopes=[SC("f")], stmts=old_stmts)
    new = FI(scopes=[SC("f"), SC("g")], stmts=new_stmts)
    inputs, nid = pair(old, new, node_kw={"funcs": [(LIB, "f")]})
    d = _plan(inputs)
    assert d.whole_files == ()
    # The new def binds a fresh name: a name unit appears, but the old
    # caller is unaffected by the addition alone.
    assert units_by_kind(d).get("name", {}).get(LIB + ":g") == 0
    assert not PL.is_selected(d, nid)


def test_class_body_change_selects_methods_and_seeds_name():
    old = FI(classes=[CL("C", body="c1")],
             scopes=[SC("C.m", body="b1")])
    new = FI(classes=[CL("C", body="c2")],
             scopes=[SC("C.m", body="b1")])
    inputs, nid = pair(old, new, node_kw={"funcs": [(LIB, "C.m")]})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["name"] == {LIB + ":C": 0}
    assert units_by_kind(d)["function"] == {LIB + "::C": 0, LIB + "::C.m": 1}


def test_unchanged_class_selects_nothing():
    old = FI(classes=[CL("C", body="c1")],
             scopes=[SC("C.m", body="b1")])
    inputs, nid = pair(old, old, node_kw={"funcs": [(LIB, "C.m")]})
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.files == ()


def test_new_class_in_test_file_selects_whole():
    lib = FI(scopes=[SC("f")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f")], classes=[CL("Helper")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    d = _plan(inputs)
    assert TEST in d.whole_files


def test_new_class_in_lib_is_not_whole_file():
    old = FI(scopes=[SC("f")])
    new = FI(scopes=[SC("f")], classes=[CL("G")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert d.whole_files == ()
    assert not PL.is_selected(d, nid)


def test_assign_change_seeds_name_unit():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    new = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a2")])
    inputs, nid = pair(old, new, node_kw={"funcs": []})
    d = _plan(inputs)
    assert units_by_kind(d)["name"] == {LIB + ":LIMIT": 0}


def test_unchanged_assign_seeds_no_name():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    inputs, nid = pair(old, old, node_kw={"funcs": []})
    d = _plan(inputs)
    assert "name" not in units_by_kind(d)
    assert d.files == ()


def test_star_import_change_widens_dependents():
    old = FI(stmts=[ST("import", bound=("*",), fp="i1")])
    new = FI(stmts=[ST("import", bound=("*",), fp="i2")])
    # Another file references a name from the star-importing module; the
    # wildcard marks the whole module changed, so its referrers reselect.
    vb = VB()
    other = "pkg/other.py"
    otest = "tests/test_other.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g")]),
                  scope_refs={"g": frozenset({key(LIB, "THING")})}),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
        otest: PF(otest, "dt2", FI(scopes=[SC("test_g")]), test=True,
                  scope_refs={"test_g": frozenset({key(other, "g")})}),
    })
    n1 = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    n2 = NODE(vb, otest + "::test_g", otest, "r1", funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt", otest: "dt2"})
    deps = SNAP(vb, [run], [n1, n2])
    inputs = INP(index, deps, versions={"do": old, "do2": FI(scopes=[SC("g")]),
                                       "dt": FI(scopes=[SC("test_f")]),
                                       "dt2": FI(scopes=[SC("test_g")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert PL.is_selected(d, n2.nodeid)
    assert not PL.is_selected(d, n1.nodeid)


def test_same_star_imports_do_not_widen():
    old = FI(stmts=[ST("import", bound=("*",), fp="i1")])
    vb = VB()
    other = "pkg/other.py"
    otest = "tests/test_other.py"
    index = IDX({
        LIB: PF(LIB, "dn", old),
        other: PF(other, "do2", FI(scopes=[SC("g")]),
                  scope_refs={"g": frozenset({key(LIB, "THING")})}),
        otest: PF(otest, "dt2", FI(scopes=[SC("test_g")]), test=True,
                  scope_refs={"test_g": frozenset({key(other, "g")})}),
    })
    n2 = NODE(vb, otest + "::test_g", otest, "r1", funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", otest: "dt2"})
    deps = SNAP(vb, [run], [n2])
    inputs = INP(index, deps, versions={"do": old, "do2": FI(scopes=[SC("g")]),
                                       "dt2": FI(scopes=[SC("test_g")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert not PL.is_selected(d, n2.nodeid)


def test_effect_change_selects_module_dependents():
    old = FI(stmts=[ST("effect", fp="e1")])
    new = FI(stmts=[ST("effect", fp="e2")])
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_effect_reorder_without_change_selects_nothing():
    stmts = [ST("effect", fp="e1"), ST("effect", fp="e2")]
    old = FI(stmts=stmts)
    new = FI(stmts=list(reversed(stmts)))  # same multiset
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]})
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert "module" not in units_by_kind(d)


def test_missing_old_version_selects_conservatively():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b1")])  # identical, but no old cached
    inputs, nid = pair(old, new, versions={}, seed_versions=False)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)  # whole-path: PW + W + AM + SK
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_unparsed_old_index_selects_whole():
    old = FI(parsed=False)
    new = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_unparsed_new_index_selects_whole():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(parsed=False)
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)


def test_doc_statement_change_is_ignored():
    old = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("doc", fp="d1")])
    new = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("doc", fp="d2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.units == ()


def test_doc_statement_bound_name_is_ignored():
    # A doc statement binding a name must not seed NAMES by itself.
    old = FI(stmts=[ST("doc", bound=("NOTE",), fp="d1")])
    new = FI(stmts=[ST("doc", bound=("NOTE",), fp="d2")])
    inputs, nid = pair(old, new, node_kw={"funcs": []})
    d = _plan(inputs)
    assert "name" not in units_by_kind(d)
    assert d.files == ()


def test_added_non_test_file_has_no_pathwide_functions():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    added = "pkg/added.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        added: PF(added, "da-new", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    # Baseline has no entry for the added file: only W/AM/SK, no PW.
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"do": old, "da-new": new,
                                       "dt": FI(scopes=[SC("test_f")])},
                 changed=(added,))
    d = _plan(inputs)
    assert not PL.is_selected(d, node.nodeid)
    assert units_by_kind(d)["module"] == {added: 0}
    assert "function" not in units_by_kind(d)


def test_removed_file_selects_its_callers():
    old = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    index = IDX({
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"do": old, "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert PL.is_selected(d, node.nodeid)


def test_test_file_body_only_edit_deselects_rest():
    lib = FI(scopes=[SC("f", body="b1")])
    old_t = FI(scopes=[SC("test_f", body="t1"), SC("test_g", body="g1")])
    new_t = FI(scopes=[SC("test_f", body="t2"), SC("test_g", body="g1")])
    vb = VB()
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt-new", new_t, test=True),
    })
    n_f = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f"), (TEST, "test_f")])
    n_g = NODE(vb, TEST + "::test_g", TEST, "r1", funcs=[(LIB, "f"), (TEST, "test_g")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt-old"})
    deps = SNAP(vb, [run], [n_f, n_g])
    inputs = INP(index, deps, versions={"d": lib, "dt-old": old_t},
                 changed=(TEST,))
    d = _plan(inputs)
    # Only CF on pre-existing scopes: no whole-file selection; the edited
    # test runs and the rest of the file is deselected.
    assert d.whole_files == ()
    assert PL.is_selected(d, n_f.nodeid)
    assert not PL.is_selected(d, n_g.nodeid)
    assert d.deselect == (n_g.nodeid,)
    assert d.files == (TEST,)


def test_test_file_constant_change_selects_whole_file():
    lib = FI(scopes=[SC("f", body="b1")])
    old_t = FI(scopes=[SC("test_f")],
               stmts=[ST("assign", bound=("FLAG",), fp="a1")])
    new_t = FI(scopes=[SC("test_f")],
               stmts=[ST("assign", bound=("FLAG",), fp="a2")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    d = _plan(inputs)
    assert TEST in d.whole_files
    assert PL.is_selected(d, nid)


# ---------------------------------------------------------------------------
# 4.4 propagation fixpoint
# ---------------------------------------------------------------------------

def test_constant_used_by_function_body_propagates():
    old = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    new = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a2")])
    lib_refs = {"f": frozenset({key(LIB, "LIMIT")})}
    inputs, nid = pair(old, new, lib_refs=lib_refs)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)  # LIMIT seed -> AF(LIB, f) -> clause d


def test_constant_unused_by_any_body_selects_nothing():
    old = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    new = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a2")])
    lib_refs = {"f": frozenset({key(LIB, "OTHER")})}
    inputs, nid = pair(old, new, lib_refs=lib_refs, node_kw={"funcs": [(LIB, "f")]})
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert units_by_kind(d)["name"] == {LIB + ":LIMIT": 0}


def test_default_referencing_changed_name_selects_referrers():
    # helper's default mentions LIMIT: the def statement's skeleton refs
    # match, binding helper as a changed name; a test scope referencing
    # helper is then affected.
    old = FI(scopes=[SC("helper", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a1"),
                    ST("def", bound=("helper",),
                       refs=[("LIMIT",)], fp="s1")])
    new = FI(scopes=[SC("helper", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a2"),
                    ST("def", bound=("helper",),
                       refs=[("LIMIT",)], fp="s1")])
    test_fi = FI(scopes=[SC("test_f")])
    inputs, nid = pair(
        old, new, test_fi=test_fi,
        node_kw={"funcs": [(LIB, "helper"), (TEST, "test_f")]},
        test_refs={"test_f": frozenset({key(LIB, "helper")})},
        lib_stmt_refs=[frozenset(), frozenset({key(LIB, "LIMIT")})],
        lib_stmt_bound=[frozenset({key(LIB, "LIMIT")}),
                        frozenset({key(LIB, "helper")})],
    )
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    # Propagated names never become units: only the LIMIT seed does.
    assert units_by_kind(d).get("name", {}) == {LIB + ":LIMIT": 0}


def test_default_with_literal_selects_nothing():
    old = FI(scopes=[SC("helper", body="b1")],
             stmts=[ST("def", bound=("helper",), refs=[], fp="s1")])
    new = FI(scopes=[SC("helper", body="b2")],
             stmts=[ST("def", bound=("helper",), refs=[], fp="s1")])
    inputs, nid = pair(old, new, node_kw={"funcs": [(LIB, "other")]})
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)


def test_subclass_of_changed_class_selects_method_callers():
    old = FI(classes=[CL("Base", body="c1"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    new = FI(classes=[CL("Base", body="c2"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    lib_class_refs = {"Sub": frozenset({key(LIB, "Base")})}
    inputs, nid = pair(old, new, lib_class_refs=lib_class_refs,
                       node_kw={"funcs": [(LIB, "Sub.run")]})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)


def test_subclass_of_unchanged_class_selects_nothing():
    old = FI(classes=[CL("Base", body="c1"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    lib_class_refs = {"Sub": frozenset({key(LIB, "Base")})}
    inputs, nid = pair(old, old, lib_class_refs=lib_class_refs,
                       node_kw={"funcs": [(LIB, "Sub.run")]})
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)


def test_reexport_chain_across_two_inits():
    # pkg/mod.py:X -> pkg/__init__.py:X -> api.py:X -> test scope.
    vb = VB()
    mod, pkginit, api = "pkg/mod.py", "pkg/__init__.py", "pkg/api.py"
    old_mod = FI(stmts=[ST("assign", bound=("X",), fp="a1")])
    new_mod = FI(stmts=[ST("assign", bound=("X",), fp="a2")])
    chain = FI(stmts=[ST("import", bound=("X",),
                         refs=[("X",)], fp="i1")])
    index = IDX({
        mod: PF(mod, "dm-new", new_mod),
        pkginit: PF(pkginit, "di", chain,
                    stmt_refs=[frozenset({key(mod, "X")})],
                    stmt_bound=[frozenset({key(pkginit, "X")})]),
        api: PF(api, "da", chain,
                stmt_refs=[frozenset({key(pkginit, "X")})],
                stmt_bound=[frozenset({key(api, "X")})]),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 scope_refs={"test_f": frozenset({key(api, "X")})},
                 imports=(api,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(TEST, "test_f")])
    run = RUN(vb, "r1", {mod: "dm-old", pkginit: "di", api: "da", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"dm-old": old_mod, "di": chain, "da": chain,
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(mod,))
    d = _plan(inputs)
    assert PL.is_selected(d, node.nodeid)


def test_reexport_chain_ignores_unrelated_change():
    vb = VB()
    mod, pkginit = "pkg/mod.py", "pkg/__init__.py"
    old_mod = FI(stmts=[ST("assign", bound=("X",), fp="a1"),
                        ST("assign", bound=("Y",), fp="y1")])
    new_mod = FI(stmts=[ST("assign", bound=("X",), fp="a1"),
                        ST("assign", bound=("Y",), fp="y2")])
    chain = FI(stmts=[ST("import", bound=("X",),
                        refs=[("X",)], fp="i1")])
    index = IDX({
        mod: PF(mod, "dm-new", new_mod),
        pkginit: PF(pkginit, "di", chain,
                    stmt_refs=[frozenset({key(mod, "X")})],
                    stmt_bound=[frozenset({key(pkginit, "X")})]),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 scope_refs={"test_f": frozenset({key(pkginit, "X")})}),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(TEST, "test_f")])
    run = RUN(vb, "r1", {mod: "dm-old", pkginit: "di", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"dm-old": old_mod, "di": chain,
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(mod,))
    d = _plan(inputs)
    # Y changed, but the chain only re-exports X: the test is unaffected.
    assert not PL.is_selected(d, node.nodeid)


def test_effect_statement_referencing_changed_name_selects_module_users():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1"),
                    ST("effect", refs=[("LIMIT",)], fp="e1")])
    new = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a2"),
                    ST("effect", refs=[("LIMIT",)], fp="e1")])
    refs = [frozenset(), frozenset({key(LIB, "LIMIT")})]
    bound = [frozenset({key(LIB, "LIMIT")}), frozenset()]
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]},
                       lib_stmt_refs=refs, lib_stmt_bound=bound)
    d = _plan(inputs)
    # LIMIT seed -> effect statement matches -> AM -> SK -> clause e.
    assert PL.is_selected(d, nid)


def test_name_change_selects_module_users_through_skeleton():
    # The LIMIT seed puts LIB in SK, so a module-body dependent reruns
    # even though no effect statement references the changed name.
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1"),
                    ST("effect", refs=[("OTHER",)], fp="e1")])
    new = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a2"),
                    ST("effect", refs=[("OTHER",)], fp="e1")])
    refs = [frozenset(), frozenset({key(LIB, "OTHER")})]
    bound = [frozenset({key(LIB, "LIMIT")}), frozenset()]
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]},
                       lib_stmt_refs=refs, lib_stmt_bound=bound)
    d = _plan(inputs)
    assert PL.is_selected(d, nid)


def test_effect_statement_without_any_change_selects_nothing():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1"),
                    ST("effect", refs=[("LIMIT",)], fp="e1")])
    refs = [frozenset(), frozenset({key(LIB, "LIMIT")})]
    bound = [frozenset({key(LIB, "LIMIT")}), frozenset()]
    inputs, nid = pair(old, old, node_kw={"funcs": [], "mods": [LIB]},
                       lib_stmt_refs=refs, lib_stmt_bound=bound)
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)


def test_parametrize_with_changed_constant_selects_whole_file():
    lib = FI(stmts=[ST("assign", bound=("CASES",), fp="a1")])
    new_lib = FI(stmts=[ST("assign", bound=("CASES",), fp="a2")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f")], stmts=[ST("effect", fp="p1")])
    inputs, nid = pair(
        lib, new_lib, test_fi=new_t, old_test_fi=old_t,
        lib_digests=("do", "dn"), changed=(LIB,),
        # @pytest.mark.parametrize(CASES) references the changed constant.
        test_stmt_refs=[frozenset({key(LIB, "CASES")})],
        test_stmt_bound=[frozenset()],
    )
    d = _plan(inputs)
    assert TEST in d.whole_files


def test_parametrize_with_literal_selects_nothing_extra():
    lib = FI(stmts=[ST("assign", bound=("CASES",), fp="a1")])
    new_lib = FI(stmts=[ST("assign", bound=("CASES",), fp="a2")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f")], stmts=[ST("effect", fp="p1")])
    inputs, nid = pair(
        lib, new_lib, test_fi=new_t, old_test_fi=old_t,
        lib_digests=("do", "dn"), changed=(LIB,),
        test_stmt_refs=[frozenset()],  # literal parametrize values
        test_stmt_bound=[frozenset()],
    )
    d = _plan(inputs)
    assert TEST not in d.whole_files
    assert not PL.is_selected(d, nid)


# ---------------------------------------------------------------------------
# Per-run baselines (D5) and the signature cap
# ---------------------------------------------------------------------------

def _two_run_pair():
    """Same file, two runs with different baseline digests."""
    vb = VB()
    old_a = FI(scopes=[SC("f", body="b-old")])
    new_cur = FI(scopes=[SC("f", body="b-new")])
    test_fi = FI(scopes=[SC("test_f")])
    index = IDX({
        LIB: PF(LIB, "d-cur", new_cur),
        TEST: PF(TEST, "dt", test_fi, test=True, imports=(LIB,)),
    })
    n_old = NODE(vb, TEST + "::test_old", TEST, "r-old", funcs=[(LIB, "f")])
    n_new = NODE(vb, TEST + "::test_new", TEST, "r-new", funcs=[(LIB, "f")])
    r_old = RUN(vb, "r-old", {LIB: "d-old", TEST: "dt"}, recorded_at=1.0)
    r_new = RUN(vb, "r-new", {LIB: "d-cur", TEST: "dt"}, recorded_at=2.0)
    deps = SNAP(vb, [r_old, r_new], [n_old, n_new])
    inputs = INP(index, deps, versions={"d-old": old_a, "dt": test_fi},
                 changed=(LIB,))
    return inputs, n_old.nodeid, n_new.nodeid


def test_runs_judged_against_own_baseline():
    inputs, old_nid, new_nid = _two_run_pair()
    d = _plan(inputs)
    # r-new already ran against the current content: its node stays.
    assert not PL.is_selected(d, new_nid)
    # r-old ran against older content: its node reruns.
    assert PL.is_selected(d, old_nid)


def test_rerecorded_node_not_reselected_but_old_run_still_is():
    # N1 regression: a constant change re-recorded by the newer run must
    # not reselect that run's node, while the older run still reselects.
    vb = VB()
    const_old = FI(stmts=[ST("assign", bound=("C",), fp="a-old")],
                   scopes=[SC("f", body="b")])
    const_new = FI(stmts=[ST("assign", bound=("C",), fp="a-new")],
                   scopes=[SC("f", body="b")])
    lib_refs = {"f": frozenset({key(LIB, "C")})}
    index = IDX({
        LIB: PF(LIB, "d-cur", const_new, scope_refs=lib_refs),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
    })
    n_old = NODE(vb, TEST + "::test_old", TEST, "r-old", funcs=[(LIB, "f")])
    n_new = NODE(vb, TEST + "::test_new", TEST, "r-new", funcs=[(LIB, "f")])
    r_old = RUN(vb, "r-old", {LIB: "d-old", TEST: "dt"}, recorded_at=1.0)
    r_new = RUN(vb, "r-new", {LIB: "d-cur", TEST: "dt"}, recorded_at=2.0)
    deps = SNAP(vb, [r_old, r_new], [n_old, n_new])
    inputs = INP(index, deps,
                 versions={"d-old": const_old,
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert PL.is_selected(d, n_old.nodeid)
    assert not PL.is_selected(d, n_new.nodeid)


def test_signature_cap_drops_oldest_runs():
    vb = VB()
    test_fi = FI(scopes=[SC("test_f")])
    files = {TEST: PF(TEST, "dt", test_fi, test=True)}
    runs, nodes, versions = [], [], {"dt": test_fi}
    for i in range(C.SELECTION_MAX_SIGNATURES + 2):
        rid = f"r-{i:04d}"
        old = FI(scopes=[SC("f", body=f"b-{i}")])
        new = FI(scopes=[SC("f", body="b-cur")])
        # Distinct stale digests force distinct signatures.
        files[LIB] = PF(LIB, "d-cur", new)
        runs.append(RUN(vb, rid, {LIB: f"d-{i}", TEST: "dt"},
                        recorded_at=float(i)))
        nodes.append(NODE(vb, TEST + f"::test_{i:04d}", TEST, rid,
                          funcs=[(LIB, "f")]))
        versions[f"d-{i}"] = old
    index = IDX(files)
    deps = SNAP(vb, runs, nodes)
    inputs = INP(index, deps, versions=versions, changed=(LIB,))
    d = _plan(inputs)
    oldest = TEST + "::test_0000"
    newest = TEST + f"::test_{C.SELECTION_MAX_SIGNATURES + 1:04d}"
    assert dict(d.fallbacks)[oldest] == "too many recorded code versions"
    # The newest run keeps its precise record and is unaffected here
    # (its stale content differs from current, so it still reselects).
    assert PL.is_selected(d, newest)
    assert d.recorded < len(nodes)


# ---------------------------------------------------------------------------
# Fixtures (N1)
# ---------------------------------------------------------------------------

def test_session_fixture_change_selects_all_users():
    vb = VB()
    conftest = "tests/conftest.py"
    old_fx = FI(scopes=[SC("boot", body="b1")])
    new_fx = FI(scopes=[SC("boot", body="b2")])
    fxid = vb.fx("tests/test_a.py::test_a", "db", "session")
    fx_old_deps = CT(vb, funcs=[(conftest, "boot")])
    # Register the fixture id before SNAP freezes the vocabulary.
    index = IDX({
        conftest: PF(conftest, "dc-new", new_fx, support=True),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_a")]), test=True),
        "tests/test_b.py": PF("tests/test_b.py", "dtb",
                               FI(scopes=[SC("test_b")]), test=True),
    })
    n_a = NODE(vb, TEST + "::test_a", TEST, "r1", fixtures=[fxid])
    n_b = NODE(vb, "tests/test_b.py::test_b", "tests/test_b.py", "r1",
               fixtures=[fxid])
    run = RUN(vb, "r1", {conftest: "dc-old", TEST: "dt",
                         "tests/test_b.py": "dtb"})
    deps = SNAP(vb, [run], [n_a, n_b],
                fixtures=[FIX(fxid, "r1", fx_old_deps)])
    inputs = INP(index, deps,
                 versions={"dc-old": old_fx, "dt": FI(scopes=[SC("test_a")]),
                           "dtb": FI(scopes=[SC("test_b")])},
                 changed=(conftest,))
    d = _plan(inputs)
    assert PL.is_selected(d, n_a.nodeid)
    assert PL.is_selected(d, n_b.nodeid)


def test_missing_fixture_record_is_opaque_and_static_hit_selects():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, node_kw={"fixtures": [0]})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)  # static reach hits via LIB itself
    assert dict(d.fallbacks)[nid] == "fixture record missing"


def test_missing_fixture_record_static_miss_skips():
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(other, "g")],
                fixtures=[7])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    # Static reach from LIB cannot prove the test affected: skipped (N3).
    assert not PL.is_selected(d, node.nodeid)


# ---------------------------------------------------------------------------
# Ambient (clause g)
# ---------------------------------------------------------------------------

def test_ambient_function_change_selects_by_static_reach():
    vb = VB()
    old = FI(scopes=[SC("serve", body="b1")])
    new = FI(scopes=[SC("serve", body="b2")])
    ambient = CT(vb, funcs=[(LIB, "serve")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1")  # no direct deps
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"}, ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    # R1/R5: the run's ambient context ran the changed function: the node
    # is deferred as g-shared (0.6.x selected it by clause g). Units only
    # count tests selected to run now.
    _assert_t2(d, node.nodeid, "g-shared", LIB)
    assert units_by_kind(d)["function"] == {LIB + "::serve": 0}


def test_ambient_function_change_reaches_the_run_without_static_import():
    """Code the ambient context ran shaped state every test of the run
    shared; a static import is not required (importlib-loaded routers in
    the persea evaluation)."""
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("serve", body="b1")])
    new = FI(scopes=[SC("serve", body="b2")])
    ambient = CT(vb, funcs=[(LIB, "serve")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt"},
              ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    _assert_t2(d, node.nodeid, "g-shared", LIB)
    assert d.selected == 0
    assert units_by_kind(d) == {"function": {LIB + "::serve": 0}}


def test_ambient_data_change_is_g_data_not_full_reason():
    vb = VB()
    data = "data/weights.bin"
    lib = FI(scopes=[SC("f", body="b1")])
    ambient = CT(vb, data=[data])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-old"},
              ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"d": lib, "dt": FI(scopes=[SC("test_f")])},
                 data_digests={data: "h-new"}, changed=(data,))
    d = _plan(inputs)
    # Spec R6 - the planner never promotes to the full suite any more; every
    # node of the run is deferred as g-data via the changed file.
    assert d.full_reason is None
    _assert_t2(d, node.nodeid, "g-data", data)


def test_ambient_data_unchanged_is_not_deferred():
    vb = VB()
    data = "data/weights.bin"
    lib = FI(scopes=[SC("f", body="b1")])
    ambient = CT(vb, data=[data])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-same"},
              ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"d": lib, "dt": FI(scopes=[SC("test_f")])},
                 data_digests={data: "h-same"}, changed=())
    d = _plan(inputs)
    assert d.full_reason is None
    assert d.tiers == {}
    assert not PL.is_selected(d, node.nodeid)


def test_unreadable_data_digest_is_stale_on_both_sides():
    """N1: '?' (oversize or unreadable) proves nothing, even against '?'.

    Before the fix a '?' baseline equalled a '?' current digest, so a
    change past the read cap selected nothing.
    """
    vb = VB()
    data = "data/weights.bin"
    lib = FI(scopes=[SC("f", body="b1")])
    ambient = CT(vb, data=[data])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "?"},
              ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"d": lib, "dt": FI(scopes=[SC("test_f")])},
                 data_digests={data: "?"}, changed=())
    d = _plan(inputs)
    assert d.full_reason is None
    _assert_t2(d, node.nodeid, "g-data", data)


def test_corrupt_ambient_ids_taint_nodes_opaque():
    vb = VB()
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    ambient = C.ContextDeps(functions=C.selection_ids([999999]),
                            modules=C.selection_ids(()),
                            data=C.selection_ids(()), opaque=False)
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"}, ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    # Unresolvable ambient id: the node is opaque, and with the changed
    # function it ran hit precisely it is T1 d (R3).
    assert d.tiers[node.nodeid] == (T1, "d")
    assert dict(d.fallbacks)[node.nodeid] == "opaque: spawned a process or overflowed"


def test_opaque_ambient_widens_with_static_hit():
    vb = VB()
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    ambient = C.ContextDeps(functions=C.selection_ids(()),
                            modules=C.selection_ids(()),
                            data=C.selection_ids(()), opaque=True)
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"}, ambient=ambient)
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert PL.is_selected(d, node.nodeid)
    assert dict(d.fallbacks)[node.nodeid] == "opaque: spawned a process or overflowed"


# ---------------------------------------------------------------------------
# Clause b: opaque / demoted nodes follow the static rule only (N3)
# ---------------------------------------------------------------------------

def test_opaque_node_selected_on_static_hit():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, node_kw={"opaque": True})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert dict(d.fallbacks)[nid] == "opaque: spawned a process or overflowed"


def test_opaque_node_skipped_on_static_miss():
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", opaque=True)
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


def _spawn_entry_inputs(entry_imports):
    vb = VB()
    entry = "pkg/__main__.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    main = FI(scopes=[SC("main", body="bm")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        entry: PF(entry, "dm", main, imports=entry_imports),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", opaque=True,
                mods=(entry,))
    run = RUN(vb, "r1", {LIB: "do", entry: "dm", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "dm": main,
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    return inputs, node.nodeid


def test_opaque_node_selected_when_its_spawned_entry_reaches_the_change():
    """A test that runs ``python -m pkg`` executes the entry module's
    imports in the child: the recorded entry module counts for the static
    rule. Before the fix only the test file's own imports did (ptest's
    CLI end-to-end tests were missed on planner edits)."""
    inputs, nid = _spawn_entry_inputs((LIB,))
    d = _plan(inputs)
    # T2 b-static (R1/R5): the static rule proves reach, not affectedness.
    _assert_t2(d, nid, "b-static")
    assert dict(d.fallbacks)[nid] == "opaque: spawned a process or overflowed"


def test_opaque_node_skipped_when_its_spawned_entry_misses_the_change():
    inputs, nid = _spawn_entry_inputs(())
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)


def test_demoted_node_selected_on_static_hit():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, test_digest="dt-cur",
                       demotions={TEST + "::test_f": "dt-cur"})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert dict(d.fallbacks)[nid] == "demoted by the selection audit"


def test_demoted_node_skipped_on_static_miss():
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt-cur", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt-cur"})
    deps = SNAP(vb, [run], [node],
                demotions={node.nodeid: "dt-cur"})
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt-cur": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


def test_demotion_clears_when_test_file_digest_changes():
    # The demotion was recorded against dt-old; the file has since been
    # re-recorded (dt-cur), so the precise rule applies again.
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, test_digest="dt-cur",
                       demotions={TEST + "::test_f": "dt-old"})
    d = _plan(inputs)
    assert PL.is_selected(d, nid)  # precise clause d still fires here
    assert nid not in dict(d.fallbacks)


def test_demoted_node_ignores_precise_hit_on_static_miss():
    # (0.6.x pinned that a precise hit on a demoted node is skipped when
    # the static rule misses; R3 reverses it, see the assertion below.)
    # (Static reach from LIB cannot reach TEST here because TEST does not
    # import LIB; the node only ran it through a dynamic import.)
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt-cur", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt-cur"})
    deps = SNAP(vb, [run], [node], demotions={node.nodeid: "dt-cur"})
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt-cur": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    # R3 flips the 0.6.x oracle: running on positive evidence is always
    # safe, so a precise hit on a demoted node is T1 d even when the
    # static rule misses.
    assert d.tiers[node.nodeid] == (T1, "d")
    assert PL.is_selected(d, node.nodeid)
    assert dict(d.fallbacks)[node.nodeid] == "demoted by the selection audit"


# ---------------------------------------------------------------------------
# 4.6 unrecorded test files (N2)
# ---------------------------------------------------------------------------

def test_added_test_file_is_selected_whole():
    # A test file with no baseline entry is an added path: whole-path
    # change selects it whole (4.3), not the 4.6 unrecorded rule.
    lib = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    fresh = "tests/test_fresh.py"
    index = IDX({
        LIB: PF(LIB, "d", lib),
        fresh: PF(fresh, "df", FI(scopes=[SC("test_new")]), test=True),
    })
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "d"})], [])
    inputs = INP(index, deps, versions={"d": lib}, changed=())
    d = _plan(inputs)
    assert fresh in d.whole_files
    assert "unrecorded" not in units_by_kind(d)


def test_removed_test_file_selects_nothing():
    # Deleting a test file selects nothing: the missing path must not
    # appear in files/whole_files (5.8 lists runnable test files), even
    # though the store still holds nodes for it.
    gone = "tests/test_gone.py"
    lib = FI(scopes=[SC("f", body="b1")])
    test_fi = FI(scopes=[SC("test_f")])
    gone_fi = FI(scopes=[SC("test_g")])
    vb = VB()
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", test_fi, test=True),
    })
    node_lib = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    node_gone = NODE(vb, gone + "::test_g", gone, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", gone: "dg-old"})
    deps = SNAP(vb, [run], [node_lib, node_gone])
    inputs = INP(index, deps,
                 versions={"d": lib, "dt": test_fi, "dg-old": gone_fi},
                 changed=(gone,))
    d = _plan(inputs)
    assert d.files == ()
    assert d.whole_files == ()
    assert d.selected == 0
    assert "file" not in units_by_kind(d)


def test_unrecorded_file_in_reach_selected_whole():
    lib = FI(scopes=[SC("f", body="b1")])
    new_lib = FI(scopes=[SC("f", body="b2")])
    vb = VB()
    fresh = "tests/test_fresh.py"
    fresh_fi = FI(scopes=[SC("test_new")])
    index = IDX({
        LIB: PF(LIB, "dn", new_lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
        fresh: PF(fresh, "df", fresh_fi, test=True, imports=(LIB,)),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt", fresh: "df"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": lib, "dt": FI(scopes=[SC("test_f")]),
                           "df": fresh_fi},
                 changed=(LIB,))
    d = _plan(inputs)
    # R1/R5: reached by the static rule only: deferred as u-static.
    _assert_t2(d, fresh, "u-static", LIB)
    assert fresh not in d.files and fresh not in d.whole_files
    assert units_by_kind(d)["unrecorded"] == {fresh: 0}
    assert "file" not in units_by_kind(d)


def test_unrecorded_file_outside_reach_skipped():
    lib = FI(scopes=[SC("f", body="b1")])
    new_lib = FI(scopes=[SC("f", body="b2")])
    vb = VB()
    far = "tests/test_far.py"
    far_fi = FI(scopes=[SC("test_new")])
    index = IDX({
        LIB: PF(LIB, "dn", new_lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(LIB,)),
        far: PF(far, "df", far_fi, test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt", far: "df"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": lib, "dt": FI(scopes=[SC("test_f")]),
                           "df": far_fi},
                 changed=(LIB,))
    d = _plan(inputs)
    assert far not in d.files


def test_non_python_change_selects_unrecorded_files():
    lib = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    fresh = "tests/test_fresh.py"
    fresh_fi = FI(scopes=[SC("test_new")])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        fresh: PF(fresh, "df", fresh_fi, test=True),
    })
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "d", fresh: "df"})], [])
    inputs = INP(index, deps, versions={"d": lib, "df": fresh_fi},
                 changed=("data/config.json",))
    d = _plan(inputs)
    # R1/R5: a recorded-baseline file with no usable node, reached only
    # by a non-Python change, is deferred as u-static (0.6.x ran it).
    _assert_t2(d, fresh, "u-static", "data/config.json")
    assert fresh not in d.files
    assert units_by_kind(d)["unrecorded"] == {fresh: 0}


def test_unknown_node_ids_run():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    # A brand-new parametrized id in a selected file is not in deselect.
    assert PL.is_selected(d, TEST + "::test_f[new-param-1]")


# ---------------------------------------------------------------------------
# Clause a: non-passing outcomes always run (N4)
# ---------------------------------------------------------------------------

def test_failed_outcome_always_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="failed")
    d = _plan(inputs)
    assert PL.is_selected(d, nid)
    assert d.files == (TEST,)
    assert d.units == ()


def test_error_outcome_always_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="error")
    d = _plan(inputs)
    assert PL.is_selected(d, nid)


def test_passing_outcome_not_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="passed")
    d = _plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.files == ()


# ---------------------------------------------------------------------------
# 4.7 reach: support files (other than conftest.py) widen to their root
# ---------------------------------------------------------------------------

def test_support_file_change_selects_tests_under_its_root():
    vb = VB()
    helper = "tests/helpers.py"
    lib = FI(scopes=[SC("f", body="b1")])
    old_h = FI(scopes=[SC("h", body="b1")])
    new_h = FI(scopes=[SC("h", body="b2")])
    index = IDX({
        helper: PF(helper, "dh-new", new_h, support=True),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
        "other/test_x.py": PF("other/test_x.py", "dx",
                              FI(scopes=[SC("test_x")]), test=True),
    })
    deps = SNAP(vb, [RUN(vb, "r1", {helper: "dh-old", TEST: "dt",
                                    "other/test_x.py": "dx"})], [])
    inputs = INP(index, deps, versions={"dh-old": old_h,
                                       "dt": FI(scopes=[SC("test_f")]),
                                       "dx": FI(scopes=[SC("test_x")])},
                 changed=(helper,), test_roots=("tests", "other"))
    d = _plan(inputs)
    # TEST has no usable node and is under tests/ with the helper: it is
    # reached by the support-root rule, so it is deferred as u-static.
    _assert_t2(d, TEST, "u-static", helper)
    assert TEST not in d.files
    assert "other/test_x.py" not in d.files
    assert "other/test_x.py" not in d.tiers


def test_conftest_change_does_not_widen_by_root():
    vb = VB()
    conftest = "tests/conftest.py"
    old_c = FI(scopes=[SC("h", body="b1")])
    new_c = FI(scopes=[SC("h", body="b2")])
    index = IDX({
        conftest: PF(conftest, "dc-new", new_c, support=True),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    }, reverse={})
    deps = SNAP(vb, [RUN(vb, "r1", {conftest: "dc-old", TEST: "dt"})], [])
    inputs = INP(index, deps, versions={"dc-old": old_c,
                                       "dt": FI(scopes=[SC("test_f")])},
                 changed=(conftest,), test_roots=("tests",))
    d = _plan(inputs)
    # No reverse edge and no root widening for conftest.py: nothing runs.
    assert TEST not in d.files


# ---------------------------------------------------------------------------
# 4.8 output shape: grouping, reached, coverage, ordering
# ---------------------------------------------------------------------------

def test_files_group_nodes_and_deselect_only_partial_files():
    old = FI(scopes=[SC("f", body="b1"), SC("g", body="g1")])
    new = FI(scopes=[SC("f", body="b2"), SC("g", body="g1")])
    vb = VB()
    other_test = "tests/test_other.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f"), SC("test_g")]),
                 test=True, imports=(LIB,)),
        other_test: PF(other_test, "dto",
                        FI(scopes=[SC("test_h")]), test=True),
    })
    n_f = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
    n_g = NODE(vb, TEST + "::test_g", TEST, "r1", funcs=[(LIB, "g")])
    n_h = NODE(vb, other_test + "::test_h", other_test, "r1",
               funcs=[(LIB, "g")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt", other_test: "dto"})
    deps = SNAP(vb, [run], [n_f, n_g, n_h])
    inputs = INP(index, deps,
                 versions={"do": old, "dt": FI(scopes=[SC("test_f"),
                                                       SC("test_g")]),
                           "dto": FI(scopes=[SC("test_h")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert d.files == (TEST,)
    assert d.whole_files == ()
    assert d.deselect == (n_g.nodeid,)
    assert d.selected == 1
    assert d.recorded == 3
    assert d.total_files == 2
    assert d.coverage == pytest.approx(1.0)
    assert PL.is_selected(d, n_f.nodeid)
    assert not PL.is_selected(d, n_g.nodeid)
    assert not PL.is_selected(d, n_h.nodeid)


def test_reached_counts_already_passed_on_this_code():
    old = FI(scopes=[SC("f", body="b1"), SC("g", body="g1")])
    new = FI(scopes=[SC("f", body="b1"), SC("g", body="g2")])
    vb = VB()
    other = "pkg/other.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    # Opaque nodes decided by the static rule still count as reached when
    # their recorded deps touch the change without a precise hit (a
    # recorded path whose other function changed: R3's T1 does not apply).
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")],
                opaque=True)
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = _plan(inputs)
    assert not PL.is_selected(d, node.nodeid)
    assert d.reached == 1
    assert d.recorded == 1


def test_units_sorted_by_count_then_label():
    old = FI(scopes=[SC("f", body="b1"), SC("g", body="g1")],
             stmts=[ST("assign", bound=("A",), fp="a1"),
                    ST("assign", bound=("B",), fp="b1")])
    new = FI(scopes=[SC("f", body="b2"), SC("g", body="g2")],
             stmts=[ST("assign", bound=("A",), fp="a2"),
                    ST("assign", bound=("B",), fp="b2")])
    vb = VB()
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", FI(scopes=[SC("t1"), SC("t2")]), test=True,
                 imports=(LIB,)),
    })
    n1 = NODE(vb, TEST + "::t1", TEST, "r1", funcs=[(LIB, "f")])
    n2 = NODE(vb, TEST + "::t2", TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    deps = SNAP(vb, [run], [n1, n2])
    inputs = INP(index, deps,
                 versions={"do": old,
                           "dt": FI(scopes=[SC("t1"), SC("t2")])},
                 changed=(LIB,))
    d = _plan(inputs)
    kinds = [(u.kind, u.label, u.tests) for u in d.units]
    assert ("function", LIB + "::f", 2) in kinds
    counts = [u.tests for u in d.units]
    assert counts == sorted(counts, reverse=True)
    # Ties break by label.
    tied = [u.label for u in d.units if u.tests == 0]
    assert tied == sorted(tied)


def test_incompatible_run_nodes_use_static_widening_reason():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, run_compat=OTHER)
    d = _plan(inputs)
    # The node is unusable and its file follows the static rule (4.6, R8):
    # reached, so it is deferred as u-static, never a full run.
    _assert_t2(d, TEST, "u-static", LIB)
    assert not PL.is_selected(d, nid)
    assert d.whole_files == ()
    assert d.recorded == 0
    assert dict(d.fallbacks)[nid] == "record from another configuration"


def test_data_deps_select_and_form_units():
    vb = VB()
    data = "data/config.json"
    lib = FI(scopes=[SC("f", body="b1")])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")],
                data=[data])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-old"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"d": lib, "dt": FI(scopes=[SC("test_f")])},
                 data_digests={data: "h-new"}, changed=(data,))
    d = _plan(inputs)
    assert PL.is_selected(d, node.nodeid)
    assert units_by_kind(d)["data"] == {data: 1}


def test_data_dep_unchanged_selects_nothing():
    vb = VB()
    data = "data/config.json"
    lib = FI(scopes=[SC("f", body="b1")])
    index = IDX({
        LIB: PF(LIB, "d", lib),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")],
                data=[data])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-same"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"d": lib, "dt": FI(scopes=[SC("test_f")])},
                 data_digests={data: "h-same"}, changed=())
    d = _plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


# ---------------------------------------------------------------------------
# needed_versions / needed_data_paths / is_selected / audit_misses
# ---------------------------------------------------------------------------

def test_needed_versions_returns_stale_py_digests():
    vb = VB()
    test_fi = FI(scopes=[SC("test_f")])
    lib = FI(scopes=[SC("f", body="b-cur")])
    index = IDX({
        LIB: PF(LIB, "d-cur", lib),
        TEST: PF(TEST, "dt", test_fi, test=True),
        "data/x.json": PF("data/x.json", "hx", FI()),
    })
    run = RUN(vb, "r1", {LIB: "d-old", TEST: "dt", "data/x.json": "hx-old"})
    other_run = RUN(vb, "r2", {LIB: "d-cur", TEST: "dt"}, compat=OTHER)
    deps = SNAP(vb, [run, other_run], [])
    assert PL.needed_versions(index, deps, K) == frozenset({"d-old"})
    assert PL.needed_versions(index, deps, OTHER) == frozenset()
    assert PL.needed_versions(index, deps, "nope") == frozenset()


def test_needed_data_paths_returns_recorded_data_only():
    vb = VB()
    run = RUN(vb, "r1", {LIB: "d", "data/a.json": "h1", "data/b.json": "h2"})
    other = RUN(vb, "r2", {LIB: "d", "data/c.json": "h3"}, compat=OTHER)
    deps = SNAP(vb, [run, other], [])
    index = IDX({LIB: PF(LIB, "d", FI())})
    assert PL.needed_data_paths(deps, K) == frozenset({"data/a.json",
                                                       "data/b.json"})
    assert PL.needed_data_paths(deps, OTHER) == frozenset({"data/c.json"})


def test_is_selected_ignores_the_deprecated_full_reason():
    # Spec R6 - full_reason is a deprecated field that nothing sets or reads;
    # a decision carrying one selects exactly its files minus deselect.
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new)
    d = _plan(inputs)
    assert d.full_reason is None
    stale = C.SelectionDecision(
        full_reason="ambient data file data/x.bin changed", files=(),
        deselect=(), whole_files=(), selected=0, reached=0, recorded=0,
        total_files=0, units=(), fallbacks=(), coverage=0.0,
    )
    assert not PL.is_selected(stale, "anything/at_all.py::test_x")
    assert PL.is_selected(d, nid)
    assert not PL.is_selected(d, "tests/other.py::test_y")


def test_audit_misses_flags_unselected_previously_passing_failures():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    vb = VB()
    other = "pkg/other.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
        "tests/test_lib2.py": PF("tests/test_lib2.py", "dt2",
                                 FI(scopes=[SC("test_q")]), test=True,
                                 imports=(LIB,)),
    })
    n_sel = NODE(vb, "tests/test_lib2.py::test_q", "tests/test_lib2.py",
                 "r1", funcs=[(LIB, "f")])
    n_miss = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt",
                         "tests/test_lib2.py": "dt2"})
    deps = SNAP(vb, [run], [n_sel, n_miss])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")]),
                           "dt2": FI(scopes=[SC("test_q")])},
                 changed=frozenset())
    d = _plan(inputs)
    assert PL.is_selected(d, n_sel.nodeid)
    assert not PL.is_selected(d, n_miss.nodeid)
    # The unselected node has a passing prior record but failed: a miss.
    assert PL.audit_misses(d, deps, [n_miss.nodeid]) == (n_miss.nodeid,)
    # The selected node is not a miss even though it also failed.
    assert PL.audit_misses(d, deps, [n_sel.nodeid]) == ()


def test_audit_misses_ignores_previously_failing_and_selected():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, outcome="failed")
    d = _plan(inputs)
    deps = inputs.deps
    # Previously failed records are always selected (clause a): no miss.
    assert PL.audit_misses(d, deps, [nid]) == ()
    assert PL.audit_misses(d, deps, ["tests/ghost.py::test_no"]) == ()


def test_empty_snapshot_plans_empty_decision():
    vb = VB()
    index = IDX({})
    deps = SNAP(vb, [], [])
    d = _plan(INP(index, deps, changed=()))
    assert d.files == ()
    assert d.deselect == ()
    assert d.selected == 0 and d.recorded == 0 and d.reached == 0
    assert d.total_files == 0 and d.coverage == 0.0
    assert d.units == () and d.fallbacks == ()
    assert d.full_reason is None
    assert PL.audit_misses(d, deps, ["tests/x.py::t"]) == ()


def test_determinism_identical_inputs_identical_output():
    old = FI(scopes=[SC("f", body="b1"), SC("g", body="g1")],
             stmts=[ST("assign", bound=("A",), fp="a1")])
    new = FI(scopes=[SC("f", body="b2"), SC("g", body="g2")],
             stmts=[ST("assign", bound=("A",), fp="a2")])
    inputs_a, _ = pair(old, new)
    inputs_b, _ = pair(old, new)
    assert _plan(inputs_a) == _plan(inputs_b)


@pytest.mark.skipif(importlib.util.find_spec("ptest.source_index") is None,
                    reason="awaiting T1")
class TestWithSourceIndex:
    """Realistic cover: FileIndex fragments from the real T1 indexer."""

    def test_body_change_selects_through_real_index(self):
        from ptest import source_index as SI

        old_raw = b"def f():\n    return 1\n"
        new_raw = b"def f():\n    return 2\n"
        old_idx = SI.index_source(old_raw)
        new_idx = SI.index_source(new_raw)
        assert old_idx.parsed and new_idx.parsed
        test_raw = b"def test_f():\n    pass\n"
        test_idx = SI.index_source(test_raw)
        assert test_idx.parsed
        vb = VB()
        index = IDX({
            LIB: PF(LIB, "d-new", new_idx),
            TEST: PF(TEST, "dt", test_idx, test=True, imports=(LIB,)),
        })
        node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")])
        run = RUN(vb, "r1", {LIB: "d-old", TEST: "dt"})
        deps = SNAP(vb, [run], [node])
        inputs = INP(index, deps,
                     versions={"d-old": old_idx, "dt": test_idx},
                     changed=(LIB,))
        d = _plan(inputs)
        assert PL.is_selected(d, node.nodeid)


def test_changed_default_selects_top_level_function_caller():
    """N1: a def whose skeleton refs a changed name is AF on itself.

    TIMEOUT 5->6 changes only the assign fingerprint, so NAMES gains
    pkg/lib.py:TIMEOUT. The `def fetch(timeout=TIMEOUT)` statement refs
    match; propagation must mark (pkg/lib.py, fetch) affected or the
    recorded caller is skipped. Failed before the fix: only the bound
    name propagated, never AF on the function.
    """
    timeout = key(LIB, "TIMEOUT")
    fetch = key(LIB, "fetch")
    old = FI(scopes=[SC("fetch")],
             stmts=[ST("assign", bound=("TIMEOUT",), fp="t1"),
                    ST("def", bound=("fetch",), refs=[("TIMEOUT",)],
                       fp="f1")])
    new = FI(scopes=[SC("fetch")],
             stmts=[ST("assign", bound=("TIMEOUT",), fp="t2"),
                    ST("def", bound=("fetch",), refs=[("TIMEOUT",)],
                       fp="f1")])
    inputs, nid = pair(
        old, new,
        node_kw={"funcs": [(LIB, "fetch")]},
        lib_stmt_refs=[frozenset(), {timeout}],
        lib_stmt_bound=[{timeout}, {fetch}])
    d = _plan(inputs)
    assert PL.is_selected(d, nid)


def test_changed_default_selects_method_caller():
    """N1: a method default referencing a changed constant selects callers.

    The class refs carry the method-skeleton chain (TIMEOUT) and the
    Client.get scope refs match, so (pkg/lib.py, Client.get) lands in
    CF/AF and the recorded `client.Client().get()` caller is selected.
    """
    timeout = key(LIB, "TIMEOUT")
    old = FI(scopes=[SC("Client.get")], classes=[CL("Client")],
             stmts=[ST("assign", bound=("TIMEOUT",), fp="t1")])
    new = FI(scopes=[SC("Client.get")], classes=[CL("Client")],
             stmts=[ST("assign", bound=("TIMEOUT",), fp="t2")])
    inputs, nid = pair(
        old, new,
        node_kw={"funcs": [(LIB, "Client.get")]},
        lib_refs={"Client.get": {timeout}},
        lib_class_refs={"Client": {timeout}},
        lib_stmt_refs=[frozenset()],
        lib_stmt_bound=[{timeout}])
    d = _plan(inputs)
    assert PL.is_selected(d, nid)



# --- import-time changes reach importers (N1, persea m0003) -------------
# The recorded test never executed the changed code: it only imported the
# module (here: called ``other``). Each import-time change must still
# select it through the static rule; each body-only twin must not.

def _imported_only(old, new, **kw):
    return pair(old, new, node_kw={"funcs": [(LIB, "other")]}, **kw)


def test_skeleton_change_reaches_importers():
    old = FI(scopes=[SC("f", skel="s1"), SC("other")])
    new = FI(scopes=[SC("f", skel="s2"), SC("other")])
    inputs, nid = _imported_only(old, new)
    d = _plan(inputs)
    # R1/R5: import-time change reaching an importer: deferred (g-reach).
    _assert_t2(d, nid, "g-reach")


def test_body_only_change_does_not_reach_importers():
    old = FI(scopes=[SC("f", body="b1"), SC("other")])
    new = FI(scopes=[SC("f", body="b2"), SC("other")])
    inputs, nid = _imported_only(old, new)
    assert not PL.is_selected(_plan(inputs), nid)


def test_name_used_in_an_annotation_reaches_importers():
    """``Manage = Annotated[..., Depends(...)]`` read by FastAPI at import:
    tests rejected inside the dependency never run the route body."""
    old_stmts = [ST("assign", bound=("Manage",), fp="m1"),
                 ST("def", bound=("route",), fp="r1", refs=(("Manage",),))]
    new_stmts = [ST("assign", bound=("Manage",), fp="m2"),
                 ST("def", bound=("route",), fp="r1", refs=(("Manage",),))]
    old = FI(scopes=[SC("route"), SC("other")], stmts=old_stmts)
    new = FI(scopes=[SC("route"), SC("other")], stmts=new_stmts)
    inputs, nid = _imported_only(
        old, new, lib_stmt_refs=[(), (LIB + ":Manage",)],
        lib_stmt_bound=[(LIB + ":Manage",), (LIB + ":route",)])
    d = _plan(inputs)
    # R1/R5: import-time change reaching an importer: deferred (g-reach).
    _assert_t2(d, nid, "g-reach")


def test_name_used_only_in_a_body_stays_precise():
    old_stmts = [ST("assign", bound=("LIMIT",), fp="m1")]
    new_stmts = [ST("assign", bound=("LIMIT",), fp="m2")]
    old = FI(scopes=[SC("f", refs=(("LIMIT",),)), SC("other")],
             stmts=old_stmts)
    new = FI(scopes=[SC("f", refs=(("LIMIT",),)), SC("other")],
             stmts=new_stmts)
    inputs, nid = _imported_only(
        old, new, lib_refs={"f": (LIB + ":LIMIT",)},
        lib_stmt_refs=[()], lib_stmt_bound=[(LIB + ":LIMIT",)])
    assert not PL.is_selected(_plan(inputs), nid)


def test_new_decorated_function_reaches_importers():
    old = FI(scopes=[SC("other")])
    new = FI(scopes=[SC("other"), SC("route", decorated=True)])
    inputs, nid = _imported_only(old, new)
    d = _plan(inputs)
    # R1/R5: import-time change reaching an importer: deferred (g-reach).
    _assert_t2(d, nid, "g-reach")


def test_new_method_reaches_importers():
    old = FI(classes=[CL("C")], scopes=[SC("other")])
    new = FI(classes=[CL("C")], scopes=[SC("other"), SC("C.__eq__")])
    inputs, nid = _imported_only(old, new)
    d = _plan(inputs)
    # R1/R5: import-time change reaching an importer: deferred (g-reach).
    _assert_t2(d, nid, "g-reach")


def test_class_body_change_reaches_importers():
    old = FI(classes=[CL("Model", body="c1")], scopes=[SC("other")])
    new = FI(classes=[CL("Model", body="c2")], scopes=[SC("other")])
    inputs, nid = _imported_only(old, new)
    d = _plan(inputs)
    # R1/R5: import-time change reaching an importer: deferred (g-reach).
    _assert_t2(d, nid, "g-reach")


def _ambient_case(old_lib, new_lib, *, ambient_mods=(), ambient_funcs=()):
    """The test never imports LIB statically and never ran it; the run's
    ambient context (collection, an importlib-loaded router) did."""
    vb = VB()
    test_fi = FI(scopes=[SC("test_f")])
    index = IDX({
        LIB: PF(LIB, "dn", new_lib),
        TEST: PF(TEST, "dt", test_fi, test=True, imports=()),
    })
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(TEST, "test_f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"},
              ambient=CT(vb, funcs=ambient_funcs, mods=ambient_mods))
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"do": old_lib, "dt": test_fi},
                 changed=(LIB,))
    return inputs, node.nodeid


def test_import_time_change_in_an_ambient_module_reaches_the_run():
    old = FI(scopes=[SC("route", skel="s1", decorated=True)])
    new = FI(scopes=[SC("route", skel="s2", decorated=True)])
    inputs, nid = _ambient_case(old, new, ambient_mods=[LIB])
    d = _plan(inputs)
    _assert_t2(d, nid, "g-shared", LIB)
    assert units_by_kind(d)["module"] == {LIB: 0}


def test_import_time_change_outside_the_ambient_does_not():
    old = FI(scopes=[SC("route", skel="s1", decorated=True)])
    new = FI(scopes=[SC("route", skel="s2", decorated=True)])
    inputs, nid = _ambient_case(old, new, ambient_mods=())
    assert not PL.is_selected(_plan(inputs), nid)


def test_changed_function_the_ambient_ran_reaches_the_run():
    old = FI(scopes=[SC("create_app", body="b1")])
    new = FI(scopes=[SC("create_app", body="b2")])
    inputs, nid = _ambient_case(old, new, ambient_funcs=[(LIB, "create_app")])
    _assert_t2(_plan(inputs), nid, "g-shared", LIB)


def test_body_change_of_an_ambient_module_function_not_run_stays_precise():
    old = FI(scopes=[SC("handler", body="b1"), SC("create_app")])
    new = FI(scopes=[SC("handler", body="b2"), SC("create_app")])
    inputs, nid = _ambient_case(old, new, ambient_mods=[LIB],
                                ambient_funcs=[(LIB, "create_app")])
    assert not PL.is_selected(_plan(inputs), nid)


# --- caller-local import-time edits stay precise (persea default mutants) --
# A default of a plain function or an attribute of a plain class reaches
# only code that runs it: a test that merely imported the module skips,
# as long as some selected test still imports it (an edit that breaks the
# import fails there).

def _caller_and_importer(old, new, *, caller=(LIB, "f"), importer=True,
                         ambient_mods=(), lib_refs=None, lib_stmt_refs=None,
                         lib_stmt_bound=None):
    """One test runs ``caller``; another only ran ``other`` in LIB."""
    vb = VB()
    test_fi = FI(scopes=[SC("test_f"), SC("test_other")])
    index = IDX({
        LIB: PF(LIB, "dn", new, scope_refs=lib_refs,
                stmt_refs=lib_stmt_refs, stmt_bound=lib_stmt_bound),
        TEST: PF(TEST, "dt", test_fi, test=True, imports=(LIB,)),
    })
    nodes = []
    if caller is not None:
        nodes.append(NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[caller]))
    if importer:
        nodes.append(NODE(vb, TEST + "::test_other", TEST, "r1",
                          funcs=[(LIB, "other")]))
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"},
              ambient=CT(vb, mods=ambient_mods))
    deps = SNAP(vb, [run], nodes)
    inputs = INP(index, deps, versions={"do": old, "dt": test_fi},
                 changed=(LIB,))
    return _plan(inputs)


CALLER = TEST + "::test_f"
IMPORTER = TEST + "::test_other"


def _default_edit(local, *, decorated=False):
    old = FI(scopes=[SC("f", skel="s1", local=local, decorated=decorated),
                     SC("other")])
    new = FI(scopes=[SC("f", skel="s2", local=local, decorated=decorated),
                     SC("other")])
    return old, new


def test_plain_default_edit_skips_tests_that_only_imported_it():
    d = _caller_and_importer(*_default_edit(True))

    assert PL.is_selected(d, CALLER)
    assert not PL.is_selected(d, IMPORTER)


def test_plain_default_edit_in_an_ambient_module_stays_precise():
    d = _caller_and_importer(*_default_edit(True), ambient_mods=[LIB])

    assert PL.is_selected(d, CALLER)
    assert not PL.is_selected(d, IMPORTER)


def test_plain_default_edit_no_test_runs_still_runs_an_importer():
    """Nothing runs ``f``: only an importer can show that the new default
    breaks the import (e.g. an undefined name)."""
    d = _caller_and_importer(*_default_edit(True), caller=None)

    assert PL.is_selected(d, IMPORTER)


def test_plain_default_edit_no_test_runs_reaches_the_ambient_run():
    d = _caller_and_importer(*_default_edit(True), caller=None,
                             ambient_mods=[LIB])

    assert PL.is_selected(d, IMPORTER)


def test_computed_default_edit_in_an_ambient_module_reaches_the_run():
    d = _caller_and_importer(*_default_edit(False), ambient_mods=[LIB])

    # R1/R5: the run's ambient imported the changed module: deferred.
    _assert_t2(d, IMPORTER, "g-shared")


def test_registered_default_edit_in_an_ambient_module_reaches_the_run():
    d = _caller_and_importer(*_default_edit(False, decorated=True),
                             ambient_mods=[LIB])

    # R1/R5: the run's ambient imported the changed module: deferred.
    _assert_t2(d, IMPORTER, "g-shared")


def _method_edit(method):
    """Plain class C with an edited method default; ``route`` (registered)
    names C in its signature, as a FastAPI dependency would."""
    stmts = [ST("class", bound=("C",), fp="c"),
             ST("def", bound=("route",), refs=(("C",),), fp="r")]
    old = FI(classes=[CL("C", plain=True)],
             scopes=[SC(method, skel="s1", local=True),
                     SC("route", decorated=True), SC("other")],
             stmts=stmts)
    new = FI(classes=[CL("C", plain=True)],
             scopes=[SC(method, skel="s2", local=True),
                     SC("route", decorated=True), SC("other")],
             stmts=stmts)
    return _caller_and_importer(
        old, new, caller=(LIB, method), ambient_mods=[LIB],
        lib_stmt_refs=[(), (key(LIB, "C"),)],
        lib_stmt_bound=[(key(LIB, "C"),), (key(LIB, "route"),)])


def test_plain_method_default_edit_does_not_reach_class_users():
    d = _method_edit("C.get")

    assert PL.is_selected(d, CALLER)
    assert not PL.is_selected(d, IMPORTER)


def test_constructor_default_edit_reaches_signature_readers():
    """A framework reads ``C.__init__`` when C is a dependency."""
    d = _method_edit("C.__init__")

    # R1/R5: the run's ambient imported the changed module: deferred.
    _assert_t2(d, IMPORTER, "g-shared")


def _class_edit(plain):
    old = FI(classes=[CL("Config", body="c1", plain=plain)],
             scopes=[SC("use", refs=(("Config",),)), SC("other")],
             stmts=[ST("class", bound=("Config",), fp="k")])
    new = FI(classes=[CL("Config", body="c2", plain=plain)],
             scopes=[SC("use", refs=(("Config",),)), SC("other")],
             stmts=[ST("class", bound=("Config",), fp="k")])
    return _caller_and_importer(
        old, new, caller=(LIB, "use"), ambient_mods=[LIB],
        lib_refs={"use": (key(LIB, "Config"),)},
        lib_stmt_refs=[()], lib_stmt_bound=[(key(LIB, "Config"),)])


def test_plain_class_attribute_edit_selects_only_its_users():
    d = _class_edit(True)

    assert PL.is_selected(d, CALLER)
    assert not PL.is_selected(d, IMPORTER)


def test_framework_class_attribute_edit_reaches_the_run():
    d = _class_edit(False)

    # R1/R5: the run's ambient imported the changed module: deferred.
    _assert_t2(d, IMPORTER, "g-shared")


def _constant_into_default(local):
    stmts_old = [ST("assign", bound=("TIMEOUT",), fp="t1"),
                 ST("def", bound=("fetch",), refs=(("TIMEOUT",),), fp="f")]
    stmts_new = [ST("assign", bound=("TIMEOUT",), fp="t2"),
                 ST("def", bound=("fetch",), refs=(("TIMEOUT",),), fp="f")]
    scopes = [SC("fetch", local=local, decorated=not local), SC("other")]
    return _caller_and_importer(
        FI(scopes=scopes, stmts=stmts_old), FI(scopes=scopes, stmts=stmts_new),
        caller=(LIB, "fetch"), ambient_mods=[LIB],
        lib_stmt_refs=[(), (key(LIB, "TIMEOUT"),)],
        lib_stmt_bound=[(key(LIB, "TIMEOUT"),), (key(LIB, "fetch"),)])


def test_constant_feeding_a_plain_default_stays_precise():
    d = _constant_into_default(True)

    assert PL.is_selected(d, CALLER)
    assert not PL.is_selected(d, IMPORTER)


def test_constant_feeding_a_registered_signature_reaches_the_run():
    d = _constant_into_default(False)

    # R1/R5: the run's ambient imported the changed module: deferred.
    _assert_t2(d, IMPORTER, "g-shared")


# ===========================================================================
# 0.7.0 evidence tiers (R1-R4, R9, R16, R22, R23) and the audit classifier
# ===========================================================================

NID = TEST + "::test_f"
FRESH_TEST = "tests/test_fresh.py"


def tier(d, key_):
    return d.tiers.get(key_)


def _dep_case(*, body=("b1", "b2"), node_kw=None, test_imports=(LIB,),
              ambient=None, changed=(LIB,), **kw):
    """LIB.f body changes; one node in TEST; optional ambient context."""
    old = FI(scopes=[SC("f", body=body[0])])
    new = FI(scopes=[SC("f", body=body[1])])
    vb = VB()
    test_fi = FI(scopes=[SC("test_f")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        TEST: PF(TEST, "dt", test_fi, test=True, imports=test_imports),
    })
    node = NODE(vb, NID, TEST, "r1", **(node_kw or {}))
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"},
              ambient=(ambient(vb) if ambient else None))
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps, versions={"do": old, "dt": test_fi},
                 changed=changed, **kw)
    return inputs


# --- T1 codes -------------------------------------------------------------

def test_code_a_non_passing_chosen_record():
    old = FI(scopes=[SC("f")])
    inputs, nid = pair(old, old, outcome="failed", lib_digests=("d", "d"),
                       changed=())
    assert tier(_plan(inputs), nid) == (T1, "a")


def test_code_c_changed_test_file():
    lib = FI(scopes=[SC("f")])
    old_t = FI(scopes=[SC("test_f")],
               stmts=[ST("assign", bound=("FLAG",), fp="a1")])
    new_t = FI(scopes=[SC("test_f")],
               stmts=[ST("assign", bound=("FLAG",), fp="a2")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    assert tier(_plan(inputs), nid) == (T1, "c")


def test_code_d_executed_changed_function():
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]})
    assert tier(_plan(inputs), NID) == (T1, "d")


def test_code_f_changed_data_file_read():
    vb = VB()
    data = "data/x.json"
    lib = FI(scopes=[SC("f")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d", lib),
                 TEST: PF(TEST, "dt", tfi, test=True)})
    node = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")], data=[data])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-old"})
    inputs = INP(index, SNAP(vb, [run], [node]),
                 versions={"d": lib, "dt": tfi},
                 data_digests={data: "h-new"}, changed=(data,))
    assert tier(_plan(inputs), NID) == (T1, "f")


def _skeleton_case(**kw):
    """LIB.g (not run by the test) changes skeleton; the test imported
    LIB (module context) but only ran the unchanged ``f``."""
    old = FI(scopes=[SC("f"), SC("g", skel="s1")])
    new = FI(scopes=[SC("f"), SC("g", skel="s2")])
    vb = VB()
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "dn", new),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    node = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")], mods=[LIB])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    return INP(index, SNAP(vb, [run], [node]),
               versions={"do": old, "dt": tfi}, changed=(LIB,), **kw)


def test_code_e_test_context_imported_skeleton_changed_module():
    assert tier(_plan(_skeleton_case()), NID) == (T1, "e")


def test_code_n_new_test_file_is_a_whole_t1_key_weighted_by_inventory():
    lib = FI(scopes=[SC("f")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "d", lib),
                 FRESH_TEST: PF(FRESH_TEST, "df",
                                FI(scopes=[SC("test_a")]), test=True)})
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "d"})], [],
                inventory=[FRESH_TEST + "::test_a", FRESH_TEST + "::test_b",
                           "tests/other.py::test_x"])
    d = _plan(INP(index, deps, versions={"d": lib}, changed=()))
    assert tier(d, FRESH_TEST) == (T1, "n")
    assert FRESH_TEST in d.whole_files and FRESH_TEST in d.files
    assert d.weights == {FRESH_TEST: 2}


def test_file_key_weight_is_at_least_one_without_inventory():
    lib = FI(scopes=[SC("f")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "d", lib),
                 FRESH_TEST: PF(FRESH_TEST, "df",
                                FI(scopes=[SC("test_a")]), test=True)})
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "d"})], [])
    d = _plan(INP(index, deps, versions={"d": lib}, changed=()))
    assert d.weights == {FRESH_TEST: 1}


# --- T2 codes (R1 order, R5 never selected, via) ------------------------------

def _route_ambient(vb, mods=(), funcs=()):
    return CT(vb, mods=mods, funcs=funcs)


def _ambient_inputs(*, old_lib, new_lib, ambient_mods=(), ambient_funcs=(),
                    test_imports=(), observers=None, conditional=False,
                    changed=(LIB,), since_gate=()):
    vb = VB()
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "dn", new_lib),
                 TEST: PF(TEST, "dt", tfi, test=True,
                          imports=test_imports)})
    node = NODE(vb, NID, TEST, "r1", funcs=[(TEST, "test_f")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"},
              ambient=CT(vb, funcs=ambient_funcs, mods=ambient_mods))
    inputs = INP(index, SNAP(vb, [run], [node]),
                 versions={"do": old_lib, "dt": tfi}, changed=changed,
                 observers=observers, conditional=conditional,
                 since_gate=since_gate)
    return inputs


ROUTE_OLD = FI(scopes=[SC("route", skel="s1", decorated=True)])
ROUTE_NEW = FI(scopes=[SC("route", skel="s2", decorated=True)])


def test_code_g_shared_ambient_module_and_via_drops_qualname():
    d = _plan(_ambient_inputs(old_lib=ROUTE_OLD, new_lib=ROUTE_NEW,
                              ambient_mods=[LIB]))
    assert tier(d, NID) == (T2, "g-shared")
    assert d.via[NID] == LIB
    assert not PL.is_selected(d, NID)
    assert d.files == ()


def test_code_g_shared_ambient_function_via_is_the_path():
    old = FI(scopes=[SC("create_app", body="b1")])
    new = FI(scopes=[SC("create_app", body="b2")])
    d = _plan(_ambient_inputs(old_lib=old, new_lib=new,
                              ambient_funcs=[(LIB, "create_app")]))
    assert tier(d, NID) == (T2, "g-shared")
    assert d.via[NID] == LIB


def test_code_g_reach_static_reach_of_an_ambient_change():
    d = _plan(_ambient_inputs(old_lib=ROUTE_OLD, new_lib=ROUTE_NEW,
                              test_imports=(LIB,)))
    assert tier(d, NID) == (T2, "g-reach")
    assert d.via[NID] == LIB


def test_code_g_data_ambient_data_change_replaces_full_reason():
    vb = VB()
    data = "data/weights.bin"
    lib = FI(scopes=[SC("f", body="b1")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d", lib),
                 TEST: PF(TEST, "dt", tfi, test=True)})
    node = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")])
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt", data: "h-old"},
              ambient=CT(vb, data=[data]))
    d = _plan(INP(index, SNAP(vb, [run], [node]),
                  versions={"d": lib, "dt": tfi},
                  data_digests={data: "h-new"}, changed=(data,)))
    assert d.full_reason is None
    assert tier(d, NID) == (T2, "g-data")
    assert d.via[NID] == data
    assert not PL.is_selected(d, NID)


def test_r1_t1_beats_t2_and_codes_follow_the_documented_order():
    # Clause d (T1) beats the g-shared that also holds for this node.
    def amb(vb):
        return CT(vb, funcs=[(LIB, "f")])
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]}, ambient=amb)
    assert tier(_plan(inputs), NID) == (T1, "d")
    # Clause f (data) beats clause e (skeleton) when both hold.
    case = _skeleton_case()
    assert tier(_plan(case), NID) == (T1, "e")


def test_f_precedes_e_when_both_hold():
    vb = VB()
    data = "data/x.json"
    old = FI(scopes=[SC("f"), SC("g", skel="s1")])
    new = FI(scopes=[SC("f"), SC("g", skel="s2")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "dn", new),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    node = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")], mods=[LIB],
                data=[data])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt", data: "h-old"})
    d = _plan(INP(index, SNAP(vb, [run], [node]),
                  versions={"do": old, "dt": tfi},
                  data_digests={data: "h-new"}, changed=(LIB, data)))
    assert tier(d, NID) == (T1, "f")


def _opaque_static_case(*, opaque=True, reach=True, since_gate=(),
                        changed=(LIB,), demoted=False, fresh=False):
    vb = VB()
    other = "pkg/other.py"
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", tfi, test=True,
                 imports=((LIB,) if reach else (other,))),
    })
    node = NODE(vb, NID, TEST, "r1", opaque=opaque)
    run = RUN(vb, "r1", {LIB: "dn" if fresh else "do", other: "do2",
                         TEST: "dt"})
    deps = SNAP(vb, [run], [node],
                demotions=({NID: "dt"} if demoted else None))
    return INP(index, deps,
               versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                         "dt": tfi, "dn": new},
               changed=changed, since_gate=since_gate)


def test_code_b_static_opaque_node_reached_statically():
    d = _plan(_opaque_static_case())
    assert tier(d, NID) == (T2, "b-static")
    assert d.via[NID] == LIB
    assert not PL.is_selected(d, NID)
    assert dict(d.fallbacks)[NID] == "opaque: spawned a process or overflowed"


def test_code_b_static_demoted_node_reached_statically():
    d = _plan(_opaque_static_case(opaque=False, demoted=True))
    assert tier(d, NID) == (T2, "b-static")
    assert dict(d.fallbacks)[NID] == "demoted by the selection audit"


def test_opaque_node_static_miss_has_no_tier():
    d = _plan(_opaque_static_case(reach=False))
    assert tier(d, NID) is None


def test_b_static_via_falls_back_to_non_py_path_then_test_file():
    inputs = _opaque_static_case(reach=False, changed=("data/x.json",))
    d = _plan(inputs)
    assert tier(d, NID) == (T2, "b-static")
    assert d.via[NID] == "data/x.json"


def test_code_u_static_unrecorded_file_reached_statically():
    lib = FI(scopes=[SC("f", body="b2")])
    old = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "dn", lib),
                 TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                          imports=(LIB,))})
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    deps = SNAP(vb, [run], [], inventory=[TEST + "::a", TEST + "::b",
                                          TEST + "::c"])
    d = _plan(INP(index, deps, versions={"do": old,
                                         "dt": FI(scopes=[SC("test_f")])},
                  changed=(LIB,)))
    assert tier(d, TEST) == (T2, "u-static")
    assert d.via[TEST] == LIB
    assert d.files == () and d.whole_files == ()
    assert d.weights == {TEST: 3}


def test_u_static_via_prefers_py_then_non_py():
    lib = FI(scopes=[SC("f", body="b2")])
    old = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "dn", lib),
                 TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                          imports=(LIB,))})
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    deps = SNAP(vb, [run], [])
    vers = {"do": old, "dt": FI(scopes=[SC("test_f")])}
    d = _plan(INP(index, deps, versions=vers,
                  changed=(LIB, "assets/a.bin")))
    assert d.via[TEST] == LIB
    d2 = _plan(INP(index, deps, versions=vers, changed=("assets/a.bin",)))
    assert d2.via[TEST] == "assets/a.bin"


# --- R3 / corrupt ------------------------------------------------------------

def test_opaque_and_demoted_precise_hit_is_t1():
    # Spec R3 - a precise hit (clause d) on an opaque node is T1 even when the
    # static rule misses (the node ran LIB through a dynamic import).
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")], "opaque": True},
                       test_imports=())
    d = _plan(inputs)
    assert tier(d, NID) == (T1, "d")
    assert dict(d.fallbacks)[NID] == "opaque: spawned a process or overflowed"
    # Same for a demoted node.
    vb_inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]}, test_imports=())
    demoted = C.SelectionInputs(
        index=vb_inputs.index,
        deps=C.DependencySnapshot(
            vocabulary=vb_inputs.deps.vocabulary, runs=vb_inputs.deps.runs,
            nodes=vb_inputs.deps.nodes, fixtures={}, demotions={NID: "dt"}),
        versions=vb_inputs.versions, data_digests={},
        compatibility=K, changed=vb_inputs.changed, test_roots=("tests",))
    d2 = _plan(demoted)
    assert tier(d2, NID) == (T1, "d")
    assert dict(d2.fallbacks)[NID] == "demoted by the selection audit"


def test_corrupt_ids_become_t2_never_unaffected():
    # Unresolvable node ids: even with a static miss the node is T2
    # b-static (via the test file), never silently unaffected.
    vb = VB()
    tfi = FI(scopes=[SC("test_f")])
    lib = FI(scopes=[SC("f")])
    index = IDX({LIB: PF(LIB, "d", lib),
                 TEST: PF(TEST, "dt", tfi, test=True)})
    node = C.NodeRecord(
        nodeid=NID, test_file=TEST, outcome="passed", run_id="r1",
        deps=C.ContextDeps(functions=C.selection_ids([999999]),
                           modules=C.selection_ids(()),
                           data=C.selection_ids(()), opaque=False),
        fixtures=C.selection_ids(()))
    run = RUN(vb, "r1", {LIB: "d", TEST: "dt"})
    d = _plan(INP(index, SNAP(vb, [run], [node]),
                  versions={"d": lib, "dt": tfi}, changed=()))
    assert tier(d, NID) == (T2, "b-static")
    assert d.via[NID] == TEST
    # Corrupt ambient ids behave the same way.
    amb_bad = C.ContextDeps(functions=C.selection_ids([999999]),
                            modules=C.selection_ids(()),
                            data=C.selection_ids(()), opaque=False)
    vb2 = VB()
    run2 = RUN(vb2, "r1", {LIB: "d", TEST: "dt"}, ambient=amb_bad)
    node2 = NODE(vb2, NID, TEST, "r1")
    d2 = _plan(INP(index, SNAP(vb2, [run2], [node2]),
                   versions={"d": lib, "dt": tfi}, changed=()))
    assert tier(d2, NID) == (T2, "b-static")


def test_corrupt_ids_with_precise_hit_stay_t1():
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]})
    run = inputs.deps.runs["r1"]
    bad = C.RunBaseline(
        run_id="r1", recorded_at=0.0, compatibility=K, digests=run.digests,
        ambient=C.ContextDeps(functions=C.selection_ids([999999]),
                              modules=C.selection_ids(()),
                              data=C.selection_ids(()), opaque=False))
    deps = C.DependencySnapshot(
        vocabulary=inputs.deps.vocabulary, runs={"r1": bad},
        nodes=inputs.deps.nodes, fixtures={}, demotions={})
    d = _plan(C.SelectionInputs(
        index=inputs.index, deps=deps, versions=inputs.versions,
        data_digests={}, compatibility=K, changed=inputs.changed,
        test_roots=("tests",)))
    assert tier(d, NID) == (T1, "d")


# --- R4 witness ---------------------------------------------------------------

def _witness_case(*, audit=False, ambient_mods=(), second_file=True):
    """LIB.f gets a plain default edit. Nothing ran ``f``; two test files
    import LIB and each hold two recorded nodes."""
    old = FI(scopes=[SC("f", skel="s1", local=True), SC("other")])
    new = FI(scopes=[SC("f", skel="s2", local=True), SC("other")])
    vb = VB()
    t_a, t_b = "tests/test_a.py", "tests/test_b.py"
    files = {LIB: PF(LIB, "dn", new)}
    for t in (t_a, t_b):
        files[t] = PF(t, "d" + t, FI(scopes=[SC("t1"), SC("t2")]),
                      test=True, imports=(LIB,))
    index = IDX(files)
    nodes = [NODE(vb, f"{t}::{n}", t, "r1", funcs=[(LIB, "other")])
             for t in (t_a, t_b) for n in ("t2", "t1")]
    run = RUN(vb, "r1", {LIB: "do", t_a: "d" + t_a, t_b: "d" + t_b},
              ambient=CT(vb, mods=ambient_mods))
    vers = {"do": old, "d" + t_a: FI(scopes=[SC("t1"), SC("t2")]),
            "d" + t_b: FI(scopes=[SC("t1"), SC("t2")])}
    return INP(index, SNAP(vb, [run], nodes), versions=vers, changed=(LIB,),
               audit=audit), [n.nodeid for n in nodes]


def test_soft_fallback_witness_is_exactly_one():
    inputs, ids = _witness_case()
    d = _plan(inputs)
    witnesses = [k for k, v in d.tiers.items() if v == (T1, "w")]
    # Smallest node of the smallest test file reaching the path.
    assert witnesses == ["tests/test_a.py::t1"]
    assert d.selected == 1
    assert d.files == ("tests/test_a.py",)
    assert "tests/test_a.py::t2" in d.deselect


def test_soft_fallback_witness_audit_keeps_the_whole_set():
    inputs, ids = _witness_case(audit=True)
    d = _plan(inputs)
    assert {k for k, v in d.tiers.items() if v == (T1, "w")} == set(ids)


def test_soft_fallback_ambient_import_witness_is_one_node_of_smallest_file():
    # Nothing statically reaches the path and no node ran it, but the
    # run's ambient context imported it: the run's smallest file's
    # smallest node is the witness.
    old = FI(scopes=[SC("f", skel="s1", local=True), SC("other")])
    new = FI(scopes=[SC("f", skel="s2", local=True), SC("other")])
    vb = VB()
    t_a, t_b = "tests/test_a.py", "tests/test_b.py"
    files = {LIB: PF(LIB, "dn", new)}
    for t in (t_a, t_b):
        files[t] = PF(t, "d" + t, FI(scopes=[SC("t1")]), test=True)
    nodes = [NODE(vb, f"{t}::t1", t, "r1") for t in (t_b, t_a)]
    run = RUN(vb, "r1", {LIB: "do", t_a: "d" + t_a, t_b: "d" + t_b},
              ambient=CT(vb, mods=[LIB]))
    vers = {"do": old, "d" + t_a: FI(scopes=[SC("t1")]),
            "d" + t_b: FI(scopes=[SC("t1")])}
    d = _plan(INP(IDX(files), SNAP(vb, [run], nodes), versions=vers,
                  changed=(LIB,)))
    assert [k for k, v in d.tiers.items() if v == (T1, "w")] == [
        "tests/test_a.py::t1"]


def test_witness_overrides_a_t2_tier_and_skips_when_t1_node_covers_path():
    inputs, ids = _witness_case()
    # A T1 node that already ran the path means no witness is needed.
    vb = VB()
    old = FI(scopes=[SC("f", skel="s1", local=True), SC("other")])
    new = FI(scopes=[SC("f", skel="s2", local=True), SC("other")])
    tfi = FI(scopes=[SC("t1"), SC("t2")])
    index = IDX({LIB: PF(LIB, "dn", new),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    caller = NODE(vb, TEST + "::t1", TEST, "r1", funcs=[(LIB, "f")])
    other = NODE(vb, TEST + "::t2", TEST, "r1", funcs=[(LIB, "other")])
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    d = _plan(INP(index, SNAP(vb, [run], [caller, other]),
                  versions={"do": old, "dt": tfi}, changed=(LIB,)))
    assert tier(d, TEST + "::t1") == (T1, "d")
    assert tier(d, TEST + "::t2") is None
    assert not any(v == (T1, "w") for v in d.tiers.values())


# --- R9 since_gate --------------------------------------------------------

def test_since_gate_drives_b_and_u_reach_with_empty_changed():
    # The record is fresh (nothing stale) and the diff is empty, but LIB
    # differs from the gate: the static rule still reaches the node.
    d = _plan(_opaque_static_case(changed=(), since_gate=(LIB,),
                                  fresh=True))
    _assert_t2(d, NID, "b-static", LIB)
    d0 = _plan(_opaque_static_case(changed=(), since_gate=(), fresh=True))
    assert d0.tiers == {}


def test_since_gate_non_python_reaches_unrecorded_files_u_static():
    lib = FI(scopes=[SC("f")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "d", lib),
                 TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]),
                          test=True)})
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "d", TEST: "dt"})], [])
    vers = {"d": lib, "dt": FI(scopes=[SC("test_f")])}
    d = _plan(INP(index, deps, versions=vers, changed=(),
                  since_gate=("assets/a.bin",)))
    assert tier(d, TEST) == (T2, "u-static")
    assert d.via[TEST] == "assets/a.bin"


def test_since_gate_does_not_widen_precise_clauses():
    # A path changed since the gate but identical to the record's
    # baseline is not stale: the precise clauses ignore since_gate.
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]}, body=("b1", "b1"),
                       changed=(), since_gate=(LIB,))
    assert tier(_plan(inputs), NID) is None


# --- R16 / R44 observers --------------------------------------------------------

def _observer_case(**kw):
    return _ambient_inputs(old_lib=ROUTE_OLD, new_lib=ROUTE_NEW, **kw)


def test_observer_promoted_t1_o_when_learned_path_is_import_time_changed():
    d = _plan(_observer_case(observers={NID: {LIB}}))
    assert tier(d, NID) == (T1, "o")
    assert d.observers_promoted == 1 and d.observers_deferred == 0
    assert d.conditional == ()


def test_observer_not_promoted_when_its_path_did_not_change():
    d = _plan(_observer_case(observers={NID: {"pkg/unrelated.py"}}))
    assert tier(d, NID) is None


def test_observer_star_requires_import_time_change_somewhere():
    d = _plan(_observer_case(observers={NID: {"*"}}))
    assert tier(d, NID) == (T1, "o")
    quiet = _dep_case(node_kw={"funcs": [(TEST, "test_f")]}, test_imports=(),
                      observers={NID: {"*"}}, changed=())
    assert tier(_plan(quiet), NID) is None


def test_observer_promoted_when_learned_path_was_deleted():
    # The learned path is gone from the index and stale for the run.
    vb = VB()
    gone = "pkg/gone.py"
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({TEST: PF(TEST, "dt", tfi, test=True)})
    node = NODE(vb, NID, TEST, "r1")
    run = RUN(vb, "r1", {gone: "dg", TEST: "dt"})
    d = _plan(INP(index, SNAP(vb, [run], [node]), versions={"dt": tfi},
                  changed=(gone,), observers={NID: {gone}}))
    assert tier(d, NID) == (T1, "o")


def test_observer_with_conditional_stays_t2_and_is_listed():
    d = _plan(_observer_case(observers={NID: {LIB}}, conditional=True,
                             ambient_mods=[LIB]))
    assert tier(d, NID) == (T2, "g-shared")
    assert d.conditional == ((NID, "r1"),)
    assert d.observers_deferred == 1 and d.observers_promoted == 0
    assert d.files == (TEST,) and d.deselect == (NID,)
    assert TEST not in d.whole_files


def test_observer_with_conditional_and_no_t2_clause_runs_unconditionally():
    d = _plan(_observer_case(observers={NID: {LIB}}, conditional=True))
    assert tier(d, NID) == (T1, "o")
    assert d.conditional == ()


def test_observer_keeps_an_earlier_t1_code():
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]},
                       observers={NID: {"*"}})
    assert tier(_plan(inputs), NID) == (T1, "d")


# --- R22 / R23 chosen record -----------------------------------------------------

def _two_record_case():
    """One node, two runs: r-sibling (newest) recorded against stale LIB
    code, r-near (older) against the current one. ``records`` is newest
    first, as the store returns it."""
    vb = VB()
    old = FI(scopes=[SC("f", body="b-old")])
    new = FI(scopes=[SC("f", body="b-new")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d-cur", new),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    r_near = RUN(vb, "r-near", {LIB: "d-cur", TEST: "dt"}, recorded_at=1.0)
    r_sib = RUN(vb, "r-sibling", {LIB: "d-old", TEST: "dt"}, recorded_at=2.0)
    n_near = NODE(vb, NID, TEST, "r-near", funcs=[(LIB, "f")])
    n_sib = NODE(vb, NID, TEST, "r-sibling", funcs=[(LIB, "f")])
    deps = SNAP(vb, [r_near, r_sib], [n_sib],
                records={NID: [n_sib, n_near]})
    return INP(index, deps, versions={"d-old": old, "dt": tfi},
               changed=(LIB,)), deps


def test_sibling_record_never_chosen_over_distance_zero_record():
    # r-near ran the current code (distance 0); the newer sibling record
    # r-sibling is stale and must never override it, whatever its age.
    inputs, _ = _two_record_case()
    d = _plan(inputs)
    assert d.chosen == {NID: "r-near"}
    assert tier(d, NID) is None
    assert not PL.is_selected(d, NID)


def test_older_closer_record_beats_newer_stale_record():
    # The newest record is stale; the older one is closer to the tree.
    vb = VB()
    cur = FI(scopes=[SC("f", body="b-cur")])
    mid = FI(scopes=[SC("f", body="b-other")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d-cur", cur),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    r1 = RUN(vb, "r1", {LIB: "d-cur", TEST: "dt"}, recorded_at=1.0)
    r2 = RUN(vb, "r2", {LIB: "d-mid", TEST: "dt"}, recorded_at=2.0)
    n1 = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")])
    n2 = NODE(vb, NID, TEST, "r2", funcs=[(LIB, "f")])
    deps = SNAP(vb, [r1, r2], [n2], records={NID: [n2, n1]})
    d = _plan(INP(index, deps, versions={"d-mid": mid, "dt": tfi},
                  changed=()))
    assert d.chosen == {NID: "r1"}
    assert tier(d, NID) is None


def test_closer_failing_record_is_not_hidden_by_a_newer_passing_one():
    vb = VB()
    cur = FI(scopes=[SC("f", body="b-cur")])
    mid = FI(scopes=[SC("f", body="b-other")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d-cur", cur),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    r1 = RUN(vb, "r1", {LIB: "d-cur", TEST: "dt"}, recorded_at=1.0)
    r2 = RUN(vb, "r2", {LIB: "d-mid", TEST: "dt"}, recorded_at=2.0)
    n1 = NODE(vb, NID, TEST, "r1", funcs=[(LIB, "f")], outcome="failed")
    n2 = NODE(vb, NID, TEST, "r2", funcs=[(LIB, "f")])
    deps = SNAP(vb, [r1, r2], [n2], records={NID: [n2, n1]})
    d = _plan(INP(index, deps, versions={"d-mid": mid, "dt": tfi},
                  changed=()))
    assert d.chosen == {NID: "r1"}
    assert tier(d, NID) == (T1, "a")


def test_distance_ties_go_to_newest_then_smallest_run_id():
    vb = VB()
    cur = FI(scopes=[SC("f")])
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({LIB: PF(LIB, "d", cur),
                 TEST: PF(TEST, "dt", tfi, test=True)})
    runs = [RUN(vb, rid, {LIB: "d", TEST: "dt"}, recorded_at=at)
            for rid, at in (("rb", 5.0), ("ra", 5.0), ("rz", 1.0))]
    recs = [NODE(vb, NID, TEST, rid, funcs=[(LIB, "f")])
            for rid in ("rz", "rb", "ra")]
    deps = SNAP(vb, runs, [recs[1]],
                records={NID: recs})
    d = _plan(INP(index, deps, versions={"d": cur, "dt": tfi}, changed=()))
    assert d.chosen == {NID: "ra"}


def test_cap_falls_back_to_the_next_usable_record():
    vb = VB()
    tfi = FI(scopes=[SC("test_f")])
    files = {TEST: PF(TEST, "dt", tfi, test=True)}
    runs, vers = [], {"dt": tfi}
    n_old = None
    for i in range(C.SELECTION_MAX_SIGNATURES + 1):
        files[LIB] = PF(LIB, "d-cur", FI(scopes=[SC("f", body="b-cur")]))
        runs.append(RUN(vb, f"r-{i:04d}", {LIB: f"d-{i}", TEST: "dt"},
                        recorded_at=float(i)))
        vers[f"d-{i}"] = FI(scopes=[SC("f", body=f"b-{i}")])
    # Newest run keeps the cap; the oldest run falls over the cap.
    over_cap = "r-0000"
    usable = f"r-{C.SELECTION_MAX_SIGNATURES:04d}"
    n_over = NODE(vb, NID, TEST, over_cap, funcs=[(LIB, "f")])
    n_ok = NODE(vb, NID, TEST, usable, funcs=[(LIB, "f")])
    deps = SNAP(vb, runs, [n_ok], records={NID: [n_ok, n_over]})
    d = _plan(INP(IDX(files), deps, versions=vers, changed=(LIB,)))
    assert d.chosen == {NID: usable}
    assert NID not in dict(d.fallbacks)
    only_over = SNAP(vb, runs, [n_over], records={NID: [n_over]})
    d2 = _plan(INP(IDX(files), only_over, versions=vers, changed=(LIB,)))
    assert dict(d2.fallbacks)[NID] == "too many recorded code versions"


def test_node_with_only_other_config_records_falls_back():
    vb = VB()
    tfi = FI(scopes=[SC("test_f")])
    index = IDX({TEST: PF(TEST, "dt", tfi, test=True)})
    run = RUN(vb, "r1", {TEST: "dt"}, compat=OTHER)
    node = NODE(vb, NID, TEST, "r1")
    d = _plan(INP(index, SNAP(vb, [run], [node]), versions={"dt": tfi},
                  changed=()))
    assert dict(d.fallbacks)[NID] == "record from another configuration"
    assert NID not in d.chosen


# --- whole files, deselect, counts -----------------------------------------------

def test_t2_nodes_of_a_partly_selected_file_are_deselected_not_whole():
    vb = VB()
    old = FI(scopes=[SC("f", body="b1"), SC("g")])
    new = FI(scopes=[SC("f", body="b2"), SC("g")])
    tfi = FI(scopes=[SC("t_f"), SC("t_g")])
    index = IDX({LIB: PF(LIB, "dn", new),
                 TEST: PF(TEST, "dt", tfi, test=True, imports=(LIB,))})
    n_f = NODE(vb, TEST + "::t_f", TEST, "r1", funcs=[(LIB, "f")])
    n_g = NODE(vb, TEST + "::t_g", TEST, "r1", funcs=[(LIB, "g")],
               opaque=True)
    run = RUN(vb, "r1", {LIB: "do", TEST: "dt"})
    d = _plan(INP(IDX({LIB: PF(LIB, "dn", new),
                       TEST: PF(TEST, "dt", tfi, test=True,
                                imports=(LIB,))}),
                  SNAP(vb, [run], [n_f, n_g]),
                  versions={"do": old, "dt": tfi}, changed=(LIB,)))
    assert tier(d, TEST + "::t_f") == (T1, "d")
    assert tier(d, TEST + "::t_g") == (T2, "b-static")
    assert d.files == (TEST,) and d.deselect == (TEST + "::t_g",)
    assert d.whole_files == ()
    assert d.reached == 0


def test_t2_never_counts_as_reached():
    # g-shared node whose recorded code also changed: T2, not reached.
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")]}, body=("b1", "b1"),
                       changed=(LIB,),
                       ambient=lambda vb: CT(vb, funcs=[(LIB, "f")]))
    d = _plan(inputs)
    assert d.reached <= 1
    assert all(tier(d, n) != (T2, "g-shared") or d.reached == 0
               for n in d.tiers)


# --- the planner no longer sets full_reason ----------------------------------------

def test_policy_digest_and_compatibility_unchanged_for_same_config():
    # Planner half of the R46 invariant: a config without the new keys
    # (default fields) and the same inputs with explicit defaults give
    # byte-identical decisions.
    old = FI(scopes=[SC("f", body="b1"), SC("g", body="g1")])
    new = FI(scopes=[SC("f", body="b2"), SC("g", body="g2")])
    a, _ = pair(old, new)
    b = C.SelectionInputs(
        index=a.index, deps=a.deps, versions=a.versions,
        data_digests=a.data_digests, compatibility=a.compatibility,
        changed=a.changed, test_roots=a.test_roots, since_gate=frozenset(),
        observers={}, conditional=False, audit=False)
    assert PL.plan(a) == PL.plan(b)


# --- audit classifier (R15) ---------------------------------------------------------

def _audit_world():
    """Three nodes in one run, one per R15 class.

    d1: T1 d (precise hit); g1: T2 g-reach with am non-empty (learnable);
    m1: unaffected (a miss when it fails).
    """
    vb = VB()
    old = FI(scopes=[SC("route", skel="s1", decorated=True),
                     SC("f", body="b1")])
    new = FI(scopes=[SC("route", skel="s2", decorated=True),
                     SC("f", body="b2")])
    other = "pkg/other.py"
    files = {
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g")])),
        "tests/test_d.py": PF("tests/test_d.py", "d1",
                              FI(scopes=[SC("t")]), test=True,
                              imports=(other,)),
        "tests/test_g.py": PF("tests/test_g.py", "d2",
                              FI(scopes=[SC("t")]), test=True,
                              imports=(LIB,)),
        "tests/test_m.py": PF("tests/test_m.py", "d3",
                              FI(scopes=[SC("t")]), test=True,
                              imports=(other,)),
    }
    d1 = NODE(vb, "tests/test_d.py::t", "tests/test_d.py", "r1",
              funcs=[(LIB, "f")])
    g1 = NODE(vb, "tests/test_g.py::t", "tests/test_g.py", "r1",
              funcs=[("tests/test_g.py", "t")])
    m1 = NODE(vb, "tests/test_m.py::t", "tests/test_m.py", "r1",
              funcs=[(other, "g")])
    run = RUN(vb, "r1", {LIB: "do", other: "do2",
                         "tests/test_d.py": "d1", "tests/test_g.py": "d2",
                         "tests/test_m.py": "d3"})
    vers = {"do": old, "do2": FI(scopes=[SC("g")]),
            "d1": FI(scopes=[SC("t")]), "d2": FI(scopes=[SC("t")]),
            "d3": FI(scopes=[SC("t")])}
    deps = SNAP(vb, [run], [d1, g1, m1])
    inputs = INP(IDX(files), deps, versions=vers, changed=(LIB,))
    return inputs, deps


def test_plan_of_the_audit_world_has_one_node_per_class():
    inputs, _ = _audit_world()
    d = _plan(inputs)
    assert tier(d, "tests/test_d.py::t") == (T1, "d")
    assert tier(d, "tests/test_g.py::t") == (T2, "g-reach")
    assert tier(d, "tests/test_m.py::t") is None
    assert d.learnable == {"tests/test_g.py::t": frozenset({LIB})}


def test_audit_classify_learns_unlearns_and_misses():
    inputs, deps = _audit_world()
    d = _plan(inputs)
    failing = ["tests/test_d.py::t", "tests/test_g.py::t",
               "tests/test_m.py::t"]
    result = PL.audit_classify(d, deps, failing, failing)
    # (a) learned: T2 with am | pw paths.
    assert result.learned == {"tests/test_g.py::t": frozenset({LIB})}
    # (d) T1 failure produces nothing; (c) the unaffected node is a miss.
    assert result.misses == ("tests/test_m.py::t",)
    assert result.unlearned == ()
    assert result.previously_deferred == 1
    assert result.failed == 1
    assert PL.audit_misses(d, deps, failing) == ("tests/test_m.py::t",)


def test_audit_classify_unlearned_has_no_paths_to_learn():
    d = _plan(_opaque_static_case(changed=("data/x.json",), reach=False))
    deps = _opaque_static_case().deps
    result = PL.audit_classify(d, deps, [NID], [NID])
    assert result.learned == {}
    assert result.unlearned == (NID,)
    assert result.misses == ()
    assert result.previously_deferred == 1 and result.failed == 1


def test_audit_classify_t2_file_key_failures_are_unlearned():
    lib = FI(scopes=[SC("f", body="b2")])
    old = FI(scopes=[SC("f", body="b1")])
    vb = VB()
    index = IDX({LIB: PF(LIB, "dn", lib),
                 TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                          imports=(LIB,))})
    deps = SNAP(vb, [RUN(vb, "r1", {LIB: "do", TEST: "dt"})], [])
    d = _plan(INP(index, deps, versions={"do": old,
                                         "dt": FI(scopes=[SC("test_f")])},
                  changed=(LIB,)))
    ran = [TEST + "::a", TEST + "::b"]
    result = PL.audit_classify(d, deps, [TEST + "::a"], ran)
    assert result.previously_deferred == 2
    assert result.failed == 1
    assert result.unlearned == (TEST + "::a",)
    assert result.misses == ()


def test_audit_classify_ignores_non_passing_chosen_records():
    inputs = _dep_case(node_kw={"funcs": [(LIB, "f")], "outcome": "failed"})
    d = _plan(inputs)
    result = PL.audit_classify(d, inputs.deps, [NID], [NID])
    assert result.misses == () and result.learned == {}
    assert result.previously_deferred == 0 and result.failed == 0
