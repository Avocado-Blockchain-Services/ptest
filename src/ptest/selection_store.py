"""Per-project SQLite dependency store and parse cache (T3).

The store lives at ``<state>/projects/<project_id>/selection.db`` (0700
dirs, 0600 file) and is opened through ``storage.open_database``, so
hot-journal recovery, safe pragmas and corruption classification come from
there. ``open_store`` raises ``C.Problem`` only.

Schema (no file contents, no source text, no unkeyed digests — only keyed
MACs handed in by the caller, interned paths/qualnames and packed id
sets):

* ``kv``: schema version, audit counters, recorder-inactive facts and the
  last two full-run inventories (node ids absent from both are pruned).
* ``paths`` / ``funcs`` / ``fixtures``: interned vocabulary.
* ``cache``: digest -> encoded ``FileIndex`` blob (the parse cache; only
  rows at the current ``SOURCE_INDEX_VERSION`` are served).
* ``runs`` / ``run_digests``: one baseline per run (keyed digest of every
  ``.py`` and recorded data path) plus the run's ambient deps.
* ``nodes``: one row per recorded test (packed dep sets, outcome, run).
* ``fixture_deps``: per-(fixture, run) dep sets.
* ``demotions``: audit-demoted node ids keyed by test-file digest.

``update`` is a single ``BEGIN IMMEDIATE`` transaction that replaces
exactly the tests that ran; concurrent upserts from two checkouts
serialize on the RESERVED lock (busy_timeout) and the last committed
writer wins. Eviction is oldest-first with the newest run always kept;
hitting the hard cap raises ``capacity-exceeded`` after one evict-and-
retry.

Contracts-barrier seam: the ``SELECTION_*`` constants, helpers and
dataclasses below live in ``contracts.py`` once the section-12 barrier
lands (T5 is the owner of record). Until then this module declares the
identical frozen shapes locally — every name aliases ``contracts`` when
the barrier is present. ``selection_ingest`` reuses these names from
here, so the seam is a single conditional block.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
import time
import zlib
from array import array
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from . import files, storage

_PHASE = "selection-store"
_HEX32 = re.compile(r"[0-9a-f]{32}")

_SCHEMA_VERSION = "1"
_CACHE_PRUNE_AFTER_S = 14 * 24 * 3600
_INVENTORY_CHUNK = 5000


def _fail(code: str, message: str) -> None:
    raise C.Problem(code=code, message=message, phase=_PHASE, retryable=False)


# -- contracts-barrier compatibility (single seam; see module docstring) -----

_HAS_BARRIER = hasattr(C, "SELECTION_PROTOCOL")

if _HAS_BARRIER:
    DepVocabulary = C.DepVocabulary
    ContextDeps = C.ContextDeps
    RunBaseline = C.RunBaseline
    NodeRecord = C.NodeRecord
    FixtureRecord = C.FixtureRecord
    DependencySnapshot = C.DependencySnapshot
    SelectionStoreMeta = C.SelectionStoreMeta
    RecordedNode = C.RecordedNode
    RunDependencies = C.RunDependencies
    selection_ids = C.selection_ids
    selection_test_file = C.selection_test_file
    selection_nodeid_safe = C.selection_nodeid_safe
    selection_normalize_qualname = C.selection_normalize_qualname
    selection_empty_context = C.selection_empty_context
    selection_code_path = C.selection_code_path
    selection_data_path = C.selection_data_path
    selection_store_path = C.selection_store_path
    selection_deps_path = C.selection_deps_path
    selection_deps_pid = C.selection_deps_pid
    selection_deselect_path = C.selection_deselect_path
    SELECTION_STORE_MAX_BYTES = C.SELECTION_STORE_MAX_BYTES
    SELECTION_STORE_TARGET_BYTES = C.SELECTION_STORE_TARGET_BYTES
    SELECTION_MAX_RUNS = C.SELECTION_MAX_RUNS
    SELECTION_OUTCOMES = C.SELECTION_OUTCOMES
    SELECTION_FIXTURE_SCOPES = C.SELECTION_FIXTURE_SCOPES
    SELECTION_DEPS_FORMAT = C.SELECTION_DEPS_FORMAT
    SELECTION_DEPS_INFIX = C.SELECTION_DEPS_INFIX
    SELECTION_DEPS_MAX_BYTES = C.SELECTION_DEPS_MAX_BYTES
    SELECTION_DESELECT_FORMAT = C.SELECTION_DESELECT_FORMAT
    SELECTION_DESELECT_SUFFIX = C.SELECTION_DESELECT_SUFFIX
    SELECTION_DESELECT_MAX_BYTES = C.SELECTION_DESELECT_MAX_BYTES
    SELECTION_DESELECT_MAX_IDS = C.SELECTION_DESELECT_MAX_IDS
    SOURCE_INDEX_VERSION = C.SOURCE_INDEX_VERSION
else:  # Local frozen shapes; replaced by the barrier, never diverged from it.
    SELECTION_STORE_MAX_BYTES = 64 * 1024 * 1024
    SELECTION_STORE_TARGET_BYTES = 48 * 1024 * 1024
    SELECTION_MAX_RUNS = 256
    SELECTION_OUTCOMES = frozenset({
        "passed", "failed", "error", "skipped", "xfailed", "xpassed",
        "unknown",
    })
    SELECTION_FIXTURE_SCOPES = frozenset({"class", "module", "package",
                                          "session"})
    SELECTION_DEPS_FORMAT = "ptest-selection-deps-v1"
    SELECTION_DEPS_INFIX = ".deps-"
    SELECTION_DEPS_MAX_BYTES = 64 * 1024 * 1024
    SELECTION_DESELECT_FORMAT = "ptest-selection-deselect-v1"
    SELECTION_DESELECT_SUFFIX = ".deselect"
    SELECTION_DESELECT_MAX_BYTES = 16 * 1024 * 1024
    SELECTION_DESELECT_MAX_IDS = 200000
    SELECTION_PATH_MAX_BYTES = 4096
    SELECTION_SKIP_DIRS = frozenset({
        "node_modules", "__pycache__", "site-packages", "build", "dist",
        "venv", ".tox", "htmlcov",
    })
    SELECTION_OUTPUT_DIRS = frozenset({
        "build", "dist", "node_modules", ".venv", "venv", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
        ".hypothesis",
    })
    SOURCE_INDEX_VERSION = 1

    def selection_ids(values: Iterable[int]) -> array:
        return array("I", sorted(set(values)))

    def selection_test_file(nodeid: str) -> str:
        return nodeid.split("::", 1)[0]

    def _selection_relpath_ok(path: object) -> bool:
        if (not isinstance(path, str) or not path or "\x00" in path
                or "\\" in path):
            return False
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            return False
        try:
            if len(path.encode("utf-8")) > SELECTION_PATH_MAX_BYTES:
                return False
        except UnicodeEncodeError:
            return False
        return all(part not in ("", ".", "..") for part in path.split("/"))

    def selection_code_path(path: object) -> bool:
        if not _selection_relpath_ok(path) or not path.endswith(".py"):
            return False
        parts = path.split("/")
        return not any(part.startswith(".")
                       or part in SELECTION_SKIP_DIRS
                       or part.endswith(".egg-info") for part in parts[:-1]) \
            and not parts[-1].startswith(".")

    def selection_data_path(path: object) -> bool:
        if not _selection_relpath_ok(path):
            return False
        lowered = path.lower()
        if lowered.endswith((".py", ".pyc", ".pyo")):
            return False
        return not any(part.startswith(".")
                       or part in SELECTION_SKIP_DIRS
                       or part in SELECTION_OUTPUT_DIRS
                       or part.endswith(".egg-info")
                       for part in path.split("/"))

    def selection_nodeid_safe(nodeid: object) -> bool:
        if (not isinstance(nodeid, str) or "::" not in nodeid
                or "\x00" in nodeid):
            return False
        try:
            if len(nodeid.encode("utf-8")) > 4096:
                return False
        except UnicodeEncodeError:
            return False
        return _selection_relpath_ok(nodeid.split("::", 1)[0])

    def selection_normalize_qualname(qualname: object) -> str | None:
        if (not isinstance(qualname, str) or not qualname
                or "\x00" in qualname or len(qualname) > 1024):
            return None
        kept: list[str] = []
        for part in qualname.split("."):
            if not part or part.startswith("<"):
                break
            kept.append(part)
        return ".".join(kept) or None

    def selection_store_path(domain_root: Path, project_id: str) -> Path:
        if (not isinstance(project_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", project_id)):
            raise ValueError("selection store needs a 32-hex project id")
        return (Path(domain_root) / "projects" / project_id
                / "selection.db")

    def selection_deps_path(report_path: Path, pid: int) -> Path:
        if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("dependency file pid must be a positive int")
        return Path(f"{report_path}{SELECTION_DEPS_INFIX}{pid}")

    def selection_deps_pid(report_name: str, name: str) -> int | None:
        prefix = report_name + SELECTION_DEPS_INFIX
        if not name.startswith(prefix):
            return None
        digits = name[len(prefix):]
        if (not 1 <= len(digits) <= 10 or not digits.isascii()
                or not digits.isdigit() or digits[0] == "0"):
            return None
        return int(digits)

    def selection_deselect_path(report_path: Path) -> Path:
        return Path(f"{report_path}{SELECTION_DESELECT_SUFFIX}")

    @dataclass(frozen=True, slots=True, eq=False)
    class DepVocabulary:
        paths: tuple[str, ...]
        functions: tuple[tuple[int, str], ...]
        fixtures: tuple[tuple[str, str, str], ...]

    @dataclass(frozen=True, slots=True, eq=False)
    class ContextDeps:
        functions: array
        modules: array
        data: array
        opaque: bool

    def selection_empty_context(*, opaque: bool = False) -> ContextDeps:
        return ContextDeps(functions=array("I"), modules=array("I"),
                           data=array("I"), opaque=opaque)

    @dataclass(frozen=True, slots=True, eq=False)
    class RunBaseline:
        run_id: str
        recorded_at: float
        compatibility: str
        digests: Mapping[int, str]
        ambient: ContextDeps

    @dataclass(frozen=True, slots=True, eq=False)
    class NodeRecord:
        nodeid: str
        test_file: str
        outcome: str
        run_id: str
        deps: ContextDeps
        fixtures: array

    @dataclass(frozen=True, slots=True, eq=False)
    class FixtureRecord:
        fixture: int
        run_id: str
        deps: ContextDeps

    @dataclass(frozen=True, slots=True, eq=False)
    class DependencySnapshot:
        vocabulary: DepVocabulary
        runs: Mapping[str, RunBaseline]
        nodes: Mapping[str, NodeRecord]
        fixtures: Mapping[int, FixtureRecord]
        demotions: Mapping[str, str]

    @dataclass(frozen=True, slots=True)
    class SelectionStoreMeta:
        size_bytes: int
        nodes: int
        runs: int
        newest_recorded_at: float | None
        python: tuple[int, int] | None
        inactive_reason: str | None
        audit_checked: int
        audit_misses: int
        demoted: int

    @dataclass(frozen=True, slots=True, eq=False)
    class RecordedNode:
        nodeid: str
        outcome: str
        deps: ContextDeps
        fixtures: array

    @dataclass(frozen=True, slots=True, eq=False)
    class RunDependencies:
        vocabulary: DepVocabulary
        nodes: Mapping[str, RecordedNode]
        fixtures: Mapping[int, ContextDeps]
        ambient: ContextDeps
        complete: bool
        recording: bool
        python: tuple[int, int] | None
        inactive_reason: str | None
        notes: tuple[str, ...]


__all__ = [
    "DepVocabulary", "ContextDeps", "RunBaseline", "NodeRecord",
    "FixtureRecord", "DependencySnapshot", "SelectionStoreMeta",
    "RecordedNode", "RunDependencies", "SelectionStore",
    "SELECTION_STORE_MAX_BYTES", "SELECTION_STORE_TARGET_BYTES",
    "open_store", "remove_store",
]


# -- packed contexts --------------------------------------------------------

def _pack_ctx(deps) -> bytes:
    payload = (b'{"f":' + _pack_ids(deps.functions) + b',"m":'
               + _pack_ids(deps.modules) + b',"d":'
               + _pack_ids(deps.data) + b',"o":'
               + (b"1" if deps.opaque else b"0") + b"}")
    return zlib.compress(payload, 6)


def _pack_ids(values) -> bytes:
    return b"[" + b",".join(str(int(item)).encode() for item in values) + b"]"


def _unpack_ctx(blob: bytes):
    import json
    try:
        data = json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError, RecursionError):
        return selection_empty_context(opaque=True)
    if not isinstance(data, dict):
        return selection_empty_context(opaque=True)
    try:
        funcs = selection_ids(int(item) for item in data.get("f", ()))
        modules = selection_ids(int(item) for item in data.get("m", ()))
        datas = selection_ids(int(item) for item in data.get("d", ()))
    except (ValueError, TypeError):
        return selection_empty_context(opaque=True)
    return ContextDeps(functions=funcs, modules=modules, data=datas,
                       opaque=bool(data.get("o", False)))


# -- schema ------------------------------------------------------------------

_TABLES = ("kv", "paths", "funcs", "fixtures", "cache", "runs",
           "run_digests", "nodes", "fixture_deps", "demotions")

_SCHEMA_SQL = """
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


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    for statement in _SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement.replace(
                "CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    conn.execute("INSERT OR IGNORE INTO kv(key, value)"
                 " VALUES('schema_version', ?)", (_SCHEMA_VERSION,))
    conn.execute("INSERT OR IGNORE INTO kv(key, value)"
                 " VALUES('audit_checked', '0')")
    conn.execute("INSERT OR IGNORE INTO kv(key, value)"
                 " VALUES('audit_misses', '0')")


def _tables_present(conn: sqlite3.Connection) -> set[str]:
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error:
        _fail("coordinator-corrupt", "selection store is unreadable")
        raise AssertionError("unreachable")


def _read_schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        _fail("coordinator-corrupt", "selection store is unreadable")
        raise AssertionError("unreachable")
    return row[0] if row else None


def _check_schema_version(conn: sqlite3.Connection) -> None:
    if _read_schema_version(conn) != _SCHEMA_VERSION:
        _fail("coordinator-corrupt", "selection store has an unknown schema")


def _check_schema(conn: sqlite3.Connection) -> None:
    tables = _tables_present(conn)
    if (set(_TABLES) <= tables
            and _read_schema_version(conn) == _SCHEMA_VERSION):
        return
    # Empty, partial, or version-row-not-yet-visible: another process may
    # be creating the schema right now (its CREATE TABLE batch is not
    # atomic across connections, and its kv rows land after the last
    # CREATE). Serialize on the RESERVED lock, then look again — only a
    # store that is still incomplete under the lock is corrupt.
    try:
        _begin_immediate(conn)
    except sqlite3.Error as exc:
        if storage.is_transient_sqlite(exc):
            _fail_retryable("coordinator-unavailable",
                            "selection store is busy")
            raise AssertionError("unreachable")
        if _is_io_error(exc):
            _fail("state-unavailable", "selection store is unavailable")
            raise AssertionError("unreachable")
        _fail("coordinator-corrupt", "selection store is unreadable")
        raise AssertionError("unreachable")
    try:
        tables = _tables_present(conn)
        if not tables:
            try:
                _create_schema(conn)
            except sqlite3.Error:
                _fail("coordinator-corrupt",
                      "selection store cannot be initialised")
                raise AssertionError("unreachable")
        elif not set(_TABLES) <= tables:
            _fail("coordinator-corrupt",
                  "selection store has an unknown schema")
            raise AssertionError("unreachable")
        _check_schema_version(conn)
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if storage.is_transient_sqlite(exc):
                _fail_retryable("coordinator-unavailable",
                                "selection store is busy")
                raise AssertionError("unreachable")
            if _is_io_error(exc):
                _fail("state-unavailable", "selection store is unavailable")
                raise AssertionError("unreachable")
            _fail("coordinator-corrupt",
                  "selection store cannot be initialised")
            raise AssertionError("unreachable")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# -- open / remove ------------------------------------------------------------

def _project_dir(domain: C.DomainPaths, project_id: str, *,
                 create: bool) -> Path:
    try:
        root = Path(domain.root)
    except (TypeError, ValueError):
        _fail("state-unavailable", "state root is unavailable")
        raise AssertionError("unreachable")
    projects = root / "projects"
    project = projects / project_id
    if create:
        try:
            stamp = os.lstat(root)
        except FileNotFoundError:
            _fail("state-unavailable", "state root does not exist")
            raise AssertionError("unreachable")
        except OSError:
            _fail("state-unavailable", "state root is unavailable")
            raise AssertionError("unreachable")
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            _fail("unsafe-path", "state root is unsafe")
        if stamp.st_uid != os.getuid():
            _fail("unsafe-path", "state root has a foreign owner")
        try:
            files.ensure_private_dir(root, "projects")
            files.ensure_private_dir(projects, project_id)
        except C.Problem:
            raise
        except OSError:
            _fail("state-unavailable", "selection store dir is unavailable")
            raise AssertionError("unreachable")
        return project
    for candidate in (projects, project):
        try:
            stamp = os.lstat(candidate)
        except FileNotFoundError:
            _fail("state-unavailable", "selection store does not exist")
            raise AssertionError("unreachable")
        except OSError:
            _fail("state-unavailable", "selection store is unavailable")
            raise AssertionError("unreachable")
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            _fail("unsafe-path", f"path {candidate.name} is unsafe")
        if stamp.st_uid != os.getuid():
            _fail("unsafe-path", f"path {candidate.name} has a foreign owner")
    return project


def _check_db_file(db: Path) -> os.stat_result | None:
    try:
        stamp = os.lstat(db)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("state-unavailable", "selection store is unavailable")
        raise AssertionError("unreachable")
    if stat.S_ISLNK(stamp.st_mode):
        _fail("unsafe-path", "selection store is a symlink")
    if not stat.S_ISREG(stamp.st_mode):
        _fail("unsafe-path", "selection store is not a regular file")
    if stamp.st_uid != os.getuid():
        _fail("unsafe-path", "selection store has a foreign owner")
    if stat.S_IMODE(stamp.st_mode) != 0o600:
        _fail("unsafe-path", "selection store must be mode 0600")
    if stamp.st_nlink != 1:
        _fail("unsafe-path", "selection store must have exactly one link")
    if stamp.st_size > SELECTION_STORE_MAX_BYTES:
        _fail("capacity-exceeded", "selection store exceeds its size cap")
    return stamp


def open_store(domain: C.DomainPaths, project_id: str, *,
               create: bool) -> SelectionStore:
    """Open the project's dependency store, creating it when asked.

    Raises ``C.Problem`` only: ``state-unavailable`` (absent with
    ``create=False``, or I/O), ``unsafe-path`` (symlink/foreign/mode/link
    on the dir or db), ``coordinator-corrupt`` (unreadable schema) and
    ``capacity-exceeded`` (over the hard cap).
    """
    if (not isinstance(project_id, str)
            or not _HEX32.fullmatch(project_id)):
        _fail("unsafe-path", "selection store needs a 32-hex project id")
    if not isinstance(create, bool):
        raise TypeError("create must be bool")
    project = _project_dir(domain, project_id, create=create)
    db = project / "selection.db"
    stamp = _check_db_file(db)
    if stamp is None and not create:
        _fail("state-unavailable", "selection store does not exist")
    try:
        conn = storage.open_database(project, "selection.db",
                                     max_bytes=SELECTION_STORE_MAX_BYTES)
    except C.Problem as exc:
        if exc.code == "already-exists":
            try:
                conn = storage.open_database(
                    project, "selection.db",
                    max_bytes=SELECTION_STORE_MAX_BYTES)
            except C.Problem:
                raise
        else:
            raise
    try:
        conn.isolation_level = None
        _check_schema(conn)
    except C.Problem:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise
    return SelectionStore(conn, db, project_id)


def remove_store(domain: C.DomainPaths, project_id: str) -> bool:
    """Identity-checked unlink of ``selection.db`` (plus its journal).

    Never follows links. True when the store itself was removed, False
    when absent or unsafe.
    """
    if (not isinstance(project_id, str)
            or not _HEX32.fullmatch(project_id)):
        return False
    try:
        project = Path(domain.root) / "projects" / project_id
    except (TypeError, ValueError):
        return False
    removed = False
    for leaf in ("selection.db", "selection.db-journal"):
        path = project / leaf
        try:
            stamp = os.lstat(path)
        except OSError:
            continue
        if (stat.S_ISLNK(stamp.st_mode)
                or not stat.S_ISREG(stamp.st_mode)
                or stamp.st_uid != os.getuid()):
            continue
        try:
            if files.unlink_if_same(project, leaf, stamp.st_dev,
                                    stamp.st_ino):
                removed = removed or leaf == "selection.db"
        except C.Problem:
            continue
        except OSError:
            continue
    return removed


def _is_full(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and not isinstance(code, bool):
        if code & 0xFF == 13:
            return True
    return "full" in str(exc).lower()


def _fail_retryable(code: str, message: str) -> None:
    raise C.Problem(code=code, message=message, phase=_PHASE, retryable=True)


def _is_io_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and not isinstance(code, bool):
        if code & 0xFF == 10:
            return True
    return "disk i/o error" in str(exc).lower()


# Upper bound for acquiring the RESERVED lock. A full-size update holds
# it for several seconds (measured ~6.5 s for 20k nodes), far past the
# connection's 2 s busy_timeout, so one busy_timeout wait is not enough.
_BEGIN_IMMEDIATE_BUDGET_S = 60.0
_BEGIN_IMMEDIATE_POLL_S = 0.05


def _map_update_error(exc: sqlite3.Error) -> None:
    """Raise the typed ``C.Problem`` for a failed update (never returns
    without raising: a lock/I-O failure becomes a Problem, anything else
    is re-raised unchanged)."""
    if storage.is_transient_sqlite(exc):
        _fail_retryable("coordinator-unavailable",
                        "selection store is busy")
    if _is_io_error(exc):
        _fail("state-unavailable", "selection store is unavailable")
    raise exc


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Take the RESERVED lock, waiting past a concurrent big writer.

    Each ``BEGIN IMMEDIATE`` waits only one busy_timeout; a realistic
    update holds the lock longer than that, so retry a busy/locked
    failure until the budget runs out. Anything else raises at once.
    """
    deadline = time.monotonic() + _BEGIN_IMMEDIATE_BUDGET_S
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.Error as exc:
            if not storage.is_transient_sqlite(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(_BEGIN_IMMEDIATE_POLL_S)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _unpack_ids(blob: bytes) -> array:
    import json
    try:
        data = json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError, RecursionError):
        return array("I")
    if not isinstance(data, dict):
        return array("I")
    try:
        return selection_ids(int(item) for item in data.get("f", ()))
    except (ValueError, TypeError):
        return array("I")


def _pack_fixture_ids(values: Iterable[int]) -> bytes:
    import json
    return zlib.compress(json.dumps(
        {"f": sorted(set(int(item) for item in values))},
        ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _pack_inv(nodeids: Iterable[str]) -> str:
    import json
    return json.dumps(sorted(set(nodeids)), ensure_ascii=True,
                      separators=(",", ":"))


def _unpack_inv(text: str | None) -> frozenset[str]:
    import json
    if not text:
        return frozenset()
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(item for item in data if isinstance(item, str))


@dataclass(frozen=True, slots=True)
class _PlannedUpdate:
    run_id: str
    recorded_at: float
    compatibility: str
    digests: dict
    full: bool
    vocab_paths: tuple
    vocab_funcs: tuple
    vocab_fixtures: tuple
    fixture_keys: dict
    nodes: dict
    fixtures: dict
    ambient: object
    bad: frozenset


class SelectionStore:
    """One project's dependency store; also the parse cache."""

    def __init__(self, conn: sqlite3.Connection, path: Path,
                 project_id: str) -> None:
        self._conn = conn
        self._path = Path(path)
        self._project_id = project_id
        self._lock = threading.Lock()
        self._closed = False

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> SelectionStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _guard(self) -> sqlite3.Connection:
        if self._closed:
            _fail("state-unavailable", "selection store is closed")
            raise AssertionError("unreachable")
        return self._conn

    # -- parse cache -----------------------------------------------------

    def get_many(self, digests: Iterable[str]) -> dict[str, bytes]:
        """Digest -> blob for cached rows at the current index version."""
        with self._lock:
            conn = self._guard()
            wanted = [item for item in digests]
            for item in wanted:
                if not isinstance(item, str) or not item:
                    raise ValueError("cache digests must be nonempty str")
            found: dict[str, bytes] = {}
            for chunk in _chunks(wanted, 500):
                if not chunk:
                    continue
                rows = conn.execute(
                    "SELECT digest, blob FROM cache WHERE digest IN (%s)"
                    " AND version = ?" % ",".join("?" * len(chunk)),
                    (*chunk, SOURCE_INDEX_VERSION)).fetchall()
                for digest, blob in rows:
                    found[digest] = bytes(blob)
            if found:
                now = time.time()
                for chunk in _chunks(list(found), 500):
                    conn.execute(
                        "UPDATE cache SET used_at = ? WHERE digest IN (%s)"
                        % ",".join("?" * len(chunk)),
                        (now, *chunk))
            return found

    def put_many(self, blobs: Mapping[str, bytes]) -> None:
        """Store digest -> encoded-index blobs at the current version."""
        with self._lock:
            conn = self._guard()
            items = list(blobs.items())
            for digest, blob in items:
                if not isinstance(digest, str) or not digest:
                    raise ValueError("cache digests must be nonempty str")
                if not isinstance(blob, (bytes, bytearray, memoryview)):
                    raise ValueError("cache blobs must be bytes")
            now = time.time()
            for digest, blob in items:
                conn.execute(
                    "INSERT OR REPLACE INTO cache(digest, version, blob,"
                    " used_at) VALUES(?, ?, ?, ?)",
                    (digest, SOURCE_INDEX_VERSION, bytes(blob), now))

    # -- snapshot / meta ---------------------------------------------------

    def snapshot(self) -> DependencySnapshot:
        """The whole store as the planner sees it (dense re-indexed)."""
        with self._lock:
            conn = self._guard()
            path_of = {row[0]: row[1] for row in conn.execute(
                "SELECT id, path FROM paths")}
            func_of = {row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT id, path_id, qualname FROM funcs")}
            fixture_key_of = {row[0]: (row[1], row[2], row[3])
                              for row in conn.execute(
                                  "SELECT id, baseid, argname, scope"
                                  " FROM fixtures")}
            run_rows = conn.execute(
                "SELECT run_id, recorded_at, compatibility, ambient"
                " FROM runs").fetchall()
            digest_rows = conn.execute(
                "SELECT run_id, path_id, digest FROM run_digests").fetchall()
            node_rows = conn.execute(
                "SELECT nodeid, test_file, outcome, run_id, deps, fixtures"
                " FROM nodes").fetchall()
            fixture_rows = conn.execute(
                "SELECT fixture_id, run_id, deps FROM fixture_deps").fetchall()
            demotions = {row[0]: row[1] for row in conn.execute(
                "SELECT nodeid, digest FROM demotions")}

            used_paths: set[str] = set()
            for _, path_id, _ in digest_rows:
                if path_id in path_of:
                    used_paths.add(path_of[path_id])
            raw_ctxs = ([_unpack_ctx(row[4]) for row in node_rows]
                        + [_unpack_ctx(row[2]) for row in fixture_rows]
                        + [_unpack_ctx(row[3]) for row in run_rows])
            for ctx in raw_ctxs:
                for fid in ctx.functions:
                    if fid in func_of and func_of[fid][0] in path_of:
                        used_paths.add(path_of[func_of[fid][0]])
                for pid in (*ctx.modules, *ctx.data):
                    if pid in path_of:
                        used_paths.add(path_of[pid])
            path_index = {path: num for num, path in
                          enumerate(sorted(used_paths))}

            used_funcs: set[tuple[int, str]] = set()
            for ctx in raw_ctxs:
                for fid in ctx.functions:
                    if fid in func_of and func_of[fid][0] in path_of:
                        used_funcs.add((path_index[path_of[func_of[fid][0]]],
                                        func_of[fid][1]))
            func_index = {key: num for num, key in
                          enumerate(sorted(used_funcs))}

            used_fixtures = {fixture_key_of[row[0]] for row in fixture_rows
                             if row[0] in fixture_key_of}
            for row in node_rows:
                for fid in _unpack_ids(row[5]):
                    if fid in fixture_key_of:
                        used_fixtures.add(fixture_key_of[fid])
            fixture_index = {key: num for num, key in
                             enumerate(sorted(used_fixtures))}

            def remap(ctx) -> ContextDeps:
                funcs = selection_ids(
                    func_index[(path_index[path_of[func_of[f][0]]],
                                func_of[f][1])]
                    for f in ctx.functions
                    if f in func_of and func_of[f][0] in path_of)
                modules = selection_ids(
                    path_index[path_of[p]] for p in ctx.modules
                    if p in path_of)
                datas = selection_ids(
                    path_index[path_of[p]] for p in ctx.data
                    if p in path_of)
                return ContextDeps(functions=funcs, modules=modules,
                                   data=datas, opaque=ctx.opaque)

            runs: dict[str, RunBaseline] = {}
            for run_id, recorded_at, compatibility, ambient in run_rows:
                runs[run_id] = RunBaseline(
                    run_id=run_id, recorded_at=recorded_at,
                    compatibility=compatibility, digests={},
                    ambient=remap(_unpack_ctx(ambient)))
            for run_id, path_id, digest in digest_rows:
                if run_id in runs and path_id in path_of:
                    path = path_of[path_id]
                    if path in path_index:
                        runs[run_id].digests[path_index[path]] = digest

            nodes: dict[str, NodeRecord] = {}
            for nodeid, test_file, outcome, run_id, deps, fixtures in \
                    node_rows:
                nodes[nodeid] = NodeRecord(
                    nodeid=nodeid, test_file=test_file, outcome=outcome,
                    run_id=run_id, deps=remap(_unpack_ctx(deps)),
                    fixtures=selection_ids(
                        fixture_index[fixture_key_of[f]]
                        for f in _unpack_ids(fixtures)
                        if f in fixture_key_of))

            fixtures_out: dict[int, FixtureRecord] = {}
            for fixture_id, run_id, deps in fixture_rows:
                if fixture_id in fixture_key_of:
                    dense = fixture_index[fixture_key_of[fixture_id]]
                    fixtures_out[dense] = FixtureRecord(
                        fixture=dense, run_id=run_id,
                        deps=remap(_unpack_ctx(deps)))

            vocabulary = DepVocabulary(
                paths=tuple(sorted(used_paths)),
                functions=tuple(key for key, _ in sorted(
                    func_index.items(), key=lambda item: item[1])),
                fixtures=tuple(key for key, _ in sorted(
                    fixture_index.items(), key=lambda item: item[1])))
            return DependencySnapshot(
                vocabulary=vocabulary, runs=runs, nodes=nodes,
                fixtures=fixtures_out, demotions=dict(demotions))

    def meta(self) -> SelectionStoreMeta:
        """Store facts for status lines (human only)."""
        with self._lock:
            conn = self._guard()
            nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            newest = conn.execute(
                "SELECT MAX(recorded_at) FROM runs").fetchone()[0]
            demoted = conn.execute(
                "SELECT COUNT(*) FROM demotions").fetchone()[0]
            kv = {row[0]: row[1] for row in conn.execute(
                "SELECT key, value FROM kv WHERE key IN "
                "('audit_checked', 'audit_misses', 'inactive_reason',"
                " 'python_major', 'python_minor')")}
            try:
                size = os.stat(self._path).st_size
            except OSError:
                size = 0
            major, minor = kv.get("python_major"), kv.get("python_minor")
            python = None
            if major is not None and minor is not None:
                try:
                    python = (int(major), int(minor))
                except ValueError:
                    python = None
            return SelectionStoreMeta(
                size_bytes=size, nodes=int(nodes), runs=int(runs),
                newest_recorded_at=newest, python=python,
                inactive_reason=kv.get("inactive_reason"),
                audit_checked=int(kv.get("audit_checked", "0") or "0"),
                audit_misses=int(kv.get("audit_misses", "0") or "0"),
                demoted=int(demoted))

    # -- update ------------------------------------------------------------

    def update(self, run, *, run_id: str, recorded_at: float,
               compatibility: str, digests: Mapping[str, str | None],
               full: bool) -> None:
        """Record one run: one transaction replacing only the tests it ran.

        Never fails for size (oldest-first eviction keeps the newest run);
        raises ``capacity-exceeded`` only when even that cannot fit, after
        one evict-and-retry. A lock held past the wait budget raises
        retryable ``coordinator-unavailable`` and an I/O failure raises
        ``state-unavailable`` (never a raw sqlite error).
        """
        planned = self._plan_update(run, run_id=run_id,
                                    recorded_at=recorded_at,
                                    compatibility=compatibility,
                                    digests=digests, full=full)
        with self._lock:
            conn = self._guard()
            try:
                if self._apply_update(conn, planned):
                    self._vacuum(conn)
                return
            except sqlite3.Error as exc:
                if not _is_full(exc):
                    _map_update_error(exc)
                    raise AssertionError("unreachable")
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self._evict_oldest(conn, exclude=planned.run_id)
                self._vacuum(conn)
                try:
                    if self._apply_update(conn, planned):
                        self._vacuum(conn)
                    return
                except sqlite3.Error as retry_exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    if _is_full(retry_exc):
                        _fail("capacity-exceeded",
                              "selection store cannot fit the newest run")
                    _map_update_error(retry_exc)
                    raise

    def _plan_update(self, run, *, run_id, recorded_at, compatibility,
                     digests, full) -> _PlannedUpdate:
        if not isinstance(run_id, str) or not _HEX32.fullmatch(run_id):
            raise ValueError("run_id must be a 32-hex string")
        if isinstance(recorded_at, bool) or not isinstance(
                recorded_at, (int, float)):
            raise ValueError("recorded_at must be a number")
        if not isinstance(compatibility, str) or not compatibility:
            raise ValueError("compatibility must be a nonempty string")
        if not isinstance(digests, Mapping):
            raise ValueError("digests must be a mapping")
        for path, digest in digests.items():
            if not isinstance(path, str) or not path:
                raise ValueError("digest paths must be nonempty str")
            if digest is not None and not isinstance(digest, str):
                raise ValueError("digests must be str or None")
        if not isinstance(full, bool):
            raise ValueError("full must be bool")
        try:
            vocabulary = run.vocabulary
            nodes = dict(run.nodes)
            fixtures = dict(run.fixtures)
            ambient = run.ambient
        except (AttributeError, TypeError, ValueError):
            raise ValueError("run must look like RunDependencies")
        try:
            vocab_paths = tuple(vocabulary.paths)
            vocab_funcs = tuple(vocabulary.functions)
            vocab_fixtures = tuple(vocabulary.fixtures)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("run vocabulary is malformed")
        for path in vocab_paths:
            if not isinstance(path, str):
                raise ValueError("vocabulary paths must be str")
        for entry in vocab_funcs:
            try:
                path_idx, qualname = entry
            except (TypeError, ValueError):
                raise ValueError("vocabulary functions must be pairs")
            if (not isinstance(path_idx, int) or isinstance(path_idx, bool)
                    or not isinstance(qualname, str)):
                raise ValueError("vocabulary functions must be pairs")
            if not 0 <= path_idx < len(vocab_paths):
                raise ValueError("vocabulary function path is out of range")
        fixture_keys = {}
        for entry in vocab_fixtures:
            try:
                baseid, argname, scope = entry
            except (TypeError, ValueError):
                raise ValueError("vocabulary fixtures must be triples")
            if (not isinstance(baseid, str) or not isinstance(argname, str)
                    or scope not in SELECTION_FIXTURE_SCOPES):
                raise ValueError("vocabulary fixture key is malformed")
            fixture_keys[baseid, argname, scope] = entry
        referenced: set[str] = set()
        for entry in vocab_funcs:
            referenced.add(vocab_paths[entry[0]])
        renoded: dict = {}
        for node in nodes.values():
            self._check_node(node, len(vocab_funcs), len(vocab_fixtures),
                             len(vocab_paths))
            renoded[node.nodeid] = node
        nodes = renoded
        for node in nodes.values():
            for index in (*node.deps.modules, *node.deps.data):
                referenced.add(vocab_paths[int(index)])
            for index in node.fixtures:
                key = vocab_fixtures[int(index)]
                fixture_keys.setdefault(key, key)
        for index, ctx in fixtures.items():
            if (not isinstance(index, int) or isinstance(index, bool)
                    or index not in range(len(vocab_fixtures))):
                raise ValueError("fixture index is out of range")
            self._check_ctx(ctx, len(vocab_funcs), len(vocab_paths))
            key = vocab_fixtures[int(index)]
            fixture_keys.setdefault(key, key)
        self._check_ctx(ambient, len(vocab_funcs), len(vocab_paths))
        for index in (*ambient.modules, *ambient.data):
            referenced.add(vocab_paths[int(index)])
        bad = {path for path in referenced if digests.get(path) is None}
        return _PlannedUpdate(
            run_id=run_id, recorded_at=float(recorded_at),
            compatibility=compatibility, digests=dict(digests), full=full,
            vocab_paths=vocab_paths, vocab_funcs=vocab_funcs,
            vocab_fixtures=vocab_fixtures, fixture_keys=fixture_keys,
            nodes=nodes, fixtures=fixtures, ambient=ambient, bad=bad)

    @staticmethod
    def _check_ctx(ctx, n_funcs: int, n_paths: int) -> None:
        try:
            funcs = tuple(ctx.functions)
            modules = tuple(ctx.modules)
            data = tuple(ctx.data)
            opaque = ctx.opaque
        except (AttributeError, TypeError, ValueError):
            raise ValueError("context deps are malformed")
        if not isinstance(opaque, bool):
            raise ValueError("context opaque must be bool")
        for index in funcs:
            if (not isinstance(index, int) or isinstance(index, bool)
                    or index not in range(n_funcs)):
                raise ValueError("function index is out of range")
        for index in (*modules, *data):
            if (not isinstance(index, int) or isinstance(index, bool)
                    or index not in range(n_paths)):
                raise ValueError("path index is out of range")

    @staticmethod
    def _check_node(node, n_funcs: int, n_fixtures: int, n_paths: int) -> None:
        try:
            nodeid = node.nodeid
            outcome = node.outcome
            fixtures = tuple(node.fixtures)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("recorded node is malformed")
        if not isinstance(nodeid, str) or not nodeid:
            raise ValueError("node ids must be nonempty str")
        if outcome not in SELECTION_OUTCOMES:
            raise ValueError(f"node outcome {outcome!r} is unknown")
        for index in fixtures:
            if (not isinstance(index, int) or isinstance(index, bool)
                    or index not in range(n_fixtures)):
                raise ValueError("node fixture index is out of range")
        SelectionStore._check_ctx(node.deps, n_funcs, n_paths)

    def _apply_update(self, conn: sqlite3.Connection,
                      planned: _PlannedUpdate) -> bool:
        """Run the update transaction; True when rows were evicted."""
        _begin_immediate(conn)
        try:
            path_ids = self._intern_paths(conn, planned)
            func_ids = self._intern_funcs(conn, planned, path_ids)
            fixture_ids = self._intern_fixtures(conn, planned)
            self._store_run(conn, planned, path_ids)
            self._store_nodes(conn, planned, path_ids, func_ids,
                              fixture_ids)
            self._store_fixtures(conn, planned, path_ids, func_ids,
                                 fixture_ids)
            if planned.full:
                self._prune_full(conn, planned)
            conn.execute(
                "DELETE FROM runs WHERE run_id NOT IN"
                " (SELECT DISTINCT run_id FROM nodes)")
            self._prune_cache(conn)
            evicted = self._evict_to_target(conn, exclude=planned.run_id)
            conn.execute("COMMIT")
            return evicted
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _vacuum(conn: sqlite3.Connection) -> None:
        """Compact the file; best-effort (size hygiene, never correctness).

        Runs outside any transaction, after the commit that freed the
        rows: row deletes leave freeblocks inside pages, which only a
        rebuild returns to the filesystem. A busy/locked/full failure is
        ignored — the file stays larger and the next update retries.
        """
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass

    def _intern_paths(self, conn: sqlite3.Connection,
                      planned: _PlannedUpdate) -> dict[str, int]:
        for path in (*planned.digests, *planned.vocab_paths):
            conn.execute("INSERT OR IGNORE INTO paths(path) VALUES(?)",
                         (path,))
        return {row[0]: row[1] for row in conn.execute("SELECT path, id"
                                                       " FROM paths")}

    def _intern_funcs(self, conn: sqlite3.Connection,
                      planned: _PlannedUpdate,
                      path_ids: dict[str, int]) -> dict[tuple[int, str], int]:
        for path_idx, qualname in planned.vocab_funcs:
            conn.execute(
                "INSERT OR IGNORE INTO funcs(path_id, qualname)"
                " VALUES(?, ?)",
                (path_ids[planned.vocab_paths[path_idx]], qualname))
        return {(row[0], row[1]): row[2] for row in conn.execute(
            "SELECT path_id, qualname, id FROM funcs")}

    def _intern_fixtures(self, conn: sqlite3.Connection,
                         planned: _PlannedUpdate) -> dict[tuple, int]:
        for key in planned.fixture_keys.values():
            baseid, argname, scope = key
            conn.execute(
                "INSERT OR IGNORE INTO fixtures(baseid, argname, scope)"
                " VALUES(?, ?, ?)", (baseid, argname, scope))
        return {(row[0], row[1], row[2]): row[3] for row in conn.execute(
            "SELECT baseid, argname, scope, id FROM fixtures")}

    def _opaque(self, ctx, planned: _PlannedUpdate,
                path_ids: dict[str, int], func_ids: dict) -> ContextDeps:
        if ctx.opaque:
            opaque = True
        else:
            opaque = False
            for fid in ctx.functions:
                entry = planned.vocab_funcs[int(fid)]
                if planned.vocab_paths[entry[0]] in planned.bad:
                    opaque = True
                    break
            if not opaque:
                for pid in (*ctx.modules, *ctx.data):
                    if planned.vocab_paths[int(pid)] in planned.bad:
                        opaque = True
                        break
        return ContextDeps(
            functions=selection_ids(
                func_ids[path_ids[planned.vocab_paths[
                    planned.vocab_funcs[int(f)][0]]],
                    planned.vocab_funcs[int(f)][1]]
                for f in ctx.functions),
            modules=selection_ids(
                path_ids[planned.vocab_paths[int(p)]]
                for p in ctx.modules),
            data=selection_ids(
                path_ids[planned.vocab_paths[int(p)]] for p in ctx.data),
            opaque=opaque)

    def _store_run(self, conn: sqlite3.Connection,
                   planned: _PlannedUpdate, path_ids: dict[str, int]) -> None:
        ambient = self._opaque(planned.ambient, planned, path_ids, {})
        conn.execute(
            "INSERT OR REPLACE INTO runs(run_id, recorded_at,"
            " compatibility, ambient) VALUES(?, ?, ?, ?)",
            (planned.run_id, planned.recorded_at, planned.compatibility,
             _pack_ctx(ambient)))
        conn.execute("DELETE FROM run_digests WHERE run_id = ?",
                     (planned.run_id,))
        for path, digest in planned.digests.items():
            if digest is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO run_digests(run_id, path_id, digest)"
                " VALUES(?, ?, ?)",
                (planned.run_id, path_ids[path], digest))

    def _store_nodes(self, conn: sqlite3.Connection,
                     planned: _PlannedUpdate, path_ids: dict[str, int],
                     func_ids: dict, fixture_ids: dict) -> None:
        fresh = set(planned.nodes)
        old = {row[0] for row in conn.execute(
            "SELECT nodeid FROM nodes WHERE run_id = ?", (planned.run_id,))}
        for nodeid in old - fresh:
            conn.execute("DELETE FROM nodes WHERE nodeid = ?", (nodeid,))
        for nodeid, node in planned.nodes.items():
            deps = self._opaque(node.deps, planned, path_ids, func_ids)
            refs = selection_ids(
                fixture_ids[planned.fixture_keys[
                    planned.vocab_fixtures[int(f)]]]
                for f in node.fixtures)
            conn.execute(
                "INSERT OR REPLACE INTO nodes(nodeid, test_file, outcome,"
                " run_id, deps, fixtures) VALUES(?, ?, ?, ?, ?, ?)",
                (nodeid, selection_test_file(nodeid), node.outcome,
                 planned.run_id, _pack_ctx(deps),
                 _pack_fixture_ids(refs)))

    def _store_fixtures(self, conn: sqlite3.Connection,
                        planned: _PlannedUpdate, path_ids: dict[str, int],
                        func_ids: dict, fixture_ids: dict) -> None:
        conn.execute("DELETE FROM fixture_deps WHERE run_id = ?",
                     (planned.run_id,))
        for index, ctx in planned.fixtures.items():
            key = planned.fixture_keys[planned.vocab_fixtures[int(index)]]
            deps = self._opaque(ctx, planned, path_ids, func_ids)
            conn.execute(
                "INSERT OR REPLACE INTO fixture_deps(fixture_id, run_id,"
                " deps) VALUES(?, ?, ?)",
                (fixture_ids[key], planned.run_id, _pack_ctx(deps)))

    def _prune_full(self, conn: sqlite3.Connection,
                    planned: _PlannedUpdate) -> None:
        fresh = sorted(planned.nodes)
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'last_full_inv'").fetchone()
        previous = _unpack_inv(row[0] if row else None)
        keep = set(fresh) | set(previous)
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES('last_full_inv',"
            " ?)", (_pack_inv(fresh),))
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES('prev_full_inv',"
            " ?)", (_pack_inv(sorted(previous)),))
        conn.execute("CREATE TEMPORARY TABLE keep_nodes(nodeid TEXT"
                     " PRIMARY KEY)")
        try:
            for chunk in _chunks(sorted(keep), _INVENTORY_CHUNK):
                if chunk:
                    conn.execute(
                        "INSERT OR IGNORE INTO keep_nodes(nodeid) VALUES %s"
                        % ",".join(["(?)"] * len(chunk)), tuple(chunk))
            conn.execute(
                "DELETE FROM nodes WHERE nodeid NOT IN"
                " (SELECT nodeid FROM keep_nodes)")
        finally:
            conn.execute("DROP TABLE keep_nodes")

    def _prune_cache(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - _CACHE_PRUNE_AFTER_S
        conn.execute(
            "DELETE FROM cache WHERE used_at < ? AND digest NOT IN"
            " (SELECT digest FROM run_digests)", (cutoff,))

    def _db_size(self, conn: sqlite3.Connection) -> int:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count - freelist_count) * int(page_size)

    def _evict_to_target(self, conn: sqlite3.Connection,
                         *, exclude: str) -> bool:
        """Oldest-first eviction; True when any rows were evicted."""
        evicted = False
        while True:
            runs = conn.execute(
                "SELECT run_id FROM runs ORDER BY recorded_at ASC,"
                " rowid ASC").fetchall()
            run_ids = [row[0] for row in runs]
            oversize = self._db_size(conn) > SELECTION_STORE_TARGET_BYTES
            if ((not oversize and len(run_ids) <= SELECTION_MAX_RUNS)
                    or len(run_ids) <= 1):
                return evicted
            victim = next((rid for rid in run_ids if rid != exclude),
                          run_ids[0] if exclude not in run_ids else None)
            if victim is None:
                return evicted
            conn.execute("DELETE FROM nodes WHERE run_id = ?", (victim,))
            conn.execute(
                "DELETE FROM runs WHERE run_id NOT IN"
                " (SELECT DISTINCT run_id FROM nodes)")
            evicted = True
            try:
                conn.execute("PRAGMA incremental_vacuum").fetchall()
            except sqlite3.Error:
                pass

    def _evict_oldest(self, conn: sqlite3.Connection, *,
                      exclude: str) -> None:
        try:
            _begin_immediate(conn)
        except sqlite3.Error:
            return
        try:
            rows = conn.execute(
                "SELECT run_id FROM runs ORDER BY recorded_at ASC,"
                " rowid ASC").fetchall()
            run_ids = [row[0] for row in rows]
            victim = next((rid for rid in run_ids if rid != exclude), None)
            if victim is not None:
                conn.execute("DELETE FROM nodes WHERE run_id = ?",
                             (victim,))
                conn.execute(
                    "DELETE FROM runs WHERE run_id NOT IN"
                    " (SELECT DISTINCT run_id FROM nodes)")
            try:
                conn.execute("PRAGMA incremental_vacuum").fetchall()
            except sqlite3.Error:
                pass
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    # -- outcomes / invalidate / demote / audit / inactive -------------------

    def mark_outcomes(self, outcomes: Mapping[str, str]) -> None:
        """Refresh outcomes of recorded tests; unknown ids are ignored."""
        with self._lock:
            conn = self._guard()
            for nodeid, outcome in dict(outcomes).items():
                if not isinstance(nodeid, str) or not nodeid:
                    raise ValueError("node ids must be nonempty str")
                if outcome not in SELECTION_OUTCOMES:
                    raise ValueError(f"node outcome {outcome!r} is unknown")
                conn.execute("UPDATE nodes SET outcome = ? WHERE nodeid = ?",
                             (outcome, nodeid))

    def invalidate(self, test_files: Iterable[str] | None) -> None:
        """Mark records outcome ``unknown`` (all when ``None``)."""
        with self._lock:
            conn = self._guard()
            if test_files is None:
                conn.execute("UPDATE nodes SET outcome = 'unknown'")
                return
            wanted = list(test_files)
            for path in wanted:
                if not isinstance(path, str) or not path:
                    raise ValueError("test files must be nonempty str")
            for chunk in _chunks(wanted, 500):
                if chunk:
                    conn.execute(
                        "UPDATE nodes SET outcome = 'unknown'"
                        " WHERE test_file IN (%s)"
                        % ",".join("?" * len(chunk)), tuple(chunk))

    def demote(self, nodeids: Mapping[str, str]) -> None:
        """Audit-demote node ids keyed by their test-file digest."""
        with self._lock:
            conn = self._guard()
            for nodeid, digest in dict(nodeids).items():
                if not isinstance(nodeid, str) or not nodeid:
                    raise ValueError("node ids must be nonempty str")
                if not isinstance(digest, str) or not digest:
                    raise ValueError("demotion digests must be nonempty str")
                conn.execute(
                    "INSERT OR REPLACE INTO demotions(nodeid, digest)"
                    " VALUES(?, ?)", (nodeid, digest))

    def record_audit(self, checked: int, misses: int) -> None:
        """Accumulate self-audit counters."""
        for value in (checked, misses):
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                raise ValueError("audit counts must be non-negative ints")
        with self._lock:
            conn = self._guard()
            for key, value in (("audit_checked", checked),
                               ("audit_misses", misses)):
                row = conn.execute("SELECT value FROM kv WHERE key = ?",
                                   (key,)).fetchone()
                total = int(row[0]) + value if row else value
                conn.execute(
                    "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                    (key, str(total)))

    def note_inactive(self, reason: str | None,
                      python: tuple[int, int] | None) -> None:
        """Remember why recording is inactive (status lines only)."""
        if reason is not None and not isinstance(reason, str):
            raise ValueError("inactive reason must be str or None")
        if python is not None:
            try:
                major, minor = python
            except (TypeError, ValueError):
                raise ValueError("python must be a (major, minor) pair")
            if (isinstance(major, bool) or isinstance(minor, bool)
                    or not isinstance(major, int)
                    or not isinstance(minor, int)
                    or major < 0 or minor < 0):
                raise ValueError("python must be a (major, minor) pair")
        with self._lock:
            conn = self._guard()
            if reason is None:
                conn.execute("DELETE FROM kv WHERE key = 'inactive_reason'")
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO kv(key, value) VALUES"
                    "('inactive_reason', ?)", (reason[:200],))
            if python is None:
                conn.execute("DELETE FROM kv WHERE key IN ('python_major',"
                             " 'python_minor')")
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO kv(key, value) VALUES"
                    "('python_major', ?)", (str(python[0]),))
                conn.execute(
                    "INSERT OR REPLACE INTO kv(key, value) VALUES"
                    "('python_minor', ?)", (str(python[1]),))
