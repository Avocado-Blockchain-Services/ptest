"""Provider-boundary tests (task 1A owned).

Synthetic fake CLI executables only. Nothing here qualifies a real
provider; real adversarial qualification is a separate required gate.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ptest import agent_providers as ap
from ptest.contracts import Problem

PACKET = b'{"child":"alpha","files":[]}'
SCHEMA = b'{"schema":"ptest.agent-assessment/v1"}'
MARKER = "PTEST-PACKET-MARKER-9f31"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_bin(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


def _env_for(bindir: Path) -> dict[str, str]:
    return {"PATH": str(bindir)}


def _resolve(bindir: Path, name: str, body: str):
    _write_bin(bindir, name, body)
    return ap.resolve_reviewer(name, _env_for(bindir))


def _synthetic(adapter):
    """Mark an adapter qualified for synthetic tests only.

    This does NOT qualify the real provider profile; it only lets the
    subprocess/normalization machinery run against fake executables.
    """
    return dataclasses.replace(
        adapter,
        qualified=True,
        qualification_note="synthetic-test-only; real profile unproven",
    )


def _no_progress(events: list):
    def _cb(event):
        events.append(event)

    return _cb


def _assert_dead(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        pytest.fail("child alive but unowned")
    else:
        pytest.fail(f"child {pid} still alive")


def _neighbor():
    return subprocess.Popen(["sleep", "30"])


# ---- contract surface ----------------------------------------------------

def test_supported_reviewers_exact_order():
    assert ap.SUPPORTED_REVIEWERS == ("claude", "codex", "opencode")


def test_exact_function_signatures():
    assert list(inspect.signature(ap.launch_review).parameters) == [
        "adapter", "packet", "schema", "timeout_s", "progress",
    ]
    assert list(inspect.signature(ap.resolve_reviewer).parameters) == [
        "name", "env",
    ]


def test_resolve_reviewer_rejects_unknown_name(bindir):
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("gemini", _env_for(bindir))
    assert exc.value.code == "provider-unavailable"


def test_resolve_reviewer_missing_executable(bindir):
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("claude", _env_for(bindir))
    assert exc.value.code == "provider-unavailable"


def test_resolve_reviewer_fail_closed_unqualified(bindir):
    _write_bin(bindir, "claude", "#!/bin/sh\nexit 0\n")
    adapter = ap.resolve_reviewer("claude", _env_for(bindir))
    assert adapter.name == "claude"
    assert adapter.qualified is False
    assert tuple(adapter.argv[:1]) != ()
    assert adapter.argv[0].endswith("/claude")
    assert len(adapter.argv) > 1  # fixed containment argv, not bare binary
    assert "--bare" not in adapter.argv  # no auth-disabling fallback


def test_qualification_status_all_unproven(bindir):
    for name in ap.SUPPORTED_REVIEWERS:
        status = ap.qualification_status(name)
        assert status.qualified is False


def test_launch_rejects_unqualified_adapter(bindir):
    _write_bin(bindir, "codex", "#!/bin/sh\nexit 0\n")
    adapter = ap.resolve_reviewer("codex", _env_for(bindir))
    assert adapter.qualified is False
    with pytest.raises(Problem) as exc:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert exc.value.code == "provider-unqualified"


def test_launch_rejects_bad_timeout(bindir):
    _write_bin(bindir, "claude", "#!/bin/sh\nexit 0\n")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(bindir)))
    for bad in (0, -1, 901, "10"):
        with pytest.raises((Problem, TypeError)):
            ap.launch_review(adapter, PACKET, SCHEMA, bad, _no_progress([]))


def test_launch_rejects_oversize_or_empty_inputs(bindir):
    _write_bin(bindir, "claude", "#!/bin/sh\nexit 0\n")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(bindir)))
    with pytest.raises(Problem):
        ap.launch_review(adapter, b"", SCHEMA, 10, _no_progress([]))
    with pytest.raises(Problem):
        ap.launch_review(adapter, PACKET, b"", 10, _no_progress([]))
    with pytest.raises(Problem):
        ap.launch_review(adapter, b"x" * (2 * 1024 * 1024), SCHEMA, 10,
                         _no_progress([]))
    with pytest.raises(TypeError):
        ap.launch_review(adapter, PACKET.decode(), SCHEMA, 10, _no_progress([]))


def test_launch_missing_executable_at_launch(bindir, tmp_path):
    missing = tmp_path / "gone" / "claude"
    adapter = ap.ReviewerAdapter(
        name="claude",
        executable=str(missing),
        argv=(str(missing), "--print"),
        qualified=True,
        qualification_note="synthetic-test-only",
    )
    with pytest.raises(Problem) as exc:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert exc.value.code == "provider-unavailable"


# ---- clean success per provider ------------------------------------------

CLAUDE_OK = "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\nprintf '{\"result\": \"CLAUDE-OK\"}'\nexit 0\n"
CODEX_OK = (
    "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\n"
    "printf '%s\\n' '{\"type\":\"message\",\"content\":\"FIRST\"}' '{\"type\":\"message\",\"content\":\"LAST\"}'\n"
    "exit 0\n"
)
OPENCODE_OK = (
    "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\n"
    "printf '%s\\n' '{\"event\":\"progress\",\"data\":\"50%\"}' '{\"event\":\"result\",\"data\":\"DONE\"}'\n"
    "exit 0\n"
)


def test_claude_clean_success(bindir):
    adapter = _synthetic(_resolve(bindir, "claude", CLAUDE_OK))
    events: list = []
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress(events))
    assert result.ok is True
    assert result.assessment == b"CLAUDE-OK"
    assert result.error == ""
    assert result.exit_code == 0
    assert result.provider == "claude"
    assert any(e.phase == "reviewing" and e.provider == "claude" for e in events)


def test_codex_clean_success_last_message_wins(bindir):
    adapter = _synthetic(_resolve(bindir, "codex", CODEX_OK))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b"LAST"


def test_opencode_clean_success_single_result_event(bindir):
    adapter = _synthetic(_resolve(bindir, "opencode", OPENCODE_OK))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b"DONE"


# ---- argv / environment isolation ----------------------------------------

def test_fixed_argv_and_hostile_packet_never_executes(bindir, tmp_path):
    canary = tmp_path / "pwned"
    hostile = (
        b'{"evil": "$(touch ' + str(canary).encode() + b') ; rm -rf / ' + MARKER.encode() + b'"}'
    )
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "for a in \"$@\"; do case \"$a\" in *" + MARKER + "*) exit 7;; esac; done\n"
        "printf '{\"result\": \"nargs=%s\"}' \"$#\"\nexit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "claude", body))
    result = ap.launch_review(adapter, hostile, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == f"nargs={len(adapter.argv) - 1}".encode()
    assert result.argv == adapter.argv
    assert MARKER not in "\x00".join(result.argv)
    assert not canary.exists()


def test_scratch_cwd_fresh_and_outside_repo(bindir):
    adapter = _synthetic(_resolve(bindir, "claude", CLAUDE_OK))
    first = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    second = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert first.ok and second.ok
    assert first.scratch != second.scratch
    for scratch in (first.scratch, second.scratch):
        assert str(REPO_ROOT) not in scratch
        assert not Path(scratch).exists()  # task-owned dir cleaned up


def test_sanitized_child_env_drops_credentials():
    src = {
        "PATH": "/x",
        "HOME": "/h",
        "TERM": "xterm",
        "ANTHROPIC_API_KEY": "sk-x",
        "OPENAI_API_KEY": "sk-y",
        "GH_TOKEN": "t",
        "AWS_SECRET_ACCESS_KEY": "s",
        "MY_PASSWORD": "p",
        "RANDOM_VAR": "1",
    }
    out = ap.sanitized_child_env(src)
    assert out["PATH"] == "/x"
    assert out["HOME"] == "/h"
    for key in out:
        lowered = key.lower()
        assert "token" not in lowered
        assert "secret" not in lowered
        assert key == "PATH" or "key" not in lowered
        assert "password" not in lowered
        assert "credential" not in lowered
    assert "RANDOM_VAR" not in out


def test_source_packet_never_leaks_to_logs_or_result(bindir, caplog):
    packet = b'{"secret": "' + MARKER.encode() + b'"}'
    body = "#!/bin/sh\ncat >/dev/null\nprintf 'not json{{{' \nexit 0\n"
    adapter = _synthetic(_resolve(bindir, "claude", body))
    with caplog.at_level("INFO"):
        result = ap.launch_review(adapter, packet, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert MARKER not in caplog.text
    assert MARKER not in repr(result)
    assert MARKER.encode() not in result.assessment


# ---- failure normalization -------------------------------------------------

def test_malformed_result_is_failure(bindir):
    body = "#!/bin/sh\ncat >/dev/null\nprintf 'not json{{{' \nexit 0\n"
    adapter = _synthetic(_resolve(bindir, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.assessment == b""
    assert result.error == "invalid-assessment"


def test_duplicate_result_is_failure(bindir):
    body = "#!/bin/sh\ncat >/dev/null\nprintf '{\"results\": [\"A\", \"B\"]}'\nexit 0\n"
    adapter = _synthetic(_resolve(bindir, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_opencode_duplicate_result_events_are_failure(bindir):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"event\":\"result\",\"data\":\"A\"}' '{\"event\":\"result\",\"data\":\"B\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "opencode", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_without_message_is_failure(bindir):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"type\":\"status\",\"content\":\"working\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_claude_tool_attempt_is_failure_not_assessment(bindir):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '{\"result\": \"X\", \"tool_use\": [{\"id\": \"1\"}]}'\nexit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"
    assert result.assessment == b""


def test_codex_tool_call_is_failure(bindir):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"type\":\"tool_call\",\"content\":\"rm\"}' '{\"type\":\"message\",\"content\":\"HI\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_opencode_tool_event_is_failure(bindir):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"event\":\"tool\",\"data\":\"exec\"}' '{\"event\":\"result\",\"data\":\"HI\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "opencode", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_nonzero_exit_is_failure_despite_valid_payload(bindir):
    body = "#!/bin/sh\ncat >/dev/null\nprintf '{\"result\": \"X\"}'\nexit 3\n"
    adapter = _synthetic(_resolve(bindir, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.assessment == b""
    assert result.exit_code == 3


# ---- lifecycle: timeout / cap / cancel ------------------------------------

HANG = "#!/bin/sh\ncat >/dev/null\nsleep 30\nexit 0\n"


def test_timeout_reaps_owned_child_and_spares_neighbor(bindir):
    adapter = _synthetic(_resolve(bindir, "claude", HANG))
    neighbor = _neighbor()
    try:
        result = ap.launch_review(adapter, PACKET, SCHEMA, 2, _no_progress([]))
    finally:
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
    assert result.ok is False
    assert result.timed_out is True
    assert result.error == "timeout"
    _assert_dead(result.pid)
    assert neighbor_alive


def test_stderr_flood_counts_toward_combined_cap(bindir):
    body = ("#!/bin/sh\ncat >/dev/null\n"
            "head -c 700000 /dev/zero | tr '\\0' 'B' 1>&2\nexit 0\n")
    adapter = _synthetic(_resolve(bindir, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 20, _no_progress([]))
    assert result.ok is False
    assert result.truncated is True
    assert result.error == "output-exhausted"
    _assert_dead(result.pid)


def test_output_cap_reaps_owned_child_and_spares_neighbor(bindir):
    body = "#!/bin/sh\ncat >/dev/null\nhead -c 700000 /dev/zero | tr '\\0' 'A'\nexit 0\n"
    adapter = _synthetic(_resolve(bindir, "claude", body))
    neighbor = _neighbor()
    try:
        result = ap.launch_review(adapter, PACKET, SCHEMA, 20, _no_progress([]))
    finally:
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
    assert result.ok is False
    assert result.truncated is True
    assert result.error == "output-exhausted"
    _assert_dead(result.pid)
    assert neighbor_alive


def test_cancel_reaps_owned_child_and_spares_neighbor(bindir):
    adapter = _synthetic(_resolve(bindir, "codex", HANG))
    neighbor = _neighbor()

    def _cancel_immediately(event):
        raise KeyboardInterrupt

    try:
        result = ap.launch_review(adapter, PACKET, SCHEMA, 20, _cancel_immediately)
    finally:
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
    assert result.ok is False
    assert result.cancelled is True
    assert result.error == "cancelled"
    _assert_dead(result.pid)
    assert neighbor_alive


# ---- record validation -----------------------------------------------------

def test_record_validation():
    with pytest.raises(TypeError):
        ap.ReviewerAdapter(name="claude", executable=123, argv=("x",),
                           qualified=True, qualification_note="s")
    with pytest.raises(ValueError):
        ap.ProgressEvent(phase="nope", provider="claude", elapsed_s=0.0)
    with pytest.raises(TypeError):
        ap.ProviderResult(provider="claude", ok=True, assessment="nope",
                          error="", exit_code=0, timed_out=False,
                          cancelled=False, truncated=False,
                          pid=1, argv=("x",), scratch="/tmp/x")
    assert json.dumps({"kinds": list(ap.SUPPORTED_REVIEWERS)})


def test_broken_progress_callback_cannot_fail_review(bindir):
    adapter = _synthetic(_resolve(bindir, "claude", CLAUDE_OK))

    def _broken(event):
        raise RuntimeError("progress sink down")

    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _broken)
    assert result.ok is True
    assert result.assessment == b"CLAUDE-OK"


def test_progress_event_validation_bounds():
    with pytest.raises(ValueError):
        ap.ProgressEvent(phase="reviewing", provider="", elapsed_s=0.0)
    with pytest.raises(TypeError):
        ap.ProgressEvent(phase="reviewing", provider="claude", elapsed_s="0")
