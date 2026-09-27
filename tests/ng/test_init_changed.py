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

import pytest

from ptest import contracts as C


def _pytest_repo(root: Path, venv_stub, *, cov_pair: bool = True) -> Path:
    from ptest.runtime.pytest_bridge import _COVERAGE_TUPLE

    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\ndependencies = []\n', encoding="utf-8")
    if cov_pair:
        pytest_cov, coverage = _COVERAGE_TUPLE
        venv_stub(root, pytest_cov=pytest_cov, coverage=coverage)
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


def test_changed_setup_later_writes_cov_and_selection_draft(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "later")) == 0

    args = _runner_args(tmp_path)
    assert "--cov" in args
    assert "--cov-report" in args
    selection = _selection(tmp_path)
    assert selection is not None and selection.get("enabled") is True
    # The draft is a complete, closed policy, not just flipped enablement.
    assert selection.get("closed_inputs") is True
    assert selection.get("input_roots")
    assert selection.get("full_triggers")
    assert selection.get("groups")
    assert calls == []
    out = capsys.readouterr().out
    assert "baseline" in out


def test_changed_setup_now_writes_and_runs_full_baseline(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "now")) == 0

    assert "--cov" in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert len(calls) == 1
    assert calls[0].mode is C.Mode.FULL


def test_changed_setup_no_writes_no_coverage(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "no")) == 0

    assert "--cov" not in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert calls == []


def test_changed_setup_enter_defaults_to_later(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
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
    assert "Set up the optional coverage engine (ptest --shadow) for" in err
    assert "[now/later/no] (default: later)" in err


def test_changed_setup_non_interactive_default_is_later(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv()) == 0

    assert "--cov" in _runner_args(tmp_path)
    assert _selection(tmp_path).get("enabled") is True
    assert calls == []


# --- choice answers -----------------------------------------------------------


def test_ask_choice_reasks_on_unrecognised_answer(monkeypatch, capsys):
    from ptest import init_changed

    answers = iter(["nao", "now"])
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: next(answers))

    assert init_changed.ask_choice("api") == "now"
    err = capsys.readouterr().err
    assert err.count("Set up the optional coverage engine (ptest --shadow) for") == 2
    assert "nao" in err


def test_ask_choice_enter_and_eof_take_the_default(monkeypatch, capsys):
    from ptest import init_changed

    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "")
    assert init_changed.ask_choice("api") == "later"

    def _eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert init_changed.ask_choice("api") == "later"


def test_changed_setup_now_surfaces_baseline_recorded(
        case, tmp_path, monkeypatch, capsys, venv_stub):
    from ptest import operations
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    result = case.result(baseline_published=True)
    plan = C.Plan(mode=C.Mode.FULL, execution="full")

    def fake_execute(domain, config, request):
        calls.append(request)
        note = operations._baseline_note(plan=plan, advanced=True, result=result)
        operations._emit_end(request, result, 0.0, baseline_note=note)
        return type("Result", (), {
            "status": C.Status.PASSED, "exit_code": 0, "reasons": ()})()

    monkeypatch.setattr(operations, "execute", fake_execute)

    assert main(_init_argv("--changed-setup", "now")) == 0
    assert len(calls) == 1
    assert calls[0].mode is C.Mode.FULL
    assert "ptest: baseline recorded" in capsys.readouterr().err


# --- eligibility ------------------------------------------------------------


def test_changed_setup_without_pytest_cov_prints_needs_line(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub, cov_pair=False)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later")) == 0

    assert "--cov" not in _runner_args(tmp_path)
    out = capsys.readouterr().out
    assert (".: the optional coverage engine (ptest --shadow) needs pytest-cov; "
            "bare ptest does not") in out


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
    assert "Set up the optional coverage engine" not in captured.err


def test_changed_setup_dry_run_shows_and_writes_nothing(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    assert main(_init_argv("--changed-setup", "now", "--dry-run")) == 0

    assert not (tmp_path / ".ptest.toml").exists()
    assert calls == []
    out = capsys.readouterr().out
    assert "--cov" in out or "would" in out


_CHANGED_MARKERS = ("coverage engine", "selection is off in this config",
                     "would set up", "baseline")


def _changed_lines(out: str) -> list[str]:
    return [line for line in out.splitlines()
            if any(marker in line for marker in _CHANGED_MARKERS)]


@pytest.mark.parametrize("choice", ["no", "later", "now"])
@pytest.mark.parametrize("existing", [False, True])
def test_changed_setup_preview_matches_real_run(
        tmp_path, monkeypatch, capsys, venv_stub, choice, existing):
    """Twins: dry-run preview describes exactly what the real run does."""
    from ptest import init_changed
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    calls: list = []
    _stub_execute(monkeypatch, calls)

    if existing:
        assert main(_init_argv("--changed-setup", "no")) == 0
        # Fresh configs enable graph selection; rewrite to a 0.2.x config
        # so the existing-config path still applies.
        config_path = tmp_path / ".ptest.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "enabled = true", "enabled = false", 1),
            encoding="utf-8")
        before = config_path.read_bytes()
        capsys.readouterr()
        calls.clear()
    else:
        before = None

    assert main(_init_argv("--changed-setup", choice, "--dry-run")) == 0
    if existing:
        assert (tmp_path / ".ptest.toml").read_bytes() == before
    else:
        assert not (tmp_path / ".ptest.toml").exists()
    assert calls == []
    preview_lines = _changed_lines(capsys.readouterr().out)

    assert main(_init_argv("--changed-setup", choice)) == 0
    real_lines = _changed_lines(capsys.readouterr().out)

    if choice == "no":
        assert preview_lines == []
        assert real_lines == []
    elif existing:
        expected = [init_changed.EXISTING_LINE.format(project=".")]
        assert preview_lines == expected
        assert real_lines == expected
    else:
        assert preview_lines == [init_changed.DRY_RUN_LINE.format(project=".")]
        if choice == "later":
            assert real_lines == [init_changed.LATER_LINE.format(project=".")]
        else:
            assert real_lines == [init_changed.NOW_LINE.format(project=".")]
            assert len(calls) == 1
            assert calls[0].mode is C.Mode.FULL
    if existing:
        assert (tmp_path / ".ptest.toml").read_bytes() == before
        assert calls == []
    elif choice in ("later", "now"):
        assert "--cov" in _runner_args(tmp_path)
        assert _selection(tmp_path).get("enabled") is True
    else:
        assert "--cov" not in _runner_args(tmp_path)


def test_changed_setup_monorepo_preview_matches_real_run(
        tmp_path, monkeypatch, capsys, venv_stub, monorepo):
    """Reported shape: existing dispatcher children honour the choice."""
    from ptest import init_changed
    from ptest.cli import main
    from ptest.runtime.pytest_bridge import _COVERAGE_TUPLE

    pytest_cov, coverage = _COVERAGE_TUPLE
    uv_launcher = ("uv", "run", "--locked", "--no-sync", "python")
    root = monorepo(
        {"api": {"kind": "pytest", "launcher": uv_launcher,
                 "project_id": "ab" * 16},
         "job": {"kind": "pytest", "launcher": uv_launcher,
                 "project_id": "cd" * 16}},
        parent=tmp_path, name="mono",
        root_toml='version = 2\n\n[monorepo]\nchildren = ["api", "job"]\n')
    venv_stub(root, pytest_cov=pytest_cov, coverage=coverage)
    venv_stub(root / "api", pytest_cov=pytest_cov, coverage=coverage)
    venv_stub(root / "job", pytest_cov=pytest_cov, coverage=coverage)
    monkeypatch.chdir(root)
    before = {child: (root / child / ".ptest.toml").read_bytes()
              for child in ("api", "job")}

    def _run(*extra: str):
        argv = ("init", "--agents", "none", "--no-doctor", "--no-smoke",
                *extra)
        assert main(argv) == 0
        return _changed_lines(capsys.readouterr().out)

    assert _run("--changed-setup", "no", "--dry-run") == []
    assert _run("--changed-setup", "no") == []

    expected = [init_changed.EXISTING_LINE.format(project=child)
                for child in ("api", "job")]
    assert _run("--changed-setup", "later", "--dry-run") == expected
    assert _run("--changed-setup", "later") == expected

    for child in ("api", "job"):
        assert (root / child / ".ptest.toml").read_bytes() == before[child]


def test_changed_setup_existing_config_points_to_doctor_fix(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)
    assert main(_init_argv("--changed-setup", "no")) == 0
    # Fresh configs enable graph selection; rewrite to a 0.2.x config
    # so the existing-config path still applies.
    config_path = tmp_path / ".ptest.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "enabled = true", "enabled = false", 1),
        encoding="utf-8")
    before = config_path.read_bytes()
    capsys.readouterr()

    assert main(_init_argv("--changed-setup", "later")) == 0

    assert (tmp_path / ".ptest.toml").read_bytes() == before
    out = capsys.readouterr().out
    assert "doctor --fix" in out


# --- grammar ----------------------------------------------------------------


def test_changed_setup_invalid_value_is_rejected(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "someday")) == 2
    assert not (tmp_path / ".ptest.toml").exists()


def test_changed_setup_repeated_flag_is_rejected(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
    monkeypatch.chdir(tmp_path)

    assert main(_init_argv("--changed-setup", "later", "--changed-setup", "no")) == 2


def test_changed_setup_cannot_combine_with_json(tmp_path, monkeypatch, capsys, venv_stub):
    from ptest.cli import main

    _pytest_repo(tmp_path, venv_stub)
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
    assert len(guide.splitlines()) <= 100
    assert ("| After each edit | `ptest` (bare `ptest` runs the tests your change "
            "reaches: git diff vs the branch base, no baseline or coverage needed) |"
            ) in guide
    assert "| Integrated change, before handoff | `ptest --full` once |" in guide
    assert "the first run records a baseline" not in guide
    assert "baseline recorded" in guide


def test_skill_template_defaults_to_changed():
    import ptest.agent_rules as rules_module

    text = rules_module._provider_text("claude").decode("utf-8")
    assert "`ptest` after each edit runs the tests your change reaches" in text
    assert "`ptest --full` once before handoff" in text
    assert "docs/ptest-agent.md" in text


def test_changed_setup_strings_never_name_changed_flag():
    from ptest import init_changed

    for constant in (init_changed.QUESTION, init_changed.NEEDS_COV_LINE,
                     init_changed.LATER_LINE, init_changed.NOW_LINE,
                     init_changed.EXISTING_LINE, init_changed.DRY_RUN_LINE):
        assert "ptest --changed" not in constant


def test_init_then_uninstall_recognises_new_guide_and_skill(tmp_path, monkeypatch, venv_stub):
    from ptest.cli import main
    import ptest.agent_rules as rules_module

    _pytest_repo(tmp_path, venv_stub, cov_pair=False)
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
                 "--no-doctor", "--no-smoke", "--changed-setup", "no")) == 0

    assert (target / "SKILL.md").read_bytes() == rules_module._provider_text("claude")
