from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from ptest import contracts as C
from ptest.cli import main, parse_argv
from ptest.runners import adapter_for, registered_kinds


def test_execution_parser_stops_at_first_unknown_and_preserves_tail():
    parsed = parse_argv(("--workers", "2", "-k", "--full"))
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.workers == 2
    assert parsed.runner_argv == ("-k", "--full")


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
    ("doctor", ("--probe", "tests/a.py"), "unsupported-capability"),
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
    options = ("--json",) if json_mode else ()
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
        expected = {
            "where": f"root: {root}", "register": "register: initialized",
            "init": f"existing: {root / '.ptest.toml'}",
            "plan": "plan: full (static preview)", "status": "queued: 0\nactive: 0",
            "history": "history: 0 runs", "doctor": "cache.global-flush",
        }
        assert expected[command] in captured.out


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
        ("vitest", ("tests",), False, "unavailable", "prepared only"),
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


def test_guide_print_and_exclusive_export_match_package_resource(
        tmp_path, monkeypatch, capsys):
    from importlib.resources import files

    monkeypatch.chdir(tmp_path)
    bundled = files("ptest").joinpath("resources", "agent-guide.md").read_bytes()
    assert main(("guide",)) == 0
    captured = capsys.readouterr()
    assert captured.out.encode() == bundled
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
            assert captured.out == f"{action}: {target}\n"
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
    assert "\\x1b[31m" in captured.out
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
    assert "[truncated]" in captured.out
    assert len(captured.out.encode()) < 1100
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
