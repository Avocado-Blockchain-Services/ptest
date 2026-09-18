"""Fresh local configuration resolution and validation contracts."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptest.config import resolve_config
from ptest.contracts import RunnerKind


def _write_config(root: Path, *, project_id: str = "ab" * 16,
                  runner: str = "pytest", extra: str = "") -> Path:
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "\n[runner]\n"
        f'kind = "{runner}"\n'
        'launcher = ["python"]\n'
        'test_roots = ["tests"]\n'
        "\n[resources]\n"
        "\n[selection]\n"
        "enabled = false\n"
        f"{extra}",
        encoding="utf-8",
    )
    return path


def test_missing_config_is_initialization_required(tmp_path):
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.path is None
    assert resolution.problem is not None
    assert resolution.problem.code == "initialization-required"


def test_nearest_config_wins_for_nested_roots(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _write_config(outer, project_id="11" * 16)
    _write_config(inner, project_id="22" * 16)

    resolution = resolve_config(inner)

    assert resolution.root == inner
    assert resolution.path == inner / ".ptest.toml"
    assert resolution.config is not None
    assert resolution.config.project_id == "22" * 16


def test_parent_config_applies_without_git(tmp_path):
    root = tmp_path / "project"
    child = root / "src" / "package"
    child.mkdir(parents=True)
    _write_config(root, project_id="33" * 16)

    resolution = resolve_config(child)

    assert resolution.root == root
    assert resolution.config is not None
    assert resolution.config.project_id == "33" * 16


def test_resolution_ignores_legacy_environment_and_files(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _write_config(root, project_id="44" * 16)
    sentinel = tmp_path / "legacy-sentinel"
    sentinel.write_text("must not be read", encoding="utf-8")
    for name in ("PTEST_CONFIG", "PTEST_STATE_DIR", "PTEST_HOME",
                 "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(sentinel))

    resolution = resolve_config(root)

    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.project_id == "44" * 16
    assert resolution.warnings == ()
    assert sentinel.read_text(encoding="utf-8") == "must not be read"


@pytest.mark.parametrize("project_id", ["a" * 31, "a" * 33, "A" * 32,
                                         "z" * 32])
def test_project_id_must_be_lowercase_hex32(tmp_path, project_id):
    _write_config(tmp_path, project_id=project_id)

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"
    assert project_id not in resolution.problem.message


@pytest.mark.parametrize(
    "body",
    [
        'version = 1\nproject_id = "' + "ab" * 16 + '"\nversion = 1\n',
        'version = 1\nproject_id = "' + "ab" * 16 + '"\nunknown = true\n',
        'version = "1"\nproject_id = "' + "ab" * 16 + '"\n',
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n[runner]\nkind = "pytest"\nlauncher = ["python"]\ntest_roots = ["../tests"]\n',
    ],
)
def test_invalid_execution_config_fails_closed(tmp_path, body):
    (tmp_path / ".ptest.toml").write_text(body, encoding="utf-8")

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_invalid_selection_disables_only_selection(tmp_path):
    _write_config(
        tmp_path,
        extra='full_ratio = 0.01\n',
    )

    resolution = resolve_config(tmp_path)

    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.kind is RunnerKind.PYTEST
    assert resolution.config.selection.enabled is False
    assert [warning.code for warning in resolution.warnings] == ["policy-invalid"]


def test_invalid_utf8_and_oversized_config_fail_closed(tmp_path):
    path = tmp_path / ".ptest.toml"
    path.write_bytes(b"\xff")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"

    path.write_bytes(b"x" * (256 * 1024 + 1))
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_config_symlink_is_unsafe_and_not_followed(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".ptest.toml").symlink_to(outside)

    resolution = resolve_config(tmp_path)

    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "unsafe-path"


def test_literal_argv_accepts_metacharacters_but_rejects_controls(tmp_path):
    _write_config(tmp_path)
    path = tmp_path / ".ptest.toml"
    path.write_text(
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n'
        "[runner]\nkind = \"pytest\"\n"
        'launcher = ["python", "$(touch sentinel)", "a b"]\n'
        'test_roots = ["tests"]\n',
        encoding="utf-8",
    )
    resolution = resolve_config(tmp_path)
    assert resolution.config is not None
    assert resolution.config.runner.launcher[1] == "$(touch sentinel)"

    path.write_bytes(
        ('version = 1\nproject_id = "' + "ab" * 16 + '"\n'
         '[runner]\nkind = "pytest"\nlauncher = ["python\\u0001"]\n'
         'test_roots = ["tests"]\n').encode("utf-8")
    )
    resolution = resolve_config(tmp_path)
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"
    assert "python" not in resolution.problem.message
