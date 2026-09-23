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


def test_uv_init_marks_only_dependency_environment_as_non_input(tmp_path):
    """Generated uv projects ignore their tool environment, not arbitrary ignored files."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n"
        "[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    result = init_project(tmp_path, _options())

    assert result.action is InitAction.CREATED
    resolved = resolve_config(tmp_path)
    assert resolved.config is not None
    assert resolved.config.selection.non_input_outputs == (".venv",)
    assert resolved.config.selection.ignored_inputs == ()
    text = result.target.read_text(encoding="utf-8")
    assert 'non_input_outputs = [".venv"]' in text


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


def _child_root(tmp_path, name, runner_marker):
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / runner_marker).write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    return root


def test_monorepo_init_records_per_child_config_actions(tmp_path):
    api = _child_root(tmp_path, "api", "pyproject.toml")
    web = _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))

    result = init_project(tmp_path, options)

    assert result.action is InitAction.CREATED
    by_target = {item.target: item.action for item in result.details}
    assert by_target["api/.ptest.toml"] == "created"
    assert by_target["web/.ptest.toml"] == "created"
    assert all(item.source == "config" for item in result.details)
    assert (api / ".ptest.toml").is_file()
    assert (web / ".ptest.toml").is_file()


def test_monorepo_dry_run_reports_would_create_without_writing(tmp_path):
    _child_root(tmp_path, "api", "pyproject.toml")
    _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=True, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))

    result = init_project(tmp_path, options)

    assert result.action is InitAction.PREVIEW
    assert result.exists is False
    records = [item for item in result.details if item.action != "note"]
    assert {item.action for item in records} == {"would create"}
    assert [item.target for item in records] == [
        ".ptest.toml", "api/.ptest.toml", "web/.ptest.toml"]
    notes = [item for item in result.details if item.action == "note"]
    assert [(item.target, item.action, item.source) for item in notes] == [
        ("api · pytest · ready with caveats: "
         'ptest --full unavailable: test_roots is "."', "note", "config"),
        ("web · pytest · ready with caveats: "
         'ptest --full unavailable: test_roots is "."', "note", "config"),
    ]
    assert not (tmp_path / ".ptest.toml").exists()
    assert not (tmp_path / "api" / ".ptest.toml").exists()


def test_monorepo_repeat_init_reports_existing_children_truthfully(tmp_path):
    _child_root(tmp_path, "api", "pyproject.toml")
    _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))
    init_project(tmp_path, options)
    before = {path: path.read_bytes() for path in
              (tmp_path / ".ptest.toml", tmp_path / "api" / ".ptest.toml",
               tmp_path / "web" / ".ptest.toml")}

    repeat = init_project(tmp_path, options)

    assert repeat.action is InitAction.EXISTING
    by_target = {item.target: item.action for item in repeat.details}
    assert by_target[".ptest.toml"] == "already present"
    assert by_target["api/.ptest.toml"] == "already present"
    assert by_target["web/.ptest.toml"] == "already present"
    assert all(item.source == "config" for item in repeat.details)
    after = {path: path.read_bytes() for path in before}
    assert after == before


def test_monorepo_repeat_with_missing_child_reports_attention(tmp_path):
    from ptest.init_render import render_init

    _child_root(tmp_path, "api", "pyproject.toml")
    _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))
    init_project(tmp_path, options)
    (tmp_path / "web" / ".ptest.toml").unlink()

    repeat = init_project(tmp_path, options)

    assert repeat.action is InitAction.EXISTING
    by_target = {item.target: item.action for item in repeat.details}
    assert by_target[".ptest.toml"] == "already present"
    assert by_target["api/.ptest.toml"] == "already present"
    assert "web/.ptest.toml" not in by_target
    assert repeat.warnings != ()
    assert "ptest init needs attention" in render_init(repeat)


def test_monorepo_repeat_with_malformed_child_reports_attention(tmp_path):
    from ptest.init_render import render_init

    _child_root(tmp_path, "api", "pyproject.toml")
    _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))
    init_project(tmp_path, options)
    (tmp_path / "web" / ".ptest.toml").write_bytes(b"[[[ not toml\n")

    repeat = init_project(tmp_path, options)

    assert repeat.action is InitAction.EXISTING
    by_target = {item.target: item.action for item in repeat.details}
    assert by_target[".ptest.toml"] == "already present"
    assert by_target["api/.ptest.toml"] == "already present"
    assert "web/.ptest.toml" not in by_target
    assert repeat.warnings != ()
    assert "ptest init needs attention" in render_init(repeat)


def test_monorepo_repeat_with_unsafe_child_reports_attention(tmp_path):
    import shutil

    from ptest.init_render import render_init

    _child_root(tmp_path, "api", "pyproject.toml")
    web = _child_root(tmp_path, "web", "pyproject.toml")
    options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                          children=(("api", RunnerKind.PYTEST), ("web", RunnerKind.PYTEST)))
    init_project(tmp_path, options)
    outside = tmp_path.parent / "outside-unsafe-child"
    outside.mkdir(exist_ok=True)
    # A byte-identical, otherwise valid config outside the repository must
    # still report attention: the symlinked child directory is unsafe and
    # its target must never count as "already present".
    (outside / ".ptest.toml").write_bytes((web / ".ptest.toml").read_bytes())
    shutil.rmtree(web)
    web.symlink_to(outside, target_is_directory=True)

    repeat = init_project(tmp_path, options)

    assert repeat.action is InitAction.EXISTING
    by_target = {item.target: item.action for item in repeat.details}
    assert by_target[".ptest.toml"] == "already present"
    assert by_target["api/.ptest.toml"] == "already present"
    assert "web/.ptest.toml" not in by_target
    assert repeat.warnings != ()
    assert "ptest init needs attention" in render_init(repeat)
    assert web.is_symlink()


def test_child_state_rejects_repository_escape_declarations(tmp_path):
    from ptest.config import _child_state

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    created = init_project(seed, _options(runner=RunnerKind.PYTEST))
    assert created.action is InitAction.CREATED
    sibling = tmp_path.parent / "escape-sibling"
    sibling.mkdir(exist_ok=True)
    (sibling / ".ptest.toml").write_bytes((seed / ".ptest.toml").read_bytes())

    assert _child_state(tmp_path, "../escape-sibling") == "invalid"
    assert _child_state(tmp_path, "/etc") == "invalid"
    assert _child_state(tmp_path, "") == "invalid"


def test_single_init_details_carry_root_config_action(tmp_path):
    case_created = tmp_path / "case-created"
    case_created.mkdir()
    created = init_project(case_created, _options(runner=RunnerKind.PYTEST))

    assert [(item.target, item.action, item.source) for item in created.details] == [
        (".ptest.toml", "created", "config"),
        (". · pytest · ready with caveats: "
         'ptest --full unavailable: test_roots is "."', "note", "config"),
    ]
    case_preview = tmp_path / "case-preview"
    case_preview.mkdir()
    preview = init_project(case_preview,
                           _options(runner=RunnerKind.PYTEST, dry_run=True))
    assert [(item.target, item.action, item.source) for item in preview.details] == [
        (".ptest.toml", "would create", "config"),
        (". · pytest · ready with caveats: "
         'ptest --full unavailable: test_roots is "."', "note", "config"),
    ]
    assert not (case_preview / ".ptest.toml").exists()


def test_existing_invalid_config_keeps_warnings_for_attention_header(tmp_path):
    (tmp_path / ".ptest.toml").write_bytes(b"version = 999\n")

    result = init_project(tmp_path, _options(dry_run=False))

    assert result.action is InitAction.EXISTING
    assert result.warnings != ()
    assert result.details == ()


def test_standalone_xdist_init_reports_serial_caveat_and_run_notes(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n 4"\n', encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n")

    result = init_project(tmp_path, _options(runner=RunnerKind.PYTEST))

    assert result.action is InitAction.CREATED
    assert [(item.target, item.action, item.source) for item in result.details] == [
        (".ptest.toml", "created", "config"),
        (". · pytest · ready with caveats: "
         "serial: xdist disabled under ptest (-n 0)", "note", "config"),
        ("run: ptest tests/test_a.py", "note", "config"),
        ("run: ptest --full", "note", "config"),
    ]


def _write_child_config(path, kind, launcher, extra=""):
    (path / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        "[runner]\n"
        f'kind = "{kind}"\n'
        f"launcher = {launcher}\n"
        'args = []\n'
        'full_args = []\n'
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n' + extra,
        encoding="utf-8",
    )


def test_existing_persea_shaped_monorepo_reports_per_project_notes(tmp_path):
    api = tmp_path / "api"
    web = tmp_path / "web"
    (api / "tests").mkdir(parents=True)
    (web / "tests").mkdir(parents=True)
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8",
    )
    (api / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n'
        'addopts = \'-n 4 --dist=loadgroup -m "not slow"\'\n',
        encoding="utf-8",
    )
    (api / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8",
    )
    _write_child_config(api, "pytest", '["python"]')
    (web / "tests" / "a.test.ts").write_text(
        "export {};\n", encoding="utf-8")
    _write_child_config(
        web, "vitest", '["node"]',
        extra='[setup]\nargv = ["npm", "ci"]\nrequired_paths = ["node_modules"]\n'
              "network = true\nlifecycle_scripts = true\n",
    )

    result = init_project(tmp_path, _options(dry_run=False))

    assert result.action is InitAction.EXISTING
    assert [(item.target, item.action, item.source) for item in result.details] == [
        (".ptest.toml", "already present", "config"),
        ("api/.ptest.toml", "already present", "config"),
        ("web/.ptest.toml", "already present", "config"),
        ("api · pytest · not runnable: "
         "pytest addopts enable xdist, which ptest runs serially"
         ' — fix: add "-n", "0" to [runner] args in api/.ptest.toml',
         "note", "config"),
        ("web · vitest · ready with caveats: "
         "exclusive: Vitest runs as one command and manages its own workers; "
         "first run executes setup: npm ci", "note", "config"),
        ("run: ptest web/tests/a.test.ts", "note", "config"),
    ]


def test_monorepo_dry_run_marks_preexisting_children_already_present(tmp_path):
    api = _child_root(tmp_path, "api", "pyproject.toml")
    web = _child_root(tmp_path, "web", "pyproject.toml")
    create_options = InitOptions(runner=None, dry_run=False, reveal_command=False,
                                 children=(("api", RunnerKind.PYTEST),
                                           ("web", RunnerKind.PYTEST)))
    init_project(tmp_path, create_options)
    (tmp_path / ".ptest.toml").unlink()
    (api / ".ptest.toml").unlink()
    preview_options = InitOptions(runner=None, dry_run=True, reveal_command=False,
                                  children=(("api", RunnerKind.PYTEST),
                                            ("web", RunnerKind.PYTEST)))

    result = init_project(tmp_path, preview_options)

    assert result.action is InitAction.PREVIEW
    by_target = {item.target: item.action for item in result.details}
    assert by_target[".ptest.toml"] == "would create"
    assert by_target["api/.ptest.toml"] == "would create"
    assert by_target["web/.ptest.toml"] == "already present"
    assert not (tmp_path / ".ptest.toml").exists()
    assert not (api / ".ptest.toml").exists()


def _make_cli_init_repo(root):
    marker = root / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n', encoding="utf-8")


def test_tty_init_default_offer_runs_after_created_and_existing_init(
        tmp_path, monkeypatch, capsys):
    from ptest import cli

    _make_cli_init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def unqualified_status(name):
        assert (tmp_path / ".ptest.toml").is_file()
        return cli.agent_providers.QualificationStatus(
            name=name, qualified=False, argv=(name,),
            note="synthetic unqualified profile")

    monkeypatch.setattr(
        cli.agent_providers, "qualification_status", unqualified_status)
    monkeypatch.setattr(
        cli.agent_providers, "resolve_reviewer",
        lambda *a, **k: pytest.fail("resolved reviewer for unqualified offer"),
    )
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("prompted with no qualified reviewer"))
    monkeypatch.setattr(
        cli.doctor, "inspect_workspace",
        lambda *a, **k: pytest.fail("scanned source with no qualified reviewer"),
    )

    args = ("init", "--runner", "pytest", "--agents", "none")
    assert cli.main(args) == 0
    created = capsys.readouterr()
    assert "provider-unqualified" in created.err

    assert cli.main(args) == 0
    existing = capsys.readouterr()
    assert "provider-unqualified" in existing.err


@pytest.mark.parametrize("extra", [
    ("--no-doctor",),
    ("--json",),
    ("--dry-run",),
])
def test_tty_init_no_doctor_json_and_dry_run_never_offer_review(
        tmp_path, monkeypatch, capsys, extra):
    from ptest import cli

    _make_cli_init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("unexpected init prompt"))
    monkeypatch.setattr(
        cli.agent_providers, "resolve_reviewer",
        lambda *a, **k: pytest.fail("resolved reviewer for suppressed init offer"),
    )

    assert cli.main(("init", "--runner", "pytest", "--agents", "none", *extra)) == 0
    captured = capsys.readouterr()
    assert "review" not in captured.err.lower()


def test_tty_init_decline_is_success_for_existing_config_and_scans_offline(
        tmp_path, monkeypatch, capsys):
    from ptest import cli
    from ptest.agent_providers import QualificationStatus, ReviewerAdapter

    _make_cli_init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(("init", "--runner", "pytest", "--agents", "none")) == 0
    capsys.readouterr()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        cli.agent_providers, "qualification_status",
        lambda name: QualificationStatus(
            name=name, qualified=True, argv=(name,), note="synthetic"),
    )
    monkeypatch.setattr(
        cli.agent_providers, "resolve_reviewer",
        lambda name, env: ReviewerAdapter(
            "claude", "/fake/claude", ("/fake/claude",),
            qualified=True, qualification_note="test"),
    )
    prompts = []
    monkeypatch.setattr("builtins.input", lambda: prompts.append(1) or "no")
    inspected = []
    real_inspect = cli.doctor.inspect_workspace
    monkeypatch.setattr(
        cli.doctor, "inspect_workspace",
        lambda *a, **k: inspected.append(1) or real_inspect(*a, **k),
    )
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        lambda *a, **k: pytest.fail("declined init launched reviewer"),
    )

    assert cli.main(("init", "--runner", "pytest", "--agents", "none")) == 0
    captured = capsys.readouterr()
    assert prompts == [1]
    assert inspected == [1]
    assert "ptest doctor" in captured.out
    assert "review not yet performed" in captured.out


def test_tty_init_offer_with_two_installed_shows_menu_and_decline_keeps_files(
        tmp_path, monkeypatch, capsys):
    from ptest import cli
    from ptest.agent_providers import QualificationStatus, ReviewerAdapter

    _make_cli_init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        cli.agent_providers, "qualification_status",
        lambda name: QualificationStatus(
            name=name, qualified=name != "opencode", argv=(name,),
            note="synthetic"),
    )
    resolved = []

    def resolve(name, env):
        resolved.append(name)
        return ReviewerAdapter(
            name, f"/fake/{name}", (f"/fake/{name}",),
            qualified=True, qualification_note="test")

    monkeypatch.setattr(cli.agent_providers, "resolve_reviewer", resolve)
    inputs = []
    monkeypatch.setattr("builtins.input", lambda: inputs.append(1) or "")
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        lambda *a, **k: pytest.fail("menu decline launched reviewer"),
    )

    assert cli.main(("init", "--runner", "pytest", "--agents", "none")) == 0
    captured = capsys.readouterr()
    assert inputs == [1]
    assert resolved == ["claude", "codex"]
    assert "Choose a reviewer for this review:" in captured.err
    assert "1) claude" in captured.err
    assert "2) codex" in captured.err
    assert "opencode" not in captured.err
    assert "Run this review once?" not in captured.err
    assert "Optimization review is disabled" in captured.err
    assert (tmp_path / ".ptest.toml").is_file()
    assert not (tmp_path / "recommendations.md").exists()


def test_explicit_init_doctor_failure_preserves_initialized_files(
        case, tmp_path, monkeypatch, capsys):
    from ptest import cli
    from ptest.agent_providers import ProviderResult, QualificationStatus

    _make_cli_init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    statuses = []

    def qualified(name):
        statuses.append(name)
        return QualificationStatus(name=name, qualified=True,
                                   argv=(name,), note="synthetic")

    monkeypatch.setattr(cli.agent_providers, "qualification_status", qualified)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda: prompts.append(1) or "yes")
    launched = []

    def fail_launches(adapter, requests, timeout_s, *, concurrency=4,
                      on_done=None):
        for _request, _schema in requests:
            launched.append(adapter.name)
        return tuple(
            ProviderResult(
                provider=adapter.name, ok=False, assessment=b"",
                error="provider-failed", exit_code=7, timed_out=False,
                cancelled=False, truncated=False, pid=2001, argv=adapter.argv,
                scratch="/tmp/ptest-review-test")
            for _ in requests)

    monkeypatch.setattr(cli.agent_providers, "launch_reviews", fail_launches)

    domain = case.domain()
    assert cli.main(("--fixture-domain", str(domain.root),
                     "init", "--runner", "pytest", "--agents", "none",
                     "--doctor", "--reviewer", "claude",
                     "--allow-model-review")) == 2

    captured = capsys.readouterr()
    assert prompts == []
    assert "Model review disclosure: claude" in captured.err
    assert "Run this review once?" not in captured.err
    assert "initialization succeeded; review incomplete" in captured.err
    assert "provider-failed" in captured.err
    assert (tmp_path / ".ptest.toml").is_file()
    assert not (tmp_path / "recommendations.md").exists()
    assert statuses == ["claude", "claude"]
    assert launched and all(name == "claude" for name in launched)
