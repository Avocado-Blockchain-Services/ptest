"""Dynamic-selection integration glue (T5).

Static ``impact.plan`` plus the dynamic refinement step, the recording
environment for operations, and the post-run ingest/audit/update. The
engine reaches T1-T3 only through the module-level lazy accessors below,
so unit tests inject fakes there and the readiness-gated E2E covers the
real T1-T4 wiring.

A ``dynamic_ok`` verdict whose ``project_index`` is None (a stubbed or
foreign planner) builds the index once with the planning key and cache;
this is not a fallback and prints no reason. Every other failure falls
back to the static verdict with a frozen reason (design 2.8); falling
back is never an error.

``preview`` has no stderr output and performs no record writes (only
parse-cache puts inside the planning context). ``after_run`` never
raises.

0.7.0: a dynamic plan never promotes itself to the full suite (R6); it
reports what runs now (T1) and what waits for the gate (T2) through
``Impact.selection``, measured since the gate marker (R9). A gate learns
observers and writes the marker in its single store transaction.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import platform
import stat
import time
from pathlib import Path

from . import contracts as C
from . import progress
from . import render


# --- lazy accessors (T1-T3 seam; tests monkeypatch these) -------------------

def _impact():
    from . import impact
    return impact


def _source_index():
    from . import source_index
    return source_index


def _planner():
    from . import selection_planner
    return selection_planner


def _store():
    from . import selection_store
    return selection_store


def _ingest():
    from . import selection_ingest
    return selection_ingest


def _source_key(domain: C.DomainPaths) -> bytes | None:
    """The domain fingerprint key, or None before any run created it."""
    try:
        from . import source
    except ImportError:
        return None
    try:
        return source._key(domain)
    except Exception:
        return None


def _close_quietly(store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _open_store(domain, project_id, *, create):
    """Open the selection store, or None when it is unavailable."""
    store, _ = _open_store_reason(domain, project_id, create=create)
    return store


def _open_store_reason(domain, project_id, *, create):
    """Open the selection store with its failure class.

    Returns ``(store_or_None, never_created)``. ``never_created`` is
    True only when the store module reports ``state-unavailable`` for a
    store that was never created (a first run, an upgrade, or
    ``dynamic`` newly enabled) as opposed to a genuinely broken store.
    """
    try:
        store_mod = _store()
    except Exception:
        return None, False
    try:
        return store_mod.open_store(domain, project_id, create=create), False
    except Exception as exc:
        if getattr(exc, "code", None) == "state-unavailable" and str(
                getattr(exc, "message", exc)).endswith("does not exist"):
            return None, True
        if create and _store_damaged(exc):
            # The store is a derived cache: a damaged file would otherwise
            # pin every later run to the static rule. A store from another
            # schema version is not damaged and is never removed.
            try:
                if store_mod.remove_store(domain, project_id):
                    return store_mod.open_store(
                        domain, project_id, create=True), False
            except Exception:
                pass
        return None, False


def _store_damaged(exc: BaseException) -> bool:
    """True for a store SQLite cannot read, not for an unknown schema."""
    if getattr(exc, "code", None) != "coordinator-corrupt":
        return False
    message = str(getattr(exc, "message", exc))
    return message.endswith(" is corrupt") or message.endswith(
        " is unreadable") or message.endswith(" cannot be initialised")


# --- static reasons (frozen, design 2.8) ------------------------------------

_NO_RECORDS = "no dependency records yet — any run records them"
_DYNAMIC_FALSE = "dynamic = false in .ptest.toml"
_STORE_DOWN = "dependency store unavailable"
_OTHER_CONFIG = "dependency records are from another configuration"
_PLANNER_FAILED = "dynamic planner failed"


def _python_reason(version) -> str:
    return (f"test Python {version[0]}.{version[1]} cannot record "
            f"dependencies (needs 3.12+)")


def _inactive_reason(reason) -> str:
    return f"recording was unavailable: {reason}"


# --- planning context --------------------------------------------------------

@contextlib.contextmanager
def planning(domain: C.DomainPaths, config: C.Config):
    """Set ``impact.PLANNING`` to the key and store-as-cache for one plan.

    A no-op when the planner has no ``PLANNING`` slot (a stubbed or older
    impact): routing and notes stay byte-identical to 0.4. Creates the
    store when absent so the parse cache persists from the first plan (an
    empty store still plans as "no dependency records yet"); never raises.
    """
    try:
        impact = _impact()
    except ImportError:
        yield None
        return
    slot = getattr(impact, "PLANNING", None)
    if slot is None:
        yield None
        return
    try:
        key = _source_key(domain)
    except Exception:
        key = None
    cache = None
    if key is not None:
        cache = _open_store(domain, config.project_id, create=True)
    try:
        context = C.PlanningContext(key=key, cache=cache)
    except Exception:
        if cache is not None:
            _close_quietly(cache)
        yield None
        return
    token = None
    try:
        token = slot.set(context)
    except Exception:
        if cache is not None:
            _close_quietly(cache)
        yield None
        return
    try:
        yield context
    finally:
        try:
            slot.reset(token)
        except Exception:
            pass
        if cache is not None:
            _close_quietly(cache)


# --- compatibility fingerprint ----------------------------------------------

_LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "pyproject.toml",
              "setup.py", "setup.cfg", "pytest.ini", "tox.ini",
              ".ptest.toml", ".python-version")

_FILE_READ_CAP = 64 * 1024 * 1024


def _digest_bounded(key: bytes, path: Path, cap: int) -> str | None:
    """Keyed digest of a whole regular non-link file, streamed.

    None when the file is absent or unreadable; ``"?"`` when it is larger
    than ``cap`` bytes. A digest never covers only a prefix: a change past
    the cap must not leave the digest equal (N1).
    """
    try:
        stamp = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        mac = hmac.new(key, digestmod=hashlib.sha256)
        total = 0
        while True:
            piece = os.read(fd, 65536)
            if not piece:
                break
            total += len(piece)
            if total > cap:
                return "?"
            mac.update(piece)
        return mac.hexdigest()
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _compat_digest(digest: str) -> str:
    """An oversize input never matches a stored fingerprint (records unusable,
    static rule), because a prefix cannot prove the file unchanged."""
    if digest == "?":
        return "?" + os.urandom(16).hex()
    return digest


def compatibility_fingerprint(key: bytes, config: C.Config,
                              project_root) -> str:
    """HMAC over the selection protocol, policy, runner, platform and the
    keyed digests of root lockfiles, configs and literal full_triggers.

    Identical at plan and ingest time for unchanged inputs; any byte
    change in a listed file changes it.
    """
    root = Path(project_root)
    entries: list[list[str]] = []
    for name in _LOCKFILES:
        digest = _digest_bounded(key, root / name, _FILE_READ_CAP)
        if digest is None:
            continue
        entries.append([name, _compat_digest(digest)])
    try:
        reqs = sorted(path.name for path in root.glob("requirements*.txt"))
    except OSError:
        reqs = []
    for name in reqs:
        if name in _LOCKFILES:
            continue
        digest = _digest_bounded(key, root / name, _FILE_READ_CAP)
        if digest is None:
            continue
        entries.append([name, _compat_digest(digest)])
    runner = config.runner
    payload = {
        "protocol": C.SELECTION_PROTOCOL,
        "policy": hashlib.sha256(repr(config.selection).encode()).hexdigest(),
        "runner": [runner.kind.value, list(runner.launcher),
                   list(runner.args)],
        "platform": [platform.system(), platform.machine()],
        "files": sorted(entries),
        "triggers": sorted(config.selection.full_triggers),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


# --- tiers: counting and the selection report -------------------------------

_T1 = C.SELECTION_TIER_RUN_NOW
_T2 = C.SELECTION_TIER_DEFERRED
_DETAIL_FILES = 20


@dataclasses.dataclass(frozen=True, slots=True)
class _Tally:
    """What one plan runs now and defers, counted in tests (R30, R32, R34)."""

    run_now: int
    deferred: int
    t1_codes: dict
    t2_codes: dict
    by_reason: dict
    by_file: dict
    conditional: tuple
    promoted: int
    observers_deferred: int


def _under_scope(path: str, scope) -> bool:
    if scope is None:
        return True
    return any(folder == "" or path == folder
               or path.startswith(folder + "/") for folder in scope)


def _safe_path(path, fallback: str) -> str:
    return path if C.selection_relpath_safe(path) else fallback


def _tally(decision, *, kept, kept_conditional, observed,
           scope=None, whole=frozenset()) -> _Tally:
    """Count one decision's tiers, honouring what the run request kept.

    ``kept`` is the deselect the request can bind; ids the decision wanted
    to deselect but the request gave up (R5) run, so they count as run and
    leave the deferred counts. ``scope`` restricts every count to test
    files under the given folders (R40). Test files in ``whole`` run
    whole (``ptest <folder> <file>``), so their nodes count as run and
    never as deferred or conditional (R30).
    """
    tiers = getattr(decision, "tiers", None) or {}
    via = getattr(decision, "via", None) or {}
    weights = getattr(decision, "weights", None) or {}
    given_up = frozenset(getattr(decision, "deselect", ()) or ()) \
        - frozenset(kept)
    run_now = deferred = promoted = observers_deferred = 0
    t1_codes: dict = {}
    t2_codes: dict = {}
    by_reason: dict = {}
    by_file: dict = {}
    for key, entry in tiers.items():
        tier, code = entry
        is_file = "::" not in key
        path = key if is_file else C.selection_test_file(key)
        if not _under_scope(path, scope):
            continue
        weight = max(1, int(weights.get(key, 1))) if is_file else 1
        if tier != _T1 and path in whole:
            run_now += weight
        elif tier == _T1:
            run_now += weight
            t1_codes[code] = t1_codes.get(code, 0) + weight
            if code == "o":
                promoted += 1
        elif key in given_up:
            run_now += 1
        else:
            deferred += weight
            t2_codes[code] = t2_codes.get(code, 0) + weight
            reason_path = _safe_path(via.get(key) or path, path)
            by_reason[(code, reason_path)] = \
                by_reason.get((code, reason_path), 0) + weight
            by_file[path] = by_file.get(path, 0) + weight
            if not is_file and key in observed:
                observers_deferred += 1
    for nodeid in given_up:
        if nodeid not in tiers and _under_scope(
                C.selection_test_file(nodeid), scope):
            run_now += 1
    conditional = []
    for nodeid, _run in getattr(decision, "conditional", ()) or ():
        if (not _under_scope(C.selection_test_file(nodeid), scope)
                or C.selection_test_file(nodeid) in whole):
            continue
        if nodeid in given_up:
            promoted += 1
        elif nodeid in kept_conditional:
            entry = tiers.get(nodeid)
            if entry is not None and entry[0] == _T2:
                path = C.selection_test_file(nodeid)
                conditional.append(
                    (nodeid, entry[1],
                     _safe_path(via.get(nodeid) or path, path)))
    return _Tally(run_now=run_now, deferred=deferred, t1_codes=t1_codes,
                  t2_codes=t2_codes, by_reason=by_reason, by_file=by_file,
                  conditional=tuple(sorted(conditional)), promoted=promoted,
                  observers_deferred=observers_deferred)


def _report(**fields):
    """A ``SelectionReport`` for an iteration run, or None when the fields
    do not validate (a report problem never fails a run)."""
    try:
        return C.SelectionReport(iteration=True, **fields)
    except (TypeError, ValueError):
        return None


def _dynamic_report(tally: _Tally, *, gate_recorded: bool):
    reasons = tuple(sorted(
        ((code, path, tests) for (code, path), tests
         in tally.by_reason.items() if tests > 0),
        key=lambda item: (C.SELECTION_T2_CODES.index(item[0]), item[1])))
    by_file = tuple(sorted(
        (path, tests) for path, tests in tally.by_file.items() if tests > 0))
    report = _report(
        engine="dynamic", run_now=tally.run_now, deferred=tally.deferred,
        deferred_by_reason=reasons, deferred_by_file=by_file,
        observers_promoted=tally.promoted,
        observers_deferred=tally.observers_deferred,
        gate_recorded=gate_recorded, conditional=tally.conditional)
    if report is None:
        report = _report(engine="dynamic", run_now=tally.run_now,
                         deferred=None, gate_recorded=gate_recorded)
    return report


def _static_report(static, *, gate_recorded: bool = False,
                   engine: str | None = "static"):
    """The report of a run the dynamic planner did not decide (R34 matrix)."""
    kind = getattr(static, "kind", "")
    if kind == "full":
        return _report(engine=engine, run_now=None, deferred=0,
                       full_reason=getattr(static, "reason", "") or None,
                       gate_recorded=gate_recorded)
    if kind in ("selected", "none"):
        return _report(engine=engine, run_now=None, deferred=None,
                       gate_recorded=gate_recorded)
    return None


# --- refine -----------------------------------------------------------------

@dataclasses.dataclass(frozen=True, slots=True)
class _PlanFacts:
    """Inputs of the R32 ``-v`` lines that are not in the decision."""

    declared: int
    learned: int
    since_count: int
    gate_age: float | None


def _codes_text(counts: dict, order) -> str:
    parts = [f"{code} {counts[code]}" for code in order if counts.get(code)]
    return ", ".join(parts) if parts else "none"


def _tier_lines(tally: _Tally, facts: _PlanFacts) -> list:
    """R32: tiers, deferred top, observers and since-gate lines."""
    lines = [f"tiers: {tally.run_now} run now "
             f"({_codes_text(tally.t1_codes, C.SELECTION_T1_CODES)}) · "
             f"{tally.deferred} deferred "
             f"({_codes_text(tally.t2_codes, C.SELECTION_T2_CODES)})"]
    if tally.deferred > 0:
        top = sorted(tally.by_file.items(),
                     key=lambda item: (-item[1], item[0]))[:_DETAIL_FILES]
        lines.append("deferred top: " + " · ".join(
            f"{render.terminal_text(path)} {tests}" for path, tests in top))
    lines.append(f"observers: {facts.declared} declared · "
                 f"{facts.learned} learned · {tally.promoted} promoted · "
                 f"{tally.observers_deferred} deferred")
    if facts.gate_age is None:
        lines.append("since gate: no gate recorded")
    else:
        lines.append(f"since gate: {C.plural(facts.since_count, 'path')} · "
                     f"gate {progress.format_duration(facts.gate_age)} ago")
    return lines


def _detail_lines(decision, meta, tally: _Tally | None = None,
                  facts: _PlanFacts | None = None) -> tuple[str, ...]:
    """The ``-v selection:`` bodies for one dynamic decision (unescaped)."""
    try:
        coverage = f"{float(decision.coverage):.0%}"
    except (TypeError, ValueError):
        coverage = "0%"
    size = progress.format_store_bytes(getattr(meta, "size_bytes", 0))
    newest = getattr(meta, "newest_recorded_at", None)
    if isinstance(newest, (int, float)) and not isinstance(newest, bool):
        age = progress.format_duration(max(0.0, time.time() - newest))
    else:
        age = "unknown"
    lines = [f"engine dynamic · "
             f"{C.plural(getattr(decision, 'recorded', 0), 'test')} recorded · "
             f"coverage {coverage} of test files · store {size} · "
             f"newest {age} ago"]
    if tally is not None and facts is not None:
        lines.extend(_tier_lines(tally, facts))
    for unit in list(getattr(decision, "units", ()) or [])[:20]:
        lines.append(f"changed {getattr(unit, 'kind', '?')} "
                     f"{getattr(unit, 'label', '?')} → "
                     f"{C.plural(getattr(unit, 'tests', 0), 'test')}")
    rest_units = len(getattr(decision, "units", ()) or ()) - 20
    if rest_units > 0:
        lines.append(f"… +{rest_units} more changed units")
    for target, reason in list(getattr(decision, "fallbacks", ()) or [])[:20]:
        lines.append(f"fallback {target}: {reason}")
    rest_fallbacks = len(getattr(decision, "fallbacks", ()) or ()) - 20
    if rest_fallbacks > 0:
        lines.append(f"… +{rest_fallbacks} more fallbacks")
    return tuple(lines)


def _replace(static, **fields):
    """Refine one verdict, tolerating stubbed impact shapes.

    Fields the verdict's class does not have are dropped (a stub that
    mirrors an older ``Impact``); any other failure keeps the verdict.
    """
    if dataclasses.is_dataclass(static):
        names = {field.name for field in dataclasses.fields(static)}
        fields = {name: value for name, value in fields.items()
                  if name in names}
        try:
            return dataclasses.replace(static, **fields)
        except (TypeError, ValueError):
            return static
    return static


def _static_fallback(static, reason: str, *, gate_recorded: bool = False,
                     report_only: bool = False):
    """Keep the static verdict, naming why (design 2.8).

    ``report_only`` (an empty-diff plan, R10) keeps the verdict exactly as
    it was and only attaches the report: nothing was selected statically
    either, so there is no reason to print.
    """
    report = _static_report(static, gate_recorded=gate_recorded)
    if report_only:
        return _replace(static, selection=report)
    return _replace(static, static_reason=reason,
                    details=(f"engine static: {reason}",), selection=report)


def _is_pytest(config) -> bool:
    kind = getattr(getattr(config, "runner", None), "kind", None)
    return getattr(kind, "value", kind) == "pytest"


def _changed_set(static, repo_changed) -> frozenset:
    relevant = getattr(static, "relevant", None)
    if relevant is None:
        relevant = repo_changed or ()
    return frozenset(item for item in relevant if isinstance(item, str))


def _data_digest(key: bytes, root: Path, rel: str) -> str:
    """Current keyed digest of one recorded data path ('' absent)."""
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        return "?"
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return "?"
    digest = _digest_bounded(key, root / rel, C.SELECTION_DATA_MAX_BYTES)
    if digest is None:
        try:
            if not (root / rel).exists():
                return ""
        except OSError:
            pass
        return "?"
    return digest


def _plan_inputs(index, snapshot, compatibility, changed, test_roots,
                 store, *, key, project_root, since_gate=frozenset(),
                 observers=None, conditional=False, audit=False) -> object:
    """Build planner inputs, fetching old versions and data digests."""
    planner = _planner()
    source_index = _source_index()
    try:
        needed = planner.needed_versions(index, snapshot, compatibility)
    except Exception:
        needed = frozenset()
    blobs: dict = {}
    if needed and store is not None:
        try:
            blobs = dict(store.get_many(needed))
        except Exception:
            blobs = {}
    versions = {}
    for digest in needed:
        blob = blobs.get(digest)
        if blob is None:
            continue
        try:
            old = source_index.decode_index(blob)
        except Exception:
            old = None
        if old is not None:
            versions[digest] = old
    try:
        data_paths = planner.needed_data_paths(snapshot, compatibility)
    except Exception:
        data_paths = frozenset()
    root = Path(project_root)
    data_digests = {}
    for path in data_paths:
        try:
            data_digests[path] = _data_digest(key, root, path)
        except Exception:
            data_digests[path] = "?"
    return (planner, C.SelectionInputs(
        index=index, deps=snapshot, versions=versions,
        data_digests=data_digests,
        compatibility=compatibility, changed=changed,
        test_roots=tuple(test_roots), since_gate=frozenset(since_gate),
        observers=dict(observers or {}), conditional=bool(conditional),
        audit=bool(audit)))


# --- since the gate (R9, R27) -----------------------------------------------

def _project_prefix(top, project_root) -> str | None:
    """The project's path inside the git top ('' for the root)."""
    try:
        rel = Path(project_root).resolve().relative_to(
            Path(top).resolve()).as_posix()
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    return "" if rel == "." else rel


def _since_gate(key, top, project_root, config, gate):
    """Project paths that differ from the gate marker, and whether the
    marker was usable.

    Mirrors ``lastgreen.consult``: the names Git reports since the gate
    commit (committed, staged, unstaged and untracked), minus paths that
    were dirty at the gate and still hold the same bytes, plus dirty-at-gate
    paths whose keyed digest differs. An oversize or unreadable file on
    either side compares as changed. A missing marker, an unknown commit or
    unusable Git evidence is "no gate recorded": the empty set.
    """
    if gate is None or top is None or key is None:
        return frozenset(), False
    try:
        from . import monorepo
        names = monorepo.worktree_changed_files(Path(top), gate.commit)
    except Exception:
        names = None
    if names is None:
        return frozenset(), False
    dirty = dict(getattr(gate, "dirty", None) or {})
    root = Path(top)
    paths = {path for path in names if path not in dirty}
    for path, recorded in dirty.items():
        if not C.selection_relpath_safe(path):
            # The marker lives in a store file: a path that could leave the
            # checkout is never read and names nothing a plan could use.
            continue
        now = _digest_bounded(key, root / path, _FILE_READ_CAP)
        if recorded in (None, "?") or now in (None, "?") or now != recorded:
            paths.add(path)
    try:
        return _impact().relevant_paths(top, project_root, config,
                                        paths), True
    except Exception:
        return frozenset(), False


def _gate_flag(key, top, project_root, config, snapshot) -> bool:
    """Whether a usable gate marker exists (the R29 hint input)."""
    return _since_gate(key, top, project_root, config,
                       getattr(snapshot, "gate", None))[1]


def _merged_observers(snapshot, policy) -> tuple[dict, int, int]:
    """Learned rows plus declared prefixes expanded to ``*`` (R13, R14).

    Declared entries are literal path prefixes (``selection._matches``):
    every recorded node of a matching test file observes every path.
    Returns the merged map and the declared and learned node counts.
    """
    learned = {nodeid: frozenset(paths) for nodeid, paths
               in (getattr(snapshot, "observers", None) or {}).items()
               if paths}
    merged = dict(learned)
    prefixes = tuple(getattr(policy, "observers", ()) or ())
    declared = 0
    if prefixes:
        from .selection import _matches
        seen: dict = {}
        for nodeid, record in (getattr(snapshot, "nodes", None)
                               or {}).items():
            path = getattr(record, "test_file", None) \
                or C.selection_test_file(nodeid)
            hit = seen.get(path)
            if hit is None:
                hit = seen[path] = _matches(path, prefixes)
            if hit:
                declared += 1
                merged[nodeid] = frozenset(merged.get(nodeid, ())) \
                    | frozenset({C.SELECTION_OBSERVER_ALL})
    return merged, declared, len(learned)


def refine(domain, top, project_root, config: C.Config,
           repo_changed, static):
    """Refine one static verdict with the dynamic planner, or keep it.

    Never raises: any failure keeps the static verdict with a frozen
    static reason. Never writes records and never prints. Sets
    ``Impact.selection`` for pytest verdicts (R34).
    """
    try:
        return _refine(domain, top, project_root, config, repo_changed,
                       static)
    except Exception:
        try:
            return _static_fallback(static, _PLANNER_FAILED)
        except Exception:
            return static


def _refine(domain, top, project_root, config: C.Config,
            repo_changed, static):
    if not _is_pytest(config):
        return static
    selection = getattr(config, "selection", None)
    enabled = selection is None or getattr(selection, "enabled", True)
    empty_diff = (not getattr(static, "dynamic_ok", False)
                  and getattr(static, "kind", None) == "none")
    if not getattr(static, "dynamic_ok", False) and not (
            empty_diff and enabled):
        # A trigger, an unavailable diff, selection off or the legacy
        # index tail: not decided by the dynamic planner.
        return _replace(static, selection=_static_report(
            static, engine=None if getattr(static, "kind", "") == "full"
            else "static"))
    if not enabled:
        return _replace(static, selection=_static_report(static))
    if selection is not None and not getattr(selection, "dynamic", True):
        return _static_fallback(static, _DYNAMIC_FALSE,
                                report_only=empty_diff)
    try:
        key = _source_key(domain)
    except Exception:
        key = None
    if key is None:
        return _static_fallback(static, _NO_RECORDS, report_only=empty_diff)
    store, never_created = _open_store_reason(
        domain, config.project_id, create=False)
    if store is None:
        # A store that was never created is the empty-store case from
        # spec 6.6, not a broken store.
        return _static_fallback(
            static, _NO_RECORDS if never_created else _STORE_DOWN,
            report_only=empty_diff)
    try:
        return _refine_with_store(store, key, top, project_root, config,
                                  repo_changed, static, empty_diff)
    finally:
        _close_quietly(store)


def _refine_with_store(store, key: bytes, top, project_root,
                       config: C.Config, repo_changed, static,
                       empty_diff: bool = False):
    def fallback(reason, snapshot=None):
        flag = False
        if snapshot is not None:
            try:
                flag = _gate_flag(key, top, project_root, config, snapshot)
            except Exception:
                flag = False
        return _static_fallback(static, reason, gate_recorded=flag,
                                report_only=empty_diff)

    try:
        snapshot = store.snapshot()
    except Exception:
        return fallback(_PLANNER_FAILED)
    try:
        meta = store.meta()
    except Exception:
        meta = None
    runs = getattr(snapshot, "runs", None) or {}
    if not runs:
        if meta is not None:
            version = getattr(meta, "python", None)
            if (isinstance(version, tuple) and len(version) == 2
                    and all(isinstance(v, int) for v in version)
                    and tuple(version) < (3, 12)):
                return fallback(_python_reason(version))
            inactive = getattr(meta, "inactive_reason", None)
            if inactive:
                return fallback(_inactive_reason(inactive))
        return fallback(_NO_RECORDS)
    try:
        compatibility = compatibility_fingerprint(key, config, project_root)
    except Exception:
        return fallback(_PLANNER_FAILED)
    if not any(getattr(run, "compatibility", None) == compatibility
               for run in runs.values()):
        return fallback(_OTHER_CONFIG, snapshot)
    index = getattr(static, "project_index", None)
    if index is None:
        try:
            source_index = _source_index()
        except ImportError:
            return fallback(_PLANNER_FAILED, snapshot)
        cache = store if key is not None else None
        try:
            index = source_index.build_project_index(
                Path(project_root), config, key=key, cache=cache)
        except Exception:
            return fallback(_PLANNER_FAILED, snapshot)
        if getattr(index, "complete", False) is not True:
            return fallback(_PLANNER_FAILED, snapshot)
    changed = _changed_set(static, repo_changed)
    since, gate_recorded = _since_gate(key, top, project_root, config,
                                       getattr(snapshot, "gate", None))
    policy = getattr(config, "selection", None)
    observers, declared, learned = _merged_observers(snapshot, policy)
    test_roots = tuple(getattr(getattr(config, "runner", None),
                               "test_roots", ()) or ())
    planner, inputs = _plan_inputs(
        index, snapshot, compatibility, changed, test_roots, store, key=key,
        project_root=project_root, since_gate=since, observers=observers,
        conditional=bool(getattr(policy, "ambient_fingerprint", ())))
    try:
        decision = planner.plan(inputs)
    except Exception:
        return fallback(_PLANNER_FAILED, snapshot)
    gate = getattr(snapshot, "gate", None)
    age = None
    if gate_recorded and gate is not None:
        stamp = getattr(gate, "recorded_at", None)
        age = max(0.0, time.time() - stamp) \
            if isinstance(stamp, (int, float)) else 0.0
    facts = _PlanFacts(declared=declared, learned=learned,
                       since_count=len(since), gate_age=age)
    return _decision_impact(static, decision, meta, snapshot=snapshot,
                            observed=frozenset(observers),
                            gate_recorded=gate_recorded, facts=facts)


def _bindable_deselect(deselect: tuple) -> tuple[tuple[str, ...],
                                                 frozenset]:
    """Deselect ids the run request accepts, and the ids given up (D7).

    A file holding any id that cannot be bound exactly keeps all of its
    tests; over the id cap nothing is deselected. Both only widen the run,
    so falling back is never an error (G2). The given-up ids run (R5).
    """
    unsafe_files = {C.selection_test_file(nodeid) if isinstance(nodeid, str)
                    else None
                    for nodeid in deselect
                    if not C.selection_nodeid_safe(nodeid)}
    kept = tuple(nodeid for nodeid in deselect
                 if C.selection_nodeid_safe(nodeid)
                 and C.selection_test_file(nodeid) not in unsafe_files)
    if len(kept) > C.SELECTION_DESELECT_MAX_IDS:
        kept = ()
    return kept, frozenset(deselect) - frozenset(kept)


def _decision_impact(static, decision, meta, *, snapshot=None,
                     observed=frozenset(), gate_recorded=False,
                     facts: _PlanFacts | None = None):
    files = tuple(getattr(decision, "files", ()) or ())
    kept, _given_up = _bindable_deselect(
        tuple(getattr(decision, "deselect", ()) or ()))
    runs = getattr(snapshot, "runs", None) or {}
    tally = _tally(decision, kept=kept,
                   kept_conditional=frozenset(
                       nodeid for nodeid, _ in
                       getattr(decision, "conditional", ()) or ()
                       if nodeid in kept),
                   observed=observed)
    # Only the observers the report still lists as conditional (bindable
    # and deferred) are handed to the run request.
    listed = {entry[0] for entry in tally.conditional}
    conditional = [
        (nodeid, getattr(runs.get(run_id), "fingerprint", None))
        for nodeid, run_id in getattr(decision, "conditional", ()) or ()
        if nodeid in listed]
    report = _dynamic_report(tally, gate_recorded=gate_recorded)
    details = _detail_lines(decision, meta, tally, facts)
    common = dict(
        engine="dynamic", reached=int(getattr(decision, "reached", 0) or 0),
        units=tuple(getattr(decision, "units", ()) or ()), details=details,
        static_reason="", reason="", selection=report, decision=decision,
        observed=frozenset(observed))
    if tally.run_now > 0:
        return _replace(static, kind="selected", files=files, deselect=kept,
                        tests=tally.run_now, conditional=tuple(conditional),
                        total=int(getattr(decision, "total_files", 0) or 0),
                        **common)
    if conditional:
        # Nothing is certain to run, but promotable observers need a
        # launch (spec R44), so the plan keeps their files and the binding.
        return _replace(static, kind="none", files=files, deselect=kept,
                        tests=0, conditional=tuple(conditional), **common)
    return _replace(static, kind="none", files=(), deselect=(), tests=0,
                    conditional=(), **common)


def narrow(impact, folders: tuple[str, ...], whole: tuple[str, ...] = ()):
    """Restrict a dynamic plan to test files under ``folders`` (R40).

    ``""`` stands for the whole project. Files, deselect, conditional
    entries and the report's counts and aggregates are recounted for the
    folders; tests outside them are neither run nor counted. ``whole`` are
    test files the user named explicitly: they run whole, so their nodes
    are never counted as deferred (R30). A verdict the dynamic planner did
    not decide only has its files narrowed. Never raises.
    """
    try:
        return _narrow(impact, tuple(folders), frozenset(whole))
    except Exception:
        return impact


def _narrow(impact, folders: tuple, whole: frozenset = frozenset()):
    files = tuple(path for path in getattr(impact, "files", ()) or ()
                  if _under_scope(path, folders))
    decision = getattr(impact, "decision", None)
    report = getattr(impact, "selection", None)
    if decision is None or report is None or report.engine != "dynamic":
        return _replace(impact, files=files)
    deselect = tuple(nodeid for nodeid in impact.deselect
                     if _under_scope(C.selection_test_file(nodeid), folders))
    conditional = tuple(item for item in impact.conditional
                        if _under_scope(C.selection_test_file(item[0]),
                                        folders))
    tally = _tally(decision, kept=impact.deselect,
                   kept_conditional=frozenset(n for n, _ in conditional),
                   observed=getattr(impact, "observed", frozenset()),
                   scope=folders, whole=whole)
    narrowed = _dynamic_report(tally, gate_recorded=report.gate_recorded)
    if narrowed is not None:
        narrowed = dataclasses.replace(
            narrowed, fingerprint=report.fingerprint,
            fingerprint_reason=report.fingerprint_reason)
    if tally.run_now > 0:
        return _replace(impact, kind="selected", files=files,
                        deselect=deselect, conditional=conditional,
                        tests=tally.run_now, selection=narrowed)
    if conditional:
        return _replace(impact, kind="none", files=files, deselect=deselect,
                        conditional=conditional, tests=0, selection=narrowed)
    return _replace(impact, kind="none", files=(), deselect=(),
                    conditional=(), tests=0, selection=narrowed)


def promote_conditional(report: C.SelectionReport, promoted,
                        fingerprint, reason: str | None = None):
    """Move promoted conditional observers from deferred to run (R44).

    ``promoted`` are node ids of ``report.conditional``; each leaves the
    deferred counts and aggregates and joins ``run_now`` and
    ``observers_promoted``. ``fingerprint`` is this run's ambient digest
    (64 hex) or None when it is absent; an outcome word is accepted as is.
    The D7 outcome is ``absent`` without a digest, else ``changed`` when
    anything was promoted, else ``unchanged``. Pure; never raises.
    """
    try:
        return _promote(report, frozenset(promoted), fingerprint, reason)
    except Exception:
        return report


def _promote(report, promoted, fingerprint, reason):
    entries = {nodeid: (code, path)
               for nodeid, code, path in report.conditional}
    hit = sorted(nodeid for nodeid in promoted if nodeid in entries)
    reasons = {(code, path): tests
               for code, path, tests in report.deferred_by_reason}
    by_file = dict(report.deferred_by_file)
    for nodeid in hit:
        code, path = entries[nodeid]
        reasons[(code, path)] = reasons.get((code, path), 0) - 1
        file = C.selection_test_file(nodeid)
        by_file[file] = by_file.get(file, 0) - 1
    if fingerprint in C.SELECTION_FINGERPRINT_OUTCOMES:
        outcome = fingerprint
    elif not fingerprint:
        outcome = "absent"
    else:
        outcome = "changed" if hit else "unchanged"
    remaining = tuple(item for item in report.conditional
                      if item[0] not in set(hit))
    k = len(hit)
    return dataclasses.replace(
        report,
        run_now=(report.run_now or 0) + k,
        deferred=None if report.deferred is None else report.deferred - k,
        deferred_by_reason=tuple(sorted(
            ((code, path, tests) for (code, path), tests in reasons.items()
             if tests > 0),
            key=lambda item: (C.SELECTION_T2_CODES.index(item[0]),
                              item[1]))),
        deferred_by_file=tuple(sorted(
            (path, tests) for path, tests in by_file.items() if tests > 0)),
        observers_promoted=report.observers_promoted + k,
        observers_deferred=max(0, report.observers_deferred - k),
        fingerprint=outcome, fingerprint_reason=reason,
        conditional=remaining)


def preview(domain, top, project_root, config: C.Config,
            repo_changed) -> object:
    """Static plan plus dynamic refinement, for the T6 harness.

    No stderr output and no record writes; only parse-cache puts are
    allowed. Never raises for state problems.
    """
    impact = _impact()
    try:
        with planning(domain, config):
            static = impact.plan(top, project_root, config, repo_changed)
    except Exception:
        static = impact.plan(top, project_root, config, repo_changed)
    return refine(domain, top, project_root, config, repo_changed, static)


# --- prepare_run (operations seam) -------------------------------------------

def recording_enabled(config: C.Config, request) -> bool:
    """Whether a run records dependencies (design D9, minus the binding).

    Pytest runner with ``[selection]`` enabled and dynamic on, and a run
    that is neither shadow nor a probe. Operations additionally requires
    a report binding.
    """
    try:
        if not _is_pytest(config):
            return False
        selection = getattr(config, "selection", None)
        if selection is None:
            return False
        if not getattr(selection, "enabled", False):
            return False
        if not getattr(selection, "dynamic", True):
            return False
        if bool(getattr(request, "shadow", False)):
            return False
        if getattr(request, "probe", None) is not None:
            return False
        mode = getattr(request, "mode", None)
        if mode is not None and getattr(mode, "name", mode) == "PROBE":
            return False
        return True
    except Exception:
        return False


def prepare_run(domain, config: C.Config, request, report_path,
                run_id: str) -> tuple[tuple, Path | None]:
    """Recording env plus the deselect binding for one attempt.

    Returns ``(env_updates, binding_path_or_None)``. A binding write
    failure still runs: the record env stays, the deselect env is
    dropped, and every test in the argv files runs. Never raises.
    """
    try:
        return _prepare_run(domain, config, request, report_path, run_id)
    except Exception:
        try:
            if recording_enabled(config, request) \
                    and report_path is not None:
                return (((C.SELECTION_RECORD_ENV, "1"),), None)
        except Exception:
            pass
        return ((), None)


def _prepare_run(domain, config: C.Config, request, report_path,
                 run_id: str) -> tuple[tuple, Path | None]:
    if not recording_enabled(config, request) or report_path is None:
        return ((), None)
    env = [(C.SELECTION_RECORD_ENV, "1")]
    binding = None
    deselect = tuple(getattr(request, "deselect", ()) or ())
    mode = getattr(request, "mode", None)
    is_scoped = mode is not None and (
        mode is getattr(C.Mode, "SCOPED", mode)
        or getattr(mode, "value", mode) == "scoped")
    if deselect and is_scoped:
        safe = [nodeid for nodeid in deselect
                if C.selection_nodeid_safe(nodeid)]
        if safe:
            try:
                ingest = _ingest()
            except Exception:
                ingest = None
            if ingest is not None:
                try:
                    binding = ingest.write_deselect_binding(
                        Path(report_path), run_id, safe)
                except Exception:
                    binding = None
                if binding is not None:
                    env.append((C.SELECTION_DESELECT_ENV, str(binding)))
                    binding = Path(binding)
    return (tuple(env), binding)


# --- conditional binding rewrite (operations seam, R44) -----------------------

def rewrite_binding(report_path, run_id: str, deselect) -> bool:
    """Rewrite the attempt's deselect binding to ``deselect`` (R44).

    The new binding keeps every id except the promoted conditional ones,
    so T2 neighbours stay deselected. True when the binding was replaced;
    False on any failure (the caller then fails closed). Never raises.
    """
    try:
        _ingest().rewrite_deselect_binding(Path(report_path), run_id,
                                           tuple(deselect))
        return True
    except Exception:
        return False


# --- cleanup (operations seam) ------------------------------------------------

def cleanup(report_path) -> None:
    """Delete one attempt's private ingest files. Never raises.

    Operations calls this in ``execute()``'s outer ``finally`` (next to
    ``stack_dumps.cleanup``) so bindings and dependency files are
    removed on every outcome — including setup failure, pre-launch
    cancellation and paths that never reach ``after_run``.
    """
    try:
        ingest = _ingest()
    except Exception:
        return
    if report_path is None:
        return
    try:
        ingest.cleanup(Path(report_path))
    except Exception:
        pass


# --- after_run (operations seam) ----------------------------------------------

def after_run(*, domain, config: C.Config, project_id: str, run_id: str,
              report_path, project_root, expected_workers: int,
              execution: str, valid_handoff: bool, unchanged_inputs: bool,
              cancelled: bool, guard_failed: bool, setup_failed: bool,
              argv_files, verbose: bool = False,
              quiet: bool = False, fingerprint=None,
              ambient_fingerprint=None):
    """Ingest one attempt's dependency files into the selection store.

    Records are written only for a valid handoff of a consumed report
    with unchanged inputs, no cancellation, no guard or setup problem
    and complete dependency files (design D10); failed nodes widen
    selection through ``mark_outcomes`` and missing files through
    ``invalidate``. Full runs self-audit before updating (design D11) and
    are gates: they learn observers and write the gate marker in the same
    transaction (R15, R27), and the gate report is returned (None for
    iteration runs and for anything not ingested). ``fingerprint`` (also
    accepted as ``ambient_fingerprint``) is the run's ambient fingerprint,
    recorded on the run row (R43). Prints the audit, gate and ``-v`` lines.
    Never raises.
    """
    if fingerprint is None:
        fingerprint = ambient_fingerprint
    try:
        return _after_run(
            domain=domain, config=config, project_id=project_id,
            run_id=run_id, report_path=report_path,
            project_root=project_root, expected_workers=expected_workers,
            execution=execution, valid_handoff=valid_handoff,
            unchanged_inputs=unchanged_inputs, cancelled=cancelled,
            guard_failed=guard_failed, setup_failed=setup_failed,
            argv_files=argv_files, verbose=verbose, quiet=quiet,
            fingerprint=fingerprint)
    except Exception:
        return None


def _after_run(*, domain, config, project_id, run_id, report_path,
               project_root, expected_workers, execution, valid_handoff,
               unchanged_inputs, cancelled, guard_failed, setup_failed,
               argv_files, verbose, quiet, fingerprint=None):
    try:
        ingest = _ingest()
    except Exception:
        ingest = None
    if report_path is None or ingest is None:
        if verbose and ingest is None and report_path is not None:
            progress.emit(progress.format_selection_not_recorded(
                "dependency ingest is unavailable"), quiet=quiet)
        return None
    # Private ingest files are owned by operations' outer finally
    # (engine.cleanup, next to stack_dumps.cleanup), so every outcome —
    # including ones that never reach here — removes them.
    try:
        run = ingest.read_run(Path(report_path), run_id=run_id,
                              expected_workers=int(expected_workers))
    except Exception:
        run = None
    if cancelled:
        # D10: failures seen before the cancel stay selected (N4). Prior
        # records stay valid against their own baselines, so nothing is
        # invalidated: a cancelled full run must not force the next
        # changed run to the full suite.
        _fallbacks(run, domain, config, project_id, argv_files,
                   verbose, quiet, reason="the run was cancelled",
                   invalidate=False)
        return None
    if run is None:
        _fallbacks(None, domain, config, project_id, argv_files,
                   verbose, quiet,
                   reason="dependency files are unavailable")
        return None
    if not getattr(run, "recording", False):
        _note_inactive(domain, project_id, run)
    try:
        key = _source_key(domain)
    except Exception:
        key = None
    ingestible = (bool(valid_handoff) and bool(unchanged_inputs)
                  and not guard_failed and not setup_failed
                  and bool(getattr(run, "complete", False))
                  and bool(getattr(run, "recording", False))
                  and key is not None)
    if ingestible:
        return _ingest_run(run, domain, config, project_id, run_id, key,
                           project_root, execution, expected_workers,
                           verbose, quiet, fingerprint)
    _fallbacks(run, domain, config, project_id, argv_files,
               verbose, quiet,
               reason=_not_recorded_reason(
                   valid_handoff, unchanged_inputs, guard_failed,
                   setup_failed, run, key))
    return None


def _note_inactive(domain, project_id, run) -> None:
    """Persist a bridge inactivity report for future static reasons.

    ``store.meta`` is the only source for the ``test Python 3.N ...``
    and ``recording was unavailable: ...`` reasons; without this write
    they could never print. Status-only: never writes dependency
    records and never raises.
    """
    inactive = getattr(run, "inactive_reason", None)
    python = getattr(run, "python", None)
    if inactive is None and python is None:
        return
    try:
        store = _open_store(domain, project_id, create=True)
    except Exception:
        return
    if store is None:
        return
    try:
        store.note_inactive(inactive, python)
    except Exception:
        pass
    finally:
        _close_quietly(store)


def _not_recorded_reason(valid_handoff: bool, unchanged_inputs: bool,
                         guard_failed: bool, setup_failed: bool, run,
                         key) -> str:
    if not valid_handoff:
        return "the native report was not consumed"
    if guard_failed:
        return "the guard reported a problem"
    if setup_failed:
        return "setup failed"
    if not unchanged_inputs:
        return "inputs changed during the run"
    if key is None:
        return "no fingerprint key; dependencies cannot be recorded"
    if not getattr(run, "complete", False):
        return "dependency files are incomplete"
    inactive = getattr(run, "inactive_reason", None)
    if inactive:
        return f"recording was inactive: {inactive}"
    return "recording was inactive"


def _fallbacks(run, domain, config, project_id, argv_files, verbose, quiet,
               *, reason: str, invalidate: bool = True) -> None:
    """Widen-only writes for a run that records nothing (design D10)."""
    store = _open_store(domain, project_id, create=False)
    if store is None:
        if verbose:
            progress.emit(progress.format_selection_not_recorded(
                "dependency store unavailable"), quiet=quiet)
        return
    try:
        nodes = getattr(run, "nodes", None) or {}
        failed = {nodeid: node.outcome for nodeid, node in nodes.items()
                  if getattr(node, "outcome", None) in ("failed", "error")}
        if failed:
            try:
                store.mark_outcomes(failed)
            except Exception:
                pass
        if invalidate and (run is None
                           or not getattr(run, "complete", False)):
            try:
                store.invalidate(argv_files)
            except Exception:
                pass
    finally:
        _close_quietly(store)
    if verbose:
        progress.emit(progress.format_selection_not_recorded(
            render.terminal_text(reason)), quiet=quiet)


def _run_data_paths(run) -> frozenset:
    """Every recorded data path id resolved through the run vocabulary."""
    vocab = getattr(run, "vocabulary", None)
    paths = tuple(getattr(vocab, "paths", None) or ())
    if not paths:
        return frozenset()
    ids: set[int] = set()
    for context in [getattr(run, "ambient", None),
                    *getattr(run, "fixtures", {}).values()]:
        for pid in list(getattr(context, "data", None) or []):
            try:
                ids.add(int(pid))
            except (TypeError, ValueError):
                continue
    for node in getattr(run, "nodes", {}).values():
        for pid in list(getattr(getattr(node, "deps", None), "data",
                                None) or []):
            try:
                ids.add(int(pid))
            except (TypeError, ValueError):
                continue
    return frozenset(paths[pid] for pid in ids
                     if 0 <= pid < len(paths)
                     and C.selection_data_path(paths[pid]))


def _current_digests(key: bytes, project_root, run) -> dict:
    """Keyed digests of every .py file and recorded data path (None when
    a path cannot be read; such contexts record opaque)."""
    digests: dict[str, str | None] = {}
    try:
        source_index = _source_index()
    except ImportError:
        source_index = None
    root = Path(project_root)
    if source_index is not None:
        try:
            python_files = list(
                source_index.iter_python_files(root))
        except Exception:
            python_files = []
        for rel in python_files:
            if not isinstance(rel, str):
                continue
            try:
                raw = source_index.read_source(root, rel)
            except Exception:
                raw = None
            digests[rel] = (C.selection_file_digest(key, raw)
                            if raw is not None else None)
    for rel in _run_data_paths(run):
        digest = _data_digest(key, root, rel)
        digests[rel] = None if digest == "?" else digest
    return digests


@dataclasses.dataclass(frozen=True, slots=True)
class _Audit:
    """One gate's R15 outcome: what to write and what to report."""

    classified: object
    demoted: int
    rows: dict


def _valid_fingerprint(value) -> str | None:
    """A 64-hex ambient fingerprint, or None (absent)."""
    if (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)):
        return value
    return None


def _gate_marker(key, project_root):
    """The marker of this gate's tree, or ``(None, True)`` to clear it (R27).

    The marker names HEAD and every path dirty under the project with its
    keyed digest. More than ``MAX_GREEN_FILES`` dirty paths, no Git, or an
    unreadable HEAD clear the marker instead of measuring from a stale one.
    """
    try:
        top = _impact().git_top(Path(project_root))
        prefix = _project_prefix(top, project_root) if top else None
        if top is None or prefix is None:
            return None, True
        from . import lastgreen
        head = lastgreen.head_commit(top)
        dirty = lastgreen.dirty_files(top, prefix)
        if head is None or dirty is None \
                or len(dirty) > lastgreen.MAX_GREEN_FILES:
            return None, True
        digests = {path: _digest_bounded(key, Path(top) / path,
                                         _FILE_READ_CAP)
                   for path in dirty}
        return C.GateMarker(commit=head, dirty=digests,
                            recorded_at=time.time()), False
    except Exception:
        return None, True


def _ingest_run(run, domain, config, project_id, run_id, key,
                project_root, execution, expected_workers, verbose,
                quiet, fingerprint=None):
    """Audit, then record one run in a single store transaction.

    Returns the gate report for a full-mode (gate) ingest that landed,
    else None. Observers learned at the gate, the gate marker and the
    run's fingerprint travel in the same ``store.update`` transaction: a
    rejected update writes none of them.
    """
    store = _open_store(domain, project_id, create=True)
    if store is None:
        if verbose:
            progress.emit(progress.format_selection_not_recorded(
                "dependency store unavailable"), quiet=quiet)
        return None
    try:
        try:
            compatibility = compatibility_fingerprint(
                key, config, project_root)
        except Exception:
            if verbose:
                progress.emit(progress.format_selection_not_recorded(
                    "compatibility could not be computed"), quiet=quiet)
            return None
        gate = str(execution) == "full"
        audit = None
        if gate:
            audit = _self_audit(run, store, key, project_root, config,
                                compatibility, verbose, quiet)
        try:
            digests = _current_digests(key, project_root, run)
        except Exception:
            digests = {}
        try:
            # The versions these records were taken against must be
            # diffable later (a run that did not plan first never
            # indexed them).
            _source_index().ensure_cached(project_root, digests, key=key,
                                          cache=store)
        except Exception:
            pass
        marker, clear = _gate_marker(key, project_root) if gate \
            else (None, False)
        rows = audit.rows if audit is not None else {}
        try:
            lost = store.update(
                run, run_id=run_id, recorded_at=time.time(),
                compatibility=compatibility, digests=digests, full=gate,
                fingerprint=_valid_fingerprint(fingerprint),
                observers=rows or None, gate=marker, clear_gate=clear)
        except Exception:
            if verbose:
                progress.emit(progress.format_selection_not_recorded(
                    "the dependency store rejected the update"), quiet=quiet)
            return None
        evicted = lost if isinstance(lost, int) \
            and not isinstance(lost, bool) else 0
        try:
            # Recording worked: any remembered inactivity is stale.
            store.note_inactive(None, None)
        except Exception:
            pass
        if verbose:
            count = len(getattr(run, "nodes", None) or {})
            progress.emit(progress.format_selection_recorded(
                count, int(expected_workers)), quiet=quiet)
        if not gate:
            return None
        classified = audit.classified if audit is not None else None
        report = C.SelectionGateReport(
            previously_deferred=int(
                getattr(classified, "previously_deferred", 0)),
            failed=int(getattr(classified, "failed", 0)),
            observers_learned=len(rows), observers_evicted=evicted,
            unlearned=len(getattr(classified, "unlearned", ()) or ()),
            demoted=audit.demoted if audit is not None else 0)
        if audit is not None:
            _gate_lines(report, verbose, quiet)
        return report
    finally:
        _close_quietly(store)


def _gate_lines(report: C.SelectionGateReport, verbose: bool,
                quiet: bool) -> None:
    """R33: what the gate ran of the deferred tests and what it learned."""
    n, learned = report.previously_deferred, report.observers_learned
    if n > 0 or learned > 0:
        line = (f"ptest: selection gate: ran "
                f"{C.plural(n, 'previously deferred test')} · "
                f"{report.failed} failed · "
                f"{C.plural(learned, 'observer')} learned")
        if report.unlearned > 0:
            line += (f" · {report.unlearned} failed without a "
                     f"learnable path")
        progress.emit(line, quiet=quiet)
    elif verbose:
        progress.emit("ptest: -v selection gate: nothing was deferred",
                      quiet=quiet)
    if verbose and report.observers_evicted > 0:
        progress.emit(
            f"ptest: -v selection gate: "
            f"{C.plural(report.observers_evicted, 'observer row')} evicted "
            f"(store cap {C.SELECTION_OBSERVER_ROWS_MAX})", quiet=quiet)


def _self_audit(run, store, key, project_root, config, compatibility,
                verbose: bool, quiet: bool):
    """Audit a full run against the pre-update snapshot (design D11, R15).

    Plans the snapshot with the iteration rules (``changed = {}``, the gate
    marker's ``since_gate``, the merged observers; R4 keeps today's whole
    fallback set) and classifies every failing node whose chosen record
    passed: a failing deferred node with paths to learn becomes an observer
    row (no demotion), one without is only counted, a node no tier or
    selection covered is a miss and is demoted. Returns what the update
    transaction writes, or None when the audit could not run.
    """
    try:
        before = store.snapshot()
    except Exception:
        return None
    try:
        source_index = _source_index()
        index = source_index.build_project_index(
            Path(project_root), config, key=key, cache=store)
    except Exception:
        return None
    if getattr(index, "complete", False) is not True:
        return None
    test_roots = tuple(getattr(getattr(config, "runner", None),
                               "test_roots", ()) or ())
    try:
        try:
            top = _impact().git_top(Path(project_root))
        except Exception:
            top = None
        since, _ = _since_gate(key, top, project_root, config,
                               getattr(before, "gate", None))
        observers, _, _ = _merged_observers(
            before, getattr(config, "selection", None))
        planner, inputs = _plan_inputs(
            index, before, compatibility, frozenset(), test_roots, store,
            key=key, project_root=project_root, since_gate=since,
            observers=observers, conditional=False, audit=True)
        nodes = getattr(run, "nodes", None) or {}
        failed = sorted(
            nodeid for nodeid, node in nodes.items()
            if getattr(node, "outcome", None) in ("failed", "error"))
        decision = planner.plan(inputs)
        classified = planner.audit_classify(decision, before, failed,
                                            sorted(nodes))
        misses = tuple(classified.misses)
    except Exception:
        return None
    files = getattr(index, "files", None) or {}
    demotions = {}
    for nodeid in misses:
        entry = files.get(C.selection_test_file(nodeid)) \
            if hasattr(files, "get") else None
        digest = getattr(entry, "digest", "") or ""
        # A miss outside every indexed test file (a doctest in a source
        # module) has nothing to pin; the planner never selects it anyway.
        if isinstance(digest, str) and digest:
            demotions[nodeid] = digest
    demoted = 0
    if demotions:
        try:
            store.demote(demotions)
            demoted = len(demotions)
        except Exception:
            demoted = 0
    try:
        store.record_audit(len(failed), len(misses))
    except Exception:
        pass
    if demoted:
        progress.emit(progress.format_selection_audit(demoted),
                      quiet=quiet)
    if verbose:
        progress.emit(progress.format_selection_vaudit(
            len(failed), len(misses)), quiet=quiet)
    known = getattr(before, "observers", None) or {}
    rows = {}
    for nodeid, paths in (getattr(classified, "learned", None) or {}).items():
        have = frozenset(known.get(nodeid, ()))
        if C.SELECTION_OBSERVER_ALL in have:
            continue
        fresh = frozenset(paths) - have
        if fresh:
            rows[nodeid] = fresh
    return _Audit(classified=classified, demoted=demoted, rows=rows)


def status_lines(domain, items) -> list:
    """Human-only status lines for existing selection stores.

    ``items`` are ``(declaration_or_None, config)`` pairs; one line per
    store that exists. Never creates a store and never raises; JSON is
    unchanged.
    """
    lines: list[str] = []
    try:
        for declaration, config in items:
            try:
                store = _open_store(domain, getattr(config, "project_id",
                                                   ""), create=False)
            except Exception:
                continue
            if store is None:
                continue
            try:
                try:
                    meta = store.meta()
                except Exception:
                    continue
                newest = getattr(meta, "newest_recorded_at", None)
                age = None
                if isinstance(newest, (int, float)) \
                        and not isinstance(newest, bool):
                    age = max(0.0, time.time() - newest)
                line = progress.format_selection_store(
                    render.terminal_text(declaration)
                    if declaration else None,
                    int(getattr(meta, "nodes", 0) or 0),
                    int(getattr(meta, "size_bytes", 0) or 0), age)
                observers = int(getattr(meta, "observers", 0) or 0)
                if observers > 0:
                    line += f" · {C.plural(observers, 'learned observer')}"
                lines.append(line)
            finally:
                _close_quietly(store)
    except Exception:
        pass
    return lines

