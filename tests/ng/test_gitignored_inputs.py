"""Gitignored files as inputs (ptest 0.7.4 G5, G6, G10).

Oracles: docs/specs/2026-11-20-ptest-0.7.4-fresh-worktree-visibility.md (G5,
G6, G10, abuse rows A11-A13), PEP 3147 (a ``__pycache__`` file is never
imported without its source), and GitHub issue #5 (one gitignored file over
16 MiB aborted a full run with exit 70).
"""
from __future__ import annotations

import itertools
import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import config as config_api
from ptest import contracts as C
from ptest import doctor_fix, history, progress, selection, source
from ptest.cli import main
from support import git, init_git_repo, write_ptest_toml

PROJECT_ID = "ef" * 16
_OVER_LIMIT = 17 * 1024 * 1024   # issue #5: 17 MB; the per-file limit is 16 MiB


def _write(root: Path, rel: str, data: bytes = b"x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sparse(root: Path, rel: str, size: int = _OVER_LIMIT) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


@pytest.fixture
def snap(case):
    """Snapshot of a fixture repo in a fresh domain with its own key."""
    domain = case.domain()
    source.ensure_fingerprint_key(domain)

    def take(root: Path, policy: C.SelectionPolicy | None = None):
        checkout = case.checkout(domain)
        object.__setattr__(checkout, "root", root)
        config = case.config(checkout=checkout)
        if policy is not None:
            config = replace(config, selection=policy)
        return source.snapshot(domain, config, None, None, runtime_identity="a" * 64)

    def listing(root: Path, policy: C.SelectionPolicy | None = None):
        checkout = case.checkout(domain)
        object.__setattr__(checkout, "root", root)
        config = case.config(checkout=checkout)
        if policy is not None:
            config = replace(config, selection=policy)
        return source.undeclared_gitignored(config)

    return domain, take, listing


def _domain_repo(domain, *, gitignore: str, files=None) -> Path:
    return init_git_repo(domain.root / "repo", files={
        ".gitignore": gitignore, "src/a.py": "one\n", "tests/test_a.py": "pass\n",
        **(files or {})}, message="initial")


def _ignored(snapshot: C.InputSnapshot) -> set[str]:
    return {change.new for change in snapshot.changes if change.kind == "ignored"}


# G5: orphan bytecode is never an input


def test_orphan_pycache_bytecode_is_not_an_input(snap):
    """Oracle: G5 / PEP 3147. Bytecode of a deleted module stays out of the
    snapshot: clean tree, no undeclared gitignored input, no fingerprint."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="__pycache__/\n")
    before = take(root)
    _write(root, "src/__pycache__/gone.cpython-313.pyc")
    _write(root, "src/__pycache__/gone.cpython-313.opt-1.pyo")
    _write(root, "tests/__pycache__/test_gone.cpython-313-pytest-9.1.1.pyc")
    after = take(root)
    assert after.clean and after.changes == ()
    assert not [item for item in after.files if "__pycache__" in item.path]
    assert after.digest == before.digest


def test_unignored_orphan_bytecode_is_not_an_untracked_input(snap):
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="")
    _write(root, "src/__pycache__/gone.cpython-313.pyc")
    after = take(root)
    assert after.clean and after.changes == ()


def test_legacy_sourceless_pyc_still_counts(snap):
    """Oracle: A11. A sourceless .pyc outside __pycache__ is importable, so
    it remains an undeclared gitignored input."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="*.pyc\n")
    _write(root, "src/legacy.pyc")
    after = take(root)
    assert not after.clean
    assert _ignored(after) == {"src/legacy.pyc"}


def test_tracked_bytecode_stays_an_input(snap):
    """G5 is about gitignored/untracked files; a tracked file is source."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="",
                        files={"src/__pycache__/t.cpython-313.pyc": "v1\n"})
    (root / "src/__pycache__/t.cpython-313.pyc").write_text("v2\n")
    after = take(root)
    assert not after.clean
    assert [change.kind for change in after.changes] == ["modified"]


_COMPONENTS = ("a", "__pycache__", "x__pycache__", "__pycache__x", ".", "b")
_NAMES = ("m.cpython-313.pyc", "m.pyo", "m.py", "m.pyc.txt", "pyc", "__pycache__")


def _expected_bytecode(parts: tuple[str, ...], name: str) -> bool:
    return "__pycache__" in parts and name.endswith((".pyc", ".pyo"))


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_bytecode_path_rule_over_generated_paths(depth):
    """Oracle: G5's rule as an invariant. hypothesis is not a dependency, so
    the paths are the full product of components and names, not a sample:
    a __pycache__ component plus a .pyc/.pyo suffix is bytecode; anything
    else is unchanged."""
    for parts in itertools.product(_COMPONENTS, repeat=depth):
        for name in _NAMES:
            path = "/".join((*parts, name))
            assert source.is_bytecode_path(path) is _expected_bytecode(parts, name), path


@pytest.mark.parametrize("entry,expected", [
    ("src/__pycache__/gone.cpython-313.pyc", True),
    ("__pycache__", True),
    ("build/__pycache__", True),
    ("build/__pycache__/", True),
    ("legacy.pyc", False),
    ("data/fixture.json", False),
    ("src/__pycache__x/file.pyc", False),
    ("src/__pycache__/notes.txt", False),
])
def test_bytecode_entry_shapes(entry, expected):
    assert source.is_bytecode_entry(entry) is expected


# G5: doctor --fix


_TOOL_IGNORE = "__pycache__/\n*.pyc\n"


def _repo(tmp_path: Path, *, gitignore: str, tail: str = "", files=None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    write_ptest_toml(root, kind="pytest", launcher=("python",), args=(),
                     project_id=PROJECT_ID, tail=tail)
    init_git_repo(root, files={
        ".gitignore": gitignore, "src/app.py": "x = 1\n",
        "tests/test_app.py": "def test_x():\n    pass\n", **(files or {})},
        message="base")
    return root


def _plan(root: Path, **kwargs) -> doctor_fix.FixPlan:
    return doctor_fix.plan_all(root, config_api.resolve_config(root), **kwargs)


def _selection_changes(plan: doctor_fix.FixPlan) -> dict:
    return {change.key: change.value for item in plan.files for change in item.changes
            if change.table == "selection"}


def _notes(plan: doctor_fix.FixPlan) -> str:
    return "\n".join(plan.notes)


def test_fix_drafts_nothing_for_orphan_bytecode(tmp_path):
    """Oracle: G5 / incident item 5: stale pyc files were drafted one by one."""
    root = _repo(tmp_path, gitignore=_TOOL_IGNORE)
    _write(root, "src/hosted_mcp/__pycache__/server.cpython-313.pyc")
    _write(root, "src/hosted_mcp/__pycache__/server.cpython-313.opt-2.pyc")
    plan = _plan(root)
    assert plan.files == ()


def test_bytecode_migration_removes_bytecode_entries_and_counts_them(tmp_path):
    """Oracle: G5, recognised by shape like the 0.7.2 helper migration."""
    entries = ('["src/__pycache__/gone.cpython-313.pyc", "build/__pycache__", '
               '"data/fixture.json"]')
    root = _repo(tmp_path, gitignore="data/\n",
                 tail=f"[selection]\nignored_inputs = {entries}\n")
    plan = _plan(root)
    assert _selection_changes(plan) == {"ignored_inputs": ("data/fixture.json",)}
    assert "removed 2 bytecode entries from ignored_inputs" in _notes(plan)


def test_bytecode_migration_applies_to_the_run_start_notice(tmp_path):
    """It is a config-shape rule: it does not need the working tree."""
    root = _repo(tmp_path, gitignore="",
                 tail='[selection]\nignored_inputs = ["a/__pycache__/x.cpython-313.pyc"]\n')
    assert _plan(root, tree_drafts=False).change_count == 1


def test_bytecode_migration_keeps_other_entries(tmp_path):
    """Oracle: A12. Nothing but bytecode-shaped entries is removed."""
    entries = ('["legacy.pyc", "data", "src/__pycache__x/file.pyc", '
               '"src/__pycache__/notes.txt"]')
    root = _repo(tmp_path, gitignore="",
                 tail=f"[selection]\nignored_inputs = {entries}\n")
    plan = _plan(root)
    # "src/__pycache__/notes.txt" is not bytecode, though it sits in a cache dir.
    assert plan.files == ()
    assert "bytecode entr" not in _notes(plan)


def test_bytecode_migration_writes_the_file(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, gitignore="data/\n", tail=(
        '[selection]\nignored_inputs = ["x/__pycache__/m.cpython-313.pyc", "data"]\n'))
    monkeypatch.chdir(root)
    assert main(("doctor", "--fix")) == 0
    out = capsys.readouterr().out
    parsed = tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))
    assert parsed["selection"]["ignored_inputs"] == ["data"]
    assert "removed 1 bytecode entry from ignored_inputs" in out


# G6: the names-only check


def test_names_only_listing_names_the_files_the_snapshot_refuses(snap):
    """Oracle: G6 / incident item 4. Doctor must not report clean while the
    gate refuses the baseline: same exclusions as the snapshot."""
    domain, take, listing = snap
    root = _domain_repo(domain, gitignore=(
        "build/\ndata/\n.env\n.pytest_cache/\n.hypothesis/\n__pycache__/\n*.pyc\n"
        "graphify-out/\n"))
    _write(root, "build/out.o")
    _write(root, "data/fixture.json")
    _write(root, "data/deep/er/x.bin")
    _write(root, ".env", b"SECRET=hunter2\n")
    _write(root, ".pytest_cache/CACHEDIR.TAG")
    _write(root, ".hypothesis/examples/ab")
    _write(root, "src/__pycache__/gone.cpython-313.pyc")
    _write(root, "graphify-out/graph.json")
    _write(root, "legacy.pyc")
    expected = _ignored(take(root))
    assert expected == {"build/out.o", "data/fixture.json", "data/deep/er/x.bin",
                        ".env", "legacy.pyc"}
    found = listing(root)
    assert set(found.paths) == expected
    assert not found.truncated


def test_names_only_listing_skips_declared_entries(snap):
    domain, take, listing = snap
    root = _domain_repo(domain, gitignore="build/\ndata/\nlogs/\n")
    for rel in ("build/out.o", "data/a", "data/b/c", "logs/x"):
        _write(root, rel)
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True,
                               input_roots=("src", "tests"),
                               ignored_inputs=("data",),
                               non_input_outputs=("build",))
    assert set(listing(root, policy).paths) == {"logs/x"}
    assert _ignored(take(root, policy)) == {"logs/x"}


def test_names_only_listing_prunes_dependency_installs_before_walking(snap, monkeypatch):
    """Oracle: G6. node_modules, .venv and __pycache__ are never walked; an
    undeclared install is named once, by its directory, since the gate
    counts it."""
    domain, _, listing = snap
    root = _domain_repo(domain, gitignore="node_modules/\n.venv/\n__pycache__/\nlogs/\n")
    for index in range(5):
        _write(root, f"node_modules/pkg/f{index}.js")
        _write(root, f".venv/lib/f{index}.py")
    _write(root, "logs/x")
    walked = []
    real_walk = os.walk
    monkeypatch.setattr(os, "walk", lambda top, *a, **k: (
        walked.append(str(top)) or real_walk(top, *a, **k)))
    found = listing(root)
    assert set(found.paths) == {"node_modules", ".venv", "logs/x"}
    assert not [top for top in walked if "node_modules" in top or ".venv" in top]
    declared = C.SelectionPolicy(enabled=True, closed_inputs=True,
                                 input_roots=("src", "tests"),
                                 non_input_outputs=(".venv", "node_modules"))
    assert set(listing(root, declared).paths) == {"logs/x"}


def test_names_only_listing_reads_no_content_and_no_clock(snap, monkeypatch):
    """Oracle: G6 / incident item 4. Same tree, same answer, however slow the
    clock: no hashing, and no wall-clock deadline."""
    domain, _, listing = snap
    root = _domain_repo(domain, gitignore="data/\n")
    for index in range(40):
        _write(root, f"data/sub{index % 4}/f{index}.bin")
    first = listing(root)
    ticks = itertools.count(0, 10_000)
    monkeypatch.setattr(source.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(source, "_fingerprints",
                        lambda *a, **k: pytest.fail("hashed file content"))
    second = listing(root)
    assert second == first
    assert len(first.paths) == 40


def test_names_only_listing_bound_is_an_entry_count(snap, monkeypatch):
    domain, _, listing = snap
    root = _domain_repo(domain, gitignore="data/\n")
    for index in range(10):
        _write(root, f"data/f{index:02d}")
    monkeypatch.setattr(source, "LISTING_ENTRY_LIMIT", 4)
    first = listing(root)
    assert first.truncated
    assert first.examined == 4
    assert first.paths == ("data/f00", "data/f01", "data/f02", "data/f03")
    assert listing(root) == first   # the same bound gives the same answer


def test_names_only_listing_is_unavailable_outside_git(tmp_path, case):
    checkout = case.checkout(case.domain())
    object.__setattr__(checkout, "root", tmp_path)
    assert source.undeclared_gitignored(case.config(checkout=checkout)) is None


def test_fix_says_when_the_listing_hit_its_bound(tmp_path, monkeypatch):
    root = _repo(tmp_path, gitignore="data/\n")
    for index in range(10):
        _write(root, f"data/f{index:02d}")
    monkeypatch.setattr(source, "LISTING_ENTRY_LIMIT", 4)
    notes = _notes(_plan(root))
    assert "gitignored files only partly checked: stopped after 4 directory entries" in notes


def test_fix_drafts_an_undeclared_dependency_install_as_an_output(tmp_path):
    root = _repo(tmp_path, gitignore=".venv/\nnode_modules/\n")
    _write(root, ".venv/bin/python")
    _write(root, "node_modules/pkg/index.js")
    assert _selection_changes(_plan(root)) == {
        "non_input_outputs": (".venv", "node_modules")}


# G6: doctor --offline reports it (SELECT-001 gap)


def _answers(root: Path, **declared):
    from ptest import deterministic_items as DI
    from factories_agents import (agent_domain, agent_packet_for,
                                  agent_resolution)

    config = replace(config_api.resolve_config(root).config,
                     config_path=root / ".ptest.toml")
    config = replace(config, selection=replace(
        config.selection, enabled=True, closed_inputs=True,
        input_roots=("src", "tests"), **declared))
    packet = agent_packet_for(root, None)
    return DI.answers_for(agent_domain(root), agent_resolution(root, config), packet)


def test_offline_doctor_reports_undeclared_gitignored_inputs_as_an_item(tmp_path):
    """Oracle: G6. A closed-selection project whose gate would refuse the
    baseline is a SELECT-001 gap naming the file, not 'ok'."""
    root = _repo(tmp_path, gitignore="build/\n")
    _write(root, "build/out.o")
    answer = _answers(root)["SELECT-001"]
    assert answer.status == "gap"
    assert "build" in answer.reason
    assert "1 gitignored file" in answer.finding_summary
    assert "non_input_outputs" in answer.finding_change
    assert C.aa_prose_is_untrusted(answer.finding_summary) is False
    assert C.aa_prose_is_untrusted(answer.finding_change) is False
    assert answer.reason == answer.finding_summary.rstrip(".")


def test_offline_item_is_satisfied_again_once_declared(tmp_path):
    root = _repo(tmp_path, gitignore="build/\n")
    _write(root, "build/out.o")
    assert _answers(root)["SELECT-001"].status == "gap"
    assert _answers(root, non_input_outputs=("build",))["SELECT-001"].status == "satisfied"


def test_offline_item_names_non_input_outputs_for_env_files(tmp_path):
    """Oracle: A13. A worktree's .env is a symlink; ignored_inputs refuses it,
    so the fix never recommends that table for .env files."""
    root = _repo(tmp_path, gitignore=".env\n")
    shared = tmp_path / "shared.env"
    shared.write_text("A=1\n")
    (root / ".env").symlink_to(shared)
    answer = _answers(root)["SELECT-001"]
    assert answer.status == "gap"
    assert ".env" in answer.reason
    assert "non_input_outputs" in answer.finding_change
    assert "or ignored_inputs for files a test reads" not in answer.finding_change
    assert "A=1" not in answer.reason + answer.finding_summary + answer.finding_change


def test_offline_item_prints_paths_made_of_plain_characters_only(tmp_path):
    """A hostile file name never reaches the prose; the count still covers it."""
    root = _repo(tmp_path, gitignore="*red*\n")
    _write(root, "$(rm -rf x) [31mred é")
    answer = _answers(root)["SELECT-001"]
    assert answer.status == "gap"
    prose = answer.reason + answer.finding_summary + answer.finding_change
    assert "$(" not in prose and "31m" not in prose
    assert "1 gitignored file" in answer.finding_summary


def test_offline_item_names_three_paths_and_counts_the_rest(tmp_path):
    root = _repo(tmp_path, gitignore="/a*\n")
    for name in ("a1", "a2", "a3", "a4", "a5"):
        _write(root, name)
    answer = _answers(root)["SELECT-001"]
    assert "a1, a2, a3, +2 more" in answer.finding_summary
    assert "5 gitignored files" in answer.finding_summary


def test_a_clean_project_keeps_its_satisfied_selection_answer(tmp_path):
    root = _repo(tmp_path, gitignore="graphify-out/\n__pycache__/\n")
    _write(root, "graphify-out/graph.json")
    _write(root, "src/__pycache__/gone.cpython-313.pyc")
    assert _answers(root)["SELECT-001"].status == "satisfied"


def test_a13_doctor_never_drafts_a_symlinked_env_into_ignored_inputs(tmp_path):
    """Oracle: A13. The note names non_input_outputs, the table that works."""
    root = _repo(tmp_path, gitignore=".env\n")
    shared = tmp_path / "shared.env"
    shared.write_text("A=1\n")
    (root / ".env").symlink_to(shared)
    plan = _plan(root)
    assert _selection_changes(plan) == {}
    assert "non_input_outputs" in _notes(plan)


# G10 / issue #5: a large gitignored file never aborts the run


def test_issue5_root_mypy_cache_over_the_limit_is_a_non_input(snap):
    """Oracle: GitHub issue #5, verbatim repro (root .mypy_cache, 17 MB)."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore=".venv/\n.mypy_cache/\n")
    _sparse(root, ".mypy_cache/3.12/cache.db")
    after = take(root)
    assert after.digest is not None and after.limitations == ()
    assert after.clean and after.changes == ()


def test_nested_over_limit_cache_is_an_undeclared_input_not_a_scan_limit(snap):
    """Oracle: G10. Outside the root built-ins the cache is an undeclared
    input: the snapshot stays available and the tree is unclean."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore=".mypy_cache/\n")
    _sparse(root, "svc/.mypy_cache/3.12/cache.db")
    after = take(root)
    assert after.digest is not None and after.limitations == ()
    assert not after.clean
    assert [(change.new, change.kind, change.size) for change in after.changes] == [
        ("svc/.mypy_cache/3.12/cache.db", "ignored", _OVER_LIMIT)]
    assert "svc/.mypy_cache/3.12/cache.db" not in {item.path for item in after.files}


def test_over_limit_undeclared_data_refuses_the_baseline_and_names_it(snap):
    """Oracle: G10. The refusal names the file; its size rides on the change."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="data/\n")
    _sparse(root, "data/blob.bin")
    before = take(root)
    after = take(root)
    assert after.digest is not None and not after.clean
    (change,) = after.changes
    assert (change.new, change.kind, change.size) == ("data/blob.bin", "ignored", _OVER_LIMIT)
    detail = progress._unclean_detail(before, after)
    assert "data/blob.bin" in detail


def test_over_limit_file_beside_small_ones_keeps_the_rest_fingerprinted(snap):
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="data/\n")
    _sparse(root, "data/blob.bin")
    _write(root, "data/small.json", b"{}")
    after = take(root)
    assert after.digest is not None
    assert {change.new: change.size for change in after.changes} == {
        "data/blob.bin": _OVER_LIMIT, "data/small.json": None}
    assert "data/small.json" in {item.path for item in after.files}
    assert "data/blob.bin" not in {item.path for item in after.files}


def test_over_limit_declared_ignored_input_is_not_fatal_either(snap):
    """A declared input too big to hash cannot be proven; the snapshot stays
    available and the baseline is refused."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="data/\n")
    _sparse(root, "data/blob.bin")
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True,
                               input_roots=("src", "tests"), ignored_inputs=("data",))
    after = take(root, policy)
    assert after.digest is not None and after.limitations == ()
    assert not after.clean


def test_tracked_file_over_the_limit_keeps_the_scan_limit(snap):
    """Oracle: G10 'tracked files keep today's limit and behaviour'."""
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="")
    _sparse(root, "src/huge.bin")
    git(root, "add", "src/huge.bin")
    git(root, "commit", "-qm", "huge")
    after = take(root)
    assert after.digest is None
    assert [(item.code, item.message) for item in after.limitations] == [
        ("scan-limit", "input fingerprint byte limit exceeded")]


def test_untracked_file_over_the_limit_keeps_the_scan_limit(snap):
    domain, take, _ = snap
    root = _domain_repo(domain, gitignore="")
    _sparse(root, "src/huge.bin")
    after = take(root)
    assert after.digest is None
    assert after.limitations[0].code == "scan-limit"


def test_change_size_is_never_persisted():
    """A stored snapshot keeps its old shape, so an older ptest reads it."""
    change = C.Change(old=None, new="data/blob.bin", kind="ignored", size=_OVER_LIMIT)
    stored = history._change_dict(change)
    assert set(stored) == {"old", "new", "kind"}
    assert C.Change(**stored) == C.Change(old=None, new="data/blob.bin", kind="ignored")


def test_change_size_must_be_a_bounded_non_negative_int():
    for bad in (-1, True, 1.5, "7"):
        with pytest.raises((TypeError, ValueError)):
            C.Change(old=None, new="x", kind="ignored", size=bad)
