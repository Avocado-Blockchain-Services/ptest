"""Fresh native init previews and exclusive project configuration creation."""
from __future__ import annotations

import json
from pathlib import Path

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


def test_pytest_native_preview_is_static_and_does_not_copy_addopts(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--cov=secret --cov-fail-under=99"\n',
        encoding="utf-8",
    )
    sentinel = tmp_path / "execution-sentinel"
    (tmp_path / "conftest.py").write_text(
        "raise RuntimeError('should not execute')\n", encoding="utf-8"
    )

    result = init_project(tmp_path, _options(dry_run=True))

    assert result.config is not None
    assert result.config.runner_kind is RunnerKind.PYTEST
    assert result.config.commands[0].argument_count == 1
    assert result.config.commands[1].argument_count == 1
    assert not sentinel.exists()


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
