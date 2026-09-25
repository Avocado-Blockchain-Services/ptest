"""Twins for spec section C: `ptest init` offers `--changed` setup.

Covers the init question (now/later/no + Enter default), the
non-interactive `--changed-setup` flag, the no-pytest-cov line, the vitest
skip, dry-run preview, existing-config guidance, and the shipped
guide/skill/help/README default loop.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from ptest import contracts as C


def _stub_cov_venv(root: Path) -> None:
    from ptest.runtime.pytest_bridge import _COVERAGE_TUPLE

    packages = root / ".venv" / "lib" / "python3.12" / "site-packages"
    pytest_cov, coverage = _COVERAGE_TUPLE
    (packages / f"pytest_cov-{pytest_cov}.dist-info").mkdir(parents=True)
    (packages / f"coverage-{coverage}.dist-info").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12\n",
        encoding="utf-8",
    )


def _pytest_repo(root: Path, *, cov_pair: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\ndependencies = []\n', encoding="utf-8")
    if cov_pair:
        _stub_cov_venv(root)
    return root


def _init_argv(*extra: str) -> tuple[str, ...]:
    return ("init", "--runner", "pytest", "--agents", "none",
            "--no-doctor", "--no-smoke", *extra)


def _read_config(root: Path) -> dict:
    return tomllib.loads((root / ".ptest.toml").read_text(encoding="utf-8"))


def _runner_args(root: Path) -> list:
    return list(_read_config(root)["runner"]["args"])


def _selection(root: Path) -> dict | None:
    return _read_config(root).get("selection")


def _stub_execute(monkeypatch, calls, *, status=C.Status.PASSED):
    from ptest import operations

    def fake(domain, config, request):
        calls.append(request)
        return type("Result", (), {
            "status": status, "exit_code": 0 if status is C.Status.PASSED else 1,
            "reasons": (), "counts": None})()

    monkeypatch.setattr(operations, "execute", fake)


# --- choice matrix ----------------------------------------------------------


def test_changed_setup_later_writes_cov_and_selection_draft(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "later")) == 0

    args = _runner_args(tmp_path)
    assert "--cov" in args
    assert "--cov-report" in args
    selection = _selection(tmp_path)
    assert selection is not None and selection.get("enabled") is True
    assert calls == []
    out = capsys.readouterr().out
    assert "baseline" in out


def test_changed_setup_now_writes_and_runs_full_baseline(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "now")) == 0

    assert "--cov" in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert len(calls) == 1
    assert calls[0].mode is C.Mode.FULL


def test_changed_setup_no_leaves_selection_off(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "no")) == 0

    assert "--cov" not in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is not True
    assert calls == []


def test_changed_setup_enter_defaults_to_later(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv()) == 0

    assert "--cov" in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert calls == []
    err = capsys.readouterr().err
    assert "Set up ptest --changed for" in err
    assert "[now/later/no] (default: later)" in err


def test_changed_setup_non_interactive_default_is_later(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv()) == 0

    assert "--cov" in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert calls == []


# --- eligibility ------------------------------------------------------------


def test_changed_setup_without_pytest_cov_prints_needs_line(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path, cov_pair=False)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later")) == 0

    assert "--cov" not in _runner_args(tmp_path)
    out = capsys.readouterr().out
    assert "ptest --changed needs pytest-cov (add it to the test deps)" in out


def test_changed_setup_skips_vitest_without_a_question(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "example.test.ts").write_text(
        "test('x', () => {})\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "fixture"}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("vitest must never be asked")

    monkeypatch.setattr("builtins.input", _boom)

    assert main(("init", "--runner", "vitest", "--agents", "none",
                 "--no-doctor", "--no-smoke")) == 0

    captured = capsys.readouterr()
    assert "--changed" not in captured.out
    assert "Set up ptest --changed" not in captured.err


def test_changed_setup_dry_run_shows_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "now", "--dry-run")) == 0

    assert not (tmp_path / ".ptest.toml").exists()
    assert calls == []
    out = capsys.readouterr().out
    assert "--cov" in out or "would" in out


def test_changed_setup_existing_config_points_to_doctor_fix(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(_init_argv("--changed-setup", "no")) == 0
    before = (tmp_path / ".ptest.toml").read_bytes()
    capsys.readouterr()

    assert main(_init_argv("--changed-setup", "later")) == 0

    assert (tmp_path / ".ptest.toml").read_bytes() == before
    out = capsys.readouterr().out
    assert "doctor --fix" in out


# --- grammar ----------------------------------------------------------------


def test_changed_setup_invalid_value_is_rejected(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "someday")) == 2
    assert not (tmp_path / ".ptest.toml").exists()


def test_changed_setup_repeated_flag_is_rejected(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later", "--changed-setup", "no")) == 2


def test_changed_setup_cannot_combine_with_json(tmp_path, monkeypatch, capsys):
    from ptest.cli import main

    _pytest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later", "--json")) == 2


def test_changed_setup_values_parse():
    from ptest.cli import parse_argv

    assert parse_argv(("init", "--changed-setup", "now")).changed_setup == "now"
    assert parse_argv(("init", "--changed-setup", "later")).changed_setup == "later"
    assert parse_argv(("init", "--changed-setup", "no")).changed_setup == "no"
    assert parse_argv(("init",)).changed_setup is None


# --- shipped guidance default loop ------------------------------------------


def test_repository_guide_default_loop_is_changed():
    from importlib.resources import files

    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 45
    assert "After each edit run the default loop `ptest --changed`" in guide
    assert "Run `ptest --full` once after the integrated change" in guide
    assert "first `ptest --changed` may run everything" in guide
    assert "record a baseline" in guide


def test_skill_template_defaults_to_changed():
    import ptest.agent_rules as rules_module

    text = rules_module._provider_text("claude").decode("utf-8")
    assert "ptest --changed" in text
    assert "docs/ptest-agent.md" in text


def test_init_then_uninstall_recognises_new_guide_and_skill(tmp_path, monkeypatch):
    from ptest.cli import main
    import ptest.agent_rules as rules_module

    _pytest_repo(tmp_path, cov_pair=False)
    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "claude",
                 "--no-doctor", "--no-smoke",
                 "--changed-setup", "no")) == 0

    guide = tmp_path / "docs" / "ptest-agent.md"
    assert guide.read_bytes() == rules_module._guide()

    from ptest import uninstall as uninstall_api

    assert uninstall_api._is_managed_guide(guide.read_bytes()) is True
    guide_entry = uninstall_api._decide_guide(guide.read_bytes())
    assert guide_entry is not None and guide_entry.action == uninstall_api.REMOVE


def test_init_upgrades_previous_skill_in_place(tmp_path, monkeypatch):
    from ptest.cli import main
    import ptest.agent_rules as rules_module

    _pytest_repo(tmp_path, cov_pair=False)
    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(
        rules_module._pre_changed_provider_text("claude"))
    monkeypatch.chdir(tmp_path)

    assert main(("init", "--runner", "pytest", "--agents", "claude",
                 "--no-doctor", "--no-smoke", "--changed-setup", "no")) == 0

    assert (target / "SKILL.md").read_bytes() == rules_module._provider_text("claude")


def test_getting_started_shows_changed():
    from ptest import help as help_api
    from pathlib import Path as _Path

    assert "ptest --changed" in help_api.overview()
    agents = help_api.topic("agents")
    assert agents is not None and "ptest --changed" in agents
    readme = (_Path(__file__).resolve().parent.parent.parent
              / "README.md").read_text(encoding="utf-8")
    assert "ptest --changed" in readme
