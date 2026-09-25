"""Provider-boundary tests (task 1A owned).

Synthetic fake CLI executables only. Nothing here qualifies a real
provider; real adversarial qualification is a separate required gate.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ptest import agent_providers as ap
from ptest.contracts import Problem
from factories_agents import (
    agent_codex_stream,
    agent_echo_claude_script,
    fake_provider_env as _env_for,
    write_fake_provider,
)
from support import PYTHON_SHEBANG

PACKET = b'{"child":"alpha","files":[]}'
SCHEMA = b'{"schema":"ptest.agent-assessment/v1"}'
MARKER = "PTEST-PACKET-MARKER-9f31"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(fake_provider_bin: Path, name: str, body: str):
    write_fake_provider(fake_provider_bin, name, script=body)
    return ap.resolve_reviewer(name, _env_for(fake_provider_bin))


def _synthetic(adapter):
    """Mark an adapter qualified for synthetic tests only.

    This does NOT qualify the real provider profile; it only lets the
    subprocess/normalization machinery run against fake executables.
    Only needed for providers the record leaves unqualified (opencode);
    qualified providers already resolve as qualified.
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
        "adapter", "packet", "schema", "timeout_s", "progress", "cancel",
    ]
    assert inspect.signature(ap.launch_review).parameters["cancel"].default is None
    assert inspect.signature(
        ap.launch_review).parameters["cancel"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(ap.resolve_reviewer).parameters) == [
        "name", "env",
    ]
    assert list(inspect.signature(ap.launch_reviews).parameters) == [
        "adapter", "requests", "timeout_s", "concurrency", "on_done",
        "progress", "deadline",
    ]
    assert list(inspect.signature(ap.with_model).parameters) == [
        "adapter", "model",
    ]
    assert list(inspect.signature(ap.discover_models).parameters) == [
        "adapter",
    ]
    assert list(inspect.signature(ap.cli_version).parameters) == [
        "adapter",
    ]


def test_resolve_reviewer_rejects_unknown_name(fake_provider_bin):
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("gemini", _env_for(fake_provider_bin))
    assert exc.value.code == "provider-unavailable"


def test_resolve_reviewer_missing_executable(fake_provider_bin):
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    assert exc.value.code == "provider-unavailable"


def test_resolve_reviewer_fail_closed_unqualified(fake_provider, fake_provider_bin):
    fake_provider("opencode")
    adapter = ap.resolve_reviewer("opencode", _env_for(fake_provider_bin))
    assert adapter.name == "opencode"
    assert adapter.qualified is False
    assert tuple(adapter.argv[:1]) != ()
    assert adapter.argv[0].endswith("/opencode")
    assert len(adapter.argv) > 1  # fixed containment argv, not bare binary
    assert "--bare" not in adapter.argv  # no auth-disabling fallback
    fake_provider("claude")
    qualified = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    assert qualified.qualified is True
    assert qualified.argv[0].endswith("/claude")


def test_qualification_status_per_provider(fake_provider_bin):
    assert ap.qualification_status("claude").qualified is True
    assert ap.qualification_status("codex").qualified is True
    assert ap.qualification_status("opencode").qualified is False


def test_fourth_supported_name_is_unqualified(monkeypatch):
    monkeypatch.setattr(
        ap, "SUPPORTED_REVIEWERS", ("claude", "codex", "opencode", "fourth"))
    status = ap.qualification_status("fourth")
    assert status.qualified is False


def test_launch_rejects_unqualified_adapter(fake_provider, fake_provider_bin):
    fake_provider("opencode")
    adapter = ap.resolve_reviewer("opencode", _env_for(fake_provider_bin))
    assert adapter.qualified is False
    with pytest.raises(Problem) as exc:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert exc.value.code == "provider-unqualified"


def test_launch_rejects_bad_timeout(fake_provider, fake_provider_bin):
    fake_provider("claude")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(fake_provider_bin)))
    for bad in (0, -1, 901, "10"):
        with pytest.raises((Problem, TypeError)):
            ap.launch_review(adapter, PACKET, SCHEMA, bad, _no_progress([]))


def test_launch_rejects_oversize_or_empty_inputs(fake_provider, fake_provider_bin):
    fake_provider("claude")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(fake_provider_bin)))
    with pytest.raises(Problem):
        ap.launch_review(adapter, b"", SCHEMA, 10, _no_progress([]))
    with pytest.raises(Problem):
        ap.launch_review(adapter, PACKET, b"", 10, _no_progress([]))
    with pytest.raises(Problem):
        ap.launch_review(adapter, b"x" * (2 * 1024 * 1024), SCHEMA, 10,
                         _no_progress([]))
    with pytest.raises(TypeError):
        ap.launch_review(adapter, PACKET.decode(), SCHEMA, 10, _no_progress([]))


def test_launch_missing_executable_at_launch(fake_provider_bin, tmp_path):
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

CLAUDE_OK = (
    "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\n"
    "printf '{\"type\": \"result\", \"subtype\": \"success\", \"is_error\": false, "
    "\"num_turns\": 1, \"permission_denials\": [], \"result\": \"CLAUDE-OK\"}'\n"
    "exit 0\n"
)
CODEX_OK = (
    "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\n"
    "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"THREAD-PLACEHOLDER\"}' "
    "'{\"type\":\"turn.started\"}' "
    "'{\"type\":\"item.completed\",\"item\":{\"id\":\"item_0\",\"type\":\"agent_message\",\"text\":\"FIRST\"}}' "
    "'{\"type\":\"item.completed\",\"item\":{\"id\":\"item_1\",\"type\":\"agent_message\",\"text\":\"LAST\"}}' "
    "'{\"type\":\"turn.completed\",\"usage\":{}}'\n"
    "exit 0\n"
)
OPENCODE_OK = (
    "#!/bin/sh\ncat >/dev/null\ntest -f schema.json || exit 5\n"
    "printf '%s\\n' '{\"event\":\"progress\",\"data\":\"50%\"}' '{\"event\":\"result\",\"data\":\"DONE\"}'\n"
    "exit 0\n"
)


def test_claude_clean_success(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    events: list = []
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress(events))
    assert result.ok is True
    assert result.assessment == b"CLAUDE-OK"
    assert result.error == ""
    assert result.exit_code == 0
    assert result.provider == "claude"
    assert any(e.phase == "reviewing" and e.provider == "claude" for e in events)


def test_codex_clean_success_last_message_wins(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", CODEX_OK))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b"LAST"


def test_opencode_clean_success_single_result_event(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "opencode", OPENCODE_OK))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b"DONE"


# ---- argv / environment isolation ----------------------------------------

def test_fixed_argv_and_hostile_packet_never_executes(fake_provider_bin, tmp_path):
    canary = tmp_path / "pwned"
    hostile = (
        b'{"evil": "$(touch ' + str(canary).encode() + b') ; rm -rf / ' + MARKER.encode() + b'"}'
    )
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "for a in \"$@\"; do case \"$a\" in *" + MARKER + "*) exit 7;; esac; done\n"
        "printf '{\"type\": \"result\", \"subtype\": \"success\", \"is_error\": false, "
        "\"num_turns\": 1, \"permission_denials\": [], \"result\": \"nargs=%s\"}' \"$#\"\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    result = ap.launch_review(adapter, hostile, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == f"nargs={len(adapter.argv) - 1}".encode()
    assert result.argv == adapter.argv
    assert MARKER not in "\x00".join(result.argv)
    assert not canary.exists()


def test_scratch_cwd_fresh_and_outside_repo(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
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


def test_source_packet_never_leaks_to_logs_or_result(fake_provider_bin, caplog):
    packet = b'{"secret": "' + MARKER.encode() + b'"}'
    body = "#!/bin/sh\ncat >/dev/null\nprintf 'not json{{{' \nexit 0\n"
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    with caplog.at_level("INFO"):
        result = ap.launch_review(adapter, packet, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert MARKER not in caplog.text
    assert MARKER not in repr(result)
    assert MARKER.encode() not in result.assessment


# ---- failure normalization -------------------------------------------------

def test_malformed_result_is_failure(fake_provider_bin):
    body = "#!/bin/sh\ncat >/dev/null\nprintf 'not json{{{' \nexit 0\n"
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.assessment == b""
    assert result.error == "invalid-assessment"


def test_duplicate_result_is_failure(fake_provider_bin):
    body = "#!/bin/sh\ncat >/dev/null\nprintf '{\"results\": [\"A\", \"B\"]}'\nexit 0\n"
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_opencode_duplicate_result_events_are_failure(fake_provider_bin):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"event\":\"result\",\"data\":\"A\"}' '{\"event\":\"result\",\"data\":\"B\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "opencode", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_without_message_is_failure(fake_provider_bin):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"type\":\"status\",\"content\":\"working\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_claude_tool_attempt_is_failure_not_assessment(fake_provider_bin):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '{\"type\": \"result\", \"subtype\": \"success\", \"is_error\": false, "
        "\"num_turns\": 1, "
        "\"permission_denials\": [{\"tool\": \"Read\"}], \"result\": \"X\"}'\nexit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"
    assert result.assessment == b""


def test_codex_tool_call_is_failure(fake_provider_bin):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' "
        "'{\"type\":\"thread.started\",\"thread_id\":\"THREAD-PLACEHOLDER\"}' "
        "'{\"type\":\"turn.started\"}' "
        "'{\"type\":\"item.completed\",\"item\":{\"id\":\"item_0\",\"type\":\"command_execution\",\"text\":\"rm\"}}' "
        "'{\"type\":\"item.completed\",\"item\":{\"id\":\"item_1\",\"type\":\"agent_message\",\"text\":\"HI\"}}' "
        "'{\"type\":\"turn.completed\",\"usage\":{}}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_opencode_tool_event_is_failure(fake_provider_bin):
    body = (
        "#!/bin/sh\ncat >/dev/null\n"
        "printf '%s\\n' '{\"event\":\"tool\",\"data\":\"exec\"}' '{\"event\":\"result\",\"data\":\"HI\"}'\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "opencode", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_nonzero_exit_is_failure_despite_valid_payload(fake_provider_bin):
    body = "#!/bin/sh\ncat >/dev/null\nprintf '{\"result\": \"X\"}'\nexit 3\n"
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.assessment == b""
    assert result.exit_code == 3


# ---- lifecycle: timeout / cap / cancel ------------------------------------

HANG = "#!/bin/sh\ncat >/dev/null\nsleep 30\nexit 0\n"


def test_timeout_reaps_owned_child_and_spares_neighbor(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", HANG))
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


def test_stderr_flood_counts_toward_combined_cap(fake_provider_bin):
    body = ("#!/bin/sh\ncat >/dev/null\n"
            "head -c 700000 /dev/zero | tr '\\0' 'B' 1>&2\nexit 0\n")
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", body))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 20, _no_progress([]))
    assert result.ok is False
    assert result.truncated is True
    assert result.error == "output-exhausted"
    _assert_dead(result.pid)


def test_output_cap_reaps_owned_child_and_spares_neighbor(fake_provider_bin):
    body = "#!/bin/sh\ncat >/dev/null\nhead -c 700000 /dev/zero | tr '\\0' 'A'\nexit 0\n"
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
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


def test_cancel_reaps_owned_child_and_spares_neighbor(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", HANG))
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


def test_timeout_kills_descendant_after_direct_child_exit(fake_provider_bin, tmp_path):
    """Direct child exits early; same-group descendant must still die.

    Negative contract: timeout cleanup must not skip the owned group just
    because the direct Popen child already exited, and must never touch an
    unrelated neighbor. No provider output is retained.
    """
    pidfile = tmp_path / "descendant.pid"
    body = (
        "#!/bin/sh\ncat >/dev/null\nsleep 30 &\n"
        f"echo $! > {pidfile}\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
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
    assert result.assessment == b""
    _assert_dead(result.pid)
    assert pidfile.exists()
    descendant = int(pidfile.read_text(encoding="utf-8").strip())
    assert descendant not in (result.pid, neighbor.pid)
    for _ in range(50):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pytest.fail("descendant alive but unowned")
        time.sleep(0.1)
    else:
        pytest.fail(f"descendant {descendant} still alive after timeout cleanup")
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
                          pid=1, argv=("x",),
                          # Payload-only scratch label; never a filesystem path.
                          scratch="/tmp/x")
    assert json.dumps({"kinds": list(ap.SUPPORTED_REVIEWERS)})


def test_find_executable_ignores_relative_path_components(tmp_path, monkeypatch):
    """Relative PATH entries must never resolve a cwd-relative binary."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    write_fake_provider(repo / "bin", "claude", script="#!/bin/sh\necho UNTRUSTED\n")
    trusted = tmp_path / "tools"
    trusted.mkdir()
    write_fake_provider(trusted, "claude", script="#!/bin/sh\necho TRUSTED\n")
    monkeypatch.chdir(repo)
    env = {"PATH": os.pathsep.join(["bin", ".", "", str(trusted)])}
    adapter = ap.resolve_reviewer("claude", env)
    assert adapter.executable == str(trusted / "claude")
    assert os.path.isabs(adapter.executable)
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("claude", {"PATH": os.pathsep.join(["bin", "."])})
    assert exc.value.code == "provider-unavailable"


def test_launch_popen_failure_cleans_scratch(fake_provider, fake_provider_bin, monkeypatch):
    """Popen failure after mkdtemp must not leak the owned scratch dir."""
    fake_provider("claude")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(fake_provider_bin)))
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _capture(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(ap.tempfile, "mkdtemp", _capture)

    def _boom(*args, **kwargs):
        raise OSError("injected popen failure")

    monkeypatch.setattr(ap.subprocess, "Popen", _boom)
    with pytest.raises(Problem) as exc:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert exc.value.code == "provider-unavailable"
    assert len(created) == 1
    scratch = created[0]
    assert os.path.basename(scratch).startswith("ptest-review-")
    assert scratch.startswith(tempfile.gettempdir())
    assert not Path(scratch).exists()


def test_broken_progress_callback_cannot_fail_review(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))

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


# ---- pidfd containment regressions (provider reaudit 2026-09-22) ------------
#
# Audit rejected 7e0c184 for two blockers: (1) a numeric member PID validated
# in _owned_group_members can exit and be recycled before the later
# os.kill(SIGTERM/KILL), signaling an unrelated replacement; (2) a missing
# leader_start or member_start is treated as verified ownership. These tests
# pin the fail-closed property: member cleanup must never signal by numeric
# PID, and unreadable identity must raise instead of killing.


def _spawn_leader_with_member():
    """A session leader plus one same-group descendant; caller must reap."""
    leader = subprocess.Popen(["sh", "-c", "sleep 60 & wait"],
                              start_new_session=True)
    pgid = os.getpgid(leader.pid)
    sid = os.getsid(leader.pid)
    member = None
    for _ in range(100):
        for mid in (ap._group_member_pids(pgid) or []):
            if mid != leader.pid:
                member = mid
                break
        if member is not None:
            break
        time.sleep(0.05)
    assert member is not None
    return leader, pgid, sid, member


def _reap_leader_with_member(leader, member):
    try:
        leader.terminate()
    except OSError:
        pass
    try:
        leader.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            leader.kill()
        except OSError:
            pass
        try:
            leader.wait(timeout=5)
        except OSError:
            pass
    try:
        os.kill(member, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def test_stale_member_pid_never_signaled_numerically(fake_provider_bin, tmp_path,
                                                     monkeypatch):
    """A recycled member PID must never receive a stale numeric signal.

    The descendant exits on its own schedule while a neighbor runs. Any
    SIGTERM/SIGKILL addressed by numeric PID to anything but the reaped
    direct child proves the TOCTOU channel is open: after validation that
    number may already belong to someone else. Signaling must go through a
    pinned handle instead.
    """
    pidfile = tmp_path / "descendant.pid"
    body = (
        "#!/bin/sh\ncat >/dev/null\nsleep 30 &\n"
        f"echo $! > {pidfile}\n"
        "exit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    neighbor = _neighbor()
    calls: list = []
    real_kill = os.kill
    real_killpg = os.killpg

    def _spy_kill(pid, sig):
        calls.append(("kill", pid, int(sig)))
        return real_kill(pid, sig)

    def _spy_killpg(pgid, sig):
        calls.append(("killpg", pgid, int(sig)))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", _spy_kill)
    monkeypatch.setattr(os, "killpg", _spy_killpg)
    try:
        result = ap.launch_review(adapter, PACKET, SCHEMA, 2,
                                  _no_progress([]))
    finally:
        monkeypatch.undo()
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
    assert result.ok is False
    assert result.error == "timeout"
    forbidden = [
        call for call in calls
        if call[2] in (signal.SIGTERM, signal.SIGKILL)
        and not (call[0] == "kill" and call[1] == result.pid)
    ]
    assert forbidden == []
    _assert_dead(result.pid)
    assert pidfile.exists()
    descendant = int(pidfile.read_text(encoding="utf-8").strip())
    for _ in range(50):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pytest.fail("descendant alive but unowned")
        time.sleep(0.1)
    else:
        pytest.fail(f"descendant {descendant} still alive after cleanup")
    assert neighbor_alive


def test_missing_leader_starttime_is_not_verified():
    """leader_start=None must fail closed, never count members as owned."""
    leader, pgid, sid, member = _spawn_leader_with_member()
    try:
        with pytest.raises(Problem) as exc:
            ap._owned_group_members(leader.pid, pgid, sid, None)
        assert exc.value.code == "provider-failed"
    finally:
        _reap_leader_with_member(leader, member)


def test_unreadable_member_starttime_is_not_verified(monkeypatch):
    """member_start=None must fail closed, never be appended as owned."""
    leader, pgid, sid, member = _spawn_leader_with_member()
    monkeypatch.setattr(ap, "_proc_starttime", lambda pid: None)
    try:
        with pytest.raises(Problem) as exc:
            ap._owned_group_members(leader.pid, pgid, sid, 12345)
        assert exc.value.code == "provider-failed"
    finally:
        _reap_leader_with_member(leader, member)


def test_timeout_with_unreadable_identity_fails_closed(fake_provider_bin, monkeypatch):
    """Unreadable /proc identity at runtime must raise, never kill blindly."""
    monkeypatch.setattr(ap, "_proc_starttime", lambda pid: None)
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", HANG))
    neighbor = _neighbor()
    try:
        with pytest.raises(Problem) as exc:
            ap.launch_review(adapter, PACKET, SCHEMA, 2, _no_progress([]))
    finally:
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
    assert exc.value.code == "provider-failed"
    assert neighbor_alive


def test_launch_fails_closed_without_pidfd_containment(fake_provider_bin, tmp_path,
                                                       monkeypatch):
    """Without a pinnable handle there is no sound cleanup: do not launch."""
    monkeypatch.setattr(ap, "_PIDFD_AVAILABLE", False)
    canary = tmp_path / "launched"
    body = (
        "#!/bin/sh\ntouch \"" + str(canary) + "\"\ncat >/dev/null\n"
        "printf '{\"result\": \"X\"}'\nexit 0\n"
    )
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", body))
    with pytest.raises(Problem) as exc:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert exc.value.code == "provider-failed"
    assert not canary.exists()


# ---- pidfd lifecycle repair (provider pidfd audit 2026-09-22) ---------------
#
# The pidfd audit rejected two lifecycle defects: (1) the _stop_owned survivor
# loop re-enumerates after SIGKILL but only closes pidfds and waits, so a
# member forked after the SIGKILL enumeration survives; (2)
# _owned_group_members accumulates open pidfds and raises on a later
# unverifiable member without closing the earlier pins. These tests pin the
# repair: survivors must be actively re-signaled through pinned pidfds, and
# partial validation must release earlier pins.


def test_partial_validation_closes_earlier_pins(monkeypatch):
    """A later unverifiable member must not leak earlier pidfd pins.

    Negative contract: fail-closed validation that raises after one member
    passed must still release the already-pinned descriptors; otherwise
    repeated fail-closed cleanups exhaust file descriptors.
    """
    closed: list = []
    monkeypatch.setattr(ap, "_group_member_pids",
                        lambda pgid: [41111, 42222])
    monkeypatch.setattr(
        ap, "_pin_process",
        lambda pid: {41111: 9011, 42222: 9012}[pid])
    monkeypatch.setattr(ap, "_proc_starttime", lambda pid: 200)
    monkeypatch.setattr(ap, "_proc_state", lambda pid: "S")

    def _fake_getsid(pid):
        if pid == 41111:
            return 99999
        raise OSError("injected unreadable sid")

    monkeypatch.setattr(os, "getsid", _fake_getsid)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    with pytest.raises(Problem) as exc:
        ap._owned_group_members(40000, 50000, 99999, 100)
    assert exc.value.code == "provider-failed"
    assert 9012 in closed  # failing member's own pin released
    assert 9011 in closed  # earlier validated pin released, not leaked


def test_survivor_loop_resignals_late_spawn_through_pidfd(monkeypatch):
    """A survivor found after SIGKILL must be signaled, not merely closed.

    Negative contract: the survivor loop must re-enumerate AND actively
    signal every verified survivor through its pinned pidfd until
    quiescent/deadline; only closing the descriptor lets a late-spawned
    owned child survive. No numeric kill of any member is used.
    """
    signals: list = []
    sent: list = []
    monkeypatch.setattr(
        ap, "_signal_pinned",
        lambda proc_pid, pgid, sid, leader_start, sig: signals.append(sig))
    monkeypatch.setattr(
        ap, "_pidfd_signal",
        lambda pidfd, sig: sent.append((pidfd, sig)) or True)
    rounds = [[(77777, 9021)], []]
    monkeypatch.setattr(
        ap, "_owned_group_members",
        lambda *args: rounds.pop(0) if rounds else [])
    monkeypatch.setattr(ap, "_close_pinned", lambda owned: None)

    class _FakeProc:
        pid = 66666

        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("exited direct child must not be signaled")

        def kill(self):
            raise AssertionError("exited direct child must not be signaled")

        def wait(self, timeout=None):
            return 0

    ap._stop_owned(_FakeProc(), 55555, 44444, 100)
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert (9021, signal.SIGKILL) in sent


def test_dead_pin_with_unreadable_starttime_is_skipped_safely(monkeypatch):
    """Exit race: pinned member dies before /proc starttime is readable.

    Positive contract for the fail-closed boundary: a pidfd pin whose
    process already exited (pidfd readable) must be closed and skipped,
    never raised as provider-failed. A LIVE unreadable member must still
    fail closed (see test_unreadable_member_starttime_is_not_verified).
    No numeric signal is sent; the dead pin delivers to nothing.
    """
    closed: list = []
    monkeypatch.setattr(ap, "_group_member_pids", lambda pgid: [41111])
    monkeypatch.setattr(ap, "_pin_process", lambda pid: 9011)
    monkeypatch.setattr(os, "getsid", lambda pid: 99999)
    # Descendant exits between pidfd_open and /proc stat: identity
    # unreadable, but the pin itself is dead (pidfd readable).
    monkeypatch.setattr(ap, "_proc_starttime", lambda pid: None)
    monkeypatch.setattr(ap, "_pidfd_exited", lambda pidfd: True)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    owned = ap._owned_group_members(40000, 50000, 99999, 100)
    assert owned == []
    assert 9011 in closed


# ---- per-provider qualification (2026-09-23 record) -------------------------
#
# Frozen argv tails below are copied from
# docs/research/2026-09-22-agent-provider-qualification.md, section
# "Claude and Codex qualification — 2026-09-23". Any drift fails.

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "agent_providers"

_CLAUDE_FROZEN_TAIL = (
    "--print", "--output-format", "json",
    "--input-format", "text",
    "--safe-mode", "--tools", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
)

_CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "browser_use",
    "browser_use_external", "computer_use", "hooks", "image_generation",
    "in_app_browser", "multi_agent", "plugins", "remote_plugin",
    "plugin_sharing", "skill_search", "skill_mcp_dependency_install",
    "sleep_tool", "tool_suggest", "tool_call_mcp_elicitation",
    "view_image", "code_mode_host", "goals", "guardian_approval",
    "workspace_dependencies", "in_app_chat", "in_app_local_automation",
    "browser_use_full_cdp_access", "unified_exec_tty", "shell_snapshot",
)

_CODEX_FROZEN_TAIL = (
    "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
    "--skip-git-repo-check", "--sandbox", "read-only", "--json",
    "-c", 'web_search="disabled"',
) + tuple(token for feature in _CODEX_DISABLED_FEATURES
          for token in ("--disable", feature))

_INVENTED_FLAGS = ("--allowedTools", "--mcp-config", "--no-slash-commands",
                   "--no-browser", "--no-shell")

_OPENCODE_NOTE = (
    "OpenCode's free tier refuses tool-free runs (HTTP 403 FreeTierError); "
    "not supported for review in this release"
)


def _replay(fake_provider_bin: Path, name: str, payload: bytes):
    """Fake executable that replays one fixed native envelope on stdout."""
    write_fake_provider(fake_provider_bin, name, stdout=payload)
    return ap.resolve_reviewer(name, _env_for(fake_provider_bin))


def _claude_variant(**overrides):
    envelope = json.loads(
        (FIXTURE_DIR / "claude-success.json").read_text(encoding="utf-8"))
    envelope.update(overrides)
    return json.dumps(envelope).encode("utf-8")


def test_frozen_argv_matches_qualification_record(fake_provider, fake_provider_bin):
    for name, tail in (("claude", _CLAUDE_FROZEN_TAIL),
                       ("codex", _CODEX_FROZEN_TAIL)):
        status = ap.qualification_status(name)
        assert tuple(status.argv) == (name,) + tuple(tail)
        fake_provider(name)
        adapter = ap.resolve_reviewer(name, _env_for(fake_provider_bin))
        assert tuple(adapter.argv[1:]) == tuple(tail)
        for flag in _INVENTED_FLAGS:
            assert flag not in adapter.argv
        assert "--bare" not in adapter.argv


def test_qualification_status_names_the_record():
    claude = ap.qualification_status("claude")
    assert claude.qualified is True
    assert "2026-09-23" in claude.note and "qualification" in claude.note
    codex = ap.qualification_status("codex")
    assert codex.qualified is True
    assert "2026-09-23" in codex.note and "qualification" in codex.note
    opencode = ap.qualification_status("opencode")
    assert opencode.qualified is False
    assert opencode.note == _OPENCODE_NOTE


def test_resolve_reviewer_carries_qualification_from_status(fake_provider, fake_provider_bin):
    fake_provider("claude")
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    status = ap.qualification_status("claude")
    assert adapter.qualified is True
    assert adapter.qualification_note == status.note
    fake_provider("opencode")
    denied = ap.resolve_reviewer("opencode", _env_for(fake_provider_bin))
    assert denied.qualified is False
    assert denied.qualification_note == _OPENCODE_NOTE


def test_claude_native_envelope_success_from_fixture(fake_provider_bin):
    payload = (FIXTURE_DIR / "claude-success.json").read_bytes()
    adapter = _replay(fake_provider_bin, "claude", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b'{"assessment": "payload"}'


def test_claude_extra_turn_is_tool_attempt(fake_provider_bin):
    adapter = _replay(fake_provider_bin, "claude", _claude_variant(num_turns=2))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_claude_permission_denial_is_tool_attempt(fake_provider_bin):
    # Payload-only denial path inside the fake envelope; never on disk.
    adapter = _replay(fake_provider_bin, "claude", _claude_variant(
        permission_denials=[{"tool": "Read", "path": "/tmp/decoy"}]))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_claude_missing_permission_denials_is_invalid(fake_provider_bin):
    envelope = json.loads(
        (FIXTURE_DIR / "claude-success.json").read_text(encoding="utf-8"))
    del envelope["permission_denials"]
    adapter = _replay(fake_provider_bin, "claude", json.dumps(envelope).encode("utf-8"))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_claude_error_envelope_is_invalid(fake_provider_bin):
    adapter = _replay(fake_provider_bin, "claude", _claude_variant(is_error=True))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"
    failed = _replay(fake_provider_bin, "claude", _claude_variant(subtype="error"))
    result = ap.launch_review(failed, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_native_stream_success_from_fixture(fake_provider_bin):
    payload = (FIXTURE_DIR / "codex-success.jsonl").read_bytes()
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b'{"assessment": "payload"}'


def test_codex_startup_error_item_is_accepted(fake_provider_bin):
    payload = (FIXTURE_DIR / "codex-startup-error.jsonl").read_bytes()
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.assessment == b'{"assessment": "payload"}'


@pytest.mark.parametrize("item_type", ["command_execution", "file_change",
                                       "collab_agent"])
def test_codex_tool_shaped_item_is_tool_attempt(fake_provider_bin, item_type):
    tool_item = json.dumps({"id": "item_0", "type": item_type,
                            "text": "ran"})
    message_item = json.dumps({"id": "item_1", "type": "agent_message",
                               "text": '{"assessment": "payload"}'})
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "turn.started"}',
        json.dumps({"type": "item.completed",
                    "item": json.loads(tool_item)}),
        json.dumps({"type": "item.completed",
                    "item": json.loads(message_item)}),
        '{"type": "turn.completed", "usage": {}}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "tool-attempt"


def test_codex_turn_failed_is_provider_failure(fake_provider_bin):
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0", '
        '"type": "agent_message", "text": "partial"}}',
        '{"type": "turn.failed", "error": "boom"}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "provider-failed"


def test_codex_missing_turn_completed_is_invalid(fake_provider_bin):
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0", '
        '"type": "agent_message", "text": "{\\"assessment\\": \\"payload\\"}"}}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_event_after_turn_completed_is_invalid(fake_provider_bin):
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0", '
        '"type": "agent_message", "text": "{\\"assessment\\": \\"payload\\"}"}}',
        '{"type": "turn.completed", "usage": {}}',
        '{"type": "item.completed", "item": {"id": "item_1", '
        '"type": "agent_message", "text": "trailing"}}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_second_turn_started_is_invalid(fake_provider_bin):
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "turn.started"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0", '
        '"type": "agent_message", "text": "{\\"assessment\\": \\"payload\\"}"}}',
        '{"type": "turn.completed", "usage": {}}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "invalid-assessment"


def test_codex_top_level_error_is_provider_failure(fake_provider_bin):
    payload = agent_codex_stream(
        '{"type": "thread.started", "thread_id": "THREAD-PLACEHOLDER"}',
        '{"type": "error", "message": "transport exploded"}',
    )
    adapter = _replay(fake_provider_bin, "codex", payload)
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is False
    assert result.error == "provider-failed"


# ---- review runtime: cancel event, bounded fan-out, cheap models (T6) -----

def _python_bin(fake_provider_bin: Path, name: str, script: str) -> None:
    write_fake_provider(fake_provider_bin, name, script=PYTHON_SHEBANG + script)


def test_launch_review_without_cancel_keeps_positional_shape(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    result = ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert result.ok is True
    assert result.cancelled is False


def test_launch_review_cancel_is_keyword_only(fake_provider_bin):
    import threading

    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    with pytest.raises(TypeError):
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]),
                         threading.Event())


def test_launch_review_preset_cancel_never_starts_child(fake_provider_bin, tmp_path):
    import threading

    canary = tmp_path / "launched"
    _python_bin(
        fake_provider_bin, "claude",
        "import sys\n"
        f"open({str(canary)!r}, 'w').write('x')\n"
        "sys.stdout.write('never')\n",
    )
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(fake_provider_bin)))
    cancel = threading.Event()
    cancel.set()
    result = ap.launch_review(adapter, PACKET, SCHEMA, 20, _no_progress([]),
                              cancel=cancel)
    assert result.ok is False
    assert result.cancelled is True
    assert result.error == "cancelled"
    assert not canary.exists()


def test_launch_review_cancel_event_mid_run_reaps_child(fake_provider_bin):
    import threading

    adapter = _synthetic(_resolve(fake_provider_bin, "codex", HANG))
    neighbor = _neighbor()
    cancel = threading.Event()

    def _setter():
        time.sleep(0.3)
        cancel.set()

    setter = threading.Thread(target=_setter, daemon=True)
    try:
        setter.start()
        result = ap.launch_review(adapter, PACKET, SCHEMA, 30,
                                  _no_progress([]), cancel=cancel)
    finally:
        neighbor_alive = neighbor.poll() is None
        neighbor.terminate()
        neighbor.wait()
        setter.join(timeout=5)
    assert result.ok is False
    assert result.cancelled is True
    assert result.error == "cancelled"
    _assert_dead(result.pid)
    assert neighbor_alive


def test_launch_reviews_aligns_results_and_bounds_concurrency(fake_provider_bin, tmp_path):
    log = str(tmp_path / "fanout.log")
    _python_bin(fake_provider_bin, "claude", agent_echo_claude_script(log=log, delay_s=0.4))
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    requests = [
        (json.dumps({"id": f"item-{index}"}).encode("utf-8"), SCHEMA)
        for index in range(4)
    ]
    seen: list = []

    results = ap.launch_reviews(adapter, requests, 20, concurrency=2,
                                on_done=lambda i, r: seen.append(i))

    assert [r.assessment for r in results] == [
        f"item-{index}".encode("utf-8") for index in range(4)
    ]
    assert all(r.ok for r in results)
    assert sorted(seen) == [0, 1, 2, 3]
    marks = []
    for line in Path(log).read_text(encoding="utf-8").splitlines():
        kind, stamp = line.split()
        marks.append((kind, int(stamp)))
    assert len(marks) == 8
    live = peak = 0
    for kind, _ in sorted(marks, key=lambda m: m[1]):
        live += 1 if kind == "start" else -1
        peak = max(peak, live)
    assert peak == 2


def test_launch_reviews_per_item_timeout_marks_only_that_item(fake_provider_bin):
    _python_bin(
        fake_provider_bin, "claude",
        "import json, sys, time\n"
        "body = sys.stdin.read()\n"
        "item = json.loads(body)['id']\n"
        "if item == 'slow':\n"
        "    time.sleep(30)\n"
        "envelope = {'type': 'result', 'subtype': 'success',\n"
        "            'is_error': False, 'num_turns': 1,\n"
        "            'permission_denials': [], 'result': item}\n"
        "sys.stdout.write(json.dumps(envelope))\n",
    )
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    requests = [
        (json.dumps({"id": "slow"}).encode("utf-8"), SCHEMA),
        (json.dumps({"id": "fast"}).encode("utf-8"), SCHEMA),
    ]
    results = ap.launch_reviews(adapter, requests, 3, concurrency=2)
    assert results[0].ok is False and results[0].timed_out is True
    assert results[0].error == "timeout"
    assert results[1].ok is True and results[1].assessment == b"fast"
    _assert_dead(results[0].pid)


def test_launch_reviews_does_not_start_queued_item_after_absolute_deadline(
        fake_provider_bin, monkeypatch):
    from types import SimpleNamespace

    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    clock = [100.0]
    monkeypatch.setattr(
        ap, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    started = []

    def fake_launch(adapter, packet, schema, timeout_s, progress, cancel,
                    deadline=None):
        item = json.loads(packet)["id"]
        started.append((item, timeout_s))
        assert deadline == 101.0
        clock[0] = 102.0
        return ap._result(
            adapter, ok=True, assessment=item.encode(), error="",
            exit_code=0, timed_out=False, cancelled=False, truncated=False,
            pid=os.getpid(), scratch="synthetic")

    monkeypatch.setattr(ap, "_launch_one", fake_launch)
    requests = [
        (json.dumps({"id": item}).encode("utf-8"), SCHEMA)
        for item in ("first", "queued")
    ]

    results = ap.launch_reviews(
        adapter, requests, 30, concurrency=1, deadline=101.0)

    assert started == [("first", 1)]
    assert results[0].ok is True
    assert results[1].timed_out is True


def test_launch_reviews_cancellation_does_not_start_queued_item(
        fake_provider_bin, monkeypatch, tmp_path):
    import concurrent.futures as _futures

    log = tmp_path / "cancelled-wave.log"
    _python_bin(
        fake_provider_bin, "claude",
        "import json, sys, time\n"
        "body = json.loads(sys.stdin.read())\n"
        "item = body['id']\n"
        f"open({str(log)!r}, 'a').write(item + '\\n')\n"
        "time.sleep(30)\n",
    )
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    requests = [
        (json.dumps({"id": item}).encode("utf-8"), SCHEMA)
        for item in ("first", "queued")
    ]
    real_as_completed = _futures.as_completed

    def interrupt_after_first_start(*args, **kwargs):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and log.read_text(encoding="utf-8").splitlines():
                raise KeyboardInterrupt
            time.sleep(0.02)
        raise AssertionError("provider did not start")

    monkeypatch.setattr(_futures, "as_completed", interrupt_after_first_start)
    with pytest.raises(Problem) as exc:
        ap.launch_reviews(adapter, requests, 30, concurrency=1)

    assert exc.value.code == "review-cancelled"
    assert real_as_completed is not None
    assert log.read_text(encoding="utf-8").splitlines() == ["first"]


def test_launch_reviews_keyboard_interrupt_raises_review_cancelled(
        fake_provider_bin, monkeypatch, tmp_path):
    import concurrent.futures as _futures

    pidlog = tmp_path / "pids.log"
    body = ("#!/bin/sh\n"
            f"echo $$ >> {pidlog}\n"
            "cat >/dev/null\nsleep 30\nexit 0\n")
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", body))
    requests = [(PACKET, SCHEMA), (PACKET, SCHEMA)]
    real_wait = _futures.wait
    calls = []

    def _boom(*args, **kwargs):
        calls.append(1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                lines = pidlog.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            if len([line for line in lines if line.strip()]) >= 2:
                break
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(_futures, "as_completed", _boom)
    start = time.monotonic()
    with pytest.raises(Problem) as exc:
        ap.launch_reviews(adapter, requests, 30, concurrency=2)
    elapsed = time.monotonic() - start
    assert exc.value.code == "review-cancelled"
    assert calls == [1]
    assert real_wait is not None
    assert elapsed < 10
    pids = [int(line.strip()) for line in
            pidlog.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(pids) == 2
    for pid in pids:
        _assert_dead(pid)


def test_launch_reviews_rejects_bad_concurrency(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    for bad in (0, 9):
        with pytest.raises(Problem) as exc:
            ap.launch_reviews(adapter, [(PACKET, SCHEMA)], 10,
                              concurrency=bad)
        assert exc.value.code == "invalid-bound"


def test_launch_reviews_empty_requests_returns_empty(fake_provider_bin):
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", CLAUDE_OK))
    assert ap.launch_reviews(adapter, [], 10) == ()


def test_launch_reviews_reports_each_item_as_it_completes(monkeypatch):
    """on_done fires per completion, not after the whole fan-out."""
    import threading

    from ptest.agent_providers import ProgressEvent, ProviderResult

    proceed = threading.Event()
    events: list = []

    def fake_launch_one(adapter, packet, schema, timeout_s, progress,
                        cancel):
        progress(ProgressEvent(phase="reviewing", provider=adapter.name,
                               elapsed_s=0.0))
        if packet == b"slow":
            assert proceed.wait(timeout=10), "fan-out deadlocked"
        return ProviderResult(
            provider=adapter.name, ok=True, assessment=packet, error="",
            exit_code=0, timed_out=False, cancelled=False, truncated=False,
            pid=os.getpid(), argv=adapter.argv, scratch="synthetic")

    monkeypatch.setattr(ap, "_launch_one", fake_launch_one)
    adapter = ap.ReviewerAdapter(
        name="codex", executable="/fake/codex", argv=("/fake/codex",),
        qualified=True, qualification_note="synthetic")
    outcome: dict = {}

    def run():
        outcome["results"] = ap.launch_reviews(
            adapter, [(b"fast", SCHEMA), (b"slow", SCHEMA)], 60,
            concurrency=2, on_done=lambda i, r: events.append(i),
            progress=lambda e: events.append(e.phase))

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while 0 not in events and time.monotonic() < deadline:
            time.sleep(0.01)
        # The fast item is reported while the slow one still runs; the
        # old wait-then-report loop could never do this.
        assert 0 in events
        assert 1 not in events
    finally:
        proceed.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert sorted(i for i in events if isinstance(i, int)) == [0, 1]
    assert "reviewing" in events
    assert [r.assessment for r in outcome["results"]] == [b"fast", b"slow"]


def test_with_model_appends_only_the_model_flag(fake_provider, fake_provider_bin):
    fake_provider("claude")
    claude = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    modeled = ap.with_model(claude, "haiku")
    assert tuple(modeled.argv) == tuple(claude.argv) + ("--model", "haiku")
    assert tuple(claude.argv) == tuple(
        ap.resolve_reviewer("claude", _env_for(fake_provider_bin)).argv)

    fake_provider("codex")
    codex = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert tuple(ap.with_model(codex, "gpt-5.6-luna").argv) == (
        tuple(codex.argv) + ("-m", "gpt-5.6-luna"))


@pytest.mark.parametrize("bad", ["", "--model", "-m", "has space", "a;b",
                                 "x" * 129, "-leading-dash"])
def test_with_model_rejects_invalid_ids(fake_provider, fake_provider_bin, bad):
    fake_provider("claude")
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    with pytest.raises(Problem):
        ap.with_model(adapter, bad)


def test_with_model_accepts_boundary_length_id(fake_provider, fake_provider_bin):
    fake_provider("claude")
    adapter = ap.resolve_reviewer("claude", _env_for(fake_provider_bin))
    assert ap.with_model(adapter, "a" * 128).argv[-1] == "a" * 128


def test_with_model_rejects_unqualified_provider(fake_provider, fake_provider_bin):
    fake_provider("opencode")
    adapter = ap.resolve_reviewer("opencode", _env_for(fake_provider_bin))
    with pytest.raises(Problem) as exc:
        ap.with_model(adapter, "haiku")
    assert exc.value.code == "provider-unqualified"


def _debug_models_bin(fake_provider_bin: Path, payload: bytes, *, exit_code: int = 0) -> None:
    blob = fake_provider_bin / "codex.models.payload"
    blob.write_bytes(payload)
    write_fake_provider(
        fake_provider_bin, "codex",
        script="#!/bin/sh\n"
        "if [ \"$1\" = debug ] && [ \"$2\" = models ]; then\n"
        f"  cat \"{blob}\"; exit {exit_code}\n"
        "fi\n"
        "exit 3\n",
    )


def test_discover_models_lists_only_visible_slugs(fake_provider_bin):
    payload = (FIXTURE_DIR / "codex-debug-models.json").read_bytes()
    _debug_models_bin(fake_provider_bin, payload)
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert ap.discover_models(adapter) == (
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5",
    )


@pytest.mark.parametrize("payload,exit_code", [
    (b"{}", 0),
    (b"not json", 0),
    (b'{"models": []}', 1),
    (b'{"models": "nope"}', 0),
])
def test_discover_models_returns_empty_on_failure(fake_provider_bin, payload, exit_code):
    _debug_models_bin(fake_provider_bin, payload, exit_code=exit_code)
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert ap.discover_models(adapter) == ()


def test_discover_models_over_bound_output_returns_empty(fake_provider_bin):
    blob = b'{"models": [' + b" " * (4 * 1024 * 1024 + 1) + b"]}"
    _debug_models_bin(fake_provider_bin, blob)
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert ap.discover_models(adapter) == ()


def test_discover_models_timeout_returns_empty(fake_provider_bin, monkeypatch):
    write_fake_provider(fake_provider_bin, "codex", script="#!/bin/sh\nsleep 30\nexit 0\n")
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    monkeypatch.setattr(ap, "_DISCOVERY_TIMEOUT_S", 1)
    assert ap.discover_models(adapter) == ()


def test_discover_models_non_codex_returns_empty_without_launch(tmp_path):
    adapter = ap.ReviewerAdapter(
        name="claude", executable=str(tmp_path / "missing-claude"),
        argv=(str(tmp_path / "missing-claude"),), qualified=True,
        qualification_note="synthetic",
    )
    assert ap.discover_models(adapter) == ()


def test_cli_version_reports_first_line(fake_provider_bin):
    write_fake_provider(fake_provider_bin, "codex",
                          script="#!/bin/sh\nprintf 'codex-cli 0.155.1\\nsha: abc\\n'\nexit 0\n")
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert ap.cli_version(adapter) == "codex-cli 0.155.1"


def test_cli_version_returns_none_on_failure(fake_provider_bin, monkeypatch):
    write_fake_provider(fake_provider_bin, "codex", script="#!/bin/sh\nexit 3\n")
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    assert ap.cli_version(adapter) is None
    write_fake_provider(fake_provider_bin, "codex", script="#!/bin/sh\nsleep 30\nexit 0\n")
    slow = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    monkeypatch.setattr(ap, "_VERSION_TIMEOUT_S", 1)
    assert ap.cli_version(slow) is None


def _fifo_eof_within(reader: int, deadline_s: float) -> bool:
    """True once the fifo has no writers left (read returns EOF)."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            chunk = os.read(reader, 65536)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        if chunk == b"":
            return True
    return False


def test_owned_capture_kills_grandchild_proved_by_fifo_eof(
        fake_provider_bin, monkeypatch):
    """A forked grandchild holding a fifo write end leaves no survivor.

    The fake CLI backgrounds a 60s sleep holding the fifo open, then
    sleeps itself; the 1s owned capture must kill the whole group.
    Liveness is proved by fifo EOF (all writers dead), never by a
    /proc scan. Both cli_version and discover_model_entries delegate
    to the same _run_owned_capture helper.
    """
    fifo = fake_provider_bin / "grandchild.fifo"
    os.mkfifo(fifo)
    write_fake_provider(
        fake_provider_bin, "codex",
        script="#!/bin/sh\n"
        f'F="{fifo}"\n'
        'sleep 60 <> "$F" &\n'
        "exec sleep 60\n",
    )
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))
    monkeypatch.setattr(ap, "_VERSION_TIMEOUT_S", 1)
    assert ap.cli_version(adapter) is None
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert _fifo_eof_within(reader, 10.0), \
            "grandchild survived the owned capture"
    finally:
        os.close(reader)


def test_cli_version_early_stops_on_over_bound_output(fake_provider_bin):
    """cli_version returns None well before its timeout on huge output."""
    write_fake_provider(
        fake_provider_bin, "codex",
        script="#!/bin/sh\nhead -c 1200000 /dev/zero | tr '\\0' 'v'\nsleep 30\n")
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))

    started = time.monotonic()
    try:
        assert ap.cli_version(adapter) is None
    finally:
        elapsed = time.monotonic() - started
    assert elapsed < ap._VERSION_TIMEOUT_S - 1


def test_discover_models_drops_slugs_failing_model_re(fake_provider_bin):
    """Discovered codex slugs are validated at discovery, not at pick."""
    payload = json.dumps({
        "models": [
            {"slug": "gpt-5.6-luna", "display_name": "Luna",
             "description": "fast", "visibility": "list"},
            {"slug": "../evil", "display_name": "Evil",
             "description": "path escape", "visibility": "list"},
            {"slug": "has space", "display_name": "Space",
             "description": "blank", "visibility": "list"},
            {"slug": "x" * 129, "display_name": "Long",
             "description": "over bound", "visibility": "list"},
        ],
    }).encode("utf-8")
    _debug_models_bin(fake_provider_bin, payload)
    adapter = ap.resolve_reviewer("codex", _env_for(fake_provider_bin))

    assert ap.discover_models(adapter) == ("gpt-5.6-luna",)
    assert ap.discover_model_entries(adapter) == (
        {"slug": "gpt-5.6-luna", "display_name": "Luna",
         "description": "fast"},
    )


@pytest.mark.skipif(os.name != "posix", reason="owned launch is posix-only")
def test_owned_spawn_refusals_name_the_reviewer(fake_provider_bin, monkeypatch):
    """_launch_one restores the reviewer prefix on _spawn_owned refusals."""
    adapter = _synthetic(_resolve(fake_provider_bin, "claude", "#!/bin/sh\nexit 0\n"))
    monkeypatch.setattr(ap, "_PIDFD_AVAILABLE", False)
    with pytest.raises(Problem) as refused:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert refused.value.code == "provider-failed"
    assert refused.value.message.startswith("reviewer claude launch refused: ")
    assert "pidfd containment unavailable" in refused.value.message


@pytest.mark.skipif(os.name != "posix", reason="owned launch is posix-only")
def test_unverifiable_identity_refusal_names_the_reviewer(fake_provider_bin, monkeypatch):
    """An unverifiable process identity names its reviewer, then fails closed."""
    adapter = _synthetic(_resolve(fake_provider_bin, "codex", "#!/bin/sh\nsleep 30\n"))

    def _no_pgid(pid):
        raise ProcessLookupError("synthetic unreadable pgid")

    monkeypatch.setattr(os, "getpgid", _no_pgid)
    with pytest.raises(Problem) as refused:
        ap.launch_review(adapter, PACKET, SCHEMA, 10, _no_progress([]))
    assert refused.value.code == "provider-failed"
    assert refused.value.message.startswith("reviewer codex launch refused: ")
    assert "identity unverifiable" in refused.value.message
