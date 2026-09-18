"""Fresh native init previews and exclusive project configuration creation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ptest.config import init_project, resolve_config
from ptest.contracts import InitAction, InitOptions, Problem, RunnerKind


def _options(runner=None, *, dry_run=False, reveal_command=False):
    return InitOptions(runner=runner, dry_run=dry_run,
                       reveal_command=reveal_command)


def test_init_never_overwrites_existing(tmp_path):
    path = tmp_path / ".ptest.toml"
    original = b"version = 999\n# preserve me\n"
    path.write_bytes(original)

    result = init_project(tmp_path, _options(dry_run=False))

    assert result.action is InitAction.EXISTING
    assert result.exists is True
    assert path.read_bytes() == original


def test_existing_config_wins_even_for_dry_run(tmp_path):
    path = tmp_path / ".ptest.toml"
    original = b"version = 999\n# preserve me\n"
    path.write_bytes(original)

    result = init_project(tmp_path, _options(dry_run=True))

    assert result.action is InitAction.EXISTING
    assert result.exists is True
    assert path.read_bytes() == original


def test_absent_dry_run_is_preview_and_writes_nothing(tmp_path):
    result = init_project(tmp_path, _options(dry_run=True, runner=RunnerKind.PYTEST))

    assert result.action is InitAction.PREVIEW
    assert result.exists is False
    assert result.target == tmp_path / ".ptest.toml"
    assert not result.target.exists()
    assert result.config is not None
    assert result.config.runner_kind is RunnerKind.PYTEST


def test_init_creates_one_fresh_config_exclusively(tmp_path):
    result = init_project(tmp_path, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert result.exists is True
    assert result.target.read_bytes()
    resolution = resolve_config(tmp_path)
    assert resolution.config is not None
    assert resolution.config.runner.workers == 1
    assert resolution.config.selection.enabled is False
    assert len(resolution.config.project_id) == 32


def test_pytest_native_preview_is_static_and_does_not_copy_addopts(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--cov=secret --cov-fail-under=99"\n',
        encoding="utf-8",
    )
    sentinel = tmp_path / "execution-sentinel"
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    for name in ("conftest.py", "setup.py"):
        (tmp_path / name).write_text(payload, encoding="utf-8")

    def no_execution(*args, **kwargs):
        pytest.fail("static preview attempted process execution")

    monkeypatch.setattr(subprocess, "Popen", no_execution)
    monkeypatch.setattr(os, "system", no_execution)
    native_before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    result = init_project(tmp_path, _options(dry_run=True))

    assert result.config is not None
    assert result.config.runner_kind is RunnerKind.PYTEST
    assert result.config.commands[0].argument_count == 1
    assert result.config.commands[1].argument_count == 1
    assert not sentinel.exists()
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == native_before


def test_mixed_native_evidence_requires_explicit_runner(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^3.2.7"}}),
        encoding="utf-8",
    )

    with pytest.raises(Problem) as caught:
        init_project(tmp_path, _options(dry_run=True))
    assert caught.value.code == "invalid-config"


def test_command_init_cannot_invent_argv(tmp_path):
    with pytest.raises(Problem) as caught:
        init_project(tmp_path, _options(runner=RunnerKind.COMMAND,
                                        dry_run=True))
    assert caught.value.code == "command-required"


def test_native_lock_preview_declares_setup_without_running_it(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    result = init_project(tmp_path, _options(dry_run=True))

    assert result.config is not None
    assert result.config.setup_configured is True
    assert result.config.setup_network is True
    assert result.config.setup_lifecycle_scripts is True
    assert not (tmp_path / ".ptest.toml").exists()


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "create"])
@pytest.mark.parametrize(("name", "runner", "expected_kind", "expected_launcher",
                          "expected_setup_argv"), [
    pytest.param("uv.lock", RunnerKind.PYTEST, RunnerKind.PYTEST,
                 ("uv", "run", "--locked", "--no-sync", "python"),
                 ("uv", "sync", "--locked"), id="uv-lock"),
    pytest.param("package-lock.json", RunnerKind.VITEST, RunnerKind.VITEST,
                 ("node",), ("npm", "ci"), id="package-lock"),
    pytest.param("conftest.py", None, RunnerKind.PYTEST, ("python",), None,
                 id="conftest"),
    pytest.param("pytest.ini", None, RunnerKind.PYTEST, ("python",), None,
                 id="pytest-config"),
    pytest.param("vitest.config.ts", None, RunnerKind.VITEST, ("node",), None,
                 id="vitest-config"),
])
def test_large_presence_only_native_evidence_is_never_read(
    tmp_path, monkeypatch, dry_run, name, runner, expected_kind,
    expected_launcher, expected_setup_argv,
):
    import ptest.config as config_module

    evidence = tmp_path / name
    evidence.write_bytes(b"x" * (256 * 1024 + 1))
    reads = []
    read_regular = config_module.read_regular

    def checked_read(read_root, relative, limit):
        reads.append(relative)
        if relative == name:
            raise AssertionError("presence-only native evidence was read")
        return read_regular(read_root, relative, limit)

    # Sensitivity proof: this guard really does fail if the evidence is read.
    with pytest.raises(AssertionError, match="presence-only native evidence was read"):
        checked_read(tmp_path, name, 1)
    assert reads == [name]
    reads.clear()
    monkeypatch.setattr(config_module, "read_regular", checked_read)

    result = init_project(tmp_path, _options(runner=runner, dry_run=dry_run))

    assert result.action is (InitAction.PREVIEW if dry_run else InitAction.CREATED)
    assert result.config is not None
    assert result.config.runner_kind is expected_kind
    assert result.config.setup_configured is (expected_setup_argv is not None)
    assert name not in reads
    assert evidence.stat().st_size == 256 * 1024 + 1
    if dry_run:
        assert not result.target.exists()
    else:
        resolution = resolve_config(tmp_path)
        assert resolution.problem is None
        assert resolution.config is not None
        assert resolution.config.runner.kind is expected_kind
        assert resolution.config.runner.launcher == expected_launcher
        if expected_setup_argv is None:
            assert resolution.config.setup is None
        else:
            assert resolution.config.setup is not None
            assert resolution.config.setup.argv == expected_setup_argv
        assert name not in reads


def test_native_profiles_are_selected_without_execution(tmp_path):
    (tmp_path / "go.mod").write_text("module example.test\n", encoding="utf-8")
    result = init_project(tmp_path, _options(dry_run=True))
    assert result.config is not None
    assert result.config.runner_kind is RunnerKind.GO

    cargo = tmp_path / "cargo"
    cargo.mkdir()
    (cargo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    result = init_project(cargo, _options(dry_run=True))
    assert result.config is not None
    assert result.config.runner_kind is RunnerKind.CARGO
    (cargo / "go.mod").write_text("module example.test\n", encoding="utf-8")
    with pytest.raises(Problem):
        init_project(cargo, _options(dry_run=True))


@pytest.mark.parametrize("existing_venv", [False, True])
def test_uv_generated_config_remains_valid_with_real_python_symlink(tmp_path, existing_venv):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    binary = tmp_path / ".venv/bin/python"

    def create_venv_link():
        binary.parent.mkdir(parents=True)
        binary.symlink_to(sys.executable)

    if existing_venv:
        create_venv_link()
    created = init_project(tmp_path, _options())
    original = created.target.read_bytes()
    before = resolve_config(tmp_path)
    assert before.problem is None
    if not existing_venv:
        create_venv_link()

    after = resolve_config(tmp_path)

    assert binary.is_symlink()
    assert after.problem is None
    assert after.warnings == ()
    assert after.config == before.config
    assert after.config.setup.required_paths == (".venv/bin/python",)
    assert after.config.setup.argv == ("uv", "sync", "--locked")
    assert created.target.read_bytes() == original


@pytest.mark.parametrize("marker", ["directory", "worktree-file"])
def test_monorepo_subprojects_initialize_their_own_nearest_roots(tmp_path, marker):
    (tmp_path / ".ptest.toml").write_text("invalid parent must not apply\n")
    repo = tmp_path / "monorepo"
    api, web = repo / "api", repo / "web"
    api.mkdir(parents=True)
    web.mkdir()
    if marker == "directory":
        git = repo / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "config").write_text("[core]\nrepositoryformatversion = 0\n")
    else:
        (repo / ".git").write_text("gitdir: /unreadable/shared/metadata\n")
    (api / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (web / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "3.2.7"}}))

    results = []
    for project, kind in ((api, RunnerKind.PYTEST), (web, RunnerKind.VITEST)):
        preview = init_project(project, _options(dry_run=True))
        assert preview.target == project / ".ptest.toml"
        assert preview.config.runner_kind is kind
        assert not preview.target.exists()
        created = init_project(project, _options())
        assert created.action is InitAction.CREATED
        assert created.target == preview.target
        child = project / "tests/unit"
        child.mkdir(parents=True)
        resolved = resolve_config(child)
        assert resolved.problem is None
        assert resolved.root == project
        assert resolved.config.runner.kind is kind
        results.append(resolved.config.project_id)
    assert results[0] != results[1]
    assert not (repo / ".ptest.toml").exists()
    assert resolve_config(repo).problem.code == "initialization-required"


def test_vitest_native_preview_preserves_coverage_and_executes_nothing(tmp_path, monkeypatch):
    sentinel = tmp_path / "execution-sentinel"
    (tmp_path / "package.json").write_text(json.dumps({
        "devDependencies": {"vitest": "3.2.7"},
        "scripts": {"test": "vitest --coverage", "postinstall": "touch execution-sentinel"},
    }))
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (tmp_path / "vitest.config.cjs").write_text(
        f"require('node:fs').writeFileSync({json.dumps(str(sentinel))}, 'executed');\n"
        "module.exports = {test: {coverage: {enabled: true, thresholds: {lines: 99}}}};\n"
    )
    native_before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    def no_execution(*args, **kwargs):
        pytest.fail("native preview attempted process execution")

    monkeypatch.setattr(subprocess, "Popen", no_execution)
    monkeypatch.setattr(os, "system", no_execution)
    result = init_project(tmp_path, _options(dry_run=True))

    assert result.action is InitAction.PREVIEW
    assert result.config.runner_kind is RunnerKind.VITEST
    assert result.config.setup_configured is True
    assert result.config.setup_network is True
    assert result.config.setup_lifecycle_scripts is True
    assert not sentinel.exists()
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == native_before
    created = init_project(tmp_path, _options())
    config = resolve_config(tmp_path).config
    assert created.action is InitAction.CREATED
    assert config.runner.launcher == ("node",)
    assert config.runner.args == config.runner.full_args == ()
    assert config.setup.argv == ("npm", "ci")
    assert config.setup.required_paths == ("node_modules",)
    assert not sentinel.exists()
    assert all(path.read_bytes() == original for path, original in native_before.items())
    assert not (tmp_path / "node_modules").exists()


@pytest.mark.parametrize(("name", "runner"), [
    pytest.param("pyproject.toml", None, id="pyproject"),
    pytest.param("package.json", None, id="package-manifest"),
    pytest.param("uv.lock", RunnerKind.PYTEST, id="uv-lock"),
    pytest.param("package-lock.json", RunnerKind.VITEST, id="package-lock"),
    pytest.param("conftest.py", None, id="conftest"),
    pytest.param("pytest.ini", None, id="pytest-config"),
    pytest.param("vitest.config.ts", None, id="vitest-config"),
    pytest.param("tests", RunnerKind.PYTEST, id="test-root"),
])
def test_init_refuses_symlinked_native_inputs_without_reading_target(
    tmp_path, monkeypatch, name, runner,
):
    import ptest.config as config_module

    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("must not read this target")
    (root / name).symlink_to(outside)
    reads = []
    read_regular = config_module.read_regular

    def checked_read(read_root, relative, limit):
        reads.append(relative)
        assert relative != name
        return read_regular(read_root, relative, limit)

    monkeypatch.setattr(config_module, "read_regular", checked_read)
    with pytest.raises(Problem) as caught:
        init_project(root, _options(runner=runner, dry_run=True))
    assert caught.value.code == "unsafe-path"
    assert name not in reads
    assert not (root / ".ptest.toml").exists()
    assert outside.read_text() == "must not read this target"


@pytest.mark.parametrize("name", ["pyproject.toml", "package.json"])
@pytest.mark.parametrize("content", [b"\xff", b"x" * (256 * 1024 + 1)],
                         ids=["invalid-utf8", "oversized"])
def test_hostile_native_manifests_cannot_initialize_or_write(tmp_path, name, content):
    (tmp_path / name).write_bytes(content)
    with pytest.raises(Problem) as caught:
        init_project(tmp_path, _options())
    assert caught.value.code == "invalid-config"
    assert list(tmp_path.iterdir()) == [tmp_path / name]
    assert (tmp_path / name).read_bytes() == content


@pytest.mark.parametrize("dry_run", [False, True])
def test_init_preserves_existing_config_symlink_and_its_target(tmp_path, dry_run):
    target = tmp_path / "existing.toml"
    target.write_bytes(b"user-owned sentinel\n")
    link = tmp_path / ".ptest.toml"
    link.symlink_to(target)
    result = init_project(tmp_path, _options(dry_run=dry_run))
    assert result.action is InitAction.EXISTING
    assert result.config is None
    assert [warning.code for warning in result.warnings] == ["unsafe-path"]
    assert link.is_symlink()
    assert target.read_bytes() == b"user-owned sentinel\n"


def test_concurrent_init_exclusively_creates_one_config(tmp_path, monkeypatch):
    import ptest.config as config_module

    ready = Barrier(2, timeout=3)
    create_exclusive = config_module.create_exclusive

    def simultaneous_create(*args, **kwargs):
        ready.wait()
        return create_exclusive(*args, **kwargs)

    monkeypatch.setattr(config_module, "create_exclusive", simultaneous_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(init_project, tmp_path, _options(runner=RunnerKind.PYTEST))
                   for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert {result.action for result in results} == {InitAction.CREATED, InitAction.EXISTING}
    original = (tmp_path / ".ptest.toml").read_bytes()
    resolved = resolve_config(tmp_path)
    assert resolved.problem is None
    created = next(result for result in results if result.action is InitAction.CREATED)
    assert created.config.project_id == resolved.config.project_id
    assert init_project(tmp_path, _options()).action is InitAction.EXISTING
    assert (tmp_path / ".ptest.toml").read_bytes() == original
