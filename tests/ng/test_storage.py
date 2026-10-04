"""SQLite opener bounds and failure tests (Task0 owned)."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from ptest.contracts import Problem
from ptest.storage import open_database, recover_hot_journal
from support import leave_hot_journal


def _private_root(tmp_path, name="owned"):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def test_open_database_applies_safe_pragmas(tmp_path):
    root = _private_root(tmp_path)
    conn = open_database(root, "coord.sqlite3", max_bytes=16 << 20)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        foreign = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "delete"
        assert synchronous == 2
        assert foreign == 1
        assert busy == 2000
        conn.execute("CREATE TABLE t(x TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()
    assert stat.S_IMODE(os.stat(root / "coord.sqlite3").st_mode) == 0o600


def test_open_database_enforces_max_page_count(tmp_path):
    root = _private_root(tmp_path)
    conn = open_database(root, "small.sqlite3", max_bytes=1 << 20)
    try:
        pages = conn.execute("PRAGMA max_page_count").fetchone()[0]
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert pages * size <= (1 << 20) + size
    finally:
        conn.close()


def test_open_database_read_only_absent_is_typed_absence(tmp_path):
    root = _private_root(tmp_path)
    with pytest.raises(Problem, match="state-unavailable"):
        open_database(root, "absent.sqlite3", max_bytes=1 << 20, read_only=True)
    assert not (root / "absent.sqlite3").exists()


def test_open_database_read_only_never_creates(tmp_path):
    root = _private_root(tmp_path)
    conn = open_database(root, "data.sqlite3", max_bytes=16 << 20)
    conn.execute("CREATE TABLE t(x TEXT)")
    conn.commit()
    conn.close()
    before = (root / "data.sqlite3").read_bytes()
    reader = open_database(root, "data.sqlite3", max_bytes=16 << 20, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("CREATE TABLE evil(x TEXT)")
    finally:
        reader.close()
    assert (root / "data.sqlite3").read_bytes() == before


def test_open_database_refuses_oversize(tmp_path):
    root = _private_root(tmp_path)
    conn = open_database(root, "big.sqlite3", max_bytes=16 << 20)
    conn.execute("CREATE TABLE t(x BLOB)")
    conn.execute("INSERT INTO t VALUES (zeroblob(300000))")
    conn.commit()
    conn.close()
    with pytest.raises(Problem, match="capacity-exceeded"):
        open_database(root, "big.sqlite3", max_bytes=1024)


def test_open_database_rejects_corruption(tmp_path):
    root = _private_root(tmp_path)
    (root / "rot.sqlite3").write_bytes(b"not a database at all" * 64)
    os.chmod(root / "rot.sqlite3", 0o600)
    with pytest.raises(Problem, match="coordinator-corrupt"):
        open_database(root, "rot.sqlite3", max_bytes=16 << 20)


def test_open_database_rejects_symlink(tmp_path):
    root = _private_root(tmp_path)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"x" * 100)
    (root / "link.sqlite3").symlink_to(outside)
    with pytest.raises(Problem, match="unsafe-path"):
        open_database(root, "link.sqlite3", max_bytes=16 << 20)


def test_open_database_requires_private_root(tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    with pytest.raises(Problem, match="unsafe-path"):
        open_database(root, "data.sqlite3", max_bytes=16 << 20)


def test_open_database_read_only_escapes_special_characters(tmp_path):
    root = _private_root(tmp_path, "we?ird#name")
    conn = open_database(root, "data.sqlite3", max_bytes=16 << 20)
    try:
        conn.execute("CREATE TABLE t(x TEXT)")
        conn.execute("INSERT INTO t VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()
    reader = open_database(root, "data.sqlite3", max_bytes=16 << 20,
                           read_only=True)
    try:
        assert reader.execute("SELECT x FROM t").fetchone()[0] == "ok"
    finally:
        reader.close()


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


needs_proc_fd = pytest.mark.skipif(
    not os.path.exists("/proc/self/fd"),
    reason="descriptor accounting needs /proc/self/fd",
)



def _seed_rows(root, name, rows=("committed",)):
    seed = open_database(root, name, max_bytes=16 << 20)
    try:
        seed.execute("CREATE TABLE t(x TEXT)")
        seed.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
        seed.commit()
    finally:
        seed.close()


def test_open_database_read_only_rolls_back_crashed_writer_journal(tmp_path):
    """A dead writer's hot journal is recovery work, never corruption."""
    root = _private_root(tmp_path)
    _seed_rows(root, "coord.sqlite3")
    path = root / "coord.sqlite3"
    leave_hot_journal(path)
    inode = os.stat(path).st_ino
    conn = open_database(root, "coord.sqlite3", max_bytes=16 << 20, read_only=True)
    try:
        assert conn.execute("SELECT x FROM t").fetchall() == [("committed",)]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"t"}
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO t VALUES ('write')")
    finally:
        conn.close()
    assert not (root / "coord.sqlite3-journal").exists()
    assert os.stat(path).st_ino == inode
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600



def test_recover_hot_journal_never_creates_a_missing_database(tmp_path):
    root = _private_root(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        recover_hot_journal(root / "absent.sqlite3")
    assert not (root / "absent.sqlite3").exists()

def test_open_database_read_only_leaves_live_writer_transaction_alone(tmp_path):
    """Only a dead writer's journal is rolled back; a live one keeps its work."""
    root = _private_root(tmp_path)
    _seed_rows(root, "coord.sqlite3")
    writer = sqlite3.connect(str(root / "coord.sqlite3"), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO t VALUES ('pending')")
        conn = open_database(root, "coord.sqlite3", max_bytes=16 << 20,
                             read_only=True)
        try:
            assert conn.execute("SELECT x FROM t").fetchall() == [("committed",)]
        finally:
            conn.close()
        writer.execute("COMMIT")
    finally:
        writer.close()
    check = open_database(root, "coord.sqlite3", max_bytes=16 << 20, read_only=True)
    try:
        assert check.execute("SELECT x FROM t ORDER BY rowid").fetchall() == [
            ("committed",), ("pending",)]
    finally:
        check.close()

def test_open_database_reports_lock_contention_as_unavailable(tmp_path):
    """A write-locked DB is contention (retryable), never corruption evidence."""
    root = _private_root(tmp_path)
    seed = open_database(root, "hot.sqlite3", max_bytes=16 << 20)
    try:
        seed.execute("CREATE TABLE t(x TEXT)")
        seed.execute("INSERT INTO t VALUES ('seed')")
        seed.commit()
    finally:
        seed.close()
    holder = sqlite3.connect(str(root / "hot.sqlite3"), isolation_level=None)
    try:
        # EXCLUSIVE (a committing writer): even the opener's header/pragma
        # reads cannot proceed, so the busy wait expires inside the open.
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO t VALUES ('held')")
        # A second connection opens while the write lock is held past the
        # busy timeout: the opener must report contention, not corruption.
        with pytest.raises(Problem) as caught:
            open_database(root, "hot.sqlite3", max_bytes=16 << 20)
        assert caught.value.code == "coordinator-unavailable"
        assert caught.value.retryable is True
    finally:
        try:
            holder.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        holder.close()
    # After the lock releases the same open succeeds: nothing was corrupt.
    reopened = open_database(root, "hot.sqlite3", max_bytes=16 << 20)
    try:
        assert reopened.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        reopened.close()


@needs_proc_fd
def test_open_database_closes_connection_on_pragma_rejection(tmp_path):
    root = _private_root(tmp_path)
    conn = open_database(root, "wal.sqlite3", max_bytes=16 << 20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(x TEXT)")
        conn.commit()
    finally:
        conn.close()
    base = _fd_count()
    for _ in range(50):
        with pytest.raises(Problem, match="coordinator-corrupt"):
            open_database(root, "wal.sqlite3", max_bytes=16 << 20,
                          read_only=True)
    assert _fd_count() - base <= 2
