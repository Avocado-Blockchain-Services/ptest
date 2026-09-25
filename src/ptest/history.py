"""Durable, checkout-scoped outcome history.

The history store deliberately has a smaller boundary than the scheduler.  A
caller supplies both the domain and checkout, and this module never resolves
either one from process state.  Public run summaries are produced by the
contracts serializer; the extra columns are private evidence used only for
ordering and baseline eligibility.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

from . import contracts as C
from . import reports
from .reports import NativeReportBinding
from .files import (
    create_exclusive,
    ensure_private_dir,
    publish_atomic,
    read_regular,
    validate_private_dir,
    validate_private_file,
)
from .storage import open_database

HISTORY_MAX_BYTES = C.HISTORY_MAX_BYTES
HISTORY_MAX_SUMMARIES = C.HISTORY_MAX_SUMMARIES
HISTORY_RETAIN_DAYS = C.HISTORY_RETAIN_DAYS

_PHASE = "history"
_STORE_NAME = "history.sqlite3"
_DISABLED_MARKER_NAME = "history-disabled.json"
_CAPACITY_MARKER_NAME = "history-capacity.json"
_CAPACITY_RESERVE_NAME = "history-capacity-reserve.json"
_PUBLICATION_MARKER_NAME = "history-publication.json"
_ACTIVE_PUBLICATION_NAME = "history-publication-active.json"
_WRITER_LOCK_NAME = "history-writer.lock"
_QUALIFIED_PROFILE_NAME = "qualified-native-profile.json"
_QUALIFIED_PROFILE_MAX_BYTES = 8192
_CHECKOUTS_NAME = "checkouts"
_SCHEMA_VERSION = 1
_BASE_REQUIRED_TABLES = frozenset({
    "metadata", "runs", "baselines", "obligations", "reconciliations",
})
_COMPOUND_TABLES = frozenset({
    "selection_quarantine", "attempt_evidence", "comparison_receipts",
})
_REQUIRED_TABLES = _BASE_REQUIRED_TABLES | _COMPOUND_TABLES
_FAILURE_OUTCOMES = frozenset({C.Outcome.FAILED, C.Outcome.ERROR})
_SUCCESS_OUTCOME = C.Outcome.PASSED
_FAILED_ATTEMPT_STATUSES = frozenset({
    C.Status.FAILED, C.Status.INCOMPLETE, C.Status.CANCELLED, C.Status.NOT_RUN,
})
_WHOLE_GATE_KEY = "whole-gate"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_BUSY_TIMEOUT_S = 2.0
_SQLITE_BUSY_TIMEOUT_MS = 2000
_COMPACT_MIN_FREE_BYTES = 2 * 1024 * 1024
_RECONCILIATION_MAX_ROWS = 200
_TRANSIENT_SQLITE_CODES = frozenset({
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
    getattr(sqlite3, "SQLITE_CANTOPEN", 14),
    getattr(sqlite3, "SQLITE_READONLY", 8),
    getattr(sqlite3, "SQLITE_IOERR", 10),
})
_SQLITE_FULL_CODE = getattr(sqlite3, "SQLITE_FULL", 13)
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        finished_at TEXT NOT NULL,
        summary TEXT NOT NULL,
        input_before TEXT,
        input_after TEXT,
        policy_digest TEXT,
        source_digest TEXT,
        compatibility TEXT,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        inventory TEXT
    )
    """,
    """
    CREATE TABLE baselines (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        head TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        compatibility TEXT NOT NULL,
        inventory TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        runtime_identity TEXT
    )
    """,
    """
    CREATE TABLE obligations (
        obligation_key TEXT PRIMARY KEY,
        file TEXT,
        test_id TEXT,
        sequence INTEGER NOT NULL,
        source_digest TEXT,
        compatibility TEXT,
        reason TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE reconciliations (
        reconciliation_key TEXT PRIMARY KEY,
        file TEXT,
        test_id TEXT,
        sequence INTEGER NOT NULL,
        compatibility TEXT,
        outcome TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE selection_quarantine (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        code TEXT NOT NULL CHECK (code = 'selection-shadow-quarantine'),
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        policy_digest TEXT NOT NULL,
        compatibility TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        verdict TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE attempt_evidence (
        run_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        result TEXT NOT NULL,
        inventory TEXT,
        terminal_complete INTEGER NOT NULL,
        parallel_identity INTEGER NOT NULL,
        runtime_identity TEXT,
        PRIMARY KEY (run_id, attempt_id)
    )
    """,
    """
    CREATE TABLE comparison_receipts (
        run_id TEXT PRIMARY KEY,
        verdict TEXT NOT NULL,
        expected_quarantine TEXT
    )
    """,
)
_COMPOUND_SCHEMA_STATEMENTS = _SCHEMA_STATEMENTS[-3:]


class _HistoryStateError(Exception):
    """A typed store error whose message never contains stored user data."""

    def __init__(self, code: str, *, retain_uncertainty: bool = False) -> None:
        self.code = code
        self.retain_uncertainty = retain_uncertainty
        super().__init__(code)


def _sqlite_error_code(exc: sqlite3.Error) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return code if isinstance(code, int) else None


def _is_sqlite_full(exc: sqlite3.Error) -> bool:
    return (
        _sqlite_error_code(exc) == _SQLITE_FULL_CODE
        or "database or disk is full" in str(exc).lower()
    )


def _is_transient_sqlite(exc: sqlite3.Error) -> bool:
    code = _sqlite_error_code(exc)
    if code is not None and code & 0xff in _TRANSIENT_SQLITE_CODES:
        return True
    text = str(exc).lower()
    return any(
        phrase in text
        for phrase in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "unable to open database file",
            "cannot open database file",
            "i/o error",
        )
    )


def _state_error_for_sqlite(exc: sqlite3.Error) -> _HistoryStateError:
    if _is_sqlite_full(exc):
        return _HistoryStateError("capacity-exceeded")
    if _is_transient_sqlite(exc):
        return _HistoryStateError("coordinator-unavailable")
    return _HistoryStateError("coordinator-corrupt")


def _can_persist_disabled_marker(code: str) -> bool:
    return code == "coordinator-corrupt"


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE, retryable=False)


def _reason(code: str, message: str) -> C.Reason:
    return C.Reason(code=code, message=message)


def _validate_arguments(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> None:
    if not isinstance(domain, C.DomainPaths):
        raise TypeError("history domain must be DomainPaths")
    if not isinstance(checkout, C.CheckoutIdentity):
        raise TypeError("history checkout must be CheckoutIdentity")
    validate_private_dir(domain.root)
    root = _validated_checkout_root(domain, checkout)
    expected = hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]
    if checkout.checkout_id != expected:
        raise _problem("unsafe-path", "checkout identity does not match its root")


def _validated_checkout_root(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> Path:
    root = Path(checkout.root)
    if not root.is_absolute():
        root = Path(os.getcwd()) / root
    cursor = Path(root.anchor)
    for component in root.parts[1:]:
        cursor = cursor / component
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            raise _problem("unsafe-path", "checkout root is unavailable") from None
        if not os.path.isdir(cursor) or os.path.islink(cursor):
            raise _problem("unsafe-path", "checkout root is not a regular directory")
    try:
        stamp = os.lstat(root)
    except FileNotFoundError:
        raise _problem("unsafe-path", "checkout root is unavailable") from None
    if not os.path.isdir(root) or os.path.islink(root):
        raise _problem("unsafe-path", "checkout root is not a regular directory")
    if stamp.st_uid != os.getuid():
        raise _problem("unsafe-path", "checkout root has a foreign owner")
    root = Path(os.path.abspath(root))
    if domain.fixture:
        domain_root = Path(os.path.abspath(domain.root))
        try:
            root.relative_to(domain_root)
        except ValueError:
            raise _problem("unsafe-path", "fixture checkout is outside its domain") from None
    return root


def _existing_private_dir(path: Path) -> Path | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    validate_private_dir(path)
    return path


def _history_directory(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, *, create: bool,
) -> Path | None:
    _validate_arguments(domain, checkout)
    if create:
        checkouts = ensure_private_dir(domain.root, _CHECKOUTS_NAME)
        return ensure_private_dir(checkouts, checkout.checkout_id)
    checkouts = _existing_private_dir(domain.root / _CHECKOUTS_NAME)
    if checkouts is None:
        return None
    return _existing_private_dir(checkouts / checkout.checkout_id)


def _disabled_marker(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> str | None:
    directory = _history_directory(domain, checkout, create=False)
    if directory is None:
        return None
    try:
        raw = read_regular(directory, _DISABLED_MARKER_NAME, 512)
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return None
        raise _HistoryStateError(exc.code) from None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        raise _HistoryStateError("coordinator-corrupt") from None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise _HistoryStateError("coordinator-corrupt")
    code = value.get("code")
    if not isinstance(code, str) or code not in C.REASON_CODES:
        raise _HistoryStateError("coordinator-corrupt")
    return code


def _write_disabled_marker(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, code: str,
) -> None:
    try:
        directory = _history_directory(domain, checkout, create=False)
        if directory is None:
            return
        payload = _json_bytes({"version": 1, "code": code}).encode("utf-8")
        try:
            create_exclusive(directory, _DISABLED_MARKER_NAME, payload)
        except C.Problem as exc:
            if exc.code != "already-exists":
                return
    except (C.Problem, OSError):
        return


@contextmanager
def _writer_lock(domain: C.DomainPaths, checkout: C.CheckoutIdentity):
    """Serialize bounded recovery and capacity-marker changes across publishers.

    SQLite still owns schema/transaction locking. This private advisory lock
    spans the committed pruning/VACUUM/retry steps without replacing DB inodes.
    Reads never create or acquire it; kernel ownership ends on process death.
    """
    directory = _history_directory(domain, checkout, create=True)
    assert directory is not None
    try:
        create_exclusive(directory, _WRITER_LOCK_NAME, b"")
    except C.Problem as exc:
        if exc.code != "already-exists":
            raise
    path = directory / _WRITER_LOCK_NAME
    validate_private_file(path)
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _HistoryStateError("unsafe-path")
        deadline = time.monotonic() + _SQLITE_BUSY_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _HistoryStateError("coordinator-unavailable") from None
                time.sleep(0.01)
        yield directory
    finally:
        os.close(fd)


def _capacity_sequence_for(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, name: str,
) -> int | None:
    directory = _history_directory(domain, checkout, create=False)
    if directory is None:
        return None
    try:
        validate_private_file(directory / name)
        value = json.loads(read_regular(directory, name, 512))
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return None
        raise
    except (ValueError, UnicodeError):
        raise _HistoryStateError("coordinator-corrupt") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "code", "sequence"}
        or value["version"] != 1 or value["code"] != "capacity-exceeded"
        or type(value["sequence"]) is not int or value["sequence"] < 0
    ):
        raise _HistoryStateError("coordinator-corrupt")
    return value["sequence"]


def _capacity_sequence(domain: C.DomainPaths, checkout: C.CheckoutIdentity) -> int | None:
    return _capacity_sequence_for(domain, checkout, _CAPACITY_MARKER_NAME)


def _capacity_reserve_sequence(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> int | None:
    return _capacity_sequence_for(domain, checkout, _CAPACITY_RESERVE_NAME)


def _store_capacity_reserve(directory: Path, sequence: int) -> None:
    publish_atomic(directory, _CAPACITY_RESERVE_NAME, _json_bytes({
        "version": 1, "code": "capacity-exceeded", "sequence": sequence,
    }).encode("utf-8"))


def _remove_marker(directory: Path, name: str) -> None:
    try:
        validate_private_file(directory / name)
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return
        raise
    (directory / name).unlink()


def _clear_capacity(directory: Path) -> None:
    _remove_marker(directory, _CAPACITY_MARKER_NAME)


def _publication_sequence(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> int | None:
    directory = _history_directory(domain, checkout, create=False)
    if directory is None:
        return None
    try:
        validate_private_file(directory / _PUBLICATION_MARKER_NAME)
        value = json.loads(read_regular(directory, _PUBLICATION_MARKER_NAME, 512))
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return None
        raise
    except (ValueError, UnicodeError):
        raise _HistoryStateError("coordinator-corrupt") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "code", "sequence"}
        or value["version"] != 1 or value["code"] != "selection-disabled"
        or type(value["sequence"]) is not int or value["sequence"] < 0
    ):
        raise _HistoryStateError("coordinator-corrupt")
    return value["sequence"]


def _publication_is_active(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, sequence: int,
) -> bool:
    directory = _history_directory(domain, checkout, create=False)
    if directory is None:
        return False
    try:
        validate_private_file(directory / _ACTIVE_PUBLICATION_NAME)
        value = json.loads(read_regular(directory, _ACTIVE_PUBLICATION_NAME, 512))
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return False
        raise
    except (ValueError, UnicodeError):
        raise _HistoryStateError("coordinator-corrupt") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "pid", "birth", "sequence"}
        or value["version"] != 1
        or type(value["pid"]) is not int or value["pid"] <= 0
        or type(value["birth"]) not in {int, float} or value["birth"] < 0
        or type(value["sequence"]) is not int or value["sequence"] < 0
    ):
        raise _HistoryStateError("coordinator-corrupt")
    if value["sequence"] != sequence:
        # A later publisher may commit while an older generic uncertainty
        # marker remains. Its live marker does not make that older uncertainty
        # safe to ignore.
        return False
    try:
        birth = psutil.Process(value["pid"]).create_time()
    except (psutil.Error, OSError):
        return False
    return abs(birth - float(value["birth"])) < 0.001


def _reserve_publication(
    directory: Path, sequence: int, capacity: int | None,
    publication: int | None,
) -> None:
    inherited_uncertainty = publication is not None
    if not inherited_uncertainty:
        active = {
            "version": 1,
            "pid": os.getpid(),
            "birth": psutil.Process(os.getpid()).create_time(),
            "sequence": sequence,
        }
        publish_atomic(
            directory, _ACTIVE_PUBLICATION_NAME, _json_bytes(active).encode("utf-8"),
        )
    try:
        # Advance generic uncertainty before touching the database. A writer
        # inheriting uncertainty deliberately has no active-health exemption:
        # otherwise raising the marker to its own sequence would mask the
        # earlier possibly-lost publication while this writer is live.
        publish_atomic(directory, _PUBLICATION_MARKER_NAME, _json_bytes({
            "version": 1, "code": "selection-disabled",
            "sequence": max(sequence, publication or 0),
        }).encode("utf-8"))
        _store_capacity_reserve(
            directory, max(sequence, capacity or 0, publication or 0),
        )
    except BaseException:
        names = [_CAPACITY_RESERVE_NAME]
        if publication is None:
            names.append(_PUBLICATION_MARKER_NAME)
        names.append(_ACTIVE_PUBLICATION_NAME)
        for name in names:
            try:
                _remove_marker(directory, name)
            except (C.Problem, OSError):
                pass
        raise


def _discard_publication_reservation(
    directory: Path, *, retain_publication: bool = False,
) -> None:
    first_error = None
    names = [_CAPACITY_RESERVE_NAME]
    if not retain_publication:
        names.append(_PUBLICATION_MARKER_NAME)
    names.append(_ACTIVE_PUBLICATION_NAME)
    for name in names:
        try:
            _remove_marker(directory, name)
        except (C.Problem, OSError) as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _retain_publication_uncertainty(directory: Path) -> None:
    # Both operations are deallocations. If either fails, the generic
    # publication marker still takes precedence over a false capacity claim.
    _remove_marker(directory, _CAPACITY_RESERVE_NAME)
    _remove_marker(directory, _ACTIVE_PUBLICATION_NAME)


def _claim_reserved_capacity(directory: Path) -> None:
    # Promote the already-created reserve before removing either publication
    # marker. Readers infer capacity only from this durable record, never from
    # preallocation by a healthy or abandoned publisher.
    os.replace(directory / _CAPACITY_RESERVE_NAME, directory / _CAPACITY_MARKER_NAME)
    _remove_marker(directory, _ACTIVE_PUBLICATION_NAME)
    _remove_marker(directory, _PUBLICATION_MARKER_NAME)


def _selection_marker(domain: C.DomainPaths, checkout: C.CheckoutIdentity) -> str | None:
    permanent = _disabled_marker(domain, checkout)
    if permanent is not None:
        return permanent
    # Re-read a disappearing publication marker once so a healthy writer's
    # cleanup cannot create a transient false disable. Reads never create,
    # mutate or lock history state.
    for _attempt in range(2):
        capacity = _capacity_sequence(domain, checkout)
        publication = _publication_sequence(domain, checkout)
        if publication is None:
            # A capacity claim is promoted before publication cleanup. Re-read
            # it so a reader spanning that rename cannot miss the durable state.
            if capacity is None:
                capacity = _capacity_sequence(domain, checkout)
            return "capacity-exceeded" if capacity is not None else None
        if capacity is not None:
            return "capacity-exceeded"
        if _publication_is_active(domain, checkout, publication):
            return None
        if _capacity_sequence(domain, checkout) is not None:
            return "capacity-exceeded"
        if _publication_sequence(domain, checkout) is None:
            continue
        return "selection-disabled"
    return "selection-disabled"


def _open_store(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, *, create: bool,
) -> sqlite3.Connection | None:
    directory = _history_directory(domain, checkout, create=create)
    if directory is None:
        return None
    path = directory / _STORE_NAME
    if not create:
        connection = None
        try:
            validate_private_file(path)
        except C.Problem as exc:
            if exc.code == "state-unavailable":
                return None
            raise
        try:
            if path.stat().st_size > HISTORY_MAX_BYTES:
                raise _HistoryStateError("capacity-exceeded")
            if path.stat().st_size == 0:
                return None
            before = os.lstat(path)
            absolute = path if path.is_absolute() else Path(os.path.abspath(path))
            target = f"file:{urllib.parse.quote(str(absolute), safe='/')}?mode=ro"
            connection = sqlite3.connect(
                target, uri=True, timeout=_SQLITE_BUSY_TIMEOUT_S,
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            # Every validation and payload SELECT must see the same committed
            # state; otherwise a concurrent baseline replacement can look like
            # a missing/corrupt run halfway through a read.
            connection.execute("BEGIN")
            if not _schema_tables(connection):
                connection.close()
                return None
            _validate_schema(
                connection, project_id=checkout.project_id,
                checkout_id=checkout.checkout_id,
                required_tables=_BASE_REQUIRED_TABLES,
            )
            _compound_schema_present(connection)
            after = os.lstat(path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _HistoryStateError("unsafe-path")
            return connection
        except Exception as exc:
            try:
                if connection is not None:
                    connection.close()
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise _state_error_for_sqlite(exc) from None
            raise
    for attempt in range(3):
        try:
            os.lstat(path)
            existed = True
        except FileNotFoundError:
            existed = False
        try:
            connection = open_database(
                directory, _STORE_NAME, max_bytes=HISTORY_MAX_BYTES,
            )
            try:
                _ensure_schema(
                    connection, new=not existed, project_id=checkout.project_id,
                    checkout_id=checkout.checkout_id,
                )
            except Exception:
                connection.close()
                raise
            return connection
        except C.Problem as exc:
            # A concurrent first writer can expose the SQLite header before it
            # has committed the schema.  Re-open a bounded number of times;
            # persistent corruption still fails closed below.
            if attempt < 2 and exc.code in {"already-exists", "coordinator-corrupt"}:
                continue
            if exc.code == "coordinator-corrupt":
                # T0's opener maps pragma lock errors to this same code. Only
                # our own schema/content validation is positive corruption.
                raise _HistoryStateError("coordinator-unavailable") from None
            raise
        except sqlite3.Error:
            if attempt < 2:
                continue
            raise
    raise AssertionError("unreachable")


def _ensure_schema(
    connection: sqlite3.Connection, *, new: bool, project_id: str, checkout_id: str,
) -> None:
    del new  # An empty, previously interrupted file is safe to initialize.
    connection.execute("BEGIN IMMEDIATE")
    try:
        tables = _schema_tables(connection)
        if not tables:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute("CREATE INDEX runs_by_sequence ON runs(sequence DESC)")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?), (?, ?), (?, ?), (?, ?)",
                (
                    "schema_version", "1", "selection_disabled", "0",
                    "project_id", project_id, "checkout_id", checkout_id,
                ),
            )
        else:
            _validate_schema(
                connection, project_id=project_id, checkout_id=checkout_id,
                required_tables=_BASE_REQUIRED_TABLES,
            )
            if not _compound_schema_present(connection):
                for statement in _COMPOUND_SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "ALTER TABLE baselines ADD COLUMN runtime_identity TEXT")
            _validate_schema(
                connection, project_id=project_id,
                checkout_id=checkout_id,
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    )


def _compound_schema_present(connection: sqlite3.Connection) -> bool:
    tables = _schema_tables(connection)
    present = tables & _COMPOUND_TABLES
    baseline_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(baselines)")
    }
    has_runtime_identity = "runtime_identity" in baseline_columns
    if not present and not has_runtime_identity:
        return False
    if present == _COMPOUND_TABLES and has_runtime_identity:
        return True
    raise _HistoryStateError("coordinator-corrupt")


def _validate_schema(
    connection: sqlite3.Connection, *, project_id: str, checkout_id: str,
    required_tables: frozenset[str] = _REQUIRED_TABLES,
) -> None:
    tables = _schema_tables(connection)
    if not required_tables.issubset(tables):
        raise _HistoryStateError("coordinator-corrupt")
    indexes = frozenset(
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    )
    if "runs_by_sequence" not in indexes:
        raise _HistoryStateError("coordinator-corrupt")
    version = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    disabled = connection.execute(
        "SELECT value FROM metadata WHERE key = 'selection_disabled'"
    ).fetchone()
    stored_project = connection.execute(
        "SELECT value FROM metadata WHERE key = 'project_id'"
    ).fetchone()
    stored_checkout = connection.execute(
        "SELECT value FROM metadata WHERE key = 'checkout_id'"
    ).fetchone()
    if version is None or version[0] != str(_SCHEMA_VERSION):
        raise _HistoryStateError("coordinator-corrupt")
    if disabled is None or disabled[0] not in {"0", "1"}:
        raise _HistoryStateError("coordinator-corrupt")
    if (
        stored_project is None or stored_project[0] != project_id
        or stored_checkout is None or stored_checkout[0] != checkout_id
    ):
        raise _HistoryStateError("coordinator-corrupt")


def _check_quota(connection: sqlite3.Connection) -> None:
    """Defend against an externally changed cap or an incorrectly bounded opener.

    Normal writes use max_page_count; this check is not capacity prediction.
    Read-only opening separately bounds the actual file size.
    """
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    if page_size <= 0 or page_count < 0 or page_size * page_count > HISTORY_MAX_BYTES:
        raise _HistoryStateError("capacity-exceeded")


def _json_bytes(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _change_dict(change: C.Change) -> dict:
    return {"old": change.old, "new": change.new, "kind": change.kind}


def _reason_dict(reason: C.Reason) -> dict:
    return {"code": reason.code, "message": reason.message, "paths": list(reason.paths)}


def _snapshot_dict(snapshot: C.InputSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "digest": snapshot.digest,
        "compatibility": snapshot.compatibility,
        "head": snapshot.head,
        "baseline_head": snapshot.baseline_head,
        "clean": snapshot.clean,
        "changes": [_change_dict(item) for item in snapshot.changes],
        "limitations": [_reason_dict(item) for item in snapshot.limitations],
        "files": [
            {
                "path": item.path,
                "digest": item.digest,
                "mode": item.mode,
                "size": item.size,
            }
            for item in snapshot.files
        ],
    }


def _inventory_dict(inventory: C.Inventory) -> dict:
    return {
        "adapter": inventory.adapter,
        "version": inventory.version,
        "complete": inventory.complete,
        "digest": inventory.digest,
        "tests": [
            {
                "id": item.id,
                "file": item.file,
                "outcome": item.outcome.value,
                "setup_s": item.setup_s,
                "call_s": item.call_s,
                "teardown_s": item.teardown_s,
            }
            for item in inventory.tests
        ],
    }


def _quarantine_dict(quarantine: C.SelectionQuarantine) -> dict:
    return {
        "code": quarantine.code,
        "run_id": quarantine.run_id,
        "sequence": quarantine.sequence,
        "policy_digest": quarantine.policy_digest,
        "compatibility": quarantine.compatibility,
        "input_digest": quarantine.input_digest,
        "verdict": quarantine.verdict,
    }


def _attempt_evidence_dict(evidence: C.AttemptEvidence) -> tuple:
    return (
        evidence.result.attempt_id,
        _json_bytes(C._attempt_dict(evidence.result)),
        None if evidence.inventory is None else _json_bytes(
            _inventory_dict(evidence.inventory)),
        int(evidence.terminal_complete),
        int(evidence.parallel_identity),
        evidence.runtime_identity,
    )


def _obligation_dict(obligation: C.Obligation) -> dict:
    return {
        "file": obligation.file,
        "test_id": obligation.test_id,
        "sequence": obligation.sequence,
        "source_digest": obligation.source_digest,
        "compatibility": obligation.compatibility,
        "reason": obligation.reason,
    }


def _decode_json(text: str) -> object:
    try:
        return json.loads(text)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _snapshot_from_dict(value: object) -> C.InputSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _HistoryStateError("coordinator-corrupt")
    if set(value) != {
        "digest", "compatibility", "head", "clean", "changes",
        "limitations", "files", "baseline_head",
    }:
        raise _HistoryStateError("coordinator-corrupt")
    try:
        changes = tuple(C.Change(**item) for item in value["changes"])
        limitations = tuple(C.Reason(**item) for item in value["limitations"])
        files = tuple(C.FileFingerprint(**item) for item in value["files"])
        return C.InputSnapshot(
            digest=value["digest"], compatibility=value["compatibility"],
            head=value["head"], clean=value["clean"], changes=changes,
            limitations=limitations, files=files, baseline_head=value["baseline_head"],
        )
    except (KeyError, TypeError, ValueError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _inventory_from_dict(value: object) -> C.Inventory:
    if not isinstance(value, dict):
        raise _HistoryStateError("coordinator-corrupt")
    if set(value) != {"adapter", "version", "complete", "tests", "digest"}:
        raise _HistoryStateError("coordinator-corrupt")
    try:
        tests = tuple(
            _test_record_from_dict(item)
            for item in value["tests"]
        )
        return C.Inventory(
            adapter=value["adapter"], version=value["version"],
            complete=value["complete"], tests=tests, digest=value["digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _test_record_from_dict(value: object) -> C.TestRecord:
    if not isinstance(value, dict) or set(value) != {
        "id", "file", "outcome", "setup_s", "call_s", "teardown_s",
    }:
        raise _HistoryStateError("coordinator-corrupt")
    try:
        return C.TestRecord(
            id=value["id"], file=value["file"], outcome=value["outcome"],
            setup_s=value["setup_s"], call_s=value["call_s"],
            teardown_s=value["teardown_s"],
        )
    except (KeyError, TypeError, ValueError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _obligation_from_row(row: sqlite3.Row | tuple) -> C.Obligation:
    try:
        if len(row) != 6 or not isinstance(row[5], str) or row[5] not in C.REASON_CODES:
            raise _HistoryStateError("coordinator-corrupt")
        return C.Obligation(
            file=row[0], test_id=row[1], sequence=row[2],
            source_digest=row[3], compatibility=row[4], reason=row[5],
        )
    except (TypeError, ValueError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _quarantine_from_row(
        row: sqlite3.Row | tuple | None) -> C.SelectionQuarantine | None:
    if row is None:
        return None
    try:
        if len(row) != 7:
            raise ValueError
        return C.SelectionQuarantine(
            code=row[0], run_id=row[1], sequence=row[2],
            policy_digest=row[3], compatibility=row[4],
            input_digest=row[5], verdict=row[6])
    except (TypeError, ValueError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _validate_compound_evidence(connection: sqlite3.Connection) -> None:
    run_ids = {
        row[0] for row in connection.execute("SELECT run_id FROM runs").fetchall()
    }
    rows = connection.execute(
        "SELECT run_id, attempt_id, result, inventory, terminal_complete, "
        "parallel_identity, runtime_identity FROM attempt_evidence"
    ).fetchall()
    for run_id, attempt_id, result_text, inventory_text, terminal, parallel, runtime in rows:
        if (not isinstance(run_id, str) or run_id not in run_ids
                or not isinstance(attempt_id, str)
                or not isinstance(result_text, str)
                or terminal not in (0, 1) or parallel not in (0, 1)
                or (runtime is not None and not isinstance(runtime, str))):
            raise _HistoryStateError("coordinator-corrupt")
        value = _decode_json(result_text)
        if not isinstance(value, dict):
            raise _HistoryStateError("coordinator-corrupt")
        try:
            attempt = C.AttemptResult(
                attempt_id=value["attempt_id"], phase=value["phase"],
                status=value["status"], raw_exit_code=value["raw_exit_code"],
                final_exit_code=value["final_exit_code"],
                source_valid=value["source_valid"],
                inventory_complete=value["inventory_complete"],
                timings=None)
        except (KeyError, TypeError, ValueError):
            raise _HistoryStateError("coordinator-corrupt") from None
        if attempt.attempt_id != attempt_id:
            raise _HistoryStateError("coordinator-corrupt")
        inventory = None
        if inventory_text is not None:
            if not isinstance(inventory_text, str):
                raise _HistoryStateError("coordinator-corrupt")
            inventory = _inventory_from_dict(_decode_json(inventory_text))
        try:
            C.AttemptEvidence(
                attempt_id=attempt_id, result=attempt,
                inventory=inventory, terminal_complete=bool(terminal),
                parallel_identity=bool(parallel), runtime_identity=runtime,
            )
        except (TypeError, ValueError):
            raise _HistoryStateError("coordinator-corrupt") from None
    receipts = connection.execute(
        "SELECT run_id, verdict, expected_quarantine FROM comparison_receipts"
    ).fetchall()
    for run_id, verdict, expected_text in receipts:
        if (not isinstance(run_id, str) or run_id not in run_ids
                or verdict not in {
                "matched", "suspected-miss", "unclassified-divergence",
                "incomplete"}):
            raise _HistoryStateError("coordinator-corrupt")
        if expected_text is not None:
            value = _decode_json(expected_text)
            if not isinstance(value, dict):
                raise _HistoryStateError("coordinator-corrupt")
            try:
                C.SelectionQuarantine(**value)
            except (TypeError, ValueError):
                raise _HistoryStateError("coordinator-corrupt") from None


def _validated_summary(value: object) -> dict:
    if not isinstance(value, dict):
        raise _HistoryStateError("coordinator-corrupt")
    try:
        raw = C.encode_public_document("run", value)
        return C.decode_public_document(raw).data
    except (C.Problem, TypeError, ValueError, RecursionError):
        raise _HistoryStateError("coordinator-corrupt") from None


def _summary_rows(connection: sqlite3.Connection, limit: int | None) -> tuple[dict, ...]:
    cap = HISTORY_MAX_SUMMARIES if limit is None else limit
    rows = connection.execute(
        "SELECT summary FROM runs ORDER BY sequence DESC, rowid DESC LIMIT ?",
        (cap,),
    ).fetchall()
    return tuple(_validated_summary(_decode_json(row[0])) for row in rows)


def _validate_private_runs(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT run_id, sequence, finished_at, summary, input_before, input_after, "
        "policy_digest, source_digest, compatibility, mode, status, inventory FROM runs"
    ).fetchall()
    for row in rows:
        run_id, sequence, finished_at, summary_text = row[:4]
        if (
            not isinstance(run_id, str)
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or not isinstance(finished_at, str)
            or not isinstance(summary_text, str)
        ):
            raise _HistoryStateError("coordinator-corrupt")
        summary = _validated_summary(_decode_json(summary_text))
        if (
            summary.get("run_id") != run_id
            or summary.get("finished_at") != finished_at
            or summary.get("mode") != row[9]
            or summary.get("status") != row[10]
        ):
            raise _HistoryStateError("coordinator-corrupt")
        snapshots = []
        for private_snapshot in row[4:6]:
            if private_snapshot is None:
                snapshots.append(None)
            else:
                if not isinstance(private_snapshot, str):
                    raise _HistoryStateError("coordinator-corrupt")
                snapshots.append(_snapshot_from_dict(_decode_json(private_snapshot)))
        for digest in (row[6], row[7]):
            if digest is not None and (
                not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None
            ):
                raise _HistoryStateError("coordinator-corrupt")
        if row[8] is not None and not isinstance(row[8], str):
            raise _HistoryStateError("coordinator-corrupt")
        if row[11] is not None:
            if not isinstance(row[11], str):
                raise _HistoryStateError("coordinator-corrupt")
            inventory = _inventory_from_dict(_decode_json(row[11]))
        else:
            inventory = None
        expected_digest, expected_compatibility = _snapshots_identity(
            snapshots[0], snapshots[1], summary,
        )
        if row[7] != expected_digest or row[8] != expected_compatibility:
            raise _HistoryStateError("coordinator-corrupt")
        if inventory is not None and len({item.id for item in inventory.tests}) != len(inventory.tests):
            raise _HistoryStateError("coordinator-corrupt")


def _validate_baseline(
    connection: sqlite3.Connection, baseline: C.Baseline, sequence: object,
) -> None:
    row = connection.execute(
        "SELECT sequence, input_after, policy_digest, source_digest, compatibility, inventory "
        "FROM runs WHERE run_id = ?",
        (baseline.run_id,),
    ).fetchone()
    if row is None or row[1] is None or row[5] is None:
        raise _HistoryStateError("coordinator-corrupt")
    snapshot = _snapshot_from_dict(_decode_json(row[1]))
    inventory = _inventory_from_dict(_decode_json(row[5]))
    if snapshot is None or (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or row[0] != sequence
        or snapshot.head != baseline.head
        or snapshot.digest != baseline.input_digest
        or snapshot.compatibility != baseline.compatibility
        or row[2] != baseline.policy_digest
        or row[3] != baseline.input_digest
        or row[4] != baseline.compatibility
        or inventory != baseline.inventory
    ):
        raise _HistoryStateError("coordinator-corrupt")


def _validate_reconciliations(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT reconciliation_key, file, test_id, sequence, compatibility, outcome "
        "FROM reconciliations"
    ).fetchall()
    for key, file, test_id, sequence, compatibility, outcome in rows:
        if (
            not isinstance(key, str)
            or (file is not None and not isinstance(file, str))
            or (test_id is not None and not isinstance(test_id, str))
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
            or (compatibility is not None and not isinstance(compatibility, str))
            or outcome != "removed"
        ):
            raise _HistoryStateError("coordinator-corrupt")
        if key != _obligation_key(file, test_id):
            raise _HistoryStateError("coordinator-corrupt")


def _read_state(
    connection: sqlite3.Connection, marker_code: str | None = None,
) -> tuple[C.Baseline | None, tuple[C.Obligation, ...], bool,
           tuple[C.Reason, ...], C.SelectionQuarantine | None]:
    disabled_row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'selection_disabled'"
    ).fetchone()
    if disabled_row is None or disabled_row[0] not in {"0", "1"}:
        raise _HistoryStateError("coordinator-corrupt")
    compound_schema = _compound_schema_present(connection)
    runtime_column = ", runtime_identity" if compound_schema else ""
    baseline_row = connection.execute(
        "SELECT run_id, sequence, head, input_digest, compatibility, inventory, "
        f"policy_digest, created_at{runtime_column}"  # nosec B608 - runtime_column is closed internal schema text
        " FROM baselines WHERE singleton = 1"
    ).fetchone()
    baseline = None
    if baseline_row is not None:
        try:
            baseline = C.Baseline(
                run_id=baseline_row[0], head=baseline_row[2],
                input_digest=baseline_row[3], compatibility=baseline_row[4],
                inventory=_inventory_from_dict(_decode_json(baseline_row[5])),
                policy_digest=baseline_row[6], created_at=baseline_row[7],
                runtime_identity=baseline_row[8] if compound_schema else None,
            )
        except (TypeError, ValueError):
            raise _HistoryStateError("coordinator-corrupt") from None
    rows = connection.execute(
        "SELECT file, test_id, sequence, source_digest, compatibility, reason "
        "FROM obligations ORDER BY sequence ASC, obligation_key ASC"
    ).fetchall()
    obligations = tuple(_obligation_from_row(row) for row in rows)
    quarantine = None
    if compound_schema:
        quarantine = _quarantine_from_row(connection.execute(
            "SELECT code, run_id, sequence, policy_digest, compatibility, "
            "input_digest, verdict FROM selection_quarantine WHERE singleton = 1"
        ).fetchone())
    # Validate every retained public row before making history usable.  A
    # malformed summary is uncertainty, not a reason to silently discard it.
    _validate_private_runs(connection)
    _validate_reconciliations(connection)
    if compound_schema:
        _validate_compound_evidence(connection)
    if baseline is not None:
        _validate_baseline(connection, baseline, baseline_row[1])
    if marker_code is not None:
        disabled = True
        limitations = (_reason(marker_code, "history selection is disabled"),)
    elif disabled_row[0] == "1":
        disabled = True
        limitations = (_reason("selection-disabled", "history selection is disabled"),)
    elif baseline is None:
        disabled = False
        limitations = (_reason("no-baseline", "no compatible clean full baseline is available"),)
    else:
        disabled = False
        limitations = ()
    if quarantine is not None:
        disabled = True
        limitations += (_reason(
            "selection-shadow-quarantine",
            "selection is quarantined pending a corrected shadow comparison"),)
    return baseline, obligations, disabled, limitations, quarantine


def _disabled_view(problem: C.Problem) -> C.HistoryView:
    return C.HistoryView(
        baseline=None, obligations=(), selection_disabled=True,
        limitations=(_reason(problem.code, "history state is unavailable"),),
    )


def read_history(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> C.HistoryView:
    """Read history without creating a directory, database or fingerprint key."""
    _validate_arguments(domain, checkout)
    connection = None
    try:
        marker_code = _selection_marker(domain, checkout)
        connection = _open_store(domain, checkout, create=False)
        if connection is None:
            if marker_code is not None:
                return _disabled_view(_problem(marker_code, "history selection is disabled"))
            return C.HistoryView(
                baseline=None, obligations=(), selection_disabled=False,
                limitations=(_reason("no-baseline", "no history has been recorded"),),
            )
        baseline, obligations, disabled, limitations, quarantine = _read_state(
            connection, marker_code,
        )
        return C.HistoryView(
            baseline=baseline, obligations=obligations,
            selection_disabled=disabled, limitations=limitations,
            selection_quarantine=quarantine,
        )
    except _HistoryStateError as exc:
        if _can_persist_disabled_marker(exc.code):
            _write_disabled_marker(domain, checkout, exc.code)
        return _disabled_view(_problem(exc.code, "history state is unavailable"))
    except C.Problem as exc:
        if _can_persist_disabled_marker(exc.code):
            _write_disabled_marker(domain, checkout, exc.code)
        return _disabled_view(exc)
    except OSError:
        return _disabled_view(_problem("coordinator-unavailable", "history state is unavailable"))
    except sqlite3.Error as exc:
        state_error = _state_error_for_sqlite(exc)
        if _can_persist_disabled_marker(state_error.code):
            _write_disabled_marker(domain, checkout, state_error.code)
        return _disabled_view(_problem(state_error.code, "history state is unavailable"))
    finally:
        if connection is not None:
            connection.close()


def _qualified_profile_digest(evidence: C.AttemptEvidence) -> str:
    """Hash the exact consumed evidence that authorizes a private profile."""
    return reports.evidence_content_digest(evidence)


def publish_qualified_profile(
    domain: C.DomainPaths,
    checkout: C.CheckoutIdentity,
    runner: C.RunnerKind | str,
    evidence: C.AttemptEvidence,
    *,
    binding: NativeReportBinding,
) -> None:
    """Persist a profile only from complete, passed native evidence.

    This is private promotion state.  It is deliberately not inferred from a
    config string or runner kind, and the writer lock makes replacement
    atomic.  A caller must first obtain ``evidence`` from the strict native
    report consumer and pass the same consumed binding.  The private consumed
    file identity is checked here as well, so a hand-shaped dataclass cannot
    create admission state.
    """
    _validate_arguments(domain, checkout)
    try:
        kind = runner if isinstance(runner, C.RunnerKind) else C.RunnerKind(runner)
    except (TypeError, ValueError):
        raise _problem("unsupported-capability", "native qualification runner is unsupported") from None
    if kind not in {C.RunnerKind.PYTEST, C.RunnerKind.VITEST}:
        raise _problem("unsupported-capability", "native qualification runner is unsupported")
    if (not isinstance(evidence, C.AttemptEvidence)
            or not isinstance(binding, NativeReportBinding)
            or binding._created_identity is None
            or binding._evidence_digest != reports.evidence_content_digest(evidence)
            or binding.runner != kind.value
            or binding.effective_profile != C.ExecutionTier.ADVANCED.value
            or binding.attempt_id != evidence.attempt_id):
        raise _problem("unsupported-capability", "native qualification lacks a consumed authenticated binding")
    if not _evidence_passes(evidence):
        raise _problem("unsupported-capability", "native qualification evidence is incomplete")
    if evidence.runtime_identity is None:
        raise _problem("unsupported-capability", "native qualification lacks runtime identity")
    if evidence.inventory is None or evidence.inventory.adapter != kind.value:
        raise _problem("unsupported-capability", "native qualification runner does not match inventory")
    profile = f"{kind.value}-advanced-v1"
    payload = {
        "version": 1,
        "runner": kind.value,
        "profile": profile,
        "attempt_id": evidence.attempt_id,
        "runtime_identity": evidence.runtime_identity,
        "inventory_digest": evidence.inventory.digest,
        "evidence_digest": _qualified_profile_digest(evidence),
        "parallel_identity": bool(evidence.parallel_identity),
    }
    raw = _json_bytes(payload).encode("utf-8")
    if len(raw) > _QUALIFIED_PROFILE_MAX_BYTES:
        raise _problem("capacity-exceeded", "native qualification evidence exceeds its bound")
    try:
        with _writer_lock(domain, checkout) as directory:
            publish_atomic(directory, _QUALIFIED_PROFILE_NAME, raw)
    except _HistoryStateError as exc:
        raise _problem(exc.code, "native qualification state is unavailable") from None
    except (C.Problem, OSError):
        raise _problem("coordinator-unavailable", "native qualification state is unavailable") from None


def read_qualified_profile(
    domain: C.DomainPaths,
    checkout: C.CheckoutIdentity,
    runner: C.RunnerKind | str,
) -> dict[str, str] | None:
    """Read one validated private qualification record without creating state."""
    _validate_arguments(domain, checkout)
    try:
        kind = runner if isinstance(runner, C.RunnerKind) else C.RunnerKind(runner)
    except (TypeError, ValueError):
        return None
    directory = _history_directory(domain, checkout, create=False)
    if directory is None:
        return None
    path = directory / _QUALIFIED_PROFILE_NAME
    try:
        validate_private_file(path)
        value = json.loads(read_regular(directory, _QUALIFIED_PROFILE_NAME,
                                        _QUALIFIED_PROFILE_MAX_BYTES).decode("utf-8"))
    except (C.Problem, OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    required = {"version", "runner", "profile", "attempt_id", "runtime_identity",
                "inventory_digest", "evidence_digest"}
    # Files written before parallel qualification carry no
    # ``parallel_identity`` key; they could only have been earned serially.
    optional = {"parallel_identity"}
    if (not isinstance(value, dict)
            or not required <= set(value) <= required | optional
            or value.get("version") != 1
            or value.get("runner") != kind.value
            or value.get("profile") != f"{kind.value}-advanced-v1"
            or not isinstance(value.get("attempt_id"), str)
            or not C.ATTEMPT_ID_PATTERN.fullmatch(value["attempt_id"])
            or not isinstance(value.get("runtime_identity"), str)
            or _DIGEST_RE.fullmatch(value["runtime_identity"]) is None
            or not isinstance(value.get("inventory_digest"), str)
            or _DIGEST_RE.fullmatch(value["inventory_digest"]) is None
            or not isinstance(value.get("evidence_digest"), str)
            or _DIGEST_RE.fullmatch(value["evidence_digest"]) is None
            or ("parallel_identity" in value
                and not isinstance(value["parallel_identity"], bool))):
        return None
    record = {key: value[key] for key in required if key != "version"}
    record["parallel_identity"] = bool(value.get("parallel_identity", False))
    return record


def next_sequence(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
) -> int:
    """Return the next private publication sequence without creating state."""
    _validate_arguments(domain, checkout)
    connection = None
    try:
        connection = _open_store(domain, checkout, create=False)
        if connection is None:
            return 0
        row = connection.execute(
            "SELECT MAX(sequence) FROM runs"
        ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0]) + 1
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise _problem("coordinator-unavailable", "history sequence is unavailable") from None
    finally:
        if connection is not None:
            connection.close()


def _snapshots_identity(
    before: C.InputSnapshot | None, after: C.InputSnapshot | None,
    summary: dict | None = None,
) -> tuple[str | None, str | None]:
    if before is not None and after is not None:
        if before.digest != after.digest or before.compatibility != after.compatibility:
            return None, None
    snapshot = after or before
    if snapshot is not None:
        return snapshot.digest, snapshot.compatibility
    if summary is None:
        return None, None
    return summary["plan"]["input_digest"], summary["plan"]["compatibility"]


def _result_identity(result: C.RunResult) -> tuple[str | None, str | None]:
    if result.input_before is not None or result.input_after is not None:
        return _snapshots_identity(result.input_before, result.input_after)
    return result.plan.input_digest, result.plan.compatibility


def _clean_full(
    result: C.RunResult, inventory: C.Inventory | None, *, require_tests: bool,
) -> bool:
    if inventory is None or not inventory.complete:
        return False
    if require_tests and not inventory.tests:
        return False
    if any(item.outcome is C.Outcome.UNKNOWN for item in inventory.tests):
        return False
    if len({item.id for item in inventory.tests}) != len(inventory.tests):
        return False
    if not (
        result.mode is C.Mode.FULL
        and result.plan.execution == "full"
        and result.status is C.Status.PASSED
        and result.phase == "complete"
        and result.source_valid
        and result.full_gate_eligible
        and result.exit_code == 0
        and result.runner_exit_code == 0
        and result.input_before is not None
        and result.input_after is not None
        and result.input_before.clean
        and result.input_after.clean
        # A clean checkout can contain committed changes since the prior baseline.
        and not result.input_before.limitations
        and not result.input_after.limitations
        and result.input_before.digest is not None
        and result.input_before.digest == result.input_after.digest
        and result.input_before.compatibility is not None
        and result.input_before.compatibility == result.input_after.compatibility
        and result.input_before.head is not None
        and result.input_before.head == result.input_after.head
        and result.plan.input_digest == result.input_after.digest
        and result.plan.compatibility == result.input_after.compatibility
        and result.policy_digest is not None
    ):
        return False
    if any(item.outcome in _FAILURE_OUTCOMES for item in inventory.tests):
        return False
    if any(item.status in _FAILED_ATTEMPT_STATUSES for item in result.attempts):
        return False
    return all(item.id and item.file for item in inventory.tests)


def _conclusive_pass(
    result: C.RunResult, inventory: C.Inventory | None,
) -> bool:
    return bool(
        inventory is not None
        and inventory.complete
        and result.status is C.Status.PASSED
        and result.phase == "complete"
        and result.source_valid
        and result.exit_code == 0
        and result.runner_exit_code == 0
        and result.mode in {C.Mode.AUTOMATIC, C.Mode.SCOPED, C.Mode.FULL}
        and not any(
            item.outcome in _FAILURE_OUTCOMES | {C.Outcome.UNKNOWN}
            for item in inventory.tests
        )
        and len({item.id for item in inventory.tests}) == len(inventory.tests)
        and all(item.id and item.file for item in inventory.tests)
    )


def _same_compatibility(obligation: C.Obligation, result: C.RunResult) -> bool:
    _, compatibility = _result_identity(result)
    return (
        compatibility is not None
        and obligation.compatibility is not None
        and compatibility == obligation.compatibility
    )


def _obligation_key(file: str | None, test_id: str | None) -> str:
    if test_id is not None:
        return "test:" + test_id
    if file is not None:
        return "file:" + file
    return _WHOLE_GATE_KEY


def _upsert_obligation(
    connection: sqlite3.Connection, obligation: C.Obligation,
) -> None:
    key = _obligation_key(obligation.file, obligation.test_id)
    old = connection.execute(
        "SELECT sequence FROM obligations WHERE obligation_key = ?", (key,)
    ).fetchone()
    if old is not None and old[0] > obligation.sequence:
        return
    connection.execute(
        "INSERT INTO obligations(obligation_key, file, test_id, sequence, source_digest, compatibility, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(obligation_key) DO UPDATE SET file=excluded.file, test_id=excluded.test_id, "
        "sequence=excluded.sequence, source_digest=excluded.source_digest, "
        "compatibility=excluded.compatibility, reason=excluded.reason",
        (
            key, obligation.file, obligation.test_id, obligation.sequence,
            obligation.source_digest, obligation.compatibility, obligation.reason,
        ),
    )


def _record_reconciliation(
    connection: sqlite3.Connection, obligation: C.Obligation, sequence: int,
    compatibility: str | None,
) -> None:
    key = _obligation_key(obligation.file, obligation.test_id)
    connection.execute(
        "INSERT OR REPLACE INTO reconciliations "
        "(reconciliation_key, file, test_id, sequence, compatibility, outcome) "
        "VALUES (?, ?, ?, ?, ?, 'removed')",
        (key, obligation.file, obligation.test_id, sequence, compatibility),
    )


def _clear_passed_obligations(
    connection: sqlite3.Connection, result: C.RunResult, inventory: C.Inventory,
) -> None:
    if not _conclusive_pass(result, inventory):
        return
    clean_full = _clean_full(result, inventory, require_tests=True)
    records = {
        item.id: item for item in inventory.tests if item.outcome is _SUCCESS_OUTCOME
    }
    rows = connection.execute(
        "SELECT obligation_key, file, test_id, sequence, source_digest, compatibility, reason "
        "FROM obligations"
    ).fetchall()
    for row in rows:
        obligation = _obligation_from_row(row[1:])
        record = records.get(obligation.test_id) if obligation.test_id else None
        if (
            record is None
            or result.sequence <= obligation.sequence
            or obligation.file != record.file
        ):
            continue
        if obligation.compatibility is None:
            if not clean_full:
                continue
        elif not _same_compatibility(obligation, result):
            continue
        connection.execute(
            "DELETE FROM obligations WHERE obligation_key = ?", (row[0],)
        )


def _reconcile_full_inventory(
    connection: sqlite3.Connection, result: C.RunResult, inventory: C.Inventory,
) -> None:
    if not _clean_full(result, inventory, require_tests=True):
        return
    _, compatibility = _result_identity(result)
    rows = connection.execute(
        "SELECT obligation_key, file, test_id, sequence, source_digest, compatibility, reason "
        "FROM obligations"
    ).fetchall()
    ids = {item.id for item in inventory.tests}
    files = {item.file for item in inventory.tests}
    for row in rows:
        obligation = _obligation_from_row(row[1:])
        if obligation.file is None and obligation.test_id is None:
            # Baseline publication owns whole-gate recovery; it is not a
            # deleted-test reconciliation.
            continue
        if obligation.compatibility is None:
            # A clean full inventory is the only evidence that can recover an
            # obligation created before source compatibility was known.
            pass
        elif not _same_compatibility(obligation, result):
            continue
        if obligation.test_id is not None and obligation.test_id in ids:
            continue
        if obligation.test_id is None and obligation.file is not None and obligation.file in files:
            continue
        if result.sequence <= obligation.sequence:
            continue
        connection.execute(
            "DELETE FROM obligations WHERE obligation_key = ?", (row[0],)
        )
        _record_reconciliation(connection, obligation, result.sequence, compatibility)


def _failure_reasons(
    result: C.RunResult, inventory: C.Inventory | None,
) -> tuple[C.Reason, ...]:
    if any(item.status in _FAILED_ATTEMPT_STATUSES for item in result.attempts):
        return (_reason("full-gate-obligation", "a phase or teardown failed"),)
    if result.status is C.Status.FAILED and not (
        inventory is not None and any(item.outcome in _FAILURE_OUTCOMES for item in inventory.tests)
    ):
        return (_reason("full-gate-obligation", "a failed gate has no attributable test failure"),)
    if result.status in {C.Status.INCOMPLETE, C.Status.CANCELLED, C.Status.NOT_RUN}:
        return (_reason("incomplete-inventory", "the completed test inventory is unavailable"),)
    if inventory is not None and not inventory.complete:
        return (_reason("incomplete-inventory", "the test inventory is incomplete"),)
    if inventory is not None and any(item.outcome is C.Outcome.UNKNOWN for item in inventory.tests):
        return (_reason("incomplete-inventory", "the test inventory contains unknown outcomes"),)
    return ()


def _baseline_reasons(
    result: C.RunResult, inventory: C.Inventory | None,
) -> tuple[C.Reason, ...]:
    if result.status is C.Status.NO_TESTS_NEEDED:
        return (_reason("no-tests-needed", "no tests were executed"),)
    if result.status is not C.Status.PASSED:
        return (_reason("full-gate-obligation", "the full gate did not pass"),)
    if result.input_before is None or result.input_after is None:
        return (_reason("no-baseline", "source identity was not captured"),)
    if result.input_before.digest != result.input_after.digest:
        return (_reason("changed-during-run", "source identity changed during the run"),)
    if not result.input_before.clean or not result.input_after.clean:
        return (_reason("unknown-input", "the full gate ran on a dirty source tree"),)
    if inventory is None or not inventory.complete or any(
        item.outcome is C.Outcome.UNKNOWN for item in (inventory.tests if inventory else ())
    ):
        return (_reason("incomplete-inventory", "the full test inventory is not conclusive"),)
    return (_reason("no-baseline", "the outcome is not eligible for a clean full baseline"),)


def _insert_summary(
    connection: sqlite3.Connection, result: C.RunResult,
    inventory: C.Inventory | None,
) -> None:
    summary = C.serialize_run_result(result)
    before = _json_bytes(_snapshot_dict(result.input_before)) if result.input_before else None
    after = _json_bytes(_snapshot_dict(result.input_after)) if result.input_after else None
    source_digest, compatibility = _result_identity(result)
    try:
        connection.execute(
            "INSERT INTO runs(run_id, sequence, finished_at, summary, input_before, input_after, "
            "policy_digest, source_digest, compatibility, mode, status, inventory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.run_id, result.sequence, result.finished_at, _json_bytes(summary),
                before, after, result.policy_digest, source_digest, compatibility,
                result.mode.value, result.status.value,
                None if inventory is None else _json_bytes(_inventory_dict(inventory)),
            ),
        )
    except sqlite3.IntegrityError:
        old = connection.execute(
            "SELECT summary, sequence, input_before, input_after, policy_digest, "
            "source_digest, compatibility, mode, status, inventory "
            "FROM runs WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        expected_inventory = None if inventory is None else _json_bytes(_inventory_dict(inventory))
        expected = (
            _json_bytes(summary), result.sequence, before, after,
            result.policy_digest, source_digest, compatibility,
            result.mode.value, result.status.value, expected_inventory,
        )
        if old is None or tuple(old) != expected:
            raise _HistoryStateError("coordinator-corrupt") from None


def _publish_baseline(
    connection: sqlite3.Connection, result: C.RunResult, inventory: C.Inventory,
) -> bool:
    if not _clean_full(result, inventory, require_tests=True):
        return False
    old = connection.execute(
        "SELECT sequence FROM baselines WHERE singleton = 1"
    ).fetchone()
    if old is not None and old[0] >= result.sequence:
        return False
    snapshot = result.input_after
    assert snapshot is not None
    connection.execute(
        "INSERT INTO baselines(singleton, run_id, sequence, head, input_digest, compatibility, inventory, policy_digest, created_at, runtime_identity) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET run_id=excluded.run_id, sequence=excluded.sequence, "
        "head=excluded.head, input_digest=excluded.input_digest, compatibility=excluded.compatibility, "
        "inventory=excluded.inventory, policy_digest=excluded.policy_digest, "
        "created_at=excluded.created_at, runtime_identity=excluded.runtime_identity",
        (
            result.run_id, result.sequence, snapshot.head, snapshot.digest,
            snapshot.compatibility, _json_bytes(_inventory_dict(inventory)),
            result.policy_digest, result.finished_at, result.runtime_identity,
        ),
    )
    connection.execute(
        "DELETE FROM obligations WHERE obligation_key = ? AND sequence < ?",
        (_WHOLE_GATE_KEY, result.sequence),
    )
    return True


def _value_size(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8"))


def _run_storage_size(row: sqlite3.Row | tuple) -> int:
    return sum(_value_size(value) for value in row[1:])


def _obligation_storage_size(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT obligation_key, file, test_id, sequence, source_digest, "
        "compatibility, reason FROM obligations"
    ).fetchall()
    return sum(_value_size(value) for row in rows for value in row)


def _reconciliation_storage_size(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT reconciliation_key, file, test_id, sequence, compatibility, outcome "
        "FROM reconciliations"
    ).fetchall()
    return sum(_value_size(value) for row in rows for value in row)


def _baseline_storage_size(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT singleton, run_id, sequence, head, input_digest, compatibility, "
        "inventory, policy_digest, created_at, runtime_identity "
        "FROM baselines WHERE singleton = 1"
    ).fetchone()
    return 0 if row is None else sum(_value_size(value) for value in row)


def _quarantine_storage_size(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT singleton, code, run_id, sequence, policy_digest, "
        "compatibility, input_digest, verdict FROM selection_quarantine "
        "WHERE singleton = 1"
    ).fetchone()
    return 0 if row is None else sum(_value_size(value) for value in row)


def _attempt_evidence_storage_size(
        connection: sqlite3.Connection, run_id: str | None = None) -> int:
    statement = (
        "SELECT run_id, attempt_id, result, inventory, terminal_complete, "
        "parallel_identity, runtime_identity FROM attempt_evidence"
    )
    parameters: tuple = ()
    if run_id is not None:
        statement += " WHERE run_id = ?"
        parameters = (run_id,)
    rows = connection.execute(statement, parameters).fetchall()
    return sum(_value_size(value) for row in rows for value in row)


def _comparison_receipt_storage_size(
        connection: sqlite3.Connection, run_id: str | None = None) -> int:
    statement = (
        "SELECT run_id, verdict, expected_quarantine FROM comparison_receipts"
    )
    parameters: tuple = ()
    if run_id is not None:
        statement += " WHERE run_id = ?"
        parameters = (run_id,)
    rows = connection.execute(statement, parameters).fetchall()
    return sum(_value_size(value) for row in rows for value in row)


def _delete_run(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute("DELETE FROM attempt_evidence WHERE run_id = ?", (run_id,))
    connection.execute("DELETE FROM comparison_receipts WHERE run_id = ?", (run_id,))
    connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


def _utc_time(timestamp: str) -> datetime:
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _candidate_time(result: C.RunResult) -> datetime:
    try:
        return _utc_time(result.finished_at)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _incoming_storage_size(
    result: C.RunResult, inventory: C.Inventory | None,
    evidence: tuple[C.AttemptEvidence, ...] = (),
    comparison: C.ShadowComparison | None = None,
) -> int:
    before = _json_bytes(_snapshot_dict(result.input_before)) if result.input_before else None
    after = _json_bytes(_snapshot_dict(result.input_after)) if result.input_after else None
    source_digest, compatibility = _result_identity(result)
    stored_inventory = None if inventory is None else _json_bytes(_inventory_dict(inventory))
    size = sum(
        _value_size(value)
        for value in (
            result.run_id, result.sequence, result.finished_at,
            _json_bytes(C.serialize_run_result(result)), before, after,
            result.policy_digest, source_digest, compatibility,
            result.mode.value, result.status.value, stored_inventory,
        )
    )
    for item in evidence:
        size += _value_size(result.run_id)
        size += sum(_value_size(value) for value in _attempt_evidence_dict(item))
    if comparison is not None:
        expected = (
            None if comparison.expected_quarantine is None
            else _json_bytes(_quarantine_dict(comparison.expected_quarantine)))
        size += sum(_value_size(value) for value in (
            result.run_id, comparison.verdict, expected))
    return size


def _prune(
    connection: sqlite3.Connection, *, protected_run_ids: tuple[str, ...] = (),
    incoming_bytes: int = 0, now: datetime,
) -> bool:
    protected = connection.execute(
        "SELECT run_id FROM baselines WHERE singleton = 1"
    ).fetchone()
    protected_ids = set(protected_run_ids)
    if protected is not None:
        protected_ids.add(protected[0])
    rows = connection.execute(
        "SELECT run_id, sequence, summary, input_before, input_after, policy_digest, "
        "source_digest, compatibility, mode, status, inventory "
        "FROM runs ORDER BY sequence DESC, rowid DESC"
    ).fetchall()
    keep = {
        row[0] for row in rows[:max(0, int(HISTORY_MAX_SUMMARIES))]
    }
    keep.update(protected_ids)
    cutoff = now - timedelta(days=HISTORY_RETAIN_DAYS)
    removed = False
    for row in rows:
        run_id = row[0]
        if run_id in protected_ids:
            continue
        finished_at = connection.execute(
            "SELECT finished_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if finished_at is None:
            continue
        try:
            old = _utc_time(finished_at[0]) < cutoff
        except (TypeError, ValueError, OverflowError):
            old = False
        if old or run_id not in keep:
            _delete_run(connection, run_id)
            removed = True

    reconciliation_rows = connection.execute(
        "SELECT reconciliation_key, sequence FROM reconciliations "
        "ORDER BY sequence DESC, rowid DESC"
    ).fetchall()
    reconciliation_cap = max(1, min(_RECONCILIATION_MAX_ROWS, int(HISTORY_MAX_SUMMARIES)))
    for reconciliation_key, _sequence in reconciliation_rows[reconciliation_cap:]:
        connection.execute(
            "DELETE FROM reconciliations WHERE reconciliation_key = ?",
            (reconciliation_key,),
        )
        removed = True

    # Use logical sizes to choose expendable rows, but rely on SQLite's actual
    # INSERT/SQLITE_FULL result below as the physical capacity authority.
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    reserve = page_size * 4
    logical_budget = max(0, HISTORY_MAX_BYTES - reserve)
    current = connection.execute(
        "SELECT run_id, sequence, summary, input_before, input_after, policy_digest, "
        "source_digest, compatibility, mode, status, inventory FROM runs"
    ).fetchall()
    logical = sum(_run_storage_size(row) for row in current)
    logical += (
        _obligation_storage_size(connection)
        + _reconciliation_storage_size(connection)
        + _baseline_storage_size(connection)
        + _quarantine_storage_size(connection)
        + _attempt_evidence_storage_size(connection)
        + _comparison_receipt_storage_size(connection)
        + incoming_bytes
    )
    physical = page_size * int(connection.execute("PRAGMA page_count").fetchone()[0])
    physical_shortfall = max(0, physical + incoming_bytes - HISTORY_MAX_BYTES)
    freed = 0
    candidates = [
        row for row in sorted(current, key=lambda item: (item[1], item[0]))
        if row[0] not in protected_ids
    ]
    for row in candidates:
        if logical <= logical_budget and freed >= physical_shortfall:
            break
        size = (
            _run_storage_size(row)
            + _attempt_evidence_storage_size(connection, row[0])
            + _comparison_receipt_storage_size(connection, row[0])
        )
        _delete_run(connection, row[0])
        logical -= size
        freed += size
        removed = True
    if logical > logical_budget or freed < physical_shortfall:
        reconciliation_rows = connection.execute(
            "SELECT reconciliation_key, file, test_id, sequence, compatibility, outcome "
            "FROM reconciliations ORDER BY sequence ASC, rowid ASC"
        ).fetchall()
        for row in reconciliation_rows:
            if logical <= logical_budget and freed >= physical_shortfall:
                break
            connection.execute(
                "DELETE FROM reconciliations WHERE reconciliation_key = ?", (row[0],)
            )
            size = sum(_value_size(value) for value in row)
            logical -= size
            freed += size
            removed = True
    return removed


def _needs_compaction(connection: sqlite3.Connection) -> bool:
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
    pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
    return page_size * freelist >= _COMPACT_MIN_FREE_BYTES and freelist * 4 >= pages


def _compact(connection: sqlite3.Connection) -> None:
    """Reclaim pages only after the pruning transaction has committed."""
    connection.execute("VACUUM")


def _maintenance_prune(
    connection: sqlite3.Connection, result: C.RunResult,
    inventory: C.Inventory | None,
    evidence: tuple[C.AttemptEvidence, ...] = (),
    comparison: C.ShadowComparison | None = None,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        pruned = _prune(
            connection,
            protected_run_ids=(result.run_id,),
            incoming_bytes=_incoming_storage_size(
                result, inventory, evidence, comparison),
            now=_candidate_time(result),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    if pruned and _needs_compaction(connection):
        _best_effort_compaction(connection)


def _best_effort_compaction(connection: sqlite3.Connection) -> None:
    try:
        _compact(connection)
    except (sqlite3.Error, OSError):
        # VACUUM uses temporary space and may fail even after a valid commit.
        # It is optional maintenance, never evidence of a failed publication.
        pass


def _capacity_prune(connection: sqlite3.Connection, run_id: str, batch: int) -> bool:
    """Evict real rows after SQLITE_FULL, independent of size estimates.

    The writer lock prevents concurrent publishers replenishing candidates.
    Geometrically growing batches exhaust ordinary runs first, then bounded
    reconciliation records. Baseline and compact failure/gate rows survive.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT run_id FROM runs WHERE run_id != ? AND run_id NOT IN "
            "(SELECT run_id FROM baselines) ORDER BY sequence ASC, run_id ASC LIMIT ?",
            (run_id, batch),
        ).fetchall()
        if rows:
            for (candidate_run_id,) in rows:
                _delete_run(connection, candidate_run_id)
        else:
            rows = connection.execute(
                "SELECT reconciliation_key FROM reconciliations "
                "ORDER BY sequence ASC, reconciliation_key ASC LIMIT ?", (batch,),
            ).fetchall()
            connection.executemany("DELETE FROM reconciliations WHERE reconciliation_key = ?", rows)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return bool(rows)


def _store_attempt_evidence(
        connection: sqlite3.Connection, run_id: str,
        evidence: tuple[C.AttemptEvidence, ...]) -> None:
    for item in evidence:
        values = _attempt_evidence_dict(item)
        try:
            connection.execute(
                "INSERT INTO attempt_evidence(run_id, attempt_id, result, "
                "inventory, terminal_complete, parallel_identity, runtime_identity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, *values),
            )
        except sqlite3.IntegrityError:
            old = connection.execute(
                "SELECT attempt_id, result, inventory, terminal_complete, "
                "parallel_identity, runtime_identity FROM attempt_evidence "
                "WHERE run_id = ? AND attempt_id = ?",
                (run_id, item.attempt_id),
            ).fetchone()
            if old is None or tuple(old) != values:
                raise _HistoryStateError("coordinator-corrupt") from None


def _store_comparison_receipt(
        connection: sqlite3.Connection, run_id: str,
        comparison: C.ShadowComparison) -> None:
    expected = (
        None if comparison.expected_quarantine is None
        else _json_bytes(_quarantine_dict(comparison.expected_quarantine)))
    try:
        connection.execute(
            "INSERT INTO comparison_receipts(run_id, verdict, "
            "expected_quarantine) VALUES (?, ?, ?)",
            (run_id, comparison.verdict, expected),
        )
    except sqlite3.IntegrityError:
        old = connection.execute(
            "SELECT verdict, expected_quarantine FROM comparison_receipts "
            "WHERE run_id = ?", (run_id,)).fetchone()
        if old is None or tuple(old) != (comparison.verdict, expected):
            raise _HistoryStateError("coordinator-corrupt") from None


def _evidence_passes(evidence: C.AttemptEvidence | None) -> bool:
    if (evidence is None or not evidence.terminal_complete
            or evidence.inventory is None or not evidence.inventory.complete):
        return False
    result = evidence.result
    return bool(
        result.status is C.Status.PASSED
        and result.raw_exit_code == 0 and result.final_exit_code == 0
        and result.inventory_complete
        and all(item.outcome not in _FAILURE_OUTCOMES | {C.Outcome.UNKNOWN}
                for item in evidence.inventory.tests)
    )


def _apply_quarantine_transition(
        connection: sqlite3.Connection, result: C.RunResult,
        comparison: C.ShadowComparison) -> None:
    if comparison.verdict == "incomplete":
        return
    if comparison.verdict == "matched" and comparison.expected_quarantine is None:
        return
    baseline, obligations, disabled, limitations, quarantine = _read_state(
        connection)
    history = C.HistoryView(
        baseline=baseline, obligations=obligations,
        selection_disabled=disabled, limitations=limitations,
        selection_quarantine=quarantine,
    )
    if not _shadow_transition_eligible(history, result, comparison):
        return
    source_digest, compatibility = _result_identity(result)
    if (source_digest is None or compatibility is None
            or result.policy_digest is None):
        return
    if comparison.verdict in {
            "suspected-miss", "unclassified-divergence"}:
        quarantine = C.SelectionQuarantine(
            code="selection-shadow-quarantine", run_id=result.run_id,
            sequence=result.sequence, policy_digest=result.policy_digest,
            compatibility=compatibility, input_digest=source_digest,
            verdict=comparison.verdict)
        old = _quarantine_from_row(connection.execute(
            "SELECT code, run_id, sequence, policy_digest, compatibility, "
            "input_digest, verdict FROM selection_quarantine WHERE singleton = 1"
        ).fetchone())
        if old is None or result.sequence > old.sequence:
            connection.execute(
                "INSERT INTO selection_quarantine(singleton, code, run_id, "
                "sequence, policy_digest, compatibility, input_digest, verdict) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET code=excluded.code, "
                "run_id=excluded.run_id, sequence=excluded.sequence, "
                "policy_digest=excluded.policy_digest, "
                "compatibility=excluded.compatibility, "
                "input_digest=excluded.input_digest, verdict=excluded.verdict",
                (quarantine.code, quarantine.run_id, quarantine.sequence,
                 quarantine.policy_digest, quarantine.compatibility,
                 quarantine.input_digest, quarantine.verdict),
            )
        return
    if comparison.verdict != "matched":
        return
    expected = comparison.expected_quarantine
    if expected is None or not (
            _evidence_passes(comparison.selected)
            and _evidence_passes(comparison.full)):
        return
    current = _quarantine_from_row(connection.execute(
        "SELECT code, run_id, sequence, policy_digest, compatibility, "
        "input_digest, verdict FROM selection_quarantine WHERE singleton = 1"
    ).fetchone())
    if current != expected or result.sequence <= current.sequence:
        return
    corrected = (
        result.policy_digest != current.policy_digest
        or compatibility != current.compatibility)
    baseline = connection.execute(
        "SELECT sequence, policy_digest, compatibility FROM baselines "
        "WHERE singleton = 1"
    ).fetchone()
    if not corrected or baseline is None or not (
            current.sequence < baseline[0] < result.sequence
            and baseline[1] == result.policy_digest
            and baseline[2] == compatibility):
        return
    cursor = connection.execute(
        "DELETE FROM selection_quarantine WHERE singleton = 1 AND code = ? "
        "AND run_id = ? AND sequence = ? AND policy_digest = ? "
        "AND compatibility = ? AND input_digest = ? AND verdict = ?",
        (current.code, current.run_id, current.sequence,
         current.policy_digest, current.compatibility,
         current.input_digest, current.verdict),
    )
    if cursor.rowcount != 1:
        raise _HistoryStateError("coordinator-corrupt")


def _publish_transaction(
    connection: sqlite3.Connection, result: C.RunResult,
    inventory: C.Inventory | None,
    evidence: tuple[C.AttemptEvidence, ...] = (),
    comparison: C.ShadowComparison | None = None,
) -> tuple[bool, bool]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _insert_summary(connection, result, inventory)
        if evidence:
            _store_attempt_evidence(connection, result.run_id, evidence)
        if comparison is not None:
            _store_comparison_receipt(connection, result.run_id, comparison)
        source_digest, compatibility = _result_identity(result)
        if inventory is not None:
            for record in inventory.tests:
                if record.outcome in _FAILURE_OUTCOMES:
                    _upsert_obligation(
                        connection,
                        C.Obligation(
                            file=record.file, test_id=record.id,
                            sequence=result.sequence, source_digest=source_digest,
                            compatibility=compatibility, reason="prior-failure",
                        ),
                    )
        for observed in evidence:
            if observed.inventory is None:
                continue
            for record in observed.inventory.tests:
                if record.outcome in _FAILURE_OUTCOMES:
                    _upsert_obligation(
                        connection,
                        C.Obligation(
                            file=record.file, test_id=record.id,
                            sequence=result.sequence,
                            source_digest=source_digest,
                            compatibility=compatibility,
                            reason="prior-failure",
                        ),
                    )
        failure_reasons = _failure_reasons(result, inventory)
        for failure_reason in failure_reasons:
            _upsert_obligation(
                connection,
                C.Obligation(
                    file=None, test_id=None, sequence=result.sequence,
                    source_digest=source_digest, compatibility=compatibility,
                    reason=failure_reason.code,
                ),
            )
        if inventory is not None:
            _clear_passed_obligations(connection, result, inventory)
            _reconcile_full_inventory(connection, result, inventory)
        baseline_published = False
        if inventory is not None:
            baseline_published = _publish_baseline(connection, result, inventory)
        if comparison is not None:
            _apply_quarantine_transition(connection, result, comparison)
        pruned = _prune(
            connection, protected_run_ids=(result.run_id,),
            now=_candidate_time(result),
        )
        _check_quota(connection)
        _read_state(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return baseline_published, pruned


def _publish_with_recovery(
    connection: sqlite3.Connection, result: C.RunResult,
    inventory: C.Inventory | None,
    evidence: tuple[C.AttemptEvidence, ...] = (),
    comparison: C.ShadowComparison | None = None,
) -> tuple[bool, bool]:
    try:
        _maintenance_prune(
            connection, result, inventory, evidence, comparison)
    except sqlite3.Error as exc:
        if not _is_sqlite_full(exc):
            raise
        # Even deleting estimated surplus can need a page. Try publication
        # and the same exhaustive recovery path rather than declaring full
        # while evictable rows are still present.
    # The finite candidate set cannot grow while the caller holds the checkout
    # lock. Growing batches exhaust it; one final compacted retry also covers
    # protected-only stores that already have free pages.
    remaining = connection.execute(
        "SELECT (SELECT COUNT(*) FROM runs) + (SELECT COUNT(*) FROM reconciliations)"
    ).fetchone()[0]
    batch = 1
    exhausted = False
    for _attempt in range(remaining + 2):
        try:
            return _publish_transaction(
                connection, result, inventory, evidence, comparison)
        except (sqlite3.Error, _HistoryStateError) as exc:
            full = (_is_sqlite_full(exc) if isinstance(exc, sqlite3.Error)
                    else exc.code == "capacity-exceeded")
            if not full:
                if _attempt and isinstance(exc, sqlite3.Error) and _is_transient_sqlite(exc):
                    raise _HistoryStateError(
                        "coordinator-unavailable", retain_uncertainty=True,
                    ) from None
                raise
            if exhausted:
                raise _HistoryStateError("capacity-exceeded") from None
            try:
                removed = _capacity_prune(connection, result.run_id, batch)
            except (sqlite3.Error, OSError):
                # Recovery was interrupted, not exhausted. Preserve the
                # already-reserved FULL evidence across this unavailable call.
                raise _HistoryStateError(
                    "coordinator-unavailable", retain_uncertainty=True,
                ) from None
            batch *= 2
            exhausted = not removed
            _best_effort_compaction(connection)
    raise AssertionError("capacity recovery did not exhaust its finite candidates")


def _publish_locked(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity,
    result: C.RunResult, inventory: C.Inventory | None,
    evidence: tuple[C.AttemptEvidence, ...] = (),
    comparison: C.ShadowComparison | None = None,
) -> C.PublishResult:
    with _writer_lock(domain, checkout) as directory:
        marker_code = _disabled_marker(domain, checkout)
        if marker_code is not None:
            raise _HistoryStateError(marker_code)
        publication = _publication_sequence(domain, checkout)
        reserve = _capacity_reserve_sequence(domain, checkout)
        capacity = _capacity_sequence(domain, checkout)
        # A reserve is only preallocated space. If its publisher disappeared,
        # the generic publication marker retains uncertainty; the reserve must
        # not be promoted without an observed capacity failure.
        if reserve is not None:
            _remove_marker(directory, _CAPACITY_RESERVE_NAME)
        # An active marker without generic uncertainty is from a writer that
        # never reached the database. The exclusive writer lock proves it no
        # longer owns publication, so it is safe to discard here.
        _remove_marker(directory, _ACTIVE_PUBLICATION_NAME)
        _reserve_publication(directory, result.sequence, capacity, publication)
        connection = None
        try:
            connection = _open_store(domain, checkout, create=True)
            assert connection is not None
            _read_state(connection)  # Never prune malformed evidence into apparent health.
            baseline_published, pruned = _publish_with_recovery(
                connection, result, inventory, evidence, comparison)
        except BaseException as exc:
            capacity_observed = (
                (isinstance(exc, sqlite3.Error) and _is_sqlite_full(exc))
                or (isinstance(exc, (_HistoryStateError, C.Problem))
                    and exc.code == "capacity-exceeded")
                or getattr(exc, "retain_uncertainty", False)
            )
            safe_transient = (
                (isinstance(exc, sqlite3.Error) and _is_transient_sqlite(exc))
                or (isinstance(exc, (_HistoryStateError, C.Problem))
                    and exc.code in {"coordinator-unavailable", "state-unavailable"}
                    and not getattr(exc, "retain_uncertainty", False))
            )
            try:
                if capacity_observed:
                    _claim_reserved_capacity(directory)
                elif safe_transient:
                    _discard_publication_reservation(
                        directory, retain_publication=publication is not None,
                    )
                else:
                    _retain_publication_uncertainty(directory)
            except (C.Problem, OSError):
                # All failure cleanups are conservative: a surviving generic
                # marker disables selection, and a surviving reserve can only
                # claim capacity after generic uncertainty is absent.
                pass
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise

        # Commit is final. No maintenance error below can retry publication or
        # change its receipt. Retain the marker unless a newer full gate can
        # account for ALL lost outcomes, including an overlapping late failure.
        authoritative_full = bool(
            _clean_full(result, inventory, require_tests=True)
            and inventory is not None
            and all(item.outcome is _SUCCESS_OUTCOME for item in inventory.tests)
        )
        recover_capacity = bool(
            capacity is not None
            and result.sequence > capacity
            and authoritative_full
        )
        recover_publication = bool(
            publication is not None
            and result.sequence > publication
            and authoritative_full
        )
        disabled_code = None
        if capacity is not None and not recover_capacity:
            disabled_code = "capacity-exceeded"
        elif recover_capacity:
            try:
                _clear_capacity(directory)
            except (C.Problem, OSError):
                disabled_code = "capacity-exceeded"
        if publication is not None and not recover_publication:
            disabled_code = disabled_code or "selection-disabled"
        try:
            _discard_publication_reservation(
                directory,
                retain_publication=publication is not None and not recover_publication,
            )
        except (C.Problem, OSError):
            disabled_code = disabled_code or "selection-disabled"
        if pruned:
            try:
                if _needs_compaction(connection):
                    _best_effort_compaction(connection)
            except (sqlite3.Error, OSError):
                pass
        reasons = (() if baseline_published else _baseline_reasons(result, inventory))
        if disabled_code is not None:
            reasons = (_reason(disabled_code, "history selection is disabled"),)
        quarantine_present = connection.execute(
            "SELECT 1 FROM selection_quarantine WHERE singleton = 1"
        ).fetchone() is not None
        if quarantine_present:
            reasons += (_reason(
                "selection-shadow-quarantine",
                "selection is quarantined pending a corrected shadow comparison"),)
        connection.close()
        return C.PublishResult(
            committed=True, baseline_published=baseline_published,
            selection_disabled=(disabled_code is not None or quarantine_present),
            reasons=reasons,
        )


def publish_outcome(
    domain: C.DomainPaths,
    checkout: C.CheckoutIdentity,
    result: C.RunResult,
    inventory: C.Inventory | None,
) -> C.PublishResult:
    """Commit one immutable result and reconcile durable obligations atomically."""
    _validate_arguments(domain, checkout)
    if not isinstance(result, C.RunResult):
        raise TypeError("history result must be RunResult")
    if inventory is not None and not isinstance(inventory, C.Inventory):
        raise TypeError("history inventory must be Inventory or None")
    try:
        marker_code = _disabled_marker(domain, checkout)
        if marker_code is not None:
            return C.PublishResult(
                committed=False, baseline_published=False, selection_disabled=True,
                reasons=(_reason(marker_code, "history selection is disabled"),),
            )
        return _publish_locked(domain, checkout, result, inventory)
    except (_HistoryStateError, C.Problem) as exc:
        code = exc.code
    except OSError:
        code = "coordinator-unavailable"
    except sqlite3.Error as exc:
        code = _state_error_for_sqlite(exc).code
    if _can_persist_disabled_marker(code):
        _write_disabled_marker(domain, checkout, code)
    return C.PublishResult(
        committed=False, baseline_published=False, selection_disabled=True,
        reasons=(_reason(code, "history state is unavailable"),),
    )


def _validate_compound_binding(
        checkout: C.CheckoutIdentity, result: C.RunResult,
        evidence: tuple[C.AttemptEvidence, ...], mode: C.Mode) -> None:
    if result.mode is not mode:
        raise ValueError(f"compound result mode must be {mode.value}")
    if (result.project_id != checkout.project_id
            or result.checkout_id != checkout.checkout_id):
        raise ValueError("compound result does not match checkout")
    if result.policy_digest is None:
        raise ValueError("compound result policy binding is required")
    if len({item.attempt_id for item in evidence}) != len(evidence):
        raise ValueError("compound evidence attempt ids must be distinct")
    if any(item.phase not in {"setup", "execution"}
           for item in result.attempts):
        raise ValueError("compound attempts may only use setup or execution phases")
    setup_attempts = tuple(item for item in result.attempts
                           if item.phase == "setup")
    execution_attempts = tuple(item for item in result.attempts
                               if item.phase == "execution")
    if len(setup_attempts) > 1:
        raise ValueError("compound result may contain one setup attempt")
    if setup_attempts and (
            result.attempts[0] is not setup_attempts[0]
            or setup_attempts[0].attempt_id != "a001"):
        raise ValueError("compound setup attempt must precede execution")
    expected_execution_ids = tuple(
        f"a{index:03d}" for index in range(1, len(execution_attempts) + 1))
    execution_ids = tuple(item.attempt_id for item in execution_attempts)
    if execution_ids != expected_execution_ids:
        raise ValueError("compound execution attempts must be ordered and contiguous")
    evidence_ids = tuple(item.attempt_id for item in evidence)
    if evidence_ids != execution_ids[:len(evidence)]:
        raise ValueError("compound evidence must be an ordered observed execution prefix")
    attempts_by_id = {item.attempt_id: item for item in execution_attempts}
    for observed in evidence:
        if observed.result != attempts_by_id[observed.attempt_id]:
            raise ValueError("compound evidence does not match result attempts")
    observed_ids = set(evidence_ids)
    for attempt in execution_attempts:
        if attempt.attempt_id in observed_ids:
            continue
        # The sole no-report exception is an authenticated SHADOW compound
        # deadline after a real a001 predecessor.  The guard's negative
        # signal remains per-attempt evidence (143 for raw -15), while the
        # aggregate result remains incomplete/70.
        deadline_no_report = (
            mode is C.Mode.SHADOW
            and result.status is C.Status.INCOMPLETE
            and result.exit_code == 70
            and len(execution_attempts) == 2
            and evidence_ids == ("a001",)
            and execution_attempts[0].attempt_id == "a001"
            and execution_attempts[0].status is not C.Status.NOT_RUN
            and attempt.attempt_id == "a002"
            and attempt.status is C.Status.INCOMPLETE
            and attempt.raw_exit_code is not None
            and attempt.raw_exit_code < 0
            and attempt.final_exit_code == 128 - attempt.raw_exit_code
            and any(reason.code == "execution-timeout"
                    for reason in result.reasons)
        )
        if deadline_no_report:
            continue
        if (attempt.status is not C.Status.NOT_RUN
                or attempt.raw_exit_code is not None
                or attempt.final_exit_code is not None
                or attempt.timings is not None):
            raise ValueError("unobserved compound attempts must be not-run")


def derive_shadow_verdict(
        selected: C.AttemptEvidence | None,
        full: C.AttemptEvidence | None) -> str:
    if selected is None or full is None:
        return "incomplete"
    if (not selected.terminal_complete or not full.terminal_complete
            or selected.runtime_identity is None
            or full.runtime_identity is None
            or selected.runtime_identity != full.runtime_identity
            or selected.inventory is None or full.inventory is None
            or not selected.inventory.complete or not full.inventory.complete
            or not selected.result.inventory_complete
            or not full.result.inventory_complete
            or not selected.result.source_valid or not full.result.source_valid
            or selected.result.status in {
                C.Status.INCOMPLETE, C.Status.CANCELLED, C.Status.NOT_RUN}
            or full.result.status in {
                C.Status.INCOMPLETE, C.Status.CANCELLED, C.Status.NOT_RUN}
            or any(item.outcome is C.Outcome.UNKNOWN
                   for item in (*selected.inventory.tests,
                                *full.inventory.tests))):
        return "incomplete"
    selected_by_id = {item.id: item for item in selected.inventory.tests}
    full_by_id = {item.id: item for item in full.inventory.tests}
    selected_files = {item.file for item in selected.inventory.tests}
    full_selected_ids = {
        item.id for item in full.inventory.tests if item.file in selected_files
    }
    if set(selected_by_id) != full_selected_ids:
        return "incomplete"
    if any(
            selected_by_id[test_id].outcome != full_by_id[test_id].outcome
            for test_id in selected_by_id):
        return "unclassified-divergence"
    if any(
            item.outcome in _FAILURE_OUTCOMES
            and item.file not in selected_files
            for item in full.inventory.tests):
        return "suspected-miss"
    attributed_selected_failure = any(
        item.outcome in _FAILURE_OUTCOMES
        for item in selected.inventory.tests)
    attributed_full_failure = any(
        item.outcome in _FAILURE_OUTCOMES
        for item in full.inventory.tests)
    if ((selected.result.status is C.Status.FAILED)
            != attributed_selected_failure
            or (full.result.status is C.Status.FAILED)
            != attributed_full_failure):
        return "unclassified-divergence"
    return "matched"


# Backward-compatible internal test seam; the implementation remains the
# single history-owned classifier above.
_derived_shadow_verdict = derive_shadow_verdict


def _shadow_transition_eligible(
        history: C.HistoryView, result: C.RunResult,
        comparison: C.ShadowComparison) -> bool:
    selected, full = comparison.selected, comparison.full
    baseline = history.baseline
    if (selected is None or full is None
            or result.plan.execution != "selected" or not result.plan.files
            or result.plan.baseline_run_id is None or baseline is None
            or baseline.run_id != result.plan.baseline_run_id
            or baseline.compatibility != result.plan.compatibility
            or baseline.policy_digest != result.policy_digest
            or result.input_before is None or result.input_after is None
            or result.input_before.digest != result.input_after.digest
            or result.input_before.compatibility != result.input_after.compatibility
            or not result.source_valid
            or selected.inventory is None or full.inventory is None
            or not selected.inventory.complete or not full.inventory.complete
            or selected.runtime_identity is None
            or selected.runtime_identity != full.runtime_identity):
        return False
    selected_files = {item.file for item in selected.inventory.tests}
    full_files = {item.file for item in full.inventory.tests}
    if (selected.attempt_id != "a001" or full.attempt_id != "a002"
            or selected_files != set(result.plan.files)
            or not selected_files < full_files):
        return False
    return True


def _publish_compound(
        domain: C.DomainPaths, checkout: C.CheckoutIdentity,
        result: C.RunResult, evidence: tuple[C.AttemptEvidence, ...],
        comparison: C.ShadowComparison | None) -> C.PublishResult:
    try:
        marker_code = _disabled_marker(domain, checkout)
        if marker_code is not None:
            return C.PublishResult(
                committed=False, baseline_published=False,
                selection_disabled=True,
                reasons=(_reason(
                    marker_code, "history selection is disabled"),),
            )
        return _publish_locked(
            domain, checkout, result, None, evidence, comparison)
    except (_HistoryStateError, C.Problem) as exc:
        code = exc.code
    except OSError:
        code = "coordinator-unavailable"
    except sqlite3.Error as exc:
        code = _state_error_for_sqlite(exc).code
    if _can_persist_disabled_marker(code):
        _write_disabled_marker(domain, checkout, code)
    return C.PublishResult(
        committed=False, baseline_published=False, selection_disabled=True,
        reasons=(_reason(code, "history state is unavailable"),),
    )


def publish_shadow_outcome(
        domain: C.DomainPaths, checkout: C.CheckoutIdentity,
        result: C.RunResult,
        comparison: C.ShadowComparison) -> C.PublishResult:
    """Atomically publish shadow evidence and its exact quarantine transition."""
    _validate_arguments(domain, checkout)
    if not isinstance(result, C.RunResult):
        raise TypeError("history result must be RunResult")
    if not isinstance(comparison, C.ShadowComparison):
        raise TypeError("shadow comparison must be ShadowComparison")
    evidence = tuple(
        item for item in (comparison.selected, comparison.full)
        if item is not None)
    _validate_compound_binding(checkout, result, evidence, C.Mode.SHADOW)
    if comparison.verdict != derive_shadow_verdict(
            comparison.selected, comparison.full):
        raise ValueError("shadow verdict does not match retained evidence")
    return _publish_compound(
        domain, checkout, result, evidence, comparison)


def publish_probe_outcome(
        domain: C.DomainPaths, checkout: C.CheckoutIdentity,
        result: C.RunResult,
        evidence: tuple[C.AttemptEvidence, ...]) -> C.PublishResult:
    """Atomically publish ordered probe evidence without baseline authority."""
    _validate_arguments(domain, checkout)
    if not isinstance(result, C.RunResult):
        raise TypeError("history result must be RunResult")
    if not isinstance(evidence, tuple) or not all(
            isinstance(item, C.AttemptEvidence) for item in evidence):
        raise TypeError("probe evidence must be a tuple of AttemptEvidence")
    _validate_compound_binding(checkout, result, evidence, C.Mode.PROBE)
    return _publish_compound(domain, checkout, result, evidence, None)


def _history_payload(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, limit: int | None,
) -> dict:
    _validate_arguments(domain, checkout)
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200
    ):
        raise _problem("invalid-bound", "history limit must be between 1 and 200")
    connection = None
    try:
        marker_code = _selection_marker(domain, checkout)
        connection = _open_store(domain, checkout, create=False)
        if connection is None:
            if marker_code is not None:
                raise _HistoryStateError(marker_code)
            return {"summaries": [], "obligations": []}
        _, obligations, _, _, _ = _read_state(
            connection, marker_code,
        )
        if marker_code is not None:
            raise _HistoryStateError(marker_code)
        data = {
            "summaries": list(_summary_rows(connection, limit)),
            "obligations": [_obligation_dict(item) for item in obligations],
        }
        raw = C.encode_public_document("history", data)
        return C.decode_public_document(raw).data
    except _HistoryStateError as exc:
        if _can_persist_disabled_marker(exc.code):
            _write_disabled_marker(domain, checkout, exc.code)
        raise _problem(exc.code, "history state is unavailable") from None
    except C.Problem as exc:
        if _can_persist_disabled_marker(exc.code):
            _write_disabled_marker(domain, checkout, exc.code)
        raise _problem(exc.code, "history state is unavailable") from None
    except OSError:
        raise _problem("coordinator-unavailable", "history state is unavailable") from None
    except sqlite3.Error as exc:
        state_error = _state_error_for_sqlite(exc)
        if _can_persist_disabled_marker(state_error.code):
            _write_disabled_marker(domain, checkout, state_error.code)
        raise _problem(state_error.code, "history state is unavailable") from None
    finally:
        if connection is not None:
            connection.close()


def read_history_summaries(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, limit: int | None = None,
) -> tuple[dict, ...]:
    """Return descriptor-validated public RunResult payloads for T11 rendering."""
    return tuple(_history_payload(domain, checkout, limit)["summaries"])


def read_history_payload(
    domain: C.DomainPaths, checkout: C.CheckoutIdentity, limit: int | None = None,
) -> dict:
    """Return the public history payload without a document envelope."""
    return _history_payload(domain, checkout, limit)
