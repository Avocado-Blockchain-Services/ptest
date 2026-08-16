"""What main() dispatches to, and with what command."""
from types import SimpleNamespace

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
job     = "ptest-fake"
"""


@pytest.fixture
def wired(ptest, persea_shaped, monkeypatch, tmp_path):
    """main() with the runners replaced by recorders.

    The fixture's remote backend is Cloud Run because that is the one that is
    deployed. It used to be Spot, back when Spot was compulsory; these tests are
    about ROUTING — which invocation goes off-box, carrying which command — and
    were never about Spot itself. Spot keeps its own tests, and reaching it from
    here is now a failure, which is what pins the regression that sent
    local-backend projects into the Spot submitter.
    """
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

    def no_spot(*a, **kw):
        pytest.fail("Cloud Run work must never reach the Spot submitter")

    monkeypatch.setattr(ptest, "run_cloudrun", fake_remote)
    monkeypatch.setattr(ptest, "run_spot_queue", no_spot)
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


def test_force_local_cannot_keep_a_heavy_path_here(wired):
    ptest, calls = wired
    assert ptest.main(["--local", "tests/api"]) == 0
    assert not calls["local"] and len(calls["remote"]) == 1
    assert "-n auto" in calls["remote"][0]


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


def test_broken_backend_returns_75_without_a_local_fallback(wired, monkeypatch):
    ptest, calls = wired

    def broken_remote(pcfg, cfg_, project, root, cmd, **kw):
        calls["remote"].append(cmd)
        return None            # "broken" — fall back to local

    monkeypatch.setattr(ptest, "run_cloudrun", broken_remote)
    assert ptest.main(["tests/api"]) == 75
    assert len(calls["remote"]) == 1, "the remote path must have been attempted"
    assert "-n auto" in calls["remote"][0], "the remote attempt carried container parallelism"
    assert not calls["local"]


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


def test_force_local_full_bypasses_the_remote_and_caps_workers(wired, monkeypatch):
    ptest, calls = wired
    cfg = ptest.load_config()
    cfg["projects"]["fake"]["backend"] = "local"
    monkeypatch.setattr(ptest, "load_config", lambda: cfg)

    assert ptest.main(["--full", "--local"]) == 0
    assert not calls["remote"]
    assert len(calls["local"]) == 1
    assert "-n 2" in calls["local"][0]


def test_heavy_scope_goes_remote_even_with_force_local(wired, monkeypatch):
    ptest, calls = wired
    called = []

    def unavailable(*args, **kwargs):
        called.append((args, kwargs))
        return 75

    monkeypatch.setattr(ptest, "run_cloudrun", unavailable)

    assert ptest.main(["--local", "tests/api"]) == 75
    assert len(called) == 1
    assert not calls["local"]


def test_remote_none_result_is_converted_to_75_not_local(wired, monkeypatch):
    ptest, calls = wired
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: None)

    assert ptest.main(["--full"]) == 75
    assert not calls["local"]


def test_cloudrun_project_configuration_routes_compulsory_work_to_cloud_run(wired, monkeypatch):
    ptest, calls = wired
    cfg = ptest.load_config()
    cfg["projects"]["fake"]["backend"] = "cloudrun"
    monkeypatch.setattr(ptest, "load_config", lambda: cfg)
    cloudrun_calls = []
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **k: cloudrun_calls.append(a[4]) or 0)
    monkeypatch.setattr(ptest, "run_spot_queue", lambda *a, **k: pytest.fail("must not call Spot"))

    assert ptest.main(["--full"]) == 0
    assert cloudrun_calls == [cfg["projects"]["fake"]["full"]]
    assert not calls["local"]


def test_spot_queue_backend_routes_full_runs_and_keeps_fresh_out_of_the_command(
    wired, monkeypatch
):
    ptest, calls = wired
    cfg = ptest.load_config()
    # Spot is off by default; this test is specifically about the Spot path
    # still being wired up, so it throws the switch itself.
    cfg["projects"]["fake"].update({"backend": "spot_queue", "spot_topic": "ptest-spot",
                                    "spot_enabled": True})
    monkeypatch.setattr(ptest, "load_config", lambda: cfg)

    def run_spot_queue(pcfg, cfg_, project, root, cmd, **kwargs):
        calls["remote"].append(cmd)
        calls["remote_kwargs"].append(kwargs)
        return 0

    monkeypatch.setattr(ptest, "run_spot_queue", run_spot_queue, raising=False)

    assert ptest.main(["--full", "--fresh"]) == 0
    assert calls["remote"] == ["uv run pytest --cov-fail-under=85 tests"]
    assert calls["remote_kwargs"] == [{"what": "--full", "fresh": True}]


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
    assert "Cloud Run Jobs for --full and heavy scoped runs" in output


def test_where_reports_cache_disabled_for_local_backend(wired, capsys):
    ptest, _calls = wired
    cfg = ptest.load_config()
    cfg["projects"]["fake"]["backend"] = "local"

    assert ptest.cmd_where(cfg, ptest.Path.cwd()) == 0

    assert 'unavailable until backend = "cloudrun" is configured' in capsys.readouterr().out


def test_status_prints_live_spot_queue_counts(wired, monkeypatch, capsys):
    ptest, _calls = wired
    monkeypatch.setattr(ptest, "spot_status", lambda *_args: (3, 2, 4), raising=False)

    assert ptest.main(["spot-status"]) == 0

    assert capsys.readouterr().out == (
        "Spot queue\n"
        "  queued jobs    3\n"
        "  working jobs   2\n"
        "  servers on     4\n"
    )


def test_status_refuses_a_project_without_spot_queue(wired, monkeypatch, capsys):
    ptest, _calls = wired
    cfg = ptest.load_config()
    cfg["projects"]["fake"]["backend"] = "local"
    monkeypatch.setattr(ptest, "load_config", lambda: cfg)

    assert ptest.main(["spot-status"]) == 2

    assert "Spot queue is not configured" in capsys.readouterr().err


def test_result_prints_terminal_spot_summary(wired, monkeypatch, capsys):
    ptest, _calls = wired
    key = "a" * 64
    monkeypatch.setattr(
        ptest, "spot_result",
        lambda *_args: SimpleNamespace(status="failed", exit_code=1,
                                       completed_at="2026-08-04T22:05:22+00:00",
                                       output="failure details"),
        raising=False,
    )

    assert ptest.main(["spot-result", key]) == 0

    assert capsys.readouterr().out == (
        f"Spot result {key}\n"
        "  status       failed\n"
        "  exit code    1\n"
        "  completed at 2026-08-04T22:05:22+00:00\n"
    )


def test_result_output_is_raw_for_paging(wired, monkeypatch, capsys):
    ptest, _calls = wired
    monkeypatch.setattr(
        ptest, "spot_result",
        lambda *_args: SimpleNamespace(output="first line\nsecond line\n"),
        raising=False,
    )

    assert ptest.main(["spot-result", "a" * 64, "--output"]) == 0

    assert capsys.readouterr().out == "first line\nsecond line\n"


def test_result_requires_one_request_key(wired, capsys):
    ptest, _calls = wired

    assert ptest.main(["spot-result"]) == 2

    assert "usage: ptest spot-result <request-key> [--output]" in capsys.readouterr().err


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

    assert ptest.main(["doctor"]) == 1, "an unreachable cache is not a healthy doctor"

    output = capsys.readouterr().out
    assert "cache       ✗ unavailable" in output
    assert "permission denied with secret details" not in output, \
        "the backend's error detail must not leak into operator output"
