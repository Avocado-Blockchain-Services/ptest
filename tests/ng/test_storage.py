"""SQLite opener bounds and failure tests (Task0 owned)."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from ptest.contracts import Problem
from ptest.storage import open_database


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
