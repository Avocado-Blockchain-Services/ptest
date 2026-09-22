"""Help UX: static task-oriented help routes and their negative contracts."""
from __future__ import annotations

import pytest

from ptest import contracts as C
from ptest.cli import main, parse_argv

VALID_TOPICS = ("init", "register", "where", "status", "history", "plan",
                "doctor", "guide", "rules", "run", "agents")


def _no_execution(monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("help crossed an execution/network boundary")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("ptest.operations.execute", forbidden)


def _tree_bytes(root):
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def test_help_overview_is_task_oriented(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(("help",)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for marker in ("Getting started", "ptest init", "ptest tests/",
                   "ptest --full", "doctor", "agents",
                   "ptest help", "Examples"):
        assert marker in captured.out


@pytest.mark.parametrize("argv", [("help",), ("--help",), ("-h",)])
def test_help_aliases_show_overview_without_config(
        tmp_path, monkeypatch, capsys, argv):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Getting started" in captured.out
    assert not (tmp_path / ".ptest.toml").exists()
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("topic", VALID_TOPICS)
def test_help_topic_documents_its_command(
        tmp_path, monkeypatch, capsys, topic):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", topic)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip()


def test_help_topic_contents_are_command_specific(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    expectations = {
        "init": ("--runner", "--agents", "--dry-run"),
        "doctor": ("--probe", "--scope",),
        "run": ("--full",),
        "rules": ("--apply",),
        "guide": ("--write",),
        "history": ("--limit",),
    }
    for topic, markers in expectations.items():
        assert main(("help", topic)) == 0
        out = capsys.readouterr().out
        for marker in markers:
            assert marker in out, topic


def test_help_agents_is_self_contained_workflow(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "agents")) == 0
    out = capsys.readouterr().out
    for marker in (
        "--agents codex,claude",
        "--json",
        "monorepo root",
        "ptest --full",
        "--probe",
        "--prompt",
        "unknown",
        "guide",
    ):
        assert marker in out


def test_help_agents_warns_probe_executes_and_static_scan_proves_nothing(
        tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "agents")) == 0
    out = capsys.readouterr().out
    assert "execut" in out
    assert "never" in out.lower()


@pytest.mark.parametrize("command", ["init", "doctor", "where", "guide", "rules"])
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_inspection_help_flag_shows_topic_without_running(
        tmp_path, monkeypatch, capsys, command, flag):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main((command, flag)) == 0
    shown = capsys.readouterr()
    assert shown.err == ""
    assert main(("help", command)) == 0
    direct = capsys.readouterr()
    assert shown.out == direct.out
    assert not (tmp_path / ".ptest.toml").exists()
    assert _tree_bytes(tmp_path) == before


def test_help_is_deterministic_and_writeless(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "agents")) == 0
    first = capsys.readouterr().out
    assert main(("help", "agents")) == 0
    assert capsys.readouterr().out == first
    assert list(tmp_path.iterdir()) == []


def test_help_does_not_prompt_for_agents(tmp_path, monkeypatch, capsys):
    import sys

    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input",
                        lambda *args, **kwargs: pytest.fail("help prompted"))
    assert main(("help", "init")) == 0
    assert capsys.readouterr().err == ""


def test_help_hides_configured_launcher_secrets(case, monkeypatch, capsys):
    import json

    secrets = ("launcher-secret-token", "ODD=token-secret",
               "https://user:password-secret@example.invalid/")
    domain = case.domain()
    root = case.project(domain)
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n'
        '[runner]\nkind = "command"\n'
        f'launcher = {json.dumps([secrets[0]])}\n'
        f'args = {json.dumps([secrets[1]])}\n'
        f'full_args = {json.dumps([secrets[2]])}\n'
        'test_roots = ["tests"]\n'
        'workers = 1\nlifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    before = _tree_bytes(domain.root)
    for topic in VALID_TOPICS:
        assert main(("--fixture-domain", str(domain.root), "help", topic)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        for token in secrets:
            assert token not in captured.out
    assert _tree_bytes(domain.root) == before


def test_unknown_help_topic_exits_two_with_fixed_hint(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(("help", "exec\x1b[2J\nrm -rf ~")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\x1b" not in captured.err and "\n" in captured.err
    assert "unknown help topic" in captured.err
    for topic in VALID_TOPICS:
        assert topic in captured.err
    assert _tree_bytes(tmp_path) == before


def test_unknown_topic_never_echoes_raw_controls(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "bad\r\n\x00\x07topic")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for control in ("\x1b", "\r", "\x00", "\x07"):
        assert control not in captured.err


def test_long_hostile_topic_error_is_bounded(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "x" * 3000 + "\x1b[2J")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\x1b" not in captured.err
    assert len(captured.err.encode()) <= 1025


@pytest.mark.parametrize("argv", [
    ("help", "init", "extra"),
    ("help", "--json"),
    ("help", "agents", "--json"),
    ("--help", "extra"),
    ("-h", "extra"),
    ("help", "--help"),
])
def test_malformed_help_requests_are_rejected_without_execution(
        tmp_path, monkeypatch, capsys, argv):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert _tree_bytes(tmp_path) == before


def test_help_has_no_json_option(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "--json")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    with pytest.raises(C.Problem):
        C.decode_public_document(captured.err)


@pytest.mark.parametrize("argv", [
    ("init", "--help", "--dry-run"),
    ("init", "--help", "--runner", "pytest"),
    ("doctor", "--help", "--probe"),
    ("doctor", "-h", "--scope", "tests/x.py"),
    ("guide", "--help", "--write", "guide.md"),
])
def test_mixed_help_and_action_args_are_rejected_not_run(
        tmp_path, monkeypatch, capsys, argv):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "help" in captured.err.lower()
    assert not (tmp_path / ".ptest.toml").exists()
    assert not (tmp_path / "guide.md").exists()
    assert _tree_bytes(tmp_path) == before


def test_mixed_help_with_json_keeps_json_error_contract(
        tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(("where", "--help", "--json")) == 2
    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == "where"
    assert document.error.code == "invalid-config"
    assert document.data is None
    assert captured.err == ""
    assert _tree_bytes(tmp_path) == before


def test_double_dash_help_stays_runner_tail(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("--", "--help")) == 2
    captured = capsys.readouterr()
    assert "Getting started" not in captured.out
    assert "Getting started" not in captured.err


def test_scoped_tail_help_stays_runner_tail():
    parsed = parse_argv(("tests/example.py", "--help"))
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.runner_argv == ("tests/example.py", "--help")


def test_scoped_tail_help_does_not_show_help(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("tests/example.py", "--help")) == 2
    captured = capsys.readouterr()
    assert "Getting started" not in captured.out
    assert "Getting started" not in captured.err


def test_invalid_help_writes_nothing_without_config(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(("help", "no-such-topic")) == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []


def _documented_onboarding_argv():
    from ptest import help as help_api
    import shlex
    for line in help_api.topic("agents").splitlines():
        stripped = line.strip()
        if stripped.startswith("ptest init"):
            return tuple(shlex.split(stripped.removeprefix("ptest").strip()))
    raise AssertionError("agents workflow documents no init command")


def test_documented_onboarding_autodetects_monorepo(tmp_path, monkeypatch, capsys):
    import tomllib
    (tmp_path / ".git").mkdir()
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    (api / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (web / "package.json").write_text('{"devDependencies": {"vitest": "^3"}}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argv = _documented_onboarding_argv()
    assert "--runner" not in argv, argv
    assert main(argv) == 0
    capsys.readouterr()
    root_cfg = (tmp_path / ".ptest.toml").read_bytes()
    data = tomllib.loads(root_cfg.decode("utf-8"))
    assert data.get("version") == 2
    assert sorted(data["monorepo"]["children"]) == ["api", "web"]
    assert (tmp_path / ".agents" / "skills" / "ptest" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "ptest" / "SKILL.md").is_file()


def test_documented_workers_example_sets_ptest_workers_before_scope():
    from ptest import help as help_api
    import shlex
    text = help_api.topic("run")
    example = None
    for line in text.splitlines():
        stripped = line.strip().removeprefix("e.g. ").strip()
        if stripped.startswith("ptest --workers"):
            example = tuple(shlex.split(stripped.removeprefix("ptest").strip()))
            break
    assert example is not None, text
    parsed = parse_argv(example)
    assert parsed.workers is not None
    assert parsed.runner_argv and "--workers" not in parsed.runner_argv


def test_help_never_reads_config_resolver(tmp_path, monkeypatch, capsys):
    from ptest import config as config_api

    def forbidden(*args, **kwargs):
        pytest.fail("help read config")

    monkeypatch.setattr(config_api, "resolve_config", forbidden)
    monkeypatch.setattr(config_api, "init_project", forbidden)
    monkeypatch.setattr("ptest.cli.config_api.resolve_config", forbidden)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(("help", "agents")) == 0
    capsys.readouterr()
    assert main(("doctor", "--help")) == 0
    capsys.readouterr()
    assert _tree_bytes(tmp_path) == before


def test_rules_apply_help_rejects_without_files(tmp_path, monkeypatch, capsys):
    _no_execution(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)
    assert main(("rules", "--apply", "--help")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    assert _tree_bytes(tmp_path) == before
