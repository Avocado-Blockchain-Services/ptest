"""Dependency-recorded test selection planner (planner v2).

Pure functions over the frozen contracts model. The decision procedure
is normative in design section 4: usable records (4.1), per-run
staleness (4.2), per-path diff (4.3), propagation fixpoint (4.4),
per-test clauses (4.5), unrecorded files (4.6), static reach (4.7) and
output assembly (4.8), plus the self-audit miss computation.

Purity contract: no I/O, no clock, no project-code imports. Every
output tuple is sorted, so identical inputs give identical outputs.
Unresolvable vocabulary ids make the affected node opaque (static
rule), never silently dropped.
"""
from __future__ import annotations

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


def _context_paths(vocabulary, fixture_map, ctx, fids):
    """Resolve one context plus fixtures to path sets; None means corrupt.

    Returns (functions, modules, data) with functions as (path, qualname)
    pairs, or None when any id is unresolvable (the node turns opaque).
    """
    funcs, mods, data = set(), set(), set()
    func_ids, mod_ids, data_ids = _context_ids(ctx)
    for fid in func_ids:
        resolved = _resolve_function(vocabulary, fid)
        if resolved is None:
            return None
        funcs.add(resolved)
    for pid in mod_ids:
        path = _resolve_path(vocabulary, pid)
        if path is None:
            return None
        mods.add(path)
    for pid in data_ids:
        path = _resolve_path(vocabulary, pid)
        if path is None:
            return None
        data.add(path)
    for fid in fids:
        record = fixture_map.get(fid)
        if record is None:
            return None
        nested = _context_paths(vocabulary, fixture_map, record.deps, ())
        if nested is None:
            return None
        funcs |= nested[0]
        mods |= nested[1]
        data |= nested[2]
    return (funcs, mods, data)


def plan(inputs):
    """Decide which recorded tests must run (design section 4)."""
    index = inputs.index
    deps = inputs.deps
    vocabulary = deps.vocabulary
    compatibility = inputs.compatibility
    changed = set(inputs.changed)
    changed_py = {c for c in changed if c.endswith(".py")}
    nonpy_changed = any(not c.endswith(".py") for c in changed)
    test_roots = tuple(inputs.test_roots)

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
    # oldest runs past the cap become unrecorded (static rule).
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

    # Ambient data changes anywhere force the full suite (clause g).
    # Unresolvable ambient ids taint the run: its nodes turn opaque.
    full_candidates = set()
    ambient_bad = set()
    for rid, run in compat_runs.items():
        _, _, stale_data = stale_of[rid]
        ambient_data = set()
        for pid in list(run.ambient.data):
            path = _resolve_path(vocabulary, pid)
            if path is None:
                ambient_bad.add(rid)
            else:
                ambient_data.add(path)
        for fid in list(run.ambient.functions):
            if _resolve_function(vocabulary, fid) is None:
                ambient_bad.add(rid)
        for path in ambient_data & stale_data:
            full_candidates.add(path)
    full_reason = None
    if full_candidates:
        full_reason = f"ambient data file {min(full_candidates)} changed"

    # Per-run/per-signature precomputes (D5/A3): the clause-b reach set,
    # the clause-g ambient reach set plus its changed-function labels,
    # and the stale/changed path union are identical for every node of a
    # run, so they are built once here; per node only membership tests
    # remain.
    reach_b = {}
    reach_all = {}
    touch = {}
    ambient_pre = {}
    for rid in compat_runs:
        _, stale_py, stale_data = stale_of[rid]
        reach_b[rid] = _reach(index, test_roots, set(stale_py) | changed_py)
        reach_all[rid] = C.selection_static_reach(
            index.reverse, set(stale_py) | changed_py)
        touch[rid] = (frozenset(set(stale_py) | set(stale_data))
                      | frozenset(changed))
    for rid, analysis in per_run.items():
        ambient_paths = set(analysis.am)
        ambient_labels = []
        # Changed code the run's ambient context executed (collection,
        # imports, app factories) shaped state every test of that run
        # shared, however the module was reached (importlib, plugins):
        # the whole run is affected, not only static importers.
        shared = []
        for fid in list(compat_runs[rid].ambient.functions):
            # Unresolvable ids already tainted this run opaque above.
            resolved = _resolve_function(vocabulary, fid)
            if resolved is None:
                continue
            path, qual = resolved
            if (path in analysis.pw or (path, qual) in analysis.cf
                    or (path, qual) in analysis.af):
                ambient_paths.add(path)
                shared.append(("function", f"{path}::{qual}"))
                if (path, qual) in analysis.cf:
                    ambient_labels.append(f"{path}::{qual}")
        for pid in list(compat_runs[rid].ambient.modules):
            path = _resolve_path(vocabulary, pid)
            if path is not None and (path in analysis.am
                                     or path in analysis.pw):
                shared.append(("module", path))
        ambient_pre[rid] = (_reach(index, test_roots, ambient_paths),
                            tuple(ambient_labels), tuple(shared))

    fallbacks = {}
    selected_files = set()
    selected_nodes = set()
    usable_of = {}
    paths_of = {}       # nodeid -> project paths whose code it ran
    counts = {}
    reached = set()     # passing nodes whose recorded code changed
    recorded = 0

    def _count(kind, label, nodeid):
        counts.setdefault((kind, label), set()).add(nodeid)

    for nodeid, node in deps.nodes.items():
        run = compat_runs.get(node.run_id)
        if run is None:
            fallbacks[nodeid] = _OTHER_CONFIG_REASON
            continue
        if node.test_file not in index.test_files:
            continue  # its file is gone: nothing to run
        if node.run_id in overcap:
            fallbacks[nodeid] = _CAP_REASON
            continue
        recorded += 1
        usable_of[nodeid] = node.test_file
        _, _, stale_data = stale_of[node.run_id]
        analysis = per_run[node.run_id]
        node_fids = list(node.fixtures)
        missing_fixture = any(fid not in deps.fixtures for fid in node_fids)
        own = _context_paths(vocabulary, deps.fixtures, node.deps, node_fids)
        if own is not None:
            paths_of[nodeid] = {p for p, _ in own[0]} | set(own[1])
        fixture_opaque = False
        if not missing_fixture:
            for fid in node_fids:
                if deps.fixtures[fid].deps.opaque:
                    fixture_opaque = True
        opaque_why = None
        if missing_fixture:
            opaque_why = _FIXTURE_REASON
        if (own is None or node.deps.opaque or fixture_opaque
                or run.ambient.opaque or node.run_id in ambient_bad):
            opaque_why = opaque_why or _OPAQUE_REASON
        demoted = (deps.demotions.get(nodeid)
                   == _cur_digest(index, node.test_file))

        def _select():
            selected_nodes.add(nodeid)
            selected_files.add(node.test_file)

        if node.outcome not in C.SELECTION_PASSING_OUTCOMES:
            _select()  # clause a: a non-passing outcome always runs
            continue
        if opaque_why is not None or demoted:
            # Clause b is definitive for opaque/demoted nodes (N3): the
            # static rule alone decides, and no precise clause applies.
            fallbacks[nodeid] = _DEMOTED_REASON if demoted else opaque_why
            # A recorded module that statically reaches the change counts
            # like the test file: it covers the entry module of a spawned
            # project interpreter, whose imports run in the child.
            if (node.test_file in reach_b[node.run_id]
                    or stale_data or nonpy_changed
                    or (own is not None
                        and not reach_all[node.run_id].isdisjoint(own[1]))):
                _select()
            elif own is not None:
                funcs, mods, data = own
                dep_paths = ({p for p, _ in funcs} | set(mods) | set(data))
                if dep_paths & touch[node.run_id]:
                    reached.add(nodeid)
            continue
        if node.test_file in analysis.wf:
            _select()  # clause c
            _count("file", node.test_file, nodeid)
            continue
        funcs, mods, data = own
        hit = False
        for path, qual in funcs:
            if path in analysis.pw:
                hit = True
                _count("module", path, nodeid)
            elif (path, qual) in analysis.cf:
                hit = True
                _count("function", f"{path}::{qual}", nodeid)
            elif (path, qual) in analysis.af:
                hit = True  # affected via propagation; no unit for AF
        if hit:
            _select()  # clause d
            continue
        for path in mods:
            if path in analysis.sk:
                hit = True  # clause e
                if path in analysis.am:
                    _count("module", path, nodeid)
                else:
                    for key in analysis.seeds:
                        if C.selection_key_path(key) == path:
                            _count("name", key, nodeid)
        if hit:
            _select()
            continue
        for path in data:
            if path in stale_data:
                hit = True  # clause f
                _count("data", path, nodeid)
        if hit:
            _select()
            continue
        ambient_reach, ambient_labels, shared = ambient_pre[node.run_id]
        if shared:
            for kind, label in shared:
                _count(kind, label, nodeid)
            _select()  # clause g: the run's shared import-time state
            continue
        if node.test_file in ambient_reach:
            # Clause g selects first; units count only nodes it selected
            # (4.8: each unit carries the count of nodes it selected).
            for label in ambient_labels:
                _count("function", label, nodeid)
            _select()  # clause g
            continue
        dep_paths = ({p for p, _ in funcs} | set(mods) | set(data))
        if dep_paths & touch[node.run_id]:
            reached.add(nodeid)

    # Caller-local import-time changes select only the code that runs
    # them. A change that breaks the import shows in any test importing
    # the module, so one selected test must still import it; otherwise
    # the path follows the import-time rule for that run (static reach,
    # plus the whole run when its ambient imported the module).
    fallback_modules = set()
    for rid, analysis in per_run.items():
        soft = analysis.soft - analysis.am - analysis.pw
        if not soft:
            continue
        members = [nodeid for nodeid, path in usable_of.items()
                   if deps.nodes[nodeid].run_id == rid]
        missing = set()
        for path in sorted(soft):
            reach = _reach(index, test_roots, {path})
            if not any(nodeid in selected_nodes
                       and (path in paths_of.get(nodeid, ())
                            or usable_of[nodeid] in reach)
                       for nodeid in members):
                missing.add(path)
        if not missing:
            continue
        ambient_mods = {_resolve_path(vocabulary, pid)
                        for pid in list(compat_runs[rid].ambient.modules)}
        whole = not missing.isdisjoint(ambient_mods)
        reach = _reach(index, test_roots, missing)
        fallback_modules |= missing
        for nodeid in members:
            if nodeid in selected_nodes:
                continue
            ran = missing & paths_of.get(nodeid, set())
            if whole or ran or usable_of[nodeid] in reach:
                selected_nodes.add(nodeid)
                selected_files.add(usable_of[nodeid])
                for path in (ran or missing):
                    _count("module", path, nodeid)

    # 4.6: test files with no usable node follow the static rule (N2).
    usable_files = set(usable_of.values())
    static_all = _reach(index, test_roots, changed_py)
    unrecorded = set()
    for path in sorted(index.test_files):
        if path in usable_files:
            continue
        if path in static_all or nonpy_changed:
            unrecorded.add(path)
            fallbacks[path] = _NO_RECORD_REASON

    cf_all, seeds_all, am_all = set(), set(), set()
    data_all, wf_all = set(), set()
    for analysis in memo.values():
        cf_all |= analysis.cf
        seeds_all |= analysis.seeds
        am_all |= analysis.am
        wf_all |= analysis.wf
    # 5.8 lists runnable test files: a removed path must never leak into
    # files/whole_files (or their units), however it entered WF.
    test_set = _test_set(index)
    wf_all &= test_set
    for _, _, stale_data in stale_of.values():
        data_all |= stale_data
    units = []
    for path, qual in cf_all:
        units.append(C.ChangedUnit(kind="function", label=f"{path}::{qual}",
                                   tests=len(counts.get(
                                       ("function", f"{path}::{qual}"),
                                       ()))))
    for key in seeds_all:
        units.append(C.ChangedUnit(
            kind="name", label=key,
            tests=len(counts.get(("name", key), ()))))
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

    files = sorted((selected_files | wf_all | unrecorded) & test_set)
    whole = sorted((wf_all | unrecorded) & test_set)
    whole_set = set(whole)
    files_set = set(files)
    deselect = sorted(nodeid for nodeid, path in usable_of.items()
                      if nodeid not in selected_nodes
                      and path in files_set and path not in whole_set)
    total_files = len(index.test_files)
    if total_files:
        coverage = len({p for p in usable_files if p in index.test_files}
                       ) / total_files
    else:
        coverage = 0.0
    return C.SelectionDecision(
        full_reason=full_reason,
        files=tuple(files),
        deselect=tuple(deselect),
        whole_files=tuple(whole),
        selected=len(selected_nodes),
        reached=len(reached - selected_nodes),
        recorded=recorded,
        total_files=total_files,
        units=tuple(units),
        fallbacks=tuple(sorted(fallbacks.items())),
        coverage=coverage,
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
    """True when the decision runs this node id (unknown ids run)."""
    if decision.full_reason is not None:
        return True
    return (C.selection_test_file(nodeid) in set(decision.files)
            and nodeid not in set(decision.deselect))


def audit_misses(decision, deps, failed):
    """Failing tests with a passing prior record the plan skipped."""
    out = []
    for nodeid in failed:
        record = deps.nodes.get(nodeid)
        if record is None:
            continue
        if record.outcome not in C.SELECTION_PASSING_OUTCOMES:
            continue
        if is_selected(decision, nodeid):
            continue
        out.append(nodeid)
    return tuple(sorted(out))
