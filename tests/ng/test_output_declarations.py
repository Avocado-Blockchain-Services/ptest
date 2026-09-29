"""Declared non-input outputs may be symlinks; read inputs may not (0.4.0 E)."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from ptest import config as config_api
from ptest import contracts as C
from support import init_git_repo, write_ptest_toml


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    write_ptest_toml(root, kind="command", launcher=("true",), args=(), full_args=(),
                     project_id="ab" * 16, test_roots=("tests",))
    init_git_repo(root, files={
        ".gitignore": "logs/\nbuild/\n.env\n__pycache__/\n.pytest_cache/\n*.bin\n",
        "src/app.py": "x = 1\n", "tests/test_app.py": "def test_x():\n    pass\n",
    }, message="base")
    for name in ("logs/app.log", "build/out.o", "src/__pycache__/app.pyc",
                 ".pytest_cache/v", "tests/fixture.bin"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return root


def test_a_symlinked_env_can_be_declared_as_an_output(tmp_path):
    # Worktrees share .env as a symlink to the main checkout; an output is
    # never read, so its final component may be a symlink.
    root = _project(tmp_path)
    shared = tmp_path / "shared.env"
    shared.write_text("A=1\n", encoding="utf-8")
    (root / ".env").symlink_to(shared)
    toml = root / ".ptest.toml"
    toml.write_text(toml.read_text(encoding="utf-8")
                    + '\n[selection]\nnon_input_outputs = [".env"]\n', encoding="utf-8")
    resolution = config_api.resolve_config(root)
    assert resolution.warnings == ()
    assert resolution.config.selection.non_input_outputs == (".env",)


def test_a_symlinked_input_still_invalidates_the_policy(tmp_path):
    root = _project(tmp_path)
    shared = tmp_path / "shared.env"
    shared.write_text("A=1\n", encoding="utf-8")
    (root / ".env").symlink_to(shared)
    toml = root / ".ptest.toml"
    toml.write_text(toml.read_text(encoding="utf-8")
                    + '\n[selection]\nignored_inputs = [".env"]\n', encoding="utf-8")
    resolution = config_api.resolve_config(root)
    assert resolution.config.selection.ignored_inputs == ()
    assert any("invalid" in warning.message for warning in resolution.warnings)
