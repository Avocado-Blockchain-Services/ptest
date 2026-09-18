from __future__ import annotations

import json

import pytest

from ptest import contracts as C
from ptest.cli import parse_argv
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


def test_json_error_is_one_descriptor_document(monkeypatch, capsys):
    from ptest.cli import main

    assert main(("where", "--json", "--unknown")) != 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["kind"] == "where"
    assert document["error"]["code"] == "invalid-config"
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
