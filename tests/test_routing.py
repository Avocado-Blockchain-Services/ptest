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
