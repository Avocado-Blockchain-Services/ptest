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
import stat
import subprocess
import tempfile
import time
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


def test_timeout_kills_descendant_after_direct_child_exit(bindir, tmp_path):
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
    adapter = _synthetic(_resolve(bindir, "claude", body))
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
                          pid=1, argv=("x",), scratch="/tmp/x")
    assert json.dumps({"kinds": list(ap.SUPPORTED_REVIEWERS)})


def test_find_executable_ignores_relative_path_components(tmp_path, monkeypatch):
    """Relative PATH entries must never resolve a cwd-relative binary."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    _write_bin(repo / "bin", "claude", "#!/bin/sh\necho UNTRUSTED\n")
    trusted = tmp_path / "tools"
    trusted.mkdir()
    _write_bin(trusted, "claude", "#!/bin/sh\necho TRUSTED\n")
    monkeypatch.chdir(repo)
    env = {"PATH": os.pathsep.join(["bin", ".", "", str(trusted)])}
    adapter = ap.resolve_reviewer("claude", env)
    assert adapter.executable == str(trusted / "claude")
    assert os.path.isabs(adapter.executable)
    with pytest.raises(Problem) as exc:
        ap.resolve_reviewer("claude", {"PATH": os.pathsep.join(["bin", "."])})
    assert exc.value.code == "provider-unavailable"


def test_launch_popen_failure_cleans_scratch(bindir, monkeypatch):
    """Popen failure after mkdtemp must not leak the owned scratch dir."""
    _write_bin(bindir, "claude", "#!/bin/sh\nexit 0\n")
    adapter = _synthetic(ap.resolve_reviewer("claude", _env_for(bindir)))
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


def test_stale_member_pid_never_signaled_numerically(bindir, tmp_path,
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
    adapter = _synthetic(_resolve(bindir, "claude", body))
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


def test_timeout_with_unreadable_identity_fails_closed(bindir, monkeypatch):
    """Unreadable /proc identity at runtime must raise, never kill blindly."""
    monkeypatch.setattr(ap, "_proc_starttime", lambda pid: None)
    adapter = _synthetic(_resolve(bindir, "claude", HANG))
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


def test_launch_fails_closed_without_pidfd_containment(bindir, tmp_path,
                                                       monkeypatch):
    """Without a pinnable handle there is no sound cleanup: do not launch."""
    monkeypatch.setattr(ap, "_PIDFD_AVAILABLE", False)
    canary = tmp_path / "launched"
    body = (
        "#!/bin/sh\ntouch \"" + str(canary) + "\"\ncat >/dev/null\n"
        "printf '{\"result\": \"X\"}'\nexit 0\n"
    )
    adapter = _synthetic(_resolve(bindir, "claude", body))
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
