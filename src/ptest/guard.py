"""Private local guard for one scheduler-admitted process group.

The guard has exactly one authority: launch and reap a registered process
group, then make the scheduler's authenticated DRAINING handoff.  It neither
releases a lease nor publishes a final result.
"""
from __future__ import annotations

import errno
import os
import select
import signal
import struct
import subprocess
import time

from . import platform, scheduler
from .contracts import (
    CANCEL_GRACE_S,
    CONTROL_FRAME_MAX_BYTES,
    MANIFEST_MAX_BYTES,
    ControlFrame,
    LaunchManifest,
    Problem,
    decode_control_frame,
    decode_launch_manifest,
    encode_control_frame,
)

_PHASE = "guard"
_FRAME_DEADLINE_S = 2.0
_EXIT_PROTOCOL = 70
_EXIT_REGISTRATION = 75
_EXIT_CANCELLED = 128 + signal.SIGTERM


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase=_PHASE, retryable=False)


def _problem_payload(problem: Problem) -> dict:
    return {"code": problem.code, "message": problem.message,
            "phase": problem.phase, "retryable": problem.retryable}


def _read_exact(fd: int, amount: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < amount:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _problem("protocol-mismatch", "private frame receive timed out")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise _problem("protocol-mismatch", "private frame receive timed out")
        try:
            piece = os.read(fd, amount - len(chunks))
        except OSError as exc:
            raise _problem("protocol-mismatch", "private frame receive failed") from exc
        if not piece:
            raise _problem("protocol-mismatch", "private frame was truncated")
        chunks.extend(piece)
    return bytes(chunks)


def _read_manifest(fd: int) -> LaunchManifest:
    deadline = time.monotonic() + _FRAME_DEADLINE_S
    prefix = _read_exact(fd, 4, deadline)
    (length,) = struct.unpack(">I", prefix)
    if length > MANIFEST_MAX_BYTES:
        raise _problem("protocol-mismatch", "launch manifest declares oversize length")
    try:
        return decode_launch_manifest(prefix + _read_exact(fd, length, deadline))
    except Problem:
        raise
    except (TypeError, ValueError) as exc:
        raise _problem("protocol-mismatch", "launch manifest is invalid") from exc


def _send(fd: int, frame: ControlFrame) -> None:
    raw = encode_control_frame(frame)
    offset = 0
    while offset < len(raw):
        try:
            sent = os.write(fd, raw[offset:])
        except OSError as exc:
            raise _problem("control-unavailable", "private control send failed") from exc
        if sent <= 0:
            raise _problem("control-unavailable", "private control send made no progress")
        offset += sent


def _emit(fd: int, manifest: LaunchManifest, kind: str, payload: dict) -> None:
    _send(fd, ControlFrame(protocol=1, run_id=manifest.grant.run_id,
                           nonce=manifest.grant.nonce, kind=kind, payload=payload))


def _command_environment(updates: tuple[tuple[str, str], ...]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(dict(updates))
    return env


def _cancel_own_group(identity) -> None:
    """Signal only the group the guard anchored with setsid()."""
    if identity.pid != os.getpid() or identity.pgid != identity.pid:
        raise _problem("ownership-uncertain", "guard no longer anchors its own group")
    try:
        os.killpg(identity.pgid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise _problem("ownership-uncertain", "guard group cannot be signalled") from exc


def _force_kill_own_group(identity) -> None:
    """Last-resort abnormal termination; this deliberately kills the guard too."""
    if identity.pid != os.getpid() or identity.pgid != identity.pid:
        raise _problem("ownership-uncertain", "guard no longer anchors its own group")
    try:
        os.killpg(identity.pgid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise _problem("ownership-uncertain", "guard group cannot be killed") from exc


def _consume_controls(fd: int, manifest: LaunchManifest, cancelled: list[bool],
                      parent_closed: list[bool]) -> bool:
    """Read one controller frame.  EOF permanently closes future spawning."""
    try:
        prefix = os.read(fd, 4)
    except BlockingIOError:
        return True
    except OSError as exc:
        raise _problem("protocol-mismatch", "private control read failed") from exc
    if not prefix:
        parent_closed[0] = True
        return False
    if len(prefix) < 4:
        # Pipes and stream sockets may split a valid four-byte prefix.
        prefix += _read_exact(fd, 4 - len(prefix), time.monotonic() + _FRAME_DEADLINE_S)
    (length,) = struct.unpack(">I", prefix)
    if length > CONTROL_FRAME_MAX_BYTES:
        raise _problem("protocol-mismatch", "private control declares oversize length")
    frame = decode_control_frame(
        prefix + _read_exact(fd, length, time.monotonic() + _FRAME_DEADLINE_S),
        expected_nonce=manifest.grant.nonce)
    if frame.run_id != manifest.grant.run_id or frame.kind not in {"cancel", "parent-closing"}:
        raise _problem("protocol-mismatch", "private control is not for this guard")
    if frame.kind == "cancel":
        cancelled[0] = True
    else:
        parent_closed[0] = True
    return True


def _observe_control(fd: int, manifest: LaunchManifest, cancelled: list[bool],
                     parent_closed: list[bool], timeout: float) -> None:
    ready, _, _ = select.select([fd], [], [], timeout)
    if ready:
        _consume_controls(fd, manifest, cancelled, parent_closed)


def _run_one(control_fd: int, manifest: LaunchManifest, prepared, attempt_id: str,
             phase: str, timeout_s: float | None, identity, cancelled: list[bool],
             parent_closed: list[bool]) -> int | None:
    _observe_control(control_fd, manifest, cancelled, parent_closed, 0)
    if cancelled[0] or parent_closed[0]:
        return None
    _emit(control_fd, manifest, "phase", {"phase": phase, "attempt_id": attempt_id})
    try:
        child = subprocess.Popen(
            prepared.argv, cwd=prepared.cwd, env=_command_environment(prepared.env_updates),
            close_fds=True,
        )
    except (OSError, ValueError):
        _emit(control_fd, manifest, "runner-facts", {
            "attempt_id": attempt_id, "phase": phase, "raw_exit_code": None,
            "report_name": None, "problem": _problem_payload(
                _problem("missing-executable", "runner could not be launched")),
        })
        return None
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    cancel_deadline: float | None = None
    while child.poll() is None:
        _observe_control(control_fd, manifest, cancelled, parent_closed, 0.05)
        if cancelled[0] and cancel_deadline is None:
            _cancel_own_group(identity)
            cancel_deadline = time.monotonic() + CANCEL_GRACE_S
        if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
            _force_kill_own_group(identity)
            raise AssertionError("SIGKILL of the guard group unexpectedly returned")
        if deadline is not None and time.monotonic() >= deadline:
            # A timed-out attempt is cancellation: it may not fall through and
            # start a later compound attempt after the child finally exits.
            cancelled[0] = True
            _cancel_own_group(identity)
            cancel_deadline = time.monotonic() + CANCEL_GRACE_S
            deadline = None
    result = child.returncode
    _emit(control_fd, manifest, "runner-facts", {
        "attempt_id": attempt_id, "phase": phase, "raw_exit_code": result,
        "report_name": None if prepared.report_path is None else prepared.report_path.name,
        "problem": None,
    })
    return result


def run_guard(control_fd: int, manifest_fd: int) -> int:
    """Run a bounded private manifest and emit only provisional facts."""
    if (isinstance(control_fd, bool) or isinstance(manifest_fd, bool)
            or not isinstance(control_fd, int) or not isinstance(manifest_fd, int)):
        return _EXIT_PROTOCOL
    cancelled = [False]
    parent_closed = [False]
    old_handlers = {}
    try:
        manifest = _read_manifest(manifest_fd)
        # A guard must be a session leader before it acquires scheduler authority.
        os.setsid()
        identity = platform.process_identity(os.getpid())
        if identity is None or identity.uid != os.getuid() or identity.pgid != identity.pid:
            return _EXIT_REGISTRATION
        if not scheduler.register_guard(manifest.domain, manifest.grant, identity):
            return _EXIT_REGISTRATION
        _emit(control_fd, manifest, "registered", {"guard": {
            "pid": identity.pid, "birth": identity.birth, "uid": identity.uid,
            "pgid": identity.pgid,
        }})

        def request_cancel(_signum, _frame) -> None:
            cancelled[0] = True

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_cancel)
        if manifest.setup is not None:
            setup_result = _run_one(control_fd, manifest, manifest.setup, "a001", "setup",
                                    manifest.setup_timeout_s, identity, cancelled, parent_closed)
            if setup_result != 0:
                # Setup is part of the compound attempt; a failed or unstarted
                # setup cannot safely fall through into an execution attempt.
                parent_closed[0] = True
        for prepared, attempt_id in zip(manifest.attempts, manifest.attempt_ids, strict=True):
            if parent_closed[0] or cancelled[0]:
                break
            _run_one(control_fd, manifest, prepared, attempt_id, "execution",
                     manifest.attempt_timeout_s, identity, cancelled, parent_closed)
        if cancelled[0]:
            return _EXIT_CANCELLED
        # All direct children are reaped above and spawning is permanently closed.
        parent_closed[0] = True
        if scheduler.mark_draining(manifest.domain, manifest.grant, identity):
            _emit(control_fd, manifest, "draining", {"provisional_artifact_id": None})
            return 0
        return _EXIT_REGISTRATION
    except (Problem, OSError, ValueError):
        return _EXIT_PROTOCOL
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        for fd in (control_fd, manifest_fd):
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = ["run_guard"]
