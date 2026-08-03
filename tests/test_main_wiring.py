"""What main() dispatches to, and with what command."""
import pytest

CONFIG_TOML = """
[defaults]
workers = 2
remote_scoped_min_files = 40
region = "us-central1"
gcp_project = "test-project"
bucket = "private-bucket"

[projects.fake]
root    = "{root}"
kind    = "pytest"
workers = 2
scoped  = "uv run pytest"
full    = "uv run pytest --cov-fail-under=85 tests"
backend = "cloudrun"
job     = "fake-job"
"""


@pytest.fixture
def wired(ptest, persea_shaped, monkeypatch, tmp_path):
    """main() with the runners replaced by recorders."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG_TOML.format(root=persea_shaped))
    monkeypatch.setattr(ptest, "CONFIG", cfg)
    monkeypatch.chdir(persea_shaped)
    monkeypatch.setattr(ptest, "ensure_node_deps", lambda *a, **k: 0)

    calls = {"remote": [], "remote_kwargs": [], "local": []}

    def fake_remote(pcfg, cfg_, project, root, cmd, **kw):
        calls["remote"].append(cmd)
        calls["remote_kwargs"].append(kw)
        return 0

    def fake_local(cmd, cwd, env):
        calls["local"].append(cmd)
        return 0

    monkeypatch.setattr(ptest, "run_cloudrun", fake_remote)
    monkeypatch.setattr(ptest, "run_local", fake_local)
    return ptest, calls


def test_big_path_goes_remote_with_container_parallelism(wired):
    ptest, calls = wired
    assert ptest.main(["tests/api"]) == 0
    assert len(calls["remote"]) == 1 and not calls["local"]
    cmd = calls["remote"][0]
    assert "-n auto" in cmd
    assert "tests/api" in cmd
    assert "--cov-fail-under" not in cmd, "a scoped run keeps no coverage gate"


def test_small_path_runs_local_at_the_cap(wired):
    ptest, calls = wired
    assert ptest.main(["tests/crawler"]) == 0
    assert len(calls["local"]) == 1 and not calls["remote"]
    assert "-n 2" in calls["local"][0]
    assert "-n auto" not in calls["local"][0]


def test_force_local_keeps_a_big_path_here(wired):
    ptest, calls = wired
    assert ptest.main(["--local", "tests/api"]) == 0
    assert len(calls["local"]) == 1 and not calls["remote"]
    assert "-n 2" in calls["local"][0]


def test_callers_own_flag_wins_over_container_parallelism(wired):
    ptest, calls = wired
    assert ptest.main(["tests/api", "-n", "0"]) == 0
    cmd = calls["remote"][0]
    assert cmd.index("-n auto") < cmd.index("-n 0"), "caller's flag must come last"


def test_busy_backend_refuses_with_75_and_runs_nothing(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: 75)
    assert ptest.main(["tests/api"]) == 75
    assert not calls["local"], "exit 75 means nothing ran"


def test_broken_backend_degrades_to_a_capped_local_run(wired, monkeypatch):
    ptest, calls = wired

    def broken_remote(pcfg, cfg_, project, root, cmd, **kw):
        calls["remote"].append(cmd)
        return None            # "broken" — fall back to local

    monkeypatch.setattr(ptest, "run_cloudrun", broken_remote)
    assert ptest.main(["tests/api"]) == 0
    assert len(calls["remote"]) == 1, "the remote path must have been attempted"
    assert "-n auto" in calls["remote"][0], "the remote attempt carried container parallelism"
    assert len(calls["local"]) == 1
    assert "-n 2" in calls["local"][0], "the fallback must be capped, not -n auto"
    assert "-n auto" not in calls["local"][0]


def test_remote_test_failure_is_returned_verbatim(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: 1)
    assert ptest.main(["tests/api"]) == 1
    assert not calls["local"]


def test_naming_the_whole_tree_is_still_refused_not_routed(wired):
    ptest, calls = wired
    assert ptest.main(["tests"]) == 2
    assert not calls["remote"] and not calls["local"]


def test_full_still_sends_the_full_command(wired):
    ptest, calls = wired
    assert ptest.main(["--full"]) == 0
    assert "--cov-fail-under=85" in calls["remote"][0]


def test_fresh_is_a_ptest_only_remote_benchmark_flag(wired):
    ptest, calls = wired

    assert ptest.main(["--full", "--fresh"]) == 0

    assert "--fresh" not in calls["remote"][0]
    assert calls["remote_kwargs"][0]["fresh"] is True


def test_fresh_is_rejected_when_the_selected_run_is_local(wired):
    ptest, calls = wired

    assert ptest.main(["--fresh", "tests/crawler"]) == 2

    assert not calls["remote"] and not calls["local"]


def test_where_reports_remote_cache_ttl_and_namespace(wired, capsys):
    ptest, _calls = wired

    assert ptest.main(["where"]) == 0

    output = capsys.readouterr().out
    assert "cache" in output and "3600s" in output
    assert "namespace v1" in output


def test_where_reports_cache_disabled_for_local_backend(wired, capsys):
    ptest, _calls = wired
    cfg = ptest.load_config()
    cfg["projects"]["fake"]["backend"] = "local"

    assert ptest.cmd_where(cfg, ptest.Path.cwd()) == 0

    assert "cache    off (local backend)" in capsys.readouterr().out


def test_doctor_reports_healthy_cache_coordination(wired, monkeypatch, capsys):
    ptest, _calls = wired

    class HealthyStore:
        def __init__(self, *args):
            pass

        def object_exists(self, path):
            assert path == "coord/v1/.ptest-health"
            return False

    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcsCoordination", HealthyStore)

    assert ptest.main(["doctor"]) == 0

    output = capsys.readouterr().out
    assert "cache       ✓ healthy" in output
    assert "namespace v1" in output


def test_doctor_marks_unavailable_cache_coordination_unhealthy(
    wired, monkeypatch, capsys
):
    ptest, _calls = wired

    class BrokenStore:
        def __init__(self, *args):
            pass

        def object_exists(self, path):
            raise ptest.CoordinationUnavailable("permission denied with secret details")

    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcsCoordination", BrokenStore)

    assert ptest.main(["doctor"]) == 1

    output = capsys.readouterr().out
    assert "cache       ✗ unavailable" in output
    assert "secret details" not in output
