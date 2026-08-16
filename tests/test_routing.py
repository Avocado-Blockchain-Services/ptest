"""Routing decisions: which invocations run where."""

import pytest


def test_all_test_files_finds_every_test_file(ptest, persea_shaped):
    assert len(ptest.all_test_files(persea_shaped)) == 221 + 61 + 23 + 3


def test_naming_the_whole_tree_is_still_a_refusal(ptest, persea_shaped, monkeypatch):
    """Pin existing behaviour before refactoring root_targeting_args."""
    monkeypatch.chdir(persea_shaped)
    hits = ptest.root_targeting_args(["tests"], persea_shaped, set())
    assert hits == ["tests"], "`ptest tests` must still be seen as the full suite"


def test_a_real_subdir_is_not_a_refusal(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.root_targeting_args(["tests/api"], persea_shaped, set()) == []


def test_path_like_args_keeps_bare_paths(ptest):
    assert ptest.path_like_args(["tests/api", "tests/db"]) == ["tests/api", "tests/db"]


def test_path_like_args_drops_flags(ptest):
    assert ptest.path_like_args(["-q", "--no-cov", "tests/api"]) == ["tests/api"]


def test_path_like_args_drops_a_flags_separate_value(ptest):
    assert ptest.path_like_args(["-k", "tests/api"]) == []
    assert ptest.path_like_args(["--rootdir", "tests", "tests/api"]) == ["tests/api"]
    assert ptest.path_like_args(["-n", "2", "tests/api"]) == ["tests/api"]


def test_path_like_args_keeps_inline_values_out_of_the_way(ptest):
    """`--cov=x` carries its own value, so the NEXT token is still a path."""
    assert ptest.path_like_args(["--cov=content_maker_api", "tests/api"]) == ["tests/api"]


def test_rootdir_value_is_no_longer_refused_as_the_whole_suite(ptest, persea_shaped,
                                                              monkeypatch):
    monkeypatch.chdir(persea_shaped)
    hits = ptest.root_targeting_args(["--rootdir", "tests", "tests/api"],
                                     persea_shaped, set())
    assert hits == [], "`tests` here is --rootdir's value, not a selected path"


def test_bare_invocation_with_flag_value_is_refused(ptest, persea_shaped, monkeypatch):
    """ptest --rootdir tests (value only, no path) must be refused as a disguised bare invocation."""
    monkeypatch.chdir(persea_shaped)
    # Create a pytest.ini to make this look like a valid test project
    (persea_shaped / "pytest.ini").write_text("[pytest]\n")
    monkeypatch.setattr(ptest, "run_local", lambda *a, **kw: 0)
    # When path_like_args returns empty, the bare-invocation check should fire
    assert ptest.path_like_args(["--rootdir", "tests"]) == []
    # Call main to verify it refuses
    rc = ptest.main(["--rootdir", "tests"])
    assert rc == 2, "bare invocation with flag value should be refused (exit 2)"


def test_flag_value_refusal_covers_all_value_flags(ptest, persea_shaped, monkeypatch):
    """Verify the fix covers not just --rootdir but all VALUE_FLAGS."""
    monkeypatch.chdir(persea_shaped)
    # Create a pytest.ini to make this look like a valid test project
    (persea_shaped / "pytest.ini").write_text("[pytest]\n")
    monkeypatch.setattr(ptest, "run_local", lambda *a, **kw: 0)
    # Test with -p (pytest's -p flag for plugins)
    assert ptest.path_like_args(["-p", "tests"]) == []
    rc = ptest.main(["-p", "tests"])
    assert rc == 2, "-p with value should be refused (exit 2)"


def test_narrowing_filter_overrides_bare_check(ptest, persea_shaped, monkeypatch):
    """ptest --rootdir tests -k foo should NOT be refused because -k is a narrowing filter."""
    monkeypatch.chdir(persea_shaped)
    # Create a pytest.ini to make this look like a valid test project
    (persea_shaped / "pytest.ini").write_text("[pytest]\n")
    monkeypatch.setattr(ptest, "run_local", lambda *a, **kw: 0)
    # This should NOT be refused because -k is a narrowing filter
    rc = ptest.main(["--rootdir", "tests", "-k", "foo"])
    assert rc == 0, "narrowing filter should override bare-invocation refusal (exit 0)"


def test_counts_files_under_a_directory_arg(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/api"], persea_shaped) == 221
    assert ptest.heavy_scoped_files(["tests/db"], persea_shaped) == 61
    assert ptest.heavy_scoped_files(["tests/unit"], persea_shaped) == 3


def test_counts_a_single_file_arg_as_one(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/api/test_a0.py"], persea_shaped) == 1


def test_two_args_are_a_union_not_a_sum(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    both = ptest.heavy_scoped_files(["tests/api", "tests/api/test_a0.py"], persea_shaped)
    assert both == 221, "the file is already inside the directory"
    assert ptest.heavy_scoped_files(["tests/api", "tests/db"], persea_shaped) == 282


def test_a_nonexistent_path_counts_zero(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["tests/typo"], persea_shaped) == 0


def test_no_path_args_counts_zero(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files(["-q", "--no-cov"], persea_shaped) == 0


def test_absolute_paths_are_counted(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.heavy_scoped_files([str(persea_shaped / "tests/db")], persea_shaped) == 61


def test_resolves_against_root_not_cwd(ptest, persea_shaped, monkeypatch):
    """cd tests && ptest api must NOT be read as tests/api.

    A run always executes in root (run_local's cwd, and the container
    entrypoint's cd), so `api` has to resolve against root — where it does not
    exist — not against cwd, where `tests/api` would make it look like 221
    files. Every other test in this file chdirs to root, so root / a == cwd / a
    there and none of them can catch a regression to the cwd-inclusive form.
    """
    monkeypatch.chdir(persea_shaped / "tests")
    assert ptest.heavy_scoped_files(["api"], persea_shaped) == 0


CLOUD = {"backend": "cloudrun"}
LOCAL = {"backend": "local"}
CFG = {"defaults": {"remote_scoped_min_files": 40}}


def test_big_directory_routes_remote(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, n = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, CFG, False)
    assert (route, n) == (True, 221)


def test_small_directory_stays_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, n = ptest.should_route_remote(["tests/crawler"], persea_shaped, CLOUD, CFG, False)
    assert (route, n) == (False, 23)


def test_single_file_stays_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api/test_a0.py"], persea_shaped,
                                         CLOUD, CFG, False)
    assert route is False


def test_a_narrowing_filter_keeps_a_big_path_local(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api", "-k", "foo"], persea_shaped,
                                         CLOUD, CFG, False)
    assert route is False, "a filter means the caller wants the fast loop"


def test_force_local_cannot_bypass_compulsory_heavy_spot(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, CFG, True)
    assert route is True


def test_a_local_backend_keeps_heavy_scoped_runs_here(ptest, persea_shaped, monkeypatch):
    """Replaces the compulsory-Spot rule, which is retired.

    While Spot was compulsory, a heavy scoped run routed remote regardless of
    the project's backend — correct then, because every project was supposed to
    end up on Spot. With Spot off, a `local` project has nowhere to route to,
    and pretending otherwise is how `--full` started failing with exit 2.
    """
    monkeypatch.chdir(persea_shaped)
    route, n = ptest.should_route_remote(["tests/api"], persea_shaped, LOCAL, CFG, False)
    assert (route, n) == (False, 0)


def test_an_unregistered_project_keeps_heavy_scoped_runs_here(ptest, persea_shaped,
                                                              monkeypatch):
    monkeypatch.chdir(persea_shaped)
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, {}, CFG, False)
    assert route is False, "no stanza means no remote backend to route to"


def test_full_on_a_local_backend_runs_here(ptest, persea_shaped, monkeypatch):
    """`ptest --full` on a local project must run, capped — not exit 2."""
    monkeypatch.chdir(persea_shaped)
    monkeypatch.setattr(ptest, "resolve_project", lambda cfg, cwd: (
        "fake", {"backend": "local", "kind": "pytest", "full": "pytest tests"}))

    def no_remote(*a, **kw):
        pytest.fail("a local backend must never submit remotely")

    ran = {}

    def fake_local(cmd, root, env):
        ran["cmd"] = cmd
        return 0

    monkeypatch.setattr(ptest, "run_remote_backend", no_remote)
    monkeypatch.setattr(ptest, "run_local", fake_local)
    assert ptest.main(["--full"]) == 0
    assert "pytest" in ran["cmd"]


def test_threshold_zero_disables_the_tier(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    cfg = {"defaults": {"remote_scoped_min_files": 0}}
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, CLOUD, cfg, False)
    assert route is False


def test_threshold_boundary_is_inclusive(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    at = {"defaults": {"remote_scoped_min_files": 61}}
    below = {"defaults": {"remote_scoped_min_files": 62}}
    assert ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, at, False)[0]
    assert not ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, below, False)[0]


def test_project_threshold_overrides_the_default(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    pcfg = {"backend": "cloudrun", "remote_scoped_min_files": 500}
    route, _ = ptest.should_route_remote(["tests/api"], persea_shaped, pcfg, CFG, False)
    assert route is False


def test_default_threshold_when_config_is_silent(ptest, persea_shaped, monkeypatch):
    monkeypatch.chdir(persea_shaped)
    assert ptest.should_route_remote(["tests/db"], persea_shaped, CLOUD, {}, False)[0]
    assert not ptest.should_route_remote(["tests/crawler"], persea_shaped, CLOUD, {}, False)[0]


def test_only_pytest_needs_an_explicit_parallel_flag(ptest):
    assert ptest.remote_worker_flags("pytest") == "-n auto"
    for kind in ("vitest", "npm", "go", "cargo", "unknown"):
        assert ptest.remote_worker_flags(kind) == ""
