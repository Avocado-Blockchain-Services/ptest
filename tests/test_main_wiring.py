"""What main() dispatches to, and with what command."""
import pytest

CONFIG_TOML = """
[defaults]
workers = 2
remote_scoped_min_files = 40

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

    calls = {"remote": [], "local": []}

    def fake_remote(pcfg, cfg_, project, root, cmd, **kw):
        calls["remote"].append(cmd)
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
