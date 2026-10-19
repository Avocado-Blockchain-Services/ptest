"""Dependency-recorded test selection planner (planner v2).

Pure functions over the frozen contracts model. The decision procedure
is normative in design section 4: usable records (4.1), per-run
staleness (4.2), per-path diff (4.3), propagation fixpoint (4.4),
per-test clauses (4.5), unrecorded files (4.6), static reach (4.7) and
output assembly (4.8), plus the self-audit miss computation.

0.7.0 tiers: every recorded node and considered unrecorded test file
gets at most one (tier, code). T1 ("run now") carries positive evidence
(codes a, c, d, f, n, o, e, w); T2 ("deferred") is what the proof system
cannot clear (g-shared, g-reach, g-data, b-static, u-static) and never
runs in an iteration run. Each node is judged by its chosen record: the
usable record nearest the tree (R22).

Purity contract: no I/O, no clock, no project-code imports. Every
output tuple is sorted, so identical inputs give identical outputs.
Unresolvable vocabulary ids make the affected node opaque (static
rule), never silently dropped.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ptest import contracts as C

_DEFINITION_KINDS = ("def", "class", "import", "assign")

_OPAQUE_REASON = "opaque: spawned a process or overflowed"
_DEMOTED_REASON = "demoted by the selection audit"
_NO_RECORD_REASON = "no dependency record"
_OTHER_CONFIG_REASON = "record from another configuration"
_FIXTURE_REASON = "fixture record missing"
_CAP_REASON = "too many recorded code versions"


class _Analysis:
    """One memoised per-signature diff plus propagation result (D5)."""

    __slots__ = ("cf", "seeds", "names", "wild", "am", "sk", "wf", "pw", "af",
                 "soft")

    def __init__(self):
        self.cf = set()      # (path, qualname) changed functions
        self.seeds = set()   # name keys from 4.3 only (units use these)
        self.names = set()   # seeds + propagated name keys
        self.wild = set()    # paths whose every name counts changed
        self.am = set()      # paths with effectful (ambient) changes
        self.sk = set()      # paths with skeleton changes
        self.wf = set()      # test files selected whole
        self.pw = set()      # path-wide changed paths (every function)
        self.af = set()      # (path, qualname) affected via propagation
        self.soft = set()    # paths with caller-local import-time changes


def _cur_digest(index, path):
    found = index.files.get(path)
    if found is None:
        return C.SELECTION_ABSENT_DIGEST
    return found.digest


def _baseline_map(run, vocabulary):
    """Baseline digest by path; out-of-range path ids are ignored."""
    paths = vocabulary.paths
    out = {}
    for pid, digest in run.digests.items():
        if (isinstance(pid, int) and not isinstance(pid, bool)
                and 0 <= pid < len(paths)):
            out[paths[pid]] = digest
    return out


def _stale_sets(index, basemap, data_digests):
    """4.2: stale .py paths and stale recorded data paths of one run."""
    stale_py = set()
    for path in set(basemap) | set(index.files):
        if not path.endswith(".py"):
            continue
        if basemap.get(path) != _cur_digest(index, path):
            stale_py.add(path)
    stale_data = set()
    for path, digest in basemap.items():
        if path.endswith(".py"):
            continue
        current = data_digests.get(path, "")
        # "?" is an unreadable or oversize file: it proves nothing, so it
        # is stale on either side (N1).
        if digest == "?" or current == "?" or digest != current:
            stale_data.add(path)
    return stale_py, stale_data


def _signature(stale_py, basemap, stale_data):
    return frozenset([(p, basemap.get(p)) for p in stale_py]
                     + [("data", d) for d in stale_data])


def _def_map(file_index):
    """Bound name -> ordered tuple of fingerprints (definition kinds)."""
    grouped = {}
    for stmt in file_index.statements:
        if stmt.kind not in _DEFINITION_KINDS or stmt.bound == ("*",):
            continue
        for name in stmt.bound:
            grouped.setdefault(name, []).append(stmt.fingerprint)
    return {name: tuple(fps) for name, fps in grouped.items()}


def _star_seq(file_index):
    return tuple(s.fingerprint for s in file_index.statements
                 if s.bound == ("*",))


def _effect_fps(file_index):
    return [s.fingerprint for s in file_index.statements
            if s.kind == "effect"]


def _seed_name(analysis, per_path, path, qualname):
    key = C.selection_name_key(path, qualname.split(".")[0])
    analysis.seeds.add(key)
    analysis.names.add(key)
    per_path.add(key)


def _seed_class(analysis, per_path, index, old_scopes, new_scopes,
                path, qualname):
    _seed_name(analysis, per_path, path, qualname)
    analysis.cf.add((path, qualname))
    prefix = qualname + "."
    for scopes in (old_scopes, new_scopes):
        for qual in scopes:
            if qual.startswith(prefix):
                analysis.cf.add((path, qual))


def _import_time_scope(scope) -> bool:
    """Adding or removing this scope changes what importing does: it is
    decorated (registration), or a method (the class's behaviour changes
    even for tests that never call a project method, e.g. ``__eq__``).
    A plain new or removed module function only binds a name."""
    return bool(getattr(scope, "decorated", False)) or "." in scope.qualname


def _reads_signature(qualname) -> bool:
    """A dunder method: a framework may read it without calling it
    (``__init__`` of a dependency class, ``__call__``, ``__new__``)."""
    name = qualname.rsplit(".", 1)[-1]
    return name.startswith("__") and name.endswith("__")


def _plain_class(file_index, qualname) -> bool:
    """Only code reading this class notices its change: a plain class
    whose direct methods all have caller-local skeletons."""
    for cls in file_index.classes:
        if cls.qualname == qualname:
            if not cls.plain:
                return False
            prefix = qualname + "."
            return all(scope.local for scope in file_index.scopes
                       if scope.qualname.startswith(prefix)
                       and "." not in scope.qualname[len(prefix):])
    return False


def _local_def(file_index, name) -> bool:
    for scope in file_index.scopes:
        if scope.qualname == name:
            return scope.local
    return False


def _diff_path(analysis, index, path, basemap, versions):
    """4.3: diff one stale path; fills the analysis sets."""
    old_digest = basemap.get(path)
    old = versions.get(old_digest) if old_digest is not None else None
    current = index.files.get(path)
    new = current.index if current is not None else None
    # Only a path present in the current index can be a runnable test
    # file: a removed test file must not enter WF (5.8 lists runnable
    # test files), even though the store still holds nodes for it.
    is_test = path in index.test_files
    added = path not in basemap
    if added or new is None or old is None or not old.parsed or not new.parsed:
        # Whole-path change. An added non-test file contributes no PW:
        # its functions never ran, so no record can reference them.
        if not (added and not is_test):
            analysis.pw.add(path)
        analysis.wild.add(path)
        analysis.am.add(path)
        analysis.sk.add(path)
        if is_test:
            analysis.wf.add(path)
        return old
    per_path = set()
    old_scopes = {s.qualname: s for s in old.scopes}
    new_scopes = {s.qualname: s for s in new.scopes}
    # Import-time changes (N1): a decorator, default, annotation or
    # signature, a scope added or removed, and any class change run when
    # the module is imported. Their effect (a registered route, plugin,
    # model, schema) reaches tests that never execute the function, so
    # the path is an ambient change: it reaches tests by the static rule.
    for qual, scope in new_scopes.items():
        prev = old_scopes.get(qual)
        if prev is None:
            if _import_time_scope(scope):
                analysis.am.add(path)
            if is_test:
                analysis.wf.add(path)
        else:
            if scope.body != prev.body:
                analysis.cf.add((path, qual))
            if scope.skeleton != prev.skeleton:
                analysis.cf.add((path, qual))
                if scope.local and prev.local:
                    # A plain default or annotation: only callers notice,
                    # like a body change. A top-level function or a dunder
                    # may still be read by a framework through its name
                    # (``Depends(f)``), which propagation follows.
                    analysis.soft.add(path)
                    if "." not in qual or _reads_signature(qual):
                        _seed_name(analysis, per_path, path, qual)
                else:
                    analysis.am.add(path)
                    _seed_name(analysis, per_path, path, qual)
    for qual, scope in old_scopes.items():
        if qual not in new_scopes:
            analysis.cf.add((path, qual))
            if _import_time_scope(scope):
                analysis.am.add(path)
    old_classes = {c.qualname: c for c in old.classes}
    new_classes = {c.qualname: c for c in new.classes}
    for qual, cls in new_classes.items():
        prev = old_classes.get(qual)
        if prev is None:
            if is_test:
                analysis.wf.add(path)
        elif cls.skeleton != prev.skeleton or cls.body != prev.body:
            if _plain_class(old, qual) and _plain_class(new, qual):
                analysis.soft.add(path)  # only its readers notice
            else:
                analysis.am.add(path)
            _seed_class(analysis, per_path, index, old_scopes, new_scopes,
                        path, qual)
    for qual in old_classes:
        if qual not in new_classes:
            analysis.am.add(path)
            _seed_class(analysis, per_path, index, old_scopes, new_scopes,
                        path, qual)
    old_defs = _def_map(old)
    new_defs = _def_map(new)
    for name in set(old_defs) | set(new_defs):
        if old_defs.get(name) != new_defs.get(name):
            key = C.selection_name_key(path, name)
            analysis.seeds.add(key)
            analysis.names.add(key)
            per_path.add(key)
    if _star_seq(old) != _star_seq(new):
        analysis.wild.add(path)
    if sorted(_effect_fps(old)) != sorted(_effect_fps(new)):
        analysis.am.add(path)
    if per_path or path in analysis.am or path in analysis.soft:
        analysis.sk.add(path)
    if is_test and (per_path or path in analysis.am
                    or path in analysis.soft or path in analysis.wild):
        analysis.wf.add(path)
    return old


def _matches(refs, names, wild):
    for key in refs:
        if key in names or C.selection_key_path(key) in wild:
            return True
    return False


def _propagate(index, analysis, olds):
    """4.4: fixpoint over the index; finalises names/am/cf/af/wf."""
    names = analysis.names
    wild = analysis.wild
    am = analysis.am
    soft = analysis.soft
    cf = analysis.cf
    af = analysis.af
    growing = True
    while growing:
        growing = False
        for path, current in index.files.items():
            statements = current.index.statements
            for pos, refs in enumerate(current.statement_refs):
                if not _matches(refs, names, wild):
                    continue
                kind = statements[pos].kind if pos < len(statements) else ""
                if kind in _DEFINITION_KINDS:
                    bound = (current.statement_bound[pos]
                             if pos < len(current.statement_bound) else ())
                    for key in bound:
                        if key not in names:
                            names.add(key)
                            growing = True
                    if kind in ("def", "class") and path not in am:
                        # A def or class whose skeleton references a
                        # changed name is re-created differently at import
                        # (an annotation-driven dependency, a decorator
                        # argument): an import-time change (N1), unless
                        # only its callers can notice (a plain default).
                        name = (statements[pos].bound[0]
                                if pos < len(statements)
                                and len(statements[pos].bound) == 1
                                else None)
                        if name is not None and (
                                _local_def(current.index, name)
                                if kind == "def"
                                else _plain_class(current.index, name)):
                            soft.add(path)
                        else:
                            am.add(path)
                            growing = True
                    if kind == "def":
                        # It also affects the function itself; bound names
                        # alone never match a recorded (path, qual) id.
                        prefix = path + ":"
                        for key in bound:
                            qual = (key[len(prefix):]
                                    if key.startswith(prefix) else key)
                            af.add((path, qual))
                elif kind == "effect" and path not in am:
                    am.add(path)
                    growing = True
            for cls in current.index.classes:
                refs = current.class_refs.get(cls.qualname, frozenset())
                if not _matches(refs, names, wild):
                    continue
                key = C.selection_name_key(path, cls.qualname.split(".")[0])
                if key not in names:
                    names.add(key)
                    growing = True
                if path not in am:
                    if _plain_class(current.index, cls.qualname):
                        soft.add(path)  # read only by code using it
                    else:
                        am.add(path)  # a class body runs at import time
                        growing = True
                if (path, cls.qualname) not in cf:
                    cf.add((path, cls.qualname))
                    old = olds.get(path)
                    scopes = [s.qualname for s in current.index.scopes]
                    if old is not None:
                        scopes += [s.qualname for s in old.scopes]
                    prefix = cls.qualname + "."
                    for qual in scopes:
                        if qual.startswith(prefix):
                            cf.add((path, qual))
            for scope in current.index.scopes:
                refs = current.scope_refs.get(scope.qualname, frozenset())
                if _matches(refs, names, wild):
                    # Functions never propagate further.
                    af.add((path, scope.qualname))
    for path in index.test_files:
        if path in am or path in soft:
            analysis.wf.add(path)
            continue
        for key in names:
            if C.selection_key_path(key) == path:
                analysis.wf.add(path)
                break


def _analyze(index, stale_py, basemap, versions):
    """4.3 per-path diffs plus the 4.4 fixpoint for one signature."""
    analysis = _Analysis()
    olds = {}
    for path in sorted(stale_py):
        olds[path] = _diff_path(analysis, index, path, basemap, versions)
    _propagate(index, analysis, olds)
    return analysis


def _test_set(index):
    """Current runnable test files as a set (no per-call copy)."""
    raw = index.test_files
    return (raw if isinstance(raw, (set, frozenset)) else frozenset(raw))


def _reach(index, test_roots, seeds):
    """4.7: static reach plus the support-file root widening."""
    test_set = _test_set(index)
    found = set(C.selection_static_reach(index.reverse, seeds))
    found &= test_set
    for seed in seeds:
        current = index.files.get(seed)
        if current is None or not current.support:
            continue
        if seed.rsplit("/", 1)[-1] == "conftest.py":
            continue
        for root in test_roots:
            prefix = None if root in ("", ".") else root.rstrip("/") + "/"
            if prefix is None or seed == root or seed.startswith(prefix):
                for test in test_set:
                    if (prefix is None or test == root
                            or test.startswith(prefix)):
                        found.add(test)
    return frozenset(found)


def _resolve_function(vocabulary, fid):
    if (isinstance(fid, int) and not isinstance(fid, bool)
            and 0 <= fid < len(vocabulary.functions)):
        pid, qual = vocabulary.functions[fid]
        if (isinstance(pid, int) and not isinstance(pid, bool)
                and 0 <= pid < len(vocabulary.paths)):
            return (vocabulary.paths[pid], qual)
    return None


def _resolve_path(vocabulary, pid):
    if (isinstance(pid, int) and not isinstance(pid, bool)
            and 0 <= pid < len(vocabulary.paths)):
        return vocabulary.paths[pid]
    return None


def _context_ids(ctx):
    """Raw id lists of one context (arrays may be plain sequences)."""
    return (list(ctx.functions), list(ctx.modules), list(ctx.data))


def _context_paths(vocabulary, fixture_map, ctx, fids, lenient=False):
    """Resolve one context plus fixtures to path sets; None means corrupt.

    Returns (functions, modules, data) with functions as (path, qualname)
    pairs, or None when any id is unresolvable (the node is corrupt).
    ``lenient`` skips unresolvable ids and missing fixture records
    instead, keeping what resolves (positive evidence, chosen-record
    distance); it never returns None.
    """
    funcs, mods, data = set(), set(), set()
    func_ids, mod_ids, data_ids = _context_ids(ctx)
    for fid in func_ids:
        resolved = _resolve_function(vocabulary, fid)
        if resolved is None:
            if lenient:
                continue
            return None
        funcs.add(resolved)
    for pid in mod_ids:
        path = _resolve_path(vocabulary, pid)
        if path is None:
            if lenient:
                continue
            return None
        mods.add(path)
    for pid in data_ids:
        path = _resolve_path(vocabulary, pid)
        if path is None:
            if lenient:
                continue
            return None
        data.add(path)
    for fid in fids:
        record = fixture_map.get(fid)
        if record is None:
            if lenient:
                continue
            return None
        nested = _context_paths(vocabulary, fixture_map, record.deps, (),
                                lenient)
        if nested is None:
            return None
        funcs |= nested[0]
        mods |= nested[1]
        data |= nested[2]
    return (funcs, mods, data)


_T1 = C.SELECTION_TIER_RUN_NOW
_T2 = C.SELECTION_TIER_DEFERRED
_T1_ORDER = {code: pos for pos, code in enumerate(C.SELECTION_T1_CODES)}


@dataclass(frozen=True, slots=True)
class GateAudit:
    """R15 classification of one gate's failures against its plan."""

    previously_deferred: int
    failed: int
    learned: Mapping[str, frozenset[str]]
    unlearned: tuple[str, ...]
    misses: tuple[str, ...]


def _records_of(deps):
    """Every kept record per node id, newest first (None means one each)."""
    if deps.records is not None:
        return deps.records
    return {nodeid: (record,) for nodeid, record in deps.nodes.items()}


def plan(inputs):
    """Decide which recorded tests run now (T1) and which wait (T2)."""
    index = inputs.index
    deps = inputs.deps
    vocabulary = deps.vocabulary
    compatibility = inputs.compatibility
    audit = inputs.audit
    observers = inputs.observers
    conditional_mode = inputs.conditional
    # Spec R9 - deferral is measured since the gate, so every diff-derived
    # input uses the union; the precise clauses stay on the stale sets.
    changed_all = set(inputs.changed) | set(inputs.since_gate)
    changed_py = {c for c in changed_all if c.endswith(".py")}
    nonpy_paths = sorted(c for c in changed_all if not c.endswith(".py"))
    nonpy_changed = bool(nonpy_paths)
    test_roots = tuple(inputs.test_roots)
    test_set = _test_set(index)

    compat_runs = {rid: run for rid, run in deps.runs.items()
                   if run.compatibility == compatibility}
    stale_of, sig_of = {}, {}
    for rid, run in compat_runs.items():
        basemap = _baseline_map(run, vocabulary)
        stale_py, stale_data = _stale_sets(index, basemap,
                                           inputs.data_digests)
        stale_of[rid] = (basemap, stale_py, stale_data)
        sig_of[rid] = _signature(stale_py, basemap, stale_data)

    # D5: memoise analyses by stale signature, newest runs first; the
    # oldest runs past the cap become unusable (R23).
    by_newest = sorted(compat_runs,
                       key=lambda rid: (-compat_runs[rid].recorded_at, rid))
    memo = {}
    per_run = {}
    overcap = set()
    for rid in by_newest:
        sig = sig_of[rid]
        if sig in memo:
            per_run[rid] = memo[sig]
        elif len(memo) < C.SELECTION_MAX_SIGNATURES:
            basemap, stale_py, _ = stale_of[rid]
            analysis = _analyze(index, stale_py, basemap, inputs.versions)
            memo[sig] = analysis
            per_run[rid] = analysis
        else:
            overcap.add(rid)

    path_reach_memo = {}

    def path_reach(path):
        found = path_reach_memo.get(path)
        if found is None:
            found = path_reach_memo[path] = _reach(index, test_roots,
                                                   {path})
        return found

    # Per-run ambient facts: every ambient path (chosen-record distance),
    # unresolvable ids (taint), and the smallest stale ambient data file
    # (g-data; it replaces the old full_reason).
    ambient_bad = set()
    ambient_paths = {}
    ambient_data_via = {}
    for rid, run in compat_runs.items():
        stale_data = stale_of[rid][2]
        paths, stale_ambient = set(), set()
        for pid in list(run.ambient.data):
            path = _resolve_path(vocabulary, pid)
            if path is None:
                ambient_bad.add(rid)
            else:
                paths.add(path)
                if path in stale_data:
                    stale_ambient.add(path)
        for fid in list(run.ambient.functions):
            resolved = _resolve_function(vocabulary, fid)
            if resolved is None:
                ambient_bad.add(rid)
            else:
                paths.add(resolved[0])
        for pid in list(run.ambient.modules):
            path = _resolve_path(vocabulary, pid)
            if path is None:
                ambient_bad.add(rid)
            else:
                paths.add(path)
        ambient_paths[rid] = paths
        if stale_ambient:
            ambient_data_via[rid] = min(stale_ambient)

    # Per-signature precomputes: clause-b reach, the full static reach of
    # the stale or changed code, and the touched-path union are identical
    # across all runs sharing one signature.
    reach_b, reach_all, touch = {}, {}, {}
    for rid in per_run:
        sig = sig_of[rid]
        if sig in reach_b:
            continue
        _, stale_py, stale_data = stale_of[rid]
        reach_b[sig] = _reach(index, test_roots, set(stale_py) | changed_py)
        reach_all[sig] = C.selection_static_reach(
            index.reverse, set(stale_py) | changed_py)
        touch[sig] = (frozenset(set(stale_py) | set(stale_data))
                      | frozenset(changed_all))

    # Per-analysis ambient facts (g-shared via, g-reach via-by-file).
    # g-shared is per run (its own ambient context); am_via depends only on
    # the analysis, so it is memoised per analysis.
    shared_via = {}
    am_via = {}
    for rid, analysis in per_run.items():
        key_ = id(analysis)
        shared = []
        for fid in list(compat_runs[rid].ambient.functions):
            resolved = _resolve_function(vocabulary, fid)
            if resolved is None:
                continue
            path, qual = resolved
            if (path in analysis.pw or (path, qual) in analysis.cf
                    or (path, qual) in analysis.af):
                shared.append(path)
        for pid in list(compat_runs[rid].ambient.modules):
            path = _resolve_path(vocabulary, pid)
            if path is not None and (path in analysis.am
                                     or path in analysis.pw):
                shared.append(path)
        shared_via[rid] = min(shared) if shared else None
        if key_ in am_via:
            continue
        by_file = {}
        for path in sorted(analysis.am):
            for test in path_reach(path):
                by_file.setdefault(test, path)
        am_via[key_] = by_file

    b_via_memo = {}

    def b_static_via(rid, test_file):
        """Smallest stale or changed .py path that statically reaches the
        file, else the smallest changed non-.py path, else the file."""
        sig = sig_of[rid]
        by_file = b_via_memo.get(sig)
        if by_file is None:
            by_file = {}
            for path in sorted(stale_of[rid][1] | changed_py):
                for test in path_reach(path):
                    by_file.setdefault(test, path)
            b_via_memo[sig] = by_file
        found = by_file.get(test_file)
        if found is not None:
            return found
        return nonpy_paths[0] if nonpy_paths else test_file

    fallbacks = {}
    tiers = {}           # node id / test file -> (tier, code)
    via = {}
    learnable = {}
    conditional = {}     # node id -> chosen run id
    chosen_run = {}
    usable_of = {}
    paths_of = {}        # nodeid -> project paths whose code it ran
    counts = {}
    touched = set()      # passing, untiered nodes whose recorded code changed
    recorded = 0

    def _count(kind, label, nodeid):
        counts.setdefault((kind, label), set()).add(nodeid)

    def _t1(nodeid, code):
        tiers[nodeid] = (_T1, code)
        via.pop(nodeid, None)
        learnable.pop(nodeid, None)
        conditional.pop(nodeid, None)

    for nodeid, recs in _records_of(deps).items():
        if not recs:
            continue
        test_file = recs[0].test_file
        usable = []
        saw_compat = False
        for rec in recs:
            if rec.run_id not in compat_runs:
                continue
            saw_compat = True
            if rec.run_id not in overcap:
                usable.append(rec)
        if not saw_compat:
            fallbacks[nodeid] = _OTHER_CONFIG_REASON
            continue
        if test_file not in test_set:
            continue  # its file is gone: nothing to run
        if not usable:
            fallbacks[nodeid] = _CAP_REASON
            continue
        # Spec R22 - the record nearest the tree decides, then the newest, then
        # the smallest run id; every clause below reads only that record.
        if len(usable) == 1:
            node = usable[0]
        else:
            def distance(rec):
                lenient = _context_paths(vocabulary, deps.fixtures, rec.deps,
                                         list(rec.fixtures), lenient=True)
                rid_ = rec.run_id
                paths = ({p for p, _ in lenient[0]} | lenient[1]
                         | lenient[2] | ambient_paths[rid_] | {test_file})
                stale = stale_of[rid_][1] | stale_of[rid_][2]
                return (len(stale & paths), -compat_runs[rid_].recorded_at,
                        rid_)
            node = min(usable, key=distance)
        rid = node.run_id
        run = compat_runs[rid]
        sig = sig_of[rid]
        recorded += 1
        usable_of[nodeid] = test_file
        chosen_run[nodeid] = rid
        _, stale_py, stale_data = stale_of[rid]
        analysis = per_run[rid]
        node_fids = list(node.fixtures)
        missing_fixture = any(fid not in deps.fixtures for fid in node_fids)
        own = _context_paths(vocabulary, deps.fixtures, node.deps, node_fids)
        lenient = (own if own is not None else
                   _context_paths(vocabulary, deps.fixtures, node.deps,
                                  node_fids, lenient=True))
        funcs, mods, data = lenient
        paths_of[nodeid] = {p for p, _ in funcs} | set(mods)
        fixture_opaque = False
        if not missing_fixture:
            for fid in node_fids:
                if deps.fixtures[fid].deps.opaque:
                    fixture_opaque = True
        opaque_why = None
        if missing_fixture:
            opaque_why = _FIXTURE_REASON
        if (own is None or node.deps.opaque or fixture_opaque
                or run.ambient.opaque or rid in ambient_bad):
            opaque_why = opaque_why or _OPAQUE_REASON
        demoted = (deps.demotions.get(nodeid)
                   == _cur_digest(index, test_file))
        corrupt = own is None or rid in ambient_bad
        if opaque_why is not None or demoted:
            fallbacks[nodeid] = _DEMOTED_REASON if demoted else opaque_why

        if node.outcome not in C.SELECTION_PASSING_OUTCOMES:
            tiers[nodeid] = (_T1, "a")  # a non-passing record always runs
            continue

        # T1 clauses on the chosen record (R3: opaque and demoted nodes
        # get the precise ones, a/c/d/f; e is a static-style clause).
        precise_only = opaque_why is not None or demoted
        codes = set()
        counted = False
        if test_file in analysis.wf:
            codes.add("c")
            _count("file", test_file, nodeid)
            counted = True
        hit = False
        for path, qual in funcs:
            if path in analysis.pw:
                hit = True
                if not counted:
                    _count("module", path, nodeid)
            elif (path, qual) in analysis.cf:
                hit = True
                if not counted:
                    _count("function", f"{path}::{qual}", nodeid)
            elif (path, qual) in analysis.af:
                hit = True  # affected via propagation; no unit for AF
        if hit:
            codes.add("d")
            counted = True
        if not precise_only:
            for path in mods:
                if path in analysis.sk:
                    codes.add("e")
                    if not counted:
                        if path in analysis.am:
                            _count("module", path, nodeid)
                        else:
                            for key_ in analysis.seeds:
                                if C.selection_key_path(key_) == path:
                                    _count("name", key_, nodeid)
            if "e" in codes:
                counted = True
        for path in data:
            if path in stale_data:
                codes.add("f")
                if not counted:
                    _count("data", path, nodeid)
        # Spec R16 - an observer's learned or declared paths against the
        # chosen record's import-time changes.
        observer = False
        rows = observers.get(nodeid)
        if rows:
            ambient_changed = analysis.am | analysis.pw
            stale_all = stale_py | stale_data
            for row in rows:
                if row == C.SELECTION_OBSERVER_ALL:
                    if ambient_changed:
                        observer = True
                elif row in ambient_changed or (
                        row not in index.files and row in stale_all):
                    observer = True
        if observer and not conditional_mode:
            codes.add("o")

        # T2 clauses (only reached when no T1 code holds).
        t2 = None
        if shared_via[rid] is not None:
            t2 = ("g-shared", shared_via[rid])
        elif test_file in am_via[id(analysis)]:
            t2 = ("g-reach", am_via[id(analysis)][test_file])
        elif rid in ambient_data_via:
            t2 = ("g-data", ambient_data_via[rid])
        elif corrupt or ((opaque_why is not None or demoted) and (
                test_file in reach_b[sig] or stale_data or nonpy_changed
                or not reach_all[sig].isdisjoint(mods))):
            t2 = ("b-static", b_static_via(rid, test_file))

        if codes:
            _t1(nodeid, min(codes, key=_T1_ORDER.__getitem__))
            continue
        if observer and t2 is None:
            _t1(nodeid, "o")  # no T2 clause: running is always safe (D6)
            continue
        if t2 is not None:
            tiers[nodeid] = (_T2, t2[0])
            via[nodeid] = t2[1]
            ambient_or_pw = analysis.am | analysis.pw
            if ambient_or_pw:
                learnable[nodeid] = frozenset(ambient_or_pw)
            if observer:
                conditional[nodeid] = rid
            continue
        if ({p for p, _ in funcs} | set(mods) | set(data)) & touch[sig]:
            touched.add(nodeid)

    # Caller-local import-time changes select only the code that runs
    # them. A change that breaks the import shows in any test importing
    # the module, so one selected test must still import it (R4): in an
    # iteration run exactly one witness per uncovered path; the audit
    # plan keeps today's whole set.
    fallback_modules = set()
    for rid, analysis in per_run.items():
        soft = analysis.soft - analysis.am - analysis.pw
        if not soft:
            continue
        members = sorted(nodeid for nodeid in usable_of
                         if chosen_run[nodeid] == rid)
        t1_nodes = {n for n in members
                    if tiers.get(n, (None,))[0] == _T1}
        missing = set()
        for path in sorted(soft):
            reach = path_reach(path)
            if not any(nodeid in t1_nodes
                       and (path in paths_of.get(nodeid, ())
                            or usable_of[nodeid] in reach)
                       for nodeid in members):
                missing.add(path)
        if not missing:
            continue
        ambient_mods = {_resolve_path(vocabulary, pid)
                        for pid in list(compat_runs[rid].ambient.modules)}
        fallback_modules |= missing
        if audit:
            whole = not missing.isdisjoint(ambient_mods)
            reach = _reach(index, test_roots, missing)
            for nodeid in members:
                if nodeid in t1_nodes:
                    continue
                ran = missing & paths_of.get(nodeid, set())
                if whole or ran or usable_of[nodeid] in reach:
                    _t1(nodeid, "w")
                    for path in (ran or missing):
                        _count("module", path, nodeid)
            continue
        for path in sorted(missing):
            reach = path_reach(path)
            candidates = [nodeid for nodeid in members
                          if path in paths_of.get(nodeid, ())
                          or usable_of[nodeid] in reach]
            if not candidates and path in ambient_mods:
                candidates = members
            if not candidates:
                continue
            witness = min(candidates,
                          key=lambda nodeid: (usable_of[nodeid], nodeid))
            if tiers.get(witness, (None,))[0] != _T1:
                _t1(witness, "w")
            _count("module", path, witness)

    # 4.6: test files with no usable node. A new test file (changed
    # against a run baseline) is T1 n, whole; one merely reached by the
    # static rule is T2 u-static (R1, R9).
    usable_files = set(usable_of.values())
    static_all = _reach(index, test_roots, changed_py)
    cf_all, seeds_all, am_all = set(), set(), set()
    data_all, wf_all = set(), set()
    for analysis in memo.values():
        cf_all |= analysis.cf
        seeds_all |= analysis.seeds
        am_all |= analysis.am
        wf_all |= analysis.wf
    # 5.8 lists runnable test files: a removed path must never leak into
    # files/whole_files (or their units), however it entered WF.
    wf_all &= test_set
    unrecorded = set()
    n_files = set()
    for path in sorted(index.test_files):
        if path in usable_files:
            continue
        reached_static = path in static_all or nonpy_changed
        if reached_static:
            unrecorded.add(path)
            fallbacks[path] = _NO_RECORD_REASON
        if path in wf_all:
            tiers[path] = (_T1, "n")
            n_files.add(path)
        elif reached_static:
            tiers[path] = (_T2, "u-static")
            for source in sorted(changed_py):
                if path in path_reach(source):
                    via[path] = source
                    break
            else:
                via[path] = nonpy_paths[0]
    for _, _, stale_data in stale_of.values():
        data_all |= stale_data
    units = []
    for path, qual in cf_all:
        units.append(C.ChangedUnit(kind="function", label=f"{path}::{qual}",
                                   tests=len(counts.get(
                                       ("function", f"{path}::{qual}"),
                                       ()))))
    for key_ in seeds_all:
        units.append(C.ChangedUnit(
            kind="name", label=key_,
            tests=len(counts.get(("name", key_), ()))))
    for path in am_all | fallback_modules:
        units.append(C.ChangedUnit(
            kind="module", label=path,
            tests=len(counts.get(("module", path), ()))))
    for path in data_all:
        units.append(C.ChangedUnit(
            kind="data", label=path,
            tests=len(counts.get(("data", path), ()))))
    for path in wf_all:
        units.append(C.ChangedUnit(
            kind="file", label=path,
            tests=len(counts.get(("file", path), ()))))
    for path in unrecorded:
        units.append(C.ChangedUnit(
            kind="unrecorded", label=path,
            tests=len(counts.get(("unrecorded", path), ()))))
    units.sort(key=lambda unit: (-unit.tests, unit.label))

    t1_node_ids = {nodeid for nodeid in usable_of
                   if tiers.get(nodeid, (None,))[0] == _T1}
    files_set = ({usable_of[nodeid] for nodeid in t1_node_ids} | n_files
                 | {usable_of[nodeid] for nodeid in conditional}) & test_set
    # A changed test file runs whole only when every usable node in it
    # is T1; a file holding T2 or conditional nodes keeps them deselected.
    by_file = {}
    for nodeid, path in usable_of.items():
        by_file.setdefault(path, []).append(nodeid)
    whole_set = set(n_files)
    for path in wf_all & usable_files:
        if all(nodeid in t1_node_ids for nodeid in by_file[path]):
            whole_set.add(path)
    whole_set &= test_set
    deselect = sorted(nodeid for nodeid, path in usable_of.items()
                      if nodeid not in t1_node_ids
                      and path in files_set and path not in whole_set)
    total_files = len(index.test_files)
    if total_files:
        coverage = len({p for p in usable_files if p in index.test_files}
                       ) / total_files
    else:
        coverage = 0.0

    file_keys = [k for k in tiers if k not in usable_of]
    weights = {}
    if file_keys:
        inventory_counts = {}
        for nodeid in deps.inventory:
            path = C.selection_test_file(nodeid)
            inventory_counts[path] = inventory_counts.get(path, 0) + 1
        for path in file_keys:
            weights[path] = max(1, inventory_counts.get(path, 0))
    promoted = sum(1 for nodeid in usable_of
                   if tiers.get(nodeid) == (_T1, "o"))
    deferred_observers = sum(
        1 for nodeid in usable_of
        if nodeid in observers and tiers.get(nodeid, (None,))[0] == _T2)
    return C.SelectionDecision(
        files=tuple(sorted(files_set)),
        deselect=tuple(deselect),
        whole_files=tuple(sorted(whole_set)),
        selected=len(t1_node_ids),
        reached=len(touched - set(tiers)),
        recorded=recorded,
        total_files=total_files,
        units=tuple(units),
        fallbacks=tuple(sorted(fallbacks.items())),
        coverage=coverage,
        tiers=dict(sorted(tiers.items())),
        via=dict(sorted(via.items())),
        weights=dict(sorted(weights.items())),
        chosen=dict(sorted(chosen_run.items())),
        learnable=dict(sorted(learnable.items())),
        conditional=tuple(sorted(conditional.items())),
        observers_promoted=promoted,
        observers_deferred=deferred_observers,
    )


def needed_versions(index, deps, compatibility):
    """Baseline digests of stale .py paths across compatible runs."""
    out = set()
    for run in deps.runs.values():
        if run.compatibility != compatibility:
            continue
        basemap = _baseline_map(run, deps.vocabulary)
        for path in set(basemap) | set(index.files):
            if not path.endswith(".py"):
                continue
            if basemap.get(path) != _cur_digest(index, path):
                digest = basemap.get(path)
                if digest is not None:
                    out.add(digest)
    return frozenset(out)


def needed_data_paths(deps, compatibility):
    """Every recorded data path of compatible runs."""
    out = set()
    for run in deps.runs.values():
        if run.compatibility != compatibility:
            continue
        basemap = _baseline_map(run, deps.vocabulary)
        for path in basemap:
            if not path.endswith(".py"):
                out.add(path)
    return frozenset(out)


def is_selected(decision, nodeid):
    """True when the iteration run executes this node id (unknown ids run)."""
    return (C.selection_test_file(nodeid) in set(decision.files)
            and nodeid not in set(decision.deselect))


def _chosen_record(decision, deps, nodeid):
    records = _records_of(deps).get(nodeid, ())
    wanted = decision.chosen.get(nodeid)
    if wanted is not None:
        for record in records:
            if record.run_id == wanted:
                return record
    if records:
        return records[0]
    return deps.nodes.get(nodeid)


def audit_classify(decision, deps, failed, ran):
    """R15: classify a gate's failures against the plan of its snapshot.

    (a) a failing T2 node with paths to learn; (b) a failing T2 node with
    none, or any failing id under a T2 file key; (c) a failing node whose
    chosen record passed and that no tier or selection covered (a miss);
    (d) a failing T1 node produces nothing.
    """
    failed_set = set(failed)
    ran_set = set(ran)
    tiers = decision.tiers
    t2_files = {key_ for key_, (tier_, _) in tiers.items()
                if tier_ == _T2 and key_ not in decision.chosen}
    deferred = set()
    for nodeid in ran_set:
        entry = tiers.get(nodeid)
        if entry is not None and entry[0] == _T2 and nodeid in decision.chosen:
            deferred.add(nodeid)
        elif (nodeid not in decision.chosen
              and C.selection_test_file(nodeid) in t2_files):
            deferred.add(nodeid)
    learned, unlearned, misses = {}, [], []
    for nodeid in sorted(failed_set):
        if (nodeid not in decision.chosen
                and C.selection_test_file(nodeid) in t2_files):
            unlearned.append(nodeid)
            continue
        record = _chosen_record(decision, deps, nodeid)
        if record is None or record.outcome not in C.SELECTION_PASSING_OUTCOMES:
            continue
        entry = tiers.get(nodeid)
        if entry is None:
            if not is_selected(decision, nodeid):
                misses.append(nodeid)
        elif entry[0] == _T2:
            paths = decision.learnable.get(nodeid)
            if paths:
                learned[nodeid] = paths
            else:
                unlearned.append(nodeid)
    return GateAudit(
        previously_deferred=len(deferred),
        failed=len(deferred & failed_set),
        learned=learned,
        unlearned=tuple(sorted(unlearned)),
        misses=tuple(sorted(misses)),
    )


def audit_misses(decision, deps, failed):
    """Failing tests with a passing chosen record the plan skipped."""
    return audit_classify(decision, deps, failed, ()).misses
