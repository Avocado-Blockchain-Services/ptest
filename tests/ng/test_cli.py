from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.cli import main, parse_argv
from ptest.runners import adapter_for, registered_kinds


def test_root_scope_routes_to_one_child_and_rebases_before_execution(tmp_path, monkeypatch):
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n', encoding="utf-8")
    for child in ("api", "web"):
        root = tmp_path / child
        root.mkdir()
        (root / ".ptest.toml").write_text(
            'version = 1\nproject_id = "' + ("ab" if child == "api" else "cd") * 16 + '"\n'
            '[runner]\nkind = "command"\nlauncher = ["true"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr("ptest.operations.execute", lambda domain, config, request: calls.append((domain, config, request)) or type("R", (), {"reasons": (), "exit_code": 0})())

    assert main(("api/tests/unit",)) == 0
    assert len(calls) == 1
    assert calls[0][2].argv == ("tests/unit",)


def test_init_from_monorepo_root_creates_dispatcher_without_cd(tmp_path, monkeypatch, capsys):
    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    (api / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    (web / "package.json").write_text('{"devDependencies":{"vitest":"1"}}')
    monkeypatch.chdir(tmp_path)

    assert main(("init",)) == 0
    out = capsys.readouterr().out
    assert "created" in out and ".ptest.toml" in out
    assert "created:" not in out
    assert (tmp_path / ".ptest.toml").is_file()
    assert (api / ".ptest.toml").is_file()
    assert (web / ".ptest.toml").is_file()


def test_init_explicit_agent_choice_adds_only_repository_local_skill(tmp_path, monkeypatch):
    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    monkeypatch.chdir(tmp_path)

    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    assert (tmp_path / ".agents/skills/ptest/SKILL.md").is_file()
    assert (tmp_path / "docs/ptest-agent.md").is_file()
    assert not (tmp_path / ".codex/skills/ptest/SKILL.md").exists()
    assert not (tmp_path.parent / ".agents").exists()


def test_root_full_preflights_all_children_then_runs_in_order_and_keeps_first_failure(
        tmp_path, monkeypatch):
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n', encoding="utf-8")
    for child, project_id in (("api", "ab"), ("web", "cd")):
        root = tmp_path / child
        root.mkdir()
        (root / ".ptest.toml").write_text(
            'version = 1\nproject_id = "' + project_id * 16 + '"\n'
            '[runner]\nkind = "command"\nlauncher = ["true"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config.config_path.parent.name, request.mode))
        or type("R", (), {"reasons": (), "exit_code": 9 if len(calls) == 1 else 3})(),
    )

    assert main(("--full",)) == 9
    assert calls == [("api", C.Mode.FULL), ("web", C.Mode.FULL)]


@pytest.mark.parametrize("scope", ["", "api", "web/x", "api/../x", "/api/x", "api\\x", "api/x", "api2/x"])
def test_root_scope_rejects_invalid_or_undeclared_scope_before_execution(tmp_path, monkeypatch, scope):
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr("ptest.operations.execute", lambda *args: calls.append(args))

    assert main((scope,)) == 2
    assert calls == []


def test_execution_parser_stops_at_first_unknown_and_preserves_tail():
    parsed = parse_argv(("--workers", "2", "-k", "--full"))
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.workers == 2
    assert parsed.runner_argv == ("-k", "--full")


def test_init_parser_accepts_explicit_monorepo_child_runner_pairs():
    parsed = parse_argv((
        "init", "--child", "api", "--runner", "pytest",
        "--child", "web", "--runner", "vitest",
    ))

    assert parsed.children == (("api", C.RunnerKind.PYTEST),
                               ("web", C.RunnerKind.VITEST))


def test_init_parser_accepts_explicit_agent_integrations():
    parsed = parse_argv(("init", "--agents", "codex,opencode"))

    assert parsed.agents == ("codex", "opencode")
    assert parsed.agents_explicit is True


@pytest.mark.parametrize("argv", [
    ("init", "--child", "api"),
    ("init", "--child", "api", "--runner", "pytest", "--child", "web"),
    ("init", "--child", "api", "--runner", "unknown"),
])
def test_init_parser_rejects_incomplete_or_unsupported_child_pairs(argv):
    with pytest.raises(C.Problem):
        parse_argv(argv)


@pytest.mark.parametrize("argv", [
    ("-k", "--workers"),
    ("-k", "--full"),
    ("tests/a.py", "--full"),
])
def test_runner_looking_flags_never_resume_ptest_parsing(argv):
    parsed = parse_argv(argv)
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.runner_argv == argv


def test_full_rejects_literal_narrowing_tail():
    with pytest.raises(C.Problem) as exc:
        parse_argv(("--full", "--", "-k", "slow"))
    assert exc.value.code == "invalid-config"


def test_changed_rejects_runner_tail():
    with pytest.raises(C.Problem) as exc:
        parse_argv(("--changed", "tests/a.py"))
    assert exc.value.code == "invalid-config"


@pytest.mark.parametrize("command,invalid,code", [
    ("where", ("--unknown",), "invalid-config"),
    ("status", ("--bogus",), "invalid-config"),
    ("doctor", ("--unknown",), "invalid-config"),
    ("doctor", ("--probe", "tests/a.py"), "invalid-config"),
    ("init", ("--unknown",), "invalid-config"),
    ("register", ("--unknown",), "invalid-config"),
    ("plan", ("--unknown",), "invalid-config"),
    ("history", ("--unknown",), "invalid-config"),
])
@pytest.mark.parametrize("json_first", [False, True])
def test_json_error_is_one_descriptor_document(command, invalid, code,
                                               json_first, capsys):
    options = ("--json", *invalid) if json_first else (*invalid, "--json")
    assert main((command, *options)) == 2
    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == command
    assert document.error.code == code
    assert document.data is None
    assert captured.err == ""


def test_repeated_fixture_domain_is_rejected_before_text_inspection(
        inspection_project, capsys):
    domain, _ = inspection_project
    second_domain = domain.root.parent / "private-second-fixture-token"
    before = _tree_bytes(domain.root.parent)
    argv = ("--fixture-domain", str(domain.root),
            "--fixture-domain", str(second_domain), "where")

    assert main(argv) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid-config: option cannot be repeated\n"
    assert str(domain.root) not in captured.err
    assert str(second_domain) not in captured.err
    assert _tree_bytes(domain.root.parent) == before


def test_repeated_fixture_domain_keeps_json_error_contract_before_bogus_option(
        inspection_project, capsys):
    domain, _ = inspection_project
    second_domain = domain.root.parent / "private-second-fixture-token"
    before = _tree_bytes(domain.root.parent)
    argv = ("--fixture-domain", str(domain.root),
            "--fixture-domain", str(second_domain),
            "where", "--bogus", "--json")

    assert main(argv) == 2

    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == "where"
    assert document.error.code == "invalid-config"
    assert document.error.message == "option cannot be repeated"
    assert document.data is None
    assert captured.err == ""
    for token in (str(domain.root), str(second_domain), "--bogus"):
        assert token not in captured.out
    assert _tree_bytes(domain.root.parent) == before


SECRET_ARGV = ("/private/launcher-secret", "ODD=token-secret",
               "https://user:password-secret@example.invalid/",
               "--unrecognized-secret", "$(touch injected)", "quoted 'token'; *")


@pytest.fixture
def inspection_project(case, monkeypatch):
    domain = case.domain()
    root = case.project(domain)
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n'
        '[runner]\nkind = "command"\n'
        f'launcher = {json.dumps(SECRET_ARGV[:2])}\n'
        f'args = {json.dumps(SECRET_ARGV[2:4])}\n'
        f'full_args = {json.dumps(SECRET_ARGV[4:])}\n'
        'test_roots = ["tests"]\n'
        'workers = 1\nlifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_cache.py").write_text(
        "raise RuntimeError('inspection executed project code')\n"
        "cache.flushall()\n", encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return domain, root


def _tree_bytes(root):
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


@pytest.mark.parametrize("command", [
    "where", "register", "init", "plan", "status", "history", "doctor",
])
@pytest.mark.parametrize("json_mode", [False, True])
def test_static_dispatch_is_read_only_redacted_and_contract_valid(
        inspection_project, monkeypatch, capsys, command, json_mode):
    import socket
    import subprocess

    domain, root = inspection_project
    before = _tree_bytes(domain.root)

    def no_execution(*args, **kwargs):
        pytest.fail("static inspection crossed an execution/network boundary")

    monkeypatch.setattr(subprocess, "Popen", no_execution)
    monkeypatch.setattr(socket, "create_connection", no_execution)
    options = (("--json",) if json_mode else
               (("--offline",) if command == "doctor" else ()))
    assert main(("--fixture-domain", str(domain.root), command, *options)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out
    for token in SECRET_ARGV:
        assert token not in captured.out
    assert _tree_bytes(domain.root) == before
    if json_mode:
        document = C.decode_public_document(captured.out)
        assert document.kind == command
        assert document.error is None
        data = document.data
        if command in {"where", "register"}:
            assert data["root"] == str(root)
            assert data["initialized"] is True
            assert [item["argument_count"] for item in data["commands"]] == [4, 6]
        elif command == "init":
            assert data["action"] == "existing"
            assert data["exists"] is True
            assert data["config"]["runner_kind"] == "command"
        elif command == "plan":
            assert data["execution"] == "full"
            assert data["static_preview"] is True
            assert data["reasons"][0]["code"] == "selection-disabled"
        elif command == "status":
            assert data["queued"] == data["active"] == []
            assert data["effective_limits"]["max_slots"] == 1
        elif command == "history":
            assert data == {"summaries": [], "obligations": []}
        else:
            assert any(item["code"] == "cache.global-flush" for item in data["findings"])
            assert next(item for item in data["readiness"]
                        if item["area"] == "parallel")["state"] == "unknown"
    else:
        if command == "init":
            import re

            assert re.search(r"unchanged:?\s+\.ptest\.toml", captured.out)
            return
        expected = {
            "where": f"root: {root}", "register": "register: initialized",
            "plan": "plan: full (static preview)", "status": "queued: 0\nactive: 0",
            "history": "history: 0 runs", "doctor": "cache.global-flush",
        }
        assert expected[command] in captured.out


def test_rules_requires_explicit_apply_before_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(("rules",)) == 0
    assert "create docs/ptest-agent.md" in capsys.readouterr().out
    assert not (tmp_path / "AGENTS.md").exists()

    assert main(("rules", "--apply")) == 0
    assert "applied:" in capsys.readouterr().out
    assert (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("json_mode", [False, True])
def test_where_reveal_is_labelled_stderr_only_and_never_persisted(
        inspection_project, capsys, json_mode):
    domain, root = inspection_project
    before = _tree_bytes(domain.root)
    options = ("--json",) if json_mode else ()
    assert main(("--fixture-domain", str(domain.root), "where",
                 "--reveal-command", *options)) == 0
    captured = capsys.readouterr()
    label, revealed = captured.err.split(": ", 1)
    assert label == "unredacted-command-disclosure"
    assert json.loads(revealed) == list(SECRET_ARGV)
    assert all(token not in captured.out for token in SECRET_ARGV)
    if json_mode:
        assert C.decode_public_document(captured.out).kind == "where"
    assert _tree_bytes(domain.root) == before


@pytest.mark.parametrize(
    "kind,roots,setup,expected,limitation",
    [
        ("pytest", ("tests",), False, "basic_serial", "inventory"),
        ("pytest", (".",), False, "basic_serial", "dot test root"),
        ("pytest", ("tests",), True, "basic_serial", "collection_finish"),
        ("vitest", ("tests",), False, "exclusive_command",
         "exclusive command"),
    ],
)
@pytest.mark.parametrize("json_mode", [False, True])
def test_where_describes_conditional_pytest_and_prepared_vitest_without_execution(
        case, monkeypatch, capsys, kind, roots, setup, expected, limitation, json_mode):
    domain = case.domain()
    root = case.project(domain, kind=kind)
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    lines = [
        "version = 1",
        f'project_id = "{project_id}"',
        "[runner]",
        f'kind = "{kind}"',
        f'launcher = {json.dumps(["python"] if kind == "pytest" else ["node"])}',
        "args = []",
        "full_args = []",
        f"test_roots = {json.dumps(list(roots))}",
        "workers = 8",
        'lifecycle = "cooperative-process-group"',
    ]
    if setup:
        lines.extend([
            "[setup]",
            'argv = ["uv", "sync"]',
            'required_paths = [".venv"]',
            "network = true",
            "lifecycle_scripts = true",
        ])
    (root / ".ptest.toml").write_text("\n".join(lines) + "\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("where executed a runner"))
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("where made a network request"))
    args = ("where", "--json") if json_mode else ("where",)
    assert main(("--fixture-domain", str(domain.root), *args)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_mode:
        payload = C.decode_public_document(captured.out).data
        capability = payload["capability"]
        text = " ".join(item["message"] for item in capability["limitations"])
        assert capability["execution"] == expected
    else:
        assert f"capability: {expected}" in captured.out
        text = captured.out
    assert limitation in text
    if setup:
        assert "declared setup executes" in text


@pytest.mark.parametrize("target", ["tests/test_cache.py", "tests/test_cache.py::test_a", "ab" * 16])
@pytest.mark.parametrize("json_mode", [False, True])
def test_history_filter_fails_closed_instead_of_reporting_wrong_empty_result(
        inspection_project, capsys, target, json_mode):
    domain, _ = inspection_project
    options = ("--json",) if json_mode else ()
    assert main(("--fixture-domain", str(domain.root), "history", target, *options)) == 2
    captured = capsys.readouterr()
    if json_mode:
        document = C.decode_public_document(captured.out)
        assert document.kind == "history"
        assert document.error.code == "unsupported-capability"
        assert document.data is None
        assert captured.err == ""
    else:
        assert captured.out == ""
        assert "unsupported-capability" in captured.err


@pytest.mark.parametrize("options", [
    ("--json",), ("--json", "--write", "guide.md"),
    ("--write", "guide.md", "--json"),
    ("--write", "first.md", "--write", "guide.md"),
])
def test_guide_rejects_invalid_options_before_export(tmp_path, monkeypatch, capsys, options):
    monkeypatch.chdir(tmp_path)
    assert main(("guide", *options)) == 2
    captured = capsys.readouterr()
    assert not (tmp_path / "guide.md").exists()
    assert not (tmp_path / "first.md").exists()
    assert captured.out == ""
    assert "invalid-config" in captured.err
    assert "unknown inspection option" in captured.err


def test_guide_print_and_exclusive_export_match_generated_guide(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(("guide",)) == 0
    captured = capsys.readouterr()
    bundled = captured.out.encode()
    assert b"Doctor assessment checklist" in bundled
    assert b"FIX-001" in bundled and b"TIMING-001" in bundled
    assert captured.err == ""
    assert main(("guide", "--write", "guide.md")) == 0
    captured = capsys.readouterr()
    assert captured.out.encode() == bundled
    assert captured.err == ""
    assert (tmp_path / "guide.md").read_bytes() == bundled
    (tmp_path / "guide.md").write_bytes(b"user-owned replacement")
    assert main(("guide", "--write", "guide.md")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already-exists" in captured.err
    assert (tmp_path / "guide.md").read_bytes() == b"user-owned replacement"


def test_guide_export_from_subdirectory_uses_project_root(
        inspection_project, monkeypatch, capsys):
    _, root = inspection_project
    nested = root / "tests"
    monkeypatch.chdir(nested)
    assert main(("guide", "--write", "guide.md")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (root / "guide.md").is_file()
    assert (root / "guide.md").read_text() == captured.out
    assert not (nested / "guide.md").exists()


def test_missing_guide_resource_cannot_export_substitute(
        tmp_path, monkeypatch, capsys):
    from ptest import render

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(render.importlib.resources, "files", lambda _package: tmp_path)
    assert main(("guide", "--write", "guide.md")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "state-unavailable" in captured.err
    assert not (tmp_path / "guide.md").exists()


@pytest.mark.parametrize("json_mode", [False, True])
def test_init_preview_creates_nothing_then_creation_preserves_existing_bytes(
        tmp_path, monkeypatch, capsys, json_mode):
    from ptest.config import resolve_config

    monkeypatch.chdir(tmp_path)
    options = ("--json",) if json_mode else ()
    target = tmp_path / ".ptest.toml"
    for action, extra in (("preview", ("--dry-run",)), ("created", ()), ("existing", ())):
        previous = target.read_bytes() if target.exists() else None
        assert main(("init", "--runner", "pytest", *extra, *options)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        if json_mode:
            document = C.decode_public_document(captured.out)
            assert document.kind == "init"
            assert document.error is None
            assert document.data["action"] == action
            assert document.data["exists"] is (action != "preview")
        else:
            human_action = {"preview": "would create", "created": "created",
                            "existing": "unchanged"}[action]
            assert human_action in captured.out
            assert ".ptest.toml" in captured.out
            assert f"{human_action}:" not in captured.out
        if action == "preview":
            assert not target.exists()
        else:
            assert resolve_config(tmp_path).config.runner.kind is C.RunnerKind.PYTEST
        if action == "existing":
            assert target.read_bytes() == previous


@pytest.mark.parametrize("json_mode", [False, True])
def test_nonempty_status_and_history_are_real_producer_data(
        inspection_project, case, capsys, json_mode):
    from ptest import history, platform, scheduler

    domain, root = inspection_project
    checkout = C.CheckoutIdentity(project_id="ab" * 16,
        checkout_id=hashlib.sha256(os.fsencode(root)).hexdigest()[:32], root=root)
    owner = platform.process_identity(os.getpid())
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="12" * 16, checkout=checkout, owner=owner, slots=1,
        exclusive=True, locks=(), memory_mb=None,
        deadline=time.monotonic() + 60, fixture=True,
    ))
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL,
                                 SECRET_ARGV, workers=1, provenance=("config",))
    result = case.result(sequence=1, project_id=checkout.project_id,
                         checkout_id=checkout.checkout_id, command=command,
                         status="failed", runner_exit_code=1, exit_code=1,
                         full_gate_eligible=False)
    inventory = case.inventory(("tests/test_cache.py",), outcome="failed")
    assert history.publish_outcome(domain, checkout, result, inventory).committed
    before = _tree_bytes(domain.root)
    options = ("--json",) if json_mode else ()
    for name in ("status", "history"):
        assert main(("--fixture-domain", str(domain.root), name, *options)) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert all(token not in captured.out for token in SECRET_ARGV)
        if json_mode:
            document = C.decode_public_document(captured.out)
            assert document.kind == name and document.error is None
            if name == "status":
                assert [item["run_id"] for item in document.data["queued"]] == ["12" * 16]
                assert document.data["queued"][0]["checkout_id"] == checkout.checkout_id
                assert document.data["active"] == []
            else:
                assert [item["run_id"] for item in document.data["summaries"]] == [result.run_id]
                assert document.data["summaries"][0]["status"] == "failed"
                assert any(item["file"] == "tests/test_cache.py"
                           for item in document.data["obligations"])
        else:
            assert captured.out == ("queued: 1\nactive: 0\n" if name == "status"
                                    else "history: 1 runs\n")
    assert _tree_bytes(domain.root) == before


def test_doctor_prompt_uses_actual_findings_without_commands_or_writes(
        inspection_project, capsys):
    domain, _ = inspection_project
    before = _tree_bytes(domain.root)
    assert main(("--fixture-domain", str(domain.root), "doctor", "--prompt")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "cache.global-flush" in captured.out
    assert "tests/test_cache.py" in captured.out
    assert "Repair constraints:" in captured.out
    assert all(token not in captured.out for token in SECRET_ARGV)
    assert _tree_bytes(domain.root) == before


def test_human_errors_escape_and_bound_path_evidence(
        inspection_project, monkeypatch, capsys):
    from ptest import config

    def unavailable(_cwd):
        raise C.Problem(code="state-unavailable", phase="config",
                        message="bad\x1b[2J\r\n" + "x" * 3000)

    monkeypatch.setattr(config, "resolve_config", unavailable)
    assert main(("where",)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "\x1b" not in captured.err and "\r" not in captured.err
    assert "\\x1b[2J" in captured.err
    assert "[truncated]" in captured.err
    assert len(captured.err.encode()) <= 1025


@pytest.mark.parametrize("argv", [("-k", "--json"), ("tests/a.py", "--json"),
                                  ("--", "where", "--json")])
def test_json_inside_literal_execution_tail_stays_runner_data(
        argv, tmp_path, monkeypatch, capsys):
    # Keep this parser negative test outside the repository's native config:
    # it must reject before scheduler admission, independently of setup.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ptest.operations.scheduler.enqueue",
        lambda *args, **kwargs: pytest.fail("parser rejection admitted a run"),
    )
    assert parse_argv(argv).runner_argv == (argv[1:] if argv[0] == "--" else argv)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unsupported-capability" in captured.err


@pytest.mark.parametrize("command", ["where", "init"])
def test_human_root_controls_are_escaped_while_json_keeps_typed_path(
        inspection_project, monkeypatch, capsys, command):
    domain, root = inspection_project
    hostile = root.with_name("root\x1b[31m\r\t\x7f\u009b\u202ered")
    root.rename(hostile)
    monkeypatch.chdir(hostile)
    prefix = ("--fixture-domain", str(domain.root), command)
    assert main(prefix) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for control in ("\x1b", "\r", "\t", "\x7f", "\u009b", "\u202e"):
        assert control not in captured.out
    if command == "where":
        assert "\\x1b[31m" in captured.out
    else:
        # The init banner shows repository-relative targets, so a hostile
        # root name never reaches human output; the banner still renders.
        assert "ptest already configured" in captured.out
    assert main((*prefix, "--json")) == 0
    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    key = "root" if command == "where" else "target"
    expected = hostile if command == "where" else hostile / ".ptest.toml"
    assert document.data[key] == str(expected)
    assert captured.err == ""


@pytest.mark.parametrize("command", ["where", "init"])
def test_human_root_is_bounded_in_utf8_bytes_without_truncating_json(
        inspection_project, monkeypatch, capsys, command):
    domain, root = inspection_project
    parent = domain.root
    for _ in range(10):
        parent /= "界" * 50
    parent.mkdir(parents=True)
    long_root = parent / "project"
    root.rename(long_root)
    monkeypatch.chdir(long_root)
    prefix = ("--fixture-domain", str(domain.root), command)
    assert main(prefix) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if command == "where":
        assert "[truncated]" in captured.out
    else:
        # The init banner shows repository-relative targets, so a long root
        # name stays out of human output entirely.
        assert "[truncated]" not in captured.out
        assert "ptest already configured" in captured.out
    # The fixed UTF-8 wordmark adds roughly 1.4 KiB; repository-name and
    # hostile-control bounds remain enforced by the assertions above/below.
    assert len(captured.out.encode()) < 2048
    assert main((*prefix, "--json")) == 0
    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.data["root" if command == "where" else "target"] == str(
        long_root if command == "where" else long_root / ".ptest.toml")
    assert captured.err == ""


def test_runner_registry_is_closed_and_generic_is_exclusive():
    assert registered_kinds() == tuple(C.RunnerKind)
    assert adapter_for(C.RunnerKind.COMMAND).requires_exclusive(
        _command_config())
    with pytest.raises(C.Problem) as exc:
        adapter_for("third-party-plugin")
    assert exc.value.code == "unsupported-capability"


def _command_config():
    return C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.COMMAND, launcher=("echo",)),
        setup=None, resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
    )


def _write_workers_eight_config(root, kind):
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f'kind = "{kind}"\n'
        f'launcher = {json.dumps(["python"] if kind == "pytest" else ["echo"])}\n'
        'args = ["-q"]\n'
        'full_args = []\n'
        'test_roots = ["tests"]\n'
        "workers = 8\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("command", ["where", "register"])
def test_pytest_command_summaries_enforce_serial_workers(case, monkeypatch, capsys, command):
    """A configured workers=8 must still serialize as 1 in Pytest summaries."""
    import socket
    import subprocess

    domain = case.domain()
    root = case.project(domain, kind="pytest")
    _write_workers_eight_config(root, "pytest")
    monkeypatch.chdir(root)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("inspection executed a runner"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("inspection made a network request"))
    assert main(("--fixture-domain", str(domain.root), command, "--json")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = C.decode_public_document(captured.out).data
    assert data["initialized"] is True
    assert len(data["commands"]) == 2
    summaries = {item["mode"]: item for item in data["commands"]}
    assert set(summaries) == {"scoped", "full"}
    assert summaries["scoped"]["workers"] == 1
    assert summaries["full"]["workers"] == 1


@pytest.mark.parametrize("command", ["where", "register"])
def test_generic_command_summary_preserves_configured_workers(case, monkeypatch, capsys, command):
    """The serial enforcement is Pytest-only; generic commands keep workers=8."""
    import socket
    import subprocess

    domain = case.domain()
    root = case.project(domain, kind="command")
    _write_workers_eight_config(root, "command")
    monkeypatch.chdir(root)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("inspection executed a runner"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("inspection made a network request"))
    assert main(("--fixture-domain", str(domain.root), command, "--json")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    data = C.decode_public_document(captured.out).data
    assert data["initialized"] is True
    assert data["commands"]
    assert [item["workers"] for item in data["commands"]] == [8] * len(data["commands"])


@pytest.mark.parametrize("fixture", ["non_git", "nested_no_root_config", "over_budget"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_where_describes_unverified_source_evidence_statically(
        case, monkeypatch, capsys, fixture, json_mode):
    """Static where states the full-completion evidence condition without checking it."""
    import socket
    import subprocess

    from ptest import source as source_module

    domain = case.domain()
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    test_roots = ["nested/tests"] if fixture == "nested_no_root_config" else ["tests"]
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["python"]\n'
        "args = []\n"
        "full_args = []\n"
        f"test_roots = {json.dumps(test_roots)}\n"
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    for path in test_roots:
        (root / path).mkdir(parents=True)
    if fixture == "over_budget":
        with (root / "payload.bin").open("wb") as stream:
            stream.truncate(17 * 1024 * 1024)
    monkeypatch.chdir(root)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("where executed a runner"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("where made a network request"))
    monkeypatch.setattr(source_module, "snapshot",
                        lambda *a, **k: pytest.fail("where read source evidence"))
    args = ("where", "--json") if json_mode else ("where",)
    assert main(("--fixture-domain", str(domain.root), *args)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_mode:
        capability = C.decode_public_document(captured.out).data["capability"]
        assert capability["execution"] == "basic_serial"
        text = " ".join(item["message"] for item in capability["limitations"])
    else:
        assert "capability: basic_serial" in captured.out
        text = captured.out
    assert "incomplete/70" in text


def _monorepo_cli_root(base, declarations=("api", "web")):
    (base / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = "
        + json.dumps(list(declarations)) + "\n",
        encoding="utf-8",
    )
    for index, child in enumerate(declarations):
        root = base / child
        (root / "tests").mkdir(parents=True)
        (root / ".ptest.toml").write_text(
            "version = 1\n"
            f'project_id = "{("ab" if index == 0 else "cd") * 16}"\n'
            "[runner]\n"
            'kind = "command"\n'
            'launcher = ["true"]\n'
            "args = []\n"
            "full_args = []\n"
            'test_roots = ["tests"]\n'
            "workers = 1\n"
            'lifecycle = "cooperative-process-group"\n',
            encoding="utf-8",
        )
        (root / "tests" / "cache_test.py").write_text(
            "def test_cache(client):\n    client.flushall()\n", encoding="utf-8")


def test_doctor_from_monorepo_root_renders_declared_rows_and_worksheet(
        tmp_path, monkeypatch, capsys):
    """Root content must not leak into child rows; every child stays numbered."""
    import socket
    import subprocess

    _monorepo_cli_root(tmp_path)
    (tmp_path / "root_noise_test.py").write_text("cache.flushall()\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("doctor executed a runner"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("doctor made a network request"))
    assert main(("doctor", "--offline")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    api_row = next(line for line in captured.out.splitlines() if line.startswith("| 1 | api "))
    web_row = next(line for line in captured.out.splitlines() if line.startswith("| 2 | web "))
    assert captured.out.index(api_row) < captured.out.index(web_row)
    assert "root_noise_test.py" not in captured.out
    assert "api/tests/cache_test.py" in captured.out
    assert "FIX-001" in captured.out and "TIMING-001" in captured.out
    assert "review not yet performed" in captured.out


def test_doctor_json_from_monorepo_root_keeps_single_aggregate_shape(
        tmp_path, monkeypatch, capsys):
    _monorepo_cli_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(("doctor", "--json")) == 0
    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == "doctor" and document.error is None
    assert set(document.data) == {
        "scope", "readiness", "findings", "limits", "usage", "limitations"}
    assert document.data["scope"] == []
    assert [item["area"] for item in document.data["readiness"]] == [
        "execution", "parallel", "selection", "timing"]
    assert all(item["path"].startswith(("api/", "web/"))
               for item in document.data["findings"])


def test_doctor_scope_and_unsafe_scope_exit_codes_from_monorepo_root(
        tmp_path, monkeypatch, capsys):
    _monorepo_cli_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(("doctor", "--offline", "--scope", "api")) == 0
    captured = capsys.readouterr()
    assert "| 1 | api |" in captured.out and "| web |" not in captured.out
    assert main(("doctor", "--offline", "--scope", "ghost")) == 2
    assert "invalid-config" in capsys.readouterr().err
    assert main(("doctor", "--offline", "--scope", "../api")) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert captured.out == ""
    assert main(("doctor", "--offline", "--scope", "api/.ptest.toml")) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert captured.out == ""


def test_doctor_prompt_requests_assessment_without_repair_or_execution(
        tmp_path, monkeypatch, capsys):
    import subprocess

    _monorepo_cli_root(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("prompt executed a runner"))
    assert main(("doctor", "--prompt")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Assessment request:" in captured.out
    assert "assessment authority only" in captured.out
    assert "api/tests/cache_test.py" in captured.out
    assert "FIX-001" in captured.out


def test_doctor_probe_still_executes_without_checklist(
        tmp_path, monkeypatch, capsys):
    calls = []
    _monorepo_cli_root(tmp_path, declarations=("api",))
    monkeypatch.chdir(tmp_path / "api")
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append(request) or type(
            "R", (), {"reasons": (), "exit_code": 0})(),
    )
    assert main(("doctor", "--probe", "--scope", "tests/test_probe.py")) == 0
    captured = capsys.readouterr()
    assert len(calls) == 1 and calls[0].mode is C.Mode.PROBE
    assert "FIX-001" not in captured.out


def test_init_agents_all_uses_the_closed_provider_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    parsed = parse_argv(("init", "--agents", "all"))
    assert parsed.agents == ("claude", "codex", "opencode", "gemini")
    assert parsed.agents_explicit is True

    assert main(("init", "--runner", "pytest", "--agents", "all")) == 0
    captured = capsys.readouterr()
    for relative in (".claude/skills/ptest/SKILL.md",
                     ".agents/skills/ptest/SKILL.md",
                     ".opencode/skills/ptest/SKILL.md",
                     ".gemini/skills/ptest/SKILL.md"):
        assert (tmp_path / relative).is_file()
    assert "ptest initialized" in captured.out


def test_init_json_is_non_interactive_and_byte_exact(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input",
                        lambda *args, **kwargs: pytest.fail("JSON init prompted"))
    assert main(("init", "--runner", "pytest", "--json")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    document = C.decode_public_document(captured.out)
    assert document.kind == "init"
    assert document.error is None
    assert document.data["action"] == "created"
    assert set(document.data) == {"action", "target", "exists", "warnings", "config"}
    assert "ptest initialized" not in captured.out
    assert "██████" not in captured.out
    assert "\x1b" not in captured.out
    assert not (tmp_path / "docs").exists()


def test_init_json_with_explicit_agents_reports_no_prompt(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert main(("init", "--runner", "pytest", "--agents", "claude",
                 "--json")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert C.decode_public_document(captured.out).data["action"] == "created"
    assert (tmp_path / ".claude/skills/ptest/SKILL.md").is_file()


def test_human_init_renders_ordered_banner_matching_real_files(
        tmp_path, monkeypatch, capsys):
    import re

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "claude,codex")) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert f"ptest {C.PTEST_VERSION}" in captured.out
    assert tmp_path.name in captured.out
    assert "https://github.com/Avocado-Blockchain-Services/ptest" in captured.out
    assert "\x1b" not in captured.out
    assert "ptest initialized" in captured.out
    assert "┌" in captured.out and "┘" in captured.out
    assert captured.out.index("Configuration") < captured.out.index("Guidance")
    assert captured.out.index("Guidance") < captured.out.index("Next steps")
    assert re.search(r"created:?\s+\.ptest\.toml", captured.out)
    assert re.search(r"created\s+docs/ptest-agent\.md", captured.out)
    assert ".claude/skills/ptest/SKILL.md" in captured.out
    assert ".agents/skills/ptest/SKILL.md" in captured.out
    assert ".codex/skills/ptest/SKILL.md" not in captured.out
    assert "ptest --full" in captured.out
    assert (tmp_path / ".agents/skills/ptest/SKILL.md").is_file()
    assert (tmp_path / ".claude/skills/ptest/SKILL.md").is_file()


@pytest.mark.parametrize(("no_color", "has_color"), [(False, True), (True, False)])
def test_tty_human_init_colors_banner_only_without_no_color(
        tmp_path, monkeypatch, capsys, no_color, has_color):
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    assert main(("init", "--runner", "pytest")) == 0
    captured = capsys.readouterr()

    assert f"ptest {C.PTEST_VERSION}" in captured.out
    assert tmp_path.name in captured.out
    assert ("\x1b[" in captured.out) is has_color


@pytest.mark.parametrize("no_color", [False, True])
def test_hostile_repository_name_cannot_inject_terminal_structure(
        tmp_path, monkeypatch, capsys, no_color):
    import re
    import sys

    root = tmp_path / "project\x1b[2J\r\nforged"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    assert main(("init", "--runner", "pytest")) == 0
    captured = capsys.readouterr()

    if no_color:
        assert "\x1b" not in captured.out
    else:
        stripped = re.sub(r"\x1b\[[0-9;]*m", "", captured.out)
        assert "\x1b" not in stripped
    assert "\x1b[2J" not in captured.out
    assert "\r" not in captured.out
    assert "\nforged" not in captured.out
    assert any("project" in line and "forged" in line
               for line in captured.out.splitlines())


def test_repeat_human_init_reports_already_configured(tmp_path, monkeypatch, capsys):
    import re

    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "claude")) == 0
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    capsys.readouterr()

    assert main(("init", "--runner", "pytest", "--agents", "claude")) == 0
    captured = capsys.readouterr()

    assert "ptest already configured" in captured.out
    assert "ptest initialized" not in captured.out
    assert re.search(r"unchanged:?\s+\.ptest\.toml", captured.out)
    assert "already present" not in captured.out
    for relative in ("docs/ptest-agent.md", "AGENTS.md",
                     ".claude/skills/ptest/SKILL.md"):
        assert re.search(rf"unchanged +{re.escape(relative)}", captured.out)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*")
            if path.is_file()} == before


def test_existing_invalid_config_never_claims_success(tmp_path, monkeypatch, capsys):
    (tmp_path / ".ptest.toml").write_bytes(b"version = 999\n")
    monkeypatch.chdir(tmp_path)

    assert main(("init", "--runner", "pytest")) == 0
    captured = capsys.readouterr()

    assert "attention" in captured.out.lower()
    assert "ptest initialized" not in captured.out
    assert "ptest already configured" not in captured.out


def test_dry_run_reports_planned_verbs_and_writes_nothing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "codex",
                 "--dry-run")) == 0
    captured = capsys.readouterr()

    assert "ptest init preview" in captured.out
    assert "would create" in captured.out
    assert "created:" not in captured.out
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / ".agents").exists()


def test_guidance_write_failure_emits_no_banner_and_rolls_back(
        tmp_path, monkeypatch, capsys):
    import ptest.agent_rules as rules_module

    real_create = rules_module._create_leaf

    def failing_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            raise C.Problem(code="state-unavailable", message="injected failure",
                            phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_create_leaf", failing_create)
    monkeypatch.chdir(tmp_path)

    assert main(("init", "--runner", "pytest", "--agents", "claude")) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "state-unavailable" in captured.err
    assert "ptest initialized" not in captured.err
    assert (tmp_path / ".ptest.toml").is_file()
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".claude").exists()


def test_human_banner_bounds_deep_paths_without_raw_controls(
        tmp_path, monkeypatch, capsys):
    deep = tmp_path
    for _ in range(12):
        deep /= "nested-project-directory"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    assert main(("init", "--runner", "pytest")) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert len(captured.out.encode("utf-8")) < 8192
    for control in ("\x1b", "\r", "\x00", "\x07"):
        assert control not in captured.out


# --- Agent-doctor parser/help slice: review grammar, closed modes. ---

def _forbid_launch(monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("invalid invocation crossed an execution/network boundary")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("ptest.operations.execute", forbidden)


def test_doctor_parser_defaults_request_review_without_launch_flags():
    parsed = parse_argv(("doctor",))

    assert parsed.command == "doctor"
    assert parsed.reviewer is None
    assert parsed.reviewer_explicit is False
    assert parsed.allow_model_review is False
    assert parsed.assessment_json is False
    assert parsed.review_timeout_s == 300
    assert parsed.review_timeout_explicit is False
    assert parsed.offline is False


@pytest.mark.parametrize("reviewer", ["auto", "claude", "codex", "opencode"])
def test_doctor_parser_accepts_supported_reviewers(reviewer):
    from ptest.agent_providers import SUPPORTED_REVIEWERS

    assert reviewer == "auto" or reviewer in SUPPORTED_REVIEWERS
    parsed = parse_argv(("doctor", "--reviewer", reviewer))

    assert parsed.reviewer == reviewer
    assert parsed.reviewer_explicit is True


def test_doctor_parser_accepts_full_review_option_set():
    parsed = parse_argv(("doctor", "--reviewer", "codex",
                         "--allow-model-review", "--assessment-json",
                         "--review-timeout", "60", "--scope", "tests/x.py"))

    assert parsed.reviewer == "codex"
    assert parsed.allow_model_review is True
    assert parsed.assessment_json is True
    assert parsed.review_timeout_s == 60
    assert parsed.review_timeout_explicit is True
    assert parsed.scope == "tests/x.py"


@pytest.mark.parametrize("reviewer", ["gemini", "CLAUDE", "all", "none"])
def test_doctor_parser_rejects_unsupported_reviewer(reviewer):
    with pytest.raises(C.Problem):
        parse_argv(("doctor", "--reviewer", reviewer))


@pytest.mark.parametrize("timeout", ["9", "901", "0", "abc", "30.5", ""])
def test_doctor_parser_rejects_out_of_bound_review_timeout(timeout):
    with pytest.raises(C.Problem):
        parse_argv(("doctor", "--review-timeout", timeout))


@pytest.mark.parametrize("argv", [
    ("doctor", "--reviewer", "claude", "--reviewer", "codex"),
    ("doctor", "--allow-model-review", "--allow-model-review"),
    ("doctor", "--assessment-json", "--assessment-json"),
    ("doctor", "--review-timeout", "60", "--review-timeout", "61"),
    ("doctor", "--offline", "--offline"),
])
def test_doctor_parser_rejects_repeated_review_options(argv):
    with pytest.raises(C.Problem):
        parse_argv(argv)


@pytest.mark.parametrize("argv", [
    ("doctor", "--probe", "--scope", "tests/a.py", "--reviewer", "claude"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--allow-model-review"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--assessment-json"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--review-timeout", "60"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--offline"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--json"),
    ("doctor", "--offline", "--json"),
    ("doctor", "--offline", "--prompt"),
    ("doctor", "--offline", "--assessment-json"),
    ("doctor", "--offline", "--reviewer", "claude"),
    ("doctor", "--offline", "--allow-model-review"),
    ("doctor", "--offline", "--review-timeout", "60"),
    ("doctor", "--json", "--assessment-json"),
    ("doctor", "--prompt", "--assessment-json"),
    ("doctor", "--json", "--reviewer", "claude"),
    ("doctor", "--prompt", "--reviewer", "codex"),
    ("doctor", "--json", "--allow-model-review"),
    ("doctor", "--prompt", "--allow-model-review"),
    ("doctor", "--json", "--review-timeout", "60"),
    ("doctor", "--prompt", "--review-timeout", "120"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--assessment-json",
     "--reviewer", "auto"),
])
def test_doctor_parser_rejects_incompatible_modes_before_side_effects(argv):
    with pytest.raises(C.Problem):
        parse_argv(argv)


def test_doctor_parser_keeps_legacy_modes():
    assert parse_argv(("doctor", "--json")).json is True
    assert parse_argv(("doctor", "--prompt")).prompt is True
    assert parse_argv(("doctor", "--offline")).offline is True
    assert parse_argv(("doctor", "--offline", "--scope", "tests")).scope == "tests"
    probe = parse_argv(("doctor", "--probe", "--scope", "tests/a.py")).probe
    assert probe is not None and probe.scope == "tests/a.py"


def test_init_parser_defaults_have_no_review_request():
    parsed = parse_argv(("init",))

    assert parsed.doctor_request is None
    assert parsed.reviewer is None
    assert parsed.allow_model_review is False
    assert parsed.review_timeout_s == 300


def test_init_parser_accepts_doctor_offer_and_review_options():
    parsed = parse_argv(("init", "--doctor", "--reviewer", "opencode",
                         "--allow-model-review", "--review-timeout", "120"))

    assert parsed.doctor_request is True
    assert parsed.reviewer == "opencode"
    assert parsed.allow_model_review is True
    assert parsed.review_timeout_s == 120


def test_init_parser_accepts_no_doctor():
    assert parse_argv(("init", "--no-doctor")).doctor_request is False


@pytest.mark.parametrize("argv", [
    ("init", "--doctor", "--doctor"),
    ("init", "--no-doctor", "--no-doctor"),
    ("init", "--doctor", "--no-doctor"),
    ("init", "--no-doctor", "--doctor"),
    ("init", "--doctor", "--reviewer", "claude", "--reviewer", "codex"),
    ("init", "--doctor", "--allow-model-review", "--allow-model-review"),
    ("init", "--doctor", "--review-timeout", "60", "--review-timeout", "61"),
    ("init", "--json", "--doctor"),
    ("init", "--json", "--reviewer", "claude"),
    ("init", "--json", "--allow-model-review"),
    ("init", "--json", "--review-timeout", "60"),
    ("init", "--dry-run", "--doctor"),
    ("init", "--dry-run", "--reviewer", "claude"),
    ("init", "--dry-run", "--allow-model-review"),
    ("init", "--dry-run", "--review-timeout", "60"),
    ("init", "--no-doctor", "--reviewer", "claude"),
    ("init", "--no-doctor", "--allow-model-review"),
    ("init", "--no-doctor", "--review-timeout", "60"),
    ("init", "--reviewer", "gemini"),
    ("init", "--review-timeout", "901"),
    ("init", "--assessment-json"),
])
def test_init_parser_rejects_incompatible_review_modes(argv):
    with pytest.raises(C.Problem):
        parse_argv(argv)


@pytest.mark.parametrize("argv", [
    ("doctor", "--reviewer", "gemini"),
    ("doctor", "--review-timeout", "5"),
    ("doctor", "--probe", "--scope", "tests/a.py", "--reviewer", "claude"),
    ("doctor", "--offline", "--json"),
    ("doctor", "--json", "--assessment-json"),
    ("doctor", "--reviewer", "claude", "--prompt"),
    ("init", "--json", "--doctor"),
    ("init", "--dry-run", "--reviewer", "claude"),
    ("init", "--doctor", "--no-doctor"),
])
def test_invalid_review_invocations_exit_two_without_launch(
        tmp_path, monkeypatch, capsys, argv):
    _forbid_launch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    before = _tree_bytes(tmp_path)

    assert main(argv) == 2

    captured = capsys.readouterr()
    assert captured.out == "" or "reviewer" not in captured.out
    assert captured.err.strip() or captured.out.strip()
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("argv", [
    ("doctor",),
    ("doctor", "--reviewer", "claude"),
    ("doctor", "--reviewer", "auto", "--allow-model-review"),
    ("doctor", "--allow-model-review"),
    ("doctor", "--assessment-json"),
])
def test_doctor_without_full_explicit_consent_requires_consent_before_qualification(
        tmp_path, monkeypatch, capsys, argv):
    """Non-TTY review without both explicit provider and consent is consent-required.

    Consent is checked before provider qualification: even an unqualified
    reviewer selection must still report consent-required here, never
    provider-unqualified. No provider process may start.
    """
    import sys

    _forbid_launch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("prompted without consent"))
    before = _tree_bytes(tmp_path)

    assert main(argv) == 2

    captured = capsys.readouterr()
    assert _tree_bytes(tmp_path) == before
    assert "consent-required" in captured.err + captured.out
    assert "provider-unqualified" not in captured.err + captured.out


def test_explicit_provider_and_consent_with_missing_provider_never_launches(
        tmp_path, monkeypatch, capsys):
    """Explicit reviewer plus consent reaches resolution without assuming PATH.

    PATH is emptied so no real installed provider can satisfy resolution;
    the result must be provider-unavailable/unqualified, never a launch and
    never consent-required.
    """
    import sys

    _forbid_launch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    before = _tree_bytes(tmp_path)

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 2

    captured = capsys.readouterr()
    assert _tree_bytes(tmp_path) == before
    text = captured.err + captured.out
    assert "provider-unqualified" in text or "provider-unavailable" in text
    assert "consent-required" not in text


def test_explicit_assessment_json_without_consent_reports_consent_required(
        tmp_path, monkeypatch, capsys):
    import sys

    _forbid_launch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert main(("doctor", "--assessment-json")) == 2

    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == "doctor"
    assert document.error.code == "consent-required"
    assert document.data is None
    assert captured.err == ""


def test_offline_doctor_stays_static_without_launch(
        inspection_project, monkeypatch, capsys):
    _forbid_launch(monkeypatch)
    domain, _ = inspection_project
    before = _tree_bytes(domain.root)

    assert main(("--fixture-domain", str(domain.root),
                 "doctor", "--offline")) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "cache.global-flush" in captured.out
    assert _tree_bytes(domain.root) == before


def test_init_doctor_without_explicit_consent_keeps_files_and_reports_consent(
        tmp_path, monkeypatch, capsys):
    """Non-TTY init --doctor without consent must not launch a reviewer.

    Init files are still written; the review leg reports consent-required
    and the process returns the review exit code.
    """
    import sys

    _forbid_launch(monkeypatch)
    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert main(("init", "--runner", "pytest", "--doctor",
                 "--reviewer", "claude")) == 2

    captured = capsys.readouterr()
    assert (tmp_path / ".ptest.toml").is_file()
    assert "consent-required" in captured.err + captured.out
    assert "initialization succeeded; review incomplete" in captured.err + captured.out


def test_init_doctor_on_existing_config_without_consent_keeps_bytes_and_reports_consent(
        tmp_path, monkeypatch, capsys):
    """Existing config uses a separate setup step, then init --doctor still gates."""
    import sys

    _forbid_launch(monkeypatch)
    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=8"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert main(("init", "--runner", "pytest")) == 0
    capsys.readouterr()
    before = (tmp_path / ".ptest.toml").read_bytes()

    assert main(("init", "--runner", "pytest", "--doctor",
                 "--reviewer", "claude")) == 2

    captured = capsys.readouterr()
    assert (tmp_path / ".ptest.toml").read_bytes() == before
    assert "consent-required" in captured.err + captured.out


@pytest.mark.parametrize("argv", [
    ("doctor",),
    ("doctor", "--reviewer", "claude"),
    ("doctor", "--reviewer", "auto", "--allow-model-review"),
    ("doctor", "--allow-model-review"),
])
def test_non_tty_consent_gate_precedes_reviewer_resolution_and_source_scan(
        tmp_path, monkeypatch, capsys, argv):
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *a, **k: pytest.fail("resolved reviewer before consent"),
    )
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: pytest.fail("scanned source before consent"),
    )

    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "consent-required" in captured.err


def test_ci_suppresses_tty_prompt_and_requires_explicit_consent_first(
        tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("prompted in CI"))
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *a, **k: pytest.fail("resolved reviewer before consent"),
    )
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: pytest.fail("scanned source before consent"),
    )

    assert main(("doctor",)) == 2
    assert "consent-required" in capsys.readouterr().err


def _fake_reviewer(name, *, qualified):
    from ptest.agent_providers import ReviewerAdapter

    return ReviewerAdapter(name, f"/fake/{name}", (f"/fake/{name}",),
                           qualified=qualified, qualification_note="test")


def test_tty_auto_review_uses_stable_order_skipping_unavailable(
        inspection_project, tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_qualified_profiles(monkeypatch, unqualified=("opencode",))
    resolved = []
    prompts = []

    def resolve(name, env):
        resolved.append(name)
        if name == "claude":
            raise C.Problem(code="provider-unavailable", message="not installed",
                            phase="provider")
        return _fake_reviewer(name, qualified=True)

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    monkeypatch.setattr("builtins.input", lambda: prompts.append("asked") or "yes")
    launches = []
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, pid=3001))

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert prompts == ["asked"]
    assert launches and all(name == "codex" for name, _ in launches)
    assert "Choose a reviewer" not in captured.err
    assert "recommendations.md" in captured.out


def test_tty_auto_with_no_qualified_reviewer_never_prompts_or_scans(
        inspection_project, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    _fake_qualified_profiles(monkeypatch, unqualified=("claude", "codex",
                                                       "opencode"))
    resolved = []

    def resolve(name, env):
        resolved.append(name)
        if name == "codex":
            raise C.Problem(code="provider-unavailable", message="not installed",
                            phase="provider")
        return _fake_reviewer(name, qualified=False)

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("prompted without a qualified reviewer"))
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: pytest.fail("scanned source without a qualified reviewer"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *a, **k: pytest.fail("launched without a qualified reviewer"),
    )

    assert main(("doctor",)) == 2
    assert resolved == []
    assert "provider-unqualified" in capsys.readouterr().err


def test_tty_doctor_discloses_sanitized_bounded_source_once_and_decline_is_offline(
        inspection_project, monkeypatch, capsys):
    import sys

    domain, root = inspection_project
    hostile = root.with_name("project\x1b[31m\nname")
    root.rename(hostile)
    monkeypatch.chdir(hostile)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def resolve(name, env):
        if name != "claude":
            raise C.Problem(code="provider-unavailable",
                            message="not installed", phase="provider")
        return _fake_reviewer("claude", qualified=True)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer", resolve)
    _fake_qualified_profiles(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda: prompts.append(1) or "no")
    scans = []
    real_inspect = __import__("ptest.doctor", fromlist=["inspect_workspace"]).inspect_workspace
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: scans.append(1) or real_inspect(*a, **k),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *a, **k: pytest.fail("decline launched reviewer"),
    )

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    disclosure = captured.err
    assert prompts == [1]
    assert scans == [1, 1]
    assert "claude" in disclosure
    assert r"project\x1b[31m\nname" in disclosure
    assert "bounded source" in disclosure
    assert "existing account" in disclosure
    assert "costs may apply" in disclosure
    assert "cannot perfectly detect secrets" in disclosure
    assert "--offline" in disclosure
    assert "\x1b" not in disclosure
    assert "optimization review is disabled" in (captured.err + captured.out).lower()
    assert "review not yet performed" in captured.out


def test_tty_review_disclosure_names_excluded_source_classes(
        inspection_project, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def resolve(name, env):
        if name != "claude":
            raise C.Problem(code="provider-unavailable",
                            message="not installed", phase="provider")
        return _fake_reviewer("claude", qualified=True)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer", resolve)
    _fake_qualified_profiles(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda: "no")

    assert main(("doctor",)) == 0

    disclosure = capsys.readouterr().err.lower()
    assert "secrets/private files" in disclosure
    assert "agent instructions/configuration" in disclosure
    assert "dependency environments" in disclosure
    assert "coverage/build outputs" in disclosure
    assert "generated/minified files" in disclosure


def test_tty_auto_skips_unqualified_opencode_without_prompting(
        inspection_project, monkeypatch, capsys):
    """Per-provider gate: unqualified opencode never blocks or prompts auto.

    Claude and Codex are qualified but not installed here, so auto finds
    no qualified installed provider and fails closed before any prompt,
    disclosure, or source scan.
    """
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    _fake_qualified_profiles(monkeypatch, unqualified=("opencode",))
    resolved = []
    prompts = []

    def resolve(name, env):
        resolved.append(name)
        raise C.Problem(code="provider-unavailable", message="not installed",
                        phase="provider")

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    monkeypatch.setattr("builtins.input", lambda: prompts.append(1) or "yes")
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: pytest.fail("disabled review scanned after acceptance"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *a, **k: pytest.fail("accepted review launched provider"),
    )

    assert main(("doctor",)) == 2
    assert prompts == []
    assert resolved == ["claude", "codex"]
    err = capsys.readouterr().err
    assert "provider-unqualified" in err
    assert "install claude or codex" in err
    assert "opencode is not supported" in err


def _review_project(root):
    """Create a two-child v2 project for the shared doctor review path."""
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8")
    for name, project_id in (("api", "ab"), ("web", "cd")):
        child = root / name
        child.mkdir()
        (child / ".ptest.toml").write_text(
            "version = 1\n"
            f'project_id = "{project_id * 16}"\n'
            "[runner]\nkind = \"command\"\nlauncher = [\"true\"]\n",
            encoding="utf-8")
        (child / "test_example.py").write_text(
            "def test_example():\n    assert True\n", encoding="utf-8")


def _fake_qualified_profiles(monkeypatch, *, unqualified=()):
    from ptest.agent_providers import QualificationStatus

    statuses = []

    def status(name):
        statuses.append(name)
        return QualificationStatus(
            name=name, qualified=name not in unqualified,
            argv=(name,), note="synthetic qualification result")

    monkeypatch.setattr(
        "ptest.cli.agent_providers.qualification_status", status)
    return statuses


def _fake_cli_executable(tmp_path, monkeypatch, name="claude"):
    executable = tmp_path / "bin" / name
    executable.parent.mkdir(exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent))


def _one_row_reply(request, *, status="unknown"):
    """One-row per-item reply citing the item request's own subset."""
    body = json.loads(request)
    excerpts = body["excerpts"]
    evidence = []
    if status in ("satisfied", "gap", "not-applicable") and excerpts:
        first = excerpts[0]
        evidence = [{key: first[key] for key in (
            "path", "start_line", "end_line", "sha256")}]
    if status in ("satisfied", "gap", "not-applicable") and not evidence:
        status = "unknown"
    return json.dumps({
        "status": status,
        "rationale": "The bounded source evidence does not establish this row.",
        "evidence": evidence,
        "finding": None,
    }).encode("utf-8")


def _ok_item_launches(launches, hook=None, *, pid=1000, status="unknown"):
    """Build a launch_reviews fake answering every item with a valid row."""
    from ptest.agent_providers import ProviderResult

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None):
        results = []
        for request, schema in requests:
            body = json.loads(request)
            if hook is not None:
                hook(adapter, body, request, timeout_s)
            launches.append((adapter.name, body["packet"]["declaration"]))
            results.append(ProviderResult(
                provider=adapter.name, ok=True,
                assessment=_one_row_reply(request, status=status), error="",
                exit_code=0, timed_out=False, cancelled=False,
                truncated=False, pid=pid + len(launches),
                argv=adapter.argv, scratch="/tmp/ptest-review-test"))
            if on_done is not None:
                on_done(len(results) - 1, results[-1])
        return tuple(results)

    return launch_many


def test_unconfigured_review_foregrounds_config_blocker_and_keeps_public_score(
        tmp_path, monkeypatch, capsys):
    import sys
    from ptest.agent_providers import ProviderResult

    root = tmp_path / "project"
    root.mkdir()
    (root / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    argv_log = []

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None):
        results = []
        for request, schema in requests:
            body = json.loads(request)
            assert body["packet"]["scope"] == "."
            assert body["policy"]["item"]["id"]
            argv_log.append(tuple(adapter.argv))
            results.append(ProviderResult(
                provider=adapter.name, ok=True,
                assessment=_one_row_reply(request, status="satisfied"),
                error="", exit_code=0, timed_out=False, cancelled=False,
                truncated=False, pid=8003, argv=adapter.argv,
                scratch="/tmp/ptest-review-test"))
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review")) == 0

    # The parsed flags reach the provider: claude reviews on its haiku alias.
    assert argv_log
    assert all(item[-2:] == ("--model", "haiku") for item in argv_log)

    human = capsys.readouterr()
    assert ". (unknown)" in human.out
    assert "ptest: not runnable: no ptest configuration" in human.out
    assert "run ptest init from the repository root" in human.out
    assert "Checklist review only:" in human.out
    assert "(ptest cannot run this project yet)" in human.out
    assert " of 11 checks confirmed from evidence" in human.out
    assert "Execution verification: not run." in human.out
    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "initialization-required" in report
    assert "ptest is not execution-ready" in report
    assert report.index("initialization-required") < report.index(
        "checks confirmed from evidence")

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 0
    document = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert document.kind == "agent-assessment"
    child = document.data["children"][0]
    assert child["execution"] == {
        "status": "not-executable", "detail": "no ptest configuration",
        "fix": "run ptest init from the repository root",
    }
    assert all(row["label"] for row in child["rows"])
    assert child["score"]["applicable"] == 11
    assert document.data["provider"]["profile"] == (
        "ptest-item-review-v1 model=haiku")
    blocker = next(item for item in document.data["limitations"]
                   if item["code"] == "capability-unsupported")
    assert "initialization-required" in blocker["message"]

    assert main(("doctor", "--json")) == 0
    legacy = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert legacy.kind == "doctor"
    assert "children" not in legacy.data
    assert "provider" not in legacy.data


def test_zero_planned_calls_produce_report_without_disclosure_or_launch(
        tmp_path, monkeypatch, capsys):
    """All-skipped reviews produce a report with no disclosure or launch."""
    import sys
    from ptest import agent_assessment as AA
    from ptest import cli

    root = tmp_path / "all-skipped"
    root.mkdir()
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        "[runner]\nkind = \"command\"\nlauncher = [\"true\"]\n",
        encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    real_plan = AA.plan_item_reviews

    def all_skipped(packet):
        return tuple(
            AA.ItemReview(item_id=review.item_id, label=review.label,
                          scope=review.scope, request=None,
                          schema=review.schema, excerpt_paths=(),
                          skip_reason=AA.SKIP_PREFIX + "synthetic skip")
            for review in real_plan(packet))

    monkeypatch.setattr(cli.agent_assessment, "plan_item_reviews",
                        all_skipped)
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        lambda *a, **k: pytest.fail("zero-call review launched provider"),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda: pytest.fail("zero-call review prompted for disclosure"),
    )

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 0

    document = C.decode_public_document(capsys.readouterr().out)
    assert document.kind == "agent-assessment"
    child = document.data["children"][0]
    assert [row["status"] for row in child["rows"]] == (
        ["not-applicable"] * 11)
    assert child["score"] is None
    assert (root / "recommendations.md").is_file()


def test_only_selected_profile_qualification_gates_source_scan(
        inspection_project, tmp_path, monkeypatch, capsys):
    """Per-provider gate: an unqualified sibling never blocks a qualified pick,
    while the unqualified pick itself fails closed before scan or launch."""
    import sys
    from ptest import cli

    _fake_cli_executable(tmp_path, monkeypatch)
    statuses = _fake_qualified_profiles(monkeypatch, unqualified=("codex",))
    cli._require_review_qualification("claude")
    assert statuses == ["claude"]
    with pytest.raises(C.Problem) as caught:
        cli._require_review_qualification("codex")
    assert caught.value.code == "provider-unqualified"
    statuses.clear()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: pytest.fail("scanned with an unqualified selection"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *a, **k: pytest.fail("launched with an unqualified selection"),
    )

    assert main(("doctor", "--reviewer", "codex",
                 "--allow-model-review")) == 2

    assert statuses == ["codex"]
    text = capsys.readouterr().err
    assert "provider-unqualified" in text


def test_doctor_reviews_children_sequentially_and_publishes_one_document(
        case, tmp_path, monkeypatch, capsys):
    import time
    import sys
    from ptest import recommendations

    domain = case.domain()
    root = case.project(domain)
    _review_project(root)
    (root / "api" / ".env").write_text(
        "API_TOKEN=must-not-enter-review-packet\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    launches = []
    requests = []
    timeouts = []
    publication_deadlines = []
    inspect_count = []
    real_inspect = __import__("ptest.doctor", fromlist=["inspect_workspace"]).inspect_workspace
    real_publish = recommendations.publish_recommendations

    def inspect(*args, **kwargs):
        inspect_count.append(1)
        return real_inspect(*args, **kwargs)

    def hook(adapter, body, request, timeout_s):
        item = body["policy"]["item"]
        assert item["id"] in C.AGENT_ASSESSMENT_CHECKLIST_IDS
        assert body["packet"]["declaration"] in ("api", "web")
        requests.append(request)
        timeouts.append(timeout_s)
        assert adapter.argv[-2:] == ("--model", "haiku")

    def launch_many(adapter, requests_arg, timeout_s, *, concurrency=4,
                    on_done=None):
        assert concurrency == 4
        return _ok_item_launches(launches, hook, pid=1000)(
            adapter, requests_arg, timeout_s, concurrency=concurrency,
            on_done=on_done)

    def publish(root_arg, payload, previous, *, source_proof, **kwargs):
        publication_deadlines.append(kwargs.pop("deadline", None))
        return real_publish(root_arg, payload, previous,
                            source_proof=source_proof, **kwargs)

    monkeypatch.setattr("ptest.cli.doctor.inspect_workspace", inspect)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)
    monkeypatch.setattr(recommendations, "publish_recommendations", publish)

    before = time.monotonic()
    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json", "--review-timeout", "60")) == 0
    after = time.monotonic()

    captured = capsys.readouterr()
    document = C.decode_public_document(captured.out)
    assert document.kind == "agent-assessment"
    assert [child["scope"] for child in document.data["children"]] == ["api", "web"]
    assert document.data["provider"]["name"] == "claude"
    assert document.data["provider"]["profile"] == (
        "ptest-item-review-v1 model=haiku")
    assert document.data["publication"]["status"] == "created"
    assert set(document.data) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert document.data["schema"] == C.AGENT_ASSESSMENT_SCHEMA
    assert document.data["children"][0]["score"] == {
        "satisfied": 0, "applicable": 11, "percent": 0,
    }
    for child in document.data["children"]:
        assert child["execution"]["status"] == "caveat"
        assert all(row["label"] for row in child["rows"])
    assert "execution-not-run" in {
        item["code"] for item in document.data["limitations"]
    }
    assert "partial-evidence" in {
        item["code"] for item in document.data["children"][0]["limitations"]
    }
    declarations = [declaration for _, declaration in launches]
    api_count = declarations.count("api")
    web_count = declarations.count("web")
    assert api_count > 0 and web_count > 0
    assert declarations == ["api"] * api_count + ["web"] * web_count
    assert timeouts and all(value == 60 for value in timeouts)
    assert len(publication_deadlines) == 1
    assert publication_deadlines[0] is not None
    assert before <= publication_deadlines[0] - 1800 <= after
    assert len(requests) == api_count + web_count
    assert all(b"must-not-enter-review-packet" not in item for item in requests)
    assert inspect_count == [1, 1]
    assert (root / "recommendations.md").is_file()
    assert captured.err


def test_all_item_failure_keeps_prior_report_and_emits_no_assessment(
        case, tmp_path, monkeypatch, capsys):
    import sys
    from ptest.agent_providers import ProviderResult

    domain = case.domain()
    root = case.project(domain)
    _review_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    original = b"custom report owned by the user\n"
    (root / "recommendations.md").write_bytes(original)
    launches = []

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None):
        results = []
        for request, schema in requests:
            body = json.loads(request)
            launches.append(body["packet"]["declaration"])
            results.append(ProviderResult(
                provider=adapter.name, ok=False, assessment=b"",
                error="provider-failed", exit_code=7, timed_out=False,
                cancelled=False, truncated=False, pid=2002,
                argv=adapter.argv, scratch="/tmp/ptest-review-test"))
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 2

    captured = capsys.readouterr()
    failure_document = C.decode_public_document(captured.out)
    assert failure_document.data is None
    assert failure_document.error.code == "provider-failed"
    assert "doctor review: reviewing" in captured.err
    assert launches
    declarations = [declaration for declaration in launches]
    api_count = declarations.count("api")
    web_count = declarations.count("web")
    assert api_count > 0 and web_count > 0
    assert declarations == ["api"] * api_count + ["web"] * web_count
    assert (root / "recommendations.md").read_bytes() == original


@pytest.mark.parametrize("mutation", ["source", "config", "invalid-config"])
def test_revalidation_rejects_source_or_config_drift_before_publication(
        case, tmp_path, monkeypatch, capsys, mutation):
    import sys

    domain = case.domain()
    root = case.project(domain)
    _review_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    original = b"custom report owned by the user\n"
    (root / "recommendations.md").write_bytes(original)
    launches = []
    inspect_count = []
    real_inspect = __import__("ptest.doctor", fromlist=["inspect_workspace"]).inspect_workspace

    def inspect(*args, **kwargs):
        inspect_count.append(1)
        return real_inspect(*args, **kwargs)

    mutated = []

    def hook(adapter, body, request, timeout_s):
        declaration = body["packet"]["declaration"]
        if declaration == "web" and not mutated:
            mutated.append(1)
            if mutation == "source":
                target = root / "web" / "test_example.py"
                target.write_bytes(target.read_bytes() + b"\n# changed during review\n")
            elif mutation == "config":
                target = root / "web" / ".ptest.toml"
                target.write_bytes(target.read_bytes() + b"\n# changed during review\n")
            else:
                (root / "web" / ".ptest.toml").write_bytes(b"not valid configuration\n")

    monkeypatch.setattr("ptest.cli.doctor.inspect_workspace", inspect)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, hook, pid=4000))

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 2

    captured = capsys.readouterr()
    failure_document = C.decode_public_document(captured.out)
    assert failure_document.data is None
    assert failure_document.error.code == "stale-evidence"
    assert mutated == [1]
    declarations = [declaration for _, declaration in launches]
    api_count = declarations.count("api")
    web_count = declarations.count("web")
    assert api_count > 0 and web_count > 0
    assert declarations == ["api"] * api_count + ["web"] * web_count
    assert inspect_count == [1, 1]
    assert (root / "recommendations.md").read_bytes() == original


def test_scoped_review_launches_rebased_evidence_and_rejects_in_scope_drift(
        case, tmp_path, monkeypatch, capsys):
    import sys

    domain = case.domain()
    root = case.project(domain)
    _review_project(root)
    scoped_source = root / "api" / "tests" / "test_scoped.py"
    scoped_source.parent.mkdir()
    scoped_source.write_text("def test_scoped():\n    assert True\n",
                             encoding="utf-8")
    outside_source = root / "api" / "src" / "outside.py"
    outside_source.parent.mkdir()
    outside_source.write_text("SCOPED_REVIEW_OUTSIDE_SENTINEL_251b\n",
                              encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                      str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    launches = []
    mutate_during_review = [False]
    mutated = []

    def hook(adapter, body, request, timeout_s):
        packet = body["packet"]
        assert packet["declaration"] == "api"
        assert packet["scope"] == "api/tests"
        assert body["policy"]["item"]["id"] in C.AGENT_ASSESSMENT_CHECKLIST_IDS
        assert [excerpt["path"] for excerpt in body["excerpts"]] == [
            "api/tests/test_scoped.py"] or body["excerpts"] == []
        assert b"SCOPED_REVIEW_OUTSIDE_SENTINEL_251b" not in request
        if mutate_during_review[0] and not mutated:
            mutated.append(1)
            scoped_source.write_bytes(
                scoped_source.read_bytes() + b"\n# changed during review\n")

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, hook, pid=4500))

    argv = ("doctor", "--scope", "api/tests", "--reviewer", "claude",
            "--allow-model-review", "--assessment-json")
    assert main(argv) == 0

    public_document = C.decode_public_document(capsys.readouterr().out)
    assert public_document.data["children"][0]["scope"] == "api/tests"
    published = (root / "recommendations.md").read_bytes()
    first_run_launches = len(launches)
    assert first_run_launches > 0

    mutate_during_review[0] = True
    assert main(argv) == 2

    document = C.decode_public_document(capsys.readouterr().out)
    assert document.data is None
    assert document.error.code == "stale-evidence"
    assert len(launches) == 2 * first_run_launches
    assert (root / "recommendations.md").read_bytes() == published


@pytest.mark.parametrize(("case_name", "expected_code", "exit_code"), [
    ("timeout", "provider-failed", 2),
    ("cancel", "review-cancelled", 130),
    ("invalid", "provider-failed", 2),
])
def test_incomplete_or_invalid_provider_result_has_no_report(
        inspection_project, tmp_path, monkeypatch, capsys, case_name,
        expected_code, exit_code):
    import sys
    from ptest.agent_providers import ProviderResult

    _, root = inspection_project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None):
        results = []
        for _request, _schema in requests:
            timed_out = case_name == "timeout"
            cancelled = case_name == "cancel"
            invalid = case_name == "invalid"
            results.append(ProviderResult(
                provider=adapter.name, ok=invalid,
                assessment=b"{}" if invalid else b"",
                error="" if invalid else ("timeout" if timed_out else
                                          "cancelled" if cancelled else "provider-failed"),
                exit_code=0 if invalid else None,
                timed_out=timed_out, cancelled=cancelled, truncated=False,
                pid=5001, argv=adapter.argv,
                scratch="/tmp/ptest-review-test"))
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == exit_code

    captured = capsys.readouterr()
    failure_document = C.decode_public_document(captured.out)
    assert failure_document.data is None
    assert failure_document.error.code == expected_code
    assert not (root / "recommendations.md").exists()


def test_custom_report_conflict_preserves_user_content_after_complete_review(
        inspection_project, tmp_path, monkeypatch, capsys):
    import sys

    _, root = inspection_project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    original = b"human notes: preserve these exactly\n"
    (root / "recommendations.md").write_bytes(original)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches([], pid=6001))

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 2

    captured = capsys.readouterr()
    failure_document = C.decode_public_document(captured.out)
    assert failure_document.data is None
    assert failure_document.error.code == "report-conflict"
    assert (root / "recommendations.md").read_bytes() == original




def test_total_review_deadline_prevents_late_provider_launch(
        inspection_project, tmp_path, monkeypatch, capsys):
    import sys
    from ptest import cli

    _, root = inspection_project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    monkeypatch.setattr(cli, "_REVIEW_TOTAL_TIMEOUT_S", 0)
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        lambda *a, **k: pytest.fail("provider launched after total deadline"),
    )

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 124

    document = C.decode_public_document(capsys.readouterr().out)
    assert document.data is None
    assert document.error.code == "review-timeout"
    assert not (root / "recommendations.md").exists()


@pytest.mark.parametrize(("timeout_phase", "expected_launches"), [
    ("collecting", 0),
    ("revalidating", None),
])
def test_packet_collection_timeout_prevents_late_provider_and_preserves_report(
        inspection_project, tmp_path, monkeypatch, capsys, timeout_phase,
        expected_launches):
    import sys
    from ptest import cli

    _, root = inspection_project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    original_report = b"keep the last complete report exactly\n"
    report = root / "recommendations.md"
    report.write_bytes(original_report)

    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_REVIEW_TOTAL_TIMEOUT_S", 10)
    real_build_packets = cli.agent_assessment.build_packets
    build_count = 0
    review_deadline = []
    launches = []
    launch_moments = []

    def build_packets(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        review_deadline.append(kwargs["deadline"])
        if ((timeout_phase == "collecting" and build_count == 1)
                or (timeout_phase == "revalidating" and build_count == 2)):
            clock[0] = kwargs["deadline"]
        return real_build_packets(*args, **kwargs)

    def hook(adapter, body, request, timeout_s):
        launch_moments.append(clock[0])
        assert review_deadline and clock[0] < review_deadline[0]

    monkeypatch.setattr(cli.agent_assessment, "build_packets", build_packets)
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        _ok_item_launches(launches, hook, pid=8001))

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 124

    document = C.decode_public_document(capsys.readouterr().out)
    assert document.data is None
    assert document.error.code == "review-timeout"
    if expected_launches is None:
        assert launch_moments
    else:
        assert launch_moments == []
        assert launches == []
    assert report.read_bytes() == original_report


def test_non_tty_packet_collection_and_revalidation_emit_15_second_heartbeats(
        inspection_project, tmp_path, monkeypatch, capsys):
    import sys
    from ptest import cli

    _, root = inspection_project
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)

    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_REVIEW_TOTAL_TIMEOUT_S", 100)
    real_build_packets = cli.agent_assessment.build_packets

    def build_packets(*args, **kwargs):
        pulse = kwargs["progress"]
        pulse()
        clock[0] += 15
        pulse()
        return real_build_packets(*args, **kwargs)

    monkeypatch.setattr(cli.agent_assessment, "build_packets", build_packets)
    monkeypatch.setattr(
        cli.agent_providers, "launch_reviews",
        _ok_item_launches([], pid=8002))

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 0

    stderr = capsys.readouterr().err
    assert sum("collecting |" in line for line in stderr.splitlines()) >= 2
    assert sum("validating |" in line for line in stderr.splitlines()) >= 2
    assert "elapsed=15s" in stderr
    assert "%" not in stderr


def test_valid_multi_child_report_proofs_above_256_publish_without_loss(
        case, tmp_path, monkeypatch, capsys):
    import sys
    from ptest import cli
    from ptest import recommendations

    domain = case.domain()
    root = case.project(domain)
    children = [f"child{index}" for index in range(5)]
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = "
        + json.dumps(children) + "\n", encoding="utf-8")
    for index, name in enumerate(children):
        child = root / name
        child.mkdir()
        project_id = f"{index + 1:02x}" * 16
        (child / ".ptest.toml").write_text(
            "version = 1\n"
            f'project_id = "{project_id}"\n'
            "[runner]\nkind = \"command\"\nlauncher = [\"true\"]\n",
            encoding="utf-8")
        for source_index in range(51):
            (child / f"module_{source_index:02d}.py").write_text(
                "VALUE = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _fake_cli_executable(tmp_path, monkeypatch)
    _fake_qualified_profiles(monkeypatch)
    launched = []
    collected_packets = []
    published_proofs = []
    real_publish = recommendations.publish_recommendations
    real_build_packets = cli.agent_assessment.build_packets

    def build_packets(*args, **kwargs):
        packets = real_build_packets(*args, **kwargs)
        if not collected_packets:
            collected_packets.append(packets)
        return packets

    def publish(root_arg, payload, previous, *, source_proof, **kwargs):
        published_proofs.append(len(source_proof))
        return real_publish(root_arg, payload, previous,
                            source_proof=source_proof, **kwargs)

    monkeypatch.setattr(cli.agent_assessment, "build_packets",
                        build_packets)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launched, pid=7000))
    monkeypatch.setattr(recommendations, "publish_recommendations", publish)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 0

    document = C.decode_public_document(capsys.readouterr().out)
    assert document.data is not None
    declarations = [declaration for _, declaration in launched]
    counts = [declarations.count(name) for name in children]
    assert all(count > 0 for count in counts)
    assert declarations == [
        name for name, count in zip(children, counts)
        for _ in range(count)]
    assert collected_packets
    assert published_proofs == [sum(
        len(packet.excerpts) for packets in collected_packets
        for packet in packets)]
    assert published_proofs[0] > 256
    assert (root / "recommendations.md").is_file()


# --- Per-provider qualification (2026-09-23 record): explicit, auto, e2e. ---

def _sentinel_bin(tmp_path, monkeypatch, name, sentinel):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    executable = bindir / name
    executable.write_text(
        "#!/bin/sh\ntouch \"" + str(sentinel) + "\"\ncat >/dev/null\n"
        "printf '{\"result\": \"SHOULD-NEVER-RUN\"}'\nexit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def test_opencode_explicit_selection_fails_closed_without_launch(
        tmp_path, monkeypatch, capsys):
    """Explicit opencode plus consent must fail closed before any launch.

    The fake executable is never run: the sentinel file stays absent and
    the report names provider-unqualified with the free-tier reason.
    """
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    sentinel = tmp_path / "opencode-launched"
    _sentinel_bin(tmp_path, monkeypatch, "opencode", sentinel)

    assert main(("doctor", "--reviewer", "opencode",
                 "--allow-model-review")) == 2

    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert "provider-unqualified" in text
    assert "FreeTierError" in text
    assert not sentinel.exists()


def test_auto_with_only_opencode_installed_fails_closed(
        tmp_path, monkeypatch, capsys):
    """Auto never selects the unqualified provider, even when it is alone."""
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("auto prompted"))
    sentinel = tmp_path / "opencode-launched"
    _sentinel_bin(tmp_path, monkeypatch, "opencode", sentinel)

    assert main(("doctor", "--reviewer", "auto",
                 "--allow-model-review")) == 2

    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert "provider-unqualified" in text
    assert not sentinel.exists()


def test_auto_with_codex_and_opencode_picks_codex(
        inspection_project, tmp_path, monkeypatch, capsys):
    """Auto skips missing claude and unqualified opencode, selecting codex."""
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("codex", "opencode"):
        executable = bindir / name
        executable.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n",
                              encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr("builtins.input", lambda: "yes")
    launches = []

    def hook(adapter, body, request, timeout_s):
        assert adapter.qualified is True

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, hook, pid=3009))

    assert main(("doctor", "--reviewer", "auto",
                 "--allow-model-review")) == 0

    assert launches and all(name == "codex" for name, _ in launches)


# --- Interactive reviewer choice: TTY menu when reviewer is absent/auto. ---

def _tty_menu_review(monkeypatch, tmp_path, *, answers,
                     unqualified=("opencode",), missing=()):
    """Fake qualified profiles plus resolve; feed answers to each input().

    OpenCode stays unqualified by default, mirroring the real record.
    Returns (resolved, inputs, launches). An answer of EOF raises EOFError.
    """
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    _fake_qualified_profiles(monkeypatch, unqualified=unqualified)
    resolved = []

    def resolve(name, env):
        resolved.append(name)
        if name in missing:
            raise C.Problem(code="provider-unavailable",
                            message="not installed", phase="provider")
        return _fake_reviewer(name, qualified=True)

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    inputs = []
    iterator = iter(answers)

    def fake_input(*args):
        inputs.append(args)
        answer = next(iterator)
        if answer is EOFError:
            raise EOFError("no more input")
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    launches = []
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, pid=3100))
    return resolved, inputs, launches


@pytest.mark.parametrize("argv", [
    ("doctor",),
    ("doctor", "--reviewer", "auto"),
])
def test_tty_menu_lists_two_in_order_and_selects_second(
        inspection_project, tmp_path, monkeypatch, capsys, argv):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=["2", "yes"])

    assert main(argv) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches and all(name == "codex" for name, _ in launches)
    assert len(inputs) == 2
    assert "Choose a reviewer for this review:" in captured.err
    assert captured.err.index("1) claude") < captured.err.index("2) codex")
    assert "Run this review once?" in captured.err
    assert "recommendations.md" in captured.out


def test_tty_menu_select_first_then_decline_is_offline(
        inspection_project, tmp_path, monkeypatch, capsys):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=["1", "n"])

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches == []
    assert len(inputs) == 2
    assert "Choose a reviewer for this review:" in captured.err
    assert "Model review disclosure: claude" in captured.err
    assert "optimization review is disabled" in captured.err.lower()
    assert "review not yet performed" in captured.out


@pytest.mark.parametrize("answer", ["", "9", "x", EOFError])
def test_tty_menu_decline_variants_skip_disclosure_scan_and_launch(
        inspection_project, tmp_path, monkeypatch, capsys, answer):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=[answer])
    scans = []
    real_inspect = __import__("ptest.doctor", fromlist=["inspect_workspace"]).inspect_workspace
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *a, **k: scans.append(1) or real_inspect(*a, **k),
    )

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches == []
    assert len(inputs) == 1
    assert scans == [1]
    assert "Choose a reviewer for this review:" in captured.err
    assert "Run this review once?" not in captured.err
    assert "optimization review is disabled" in captured.err.lower()
    assert "review not yet performed" in captured.out


def test_tty_single_installed_reviewer_skips_menu(
        inspection_project, tmp_path, monkeypatch, capsys):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=["yes"], missing=("claude", "opencode"))

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches and all(name == "codex" for name, _ in launches)
    assert len(inputs) == 1
    assert "Choose a reviewer" not in captured.err
    assert "Model review disclosure: codex" in captured.err


def test_tty_menu_never_lists_unqualified_opencode(
        inspection_project, tmp_path, monkeypatch, capsys):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=["1", "yes"],
        unqualified=("opencode",))

    assert main(("doctor",)) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches and all(name == "claude" for name, _ in launches)
    assert "1) claude" in captured.err
    assert "2) codex" in captured.err
    assert "opencode" not in captured.err


def test_tty_explicit_reviewer_skips_menu(
        inspection_project, tmp_path, monkeypatch, capsys):
    resolved, inputs, launches = _tty_menu_review(
        monkeypatch, tmp_path, answers=["yes"])

    assert main(("doctor", "--reviewer", "claude")) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude"]
    assert launches and all(name == "claude" for name, _ in launches)
    assert len(inputs) == 1
    assert "Choose a reviewer" not in captured.err
    assert "Model review disclosure: claude" in captured.err


def test_tty_init_doctor_menu_selects_codex_without_consent_prompt(
        tmp_path, monkeypatch, capsys):
    """TTY init --doctor --allow-model-review shows the menu, not y/N."""
    import sys

    marker = tmp_path / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("ref: refs/heads/main\n")
    (marker / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    _fake_qualified_profiles(monkeypatch, unqualified=("opencode",))
    resolved = []

    def resolve(name, env):
        resolved.append(name)
        return _fake_reviewer(name, qualified=True)

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    inputs = []

    def fake_input(*args):
        inputs.append(args)
        return "2"

    monkeypatch.setattr("builtins.input", fake_input)
    launches = []
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        _ok_item_launches(launches, pid=3100))

    assert main(("init", "--runner", "pytest", "--agents", "none",
                 "--doctor", "--allow-model-review")) == 0

    captured = capsys.readouterr()
    assert resolved == ["claude", "codex"]
    assert launches and all(name == "codex" for name, _ in launches)
    assert len(inputs) == 1
    assert "Choose a reviewer for this review:" in captured.err
    assert "Run this review once?" not in captured.err
    assert "recommendations.md" in captured.out


def _native_replay_executable(bindir, name):
    """Fake provider that wraps a one-row reply in its native envelope."""
    import sys as _sys

    script = "\n".join([
        "#!" + _sys.executable,
        "import json, sys",
        "request = json.load(sys.stdin)",
        "item_id = request['policy']['item']['id']",
        "assert item_id in " + repr(list(C.AGENT_ASSESSMENT_CHECKLIST_IDS)),
        "reply = {'status': 'unknown',",
        "        'rationale': 'The bounded source evidence does not establish this row.',",
        "        'evidence': [], 'finding': None}",
        "payload = json.dumps(reply)",
    ])
    if name == "claude":
        script += "\n" + "\n".join([
            "envelope = {'type': 'result', 'subtype': 'success',",
            "            'is_error': False, 'num_turns': 1,",
            "            'permission_denials': [], 'result': payload}",
            "sys.stdout.write(json.dumps(envelope))",
        ])
    else:
        script += "\n" + "\n".join([
            "for event in ({'type': 'thread.started', 'thread_id': 'THREAD-E2E'},",
            "              {'type': 'turn.started'},",
            "              {'type': 'item.completed',",
            "               'item': {'id': 'item_0', 'type': 'agent_message',",
            "                        'text': payload}},",
            "              {'type': 'turn.completed', 'usage': {}}):",
            "    sys.stdout.write(json.dumps(event) + '\\n')",
        ])
    executable = bindir / name
    executable.write_text(script + "\n", encoding="utf-8")
    executable.chmod(0o755)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_native_envelope_end_to_end_publishes_report(
        tmp_path, monkeypatch, capsys, provider):
    """Real per-item launches against a fake native CLI publish the report.

    Each fake executable replays a valid one-row reply inside its
    provider's native envelope; review runs non-interactively and
    writes recommendations.md.
    """
    import re
    import sys

    root = tmp_path / "project"
    root.mkdir()
    (root / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _native_replay_executable(bindir, provider)
    monkeypatch.setenv("PATH", str(bindir))

    assert main(("doctor", "--reviewer", provider,
                 "--allow-model-review")) == 0

    capsys.readouterr()
    report = root / "recommendations.md"
    assert report.is_file()
    marker, _body = report.read_bytes().split(b"\n", 1)
    assert re.fullmatch(rb"<!-- ptest-recommendations v1 sha256=[0-9a-f]{64} -->",
                        marker) is not None


_RECORDED_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "agent_assessment"


def _write_recorded_demo_project(root):
    """Rebuild the round-4 synthetic root project byte-identically.

    Sources the four admitted files from the vendored request packet, then
    adds one NUL-bearing file the packer must skip so the live packet
    carries ``excluded_count == 1`` — the shape that makes ptest emit its
    own partial-evidence limitation scoped ``"."``.
    """
    body = json.loads(
        (_RECORDED_FIXTURE_DIR / "e2e-demo-request.json")
        .read_text(encoding="utf-8"))["packet"]
    wanted = {".ptest.toml", "pyproject.toml", "src/demo/__init__.py",
              "tests/test_demo.py"}
    for item in body["excerpts"]:
        if item["path"] in wanted:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["text"], encoding="utf-8")
    (root / "notes.bin").write_bytes(b"binary-prefix\x00binary-tail\n")


def _recorded_replay_executable(bindir, name, reply):
    """Fake provider replaying one vendored real row per item, rebound live.

    The recorded row matching the requested item id replays verbatim,
    with citations rebound to the item subset; rows citing outside the
    subset keep their stale binding and become invalid replies, as in
    the per-item contract test.
    """
    import sys as _sys

    script = "\n".join([
        "#!" + _sys.executable,
        "import json, sys",
        "fixture = " + repr(str(_RECORDED_FIXTURE_DIR / reply)),
        "request = json.load(sys.stdin)",
        "item_id = request['policy']['item']['id']",
        "recorded = json.load(open(fixture, encoding='utf-8'))",
        "child = recorded['data']['children'][0]",
        "row = next(r for r in child['rows'] if r['id'] == item_id)",
        "by_path = {e['path']: e for e in request['excerpts']}",
        "def rebind(items):",
        "    for cit in items:",
        "        excerpt = by_path.get(cit['path'])",
        "        if excerpt is None:",
        "            continue",
        "        cit['sha256'] = excerpt['sha256']",
        "        cit['start_line'] = max(excerpt['start_line'],",
        "            min(cit['start_line'], excerpt['end_line']))",
        "        cit['end_line'] = max(cit['start_line'],",
        "            min(cit['end_line'], excerpt['end_line']))",
        "rebind(row.get('evidence', []))",
        "finding = next((f for f in child.get('findings', [])",
        "                if f['id'] == item_id), None)",
        "if finding is not None:",
        "    rebind(finding.get('evidence', []))",
        "    finding = {'summary': finding['summary'],",
        "               'suggested_change': finding['suggested_change'],",
        "               'evidence': finding['evidence']}",
        "reply = {'status': row['status'], 'rationale': row['rationale'],",
        "         'evidence': row['evidence'], 'finding': finding}",
        "payload = json.dumps(reply)",
    ])
    if name == "claude":
        script += "\n" + "\n".join([
            "envelope = {'type': 'result', 'subtype': 'success',",
            "            'is_error': False, 'num_turns': 1,",
            "            'permission_denials': [], 'result': payload}",
            "sys.stdout.write(json.dumps(envelope))",
        ])
    else:
        script += "\n" + "\n".join([
            "for event in ({'type': 'thread.started', 'thread_id': 'THREAD-E2E'},",
            "              {'type': 'turn.started'},",
            "              {'type': 'item.completed',",
            "               'item': {'id': 'item_0', 'type': 'agent_message',",
            "                        'text': payload}},",
            "              {'type': 'turn.completed', 'usage': {}}):",
            "    sys.stdout.write(json.dumps(event) + '\\n')",
        ])
    executable = bindir / name
    executable.write_text(script + "\n", encoding="utf-8")
    executable.chmod(0o755)


@pytest.mark.parametrize("provider,reply", [
    ("claude", "claude-e2e-raw-assessment-3.json"),
    ("codex", "codex-e2e-raw-assessment-1.json"),
])
def test_recorded_round4_replies_publish_report(
        tmp_path, monkeypatch, capsys, provider, reply):
    """End-to-end replay of both round-4 real replies publishes the report.

    Each fake executable replays its vendored real rows one item at a
    time (CLI per-item assembly/render path); review runs
    non-interactively and writes recommendations.md carrying ptest's own
    partial-evidence limitation for the root scope.
    """
    import re
    import sys

    root = tmp_path / "project"
    root.mkdir()
    _write_recorded_demo_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _recorded_replay_executable(bindir, provider, reply)
    monkeypatch.setenv("PATH", str(bindir))

    assert main(("doctor", "--reviewer", provider,
                 "--allow-model-review")) == 0

    capsys.readouterr()
    report = root / "recommendations.md"
    assert report.is_file()
    marker, body = report.read_bytes().split(b"\n", 1)
    assert re.fullmatch(rb"<!-- ptest-recommendations v1 sha256=[0-9a-f]{64} -->",
                        marker) is not None
    assert "partial-evidence" in body.decode("utf-8")


# ---- T6: review model flags, resolution helpers, disclosure seam ---------

def test_review_parser_defaults_keep_model_unset_and_concurrency_four():
    parsed = parse_argv(("doctor",))
    assert parsed.review_model is None
    assert parsed.review_model_explicit is False
    assert parsed.review_concurrency == 4
    assert parsed.review_concurrency_explicit is False


@pytest.mark.parametrize("command", ["doctor", "init"])
def test_review_model_and_concurrency_parse_where_reviewer_is_valid(command):
    if command == "doctor":
        parsed = parse_argv(
            ("doctor", "--review-model", "gpt-5.6-luna",
             "--review-concurrency", "2"))
    else:
        parsed = parse_argv(
            ("init", "--doctor", "--review-model", "gpt-5.6-luna",
             "--review-concurrency", "2"))
    assert parsed.review_model == "gpt-5.6-luna"
    assert parsed.review_model_explicit is True
    assert parsed.review_concurrency == 2
    assert parsed.review_concurrency_explicit is True


@pytest.mark.parametrize("argv", [
    ("doctor", "--review-model", "haiku", "--offline"),
    ("doctor", "--review-concurrency", "2", "--offline"),
    ("doctor", "--review-model", "haiku", "--json"),
    ("doctor", "--review-concurrency", "2", "--json"),
    ("doctor", "--review-model", "haiku", "--prompt"),
    ("doctor", "--review-concurrency", "2", "--prompt"),
    ("doctor", "--review-model", "x", "--probe", "--scope", "tests/a.py"),
    ("doctor", "--review-concurrency", "2", "--assessment-json"),
    ("doctor", "--review-concurrency", "0"),
    ("doctor", "--review-concurrency", "9"),
    ("doctor", "--review-concurrency", "many"),
    ("doctor", "--review-model", "a", "--review-model", "b"),
    ("doctor", "--review-concurrency", "2", "--review-concurrency", "3"),
    ("init", "--doctor", "--review-model", "haiku", "--json"),
    ("init", "--doctor", "--review-concurrency", "2", "--json"),
    ("init", "--dry-run", "--review-model", "haiku"),
    ("init", "--dry-run", "--review-concurrency", "2"),
    ("init", "--review-model", "haiku"),
    ("init", "--review-concurrency", "2"),
])
def test_review_model_flag_invalid_combos_exit_two_without_launch(
        tmp_path, monkeypatch, capsys, argv):
    _forbid_launch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() or captured.out.strip()


def test_declared_review_model_prefers_flag_then_env_then_alias():
    from ptest import cli

    assert cli._declared_review_model(
        "codex", "flag-model", {"PTEST_REVIEW_MODEL": "env-model"}) == \
        "flag-model"
    assert cli._declared_review_model(
        "codex", None, {"PTEST_REVIEW_MODEL": "env-model"}) == "env-model"
    assert cli._declared_review_model("claude", None, {}) == "haiku"
    assert cli._declared_review_model("codex", None, {}) is None
    assert cli._declared_review_model("codex", None,
                                      {"PTEST_REVIEW_MODEL": ""}) is None


def test_declared_review_model_starts_no_subprocess(monkeypatch):
    import subprocess
    from ptest import cli

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("declared model launched a child"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: pytest.fail("declared model ran a child"))
    assert cli._declared_review_model(
        "claude", None, {"PTEST_REVIEW_MODEL": "env-model"}) == "env-model"
    assert cli._declared_review_model("codex", None, {}) is None


def _fake_version(monkeypatch, version):
    monkeypatch.setattr("ptest.cli.agent_providers.cli_version",
                        lambda adapter: version)


def _fake_entries(monkeypatch, entries):
    monkeypatch.setattr(
        "ptest.cli.agent_providers._discover_model_entries",
        lambda adapter: tuple(entries))


def _pick_provider(monkeypatch, reply):
    from ptest.agent_providers import ProviderResult

    calls = []

    def fake_launch(adapter, request, schema, timeout_s, progress):
        calls.append(json.loads(request.decode("utf-8")))
        assert request
        assert schema
        return ProviderResult(
            provider=adapter.name, ok=True, assessment=reply,
            error="", exit_code=0, timed_out=False, cancelled=False,
            truncated=False, pid=4100, argv=adapter.argv,
            scratch="/tmp/ptest-review-test")

    monkeypatch.setattr("ptest.cli.agent_providers.launch_review",
                        fake_launch)
    return calls


def test_resolve_review_model_declared_wins_without_cache(
        tmp_path, monkeypatch):
    from ptest import cli

    _fake_version(monkeypatch, "codex-cli 0.155.1")
    _fake_entries(monkeypatch, [])
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_review",
        lambda *a, **k: pytest.fail("declared model must not pick"))
    adapter = _fake_reviewer("codex", qualified=True)
    model, version = cli._resolve_review_model(
        adapter, tmp_path / "cache", "flag-model")
    assert (model, version) == ("flag-model", "codex-cli 0.155.1")
    assert not (tmp_path / "cache").exists()


def test_resolve_review_model_claude_haiku_runs_no_discovery_or_pick(
        monkeypatch):
    from ptest import cli

    _fake_version(monkeypatch, "claude 1.2.3")
    monkeypatch.setattr(
        "ptest.cli.agent_providers._discover_model_entries",
        lambda adapter: pytest.fail("claude must not discover"))
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_review",
        lambda *a, **k: pytest.fail("claude must not pick"))
    adapter = _fake_reviewer("claude", qualified=True)
    assert cli._resolve_review_model(adapter, Path("/nonexistent"),
                                     "haiku") == ("haiku", "claude 1.2.3")


def test_resolve_review_model_codex_pick_caches_exact_slug(
        tmp_path, monkeypatch):
    from ptest import cli

    _fake_version(monkeypatch, "codex-cli 0.155.1")
    _fake_entries(monkeypatch, [
        {"slug": "gpt-5.6-luna", "display_name": "Luna",
         "description": "Fast and affordable."},
        {"slug": "gpt-5.6-sol", "display_name": "Sol",
         "description": "Frontier."},
    ])
    calls = _pick_provider(monkeypatch, b"gpt-5.6-luna")
    adapter = _fake_reviewer("codex", qualified=True)
    cache = tmp_path / "cache"
    model, version = cli._resolve_review_model(adapter, cache, None)
    assert model == "gpt-5.6-luna"
    assert version == "codex-cli 0.155.1"
    assert len(calls) == 1
    assert [entry["slug"] for entry in calls[0]["models"]] == [
        "gpt-5.6-luna", "gpt-5.6-sol"]
    assert calls[0]["models"][0]["display_name"] == "Luna"
    cached = json.loads((cache / "codex.json").read_text(encoding="utf-8"))
    assert cached == {"cli_version": "codex-cli 0.155.1",
                      "model": "gpt-5.6-luna"}

    _pick_provider(monkeypatch, b"SHOULD-NOT-BE-CALLED")
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_review",
        lambda *a, **k: pytest.fail("cache hit must not pick"))
    again, _ = cli._resolve_review_model(adapter, cache, None)
    assert again == "gpt-5.6-luna"


@pytest.mark.parametrize("reply", [b"gpt-5.6-luna.", b"gpt-5.4",
                                   b"  gpt-5.6-luna  extra  ", b""])
def test_resolve_review_model_codex_pick_requires_exact_slug(
        tmp_path, monkeypatch, reply):
    from ptest import cli

    _fake_version(monkeypatch, "codex-cli 0.155.1")
    _fake_entries(monkeypatch, [
        {"slug": "gpt-5.6-luna", "display_name": "Luna",
         "description": "Fast and affordable."},
    ])
    _pick_provider(monkeypatch, reply)
    adapter = _fake_reviewer("codex", qualified=True)
    cache = tmp_path / "cache"
    assert cli._resolve_review_model(adapter, cache, None) == (
        None, "codex-cli 0.155.1")
    assert not (cache / "codex.json").exists()


def test_resolve_review_model_cache_hits_only_same_cli_version(
        tmp_path, monkeypatch):
    from ptest import cli

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "codex.json").write_text(
        json.dumps({"cli_version": "codex-cli 0.155.1",
                    "model": "gpt-5.6-luna"}), encoding="utf-8")
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_review",
        lambda *a, **k: pytest.fail("cache hit must not pick"))
    adapter = _fake_reviewer("codex", qualified=True)
    _fake_version(monkeypatch, "codex-cli 0.155.1")
    assert cli._resolve_review_model(adapter, cache, None) == (
        "gpt-5.6-luna", "codex-cli 0.155.1")
    _fake_version(monkeypatch, "codex-cli 0.156.0")
    _fake_entries(monkeypatch, [])
    assert cli._resolve_review_model(adapter, cache, None) == (
        None, "codex-cli 0.156.0")


def test_resolve_review_model_fallback_is_not_cached(tmp_path, monkeypatch):
    from ptest import cli

    _fake_version(monkeypatch, None)
    _fake_entries(monkeypatch, [])
    adapter = _fake_reviewer("codex", qualified=True)
    cache = tmp_path / "cache"
    assert cli._resolve_review_model(adapter, cache, None) == (None, None)
    assert not (cache / "codex.json").exists()


def _disclosure_resolution(name="project"):
    from types import SimpleNamespace

    return SimpleNamespace(root=Path(name))


def test_review_disclosure_keeps_first_sentence_without_counts(monkeypatch,
                                                              capsys):
    from ptest import cli

    adapter = _fake_reviewer("codex", qualified=True)
    assert cli._render_review_disclosure(
        adapter, _disclosure_resolution(), ask=False) is True
    captured = capsys.readouterr()
    assert captured.err.startswith(
        "Model review disclosure: codex may receive bounded source text "
        "from project project")


def test_review_disclosure_states_calls_concurrency_and_model(monkeypatch,
                                                             capsys):
    from ptest import cli

    adapter = _fake_reviewer("codex", qualified=True)
    assert cli._render_review_disclosure(
        adapter, _disclosure_resolution(), ask=False, calls=11,
        concurrency=2, model="gpt-5.6-luna") is True
    captured = capsys.readouterr()
    assert captured.err.startswith("Model review disclosure: codex")
    assert ("This review makes 11 model calls (2 at a time) with model "
            "gpt-5.6-luna.") in captured.err


def test_review_disclosure_without_model_names_extra_pick_call(monkeypatch,
                                                              capsys):
    from ptest import cli

    adapter = _fake_reviewer("codex", qualified=True)
    assert cli._render_review_disclosure(
        adapter, _disclosure_resolution(), ask=False, calls=9,
        concurrency=4, model=None) is True
    captured = capsys.readouterr()
    assert "with a model chosen after consent" in captured.err
    assert "one extra call that sends only the model list" in captured.err
