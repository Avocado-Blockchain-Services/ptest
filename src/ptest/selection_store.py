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
  ``.py`` and recorded data path) plus the run's ambient deps and its
  ambient fingerprint (keyed MAC, nullable).
* ``nodes``: up to ``SELECTION_RECORDS_PER_NODE`` rows per test, one per
  run (packed dep sets, outcome); primary key ``(nodeid, run_id)``.
* ``fixture_deps``: per-(fixture, run) dep sets.
* ``demotions``: audit-demoted node ids keyed by test-file digest.
* ``observers``: learned ``(nodeid, path)`` rows; ``kv`` also holds the
  gate marker (``last_gate_*``).

``update`` is a single ``BEGIN IMMEDIATE`` transaction that replaces
exactly the tests that ran in that run and trims every touched node to its
newest K rows; concurrent writers from two checkouts serialize on the
RESERVED lock (busy_timeout) and neither replaces the other's row.
Eviction order: unreferenced parse cache, surplus records (never a node's
newest row, never the written run), then the runs holding the fewest
records; hitting the hard cap raises ``capacity-exceeded`` after one
evict-and-retry. Schema v1 stores migrate in place on open.

The ``SELECTION_*`` constants, helpers and dataclasses come from
``contracts.py``, their single source of truth.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import stat
import threading
import time
import zlib
from array import array
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from . import files, storage

_PHASE = "selection-store"
_HEX32 = re.compile(r"[0-9a-f]{32}")

_SCHEMA_VERSION = "2"
_V1 = "1"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SURPLUS_BATCH = 2000
_CACHE_PRUNE_AFTER_S = 14 * 24 * 3600
_CACHE_TOUCH_AFTER_S = 3600
_INVENTORY_CHUNK = 5000
_CACHE_DROP_BATCH = 64


def _fail(code: str, message: str) -> None:
    raise C.Problem(code=code, message=message, phase=_PHASE, retryable=False)


from .contracts import (  # noqa: E402  re-exported for callers
    DepVocabulary,
    ContextDeps,
    RunBaseline,
    NodeRecord,
    FixtureRecord,
    DependencySnapshot,
    GateMarker,
    SelectionStoreMeta,
    RecordedNode,
    RunDependencies,
    selection_ids,
    selection_test_file,
    selection_empty_context,
    SELECTION_STORE_MAX_BYTES,
    SELECTION_STORE_TARGET_BYTES,
    SELECTION_RECORDS_PER_NODE,
    SELECTION_OBSERVER_ROWS_PER_NODE,
    SELECTION_OBSERVER_ROWS_MAX,
    SELECTION_OBSERVER_ALL,
    SELECTION_MAX_RUNS,
    SELECTION_OUTCOMES,
    SELECTION_FIXTURE_SCOPES,
    SOURCE_INDEX_VERSION,
)


__all__ = [
    "DepVocabulary", "ContextDeps", "RunBaseline", "NodeRecord",
    "FixtureRecord", "DependencySnapshot", "GateMarker", "SelectionStoreMeta",
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

_V1_TABLES = ("kv", "paths", "funcs", "fixtures", "cache", "runs",
              "run_digests", "nodes", "fixture_deps", "demotions")
_TABLES = (*_V1_TABLES, "observers")
_GATE_KEYS = ("last_gate_commit", "last_gate_dirty", "last_gate_recorded_at")

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
    compatibility TEXT NOT NULL, ambient BLOB NOT NULL, fingerprint TEXT);
CREATE TABLE run_digests(run_id TEXT NOT NULL REFERENCES runs(run_id)
    ON DELETE CASCADE, path_id INTEGER NOT NULL REFERENCES paths(id),
    digest TEXT NOT NULL, PRIMARY KEY(run_id, path_id));
CREATE TABLE nodes(nodeid TEXT NOT NULL, test_file TEXT NOT NULL,
    outcome TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id),
    deps BLOB NOT NULL, fixtures BLOB NOT NULL,
    PRIMARY KEY(nodeid, run_id));
CREATE INDEX IF NOT EXISTS nodes_by_nodeid ON nodes(nodeid);
CREATE TABLE fixture_deps(fixture_id INTEGER NOT NULL
    REFERENCES fixtures(id), run_id TEXT NOT NULL REFERENCES runs(run_id)
    ON DELETE CASCADE, deps BLOB NOT NULL,
    PRIMARY KEY(fixture_id, run_id));
CREATE TABLE demotions(nodeid TEXT PRIMARY KEY, digest TEXT NOT NULL);
CREATE TABLE observers(nodeid TEXT NOT NULL, path TEXT NOT NULL,
    learned_at REAL NOT NULL, PRIMARY KEY(nodeid, path));
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


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Upgrade a v1 store to v2 inside the caller's ``BEGIN IMMEDIATE``.

    Idempotent and lossless: ``nodes`` is rebuilt with the ``(nodeid,
    run_id)`` key and a ``nodeid`` index (every row copied unchanged),
    ``runs`` gains a nullable ``fingerprint``, ``observers`` is created and
    the version row is set last. The gate marker keys are written by the
    next gate, never here.
    """
    if "fingerprint" not in {row[1] for row in _columns(conn, "runs")}:
        conn.execute("ALTER TABLE runs ADD COLUMN fingerprint TEXT")
    pk = {row[1] for row in _columns(conn, "nodes") if row[5]}
    if pk != {"nodeid", "run_id"}:
        conn.execute("DROP TABLE IF EXISTS nodes_v2")
        conn.execute(
            "CREATE TABLE nodes_v2(nodeid TEXT NOT NULL, test_file TEXT NOT"
            " NULL, outcome TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES"
            " runs(run_id), deps BLOB NOT NULL, fixtures BLOB NOT NULL,"
            " PRIMARY KEY(nodeid, run_id))")
        conn.execute(
            "INSERT INTO nodes_v2(nodeid, test_file, outcome, run_id, deps,"
            " fixtures) SELECT nodeid, test_file, outcome, run_id, deps,"
            " fixtures FROM nodes")
        conn.execute("DROP TABLE nodes")
        conn.execute("ALTER TABLE nodes_v2 RENAME TO nodes")
    conn.execute("CREATE INDEX IF NOT EXISTS nodes_by_nodeid"
                 " ON nodes(nodeid)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS observers(nodeid TEXT NOT NULL, path"
        " TEXT NOT NULL, learned_at REAL NOT NULL,"
        " PRIMARY KEY(nodeid, path))")
    conn.execute("UPDATE kv SET value = ? WHERE key = 'schema_version'",
                 (_SCHEMA_VERSION,))


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
        elif (_SCHEMA_VERSION == "2" and set(_V1_TABLES) <= tables
                and _read_schema_version(conn) == _V1):
            try:
                _migrate_v1(conn)
            except sqlite3.Error as exc:
                if storage.is_transient_sqlite(exc):
                    _fail_retryable("coordinator-unavailable",
                                    "selection store is busy")
                    raise AssertionError("unreachable")
                if _is_io_error(exc):
                    _fail("state-unavailable",
                          "selection store is unavailable")
                    raise AssertionError("unreachable")
                _fail("coordinator-corrupt",
                      "selection store cannot be migrated")
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


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    """``BEGIN IMMEDIATE`` … ``COMMIT``, rolled back on any error."""
    _begin_immediate(conn)
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    conn.execute("COMMIT")


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


def _read_gate(kv: Mapping[str, str]) -> GateMarker | None:
    """The gate marker from kv rows; any missing or malformed key means no
    usable marker (the planner then reports "no gate recorded")."""
    import json
    commit = kv.get("last_gate_commit")
    raw = kv.get("last_gate_dirty")
    stamp = kv.get("last_gate_recorded_at")
    if commit is None or raw is None or stamp is None:
        return None
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        return None
    try:
        dirty = json.loads(raw)
        recorded_at = float(stamp)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(dirty, dict) or recorded_at != recorded_at:
        return None
    for path, digest in dirty.items():
        if not isinstance(path, str) or not path:
            return None
        if digest is not None and not isinstance(digest, str):
            return None
    return GateMarker(commit=commit, dirty=dict(dirty),
                      recorded_at=recorded_at)


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
    fingerprint: str | None = None
    observers: dict = field(default_factory=dict)
    gate: object = None
    clear_gate: bool = False


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
                # One transaction, and only rows not touched within the
                # hour: a warm plan must not rewrite (and lock) the whole
                # cache on every invocation. Pruning works in days.
                now = time.time()
                try:
                    with _transaction(conn):
                        for chunk in _chunks(list(found), 500):
                            conn.execute(
                                "UPDATE cache SET used_at = ? WHERE digest"
                                " IN (%s) AND used_at < ?"
                                % ",".join("?" * len(chunk)),
                                (now, *chunk, now - _CACHE_TOUCH_AFTER_S))
                except sqlite3.Error:
                    pass  # a stale used_at only ages the row sooner
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
            # One transaction: per-row autocommit synced the journal once
            # per file (about 35 ms each, a minute on a large project).
            with _transaction(conn):
                conn.executemany(
                    "INSERT OR REPLACE INTO cache(digest, version, blob,"
                    " used_at) VALUES(?, ?, ?, ?)",
                    [(digest, SOURCE_INDEX_VERSION, bytes(blob), now)
                     for digest, blob in items])

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
                "SELECT run_id, recorded_at, compatibility, ambient,"
                " fingerprint FROM runs").fetchall()
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
            observer_rows = conn.execute(
                "SELECT nodeid, path FROM observers").fetchall()
            kv = {row[0]: row[1] for row in conn.execute(
                "SELECT key, value FROM kv WHERE key IN (?, ?, ?, ?)",
                (*_GATE_KEYS, "last_full_inv"))}
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
            for run_id, recorded_at, compatibility, ambient, fingerprint \
                    in run_rows:
                runs[run_id] = RunBaseline(
                    run_id=run_id, recorded_at=recorded_at,
                    compatibility=compatibility, digests={},
                    ambient=remap(_unpack_ctx(ambient)),
                    fingerprint=(
                        fingerprint if isinstance(fingerprint, str)
                        and _FINGERPRINT.fullmatch(fingerprint) else None))
            for run_id, path_id, digest in digest_rows:
                if run_id in runs and path_id in path_of:
                    path = path_of[path_id]
                    if path in path_index:
                        runs[run_id].digests[path_index[path]] = digest

            # Every kept record per node, newest first by (recorded_at
            # desc, run_id asc); ``nodes`` holds the newest of each.
            gathered: dict[str, list[tuple[float, str, NodeRecord]]] = {}
            for nodeid, test_file, outcome, run_id, deps, fixtures in \
                    node_rows:
                record = NodeRecord(
                    nodeid=nodeid, test_file=test_file, outcome=outcome,
                    run_id=run_id, deps=remap(_unpack_ctx(deps)),
                    fixtures=selection_ids(
                        fixture_index[fixture_key_of[f]]
                        for f in _unpack_ids(fixtures)
                        if f in fixture_key_of))
                at = runs[run_id].recorded_at if run_id in runs else 0.0
                gathered.setdefault(nodeid, []).append((at, run_id, record))
            records: dict[str, tuple[NodeRecord, ...]] = {}
            nodes: dict[str, NodeRecord] = {}
            for nodeid, rows in gathered.items():
                rows.sort(key=lambda row: (-row[0], row[1]))
                records[nodeid] = tuple(row[2] for row in rows)
                nodes[nodeid] = records[nodeid][0]

            learned: dict[str, set[str]] = {}
            for nodeid, path in observer_rows:
                learned.setdefault(nodeid, set()).add(path)

            # N1: fixture_deps holds one row per (fixture, run), and a
            # narrowed re-record must not drop deps other retained runs'
            # nodes still rely on (a higher-scope fixture behaving
            # differently per consumer, e.g. request.param). Merge every
            # retained run's record into one union per fixture; opaque is
            # sticky. Rows merge oldest-first so run_id names the newest
            # contributing run deterministically.
            recorded_at_of = {row[0]: row[1] for row in run_rows}
            ordered = sorted(
                fixture_rows,
                key=lambda row: (recorded_at_of.get(row[1], float("-inf")),
                                 row[1]))
            fixtures_out: dict[int, FixtureRecord] = {}
            for fixture_id, run_id, deps in ordered:
                if fixture_id in fixture_key_of:
                    dense = fixture_index[fixture_key_of[fixture_id]]
                    record = FixtureRecord(
                        fixture=dense, run_id=run_id,
                        deps=remap(_unpack_ctx(deps)))
                    prev = fixtures_out.get(dense)
                    fixtures_out[dense] = record if prev is None else \
                        FixtureRecord(
                            fixture=dense, run_id=record.run_id,
                            deps=ContextDeps(
                                functions=selection_ids(
                                    (*prev.deps.functions,
                                     *record.deps.functions)),
                                modules=selection_ids(
                                    (*prev.deps.modules,
                                     *record.deps.modules)),
                                data=selection_ids(
                                    (*prev.deps.data, *record.deps.data)),
                                opaque=(prev.deps.opaque
                                        or record.deps.opaque)))

            vocabulary = DepVocabulary(
                paths=tuple(sorted(used_paths)),
                functions=tuple(key for key, _ in sorted(
                    func_index.items(), key=lambda item: item[1])),
                fixtures=tuple(key for key, _ in sorted(
                    fixture_index.items(), key=lambda item: item[1])))
            return DependencySnapshot(
                vocabulary=vocabulary, runs=runs, nodes=nodes,
                fixtures=fixtures_out, demotions=dict(demotions),
                records=records,
                observers={key: frozenset(value)
                           for key, value in learned.items()},
                gate=_read_gate(kv),
                inventory=_unpack_inv(kv.get("last_full_inv")))

    def meta(self) -> SelectionStoreMeta:
        """Store facts for status lines (human only)."""
        with self._lock:
            conn = self._guard()
            nodes = conn.execute(
                "SELECT COUNT(DISTINCT nodeid) FROM nodes").fetchone()[0]
            observing = conn.execute(
                "SELECT COUNT(DISTINCT nodeid) FROM observers").fetchone()[0]
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
                demoted=int(demoted), observers=int(observing))

    # -- update ------------------------------------------------------------

    def update(self, run, *, run_id: str, recorded_at: float,
               compatibility: str, digests: Mapping[str, str | None],
               full: bool, fingerprint: str | None = None,
               observers: Mapping[str, frozenset[str]] | None = None,
               gate: GateMarker | None = None,
               clear_gate: bool = False) -> int:
        """Record one run: one transaction replacing only the tests it ran.

        The run's rows are inserted under the ``(nodeid, run_id)`` key and
        every touched node is trimmed to its newest ``K`` rows (never the
        row just written). ``fingerprint`` lands on the run row,
        ``observers`` (node id -> learned paths) in the ``observers``
        table and ``gate`` (or, with ``clear_gate`` or more dirty files
        than ``MAX_GREEN_FILES``, a cleared marker) in kv, all in this one
        transaction: a rejected update writes none of them. Returns the
        observer rows the store cap evicted in it.

        Never fails for size: unreferenced cache goes first, then surplus
        records, then the runs holding the fewest records (the newest run
        is kept); raises ``capacity-exceeded`` only when even that cannot
        fit, after one evict-and-retry. A lock held past the wait budget
        raises retryable ``coordinator-unavailable`` and an I/O failure
        raises ``state-unavailable`` (never a raw sqlite error).
        """
        planned = self._plan_update(run, run_id=run_id,
                                    recorded_at=recorded_at,
                                    compatibility=compatibility,
                                    digests=digests, full=full,
                                    fingerprint=fingerprint,
                                    observers=observers, gate=gate,
                                    clear_gate=clear_gate)
        with self._lock:
            conn = self._guard()
            try:
                evicted, lost = self._apply_update(conn, planned)
                if evicted:
                    self._vacuum(conn)
                return lost
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
                    evicted, lost = self._apply_update(conn, planned)
                    if evicted:
                        self._vacuum(conn)
                    return lost
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
                     digests, full, fingerprint=None, observers=None,
                     gate=None, clear_gate=False) -> _PlannedUpdate:
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
        if not isinstance(clear_gate, bool):
            raise ValueError("clear_gate must be bool")
        if gate is not None and clear_gate:
            raise ValueError("gate and clear_gate are mutually exclusive")
        if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or not _FINGERPRINT.fullmatch(fingerprint)):
            raise ValueError("fingerprint must be 64 lowercase hex or None")
        learned = self._check_observers(observers)
        clear_gate = self._check_gate(gate, clear_gate)
        if clear_gate:
            gate = None
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
            nodes=nodes, fixtures=fixtures, ambient=ambient, bad=bad,
            fingerprint=fingerprint, observers=learned, gate=gate,
            clear_gate=clear_gate)

    @staticmethod
    def _check_observers(observers) -> dict[str, frozenset[str]]:
        if observers is None:
            return {}
        if not isinstance(observers, Mapping):
            raise ValueError("observers must be a mapping")
        learned: dict[str, frozenset[str]] = {}
        for nodeid, paths in observers.items():
            if not isinstance(nodeid, str) or not nodeid:
                raise ValueError("observer node ids must be nonempty str")
            if isinstance(paths, (str, bytes)):
                raise ValueError("observer paths must be a collection")
            try:
                values = frozenset(paths)
            except TypeError:
                raise ValueError("observer paths must be a collection")
            for path in values:
                if not isinstance(path, str) or not path:
                    raise ValueError("observer paths must be nonempty str")
            if values:
                learned[nodeid] = values
        return learned

    @staticmethod
    def _check_gate(gate, clear_gate: bool) -> bool:
        """Validate the marker; True when the keys are to be cleared (an
        explicit request, or a dirty set over ``MAX_GREEN_FILES``)."""
        if gate is None:
            return clear_gate
        try:
            commit, dirty, stamp = gate.commit, gate.dirty, gate.recorded_at
        except AttributeError:
            raise ValueError("gate must be a GateMarker")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise ValueError("gate commit must be 40 or 64 lowercase hex")
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or stamp != stamp):
            raise ValueError("gate recorded_at must be a number")
        if not isinstance(dirty, Mapping):
            raise ValueError("gate dirty must be a mapping")
        for path, digest in dirty.items():
            if not isinstance(path, str) or not path:
                raise ValueError("gate dirty paths must be nonempty str")
            if digest is not None and not isinstance(digest, str):
                raise ValueError("gate digests must be str or None")
        from .lastgreen import MAX_GREEN_FILES
        return len(dirty) > MAX_GREEN_FILES

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
                      planned: _PlannedUpdate) -> tuple[bool, int]:
        """Run the update transaction; (rows were evicted, observer rows
        the store cap evicted)."""
        _begin_immediate(conn)
        try:
            path_ids = self._intern_paths(conn, planned)
            func_ids = self._intern_funcs(conn, planned, path_ids)
            fixture_ids = self._intern_fixtures(conn, planned)
            self._store_run(conn, planned, path_ids, func_ids)
            self._store_nodes(conn, planned, path_ids, func_ids,
                              fixture_ids)
            self._store_fixtures(conn, planned, path_ids, func_ids,
                                 fixture_ids)
            self._trim_records(conn, planned)
            if planned.full:
                self._prune_full(conn, planned)
            self._store_observers(conn, planned)
            lost = self._cap_observers(conn)
            self._store_gate(conn, planned)
            conn.execute(
                "DELETE FROM runs WHERE run_id NOT IN"
                " (SELECT DISTINCT run_id FROM nodes)")
            self._prune_cache(conn)
            evicted = self._evict_to_target(conn, exclude=planned.run_id)
            conn.execute("COMMIT")
            return evicted, lost
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
                   planned: _PlannedUpdate, path_ids: dict[str, int],
                   func_ids: dict) -> None:
        ambient = self._opaque(planned.ambient, planned, path_ids, func_ids)
        conn.execute(
            "INSERT OR REPLACE INTO runs(run_id, recorded_at,"
            " compatibility, ambient, fingerprint) VALUES(?, ?, ?, ?, ?)",
            (planned.run_id, planned.recorded_at, planned.compatibility,
             _pack_ctx(ambient), planned.fingerprint))
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
            conn.execute("DELETE FROM nodes WHERE nodeid = ? AND run_id = ?",
                         (nodeid, planned.run_id))
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
            conn.execute(
                "DELETE FROM observers WHERE nodeid NOT IN"
                " (SELECT nodeid FROM keep_nodes)")
        finally:
            conn.execute("DROP TABLE keep_nodes")

    @staticmethod
    def _trim_records(conn: sqlite3.Connection,
                      planned: _PlannedUpdate) -> None:
        """Keep at most ``K`` rows for every node the run wrote: the oldest
        by ``runs.recorded_at`` go, the row just written never does."""
        conn.execute(
            "DELETE FROM nodes WHERE rowid IN (SELECT rid FROM ("
            " SELECT nodes.rowid AS rid, ROW_NUMBER() OVER ("
            "  PARTITION BY nodes.nodeid ORDER BY nodes.run_id = ?1 DESC,"
            "  runs.recorded_at DESC, nodes.run_id ASC) AS rn"
            " FROM nodes JOIN runs ON runs.run_id = nodes.run_id"
            " WHERE nodes.nodeid IN"
            "  (SELECT nodeid FROM nodes WHERE run_id = ?1))"
            " WHERE rn > ?2)",
            (planned.run_id, SELECTION_RECORDS_PER_NODE))

    @staticmethod
    def _store_observers(conn: sqlite3.Connection,
                         planned: _PlannedUpdate) -> None:
        """Merge learned rows; a node past its per-node cap becomes the
        single ``*`` row (it widens, so it is not eviction)."""
        star = SELECTION_OBSERVER_ALL
        for nodeid, paths in planned.observers.items():
            have = {row[0] for row in conn.execute(
                "SELECT path FROM observers WHERE nodeid = ?", (nodeid,))}
            merged = have | set(paths)
            if (star in merged
                    or len(merged) > SELECTION_OBSERVER_ROWS_PER_NODE):
                conn.execute("DELETE FROM observers WHERE nodeid = ?",
                             (nodeid,))
                have = set()
                merged = {star}
            for path in sorted(merged):
                if path in have and path not in paths:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO observers(nodeid, path,"
                    " learned_at) VALUES(?, ?, ?)",
                    (nodeid, path, planned.recorded_at))

    @staticmethod
    def _cap_observers(conn: sqlite3.Connection) -> int:
        """Enforce the store-wide row cap, oldest ``learned_at`` first;
        returns how many rows went (reported, never silent)."""
        total = conn.execute("SELECT COUNT(*) FROM observers").fetchone()[0]
        excess = int(total) - SELECTION_OBSERVER_ROWS_MAX
        if excess <= 0:
            return 0
        conn.execute(
            "DELETE FROM observers WHERE rowid IN (SELECT rowid FROM"
            " observers ORDER BY learned_at ASC, nodeid ASC, path ASC"
            " LIMIT ?)", (excess,))
        return excess

    @staticmethod
    def _store_gate(conn: sqlite3.Connection,
                    planned: _PlannedUpdate) -> None:
        import json
        gate = planned.gate
        if gate is not None:
            rows = (
                ("last_gate_commit", gate.commit),
                ("last_gate_dirty", json.dumps(
                    dict(sorted(dict(gate.dirty).items())),
                    ensure_ascii=True, separators=(",", ":"))),
                ("last_gate_recorded_at", repr(float(gate.recorded_at))))
            conn.executemany(
                "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", rows)
        elif planned.clear_gate:
            conn.execute(
                "DELETE FROM kv WHERE key IN (%s)"
                % ",".join("?" * len(_GATE_KEYS)), _GATE_KEYS)

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

    def _drop_unreferenced_cache(self, conn: sqlite3.Connection, *,
                                 to_target: bool) -> bool:
        """Drop cached file versions no record references, least recently
        used first (spec 2.5); True when any row went.

        They only spare a re-parse, while a dropped record costs a run of
        every test it covered, so size pressure takes them before any run.
        """
        dropped = False
        while (not to_target
               or self._db_size(conn) > SELECTION_STORE_TARGET_BYTES):
            rows = conn.execute(
                "SELECT digest FROM cache WHERE digest NOT IN"
                " (SELECT digest FROM run_digests)"
                " ORDER BY used_at ASC, digest ASC LIMIT ?",
                (_CACHE_DROP_BATCH,)).fetchall()
            if not rows:
                break
            conn.executemany("DELETE FROM cache WHERE digest = ?", rows)
            dropped = True
        return dropped

    @staticmethod
    def _eviction_order(conn: sqlite3.Connection) -> list[tuple[str, int]]:
        """Runs with their surviving record counts, fewest first, then
        oldest: a full run outlives the small runs recorded after it."""
        return [(row[0], int(row[1])) for row in conn.execute(
            "SELECT runs.run_id, COUNT(nodes.nodeid) AS kept FROM runs"
            " LEFT JOIN nodes ON nodes.run_id = runs.run_id"
            " GROUP BY runs.run_id"
            " ORDER BY kept ASC, runs.recorded_at ASC, runs.rowid ASC")]

    @staticmethod
    def _surplus_rows(conn: sqlite3.Connection,
                      exclude: str) -> list[tuple[int, str]]:
        """``(rowid, run_id)`` of every surplus record, oldest run first:
        rows of nodes holding at least two, never a node's newest row and
        never a row of the run being written."""
        return [(int(row[0]), row[1]) for row in conn.execute(
            "SELECT rid, run_id FROM ("
            " SELECT nodes.rowid AS rid, nodes.run_id AS run_id,"
            "  runs.recorded_at AS at, ROW_NUMBER() OVER ("
            "   PARTITION BY nodes.nodeid ORDER BY runs.recorded_at DESC,"
            "   nodes.run_id ASC) AS rn"
            "  FROM nodes JOIN runs ON runs.run_id = nodes.run_id)"
            " WHERE rn > 1 AND run_id != ?"
            " ORDER BY at ASC, run_id DESC, rid ASC", (exclude,))]

    @staticmethod
    def _drop_rows(conn: sqlite3.Connection, rowids: list[int]) -> None:
        for chunk in _chunks(rowids, 500):
            conn.execute("DELETE FROM nodes WHERE rowid IN (%s)"
                         % ",".join("?" * len(chunk)), tuple(chunk))

    def _drop_surplus(self, conn: sqlite3.Connection, *,
                      exclude: str) -> bool:
        """Evict surplus records oldest first until the size target is
        met; True when any row went."""
        if self._db_size(conn) <= SELECTION_STORE_TARGET_BYTES:
            return False
        rows = self._surplus_rows(conn, exclude)
        dropped = False
        for chunk in _chunks(rows, _SURPLUS_BATCH):
            if self._db_size(conn) <= SELECTION_STORE_TARGET_BYTES:
                break
            self._drop_rows(conn, [rid for rid, _ in chunk])
            dropped = True
        if dropped:
            conn.execute(
                "DELETE FROM runs WHERE run_id NOT IN"
                " (SELECT DISTINCT run_id FROM nodes)")
            try:
                conn.execute("PRAGMA incremental_vacuum").fetchall()
            except sqlite3.Error:
                pass
        return dropped

    @staticmethod
    def _delete_run(conn: sqlite3.Connection, run_id: str) -> None:
        conn.execute("DELETE FROM nodes WHERE run_id = ?", (run_id,))
        conn.execute(
            "DELETE FROM runs WHERE run_id NOT IN"
            " (SELECT DISTINCT run_id FROM nodes)")
        try:
            conn.execute("PRAGMA incremental_vacuum").fetchall()
        except sqlite3.Error:
            pass

    def _evict_to_target(self, conn: sqlite3.Connection,
                         *, exclude: str) -> bool:
        """Meet the size target; True when any rows were evicted.

        Unreferenced cache goes first, then the runs holding the fewest
        records. A run holding more records than the newest one is never
        traded for it: the store then stays over the target, and the hard
        cap still bounds it (SQLITE_FULL takes the update's retry path).
        """
        evicted = self._drop_unreferenced_cache(conn, to_target=True)
        evicted = self._drop_surplus(conn, exclude=exclude) or evicted
        incoming = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE run_id = ?",
            (exclude,)).fetchone()[0]
        while True:
            order = self._eviction_order(conn)
            too_many = len(order) > SELECTION_MAX_RUNS
            oversize = self._db_size(conn) > SELECTION_STORE_TARGET_BYTES
            if (not oversize and not too_many) or len(order) <= 1:
                return evicted
            victim = next(((rid, kept) for rid, kept in order
                           if rid != exclude), None)
            if victim is None:
                return evicted
            if not too_many and victim[1] > incoming:
                return evicted
            self._delete_run(conn, victim[0])
            evicted = True

    def _evict_oldest(self, conn: sqlite3.Connection, *,
                      exclude: str) -> None:
        """Make room after SQLITE_FULL, in the eviction order: every
        unreferenced cache row, else the surplus records of the oldest run
        holding any, else the run holding the fewest records."""
        try:
            _begin_immediate(conn)
        except sqlite3.Error:
            return
        try:
            if not self._drop_unreferenced_cache(conn, to_target=False):
                surplus = self._surplus_rows(conn, exclude)
                if surplus:
                    oldest = surplus[0][1]
                    self._drop_rows(conn, [rid for rid, run in surplus
                                           if run == oldest])
                    conn.execute(
                        "DELETE FROM runs WHERE run_id NOT IN"
                        " (SELECT DISTINCT run_id FROM nodes)")
                else:
                    victim = next((rid for rid, _ in
                                   self._eviction_order(conn)
                                   if rid != exclude), None)
                    if victim is not None:
                        self._delete_run(conn, victim)
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
