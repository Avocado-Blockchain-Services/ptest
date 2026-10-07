"""Dependency-file reader, deselect binding writer and cleanup (T3).

The bridge (T4) writes one ``<report>.deps-<pid>`` JSON file per process
(section 2.6); ``read_run`` merges them into a ``RunDependencies``. Every
abuse case — symlink, foreign owner, wrong mode, hard link, oversize,
truncated or mistyped JSON — yields an incomplete run with a note, never
a crash and never a wrong skip. Hostile paths make their context opaque;
they are never dropped silently.

``write_deselect_binding`` creates the private ``<report>.deselect``
binding (``O_EXCL|O_NOFOLLOW``, 0600); unsafe node ids raise
``ValueError`` so the caller keeps the whole file selected. ``cleanup``
removes only this report's deps files and binding, never through links,
and never raises.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import contracts as C
from . import files
from .contracts import (
    ContextDeps,
    DepVocabulary,
    RecordedNode,
    RunDependencies,
    selection_code_path,
    selection_data_path,
    selection_deselect_path,
    selection_deps_pid,
    selection_empty_context,
    selection_ids,
    selection_nodeid_safe,
    selection_normalize_qualname,
)

DEPS_FORMAT = C.SELECTION_DEPS_FORMAT
DEPS_INFIX = C.SELECTION_DEPS_INFIX
DESELECT_FORMAT = C.SELECTION_DESELECT_FORMAT
DESELECT_SUFFIX = C.SELECTION_DESELECT_SUFFIX
DEPS_MAX_BYTES = C.SELECTION_DEPS_MAX_BYTES
DESELECT_MAX_BYTES = C.SELECTION_DESELECT_MAX_BYTES
DESELECT_MAX_IDS = C.SELECTION_DESELECT_MAX_IDS
OUTCOMES = C.SELECTION_OUTCOMES
FIXTURE_SCOPES = C.SELECTION_FIXTURE_SCOPES

MAX_DIR_ENTRIES = 4096
_MAX_NOTES = 64
_HEX32 = re.compile(r"[0-9a-f]{32}")

# Worst phase first; "unknown" (no final report) loses to any real report.
_SEVERITY = {"error": 6, "failed": 5, "xpassed": 4, "passed": 3,
             "xfailed": 2, "skipped": 1, "unknown": 0}


def _empty_run(*notes: str) -> RunDependencies:
    vocab = DepVocabulary(paths=(), functions=(), fixtures=())
    return RunDependencies(
        vocabulary=vocab, nodes={}, fixtures={},
        ambient=selection_empty_context(), complete=False, recording=False,
        python=None, inactive_reason=None, notes=tuple(notes[:_MAX_NOTES]))


def _candidate_names(report_dir: Path):
    import itertools
    try:
        iterator = os.scandir(report_dir)
    except OSError:
        return
    try:
        for entry in itertools.islice(iterator, MAX_DIR_ENTRIES):
            try:
                yield entry.name
            except OSError:
                return
    except OSError:
        return
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass


def _read_private(path: Path, limit: int) -> bytes | None:
    """Owned 0600 single-link regular file bytes, else None (never follow)."""
    try:
        stamp = os.lstat(path)
    except OSError:
        return None
    if (stat.S_ISLNK(stamp.st_mode)
            or not stat.S_ISREG(stamp.st_mode)
            or stamp.st_uid != os.getuid()
            or stat.S_IMODE(stamp.st_mode) != 0o600
            or stamp.st_nlink != 1):
        return None
    if stamp.st_size > limit:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if os.fstat(fd).st_nlink != 1:
            return None
        chunks = []
        remaining = stamp.st_size + 1
        while remaining > 0:
            piece = os.read(fd, min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(raw) > limit or len(raw) != stamp.st_size:
        return None
    return raw


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_ctx(data: object, n_funcs: int, n_paths: int) -> dict | None:
    if not isinstance(data, dict):
        return None
    try:
        funcs = data["functions"]
        modules = data["modules"]
        datas = data["data"]
        opaque = data["opaque"]
    except KeyError:
        return None
    if (not isinstance(funcs, list) or not isinstance(modules, list)
            or not isinstance(datas, list) or not isinstance(opaque, bool)):
        return None
    if (any(not _is_int(item) or item not in range(n_funcs)
            for item in funcs)
            or any(not _is_int(item) or item not in range(n_paths)
                   for item in (*modules, *datas))):
        return None
    return {"functions": list(funcs), "modules": list(modules),
            "data": list(datas), "opaque": opaque}


def _parse_lazy(data: object, n_funcs: int,
                n_paths: int) -> list[tuple[tuple, tuple]]:
    """Optional ownership edges of lazily built state; malformed → none.

    Each edge is ``[owner, state]`` with refs ``["f", function]``,
    ``["m", path]`` or (state only) ``["d", path]``. Older bridges write
    no key; a bad list loses only this widening, never the file.
    """
    if not isinstance(data, list):
        return []
    edges = []
    for entry in data:
        if not isinstance(entry, list) or len(entry) != 2:
            return []
        refs = []
        for ref in entry:
            if (not isinstance(ref, list) or len(ref) != 2
                    or ref[0] not in ("f", "m", "d") or not _is_int(ref[1])
                    or ref[1] not in range(n_funcs if ref[0] == "f"
                                           else n_paths)):
                return []
            refs.append((ref[0], ref[1]))
        if refs[0][0] == "d":
            return []
        edges.append((refs[0], refs[1]))
    return edges


def _parse_file(payload: object, *, pid: int, run_id: str) -> dict | None:
    """Validate one deps object; None when it must not be trusted."""
    if not isinstance(payload, dict):
        return None
    try:
        format_ = payload["format"]
        file_run_id = payload["run_id"]
        role = payload["role"]
        worker_id = payload["worker_id"]
        file_pid = payload["pid"]
        python = payload["python"]
        recording = payload["recording"]
        inactive = payload["inactive_reason"]
        workers = payload["workers"]
        overflow = payload["overflow"]
        tamper = payload["tamper"]
        paths = payload["paths"]
        functions = payload["functions"]
        ambient = payload["ambient"]
        fixtures = payload["fixtures"]
        nodes = payload["nodes"]
    except KeyError:
        return None
    if format_ != DEPS_FORMAT or file_run_id != run_id:
        return None
    if not _is_int(file_pid) or file_pid != pid:
        return None
    if role not in ("controller", "worker"):
        return None
    if role == "controller":
        if worker_id is not None:
            return None
    elif not isinstance(worker_id, str) or not worker_id:
        return None
    if not isinstance(recording, bool) or not isinstance(overflow, bool):
        return None
    if not isinstance(tamper, bool):
        return None
    if not isinstance(workers, list) or any(
            not isinstance(item, str) for item in workers):
        return None
    if not isinstance(paths, list) or any(
            not isinstance(item, str) for item in paths):
        return None
    if not isinstance(functions, list):
        return None
    for entry in functions:
        if (not isinstance(entry, list) or len(entry) != 3
                or not _is_int(entry[0]) or entry[0] not in range(len(paths))
                or not isinstance(entry[1], str)
                or not _is_int(entry[2])):
            return None
    ambient_ctx = _parse_ctx(ambient, len(functions), len(paths))
    if ambient_ctx is None:
        return None
    if not isinstance(fixtures, list):
        return None
    parsed_fixtures = []
    for entry in fixtures:
        if not isinstance(entry, dict):
            return None
        try:
            key = entry["key"]
        except KeyError:
            return None
        if (not isinstance(key, list) or len(key) != 3
                or not isinstance(key[0], str)
                or not isinstance(key[1], str) or key[2] not in FIXTURE_SCOPES):
            return None
        ctx = _parse_ctx({name: entry.get(name) for name in
                          ("functions", "modules", "data", "opaque")},
                         len(functions), len(paths))
        if ctx is None:
            return None
        parsed_fixtures.append({"key": [key[0], key[1], key[2]], **ctx})
    if not isinstance(nodes, list):
        return None
    parsed_nodes = []
    for entry in nodes:
        if not isinstance(entry, dict):
            return None
        try:
            nodeid = entry["nodeid"]
            outcome = entry["outcome"]
            refs = entry["fixtures"]
        except KeyError:
            return None
        if not isinstance(nodeid, str) or not nodeid:
            return None
        if outcome not in OUTCOMES:
            return None
        if (not isinstance(refs, list) or any(
                not _is_int(item) or item not in range(len(fixtures))
                for item in refs)):
            return None
        ctx = _parse_ctx({name: entry.get(name) for name in
                          ("functions", "modules", "data", "opaque")},
                         len(functions), len(paths))
        if ctx is None:
            return None
        parsed_nodes.append({"nodeid": nodeid, "outcome": outcome,
                             "fixtures": list(refs), **ctx})
    if python is not None:
        if (not isinstance(python, list) or len(python) != 2
                or not _is_int(python[0]) or not _is_int(python[1])
                or python[0] < 0 or python[1] < 0):
            python = None
        else:
            python = (python[0], python[1])
    if inactive is not None:
        inactive = inactive[:200] if isinstance(inactive, str) else None
    return {"lazy": _parse_lazy(payload.get("lazy"), len(functions),
                                len(paths)),
            "role": role, "worker_id": worker_id, "recording": recording,
            "workers": list(workers), "overflow": overflow, "tamper": tamper,
            "paths": list(paths),
            "functions": [(item[0], item[1]) for item in functions],
            "ambient": ambient_ctx, "fixtures": parsed_fixtures,
            "nodes": parsed_nodes, "python": python,
            "inactive_reason": inactive}


def _with_lazy(deps: ContextDeps,
               lazy: dict[tuple, set[tuple]]) -> ContextDeps:
    """``deps`` plus the state built under any code it ran, transitively.

    Edges come from every process of the run: state one worker built and
    the others reused (a template database guarded by a marker) belongs
    to every test that ran the code that builds it.
    """
    stack = [("f", num) for num in deps.functions if ("f", num) in lazy]
    stack += [("m", num) for num in deps.modules if ("m", num) in lazy]
    if not stack:
        return deps
    found: set[tuple] = set()
    while stack:
        for state in lazy.get(stack.pop(), ()):
            if state not in found:
                found.add(state)
                stack.append(state)
    funcs = {num for kind, num in found if kind == "f"}
    mods = {num for kind, num in found if kind == "m"}
    datas = {num for kind, num in found if kind == "d"}
    return ContextDeps(
        functions=selection_ids((*deps.functions, *funcs)),
        modules=selection_ids((*deps.modules, *mods)),
        data=selection_ids((*deps.data, *datas)), opaque=deps.opaque)


def _merge(parsed: list[dict]) -> RunDependencies:
    path_index: dict[str, int] = {}
    paths: list[str] = []
    func_index: dict[tuple[str, str], int] = {}
    funcs: list[tuple[int, str]] = []
    fixture_index: dict[tuple[str, str, str], int] = {}
    fixture_keys: list[tuple[str, str, str]] = []
    merged_fixtures: dict[int, ContextDeps] = {}
    merged_nodes: dict[str, RecordedNode] = {}
    ambient_funcs: set[int] = set()
    ambient_modules: set[int] = set()
    ambient_data: set[int] = set()
    ambient_opaque = False
    recording = False
    python = None
    inactive = None

    def intern_path(path: str) -> int:
        if path not in path_index:
            path_index[path] = len(paths)
            paths.append(path)
        return path_index[path]

    def intern_func(path: str, qualname: str | None) -> int | None:
        key = (path, qualname if qualname is not None else "")
        if key not in func_index:
            func_index[key] = len(funcs)
            funcs.append((intern_path(path), key[1]))
        return func_index[key]

    lazy: dict[tuple, set[tuple]] = {}
    for item in parsed:
        recording = recording or item["recording"]
        if python is None and item["python"] is not None:
            python = item["python"]
        if inactive is None and item["inactive_reason"] is not None:
            inactive = item["inactive_reason"]
        tamper = item["tamper"]
        file_paths = item["paths"]

        bad_code = {num for num, path in enumerate(file_paths)
                    if not selection_code_path(path)}
        bad_data = {num for num, path in enumerate(file_paths)
                    if not selection_data_path(path)}

        def remap(ctx: dict) -> ContextDeps:
            funcs_out: set[int] = set()
            modules_out: set[int] = set()
            opaque = bool(ctx["opaque"]) or tamper
            for num in ctx["functions"]:
                path_idx, raw = item["functions"][num]
                path = file_paths[path_idx]
                if path_idx in bad_code:
                    opaque = True
                qualname = selection_normalize_qualname(raw)
                if qualname is None:
                    # Module-level code: a module-body record.
                    modules_out.add(intern_path(path))
                else:
                    funcs_out.add(intern_func(path, qualname))
            for num in ctx["modules"]:
                modules_out.add(intern_path(file_paths[num]))
                if num in bad_code:
                    opaque = True
            datas_out = {intern_path(file_paths[num])
                         for num in ctx["data"]}
            for num in ctx["data"]:
                if num in bad_data:
                    opaque = True
            return ContextDeps(
                functions=selection_ids(funcs_out),
                modules=selection_ids(modules_out),
                data=selection_ids(datas_out), opaque=opaque)

        for entry in item["fixtures"]:
            key = (entry["key"][0], entry["key"][1], entry["key"][2])
            if key not in fixture_index:
                fixture_index[key] = len(fixture_keys)
                fixture_keys.append(key)
            dense = fixture_index[key]
            deps = remap(entry)
            if dense in merged_fixtures:
                old = merged_fixtures[dense]
                deps = ContextDeps(
                    functions=selection_ids(
                        (*old.functions, *deps.functions)),
                    modules=selection_ids((*old.modules, *deps.modules)),
                    data=selection_ids((*old.data, *deps.data)),
                    opaque=old.opaque or deps.opaque)
            merged_fixtures[dense] = deps
        index_of_file_fixture = {}
        for num, entry in enumerate(item["fixtures"]):
            key = (entry["key"][0], entry["key"][1], entry["key"][2])
            index_of_file_fixture[num] = fixture_index[key]

        for entry in item["nodes"]:
            deps = remap(entry)
            if not selection_nodeid_safe(entry["nodeid"]):
                deps = ContextDeps(
                    functions=deps.functions, modules=deps.modules,
                    data=deps.data, opaque=True)
            refs = selection_ids(index_of_file_fixture[num]
                                 for num in entry["fixtures"])
            nodeid = entry["nodeid"]
            if nodeid in merged_nodes:
                old = merged_nodes[nodeid]
                outcome = (entry["outcome"]
                           if _SEVERITY[entry["outcome"]]
                           >= _SEVERITY[old.outcome] else old.outcome)
                deps = ContextDeps(
                    functions=selection_ids(
                        (*old.deps.functions, *deps.functions)),
                    modules=selection_ids(
                        (*old.deps.modules, *deps.modules)),
                    data=selection_ids((*old.deps.data, *deps.data)),
                    opaque=old.deps.opaque or deps.opaque)
                refs = selection_ids((*old.fixtures, *refs))
            else:
                outcome = entry["outcome"]
            merged_nodes[nodeid] = RecordedNode(
                nodeid=nodeid, outcome=outcome, deps=deps, fixtures=refs)

        def dense(ref: tuple) -> tuple | None:
            kind, num = ref
            if kind != "f":
                if num in (bad_code if kind == "m" else bad_data):
                    return None
                return (kind, intern_path(file_paths[num]))
            path_idx, raw = item["functions"][num]
            if path_idx in bad_code:
                return None
            qualname = selection_normalize_qualname(raw)
            if qualname is None:
                return ("m", intern_path(file_paths[path_idx]))
            return ("f", intern_func(file_paths[path_idx], qualname))

        for owner_ref, state_ref in item.get("lazy", ()):
            owner, state = dense(owner_ref), dense(state_ref)
            if owner is not None and state is not None and owner != state:
                lazy.setdefault(owner, set()).add(state)

        ambient = remap(item["ambient"])
        ambient_funcs.update(ambient.functions)
        ambient_modules.update(ambient.modules)
        ambient_data.update(ambient.data)
        ambient_opaque = ambient_opaque or ambient.opaque

    if lazy:
        merged_nodes = {
            nodeid: RecordedNode(nodeid=node.nodeid, outcome=node.outcome,
                                 deps=_with_lazy(node.deps, lazy),
                                 fixtures=node.fixtures)
            for nodeid, node in merged_nodes.items()}
        merged_fixtures = {key: _with_lazy(deps, lazy)
                           for key, deps in merged_fixtures.items()}

    return RunDependencies(
        vocabulary=DepVocabulary(
            paths=tuple(paths),
            functions=tuple(funcs),
            fixtures=tuple(fixture_keys)),
        nodes=merged_nodes, fixtures=merged_fixtures,
        ambient=ContextDeps(
            functions=selection_ids(ambient_funcs),
            modules=selection_ids(ambient_modules),
            data=selection_ids(ambient_data), opaque=ambient_opaque),
        complete=True, recording=recording, python=python,
        inactive_reason=inactive, notes=())


def read_run(report_path: Path, *, run_id: str,
             expected_workers: int) -> RunDependencies:
    """Merge one attempt's dependency files. Never raises.

    Anything unexpected — missing, refused or malformed files, a wrong
    worker count, an overflow file — yields ``complete=False`` with a
    note, so the caller widens selection instead of skipping wrongly.
    """
    try:
        return _read_run(report_path, run_id=run_id,
                         expected_workers=expected_workers)
    except Exception:
        return _empty_run("ingest failed")


def _read_run(report_path: Path, *, run_id: str,
              expected_workers: int) -> RunDependencies:
    try:
        report = Path(report_path)
        report_dir = report.parent
        report_name = report.name
    except (TypeError, ValueError):
        return _empty_run("bad report path")
    if not isinstance(run_id, str) or not run_id:
        return _empty_run("bad run id")
    if (isinstance(expected_workers, bool)
            or not isinstance(expected_workers, int)
            or expected_workers < 1):
        return _empty_run("bad worker count")
    parsed: list[dict] = []
    notes: list[str] = []
    try:
        names = list(_candidate_names(report_dir))
    except Exception:
        return _empty_run("cannot list report dir")
    candidates = sorted(
        (pid, name) for name in names
        for pid in (selection_deps_pid(report_name, name),)
        if pid is not None)
    for pid, name in candidates:
        raw = _read_private(report_dir / name, DEPS_MAX_BYTES + 1)
        if raw is None or len(raw) > DEPS_MAX_BYTES:
            notes.append(f"{name}: refused or oversize")
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            notes.append(f"{name}: malformed JSON")
            continue
        item = _parse_file(payload, pid=pid, run_id=run_id)
        if item is None:
            notes.append(f"{name}: invalid dependency file")
            continue
        parsed.append(item)
    controllers = [item for item in parsed if item["role"] == "controller"]
    workers = [item for item in parsed if item["role"] == "worker"]
    if expected_workers == 1:
        if len(controllers) != 1 or controllers[0]["overflow"]:
            notes.append("serial run needs one valid controller file")
            return _incomplete(parsed, notes)
        if workers:
            notes.append(f"ignoring {len(workers)} worker files")
        merged = _merge([controllers[0]])
    else:
        if len(controllers) != 1:
            notes.append("xdist run needs one valid controller file")
            return _incomplete(parsed, notes)
        controller = controllers[0]
        if controller["overflow"]:
            notes.append("controller overflowed")
            return _incomplete(parsed, notes)
        listed = controller["workers"]
        if len(listed) != expected_workers:
            notes.append("controller worker count mismatch")
            return _incomplete(parsed, notes)
        by_worker: dict[str, list[dict]] = {}
        for item in workers:
            by_worker.setdefault(item["worker_id"], []).append(item)
        if (set(by_worker) != set(listed)
                or any(len(items) != 1 for items in by_worker.values())
                or any(items[0]["overflow"]
                       for items in by_worker.values())):
            notes.append("worker files incomplete")
            return _incomplete(parsed, notes)
        merged = _merge([controller]
                        + [by_worker[worker][0] for worker in listed])
    merged = RunDependencies(
        vocabulary=merged.vocabulary, nodes=merged.nodes,
        fixtures=merged.fixtures, ambient=merged.ambient,
        complete=merged.complete, recording=merged.recording,
        python=merged.python, inactive_reason=merged.inactive_reason,
        notes=tuple(notes[:_MAX_NOTES]))
    return merged


def _incomplete(parsed: list[dict], notes: list[str]) -> RunDependencies:
    try:
        merged = _merge(parsed)
    except Exception:
        return _empty_run(*notes)
    return RunDependencies(
        vocabulary=merged.vocabulary, nodes=merged.nodes,
        fixtures=merged.fixtures, ambient=merged.ambient, complete=False,
        recording=merged.recording, python=merged.python,
        inactive_reason=merged.inactive_reason,
        notes=tuple(notes[:_MAX_NOTES]))


def write_deselect_binding(report_path: Path, run_id: str,
                           nodeids) -> Path:
    """Write the private ``<report>.deselect`` binding (O_EXCL, 0600).

    Raises ``ValueError`` for a bad run id or any unsafe node id (the
    caller then keeps the whole file selected) and ``OSError`` when the
    file cannot be created exclusively.
    """
    if not isinstance(run_id, str) or not _HEX32.fullmatch(run_id):
        raise ValueError("deselect binding needs a 32-hex run id")
    try:
        ids = list(nodeids)
    except TypeError:
        raise ValueError("deselect node ids must be a sequence") from None
    if len(ids) > DESELECT_MAX_IDS:
        raise ValueError("deselect node ids exceed the limit")
    for item in ids:
        if not isinstance(item, str) or not selection_nodeid_safe(item):
            raise ValueError(f"deselect node id is not safe: {item!r}")
    path = selection_deselect_path(Path(report_path))
    payload = json.dumps({"format": DESELECT_FORMAT, "run_id": run_id,
                          "nodeids": ids},
                         ensure_ascii=True,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > DESELECT_MAX_BYTES:
        raise ValueError("deselect binding exceeds the size cap")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if cloexec:
        flags |= cloexec
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        stamp = os.fstat(fd)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        try:
            os.fsync(fd)
        except OSError:
            pass
    except BaseException:
        try:
            dev, ino = stamp.st_dev, stamp.st_ino
        except NameError:
            dev = ino = None
        try:
            os.close(fd)
        except OSError:
            pass
        if dev is not None:
            try:
                files.unlink_if_same(path.parent, path.name, dev, ino)
            except Exception:
                pass
        raise
    try:
        os.close(fd)
    except OSError:
        try:
            files.unlink_if_same(path.parent, path.name, stamp.st_dev,
                                 stamp.st_ino)
        except Exception:
            pass
        raise
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        parent_fd = None
    if parent_fd is not None:
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        finally:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    return path


def _remove_if_owned_regular(report_dir: Path, name: str) -> None:
    try:
        stamp = os.lstat(report_dir / name)
    except OSError:
        return
    if not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.getuid():
        return
    try:
        files.unlink_if_same(report_dir, name, stamp.st_dev, stamp.st_ino)
    except (C.Problem, OSError):
        return


def cleanup(report_path: Path) -> None:
    """Delete one attempt's deps files and binding. Never raises."""
    try:
        report = Path(report_path)
        report_dir = report.parent
        report_name = report.name
        names = [name for name in _candidate_names(report_dir)
                 if selection_deps_pid(report_name, name) is not None]
    except Exception:
        return
    for name in [report_name + DESELECT_SUFFIX, *names]:
        try:
            _remove_if_owned_regular(report_dir, name)
        except Exception:
            continue


__all__ = ["DEPS_FORMAT", "DEPS_INFIX", "DESELECT_SUFFIX", "DESELECT_FORMAT",
           "DEPS_MAX_BYTES", "MAX_DIR_ENTRIES", "read_run",
           "write_deselect_binding", "cleanup"]
