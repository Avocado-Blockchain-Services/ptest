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
    assert "reuse expensive server/schema/template setup per run or worker" in guide
    assert "fresh lightweight SQLite database or mutable instance per test/use" in guide
    assert "never use global flush" in guide
    assert guide.index("scoped `ptest` command") < guide.index("one `ptest --full` final gate")
    assert "static doctor currently has no validated per-test history timing\ninput" in guide
    assert "under 0.5 seconds is healthy" in guide
    assert "Exactly 2 seconds starts optimization" in guide
    assert "exactly 3\nseconds remains in that band" in guide
    assert "assessment authority only" in guide
    assert "function by default" in guide
    assert "session only for expensive read-only" in guide
    assert "no mutable shared fixture state" in guide
    flat = " ".join(guide.split())
    assert ("See `ptest guide` recipes: factories, databases, cache, "
            "files-ports, processes, time-network." in flat)


def test_agent_guide_names_dynamic_selection_lines():
    guide = files("ptest").joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")
    assert "(dynamic ·" in guide
    assert "(static:" in guide
    assert "selection audit:" in guide
    flat = " ".join(guide.split())
    assert "dynamic or static, is iteration only" in flat


def test_agent_guide_describes_sampled_review_first():
    guide = files("ptest").joinpath("resources", "agent-guide.md").read_text(encoding="utf-8")
    first = " ".join(guide.split("\n\n")[1].split())
    assert "an initial call and one bounded independent verification" in first
    assert "--offline" in first and "static" in first


def test_repository_guide_is_short_structured_and_links_internals_out():
    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 100
    for section in ("## The loop", "## Monorepo",
                    "## Reading ptest output", "## Exit codes",
                    "## Test-quality rules", "## Reporting"):
        assert section in guide
    assert "repository root" in guide
    assert "ptest <project>/" in guide
    assert "api/" not in guide
    assert "ptest --full" in guide
    assert "--again" in guide
    assert "joined the running full run" in guide
    # Output table: every documented line is present.
    for row in ("→ N of M test files", "via importers",
                "(dynamic ·", "(static:",
                "changed → full suite:", "vitest changed delegation",
                "no tests affected", "tests reach these changes",
                "selection audit:", "nothing to test",
                "no changes under <folder>",
                "next: ptest --full before handoff",
                "<project> · no changes", "waiting for N slots",
                "setup failed", "passed · N tests",
                "incomplete (exit 70)",
                "protocol-mismatch", "ownership-uncertain",
                "execution-timeout", "queue-timeout",
                "post-test-stall",
                "unsafe-path", "unknown command"):
        assert row in guide, row
    # Exit-code table: every documented code is present.
    for code in ("| 0 |", "| 1 |", "| 2 |", "| 70 |", "| 75 |",
                 "| 124 |", "| 130 |"):
        assert code in guide, code
    flat = " ".join(guide.split())
    assert "one initial call per model-assessed item" in flat
    assert "one bounded verification for each valid reply" in flat
    assert "`ptest doctor --offline` is static and sends nothing" in flat
    assert "It uses requested models Codex `gpt-6-sol` and Claude `opus` by default" in flat
    assert "omitted decisive callers or failure paths remain unknown" in flat
    assert "Reuse expensive server/schema setup once per run or worker" in flat
    assert "fresh SQLite database or mutable instance per test/use" in flat
    assert "Namespace shared database records and external caches by overlapping owners" in flat
    assert "fresh per-test/per-use instance can own local cache state" in flat
    assert "never globally flush caches" in flat
    assert "assessment authority only" in guide
    # ptest-repo-internal workflow (merge gate, graph refresh) must never
    # ship to other people's repositories.
    assert "graphify" not in guide
    assert "fast-forward" not in guide
    # Low-level runner internals live in README/help, not the agent guide.
    assert "xdist" not in guide
    assert "cheap-model" not in guide
    # Tightened test-design guidance: fixture scope, ownership, recipes.
    assert "factories/builders for test records" in guide
    assert "function by default" in guide
    assert "session only for expensive read-only" in guide
    assert "no mutable shared fixture state" in guide
    assert "has an owner that cleans it up" in guide
    assert "above 0.5 seconds" in guide
    assert "at 2 seconds" in guide
    assert "above 3 seconds" in guide
    flat = " ".join(guide.split())
    assert ("See `ptest guide` recipes: factories, databases, cache, "
            "files-ports, processes, time-network." in flat)
    # Failure rows must never read as permission to weaken tests.
    assert ("never weaken, skip or delete tests or assertions to get green."
            in flat)
    assert "Changed tests under one folder" in guide
    assert "`ptest <folder>`, e.g. `ptest <project>/tests`" in guide
    assert "One test file (always runs it)" in guide
    assert "All tests under one folder" in guide
    assert "`ptest --full <folder>`" in guide
    # Dependency-recorded selection rows: a changed green, dynamic or
    # static, stays iteration-only.
    flat = " ".join(guide.split())
    assert "dynamic or static, is iteration only" in flat


def test_repository_guide_documents_post_test_stall():
    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert "post-test-stall" in guide
    flat = " ".join(guide.split())
    assert "tests finished" in flat
    assert "teardown/shutdown" in flat
    assert "exit 70" in flat
    assert "stack dump" in flat
    assert "Rerun once alone" in flat
    assert "never edit tests to dodge it" in flat
    assert "[runner] stall_timeout" in flat
    assert "default 120" in flat
    assert "0 disables" in flat
    assert len(guide.splitlines()) <= 100


def test_shipped_guides_never_mention_repo_internal_workflow():
    from importlib.resources import files

    for name in ("repository-agent-guide.md", "agent-guide.md"):
        guide = files("ptest").joinpath("resources", name).read_text(
            encoding="utf-8")
        assert "graphify" not in guide, name
        assert "fast-forward" not in guide, name
        assert "fast_forward" not in guide, name


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
