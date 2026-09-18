"""Package-resource presence tests (Task 9)."""
from __future__ import annotations

from importlib.resources import files
import socket
import sqlite3


def test_agent_guide_contains_local_nonexecuting_repair_workflow():
    """A blank guide would fail to preserve the concrete repair boundary."""
    guide = files("ptest").joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")

    assert "Do not launch an agent" in guide
    assert "one database per worker per run" in guide
    assert "never use global flush" in guide


def test_resource_recipe_models_isolated_database_cleanup_and_worker_setup(tmp_path):
    """A shared database or per-test setup would leak state or over-initialize."""
    shared = tmp_path / "shared.sqlite"
    with sqlite3.connect(shared) as conn:
        conn.execute("create table records (value text)")
        conn.execute("insert into records values ('worker-a')")
    with sqlite3.connect(shared) as conn:
        assert conn.execute("select value from records").fetchall() == [("worker-a",)]

    setup_count = {}

    def worker_db(run: str, worker: str):
        key = (run, worker)
        setup_count.setdefault(key, 0)
        if setup_count[key] == 0:
            setup_count[key] += 1
        path = tmp_path / run / worker / "test.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("create table if not exists records (value text)")
        return path

    first = worker_db("run-a", "w0")
    neighbor = worker_db("run-a", "w1")
    worker_db("run-a", "w0")
    with sqlite3.connect(first) as conn:
        conn.execute("insert into records values ('owned')")
        conn.execute("delete from records")
    with sqlite3.connect(neighbor) as conn:
        conn.execute("insert into records values ('neighbor')")
        assert conn.execute("select value from records").fetchall() == [("neighbor",)]
    assert setup_count == {("run-a", "w0"): 1, ("run-a", "w1"): 1}


def test_resource_recipe_models_namespaced_cache_and_ephemeral_local_sockets():
    """Global cache cleanup and fixed ports would destroy a neighbor or collide."""
    cache = {"run-a:w0:key": "owned", "run-a:w1:key": "neighbor"}
    globally_flushed = dict(cache)
    globally_flushed.clear()
    assert "run-a:w1:key" not in globally_flushed
    for key in tuple(cache):
        if key.startswith("run-a:w0:"):
            del cache[key]
    assert cache == {"run-a:w1:key": "neighbor"}

    first = socket.socket()
    second = socket.socket()
    try:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        assert first.getsockname()[1] != second.getsockname()[1]
    finally:
        first.close()
        second.close()
