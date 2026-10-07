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
from array import array
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ptest import contracts as C

# ---------------------------------------------------------------------------
# BARRIER SHIM (T2-local, test-only seam).
#
# The frozen contracts block (design section 12) has not landed in this
# worktree yet, so the SELECTION_* names below do not exist on
# ptest.contracts here. When the barrier lands, every hasattr() passes
# and nothing is injected. Until then the missing shapes are declared
# locally, verbatim from the frozen spec, and attached to the contracts
# module so the planner (which only does C.<name> attribute reads)
# resolves them identically before and after the merge.
# Integration note for the orchestrator: delete this block after the
# merge and re-run this file; nothing else in this file changes meaning.
# ---------------------------------------------------------------------------

if not hasattr(C, "SELECTION_PROTOCOL"):
    C.SELECTION_PROTOCOL = "ptest-selection-v1"  # type: ignore[attr-defined]
if not hasattr(C, "SELECTION_ABSENT_DIGEST"):
    C.SELECTION_ABSENT_DIGEST = ""  # type: ignore[attr-defined]
if not hasattr(C, "SELECTION_PASSING_OUTCOMES"):
    C.SELECTION_PASSING_OUTCOMES = frozenset({"passed", "skipped", "xfailed"})  # type: ignore[attr-defined]
if not hasattr(C, "SELECTION_MAX_SIGNATURES"):
    C.SELECTION_MAX_SIGNATURES = 128  # type: ignore[attr-defined]


def _shim_ids(values):
    return array("I", sorted(set(values)))


def _shim_static_reach(reverse, seeds):
    queue = [s for s in seeds if isinstance(s, str)]
    seen = set(queue)
    while queue:
        for importer in reverse.get(queue.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return frozenset(seen)


def _shim_key_path(key):
    return key.rsplit(":", 1)[0]


def _shim_test_file(nodeid):
    return nodeid.split("::", 1)[0]


def _shim_name_key(path, name):
    return f"{path}:{name}"


def _shim_empty_context(*, opaque=False):
    return C.ContextDeps(functions=array("I"), modules=array("I"),
                         data=array("I"), opaque=opaque)


for _name, _obj in {
    "selection_ids": _shim_ids,
    "selection_static_reach": _shim_static_reach,
    "selection_key_path": _shim_key_path,
    "selection_test_file": _shim_test_file,
    "selection_name_key": _shim_name_key,
    "selection_empty_context": _shim_empty_context,
}.items():
    if not hasattr(C, _name):
        setattr(C, _name, _obj)


if not hasattr(C, "ScopeIndex"):
    @dataclass(frozen=True, slots=True)
    class ScopeIndex:
        qualname: str
        body: str
        skeleton: str
        refs: tuple = ()


if not hasattr(C, "ClassIndex"):
    @dataclass(frozen=True, slots=True)
    class ClassIndex:
        qualname: str
        skeleton: str
        body: str
        refs: tuple = ()


if not hasattr(C, "StatementIndex"):
    @dataclass(frozen=True, slots=True)
    class StatementIndex:
        kind: str
        bound: tuple = ()
        refs: tuple = ()
        fingerprint: str = ""


if not hasattr(C, "FileIndex"):
    @dataclass(frozen=True, slots=True)
    class FileIndex:
        parsed: bool
        imports: tuple = ()
        scopes: tuple = ()
        classes: tuple = ()
        statements: tuple = ()


if not hasattr(C, "ProjectFile"):
    @dataclass(frozen=True, slots=True, eq=False)
    class ProjectFile:
        path: str
        digest: str
        index: object
        modules: tuple = ()
        test: bool = False
        support: bool = False
        imports: frozenset = frozenset()
        scope_refs: object = field(default_factory=dict)
        class_refs: object = field(default_factory=dict)
        statement_refs: tuple = ()
        statement_bound: tuple = ()


if not hasattr(C, "ProjectIndex"):
    @dataclass(frozen=True, slots=True, eq=False)
    class ProjectIndex:
        root: object
        files: object
        test_files: frozenset = frozenset()
        reverse: object = field(default_factory=dict)
        unparsed: frozenset = frozenset()
        complete: bool = True


if not hasattr(C, "DepVocabulary"):
    @dataclass(frozen=True, slots=True, eq=False)
    class DepVocabulary:
        paths: tuple = ()
        functions: tuple = ()
        fixtures: tuple = ()


if not hasattr(C, "ContextDeps"):
    @dataclass(frozen=True, slots=True, eq=False)
    class ContextDeps:
        functions: object
        modules: object
        data: object
        opaque: bool = False


if not hasattr(C, "RunBaseline"):
    @dataclass(frozen=True, slots=True, eq=False)
    class RunBaseline:
        run_id: str
        recorded_at: float
        compatibility: str
        digests: object
        ambient: object


if not hasattr(C, "NodeRecord"):
    @dataclass(frozen=True, slots=True, eq=False)
    class NodeRecord:
        nodeid: str
        test_file: str
        outcome: str
        run_id: str
        deps: object
        fixtures: object = field(default_factory=lambda: array("I"))


if not hasattr(C, "FixtureRecord"):
    @dataclass(frozen=True, slots=True, eq=False)
    class FixtureRecord:
        fixture: int
        run_id: str
        deps: object


if not hasattr(C, "DependencySnapshot"):
    @dataclass(frozen=True, slots=True, eq=False)
    class DependencySnapshot:
        vocabulary: object
        runs: object
        nodes: object
        fixtures: object = field(default_factory=dict)
        demotions: object = field(default_factory=dict)


if not hasattr(C, "SelectionInputs"):
    @dataclass(frozen=True, slots=True, eq=False)
    class SelectionInputs:
        index: object
        deps: object
        versions: object
        data_digests: object
        compatibility: str
        changed: frozenset = frozenset()
        test_roots: tuple = ()


if not hasattr(C, "ChangedUnit"):
    @dataclass(frozen=True, slots=True)
    class ChangedUnit:
        kind: str
        label: str
        tests: int


if not hasattr(C, "SelectionDecision"):
    @dataclass(frozen=True, slots=True)
    class SelectionDecision:
        full_reason: object
        files: tuple
        deselect: tuple
        whole_files: tuple
        selected: int
        reached: int
        recorded: int
        total_files: int
        units: tuple
        fallbacks: tuple
        coverage: float


for _name in (
    "ScopeIndex", "ClassIndex", "StatementIndex", "FileIndex",
    "ProjectFile", "ProjectIndex", "DepVocabulary", "ContextDeps",
    "RunBaseline", "NodeRecord", "FixtureRecord", "DependencySnapshot",
    "SelectionInputs", "ChangedUnit", "SelectionDecision",
):
    if _name in globals() and not hasattr(C, _name):
        setattr(C, _name, globals()[_name])

from ptest import selection_planner as PL

K = "compat-fingerprint"
OTHER = "other-configuration"
LIB = "pkg/lib.py"
TEST = "tests/test_lib.py"


# ---------------------------------------------------------------------------
# Builders: hand-made index / snapshot fragments. The vocabulary builder
# VB must be shared between RUN digests and NODE deps so path ids align.
# ---------------------------------------------------------------------------

def SC(q, body="b:" + "x", skel="s:" + "x", refs=()):
    return C.ScopeIndex(qualname=q, body=body, skeleton=skel,
                        refs=tuple(tuple(r) for r in refs))


def CL(q, skel="s:" + "x", body="c:" + "x", refs=()):
    return C.ClassIndex(qualname=q, skeleton=skel, body=body,
                        refs=tuple(tuple(r) for r in refs))


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


def SNAP(vb, runs, nodes, fixtures=(), demotions=None):
    return C.DependencySnapshot(
        vocabulary=vb.build(),
        runs={r.run_id: r for r in runs},
        nodes={n.nodeid: n for n in nodes},
        fixtures={f.fixture: f for f in fixtures},
        demotions=dict(demotions or {}),
    )


def INP(index, deps, versions=None, data_digests=None, compat=K,
        changed=frozenset(), test_roots=("tests",)):
    return C.SelectionInputs(
        index=index, deps=deps, versions=dict(versions or {}),
        data_digests=dict(data_digests or {}), compatibility=compat,
        changed=frozenset(changed), test_roots=tuple(test_roots),
    )


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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert d.files == (TEST,)
    assert units_by_kind(d)["function"] == {LIB + "::f": 1}


def test_identical_fingerprints_select_nothing():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b1")])  # formatting-only edit
    inputs, nid = pair(old, new, lib_digests=("do", "dn"))
    # Baseline digest differs from current, but content fingerprints match.
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["name"] == {LIB + ":f": 0}


def test_same_skeleton_gives_no_name_unit():
    old = FI(scopes=[SC("f", skel="s1", body="b1")])
    new = FI(scopes=[SC("f", skel="s1", body="b2")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)  # body change still selects via CF
    assert "name" not in units_by_kind(d)


def test_new_scope_in_test_file_selects_whole():
    lib = FI(scopes=[SC("f")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f"), SC("test_g")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert TEST in d.whole_files


def test_new_scope_in_lib_is_not_whole_file():
    new_stmts = [ST("def", bound=("f",), fp="s1"), ST("def", bound=("g",), fp="s2")]
    old_stmts = [ST("def", bound=("f",), fp="s1")]
    old = FI(scopes=[SC("f")], stmts=old_stmts)
    new = FI(scopes=[SC("f"), SC("g")], stmts=new_stmts)
    inputs, nid = pair(old, new, node_kw={"funcs": [(LIB, "f")]})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["name"] == {LIB + ":C": 0}
    assert units_by_kind(d)["function"] == {LIB + "::C": 0, LIB + "::C.m": 1}


def test_unchanged_class_selects_nothing():
    old = FI(classes=[CL("C", body="c1")],
             scopes=[SC("C.m", body="b1")])
    inputs, nid = pair(old, old, node_kw={"funcs": [(LIB, "C.m")]})
    d = PL.plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.files == ()


def test_new_class_in_test_file_selects_whole():
    lib = FI(scopes=[SC("f")])
    old_t = FI(scopes=[SC("test_f")])
    new_t = FI(scopes=[SC("test_f")], classes=[CL("Helper")])
    inputs, nid = pair(lib, lib, test_fi=new_t, old_test_fi=old_t,
                       lib_digests=("d", "d"), test_digest="dt-new",
                       test_old_digest="dt-old", changed=(TEST,))
    d = PL.plan(inputs)
    assert TEST in d.whole_files


def test_new_class_in_lib_is_not_whole_file():
    old = FI(scopes=[SC("f")])
    new = FI(scopes=[SC("f")], classes=[CL("G")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert d.whole_files == ()
    assert not PL.is_selected(d, nid)


def test_assign_change_seeds_name_unit():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    new = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a2")])
    inputs, nid = pair(old, new, node_kw={"funcs": []})
    d = PL.plan(inputs)
    assert units_by_kind(d)["name"] == {LIB + ":LIMIT": 0}


def test_unchanged_assign_seeds_no_name():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    inputs, nid = pair(old, old, node_kw={"funcs": []})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert not PL.is_selected(d, n2.nodeid)


def test_effect_change_selects_module_dependents():
    old = FI(stmts=[ST("effect", fp="e1")])
    new = FI(stmts=[ST("effect", fp="e2")])
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]})
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_effect_reorder_without_change_selects_nothing():
    stmts = [ST("effect", fp="e1"), ST("effect", fp="e2")]
    old = FI(stmts=stmts)
    new = FI(stmts=list(reversed(stmts)))  # same multiset
    inputs, nid = pair(old, new, node_kw={"funcs": [], "mods": [LIB]})
    d = PL.plan(inputs)
    assert not PL.is_selected(d, nid)
    assert "module" not in units_by_kind(d)


def test_missing_old_version_selects_conservatively():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b1")])  # identical, but no old cached
    inputs, nid = pair(old, new, versions={}, seed_versions=False)
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)  # whole-path: PW + W + AM + SK
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_unparsed_old_index_selects_whole():
    old = FI(parsed=False)
    new = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert units_by_kind(d)["module"] == {LIB: 1}


def test_unparsed_new_index_selects_whole():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(parsed=False)
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)


def test_doc_statement_change_is_ignored():
    old = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("doc", fp="d1")])
    new = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("doc", fp="d2")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert not PL.is_selected(d, nid)
    assert d.units == ()


def test_doc_statement_bound_name_is_ignored():
    # A doc statement binding a name must not seed NAMES by itself.
    old = FI(stmts=[ST("doc", bound=("NOTE",), fp="d1")])
    new = FI(stmts=[ST("doc", bound=("NOTE",), fp="d2")])
    inputs, nid = pair(old, new, node_kw={"funcs": []})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)  # LIMIT seed -> AF(LIB, f) -> clause d


def test_constant_unused_by_any_body_selects_nothing():
    old = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a1")])
    new = FI(scopes=[SC("f", body="b1")],
             stmts=[ST("assign", bound=("LIMIT",), fp="a2")])
    lib_refs = {"f": frozenset({key(LIB, "OTHER")})}
    inputs, nid = pair(old, new, lib_refs=lib_refs, node_kw={"funcs": [(LIB, "f")]})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    # Propagated names never become units: only the LIMIT seed does.
    assert units_by_kind(d).get("name", {}) == {LIB + ":LIMIT": 0}


def test_default_with_literal_selects_nothing():
    old = FI(scopes=[SC("helper", body="b1")],
             stmts=[ST("def", bound=("helper",), refs=[], fp="s1")])
    new = FI(scopes=[SC("helper", body="b2")],
             stmts=[ST("def", bound=("helper",), refs=[], fp="s1")])
    inputs, nid = pair(old, new, node_kw={"funcs": [(LIB, "other")]})
    d = PL.plan(inputs)
    assert not PL.is_selected(d, nid)


def test_subclass_of_changed_class_selects_method_callers():
    old = FI(classes=[CL("Base", body="c1"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    new = FI(classes=[CL("Base", body="c2"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    lib_class_refs = {"Sub": frozenset({key(LIB, "Base")})}
    inputs, nid = pair(old, new, lib_class_refs=lib_class_refs,
                       node_kw={"funcs": [(LIB, "Sub.run")]})
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)


def test_subclass_of_unchanged_class_selects_nothing():
    old = FI(classes=[CL("Base", body="c1"), CL("Sub", body="s1")],
             scopes=[SC("Base.run", body="b1"), SC("Sub.run", body="b1")])
    lib_class_refs = {"Sub": frozenset({key(LIB, "Base")})}
    inputs, nid = pair(old, old, lib_class_refs=lib_class_refs,
                       node_kw={"funcs": [(LIB, "Sub.run")]})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)


def test_effect_statement_without_any_change_selects_nothing():
    old = FI(stmts=[ST("assign", bound=("LIMIT",), fp="a1"),
                    ST("effect", refs=[("LIMIT",)], fp="e1")])
    refs = [frozenset(), frozenset({key(LIB, "LIMIT")})]
    bound = [frozenset({key(LIB, "LIMIT")}), frozenset()]
    inputs, nid = pair(old, old, node_kw={"funcs": [], "mods": [LIB]},
                       lib_stmt_refs=refs, lib_stmt_bound=bound)
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, n_a.nodeid)
    assert PL.is_selected(d, n_b.nodeid)


def test_missing_fixture_record_is_opaque_and_static_hit_selects():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, node_kw={"fixtures": [0]})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, node.nodeid)


def test_ambient_function_change_static_miss_skips():
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
    d = PL.plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


def test_ambient_data_change_sets_full_reason():
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
    d = PL.plan(inputs)
    assert d.full_reason == f"ambient data file {data} changed"


def test_ambient_data_unchanged_sets_no_full_reason():
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
    d = PL.plan(inputs)
    assert d.full_reason is None
    assert not PL.is_selected(d, node.nodeid)


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
    d = PL.plan(inputs)
    # Unresolvable ambient id: static rule decides (reach hits here).
    assert PL.is_selected(d, node.nodeid)
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
    d = PL.plan(inputs)
    assert PL.is_selected(d, node.nodeid)
    assert dict(d.fallbacks)[node.nodeid] == "opaque: spawned a process or overflowed"


# ---------------------------------------------------------------------------
# Clause b: opaque / demoted nodes follow the static rule only (N3)
# ---------------------------------------------------------------------------

def test_opaque_node_selected_on_static_hit():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, node_kw={"opaque": True})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


def test_demoted_node_selected_on_static_hit():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, test_digest="dt-cur",
                       demotions={TEST + "::test_f": "dt-cur"})
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


def test_demotion_clears_when_test_file_digest_changes():
    # The demotion was recorded against dt-old; the file has since been
    # re-recorded (dt-cur), so the precise rule applies again.
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new, test_digest="dt-cur",
                       demotions={TEST + "::test_f": "dt-old"})
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)  # precise clause d still fires here
    assert nid not in dict(d.fallbacks)


def test_demoted_node_ignores_precise_hit_on_static_miss():
    # Demotion means the dynamic record is untrusted: even a precise
    # function hit must not select when the static rule provably misses.
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
    d = PL.plan(inputs)
    assert not PL.is_selected(d, node.nodeid)


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
    d = PL.plan(inputs)
    assert fresh in d.whole_files
    assert "unrecorded" not in units_by_kind(d)


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
    d = PL.plan(inputs)
    assert fresh in d.files
    assert fresh in d.whole_files
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    assert fresh in d.files
    assert units_by_kind(d)["unrecorded"] == {fresh: 0}


def test_unknown_node_ids_run():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    # A brand-new parametrized id in a selected file is not in deselect.
    assert PL.is_selected(d, TEST + "::test_f[new-param-1]")


# ---------------------------------------------------------------------------
# Clause a: non-passing outcomes always run (N4)
# ---------------------------------------------------------------------------

def test_failed_outcome_always_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="failed")
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)
    assert d.files == (TEST,)
    assert d.units == ()


def test_error_outcome_always_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="error")
    d = PL.plan(inputs)
    assert PL.is_selected(d, nid)


def test_passing_outcome_not_selected_without_changes():
    lib = FI(scopes=[SC("f", body="b1")])
    inputs, nid = pair(lib, lib, lib_digests=("d", "d"), changed=(),
                       outcome="passed")
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    # TEST has no usable node and is under tests/ with the helper.
    assert TEST in d.files
    assert "other/test_x.py" not in d.files


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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    vb = VB()
    other = "pkg/other.py"
    index = IDX({
        LIB: PF(LIB, "dn", new),
        other: PF(other, "do2", FI(scopes=[SC("g", body="bg")])),
        TEST: PF(TEST, "dt", FI(scopes=[SC("test_f")]), test=True,
                 imports=(other,)),
    })
    # Opaque nodes decided by the static rule still count as reached when
    # their recorded deps touch the change.
    node = NODE(vb, TEST + "::test_f", TEST, "r1", funcs=[(LIB, "f")],
                opaque=True)
    run = RUN(vb, "r1", {LIB: "do", other: "do2", TEST: "dt"})
    deps = SNAP(vb, [run], [node])
    inputs = INP(index, deps,
                 versions={"do": old, "do2": FI(scopes=[SC("g", body="bg")]),
                           "dt": FI(scopes=[SC("test_f")])},
                 changed=(LIB,))
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    # The node is unusable, but its file is still selected whole by the
    # static rule (4.6), so the node runs.
    assert PL.is_selected(d, nid)
    assert TEST in d.whole_files
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
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


def test_is_selected_full_reason_selects_everything():
    old = FI(scopes=[SC("f", body="b1")])
    new = FI(scopes=[SC("f", body="b2")])
    inputs, nid = pair(old, new)
    d = PL.plan(inputs)
    assert d.full_reason is None
    full = C.SelectionDecision(
        full_reason="ambient data file data/x.bin changed", files=(),
        deselect=(), whole_files=(), selected=0, reached=0, recorded=0,
        total_files=0, units=(), fallbacks=(), coverage=0.0,
    )
    assert PL.is_selected(full, "anything/at_all.py::test_x")
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
    d = PL.plan(inputs)
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
    d = PL.plan(inputs)
    deps = inputs.deps
    # Previously failed records are always selected (clause a): no miss.
    assert PL.audit_misses(d, deps, [nid]) == ()
    assert PL.audit_misses(d, deps, ["tests/ghost.py::test_no"]) == ()


def test_empty_snapshot_plans_empty_decision():
    vb = VB()
    index = IDX({})
    deps = SNAP(vb, [], [])
    d = PL.plan(INP(index, deps, changed=()))
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
    assert PL.plan(inputs_a) == PL.plan(inputs_b)


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
        d = PL.plan(inputs)
        assert PL.is_selected(d, node.nodeid)
