"""Packaged guidance and executable ownership recipe evidence (Task 9)."""
from __future__ import annotations

from importlib.resources import files
import errno
import shutil
import socket
import sqlite3

import pytest

from fixtures.doctor.ownership import CacheClient, WorkerDatabases


def test_agent_guide_contains_local_nonexecuting_repair_workflow():
    guide = files("ptest").joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")
    assert "Do not launch an agent" in guide
    assert "one database per worker per run" in guide
    assert "never use global flush" in guide
    assert guide.index("scoped `ptest` command") < guide.index("one `ptest --full` final gate")
    assert "static doctor currently has no validated per-test history timing\ninput" in guide
    assert "under 0.5 seconds is healthy" in guide
    assert "Exactly 2 seconds starts optimization" in guide
    assert "exactly 3\nseconds remains in that band" in guide
    assert "assessment authority only" in guide


def test_agent_guide_describes_doctor_v2_first():
    guide = files("ptest").joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")
    first = " ".join(guide.split("\n\n")[1].split())
    assert "one cheap-model call per checklist item" in first
    assert "--offline" in first and "static" in first


def test_repository_guide_is_short_accurate_and_owns_shared_guidance():
    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 45
    assert "repository root" in guide
    assert "ptest api/" in guide
    assert "Run `ptest --full` once after the integrated change" in guide
    assert "-n 0" in guide
    # Parallel-tier accuracy: qualified projects run workers in parallel;
    # init writes `-n 0` only into new configs for static fallbacks, never
    # rewrites an existing config, and `-n 0` is an opt-out, not the only
    # allowed xdist control.
    assert "run xdist in parallel under ptest" in guide
    assert "run serially with ptest's" in guide
    assert "writes `-n 0` into a new config only for static" in guide
    assert "never rewrites an existing config" in guide
    assert "only allowed xdist control" not in guide
    assert "Keep `-n N`, `--dist`, `--tx` out" in guide
    assert "opts out of the parallel tier" in guide
    assert "which ptest adds when the project enables xdist" not in guide
    assert "vitest run" in guide
    assert "one cheap-model call per checklist item" in guide
    assert "one database per worker per run" in guide
    assert "assessment authority only" in guide
    # The merge gate and graph refresh live only in the guide, not in skills.
    assert "graphify update ." in guide


@pytest.mark.parametrize("name, required", [
    ("databases", ("once per run or worker, not per test", "run/worker-owned", "neighbor database sentinel")),
    ("cache", ("run and worker identity", "Never call a global flush", "neighbor key")),
    ("files-ports", ("run/worker-owned", "OS-assigned ephemeral ports", "neighbor sentinel")),
    ("processes", ("foreground process group", "Do not\ndetach", "only owned descendants")),
    ("time-network", ("fake clocks", "local fakes", "scoped ptest")),
    ("factories", ("fresh test records", "per-test cleanup", "not permission to weaken a test")),
])
def test_every_recipe_is_loadable_package_data_with_its_ownership_guidance(name, required):
    # Package-content check only; execution behavior is tested below.
    content = files("ptest").joinpath("resources", "recipes", name + ".md").read_text(encoding="utf-8")
    assert all(phrase in content for phrase in required)


@pytest.mark.parametrize("global_cleanup", [True, False], ids=["bad-global", "owned-only"])
def test_database_cleanup_preserves_only_owned_namespaces(tmp_path, global_cleanup):
    databases = WorkerDatabases(tmp_path)
    identities = (("run-a", "w0"), ("run-a", "w1"), ("run-b", "w0"))
    paths = [databases.database(*identity) for identity in identities]
    for path in paths:
        with sqlite3.connect(path) as connection:
            connection.execute("insert into records values ('sentinel')")
    for path in paths:
        with sqlite3.connect(path) as connection:
            assert connection.execute("select value from records").fetchall() == [("sentinel",)]

    databases.cleanup("run-a", "w0", global_cleanup=global_cleanup)

    for index, path in enumerate(paths):
        with sqlite3.connect(path) as connection:
            expected = [] if global_cleanup or index == 0 else [("sentinel",)]
            assert connection.execute("select value from records").fetchall() == expected


def test_expensive_database_setup_runs_once_per_worker_and_run_and_each_test_is_clean(tmp_path):
    databases = WorkerDatabases(tmp_path)
    for identity in (("run-a", "w0"), ("run-a", "w1"), ("run-b", "w0")):
        for repetition in range(3):
            with databases.test_connection(*identity) as connection:
                assert connection.execute("select value from records").fetchall() == []
                connection.execute("insert into records values (?)", (str(repetition),))
                connection.commit()
                assert connection.execute("select value from records").fetchall() == [(str(repetition),)]
    assert databases.setup_calls == {("run-a", "w0"): 1, ("run-a", "w1"): 1, ("run-b", "w0"): 1}
    assert len(set(databases.paths.values())) == 3


@pytest.mark.parametrize("global_cleanup", [True, False], ids=["bad-global", "owned-only"])
def test_shared_cache_clients_observe_neighbor_loss_only_for_global_cleanup(global_cleanup):
    service = {}
    owner = CacheClient(service, "run-a", "w0")
    neighbors = [CacheClient(service, "run-a", "w1"), CacheClient(service, "run-b", "w0")]
    owner.put("key", "owned")
    for neighbor in neighbors:
        neighbor.put("key", "sentinel")
        assert neighbor.get("key") == "sentinel"
    owner.cleanup(global_cleanup=global_cleanup)
    assert owner.get("key") is None
    assert [neighbor.get("key") for neighbor in neighbors] == ([None, None] if global_cleanup else ["sentinel", "sentinel"])


@pytest.mark.parametrize("global_cleanup", [True, False], ids=["bad-global", "owned-only"])
def test_file_cleanup_has_a_neighbor_sentinel_before_teardown(tmp_path, global_cleanup):
    root = tmp_path / "runs"
    owner, neighbor = root / "run-a" / "w0", root / "run-b" / "w0"
    for path in (owner, neighbor):
        path.mkdir(parents=True)
        (path / "sentinel").write_text("retained")
        assert (path / "sentinel").read_text() == "retained"
    shutil.rmtree(root if global_cleanup else owner)
    assert not owner.exists()
    assert (neighbor / "sentinel").exists() is (not global_cleanup)
    if not global_cleanup:
        assert (neighbor / "sentinel").read_text() == "retained"


def test_shared_socket_port_collides_but_ephemeral_neighbor_survives_owned_close():
    with socket.socket() as neighbor, socket.socket() as owner, socket.socket() as conflicting:
        neighbor.bind(("127.0.0.1", 0))
        neighbor.listen()
        with pytest.raises(OSError) as collision:
            conflicting.bind(neighbor.getsockname())
        assert collision.value.errno == errno.EADDRINUSE
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        assert owner.getsockname() != neighbor.getsockname()
        owner.close()
        neighbor.settimeout(1)
        with socket.create_connection(neighbor.getsockname(), timeout=1) as client:
            client.sendall(b"neighbor-sentinel")
            accepted, _ = neighbor.accept()
            with accepted:
                accepted.settimeout(1)
                assert accepted.recv(17) == b"neighbor-sentinel"
