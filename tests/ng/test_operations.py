"""Task11 guarded command execution acceptance fixtures."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ptest import scheduler


_COMMAND_FIXTURE = Path(__file__).parent / "fixtures" / "command" / "command.py"


def _command_project(case, domain, *, args=(), full_args=()):
    root = case.project(domain, kind="command")
    shutil.copy2(_COMMAND_FIXTURE, root / "command.py")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1
    )[1].split('"', 1)[0]
    config = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([sys.executable, 'command.py'])}\n"
        f"args = {json.dumps(list(args))}\n"
        f"full_args = {json.dumps(list(full_args))}\n"
        'kind = "command"\n'
        'lifecycle = "cooperative-process-group"\n'
    )
    (root / ".ptest.toml").write_text(config, encoding="utf-8")
    return root


def test_command_success_preserves_literal_argv_and_streams(case):
    domain = case.domain()
    tokens = ("space value", "quoted 'value'", "--looks-like-a-flag", "$(not shell)", "界")
    root = _command_project(case, domain, args=("literal", *tokens))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == list(tokens)
    assert completed.stderr == b""
    assert completed.result is not None
    assert completed.result["status"] == "passed"
    assert completed.result["runner_exit_code"] == 0
    assert completed.result["counts"] is None
    assert completed.result["full_gate_eligible"] is False
    assert completed.result["baseline_published"] is False
    assert all(token not in json.dumps(completed.result) for token in tokens)


def test_command_preserves_stdout_and_stderr_bytes(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("streams",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    assert completed.stdout == b"literal stdout\n"
    assert completed.stderr == b"literal stderr\n"


@pytest.mark.parametrize("raw, expected", [(23, 23), ("signal", 143)])
def test_command_failure_preserves_native_exit_precedence(case, raw, expected):
    domain = case.domain()
    root = _command_project(case, domain, args=("signal" if raw == "signal" else "exit",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == expected
    assert completed.result is not None
    assert completed.result["status"] == "failed"
    assert completed.result["runner_exit_code"] == (-15 if raw == "signal" else 23)
    assert completed.result["exit_code"] == expected


def test_native_profiles_are_rejected_before_admission(case):
    domain = case.domain()
    root = case.project(domain, kind="pytest")

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 2
    assert b"unsupported-capability" in completed.stderr
    assert completed.result is None


def test_command_scope_appends_literal_tail_and_excludes_full_args(case):
    domain = case.domain()
    root = _command_project(case, domain, full_args=("exit",))

    completed = case.invoke(domain, root, "--", "literal", "--full", "界", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == ["--full", "界"]
    assert completed.result is not None
    assert completed.result["plan"]["execution"] == "scoped"


def test_command_completion_releases_exclusive_lease(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal", "done"))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    leases = scheduler.reconcile(domain)
    assert len(leases) == 1
    assert leases[0].state.value == "RELEASED"
