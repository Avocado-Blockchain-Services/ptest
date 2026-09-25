import subprocess
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from ptest import contracts as C
from support import git, init_git_repo


def snapshot(*args, **kwargs):
    """Unit boundary: emulate independently verified T11 native identity."""
    from ptest.source import snapshot as actual_snapshot
    if kwargs.get("pytest_full_outputs"):
        kwargs.pop("runtime_identity", None)
    else:
        kwargs.setdefault("runtime_identity", "a" * 64)
    return actual_snapshot(*args, **kwargs)


def test_static_snapshot_without_key_creates_no_state_and_cannot_narrow(case):
    from ptest.source import snapshot

    domain = case.domain()
    normal_sentinel = case.base / "normal-state-sentinel"
    normal_sentinel.write_text("do not read")
    result = snapshot(domain, case.config(), None, None)
    assert result.digest is None
    assert any(reason.code == "state-unavailable" for reason in result.limitations)
    assert not (domain.root / "input-hmac.key").exists()
    assert normal_sentinel.read_text() == "do not read"


def test_execution_key_creation_is_scoped_to_supplied_domain(case):
    from ptest.source import ensure_fingerprint_key

    first = case.domain()
    second = case.domain()
    ensure_fingerprint_key(first)
    assert (first.root / "input-hmac.key").is_file()
    assert not (second.root / "input-hmac.key").exists()


def test_snapshot_reads_git_nul_status_and_committed_base_union(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    config = _config(case, domain)
    ensure_fingerprint_key(domain)
    initial = git(root, "rev-parse", "HEAD")
    (root / "src" / "a.py").write_text("two\n")
    git(root, "add", "src/a.py"); git(root, "commit", "-m", "change")
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head=initial, input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory,
                              policy_digest=baseline.policy_digest, created_at=baseline.created_at)
    (root / "tests" / "test_a.py").write_text("dirty\n")
    (root / "untracked.py").write_text("u\n")
    result = snapshot(domain, config, baseline, initial)
    assert result.digest is not None
    assert {change.new for change in result.changes if change.new} >= {"src/a.py", "tests/test_a.py", "untracked.py"}


def test_snapshot_rejects_nonancestor_baseline_and_bad_base(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); config = _config(case, domain); ensure_fingerprint_key(domain)
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head="deadbeef", input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory, policy_digest=baseline.policy_digest,
                              created_at=baseline.created_at)
    assert snapshot(domain, config, baseline, None).digest is None
    assert snapshot(domain, config, None, "deadbeef").digest is None


def test_declared_environment_is_hmaced_without_exposing_value(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key
    from ptest import contracts as C

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = _config(case, domain)
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"), environment=("FIXTURE_TOKEN",))
    config = case.config(checkout=config.checkout, selection=policy)
    monkeypatch.setenv("FIXTURE_TOKEN", "first-secret")
    first = snapshot(domain, config, None, None)
    monkeypatch.setenv("FIXTURE_TOKEN", "second-secret")
    second = snapshot(domain, config, None, None)
    assert first.digest != second.digest
    assert "secret" not in repr(second)


def test_deleted_and_renamed_paths_are_reported_without_losing_snapshot(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    (root / "src" / "a.py").rename(root / "src" / "renamed.py")
    (root / "tests" / "test_a.py").unlink()
    git(root, "add", "-A")
    result = snapshot(domain, config, None, None)
    assert result.digest is not None
    assert any(change.kind == "renamed" for change in result.changes)
    assert any(change.kind == "deleted" for change in result.changes)


def test_ignored_generated_input_is_fingerprinted_when_declared(case):
    from ptest.source import ensure_fingerprint_key
    from ptest import contracts as C

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("generated/\n"); git(root, "add", ".gitignore"); git(root, "commit", "-m", "ignore")
    (root / "generated").mkdir(); (root / "generated" / "input.py").write_text("generated\n")
    original = _config(case, domain)
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"),
                               ignored_inputs=("generated",))
    result = snapshot(domain, case.config(checkout=original.checkout, selection=policy), None, None)
    assert result.digest is not None
    assert "generated/input.py" in {fingerprint.path for fingerprint in result.files}


def test_fingerprint_scan_envelopes_fail_closed(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key
    from ptest import source

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    monkeypatch.setattr(source, "_MAX_FILES", 1)
    assert snapshot(domain, config, None, None).digest is None
    monkeypatch.setattr(source, "_MAX_FILES", 100_000)
    monkeypatch.setattr(source, "_MAX_TOTAL_BYTES", 1)
    assert snapshot(domain, config, None, None).digest is None
    monkeypatch.setattr(source, "_MAX_TOTAL_BYTES", 512 * 1024 * 1024)
    ticks = iter((0.0, 11.0, 12.0))
    monkeypatch.setattr(source.time, "monotonic", lambda: next(ticks))
    assert snapshot(domain, config, None, None).digest is None


def test_mode_change_and_gitlink_fail_closed(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    (root / "src" / "a.py").chmod(0o755)
    assert any(change.new == "src/a.py" for change in snapshot(domain, config, None, None).changes)
    git(root, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",submodule")
    assert snapshot(domain, config, None, None).digest is None


def test_unmerged_conflict_and_shallow_baseline_fail_closed(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    initial = git(root, "rev-parse", "HEAD")
    main_branch = git(root, "branch", "--show-current")
    git(root, "checkout", "-b", "other")
    (root / "src" / "a.py").write_text("other\n"); git(root, "commit", "-am", "other")
    git(root, "checkout", main_branch)
    (root / "src" / "a.py").write_text("master\n"); git(root, "commit", "-am", "master")
    assert subprocess.run(("git", "-C", str(root), "merge", "other")).returncode != 0
    conflicted = snapshot(domain, config, None, None)
    assert conflicted.digest is None
    assert any(reason.code == "unknown-input" for reason in conflicted.limitations)
    git(root, "merge", "--abort")
    shallow = domain.root / "shallow"
    subprocess.run(("git", "clone", "--depth", "1", "file://" + str(root), str(shallow)), check=True, stdout=subprocess.PIPE)
    shallow_checkout = case.checkout(domain); object.__setattr__(shallow_checkout, "root", shallow)
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head=initial, input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory, policy_digest=baseline.policy_digest,
                              created_at=baseline.created_at)
    assert snapshot(domain, case.config(checkout=shallow_checkout), baseline, None).digest is None
def _repository(case):
    domain = case.domain()
    root = init_git_repo(
        domain.root / "repo",
        files={"src/a.py": "one\n", "tests/test_a.py": "pass\n"},
        message="initial")
    return domain, root


def _config(case, domain):
    checkout = case.checkout(domain)
    object.__setattr__(checkout, "root", domain.root / "repo")
    return case.config(checkout=checkout)


def _baseline(case, config, snap):
    return replace(case.history(with_baseline=True).baseline, head=snap.head,
                   input_digest=snap.digest, compatibility=snap.compatibility,
                   policy_digest=hashlib.sha256(repr(config.selection).encode()).hexdigest())


def test_undeclared_ignored_content_is_not_an_automatic_exemption(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("__pycache__/\n.venv/\n")
    git(root, "add", ".gitignore"); git(root, "commit", "-m", "ignore caches")
    config = _config(case, domain)
    before = snapshot(domain, config, None, None)
    (root / "src/__pycache__").mkdir()
    (root / "src/__pycache__/a.pyc").write_bytes(b"cache")
    (root / ".venv").mkdir()
    (root / ".venv/cache").write_bytes(b"cache")
    after = snapshot(domain, config, None, None)
    ignored = {change.new for change in after.changes if change.kind == "ignored"}
    assert not after.clean
    assert ignored == {".venv/cache", "src/__pycache__/a.pyc"}
    assert before.digest != after.digest
    assert ignored.issubset({item.path for item in after.files})


def test_pytest_full_snapshot_excludes_only_root_cache_and_bytecode(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    ensure_fingerprint_key(domain)
    config = _config(case, domain)
    before = snapshot(domain, config, None, None, pytest_full_outputs=True)
    (root / ".gitignore").write_text(".pytest_cache/\ntests/__pycache__/\n")
    git(root, "add", ".gitignore"); git(root, "commit", "-m", "ignore native outputs")
    (root / ".pytest_cache").mkdir(); (root / ".pytest_cache" / "CACHEDIR.TAG").write_text("cache")
    (root / "tests" / "__pycache__").mkdir(); (root / "tests" / "__pycache__" / "test_a.cpython-313-pytest-9.1.1.pyc").write_bytes(b"pyc")
    after = snapshot(domain, config, None, None, pytest_full_outputs=True)
    assert after.digest is not None
    assert ".pytest_cache/CACHEDIR.TAG" not in {item.path for item in after.files}
    assert "tests/__pycache__/test_a.cpython-313-pytest-9.1.1.pyc" not in {item.path for item in after.files}


def test_uv_environment_is_excluded_from_full_snapshot_without_ignored_bypass(case):
    """A real uv environment must not trigger symlink-loop traversal or hide other ignored input."""
    from ptest.config import init_project, resolve_config
    from ptest.contracts import InitOptions, RunnerKind
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n"
        "[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\nlocal-secret.txt\n", encoding="utf-8")
    git(root, "add", "pyproject.toml", "uv.lock", ".gitignore")
    git(root, "commit", "-m", "uv project")
    created = init_project(root, InitOptions(runner=RunnerKind.PYTEST,
                                             dry_run=False,
                                             reveal_command=False))
    assert created.target.is_file()
    config = resolve_config(root).config
    assert config is not None
    ensure_fingerprint_key(domain)
    subprocess.run(("uv", "venv", str(root / ".venv")), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (root / "local-secret.txt").write_text("must remain an input\n", encoding="utf-8")
    result = snapshot(domain, config, None, None, pytest_full_outputs=True)
    assert result.digest is not None
    paths = {item.path for item in result.files}
    assert not any(path == ".venv" or path.startswith(".venv/") for path in paths)
    assert "local-secret.txt" in paths


def test_generated_uv_config_full_candidate_launches_with_real_environment(case):
    """The generated config survives a real uv environment and reaches the native full candidate."""
    from ptest.config import init_project, resolve_config
    from ptest.contracts import InitOptions, RunnerKind

    domain, root = _repository(case)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n"
        "dependencies = ['pytest==9.1.1']\n"
        "[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\nlocal-secret.txt\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "def test_native_full():\n    assert True\n", encoding="utf-8")
    locked = subprocess.run(("uv", "lock"), cwd=root, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert locked.returncode == 0, locked.stderr.decode(errors="replace")
    git(root, "add", "pyproject.toml", "uv.lock", ".gitignore", "tests/test_a.py")
    git(root, "commit", "-m", "uv candidate")
    created = init_project(root, InitOptions(runner=RunnerKind.PYTEST,
                                             dry_run=False,
                                             reveal_command=False))
    git(root, "add", ".ptest.toml")
    git(root, "commit", "-m", "generated ptest config")
    subprocess.run(("uv", "venv", str(root / ".venv")), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert created.target.is_file()
    completed = case.invoke(domain, root, "--full", timeout=30)
    assert completed.code == 0, completed.stderr.decode(errors="replace")
    assert completed.result is not None
    assert completed.result["data"]["plan"]["execution"] == "full"


def test_pytest_full_snapshot_excludes_root_conftest_and_imported_src_bytecode(case):
    """Full output filtering follows source/module identity, not test roots."""
    from ptest import source as source_module
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    ensure_fingerprint_key(domain)
    config = _config(case, domain)
    (root / "conftest.py").write_text("VALUE = 1\n")
    (root / "src" / "__pycache__").mkdir(parents=True)
    (root / "src" / "helper.py").write_text("VALUE = 2\n")
    (root / "src" / "__pycache__" / "helper.cpython-313.pyc").write_bytes(b"pyc")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "conftest.cpython-313.pyc").write_bytes(b"pyc")
    git(root, "add", "conftest.py", "src/helper.py")
    git(root, "commit", "-m", "root conftest and imported module")
    (root / ".gitignore").write_text("__pycache__/\n**/__pycache__/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "ignore bytecode")

    result = source_module.snapshot(domain, config, None, None, pytest_full_outputs=True)

    paths = {item.path for item in result.files}
    assert "__pycache__/conftest.cpython-313.pyc" not in paths
    assert "src/__pycache__/helper.cpython-313.pyc" not in paths


def test_pytest_full_digest_domain_is_execution_only_and_separate(case):
    from ptest import source as source_module
    from ptest.source import ensure_fingerprint_key

    domain, _ = _repository(case)
    ensure_fingerprint_key(domain)
    config = _config(case, domain)
    normal = snapshot(domain, config, None, None)
    full = snapshot(domain, config, None, None, pytest_full_outputs=True)
    assert normal.digest is not None and full.digest is not None
    assert normal.digest != full.digest
    assert normal.compatibility is not None
    assert full.compatibility is None
    assert any("execution-only" in item.message for item in full.limitations)
    # Non-opt-in callers retain the Task 11D digest payload byte-for-byte;
    # only the explicit full-output domain receives the new tag.
    key = source_module._key(domain)
    assert key is not None
    identity = [(item.path, item.digest, item.mode, item.size) for item in normal.files]
    # The external HMAC is empty for this policy and is still part of the
    # established payload shape.  Build it through the same helper to avoid
    # exposing environment values in the assertion.
    expected = source_module._mac(key, [source_module._IDENTITY_PROTOCOL, identity,
                                       source_module._mac(key, [[], []])])
    assert normal.digest == expected


@pytest.mark.parametrize("path", [".pytest_cache/custom", "nested/.pytest_cache/CACHEDIR.TAG",
                                  "tests/__pycache__/sourceless.cpython-313.pyc"])
def test_pytest_full_snapshot_retains_nonstandard_cache_outputs(case, path):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    ensure_fingerprint_key(domain)
    config = _config(case, domain)
    (root / ".gitignore").write_text(".pytest_cache/\nnested/.pytest_cache/\ntests/__pycache__/\n")
    git(root, "add", ".gitignore"); git(root, "commit", "-m", "ignore caches")
    target = root / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b"output")
    result = snapshot(domain, config, None, None, pytest_full_outputs=True)
    assert path in {item.path for item in result.files}


def test_pytest_full_generated_allowlist_never_overrides_tracked_or_declared_inputs(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case)
    ensure_fingerprint_key(domain)
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "CACHEDIR.TAG").write_text("tracked cache")
    (root / "tests" / "__pycache__").mkdir()
    tracked_pyc = root / "tests" / "__pycache__" / "test_a.cpython-313.pyc"
    tracked_pyc.write_bytes(b"tracked bytecode")
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    declared_pyc = root / "generated" / "test.cpython-313.pyc"
    declared_pyc.write_bytes(b"declared bytecode")
    git(root, "add", ".")
    git(root, "commit", "-m", "track cache lookalikes")
    policy = replace(_config(case, domain).selection, ignored_inputs=("generated",))
    config = case.config(checkout=_config(case, domain).checkout, selection=policy)
    result = snapshot(domain, config, None, None, pytest_full_outputs=True)
    paths = {item.path for item in result.files}
    assert ".pytest_cache/CACHEDIR.TAG" in paths
    assert "tests/__pycache__/test_a.cpython-313.pyc" in paths
    assert "generated/test.cpython-313.pyc" in paths


@pytest.mark.parametrize("with_docs_change", [False, True])
def test_undeclared_ignored_runtime_input_forces_full(case, with_docs_change):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("local_settings.py\n")
    (root / "tests/other.py").write_text("pass\n")
    git(root, "add", ".gitignore", "tests/other.py")
    git(root, "commit", "-m", "selection fixture")
    config = replace(_config(case, domain), selection=C.SelectionPolicy(
        enabled=True, closed_inputs=True, input_roots=("src", "tests"), no_tests=("docs",),
        groups=(C.Group(name="core", sources=("src",), tests=("tests/test_a.py",)),)))
    before = snapshot(domain, config, None, None)
    baseline = replace(_baseline(case, config, before),
                       inventory=case.inventory(("tests/test_a.py", "tests/other.py")))
    (root / "src/local_settings.py").write_text("RUNTIME_FLAG = True\n")
    assert git(root, "check-ignore", "src/local_settings.py") == "src/local_settings.py"
    if with_docs_change:
        (root / "docs").mkdir()
        (root / "docs/readme.md").write_text("docs only\n")

    after = snapshot(domain, config, baseline, None)
    plan = choose_plan(config, after, C.HistoryView(baseline=baseline), case.request())

    assert any(change.kind == "ignored" and change.new == "src/local_settings.py"
               for change in after.changes)
    assert plan.execution == "full"


def test_raw_crlf_change_hidden_by_git_conversion_forces_full(case):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitattributes").write_text("*.txt text\n")
    (root / "src/golden.txt").write_bytes(b"expected\n")
    (root / "tests/other.py").write_text("pass\n")
    git(root, "add", ".gitattributes", "src/golden.txt", "tests/other.py")
    git(root, "commit", "-m", "conversion fixture")
    config = replace(_config(case, domain), selection=C.SelectionPolicy(
        enabled=True, closed_inputs=True, input_roots=("src", "tests"), no_tests=("docs",),
        groups=(C.Group(name="core", sources=("src",), tests=("tests/test_a.py",)),)))
    before = snapshot(domain, config, None, None)
    baseline = replace(_baseline(case, config, before),
                       inventory=case.inventory(("tests/test_a.py", "tests/other.py")))
    (root / "src/golden.txt").write_bytes(b"expected\r\n")
    (root / "docs").mkdir()
    (root / "docs/readme.md").write_text("docs only\n")
    assert subprocess.run(("git", "-C", str(root), "diff", "--quiet", "--", "src/golden.txt")).returncode == 0

    after = snapshot(domain, config, baseline, None)
    plan = choose_plan(config, after, C.HistoryView(baseline=baseline), case.request())

    assert any(change.kind == "raw" and change.new == "src/golden.txt"
               for change in after.changes)
    assert plan.execution == "full"


def test_snapshot_never_executes_configured_process_filter(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitattributes").write_text("*.dat filter=sentinel\n")
    (root / "src/input.dat").write_bytes(b"original\n")
    git(root, "add", ".gitattributes", "src/input.dat")
    git(root, "commit", "-m", "filter fixture")
    marker = domain.root / "filter-executed"
    driver = domain.root / "filter-driver"
    driver.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    driver.chmod(0o700)
    git(root, "config", "filter.sentinel.process", str(driver))
    git(root, "config", "filter.sentinel.required", "true")
    (root / "src/input.dat").write_bytes(b"changed\n")

    result = snapshot(domain, _config(case, domain), None, None)

    assert result.digest is None
    assert result.limitations
    assert not marker.exists()


def test_committed_delta_does_not_mark_worktree_dirty_and_newer_base_cannot_hide_it(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = _config(case, domain)
    baseline = _baseline(case, config, snapshot(domain, config, None, None))
    (root / "src/a.py").write_text("second\n")
    git(root, "commit", "-am", "second")
    result = snapshot(domain, config, baseline, "HEAD")
    assert result.clean
    assert any(change.new == "src/a.py" for change in result.changes)


@pytest.mark.parametrize("changed_path", ["docs/readme.md", "src/a.py"])
def test_environment_change_with_path_change_cannot_narrow(case, monkeypatch, changed_path):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = replace(_config(case, domain), selection=C.SelectionPolicy(
        enabled=True, closed_inputs=True, environment=("TASK8_TOKEN",), no_tests=("docs",),
        groups=(C.Group(name="core", sources=("src",), tests=("tests/test_a.py",)),)))
    (root / "tests/other.py").write_text("pass\n")
    git(root, "add", "tests/other.py"); git(root, "commit", "-m", "other test")
    monkeypatch.setenv("TASK8_TOKEN", "before")
    before = snapshot(domain, config, None, None)
    baseline = replace(_baseline(case, config, before), inventory=case.inventory(("tests/test_a.py", "tests/other.py")))
    (root / changed_path).parent.mkdir(exist_ok=True)
    (root / changed_path).write_text("edit\n")
    path_only = snapshot(domain, config, baseline, None)
    expected = "none" if changed_path.startswith("docs/") else "selected"
    assert choose_plan(config, path_only, C.HistoryView(baseline=baseline), case.request()).execution == expected
    monkeypatch.setenv("TASK8_TOKEN", "after")
    after = snapshot(domain, config, baseline, None)
    plan = choose_plan(config, after, C.HistoryView(baseline=baseline), case.request())
    assert before.compatibility and after.compatibility != before.compatibility
    assert plan.execution == "full" and plan.reasons[0].code == "incompatible-baseline"


def test_environment_absent_and_empty_are_distinct(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True, environment=("TASK8_TOKEN",)))
    monkeypatch.delenv("TASK8_TOKEN", raising=False)
    absent = snapshot(domain, config, None, None)
    monkeypatch.setenv("TASK8_TOKEN", "")
    assert snapshot(domain, config, None, None).digest != absent.digest


def test_same_environment_is_separated_by_domain_key(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key

    first, root = _repository(case); second = case.domain()
    ensure_fingerprint_key(first); ensure_fingerprint_key(second)
    config = replace(_config(case, first), selection=C.SelectionPolicy(enabled=True, closed_inputs=True, environment=("TASK8_TOKEN",)))
    monkeypatch.setenv("TASK8_TOKEN", "same-secret")
    one = snapshot(first, config, None, None)
    two = snapshot(second, config, None, None)
    assert one.digest and two.digest and one.digest != two.digest
    assert "same-secret" not in repr((one, two))


def test_mode_only_change_invalidates_input_digest(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = _config(case, domain)
    before = snapshot(domain, config, None, None)
    (root / "src/a.py").chmod(0o755)
    after = snapshot(domain, config, None, None)
    assert before.digest is not None and before.digest != after.digest


@pytest.mark.parametrize("staged", [False, True])
def test_mode_change_prevents_narrowing_even_with_mapped_change(case, staged):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        groups=(C.Group(name="core", sources=("src",), tests=("tests/test_a.py",)),)))
    baseline = replace(_baseline(case, config, snapshot(domain, config, None, None)),
                       inventory=case.inventory(("tests/test_a.py", "tests/other.py")))
    (root / "src/a.py").chmod(0o755)
    if staged:
        git(root, "add", "src/a.py")
    result = snapshot(domain, config, baseline, None)
    assert choose_plan(config, result, C.HistoryView(baseline=baseline), case.request()).execution == "full"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_runtime_edit_behind_docs_change(case, flag):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True, no_tests=("docs",)))
    baseline = _baseline(case, config, snapshot(domain, config, None, None))
    git(root, "update-index", flag, "src/a.py")
    (root / "src/a.py").write_text("hidden runtime change")
    (root / "docs").mkdir(); (root / "docs/readme.md").write_text("docs")
    result = snapshot(domain, config, baseline, None)
    assert choose_plan(config, result, C.HistoryView(baseline=baseline), case.request()).execution == "full"


def test_dd_unmerged_index_cannot_produce_usable_identity(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    oid = git(root, "rev-parse", "HEAD:src/a.py")
    git(root, "update-index", "--force-remove", "src/a.py")
    subprocess.run(("git", "-C", str(root), "update-index", "--index-info"),
                   input=f"100644 {oid} 1\tsrc/a.py\n".encode(), check=True)
    (root / "src/a.py").unlink()
    result = snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None
    assert any("conflict" in reason.message.lower() for reason in result.limitations)


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply"])
def test_in_progress_operation_with_clean_status_fails_closed(case, marker):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    target = root / ".git" / marker
    if marker.startswith("rebase-"):
        target.mkdir()
    else:
        target.write_text(git(root, "rev-parse", "HEAD") + "\n")
    assert snapshot(domain, _config(case, domain), None, None).digest is None


def test_git_environment_cannot_redirect_snapshot(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    other_domain, other = _repository(case)
    (other / "alien.py").write_text("wrong checkout")
    git(other, "add", "."); git(other, "commit", "-m", "alien")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    result = snapshot(domain, _config(case, domain), None, None)
    assert {item.path for item in result.files} == {"src/a.py", "tests/test_a.py"}


def test_snapshot_never_executes_repository_fsmonitor(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    hook = domain.root / "fsmonitor"
    marker = domain.root / "hook-executed"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '\\0'\n")
    hook.chmod(0o700)
    git(root, "config", "core.fsmonitor", str(hook))
    snapshot(domain, _config(case, domain), None, None)
    assert not marker.exists()


def test_runtime_identity_is_not_assumed_by_static_snapshot(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    result = snapshot(domain, _config(case, domain), None, None)
    assert result.digest is not None
    assert result.compatibility is None
    assert result.limitations


def test_real_divergent_commit_is_not_accepted_as_baseline_or_base(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = _config(case, domain)
    main = git(root, "branch", "--show-current")
    git(root, "checkout", "-b", "divergent")
    (root / "src/a.py").write_text("divergent\n"); git(root, "commit", "-am", "diverge")
    other = snapshot(domain, config, None, None)
    baseline = _baseline(case, config, other)
    git(root, "checkout", main)
    (root / "src/a.py").write_text("main\n"); git(root, "commit", "-am", "main")
    assert snapshot(domain, config, baseline, None).digest is None
    assert snapshot(domain, config, None, other.head).digest is None


def test_declared_non_input_ignored_output_does_not_change_identity(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("reports/\n")
    git(root, "add", ".gitignore"); git(root, "commit", "-m", "ignore declared output")
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        input_roots=("src", "tests"), non_input_outputs=("reports",)))
    before = snapshot(domain, config, None, None)
    (root / "reports").mkdir(); (root / "reports/result.xml").write_text("output")
    after = snapshot(domain, config, None, None)
    assert before.digest == after.digest and after.clean and after.changes == ()


def test_all_git_calls_share_one_snapshot_deadline(case, monkeypatch):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    tick = [0.0]
    original = source._git
    def elapsed_git(*args, **kwargs):
        result = original(*args, **kwargs)
        tick[0] += 0.6
        return result
    monkeypatch.setattr(source, "_git", elapsed_git)
    monkeypatch.setattr(source.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(source, "_TIMEOUT_S", 1.0)
    result = source.snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None
    assert any("limit" in reason.message or "deadline" in reason.message for reason in result.limitations)


def test_git_capture_bytes_are_bounded(case, monkeypatch):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    for i in range(20):
        (root / f"untracked-{i:02d}.py").write_text("x")
    monkeypatch.setattr(source, "_MAX_GIT_BYTES", 256, raising=False)
    result = source.snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None
    assert any("limit" in reason.message for reason in result.limitations)


def test_per_file_byte_limit_is_independent_of_total_limit(case, monkeypatch):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    monkeypatch.setattr(source, "_MAX_FILE_BYTES", 3)
    assert source.snapshot(domain, _config(case, domain), None, None).digest is None


def test_file_removed_during_read_returns_limitation_without_stat_race(case, monkeypatch):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    target = root / "src/a.py"
    inode = target.stat().st_ino
    read = os.read
    def disappearing_read(fd, size):
        data = read(fd, size)
        if os.fstat(fd).st_ino == inode and target.exists():
            target.unlink()
        return data
    monkeypatch.setattr(source.os, "read", disappearing_read)
    result = source.snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None and result.limitations


@pytest.mark.parametrize("scenario", json.loads((Path(__file__).parent / "fixtures/selection/revalidation.json").read_text()))
def test_queue_and_running_revalidation_identity_fixture(case, monkeypatch, scenario):
    """Produces T11 inputs; this is not a claim that execution is integrated."""
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        input_roots=("src", "tests"), environment=("TASK8_TOKEN",), non_input_outputs=("reports",)))
    monkeypatch.setenv("TASK8_TOKEN", "before")
    before = snapshot(domain, config, None, None)
    target = root / scenario["path"]
    target.parent.mkdir(exist_ok=True)
    if scenario["mutation"] == "mode":
        target.chmod(0o755)
    else:
        target.write_text("changed\n")
    if scenario["mutation"] == "environment":
        monkeypatch.setenv("TASK8_TOKEN", "after")
    after = snapshot(domain, config, None, None)
    assert before.digest and before.compatibility
    assert (after.digest != before.digest) is scenario["identity_changes"]


@pytest.mark.parametrize("influence", ["runtime", "platform", "interpreter", "protocol"])
def test_compatibility_binds_runtime_and_static_identity(case, monkeypatch, influence):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    config = _config(case, domain)
    before = snapshot(domain, config, None, None)
    runtime = "a" * 64
    if influence == "runtime":
        runtime = "b" * 64
    elif influence == "platform":
        monkeypatch.setattr(source.platform, "machine", lambda: "another-architecture")
    elif influence == "interpreter":
        monkeypatch.setattr(source.sys, "version", "different interpreter")
    else:
        monkeypatch.setattr(source, "_IDENTITY_PROTOCOL", "different protocol")
    after = snapshot(domain, config, None, None, runtime_identity=runtime)
    assert before.compatibility and after.compatibility and before.compatibility != after.compatibility


def test_pytest_worker_count_is_ptest_owned_compatibility_noise(case):
    from ptest import source

    domain, _ = _repository(case); source.ensure_fingerprint_key(domain)
    config = _config(case, domain)
    serial = snapshot(domain, config, None, None)
    declared_parallel = snapshot(
        domain, replace(config, runner=replace(config.runner, workers=8)), None, None)
    changed_runner = snapshot(
        domain, replace(config, runner=replace(config.runner, args=("--strict-markers",))),
        None, None)

    assert serial.compatibility == declared_parallel.compatibility
    assert serial.compatibility != changed_runner.compatibility


def test_declared_ignored_input_change_with_docs_forces_full(case):
    from ptest.source import ensure_fingerprint_key
    from ptest.selection import choose_plan

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("generated/\n")
    git(root, "add", ".gitignore"); git(root, "commit", "-m", "generated inputs")
    (root / "generated").mkdir(); (root / "generated/input.py").write_text("original\n")
    config = replace(_config(case, domain), selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        ignored_inputs=("generated",), no_tests=("docs",)))
    before = snapshot(domain, config, None, None)
    baseline = _baseline(case, config, before)
    assert choose_plan(config, snapshot(domain, config, baseline, None), C.HistoryView(baseline=baseline), case.request()).execution == "none"
    (root / "generated/input.py").write_text("changed\n")
    (root / "docs").mkdir(); (root / "docs/readme.md").write_text("docs\n")
    after = snapshot(domain, config, baseline, None)
    plan = choose_plan(config, after, C.HistoryView(baseline=baseline), case.request())
    assert plan.execution == "full" and plan.reasons[0].code == "incompatible-baseline"


def test_static_scan_uses_only_supplied_domain_and_does_not_refresh_index(case, monkeypatch):
    from ptest import source

    domain, root = _repository(case); source.ensure_fingerprint_key(domain)
    index = root / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    (root / "src/a.py").touch()
    read, validate = source.read_regular, source.validate_private_file
    def supplied_read(read_root, relative, limit):
        assert read_root == domain.root
        return read(read_root, relative, limit)
    def supplied_validate(path):
        assert path == domain.root / "input-hmac.key"
        return validate(path)
    monkeypatch.setattr(source, "read_regular", supplied_read)
    monkeypatch.setattr(source, "validate_private_file", supplied_validate)
    result = snapshot(domain, _config(case, domain), None, None)
    assert result.digest is not None
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_actual_sixteen_mib_file_limit_without_allocating_payload(case):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    with (root / "src/large.py").open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    result = snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None and result.limitations[0].code == "scan-limit"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "invalid-utf8"])
def test_unsafe_input_types_and_encodings_fail_closed(case, kind):
    from ptest.source import ensure_fingerprint_key

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    if kind == "symlink":
        (root / "src/unsafe").symlink_to(root / "src/a.py")
    elif kind == "fifo":
        # Git has no untracked FIFO inventory; exercise a known input replaced
        # by a FIFO, which must never block the bounded reader.
        (root / "src/a.py").unlink()
        os.mkfifo(root / "src/a.py")
    else:
        fd = os.open(os.fsencode(root) + b"/src/unsafe-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    result = snapshot(domain, _config(case, domain), None, None)
    assert result.digest is None and result.limitations
