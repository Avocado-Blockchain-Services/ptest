"""Routing decisions: which invocations run where."""


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
