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


# --- refine -----------------------------------------------------------------

def _detail_lines(decision, meta) -> tuple[str, ...]:
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
    """Refine one verdict, tolerating stubbed impact shapes."""
    if dataclasses.is_dataclass(static):
        try:
            return dataclasses.replace(static, **fields)
        except (TypeError, ValueError):
            return static
    return static


def _static_fallback(static, reason: str):
    return _replace(static, static_reason=reason,
                    details=(f"engine static: {reason}",))


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
                 store, *, key, project_root) -> object:
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
        test_roots=tuple(test_roots)))


def refine(domain, top, project_root, config: C.Config,
           repo_changed, static):
    """Refine one static verdict with the dynamic planner, or keep it.

    Never raises: any failure keeps the static verdict with a frozen
    static reason. Never writes records and never prints.
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
    if not getattr(static, "dynamic_ok", False):
        return static
    if not _is_pytest(config):
        return static
    selection = getattr(config, "selection", None)
    if selection is not None and not getattr(selection, "enabled", True):
        return static
    if selection is not None and not getattr(selection, "dynamic", True):
        return _static_fallback(static, _DYNAMIC_FALSE)
    try:
        key = _source_key(domain)
    except Exception:
        key = None
    if key is None:
        return _static_fallback(static, _NO_RECORDS)
    store, never_created = _open_store_reason(
        domain, config.project_id, create=False)
    if store is None:
        # A store that was never created is the empty-store case from
        # spec 6.6, not a broken store.
        return _static_fallback(
            static, _NO_RECORDS if never_created else _STORE_DOWN)
    try:
        return _refine_with_store(store, key, project_root, config,
                                  repo_changed, static)
    finally:
        _close_quietly(store)


def _refine_with_store(store, key: bytes, project_root, config: C.Config,
                       repo_changed, static):
    try:
        snapshot = store.snapshot()
    except Exception:
        return _static_fallback(static, _PLANNER_FAILED)
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
                return _static_fallback(static, _python_reason(version))
            inactive = getattr(meta, "inactive_reason", None)
            if inactive:
                return _static_fallback(static, _inactive_reason(inactive))
        return _static_fallback(static, _NO_RECORDS)
    try:
        compatibility = compatibility_fingerprint(key, config, project_root)
    except Exception:
        return _static_fallback(static, _PLANNER_FAILED)
    if not any(getattr(run, "compatibility", None) == compatibility
               for run in runs.values()):
        return _static_fallback(static, _OTHER_CONFIG)
    index = getattr(static, "project_index", None)
    if index is None:
        try:
            source_index = _source_index()
        except ImportError:
            return _static_fallback(static, _PLANNER_FAILED)
        cache = store if key is not None else None
        try:
            index = source_index.build_project_index(
                Path(project_root), config, key=key, cache=cache)
        except Exception:
            return _static_fallback(static, _PLANNER_FAILED)
        if getattr(index, "complete", False) is not True:
            return _static_fallback(static, _PLANNER_FAILED)
    changed = _changed_set(static, repo_changed)
    test_roots = tuple(getattr(getattr(config, "runner", None),
                               "test_roots", ()) or ())
    planner, inputs = _plan_inputs(index, snapshot, compatibility, changed,
                                   test_roots, store, key=key,
                                   project_root=project_root)
    try:
        decision = planner.plan(inputs)
    except Exception:
        return _static_fallback(static, _PLANNER_FAILED)
    return _decision_impact(static, config, decision, meta)


def _full_text(config: C.Config, decision) -> str | None:
    """The dynamic full-suite reason, or None when the run stays scoped."""
    if getattr(decision, "full_reason", None):
        return f"{decision.full_reason} (dynamic)"
    ratio = float(getattr(getattr(config, "selection", None),
                          "full_ratio", 0.70))
    selected = int(getattr(decision, "selected", 0) or 0)
    recorded = int(getattr(decision, "recorded", 0) or 0)
    files = tuple(getattr(decision, "files", ()) or ())
    total = int(getattr(decision, "total_files", 0) or 0)
    if recorded > 0 and selected >= ratio * recorded:
        return (f"{selected} of {recorded} recorded tests reach "
                f"full_ratio {ratio:g} (dynamic)")
    if total > 0 and len(files) >= ratio * total:
        return (f"{len(files)} of {total} test files reach "
                f"full_ratio {ratio:g} (dynamic)")
    try:
        limit = int(_impact().MAX_SELECTED)
    except Exception:
        limit = 200
    if len(files) > limit:
        return (f"{len(files)} test files exceed the {limit}-file "
                f"scoped limit (dynamic)")
    return None


def _bindable_deselect(deselect: tuple) -> tuple[tuple[str, ...], int]:
    """Deselect ids the run request accepts, and how many were given up (D7).

    A file holding any id that cannot be bound exactly keeps all of its
    tests; over the id cap nothing is deselected. Both only widen the run,
    so falling back is never an error (G2).
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
    return kept, len(deselect) - len(kept)


def _decision_impact(static, config: C.Config, decision, meta):
    files = tuple(getattr(decision, "files", ()) or ())
    details = _detail_lines(decision, meta)
    full_reason = _full_text(config, decision)
    if full_reason is not None:
        return _replace(static, kind="full", engine="dynamic", files=(),
                        deselect=(), tests=int(
                            getattr(decision, "selected", 0) or 0),
                        reached=int(getattr(decision, "reached", 0) or 0),
                        units=tuple(getattr(decision, "units", ()) or ()),
                        details=details, static_reason="",
                        reason=full_reason,
                        total=int(
                            getattr(decision, "total_files", 0) or 0))
    if not files:
        return _replace(static, kind="none", engine="dynamic", files=(),
                        deselect=(), tests=0,
                        reached=int(getattr(decision, "reached", 0) or 0),
                        units=tuple(getattr(decision, "units", ()) or ()),
                        details=details, static_reason="")
    deselect, restored = _bindable_deselect(
        tuple(getattr(decision, "deselect", ()) or ()))
    return _replace(static, kind="selected", engine="dynamic", files=files,
                    deselect=deselect,
                    tests=int(getattr(decision, "selected", 0) or 0) + restored,
                    reached=int(getattr(decision, "reached", 0) or 0),
                    units=tuple(getattr(decision, "units", ()) or ()),
                    details=details, static_reason="",
                    total=int(getattr(decision, "total_files", 0) or 0))


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
              quiet: bool = False) -> None:
    """Ingest one attempt's dependency files into the selection store.

    Records are written only for a valid handoff of a consumed report
    with unchanged inputs, no cancellation, no guard or setup problem
    and complete dependency files (design D10); failed nodes widen
    selection through ``mark_outcomes`` and missing files through
    ``invalidate``. Full runs self-audit before updating (design D11).
    Prints the audit and ``-v`` lines. Never raises.
    """
    try:
        _after_run(domain=domain, config=config, project_id=project_id,
                   run_id=run_id, report_path=report_path,
                   project_root=project_root,
                   expected_workers=expected_workers, execution=execution,
                   valid_handoff=valid_handoff,
                   unchanged_inputs=unchanged_inputs, cancelled=cancelled,
                   guard_failed=guard_failed, setup_failed=setup_failed,
                   argv_files=argv_files, verbose=verbose, quiet=quiet)
    except Exception:
        pass


def _after_run(*, domain, config, project_id, run_id, report_path,
               project_root, expected_workers, execution, valid_handoff,
               unchanged_inputs, cancelled, guard_failed, setup_failed,
               argv_files, verbose, quiet) -> None:
    try:
        ingest = _ingest()
    except Exception:
        ingest = None
    if report_path is None or ingest is None:
        if verbose and ingest is None and report_path is not None:
            progress.emit(progress.format_selection_not_recorded(
                "dependency ingest is unavailable"), quiet=quiet)
        return
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
        return
    if run is None:
        _fallbacks(None, domain, config, project_id, argv_files,
                   verbose, quiet,
                   reason="dependency files are unavailable")
        return
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
        _ingest_run(run, domain, config, project_id, run_id, key,
                    project_root, execution, expected_workers,
                    verbose, quiet)
    else:
        _fallbacks(run, domain, config, project_id, argv_files,
                   verbose, quiet,
                   reason=_not_recorded_reason(
                       valid_handoff, unchanged_inputs, guard_failed,
                       setup_failed, run, key))


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


def _ingest_run(run, domain, config, project_id, run_id, key,
                project_root, execution, expected_workers, verbose,
                quiet) -> None:
    store = _open_store(domain, project_id, create=True)
    if store is None:
        if verbose:
            progress.emit(progress.format_selection_not_recorded(
                "dependency store unavailable"), quiet=quiet)
        return
    try:
        try:
            compatibility = compatibility_fingerprint(
                key, config, project_root)
        except Exception:
            if verbose:
                progress.emit(progress.format_selection_not_recorded(
                    "compatibility could not be computed"), quiet=quiet)
            return
        if str(execution) == "full":
            _self_audit(run, store, key, project_root, config,
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
        try:
            store.update(run, run_id=run_id, recorded_at=time.time(),
                         compatibility=compatibility, digests=digests,
                         full=str(execution) == "full")
        except Exception:
            if verbose:
                progress.emit(progress.format_selection_not_recorded(
                    "the dependency store rejected the update"), quiet=quiet)
            return
        try:
            # Recording worked: any remembered inactivity is stale.
            store.note_inactive(None, None)
        except Exception:
            pass
        if verbose:
            count = len(getattr(run, "nodes", None) or {})
            progress.emit(progress.format_selection_recorded(
                count, int(expected_workers)), quiet=quiet)
    finally:
        _close_quietly(store)


def _self_audit(run, store, key, project_root, config, compatibility,
                verbose: bool, quiet: bool) -> None:
    """Audit a full run against the pre-update snapshot (design D11)."""
    try:
        before = store.snapshot()
    except Exception:
        return
    try:
        source_index = _source_index()
        index = source_index.build_project_index(
            Path(project_root), config, key=key, cache=store)
    except Exception:
        return
    if getattr(index, "complete", False) is not True:
        return
    test_roots = tuple(getattr(getattr(config, "runner", None),
                               "test_roots", ()) or ())
    try:
        planner, inputs = _plan_inputs(
            index, before, compatibility, frozenset(), test_roots, store,
            key=key, project_root=project_root)
        failed = sorted(
            nodeid for nodeid, node in
            (getattr(run, "nodes", None) or {}).items()
            if getattr(node, "outcome", None) in ("failed", "error"))
        decision = planner.plan(inputs)
        misses = tuple(planner.audit_misses(decision, before, failed))
    except Exception:
        return
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
                lines.append(progress.format_selection_store(
                    render.terminal_text(declaration)
                    if declaration else None,
                    int(getattr(meta, "nodes", 0) or 0),
                    int(getattr(meta, "size_bytes", 0) or 0), age))
            finally:
                _close_quietly(store)
    except Exception:
        pass
    return lines

