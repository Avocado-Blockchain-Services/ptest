"""`ptest init` and change selection: fresh configs enable it with no
coverage or baseline step; existing configs point at `ptest doctor --fix`.
Also pins the shipped guide/skill/help/README default loop.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


def _pytest_repo(root: Path, venv_stub, *, cov_pair: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\ndependencies = []\n', encoding="utf-8")
    if cov_pair:
        venv_stub(root, pytest_cov="7.1.0", coverage="7.15.0")
    return root


def _init_argv(*extra: str) -> tuple[str, ...]:
    return ("init", "--runner", "pytest", "--agents", "none",
            "--no-doctor", "--no-smoke", *extra)


def _read_config(root: Path) -> dict:
    return tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))


def test_fresh_init_enables_selection_without_coverage(tmp_path, monkeypatch, capsys, venv_stub):
    """0.3 selection is import-graph based: init adds no --cov, asks nothing."""
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv()) == 0

    config = _read_config(tmp_path)
    assert not any(arg.startswith("--cov") for arg in config["runner"]["args"])
    assert config["selection"]["enabled"] is True
    captured = capsys.readouterr()
    assert "pytest-cov" not in captured.out + captured.err
    assert "baseline" not in captured.out + captured.err


def test_existing_config_with_selection_off_points_to_doctor_fix(
        tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    assert main(_init_argv()) == 0
    raw = (tmp_path / ".ptest.toml").read_text(encoding="utf-8")
    assert raw.count("enabled = true") == 1
    (tmp_path / ".ptest.toml").write_text(
        raw.replace("enabled = true", "enabled = false"), encoding="utf-8")
    before = (tmp_path / ".ptest.toml").read_bytes()
    capsys.readouterr()

    assert main(_init_argv()) == 0

    assert (tmp_path / ".ptest.toml").read_bytes() == before
    assert "selection is off; run `ptest doctor --fix`" in capsys.readouterr().out


def test_changed_setup_flag_is_gone(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later")) == 2


def test_repository_guide_default_loop_is_changed():
    from importlib.resources import files

    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 100
    assert "| After each edit | bare `ptest` with NO project name: it runs only the tests your change reaches |" in guide
    assert "| Changed tests under one folder | `ptest <folder>`, e.g. `ptest <project>/tests` |" in guide
    assert "| One test file (always runs it) | `ptest <file>`, e.g. `ptest <project>/tests/test_x.py` |" in guide
    assert "| All tests under one folder | `ptest --full <folder>` |" in guide
    assert "| Before handoff or merge | bring the base branch in first, then `ptest --full`; fix and rerun until green |" in guide
    lowered = guide.lower()
    assert "no baseline or coverage needed" not in lowered
    assert "baseline" not in lowered
    assert "coverage" not in lowered
    assert "--changed" not in guide
    assert "--changed-setup" not in guide
    assert "automatic" not in lowered
    assert "api/" not in guide


def test_skill_template_defaults_to_changed():
    import ptest.agent_rules as rules_module

    text = rules_module._provider_text("claude").decode("utf-8")
    assert "After each edit run bare `ptest` (no project name): it runs only the tests your change reaches." in text
    assert "| `ptest` | changed tests, whole repo |" in text
    assert "| `ptest <folder>` | changed tests under that folder |" in text
    assert "| `ptest <file>` | that file, always |" in text
    assert "| `ptest --full <folder>` | all tests under that folder |" in text
    assert "| `ptest --full` | integrated gate, once before handoff |" in text
    assert "docs/ptest-agent.md" in text


def test_init_then_uninstall_recognises_new_guide_and_skill(tmp_path, monkeypatch, venv_stub):
    from ptest.cli import main
    import ptest.agent_rules as rules_module

    _pytest_repo(tmp_path, venv_stub, cov_pair=False)
    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "claude",
                 "--no-doctor", "--no-smoke")) == 0

    guide = tmp_path / "docs" / "ptest-agent.md"
    assert guide.read_bytes() == rules_module._guide()

    from ptest import uninstall as uninstall_api

    assert uninstall_api._is_managed_guide(guide.read_bytes()) is True
    guide_entry = uninstall_api._decide_guide(guide.read_bytes())
    assert guide_entry is not None and guide_entry.action == uninstall_api.REMOVE


def test_init_upgrades_previous_skill_in_place(tmp_path, monkeypatch, venv_stub):
    from ptest.cli import main
    import ptest.agent_rules as rules_module

    _pytest_repo(tmp_path, venv_stub, cov_pair=False)
    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(
        rules_module._pre_changed_provider_text("claude"))
    monkeypatch.chdir(tmp_path)

    assert main(("init", "--runner", "pytest", "--agents", "claude",
                 "--no-doctor", "--no-smoke")) == 0

    assert (target / "SKILL.md").read_bytes() == rules_module._provider_text("claude")


def test_getting_started_shows_changed():
    from ptest import help as help_api
    from pathlib import Path as _Path

    assert "default loop: tests your change reaches" in help_api.overview()
    assert "changes since the last green run" in help_api.overview()
    run = help_api.topic("run")
    assert run is not None and "merge-base" in run
    agents = help_api.topic("agents")
    assert agents is not None and "runs the tests the change reaches: changes since the last green run" in agents
    assert "no baseline or coverage step is needed" in agents
    readme = (_Path(__file__).resolve().parent.parent.parent
              / "README.md").read_text(encoding="utf-8")
    assert "`ptest` runs the tests your change reaches" in readme
    assert "changes since the last green run" in readme
    assert "`ptest --full` once before handoff" in readme
