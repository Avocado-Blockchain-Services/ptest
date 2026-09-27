"""Common SQLite opener (Task0 owned).

Centralizes safe private database opening, bounds and verified pragmas.
Scheduler and history schemas, migration and retention belong to their
owners; this module never migrates, recreates, prunes or chooses a path.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
import urllib.parse
from pathlib import Path

from .contracts import Problem
from .files import validate_private_dir, validate_single_name

_PHASE = "storage"
_BUSY_TIMEOUT_MS = 2000

# SQLITE_BUSY / SQLITE_LOCKED (primary codes; masked to ignore extended bits).
_TRANSIENT_SQLITE_CODES = frozenset({5, 6})
_TRANSIENT_SQLITE_PHRASES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


def _fail(code: str, message: str) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=False)


class TransientContention(Problem):
    """Cause-verified lock contention: safe to auto-retry with backoff.

    Behavior-identical to a retryable ``coordinator-unavailable`` Problem
    (same code, phase carried by the raiser); the type additionally tells
    retry loops that the cause was a busy/locked database rather than a
    verdict that must stand. Raised ONLY after is_transient_sqlite passed.
    """

    def __init__(self, *, message: str, phase: str):
        super().__init__(code="coordinator-unavailable", message=message,
                         phase=phase, retryable=True)


def _sqlite_error_code(exc: sqlite3.Error) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def is_transient_sqlite(exc: sqlite3.Error) -> bool:
    """True when a SQLite error reports lock contention rather than damage.

    A busy/locked database is transient pressure from a concurrent writer:
    callers must wait with bounded retries, never treat it as corruption.
    Anything else (not-a-database, io errors, full) keeps its verdict.
    """
    code = _sqlite_error_code(exc)
    if code is not None and code & 0xFF in _TRANSIENT_SQLITE_CODES:
        return True
    text = str(exc).lower()
    return any(phrase in text for phrase in _TRANSIENT_SQLITE_PHRASES)


def _identity(path: Path) -> tuple:
    stamp = os.lstat(path)
    return (stamp.st_dev, stamp.st_ino)


def open_database(root: Path, name: str, *, max_bytes: int,
                  read_only: bool = False) -> sqlite3.Connection:
    """Open a private SQLite database with verified bounds and pragmas."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        _fail("invalid-bound", "max_bytes must be an int")
    if max_bytes <= 0 or max_bytes > (1 << 40):
        _fail("invalid-bound", "max_bytes is out of range")
    if not isinstance(read_only, bool):
        raise TypeError("read_only must be bool")
    anchor = Path(root)
    validate_private_dir(anchor)
    validate_single_name(name)
    path = anchor / name
    try:
        before = os.lstat(path)
        present = True
    except FileNotFoundError:
        before = None
        present = False
    if not present:
        if read_only:
            _fail("state-unavailable", f"database {name!r} does not exist")
        fd = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _fail("already-exists", f"database {name!r} appeared during open")
        except OSError:
            _fail("state-unavailable", f"cannot create database {name!r}")
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        before = os.lstat(path)
    else:
        if stat.S_ISLNK(before.st_mode):
            _fail("unsafe-path", f"database {name!r} is a symlink")
        if not stat.S_ISREG(before.st_mode):
            _fail("unsafe-path", f"database {name!r} is not a regular file")
        if before.st_uid != os.getuid():
            _fail("unsafe-path", f"database {name!r} has a foreign owner")
        if before.st_nlink != 1:
            _fail("unsafe-path", f"database {name!r} must have exactly one hard link")
        if before.st_size > max_bytes:
            _fail("capacity-exceeded", f"database {name!r} exceeds max_bytes")
    if read_only:
        absolute = path if path.is_absolute() else Path(os.path.abspath(path))
        target = f"file:{urllib.parse.quote(str(absolute), safe='/')}?mode=ro"
        try:
            conn = sqlite3.connect(target, uri=True)
        except sqlite3.Error:
            _fail("state-unavailable", f"cannot open database {name!r}")
            raise AssertionError("unreachable")
    else:
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error:
            _fail("state-unavailable", f"cannot open database {name!r}")
            raise AssertionError("unreachable")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if sys.platform == "darwin":
            conn.execute("PRAGMA fullfsync=ON")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        if not isinstance(page_size, int) or page_size <= 0:
            _fail("coordinator-corrupt", f"database {name!r} reports no page size")
        conn.execute(f"PRAGMA max_page_count={max(1, max_bytes // page_size)}")
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        foreign = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if (str(journal).lower() != "delete" or synchronous != 2
                or foreign != 1 or busy != _BUSY_TIMEOUT_MS):
            _fail("coordinator-corrupt", f"database {name!r} rejected safe pragmas")
        conn.execute("SELECT 1").fetchone()
    except Problem:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise
    except sqlite3.Error as exc:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if is_transient_sqlite(exc):
            raise TransientContention(message=f"database {name!r} is busy",
                                      phase=_PHASE)
        _fail("coordinator-corrupt", f"database {name!r} is corrupt")
        raise AssertionError("unreachable")
    try:
        after = _identity(path)
    except FileNotFoundError:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _fail("unsafe-path", f"database {name!r} vanished during open")
        raise AssertionError("unreachable")
    if after != (before.st_dev, before.st_ino):
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _fail("unsafe-path", f"database {name!r} was replaced during open")
    return conn
