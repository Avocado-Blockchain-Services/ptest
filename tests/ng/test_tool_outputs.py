"""Tool outputs no test reads, and doctor --fix for gitignored files (0.6.4).

Oracle: docs/specs/2026-10-12-tool-outputs.md (T1-T6). The trigger was the
0.6.3 reuse trial: specvm passed `ptest --full` twice and recorded no
baseline because `graphify-out/` (+55 files) counted as test inputs.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import config as config_api
from ptest import contracts as C
from ptest import doctor_fix, operations, progress, selection
from ptest.cli import main
from support import git, init_git_repo, write_ptest_toml

PROJECT_ID = "cd" * 16
_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# a cache directory\n"


def _policy(**fields) -> C.SelectionPolicy:
    return C.SelectionPolicy(enabled=True, closed_inputs=True, **fields)


# T1 / T2: the effective list


def test_builtin_outputs_apply_without_any_configuration():
    assert selection.non_input_outputs(_policy()) == (
        "graphify-out", ".ruff_cache", ".mypy_cache")


def test_declared_outputs_come_first_and_are_not_repeated():
    policy = _policy(non_input_outputs=(".venv", "graphify-out"))
    assert selection.non_input_outputs(policy) == (
        ".venv", "graphify-out", ".ruff_cache", ".mypy_cache")


@pytest.mark.parametrize("fields,test_roots,dropped", [
    ({"ignored_inputs": (".mypy_cache",)}, (), ".mypy_cache"),
    ({"ignored_inputs": ("graphify-out/graph.json",)}, (), "graphify-out"),
    ({"input_roots": ("graphify-out",)}, (), "graphify-out"),
    ({"full_triggers": (".ruff_cache/x",)}, (), ".ruff_cache"),
    ({"always": (".mypy_cache",)}, (), ".mypy_cache"),
    ({"no_tests": ("graphify-out",)}, (), "graphify-out"),
    ({"groups": (C.Group(name="g", sources=("graphify-out",), tests=("tests",)),)},
     (), "graphify-out"),
    ({}, (".ruff_cache",), ".ruff_cache"),
])
def test_a_config_entry_that_overlaps_a_builtin_wins(fields, test_roots, dropped):
    effective = selection.non_input_outputs(_policy(**fields), test_roots)
    assert dropped not in effective
    assert len(effective) == 2


def _repo(tmp_path: Path, *, gitignore: str, tail: str = "", files=None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    write_ptest_toml(root, kind="pytest", launcher=("python",), args=(),
                     project_id=PROJECT_ID, tail=tail)
    init_git_repo(root, files={
        ".gitignore": gitignore, "src/app.py": "x = 1\n",
        "tests/test_app.py": "def test_x():\n    pass\n", **(files or {}),
    }, message="base")
    return root


def _write(root: Path, rel: str, data: bytes = b"x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_a_builtin_never_makes_a_config_invalid(tmp_path):
    root = _repo(tmp_path, gitignore=".mypy_cache/\n",
                 tail='[selection]\nignored_inputs = [".mypy_cache"]\n')
    _write(root, ".mypy_cache/3.13/app.data.json")
    resolution = config_api.resolve_config(root)
    assert resolution.warnings == ()
    assert resolution.config.selection.ignored_inputs == (".mypy_cache",)
    assert resolution.config.selection.non_input_outputs == ()


# T1 / T3: the input snapshot


@pytest.fixture
def snap(case):
    """Snapshot of a fixture repo in a fresh domain with its own key."""
    from ptest.source import ensure_fingerprint_key, snapshot

    domain = case.domain()
    ensure_fingerprint_key(domain)

    def take(root: Path, policy: C.SelectionPolicy | None = None):
        checkout = case.checkout(domain)
        object.__setattr__(checkout, "root", root)
        config = case.config(checkout=checkout)
        if policy is not None:
            config = replace(config, selection=policy)
        return snapshot(domain, config, None, None, runtime_identity="a" * 64)

    return domain, take


def _domain_repo(domain, *, gitignore: str, files=None) -> Path:
    return init_git_repo(domain.root / "repo", files={
        ".gitignore": gitignore, "src/a.py": "one\n", "tests/test_a.py": "pass\n",
        **(files or {})}, message="initial")


def test_builtin_tool_outputs_leave_the_tree_clean(snap):
    domain, take = snap
    root = _domain_repo(domain, gitignore="graphify-out/\n.ruff_cache/\n.mypy_cache/\n")
    before = take(root)
    _write(root, "graphify-out/graph.json")
    _write(root, ".ruff_cache/0.9/123", _TAG)
    _write(root, ".mypy_cache/3.13/a.data.json")
    after = take(root)
    assert after.clean and after.changes == ()
    assert after.digest == before.digest


def test_a_tracked_file_under_a_builtin_output_is_still_an_input(snap):
    domain, take = snap
    root = _domain_repo(domain, gitignore="", files={"graphify-out/GRAPH_REPORT.md": "v1\n"})
    before = take(root)
    (root / "graphify-out/GRAPH_REPORT.md").write_text("v2\n")
    after = take(root)
    assert not after.clean
    assert [change.kind for change in after.changes] == ["modified"]
    assert after.digest != before.digest


def test_a_declared_ignored_input_overrides_the_builtin(snap):
    domain, take = snap
    root = _domain_repo(domain, gitignore=".mypy_cache/\n")
    policy = _policy(input_roots=("src", "tests"), ignored_inputs=(".mypy_cache",))
    _write(root, ".mypy_cache/a.json", b"one")
    before = take(root, policy)
    _write(root, ".mypy_cache/a.json", b"two")
    after = take(root, policy)
    assert after.clean
    assert ".mypy_cache/a.json" in {item.path for item in after.files}
    assert after.digest != before.digest


def test_a_nested_cache_below_the_project_root_is_not_exempt(snap):
    domain, take = snap
    root = _domain_repo(domain, gitignore=".mypy_cache/\n")
    _write(root, "src/.mypy_cache/a.json")
    after = take(root)
    assert not after.clean
    assert {change.new for change in after.changes if change.kind == "ignored"} == {
        "src/.mypy_cache/a.json"}


def test_a_symlinked_builtin_output_is_exempt_like_a_declared_one(snap, tmp_path):
    domain, take = snap
    root = _domain_repo(domain, gitignore="graphify-out\n")
    shared = tmp_path / "shared-graph"
    shared.mkdir()
    (root / "graphify-out").symlink_to(shared)
    assert take(root).clean


# T3: the static planner and the unmapped-change check


def _config_with(case, policy: C.SelectionPolicy) -> C.Config:
    return replace(case.config(), selection=policy)


def test_unmapped_change_check_skips_builtin_outputs_only_at_the_root(case):
    policy = _policy(groups=(C.Group(name="g", sources=("src",), tests=("tests",)),))
    config = _config_with(case, policy)

    def snapshot_of(path):
        return C.InputSnapshot(digest="11" * 32, compatibility="22" * 32, head="a" * 40,
                               clean=False, changes=(C.Change(old=None, new=path, kind="ignored"),))

    assert operations._unmapped_changed_path(config, snapshot_of("graphify-out/g.json")) is None
    assert operations._unmapped_changed_path(config, snapshot_of("x/graphify-out/g")) == "x/graphify-out/g"


def test_static_impact_treats_builtin_outputs_as_ignored():
    from ptest import impact

    assert impact._is_ignored("graphify-out/tool.py", _policy())
    assert not impact._is_ignored("graphify-out/tool.py", _policy(input_roots=("graphify-out",)))


# T5: doctor --fix drafts the rest


def _plan(root: Path, **kwargs) -> doctor_fix.FixPlan:
    return doctor_fix.plan_all(root, config_api.resolve_config(root), **kwargs)


def _selection_changes(plan: doctor_fix.FixPlan) -> dict:
    return {change.key: change.value for item in plan.files for change in item.changes
            if change.table == "selection"}


def _notes(plan: doctor_fix.FixPlan) -> str:
    return "\n".join(plan.notes)


_IGNORE = (".env\n.env.*\ncache_dir/\ncache_fake/\ncache_link/\ndata/\ngraphify-out/\n"
           ".hypothesis/\nlinked\n__pycache__/\n.pytest_cache/\n")


def _populate(root: Path, tmp_path: Path, *, env: bool = True) -> None:
    if env:
        _write(root, ".env", b"A=1\n")
        _write(root, ".env.local", b"B=2\n")
    _write(root, "cache_dir/CACHEDIR.TAG", _TAG)
    _write(root, "cache_dir/blob")
    _write(root, "cache_fake/CACHEDIR.TAG", b"Signature: not-the-real-one\n")
    _write(root, "cache_fake/blob")
    real_tag = tmp_path / "real.tag"
    real_tag.write_bytes(_TAG)
    (root / "cache_link").mkdir()
    (root / "cache_link/CACHEDIR.TAG").symlink_to(real_tag)
    _write(root, "cache_link/blob")
    _write(root, "data/fixture.json", b"{}")
    _write(root, "graphify-out/graph.json")
    _write(root, ".hypothesis/examples/ab/cd")


def test_fix_drafts_outputs_and_inputs_from_gitignored_files(tmp_path):
    root = _repo(tmp_path, gitignore=_IGNORE)
    _populate(root, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "linked").symlink_to(elsewhere)

    plan = _plan(root)
    changes = _selection_changes(plan)
    # Tagged caches are outputs; anything unknown, a fake or symlinked tag
    # included, stays a fingerprinted input. Built-ins (graphify-out) and
    # pytest's own byproducts (.hypothesis) need nothing. .env files are
    # the user's choice: named, never declared.
    assert changes == {
        "non_input_outputs": ("cache_dir",),
        "ignored_inputs": ("cache_fake", "cache_link", "data"),
    }
    notes = _notes(plan)
    assert "not declared: linked is a symlink" in notes
    assert "not declared: .env, .env.local — tests may read them" in notes
    assert "ptest --full --again" in notes


def test_fix_keeps_existing_entries_and_adds_after_them(tmp_path):
    root = _repo(tmp_path, gitignore=".venv/\nreports/\ndata/\n",
                 tail='[selection]\nnon_input_outputs = [".venv"]\nignored_inputs = ["reports"]\n')
    _write(root, ".venv/bin/python")
    _write(root, "reports/r.xml")
    _write(root, "data/fixture.json")
    assert _selection_changes(_plan(root)) == {"ignored_inputs": ("reports", "data")}


def test_fix_never_drafts_an_entry_that_would_invalidate_the_policy(tmp_path):
    root = _repo(tmp_path, gitignore="gen/\n",
                 tail='[selection]\ninput_roots = ["src", "tests", "gen"]\n')
    _write(root, "gen/CACHEDIR.TAG", _TAG)
    _write(root, "gen/blob")
    plan = _plan(root)
    assert _selection_changes(plan) == {}
    assert "gen" in _notes(plan)


def test_fix_never_declares_a_directory_that_also_holds_ptest_byproducts(tmp_path):
    """fullon2 integrations: a stale .pyc (its test was deleted) sits beside
    bytecode ptest already ignores. Declaring the whole __pycache__ would
    make that bytecode a fingerprinted input; only the stale file is drafted."""
    root = _repo(tmp_path, gitignore="__pycache__/\n")
    _write(root, "tests/__pycache__/test_app.cpython-313-pytest-9.1.1.pyc")
    _write(root, "tests/__pycache__/test_gone.cpython-313-pytest-9.1.1.pyc")
    assert _selection_changes(_plan(root)) == {
        "ignored_inputs": ("tests/__pycache__/test_gone.cpython-313-pytest-9.1.1.pyc",)}


def test_fix_says_when_it_could_not_check_gitignored_files(tmp_path, monkeypatch):
    """fullon2 web: .next exceeded the input scan's byte limit and doctor
    drafted nothing without saying so."""
    from ptest import source

    root = _repo(tmp_path, gitignore="data/\n")
    _write(root, "data/x")
    limited = C.InputSnapshot(digest=None, compatibility=None, head=None, clean=False,
                              limitations=(C.Reason(code="scan-limit",
                                  message="input fingerprint byte limit exceeded"),))
    monkeypatch.setattr(source, "listing_snapshot", lambda config: limited)
    plan = _plan(root)
    assert plan.files == ()
    assert "gitignored files not checked: input fingerprint byte limit exceeded" in _notes(plan)


def test_fix_has_nothing_to_draft_for_a_clean_tree(tmp_path):
    root = _repo(tmp_path, gitignore="graphify-out/\n")
    _write(root, "graphify-out/graph.json")
    assert _plan(root).files == ()


def test_the_run_start_notice_ignores_tree_dependent_drafts(tmp_path):
    root = _repo(tmp_path, gitignore="data/\n")
    _write(root, "data/fixture.json")
    assert _plan(root).change_count == 1
    assert _plan(root, tree_drafts=False).change_count == 0


def test_fix_makes_the_tree_clean_for_the_full_mode(tmp_path, monkeypatch, capsys):
    from ptest import source

    root = _repo(tmp_path, gitignore=_IGNORE)
    _populate(root, tmp_path, env=False)
    monkeypatch.chdir(root)
    assert main(("doctor", "--fix")) == 0
    out = capsys.readouterr().out
    assert "updated .ptest.toml" in out
    git(root, "commit", "-qam", "declare gitignored files")
    config = config_api.resolve_config(root).config
    after = source.listing_snapshot(config)
    assert after.clean, after.changes
    parsed = tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))
    assert parsed["selection"]["ignored_inputs"] == ["cache_fake", "cache_link", "data"]


def test_listing_snapshot_touches_no_ptest_state(tmp_path, monkeypatch):
    from ptest import source

    root = _repo(tmp_path, gitignore="data/\n")
    _write(root, "data/x")
    monkeypatch.setattr(source, "_key", lambda domain: pytest.fail("read a domain key"))
    first = source.listing_snapshot(config_api.resolve_config(root).config)
    second = source.listing_snapshot(config_api.resolve_config(root).config)
    assert [change.new for change in first.changes] == ["data/x"]
    assert first.digest != second.digest   # a throwaway key: proves nothing


# T6: the run names the fix


def test_the_full_gate_reason_names_doctor_fix():
    snapshot = C.InputSnapshot(
        digest="11" * 32, compatibility="22" * 32, head="a" * 40, clean=False,
        changes=(C.Change(old=None, new="logs/a.log", kind="ignored"),),
        files=(C.FileFingerprint(path="logs/a.log", digest="aa" * 32, mode=0o100644, size=1),))
    reason = operations.full_run_reason(None, snapshot, "dd" * 32)
    assert reason.startswith("gitignored paths count as test inputs: logs — run ptest doctor --fix")


def test_the_baseline_note_names_doctor_fix():
    before = C.InputSnapshot(digest="11" * 32, compatibility="22" * 32, head="a" * 40,
                             clean=False, changes=(C.Change(old=None, new=".env", kind="ignored"),))
    assert "run ptest doctor --fix" in progress._unclean_detail(before, before)


def test_the_policy_digest_is_unchanged_by_builtins():
    # T4: built-ins are not policy fields, so no baseline is invalidated.
    policy = _policy(input_roots=("src",))
    holder = type("Holder", (), {"selection": policy})()
    assert selection._policy_digest(holder) == hashlib.sha256(repr(policy).encode()).hexdigest()


def test_doctor_fix_then_a_repeat_full_is_already_verified(case, monkeypatch, capsys):
    """The 0.6.4 promise end to end: a reuse project with gitignored tool
    output and an unknown gitignored file records no baseline, doctor --fix
    declares the file, and the repeat --full is already verified."""
    from support import git_commit_all, write_file
    from test_reuse_opt_in import _full, _project, _verified

    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain)
    write_file(root / ".gitignore",
               "__pycache__/\n.pytest_cache/\ngraphify-out/\n.mypy_cache/\ndata/\n")
    git_commit_all(root, "ignore tool output")
    for rel in ("graphify-out/graph.json", ".mypy_cache/3.13/a.json", "data/x.json"):
        _write(root, rel)

    blocked = _full(case, domain, root)
    assert blocked.code == 0, blocked.stderr.decode()
    assert blocked.result["data"]["baseline_published"] is False
    assert b"data/x.json" in blocked.stderr and b"ptest doctor --fix" in blocked.stderr
    assert b"graphify-out" not in blocked.stderr and b".mypy_cache" not in blocked.stderr

    monkeypatch.chdir(root)
    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    git_commit_all(root, "declare gitignored files")

    recorded = _full(case, domain, root)
    assert recorded.code == 0, recorded.stderr.decode()
    assert recorded.result["data"]["baseline_published"] is True
    repeat = _full(case, domain, root)
    assert repeat.code == 0, repeat.stderr.decode()
    assert _verified(repeat), repeat.stderr.decode()
