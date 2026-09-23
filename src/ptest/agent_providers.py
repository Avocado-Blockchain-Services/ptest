"""Owned provider subprocess boundary (task 1A owned).

Per-provider qualification for the ``claude`` / ``codex`` / ``opencode``
reviewer adapters. The frozen argv tails below are the single source of
truth, copied from the 2026-09-23 qualification record; :func:`resolve_reviewer`
takes ``qualified`` from :func:`qualification_status`, and there is no
second copy. Claude and Codex are qualified; OpenCode is not, and
:func:`launch_review` fails closed for unqualified adapters.
Synthetic tests exercise the machinery with fake executables only and
never qualify a real provider.

Containment: on POSIX the child runs as a session leader and group cleanup
signals descendants only through pidfds pinned to processes that passed
post-pin session/start-time validation; numeric killpg/kill of members is
never used, so a recycled PID can never receive our signal. Anything
unverifiable -- missing pidfd support, unreadable identity -- fails closed
instead of launching or killing.
"""
from __future__ import annotations

import concurrent.futures
import errno
import json
import logging
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from .contracts import Problem

log = logging.getLogger(__name__)

SUPPORTED_REVIEWERS = ("claude", "codex", "opencode")

PROMPT_INPUT_MAX_BYTES = 1048576
OUTPUT_MAX_BYTES = 524288
TIMEOUT_MIN_S = 1
TIMEOUT_MAX_S = 900
_TERM_GRACE_S = 2.0
_HEARTBEAT_S = 15.0

# Process-group ownership is verified on POSIX only; on other platforms the
# implementation falls back to direct-child terminate/kill (unverified).
PROCESS_GROUP_SCOPE = "posix-only; non-POSIX group control unverified"

_PHASES = frozenset({"collecting", "reviewing", "validating", "publishing"})

_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TZ", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
})
_CRED_DENY = ("token", "secret", "key", "credential", "password", "auth",
              "session", "cookie")

# Frozen argv tails (executable prepended at resolution). Single source of
# truth, copied exactly from
# docs/research/2026-09-22-agent-provider-qualification.md, section
# "Claude and Codex qualification — 2026-09-23". Deliberately no "--bare"
# Claude fallback (it disables normal auth). OpenCode keeps its unqualified
# candidate tail and stays disabled (see _OPENCODE_NOTE).
_TAILS = {
    "claude": ("--print", "--output-format", "json",
               "--input-format", "text",
               "--safe-mode", "--tools", "",
               "--strict-mcp-config",
               "--disable-slash-commands",
               "--no-session-persistence"),
    "codex": ("exec", "--ignore-user-config", "--ignore-rules",
              "--ephemeral", "--skip-git-repo-check",
              "--sandbox", "read-only", "--json",
              "-c", 'web_search="disabled"',
              "--disable", "shell_tool",
              "--disable", "unified_exec",
              "--disable", "apps",
              "--disable", "browser_use",
              "--disable", "browser_use_external",
              "--disable", "computer_use",
              "--disable", "hooks",
              "--disable", "image_generation",
              "--disable", "in_app_browser",
              "--disable", "multi_agent",
              "--disable", "plugins",
              "--disable", "remote_plugin",
              "--disable", "plugin_sharing",
              "--disable", "skill_search",
              "--disable", "skill_mcp_dependency_install",
              "--disable", "sleep_tool",
              "--disable", "tool_suggest",
              "--disable", "tool_call_mcp_elicitation",
              "--disable", "view_image",
              "--disable", "code_mode_host",
              "--disable", "goals",
              "--disable", "guardian_approval",
              "--disable", "workspace_dependencies",
              "--disable", "in_app_chat",
              "--disable", "in_app_local_automation",
              "--disable", "browser_use_full_cdp_access",
              "--disable", "unified_exec_tty",
              "--disable", "shell_snapshot"),
    "opencode": ("run", "--pure", "--format", "json",
                 "--agent", "ptest-locked-denied"),
}

_QUALIFIED_NOTE = (
    "qualified per docs/research/2026-09-22-agent-provider-qualification.md, "
    "section \"Claude and Codex qualification — 2026-09-23\""
)

_OPENCODE_NOTE = (
    "OpenCode's free tier refuses tool-free runs (HTTP 403 FreeTierError); "
    "not supported for review in this release"
)

# Explicit qualification allowlist: only these names are qualified. Every
# other supported name is unqualified by default.
_QUALIFIED = frozenset({"claude", "codex"})

_UNQUALIFIED_NOTE = "not supported for review in this release"


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="provider")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_str(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be nonempty")
    return value


def _check_argv(name: str, value: object) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be tuple, got {type(value).__name__}")
    tokens = tuple(value)
    if not tokens:
        raise ValueError(f"{name} must be nonempty")
    total = 0
    for token in tokens:
        _check_str(f"{name}[]", token, allow_empty=True)
        if "\x00" in token:
            raise ValueError(f"{name}[] must not contain NUL")
        total += len(token.encode("utf-8"))
    if total > 131072:
        raise ValueError(f"{name} exceeds 128 KiB total")
    return tokens


@dataclass(frozen=True, slots=True)
class ReviewerAdapter:
    name: str
    executable: str
    argv: tuple = ()
    qualified: bool = False
    qualification_note: str = ""

    def __post_init__(self) -> None:
        _check_str("adapter.name", self.name)
        if self.name not in SUPPORTED_REVIEWERS:
            raise ValueError(f"adapter.name {self.name!r} is not supported")
        _check_str("adapter.executable", self.executable)
        tokens = _check_argv("adapter.argv", self.argv)
        if tokens[0] != self.executable:
            raise ValueError("adapter.argv[0] must be the resolved executable")
        object.__setattr__(self, "argv", tokens)
        if not isinstance(self.qualified, bool):
            raise TypeError("adapter.qualified must be bool")
        _check_str("adapter.qualification_note", self.qualification_note)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: str
    provider: str
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        _check_str("progress.phase", self.phase)
        if self.phase not in _PHASES:
            raise ValueError(f"progress.phase {self.phase!r} is unknown")
        _check_str("progress.provider", self.provider)
        if self.provider not in SUPPORTED_REVIEWERS:
            raise ValueError("progress.provider is not supported")
        if isinstance(self.elapsed_s, bool) or not isinstance(
                self.elapsed_s, (int, float)):
            raise TypeError("progress.elapsed_s must be numeric")
        elapsed = float(self.elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("progress.elapsed_s must be finite and >= 0")
        object.__setattr__(self, "elapsed_s", elapsed)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    ok: bool
    assessment: bytes
    error: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    truncated: bool
    pid: int
    argv: tuple = ()
    scratch: str = ""

    def __post_init__(self) -> None:
        _check_str("result.provider", self.provider)
        if self.provider not in SUPPORTED_REVIEWERS:
            raise ValueError("result.provider is not supported")
        if not isinstance(self.ok, bool):
            raise TypeError("result.ok must be bool")
        if not isinstance(self.assessment, bytes):
            raise TypeError("result.assessment must be bytes")
        _check_str("result.error", self.error, allow_empty=True)
        if self.ok and self.error:
            raise ValueError("successful result must carry empty error")
        if not self.ok and not self.error:
            raise ValueError("failed result must carry an error code")
        if self.exit_code is not None and not _is_int(self.exit_code):
            raise TypeError("result.exit_code must be int or None")
        for field in ("timed_out", "cancelled", "truncated"):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"result.{field} must be bool")
        if not _is_int(self.pid) or self.pid < 1:
            raise ValueError("result.pid must be a positive int")
        object.__setattr__(self, "argv", _check_argv("result.argv", self.argv))
        _check_str("result.scratch", self.scratch)


@dataclass(frozen=True, slots=True)
class QualificationStatus:
    name: str
    qualified: bool
    argv: tuple = ()
    note: str = ""

    def __post_init__(self) -> None:
        _check_str("qualification.name", self.name)
        if self.name not in SUPPORTED_REVIEWERS:
            raise ValueError("qualification.name is not supported")
        if not isinstance(self.qualified, bool):
            raise TypeError("qualification.qualified must be bool")
        object.__setattr__(self, "argv", _check_argv("qualification.argv",
                                                     self.argv))
        _check_str("qualification.note", self.note, allow_empty=True)


def qualification_status(name: str) -> QualificationStatus:
    """Expose the per-provider qualification state of a profile.

    Names in _QUALIFIED are qualified under the frozen argv above; every
    other supported name is unqualified (OpenCode keeps its record note).
    """
    _check_str("name", name)
    if name not in SUPPORTED_REVIEWERS:
        raise _problem("provider-unavailable", f"unsupported reviewer {name}")
    if name in _QUALIFIED:
        return QualificationStatus(name=name, qualified=True,
                                   argv=(name,) + _TAILS[name],
                                   note=_QUALIFIED_NOTE)
    if name == "opencode":
        return QualificationStatus(name=name, qualified=False,
                                   argv=(name,) + _TAILS[name],
                                   note=_OPENCODE_NOTE)
    return QualificationStatus(name=name, qualified=False,
                               argv=(name,) + _TAILS.get(name, ()),
                               note=_UNQUALIFIED_NOTE)


def _find_executable(name: str, env: Mapping) -> str:
    path = env.get("PATH", "")
    if not isinstance(path, str):
        raise TypeError("env PATH must be str")
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        if not os.path.isabs(directory):
            continue
        candidate = os.path.join(directory, name)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    raise _problem("provider-unavailable", f"reviewer {name} not installed")


def resolve_reviewer(name: str, env: Mapping[str, str]) -> ReviewerAdapter:
    """Resolve a supported reviewer to its frozen qualified adapter.

    Qualification comes only from :func:`qualification_status`; there is
    no second copy and no override.
    """
    _check_str("name", name)
    if not isinstance(env, Mapping):
        raise TypeError("env must be a mapping")
    if name not in SUPPORTED_REVIEWERS:
        raise _problem("provider-unavailable", f"unsupported reviewer {name}")
    executable = _find_executable(name, env)
    status = qualification_status(name)
    return ReviewerAdapter(name=name, executable=executable,
                           argv=(executable,) + status.argv[1:],
                           qualified=status.qualified,
                           qualification_note=status.note)


def sanitized_child_env(source: Mapping) -> dict[str, str]:
    """Project a minimal allowlisted child environment.

    Credential-bearing keys never cross, even if allowlisted. Values are
    never logged by this module.
    """
    if not isinstance(source, Mapping):
        raise TypeError("source must be a mapping")
    projected: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        try:
            value = source[key]
        except KeyError:
            continue
        if not isinstance(value, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in _CRED_DENY):
            continue
        projected[key] = value
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in _CRED_DENY):
            projected.pop(key, None)
    return projected


def _result(adapter: ReviewerAdapter, *, ok: bool, assessment: bytes,
            error: str, exit_code: int | None, timed_out: bool,
            cancelled: bool, truncated: bool, pid: int,
            scratch: str) -> ProviderResult:
    return ProviderResult(provider=adapter.name, ok=ok,
                          assessment=assessment, error=error,
                          exit_code=exit_code, timed_out=timed_out,
                          cancelled=cancelled, truncated=truncated, pid=pid,
                          argv=adapter.argv, scratch=scratch)


def _proc_starttime(pid: int) -> int | None:
    """Boot-relative start time from /proc, or None when unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        tail = raw.decode("latin-1").rsplit(")", 1)[1].split()
        return int(tail[19])  # field 22 (starttime) after the comm field
    except (IndexError, ValueError):
        return None


def _group_member_pids(pgid: int) -> list[int] | None:
    """Pids currently in `pgid`, or None when the table is not enumerable."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    members: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        mid = int(entry)
        try:
            if os.getpgid(mid) == pgid:
                members.append(mid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return members


def _unverified_group() -> None:
    raise _problem(
        "provider-failed",
        "process-group ownership unverifiable after child exit; "
        "refusing to signal possibly reused PGID")


_PIDFD_SYS_OPEN = 434  # pidfd_open; validated by a self-pin probe at import
_PIDFD_SYS_SEND = 424  # pidfd_send_signal; validated the same way


def _pidfd_backend():
    """Resolve (open_fn, send_fn) for race-free signaling, or (None, None).

    Prefers :func:`os.pidfd_open` / :func:`signal.pidfd_send_signal` and
    falls back to the Linux syscalls via ctypes (some interpreters do not
    expose the wrappers). The fallback self-pins once at import, so wrong
    syscall numbers fail as ENOSYS and report unavailability instead of a
    broken backend. Anything else -- non-Linux, missing syscalls, a failed
    probe -- yields (None, None) and the caller must fail closed.
    """
    native_open = getattr(os, "pidfd_open", None)
    native_send = getattr(signal, "pidfd_send_signal", None)
    if callable(native_open) and callable(native_send):
        def _open(pid: int, flags: int = 0) -> int:
            return native_open(pid, flags)

        def _send(pidfd: int, sig: int) -> None:
            native_send(pidfd, sig)

        return _open, _send
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return None, None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.syscall.argtypes = [ctypes.c_long] * 7

        def _open(pid: int, flags: int = 0) -> int:
            fd = libc.syscall(_PIDFD_SYS_OPEN, pid, flags, 0, 0, 0, 0)
            if fd < 0:
                code = ctypes.get_errno()
                if code == errno.ESRCH:
                    raise ProcessLookupError(code, os.strerror(code))
                raise OSError(code, os.strerror(code))
            return int(fd)

        def _send(pidfd: int, sig: int) -> None:
            ret = libc.syscall(_PIDFD_SYS_SEND, pidfd, sig, 0, 0, 0, 0)
            if ret != 0:
                code = ctypes.get_errno()
                if code == errno.ESRCH:
                    raise ProcessLookupError(code, os.strerror(code))
                if code == errno.EPERM:
                    raise PermissionError(code, os.strerror(code))
                raise OSError(code, os.strerror(code))

        probe = _open(os.getpid())
        try:
            _send(probe, 0)
        finally:
            os.close(probe)
        return _open, _send
    except Exception:
        return None, None


_PIDFD_OPEN, _PIDFD_SEND = _pidfd_backend()
_PIDFD_AVAILABLE = _PIDFD_OPEN is not None and _PIDFD_SEND is not None


def _pin_process(pid: int) -> int | None:
    """Open a pidfd pinning `pid`, or None when it already exited."""
    try:
        return _PIDFD_OPEN(pid, 0)
    except (ProcessLookupError, OSError):
        return None


def _pidfd_signal(pidfd: int, sig: int) -> bool:
    """Signal through a pinned pidfd; False when the pinned process is gone.

    The descriptor refers to one specific process, never to whatever PID
    number it once held, so a recycled number can never receive this
    signal: a dead pin reports ESRCH and nothing is sent anywhere.
    """
    try:
        _PIDFD_SEND(pidfd, sig)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return False
    return True


def _pidfd_exited(pidfd: int) -> bool:
    """True when the pinned process has exited (pidfd readable).

    A pidfd becomes readable (POLLIN) once its process exits, so a
    numeric /proc read that fails after a successful pin can be
    classified: dead pin -> close and skip safely (signaling it delivers
    to nothing); still-live but unreadable -> caller must fail closed.
    Poll errors report not-exited so the caller stays fail-closed.
    """
    try:
        import select as _select
        readable, _, _ = _select.select([pidfd], [], [], 0)
        return bool(readable)
    except (OSError, ValueError, TypeError):
        return False


def _proc_state(pid: int) -> str | None:
    """Single-letter /proc state, or None when unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        return raw.decode("latin-1").rsplit(")", 1)[1].split()[0]
    except IndexError:
        return None


def _close_pinned(owned: list) -> None:
    for _, pidfd in owned:
        try:
            os.close(pidfd)
        except OSError:
            pass


def _owned_group_members(proc_pid: int, pgid: int, sid: int | None,
                         leader_start: int | None) -> list:
    """Pinned (pid, pidfd) pairs verified as still-owned members of `pgid`.

    Each candidate is pinned with pidfd_open first and validated after the
    pin: the session ID must equal our private session (with
    ``start_new_session`` no outsider can carry it) and the start time must
    be readable and no older than the leader's. Signaling later goes only
    through the returned descriptors, so a PID that exits and is recycled
    after validation can never receive our signal -- the descriptor still
    names the original process, and a dead pin delivers to nothing.

    Residual note: the post-pin numeric reads could observe a recycled
    replacement if exit-plus-reuse lands exactly between the pin and the
    read. That replacement can only pass validation by also carrying our
    private session ID, i.e. by being our own descendant, which is in
    scope to signal anyway; a mismatch on a live pin fails closed. A
    numeric read that fails while the pinned pidfd itself reports exited
    (readable) is an ordinary exit race: the dead pin is closed and
    skipped, since signaling it delivers to nothing. Anything unreadable
    on a live pin -- our own identity or a member's -- fails closed
    instead of killing.
    """
    if not _PIDFD_AVAILABLE:
        _unverified_group()
    if sid is None or leader_start is None:
        _unverified_group()
    assert sid is not None and leader_start is not None
    members = _group_member_pids(pgid)
    if members is None:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return []
        except (PermissionError, OSError):
            pass
        _unverified_group()
    assert members is not None
    owned: list = []
    try:
        for mid in members:
            if mid == proc_pid:
                continue
            pidfd = _pin_process(mid)
            if pidfd is None:
                continue  # exited while scanning; nothing pinned, nothing sent
            try:
                member_sid = os.getsid(mid)
            except ProcessLookupError:
                if _pidfd_exited(pidfd):
                    os.close(pidfd)
                    continue  # pinned process already dead; nothing to signal
                os.close(pidfd)
                _unverified_group()
            except OSError:
                if _pidfd_exited(pidfd):
                    os.close(pidfd)
                    continue  # dead pin; numeric read raced with exit
                os.close(pidfd)
                _unverified_group()
            if member_sid != sid:
                if _pidfd_exited(pidfd):
                    os.close(pidfd)
                    continue  # dead pin; numeric read saw a recycled number
                os.close(pidfd)
                _unverified_group()
            member_start = _proc_starttime(mid)
            if member_start is None or member_start < leader_start:
                if _pidfd_exited(pidfd):
                    os.close(pidfd)
                    continue  # exit race between pin and /proc stat
                os.close(pidfd)
                _unverified_group()
            member_state = _proc_state(mid)
            if member_state == "Z":
                os.close(pidfd)
                continue  # zombie: dead, owned by init; no signal needed
            if member_state is None and _pidfd_exited(pidfd):
                os.close(pidfd)
                continue  # dead pin with unreadable state; nothing to signal
            owned.append((mid, pidfd))
    except BaseException:
        # A later unverifiable member fails closed, but the earlier pins
        # already validated must still be released: the caller never
        # receives `owned`, so nobody else can close them.
        _close_pinned(owned)
        raise
    return owned


def _signal_pinned(proc_pid: int, pgid: int, sid: int | None,
                   leader_start: int | None, sig: int) -> None:
    """Signal every verified member through its pinned pidfd, then unpin."""
    owned = _owned_group_members(proc_pid, pgid, sid, leader_start)
    try:
        for _, pidfd in owned:
            _pidfd_signal(pidfd, sig)
    finally:
        _close_pinned(owned)


def _stop_owned(proc: subprocess.Popen, pgid: int | None,
                sid: int | None = None,
                leader_start: int | None = None) -> None:
    """Terminate the owned group, including descendants outliving the child.

    The direct child is signaled through its Popen handle only, which is
    sound: an unreaped child pins its PID, so no recycled number is at
    risk. Every other member is signaled exclusively through a pidfd pinned
    to a process that passed post-pin session/start-time validation; numeric
    killpg/kill of members is never used, so a recycled PID can never
    receive our signal. An unverifiable group fails closed rather than
    risking a reused PGID; survivors found after SIGKILL are re-signaled
    through their pinned pidfds until quiescent or the deadline, and members
    still surviving then are reported instead of silently leaked. The direct
    child is always reaped.
    """
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        group = pgid is not None and os.name == "posix"
        if group:
            _signal_pinned(proc.pid, pgid, sid, leader_start, signal.SIGTERM)
        deadline = time.monotonic() + _TERM_GRACE_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if group:
            _signal_pinned(proc.pid, pgid, sid, leader_start, signal.SIGKILL)
            deadline = time.monotonic() + _TERM_GRACE_S
            while True:
                survivors = _owned_group_members(proc.pid, pgid, sid,
                                                 leader_start)
                try:
                    # A TERM-ignoring member may have forked after the
                    # SIGKILL enumeration; every verified survivor is
                    # re-signaled through its pinned pidfd -- never by
                    # numeric PID -- until quiescent or the deadline.
                    for _, pidfd in survivors:
                        _pidfd_signal(pidfd, signal.SIGKILL)
                finally:
                    _close_pinned(survivors)
                if not survivors or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if survivors:
                raise _problem(
                    "provider-failed",
                    "owned group member survived SIGKILL; "
                    "containment incomplete")
    finally:
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


_CODEX_ALLOWED_EVENTS = frozenset({
    "thread.started", "turn.started",
    "item.started", "item.updated", "item.completed",
    "turn.completed",
})

# Codex tools stay listed but inert under the qualified profile, so every
# non-message stream item is treated as a tool attempt and fails review.
_CODEX_ALLOWED_ITEMS = frozenset({"agent_message", "reasoning", "error"})


def _normalize_claude(text: str) -> tuple[bool, bytes, str]:
    try:
        envelope = json.loads(text.strip() or "null")
    except (json.JSONDecodeError, ValueError):
        return False, b"", "invalid-assessment"
    if not isinstance(envelope, dict):
        return False, b"", "invalid-assessment"
    if envelope.get("type") != "result":
        return False, b"", "invalid-assessment"
    if envelope.get("subtype") != "success":
        return False, b"", "invalid-assessment"
    if envelope.get("is_error") is not False:
        return False, b"", "invalid-assessment"
    turns = envelope.get("num_turns")
    if not _is_int(turns):
        return False, b"", "invalid-assessment"
    if turns > 1:
        return False, b"", "tool-attempt"
    if turns != 1:
        return False, b"", "invalid-assessment"
    denials = envelope.get("permission_denials")
    if not isinstance(denials, list):
        return False, b"", "invalid-assessment"
    if len(denials) > 0:
        return False, b"", "tool-attempt"
    candidate = envelope.get("result")
    if not isinstance(candidate, str) or not candidate:
        return False, b"", "invalid-assessment"
    return True, candidate.encode("utf-8"), ""


def _normalize_codex(text: str) -> tuple[bool, bytes, str]:
    lines = [line for line in text.splitlines() if line.strip()]
    events: list = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            return False, b"", "invalid-assessment"
    completed_turns = 0
    started = False
    last_message: str | None = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(
                event.get("type"), str):
            return False, b"", "invalid-assessment"
        kind = event["type"]
        if kind in ("turn.failed", "error"):
            return False, b"", "provider-failed"
        if kind not in _CODEX_ALLOWED_EVENTS:
            return False, b"", "invalid-assessment"
        if completed_turns:
            return False, b"", "invalid-assessment"
        if kind == "turn.started":
            if started:
                return False, b"", "invalid-assessment"
            started = True
            continue
        if kind == "turn.completed":
            completed_turns += 1
            continue
        if kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(
                    item.get("type"), str):
                return False, b"", "invalid-assessment"
            if item["type"] not in _CODEX_ALLOWED_ITEMS:
                return False, b"", "tool-attempt"
            if kind == "item.completed" and item["type"] == "agent_message":
                message = item.get("text")
                if not isinstance(message, str):
                    return False, b"", "invalid-assessment"
                last_message = message
    if completed_turns != 1:
        return False, b"", "invalid-assessment"
    if not last_message:
        return False, b"", "invalid-assessment"
    return True, last_message.encode("utf-8"), ""


def _normalize(name: str, stdout: bytes) -> tuple[bool, bytes, str]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False, b"", "invalid-assessment"
    if name == "claude":
        return _normalize_claude(text)
    if name == "codex":
        return _normalize_codex(text)
    lines = [line for line in text.splitlines() if line.strip()]
    events: list = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            return False, b"", "invalid-assessment"
    if name == "opencode":
        results = []
        for event in events:
            if not isinstance(event, dict) or not isinstance(
                    event.get("event"), str):
                return False, b"", "invalid-assessment"
            kind = event["event"].lower()
            if kind == "tool" or kind.startswith("tool"):
                return False, b"", "tool-attempt"
            if kind == "result":
                results.append(event.get("data"))
        if len(results) != 1 or not isinstance(results[0], str) \
                or not results[0]:
            return False, b"", "invalid-assessment"
        return True, results[0].encode("utf-8"), ""
    return False, b"", "invalid-assessment"


def launch_review(adapter: ReviewerAdapter, packet: bytes, schema: bytes,
                  timeout_s: int,
                  progress: Callable[[ProgressEvent], None], *,
                  cancel: threading.Event | None = None) -> ProviderResult:
    """Run one owned provider child and normalize exactly one assessment.

    The positional call shape is unchanged; the keyword-only ``cancel``
    event stops the owned group and reports ``error="cancelled"`` when set.
    """
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise TypeError("cancel must be a threading.Event or None")
    return _launch_one(adapter, packet, schema, timeout_s, progress, cancel)


def _cancelled_result(adapter: ReviewerAdapter,
                      exit_code: int | None = None,
                      scratch: str = "unstarted") -> ProviderResult:
    return _result(adapter, ok=False, assessment=b"", error="cancelled",
                   exit_code=exit_code, timed_out=False, cancelled=True,
                   truncated=False, pid=os.getpid(), scratch=scratch)


def _launch_one(adapter: ReviewerAdapter, packet: bytes, schema: bytes,
                timeout_s: int,
                progress: Callable[[ProgressEvent], None],
                cancel: threading.Event | None) -> ProviderResult:
    """Run one owned provider child and normalize exactly one assessment."""
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if not isinstance(packet, bytes) or not isinstance(schema, bytes):
        raise TypeError("packet and schema must be bytes")
    if not _is_int(timeout_s):
        raise TypeError("timeout_s must be int")
    if not callable(progress):
        raise TypeError("progress must be callable")
    if not packet or not schema:
        raise _problem("invalid-bound", "packet and schema must be nonempty")
    if len(packet) + len(schema) > PROMPT_INPUT_MAX_BYTES:
        raise _problem("invalid-bound", "prompt input exceeds 1 MiB")
    if timeout_s < TIMEOUT_MIN_S or timeout_s > TIMEOUT_MAX_S:
        raise _problem("invalid-bound", "timeout_s outside 1..900 seconds")
    if not adapter.qualified:
        raise _problem("provider-unqualified",
                       f"reviewer {adapter.name} profile unproven")
    if not os.path.isfile(adapter.executable) or not os.access(
            adapter.executable, os.X_OK):
        raise _problem("provider-unavailable",
                       f"reviewer {adapter.name} executable missing")
    if cancel is not None and cancel.is_set():
        return _cancelled_result(adapter)

    scratch = tempfile.mkdtemp(prefix="ptest-review-")
    try:
        with open(os.path.join(scratch, "schema.json"), "wb") as handle:
            handle.write(schema)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise

    start_new = os.name == "posix"
    if start_new and not _PIDFD_AVAILABLE:
        shutil.rmtree(scratch, ignore_errors=True)
        raise _problem(
            "provider-failed",
            f"reviewer {adapter.name} launch refused: pidfd containment "
            "unavailable on this host, so group cleanup could not be "
            "proved safe")
    try:
        proc = subprocess.Popen(
            list(adapter.argv), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=scratch,
            env=sanitized_child_env(os.environ), shell=False,
            start_new_session=start_new)
    except OSError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise _problem("provider-unavailable",
                       f"reviewer {adapter.name} failed to start") from exc
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    pgid: int | None = None
    sid: int | None = None
    leader_start: int | None = None
    if start_new:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        try:
            sid = os.getsid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            sid = None
        leader_start = _proc_starttime(proc.pid)
        if pgid is None or sid is None or leader_start is None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            raise _problem(
                "provider-failed",
                f"reviewer {adapter.name} launch refused: process identity "
                "unverifiable, so group cleanup could not be proved safe")

    def _emit(phase: str) -> None:
        try:
            progress(ProgressEvent(phase=phase, provider=adapter.name,
                                   elapsed_s=time.monotonic() - start))
        except KeyboardInterrupt:
            raise
        except Exception:
            pass  # progress reporting must never fail or leak the review

    def _feed() -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(packet)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    start = time.monotonic()
    feeder = threading.Thread(target=_feed, daemon=True)
    timed_out = False
    exhausted = False
    out_chunks: list[bytes] = []
    out_len = 0
    log.info("review start provider=%s timeout_s=%d", adapter.name, timeout_s)
    try:
        _emit("reviewing")
        feeder.start()
        if proc.stdout is None or proc.stderr is None:
            _stop_owned(proc, pgid, sid, leader_start)
            raise _problem("provider-unavailable",
                           f"reviewer {adapter.name} pipes unavailable")
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        last_beat = start
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    _stop_owned(proc, pgid, sid, leader_start)
                    log.info("review cancelled provider=%s", adapter.name)
                    return _result(
                        adapter, ok=False, assessment=b"",
                        error="cancelled", exit_code=proc.poll(),
                        timed_out=False, cancelled=True, truncated=False,
                        pid=proc.pid, scratch=scratch)
                elapsed = time.monotonic() - start
                if elapsed >= timeout_s:
                    timed_out = True
                    break
                now = time.monotonic()
                if now - last_beat >= _HEARTBEAT_S:
                    _emit("reviewing")
                    last_beat = now
                remaining = timeout_s - elapsed
                for key, _ in selector.select(min(0.2, remaining)):
                    try:
                        chunk = key.fileobj.read(65536)
                    except (BlockingIOError, OSError):
                        chunk = None
                    if not chunk:
                        try:
                            selector.unregister(key.fileobj)
                        except (KeyError, ValueError):
                            pass
                        continue
                    if key.fileobj is proc.stdout:
                        out_chunks.append(chunk)
                    out_len += len(chunk)
                    if out_len > OUTPUT_MAX_BYTES:
                        exhausted = True
                        break
                if exhausted:
                    break
                if not selector.get_map():
                    if proc.poll() is not None:
                        break
                    if time.monotonic() - start >= timeout_s:
                        timed_out = True
                        break
                    time.sleep(0.05)
        finally:
            selector.close()
        feeder.join(timeout=5)
        if timed_out:
            _stop_owned(proc, pgid, sid, leader_start)
            log.info("review timeout provider=%s", adapter.name)
            return _result(adapter, ok=False, assessment=b"",
                           error="timeout", exit_code=proc.poll(),
                           timed_out=True, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        if exhausted:
            _stop_owned(proc, pgid, sid, leader_start)
            log.info("review output-exhausted provider=%s", adapter.name)
            return _result(adapter, ok=False, assessment=b"",
                           error="output-exhausted", exit_code=proc.poll(),
                           timed_out=False, cancelled=False, truncated=True,
                           pid=proc.pid, scratch=scratch)
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, timeout_s - (time.monotonic()
                                                       - start)))
            except subprocess.TimeoutExpired:
                _stop_owned(proc, pgid, sid, leader_start)
                return _result(adapter, ok=False, assessment=b"",
                               error="timeout", exit_code=proc.poll(),
                               timed_out=True, cancelled=False,
                               truncated=False, pid=proc.pid, scratch=scratch)
        exit_code = proc.poll()
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if exit_code != 0:
            log.info("review provider-failed provider=%s exit=%s",
                     adapter.name, exit_code)
            return _result(adapter, ok=False, assessment=b"",
                           error="provider-failed", exit_code=exit_code,
                           timed_out=False, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        _emit("validating")
        stdout = b"".join(out_chunks)
        ok, assessment, error = _normalize(adapter.name, stdout)
        if not ok:
            log.info("review %s provider=%s stdout_bytes=%d", error,
                     adapter.name, len(stdout))
            return _result(adapter, ok=False, assessment=b"", error=error,
                           exit_code=exit_code, timed_out=False,
                           cancelled=False, truncated=False, pid=proc.pid,
                           scratch=scratch)
        if len(assessment) > OUTPUT_MAX_BYTES:
            return _result(adapter, ok=False, assessment=b"",
                           error="invalid-assessment", exit_code=exit_code,
                           timed_out=False, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        log.info("review ok provider=%s assessment_bytes=%d", adapter.name,
                 len(assessment))
        return _result(adapter, ok=True, assessment=assessment, error="",
                       exit_code=exit_code, timed_out=False, cancelled=False,
                       truncated=False, pid=proc.pid, scratch=scratch)
    except KeyboardInterrupt:
        _stop_owned(proc, pgid, sid, leader_start)
        log.info("review cancelled provider=%s", adapter.name)
        return _result(adapter, ok=False, assessment=b"", error="cancelled",
                       exit_code=proc.poll(), timed_out=False, cancelled=True,
                       truncated=False, pid=proc.pid, scratch=scratch)
    except BaseException:
        _stop_owned(proc, pgid, sid, leader_start)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def launch_reviews(adapter: ReviewerAdapter,
                   requests: Sequence[tuple[bytes, bytes]],
                   timeout_s: int, *,
                   concurrency: int = 4,
                   on_done: Callable[[int, ProviderResult], None] | None = None,
                   progress: Callable[[ProgressEvent], None] | None = None
                   ) -> tuple[ProviderResult, ...]:
    """Run one review per request with bounded concurrency.

    Results stay aligned with ``requests``; a per-item failure marks only
    that item. ``on_done`` fires per item as it completes (via
    ``as_completed``), and ``progress`` — shared thread-safely by every
    worker — carries each item's reviewing heartbeat during long fan-out.
    KeyboardInterrupt in the waiting thread sets the shared cancel event,
    joins every worker, then raises ``review-cancelled``.
    """
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if not isinstance(requests, Sequence):
        raise TypeError("requests must be a sequence of (packet, schema)")
    pairs = list(requests)
    for pair in pairs:
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or not isinstance(pair[0], bytes)
                or not isinstance(pair[1], bytes)):
            raise TypeError("requests must hold (bytes, bytes) pairs")
    if not _is_int(timeout_s):
        raise TypeError("timeout_s must be int")
    if timeout_s < TIMEOUT_MIN_S or timeout_s > TIMEOUT_MAX_S:
        raise _problem("invalid-bound", "timeout_s outside 1..900 seconds")
    if not _is_int(concurrency):
        raise TypeError("concurrency must be int")
    if not 1 <= concurrency <= 8:
        raise _problem("invalid-bound", "concurrency outside 1..8")
    if on_done is not None and not callable(on_done):
        raise TypeError("on_done must be callable or None")
    if progress is not None and not callable(progress):
        raise TypeError("progress must be callable or None")
    if not adapter.qualified:
        raise _problem("provider-unqualified",
                       f"reviewer {adapter.name} profile unproven")
    if not pairs:
        return ()

    lock = threading.Lock()

    def _emit(event: ProgressEvent) -> None:
        if progress is None:
            return None
        try:
            with lock:
                progress(event)
        except KeyboardInterrupt:
            raise
        except Exception:
            pass  # progress reporting must never fail the review
        return None

    cancel = threading.Event()

    def _work(index: int, packet: bytes, schema: bytes) -> ProviderResult:
        try:
            return _launch_one(adapter, packet, schema, timeout_s, _emit,
                               cancel)
        except Problem as problem:
            return ProviderResult(
                provider=adapter.name, ok=False, assessment=b"",
                error=problem.code, exit_code=None, timed_out=False,
                cancelled=False, truncated=False, pid=os.getpid(),
                argv=adapter.argv, scratch="unstarted")

    def _report(index: int, result: ProviderResult) -> None:
        if on_done is None:
            return
        try:
            on_done(index, result)
        except KeyboardInterrupt:
            raise
        except Exception:
            pass  # progress reporting must never fail the review

    results: list = [None] * len(pairs)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency) as executor:
        pending = {executor.submit(_work, index, packet, schema): index
                   for index, (packet, schema) in enumerate(pairs)}
        try:
            for future in concurrent.futures.as_completed(pending):
                index = pending[future]
                results[index] = future.result()
                _report(index, results[index])
        except KeyboardInterrupt:
            cancel.set()
            raise _problem("review-cancelled",
                           "review was cancelled") from None
    return tuple(results)


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def with_model(adapter: ReviewerAdapter, model: str) -> ReviewerAdapter:
    """Return the adapter with only the frozen model flag appended.

    Claude gains ``--model <model>`` and Codex gains ``-m <model>``; any
    other provider raises provider-unqualified. The qualified tool-denial
    canary must be re-run whenever the chosen model changes.
    """
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if not isinstance(model, str) or MODEL_RE.fullmatch(model) is None:
        raise _problem("invalid-bound", f"invalid review model {model!r}")
    if adapter.name == "claude":
        extra = ("--model", model)
    elif adapter.name == "codex":
        extra = ("-m", model)
    else:
        raise _problem("provider-unqualified",
                       f"reviewer {adapter.name} profile unproven")
    return replace(adapter, argv=adapter.argv + extra)


_DISCOVERY_TIMEOUT_S = 20
_DISCOVERY_MAX_BYTES = 4 * 1024 * 1024
_VERSION_TIMEOUT_S = 10
_VERSION_MAX_BYTES = 1024 * 1024


def _spawn_owned(argv: Sequence[str]) -> tuple[subprocess.Popen,
                                              int | None, int | None,
                                              int | None]:
    """Spawn a session-leader child owned by the launch_review machinery.

    Returns ``(proc, pgid, sid, leader_start)`` for :func:`_stop_owned`.
    A Popen ``OSError`` (missing executable) propagates so callers keep
    their unavailable contracts; an unverifiable identity fails closed
    with a Problem instead of launching an unowned child.
    """
    start_new = os.name == "posix"
    if start_new and not _PIDFD_AVAILABLE:
        raise _problem(
            "provider-failed",
            "launch refused: pidfd containment unavailable on this host, "
            "so group cleanup could not be proved safe")
    proc = subprocess.Popen(
        list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=sanitized_child_env(os.environ),
        shell=False, start_new_session=start_new)
    if not start_new:
        return proc, None, None, None
    pgid: int | None = None
    sid: int | None = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    try:
        sid = os.getsid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        sid = None
    leader_start = _proc_starttime(proc.pid)
    if pgid is None or sid is None or leader_start is None:
        _stop_owned(proc, pgid, sid, leader_start)
        raise _problem(
            "provider-failed",
            "launch refused: process identity unverifiable, so group "
            "cleanup could not be proved safe")
    return proc, pgid, sid, leader_start


def _collect_owned(proc: subprocess.Popen, timeout_s: int | float,
                   max_bytes: int) -> bytes | None:
    """Incrementally read stdout up to ``max_bytes`` before ``timeout_s``.

    Returns the bytes on a zero exit, else None (timeout, over-bound
    output, nonzero exit, or read error). Teardown belongs to the caller
    via :func:`_stop_owned`, which also reaps the child.
    """
    if proc.stdout is None:
        return None
    try:
        os.set_blocking(proc.stdout.fileno(), False)
    except (OSError, ValueError):
        return None
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout_s
    selector = selectors.DefaultSelector()
    try:
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
        except (OSError, ValueError, KeyError):
            return None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready = selector.select(min(0.2, remaining))
            except OSError:
                return None
            for key, _ in ready:
                try:
                    chunk = key.fileobj.read(65536)
                except (BlockingIOError, OSError, ValueError):
                    chunk = None
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    continue
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            if not selector.get_map():
                break
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return None
        if proc.poll() != 0:
            return None
        return b"".join(chunks)
    finally:
        selector.close()


def _run_owned_capture(argv: Sequence[str], *, timeout_s: int | float,
                       max_bytes: int) -> bytes | None:
    """Run one owned child synchronously and capture its bounded stdout.

    The child runs as a session leader under the same :func:`_stop_owned`
    machinery as :func:`launch_review`: on timeout, over-bound output, or
    any error the whole group is killed and the child reaped. Returns the
    stdout bytes on a zero exit, else None. A Popen ``OSError`` propagates.
    """
    try:
        proc, pgid, sid, leader_start = _spawn_owned(argv)
    except Problem:
        return None
    outcome: bytes | None = None
    try:
        outcome = _collect_owned(proc, timeout_s, max_bytes)
    except Exception:
        outcome = None
    finally:
        try:
            _stop_owned(proc, pgid, sid, leader_start)
        except Problem:
            # Containment incomplete: fail closed, and teardown must not
            # raise out of a capture call. (An assignment here cannot
            # swallow an in-flight exception; only return could.)
            outcome = None
    return outcome


def discover_model_entries(adapter: ReviewerAdapter) -> tuple[dict, ...]:
    """Listed codex model entries in catalog order, stripped to three keys."""
    stdout = _run_owned_capture(
        [adapter.executable, "debug", "models"],
        timeout_s=_DISCOVERY_TIMEOUT_S, max_bytes=_DISCOVERY_MAX_BYTES)
    if stdout is None:
        return ()
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    entries = []
    for item in models:
        if not isinstance(item, dict):
            continue
        if item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        display = item.get("display_name")
        description = item.get("description")
        entries.append({
            "slug": slug,
            "display_name": display if isinstance(display, str) else "",
            "description": description if isinstance(description, str) else "",
        })
    return tuple(entries)


def discover_models(adapter: ReviewerAdapter) -> tuple[str, ...]:
    """List codex model slugs with visibility ``list``; () on any failure.

    Claude has no listing (aliases are used instead), so non-codex
    adapters report () without launching anything.
    """
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if adapter.name != "codex":
        return ()
    try:
        return tuple(entry["slug"]
                     for entry in discover_model_entries(adapter))
    except OSError:
        return ()


def cli_version(adapter: ReviewerAdapter) -> str | None:
    """First stdout line of ``<executable> --version``; None on failure."""
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    try:
        stdout = _run_owned_capture(
            [adapter.executable, "--version"],
            timeout_s=_VERSION_TIMEOUT_S, max_bytes=_VERSION_MAX_BYTES)
    except OSError:
        return None
    if stdout is None:
        return None
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        return None
    return lines[0].strip()[:128]
