"""Own one admitted process group, reap it, and hand off provisional facts.

Only the live session leader signals its group. DRAINING is not quiescence:
the caller must reap this guard and independently prove group/escape absence.
"""
from __future__ import annotations

import errno
import os
import select
import secrets
import signal
import struct
import subprocess
import time
from dataclasses import dataclass

import psutil

from . import platform, scheduler
from .contracts import (
    CANCEL_GRACE_S, CONTROL_FRAME_MAX_BYTES, MANIFEST_MAX_BYTES,
    DEFAULT_ATTEMPT_DECISION_TIMEOUT_S, GUARD_PROTOCOL_VERSION,
    MAX_COMPOUND_TIMEOUT_S, ControlFrame, LaunchManifest, Problem,
    decode_control_frame, decode_launch_manifest, encode_control_frame,
)

_FRAME_DEADLINE_S = 2.0
_POLL_S = 0.05
_MAX_GROUP_SCAN = 8192
_EXIT_PROTOCOL = 70
_EXIT_REGISTRATION = 75


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="guard", retryable=False)


def _problem_payload(problem: Problem) -> dict:
    return {"code": problem.code, "message": problem.message,
            "phase": problem.phase, "retryable": problem.retryable}


@dataclass
class _State:
    cancel_signal: int | None = None
    problem: Problem | None = None
    spawn_closed: bool = False
    child: subprocess.Popen | None = None
    grace_deadline: float | None = None
    spawned: bool = False
    stop_reason: str | None = None

    def cancel(self, signum: int) -> None:
        # First cancellation wins, including signals reflected by our killpg.
        if self.cancel_signal is None:
            self.cancel_signal = signum
        self.spawn_closed = True

    def fail(self, problem: Problem) -> None:
        if self.problem is None:
            self.problem = problem
        self.cancel(signal.SIGTERM)


class _StartupCancelled(Exception):
    pass


def _read_exact(fd: int, amount: int, deadline: float, state: _State) -> bytes:
    chunks = bytearray()
    while len(chunks) < amount:
        if state.cancel_signal is not None:
            raise _StartupCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _problem("protocol-mismatch", "private frame receive timed out")
        ready, _, _ = select.select([fd], [], [], min(remaining, _POLL_S))
        if not ready:
            continue
        piece = os.read(fd, amount - len(chunks))
        if not piece:
            raise _problem("protocol-mismatch", "private frame was truncated")
        chunks.extend(piece)
    return bytes(chunks)


def _read_manifest(fd: int, state: _State) -> LaunchManifest:
    deadline = time.monotonic() + _FRAME_DEADLINE_S
    prefix = _read_exact(fd, 4, deadline, state)
    (length,) = struct.unpack(">I", prefix)
    if length > MANIFEST_MAX_BYTES:
        raise _problem("protocol-mismatch", "launch manifest declares oversize length")
    manifest = decode_launch_manifest(prefix + _read_exact(fd, length, deadline, state))
    # The manifest pipe contains exactly one frame, unlike the control stream.
    # Require its terminating EOF within the same frame deadline.
    while True:
        if state.cancel_signal is not None:
            raise _StartupCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _problem("protocol-mismatch", "manifest pipe did not close")
        if select.select([fd], [], [], min(remaining, _POLL_S))[0]:
            if os.read(fd, 1):
                raise _problem("protocol-mismatch", "manifest pipe has trailing data")
            return manifest


def _send(fd: int, frame: ControlFrame) -> None:
    raw = encode_control_frame(frame)
    offset = 0
    deadline = time.monotonic() + _FRAME_DEADLINE_S
    while offset < len(raw):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], max(0, remaining))[1]:
            raise _problem("control-unavailable", "private control send timed out")
        try:
            sent = os.write(fd, raw[offset:])
        except BlockingIOError:
            continue
        if sent <= 0:
            raise _problem("control-unavailable", "private control send made no progress")
        offset += sent


def _emit(fd: int, manifest: LaunchManifest, kind: str, payload: dict) -> None:
    _send(fd, ControlFrame(protocol=GUARD_PROTOCOL_VERSION,
                           run_id=manifest.grant.run_id,
                           nonce=manifest.grant.nonce, kind=kind, payload=payload))


class _Control:
    """Incremental bounded frames; EOF removes the fd from all future polls."""

    def __init__(self, fd: int, manifest: LaunchManifest, state: _State):
        self.fd, self.manifest, self.state = fd, manifest, state
        self.read_open = self.write_open = True
        self.pending = bytearray()
        self.deadline: float | None = None
        self.expected_decision: tuple[str, int, str] | None = None
        self.decision: ControlFrame | None = None

    def disconnected(self) -> None:
        self.read_open = self.write_open = False
        self.state.spawn_closed = True
        self.pending.clear()
        self.deadline = None

    def emit(self, kind: str, payload: dict) -> None:
        if not self.write_open:
            return
        try:
            _emit(self.fd, self.manifest, kind, payload)
        except OSError as exc:
            self.disconnected()
            if exc.errno not in (errno.EPIPE, errno.ECONNRESET):
                self.state.fail(_problem("control-unavailable", "private control send failed"))
        except Problem as exc:
            self.disconnected()
            self.state.fail(exc)

    def _parse(self) -> None:
        if len(self.pending) < 4:
            return
        (length,) = struct.unpack(">I", self.pending[:4])
        if length > CONTROL_FRAME_MAX_BYTES:
            raise _problem("protocol-mismatch", "private control declares oversize length")
        if len(self.pending) < 4 + length:
            return
        frame = decode_control_frame(bytes(self.pending), expected_nonce=self.manifest.grant.nonce)
        if frame.run_id != self.manifest.grant.run_id or frame.kind not in {
                "cancel", "parent-closing", "attempt-decision"}:
            raise _problem("protocol-mismatch", "private control is not for this guard")
        self.pending.clear()
        self.deadline = None
        if frame.kind == "attempt-decision":
            expected = self.expected_decision
            actual = (frame.payload["attempt_id"], frame.payload["generation"],
                      frame.payload["gate_token"])
            if expected is None or actual != expected:
                raise _problem(
                    "protocol-mismatch",
                    "attempt decision does not match the active one-use gate")
            self.expected_decision = None
            self.decision = frame
        elif frame.kind == "cancel":
            self.state.cancel(frame.payload["signal"])
        else:
            self.read_open = False
            self.state.spawn_closed = True

    def poll(self, timeout: float = 0) -> None:
        if not self.read_open:
            if timeout:
                select.select([], [], [], timeout)
            return
        try:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise _problem("protocol-mismatch", "private control receive timed out")
                timeout = min(timeout, remaining)
            if not select.select([self.fd], [], [], timeout)[0]:
                return
            # Read exactly one frame at a time. A bounded batch consumes queued
            # controls before launch without letting a flood starve deadlines.
            for _ in range(16):
                size = (4 - len(self.pending)) if len(self.pending) < 4 else (
                    4 + struct.unpack(">I", self.pending[:4])[0] - len(self.pending))
                piece = os.read(self.fd, size)
                if not piece:
                    if self.pending:
                        raise _problem("protocol-mismatch", "private control was truncated")
                    self.disconnected()
                    return
                if not self.pending:
                    self.deadline = time.monotonic() + _FRAME_DEADLINE_S
                self.pending.extend(piece)
                self._parse()
                if not self.read_open:
                    return
        except BlockingIOError:
            return
        except OSError as exc:
            # Closing a stream with unread guard notifications may produce a
            # reset instead of EOF. Both mean the controller disappeared.
            if exc.errno == errno.ECONNRESET and not self.pending:
                self.disconnected()
                return
            self.pending.clear()
            self.read_open = False
            self.state.fail(_problem("protocol-mismatch", "private control read failed"))
        except (Problem, ValueError, TypeError):
            self.pending.clear()
            self.read_open = False
            self.state.fail(_problem("protocol-mismatch", "invalid private control frame"))

    def await_attempt_decision(
            self, *, attempt_id: str, previous_attempt_id: str | None,
            compound_deadline: float) -> bool:
        now = time.monotonic()
        deadline = min(
            now + DEFAULT_ATTEMPT_DECISION_TIMEOUT_S, compound_deadline)
        if deadline <= now:
            self.state.fail(_problem(
                "execution-timeout", "compound execution deadline expired"))
            return False
        gate_token = secrets.token_hex(16)
        generation = self.manifest.grant.generation
        self.expected_decision = (attempt_id, generation, gate_token)
        self.decision = None
        self.emit("attempt-ready", {
            "attempt_id": attempt_id,
            "previous_attempt_id": previous_attempt_id,
            "generation": generation,
            "gate_token": gate_token,
            "deadline_monotonic": deadline,
        })
        while (self.decision is None and not self.state.spawn_closed
               and self.state.cancel_signal is None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.expected_decision = None
                self.state.fail(_problem(
                    "attempt-decision-timeout",
                    "attempt decision watchdog expired"))
                return False
            self.poll(min(_POLL_S, remaining))
        if self.decision is None:
            self.expected_decision = None
            return False
        decision = self.decision
        self.decision = None
        if decision.payload["action"] == "stop":
            self.state.stop_reason = decision.payload["reason"]
            self.state.spawn_closed = True
            return False
        return True


def _signal_group(identity, signum: int) -> None:
    if (identity.pid != os.getpid() or identity.pgid != identity.pid
            or os.getpgrp() != identity.pid or os.getsid(0) != identity.pid):
        raise _problem("ownership-uncertain", "guard no longer anchors its own group")
    os.killpg(identity.pgid, signum)


def _group_needs_cleanup(identity) -> bool:
    """Bounded observation for cancellation effort, NEVER an absence proof.

    Only a caller's post-reap ESRCH probe can prove group absence. A racing fork
    or inaccessible snapshot retains that obligation even after this says no.
    """
    deadline = time.monotonic() + _POLL_S
    try:
        for count, proc in enumerate(psutil.process_iter()):
            if count >= _MAX_GROUP_SCAN or time.monotonic() >= deadline:
                return True
            if proc.pid == identity.pid:
                continue
            try:
                if os.getpgid(proc.pid) == identity.pgid and proc.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (OSError, psutil.Error):
                return True
    except (OSError, psutil.Error):
        return True
    return False


def _cancel_and_reap(state: _State, control: _Control, identity) -> None:
    if state.grace_deadline is None:
        state.grace_deadline = time.monotonic() + CANCEL_GRACE_S
        _signal_group(identity, state.cancel_signal or signal.SIGTERM)
    while True:
        child_done = state.child is None or state.child.poll() is not None
        if child_done and not _group_needs_cleanup(identity):
            return
        remaining = state.grace_deadline - time.monotonic()
        if remaining <= 0:
            _signal_group(identity, signal.SIGKILL)
            # Successful killpg includes us: there can be no later handoff.
            os._exit(_EXIT_PROTOCOL)
        control.poll(min(_POLL_S, remaining))


def _ready(control: _Control, state: _State, deadline: float) -> bool:
    control.poll()
    while control.pending and not state.spawn_closed:
        control.poll(_POLL_S)
    if time.monotonic() >= deadline and not state.spawn_closed:
        state.fail(_problem("execution-timeout", "compound execution deadline expired"))
    return not state.spawn_closed


def _runner_facts(control: _Control, prepared, attempt_id: str, phase: str,
                  result: int | None, problem: Problem | None) -> None:
    control.emit("runner-facts", {
        "attempt_id": attempt_id, "phase": phase, "raw_exit_code": result,
        "report_name": None if prepared.report_path is None else prepared.report_path.name,
        "problem": None if problem is None else _problem_payload(problem),
    })


def _predecessor_quiescent(
        manifest: LaunchManifest, identity, state: _State) -> bool:
    """Require bounded process and scheduler evidence before another spawn."""
    if _group_needs_cleanup(identity):
        state.fail(_problem(
            "ownership-uncertain",
            "a prior phase still has live or unobservable descendants"))
        return False
    lease = next(
        (item for item in scheduler.reconcile(manifest.domain)
         if item.run_id == manifest.grant.run_id),
        None,
    )
    if lease is None or lease.state is not scheduler.LeaseState.RUNNING:
        state.fail(_problem(
            "ownership-uncertain",
            "scheduler ancestry is not safe for a subsequent spawn"))
        return False
    return True


def _run_one(control: _Control, manifest: LaunchManifest, prepared,
             attempt_id: str,
             phase: str, timeout_s: float | None, compound_deadline: float,
             identity, state: _State) -> int | None:
    ready = _ready(control, state, compound_deadline)
    if ready:
        control.emit("phase", {"phase": phase, "attempt_id": attempt_id})
        # This narrows, but cannot atomically eliminate, the check/fork race.
        ready = _ready(control, state, compound_deadline)
    if not ready:
        if state.problem and state.problem.code == "execution-timeout":
            _runner_facts(control, prepared, attempt_id, phase, None, state.problem)
        return None
    deadline = compound_deadline
    timeout_scope = "compound"
    if timeout_s is not None and time.monotonic() + timeout_s < deadline:
        deadline = time.monotonic() + timeout_s
        timeout_scope = "attempt" if phase == "execution" else "setup"
    problem = None
    result = None
    try:
        state.child = subprocess.Popen(
            prepared.argv, cwd=prepared.cwd,
            env=dict(os.environ, **dict(prepared.env_updates)), close_fds=True)
        state.spawned = True
    except (OSError, ValueError):
        problem = _problem("missing-executable", "runner could not be launched")
        state.spawn_closed = True
    else:
        while state.child.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and state.cancel_signal is None:
                state.fail(_problem("execution-timeout", timeout_scope + " execution deadline expired"))
            if state.cancel_signal is not None:
                _cancel_and_reap(state, control, identity)
                break
            control.poll(min(_POLL_S, max(0, remaining)))
        # Cancellation still owns the group after a promptly exiting direct
        # child; a surviving group member must not escape the grace deadline.
        control.poll()
        if state.cancel_signal is not None:
            _cancel_and_reap(state, control, identity)
        result = state.child.wait()
        state.child = None
        problem = state.problem
    if problem is None and not _predecessor_quiescent(
            manifest, identity, state):
        problem = state.problem
        if state.cancel_signal is not None:
            _cancel_and_reap(state, control, identity)
    _runner_facts(control, prepared, attempt_id, phase, result, problem)
    return result


def run_guard(control_fd: int, manifest_fd: int) -> int:
    """Execute only after registration; every ordinary exit contains cleanup."""
    if (isinstance(control_fd, bool) or isinstance(manifest_fd, bool)
            or not isinstance(control_fd, int) or not isinstance(manifest_fd, int)
            or control_fd < 0 or manifest_fd < 0 or control_fd == manifest_fd):
        return _EXIT_PROTOCOL
    state = _State()
    control = identity = None
    handlers = {}
    try:
        def request_cancel(signum, _frame):
            state.cancel(signum)

        # Install before the first potentially blocking manifest/ledger read.
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, request_cancel)
        os.set_blocking(control_fd, False)
        manifest = _read_manifest(manifest_fd, state)
        os.close(manifest_fd)
        manifest_fd = -1
        control = _Control(control_fd, manifest, state)
        control.poll()
        while control.pending and not state.spawn_closed:
            control.poll(_POLL_S)
        if state.problem:
            return _EXIT_PROTOCOL
        if state.cancel_signal is not None:
            return 128 + state.cancel_signal
        if state.spawn_closed:
            return _EXIT_PROTOCOL
        if not os.getpgrp() == os.getpid() == os.getsid(0):
            os.setsid()
        identity = platform.process_identity(os.getpid())
        if identity is None or identity.uid != os.getuid() or identity.pgid != identity.pid:
            return _EXIT_REGISTRATION
        if not scheduler.register_guard(manifest.domain, manifest.grant, identity):
            return _EXIT_REGISTRATION
        control.emit("registered", {"guard": {
            "pid": identity.pid, "birth": identity.birth, "uid": identity.uid, "pgid": identity.pgid,
        }})
        # None is the bounded default, not permission for an unlimited run.
        compound_deadline = time.monotonic() + min(
            manifest.compound_timeout_s or MAX_COMPOUND_TIMEOUT_S, MAX_COMPOUND_TIMEOUT_S)
        if manifest.setup is not None:
            result = _run_one(control, manifest, manifest.setup,
                              manifest.attempt_ids[0],
                              "setup", manifest.setup_timeout_s, compound_deadline, identity, state)
            if result != 0:
                state.spawn_closed = True
        previous_attempt_id = None
        for prepared, attempt_id in zip(
                manifest.attempts, manifest.attempt_ids, strict=True):
            if state.spawn_closed:
                break
            if not _predecessor_quiescent(manifest, identity, state):
                break
            if not control.await_attempt_decision(
                    attempt_id=attempt_id,
                    previous_attempt_id=previous_attempt_id,
                    compound_deadline=compound_deadline):
                if (state.problem is not None
                        and state.problem.code == "execution-timeout"):
                    _runner_facts(
                        control, prepared, attempt_id, "execution", None,
                        state.problem)
                break
            _run_one(control, manifest, prepared, attempt_id, "execution",
                     manifest.attempt_timeout_s, compound_deadline, identity, state)
            previous_attempt_id = attempt_id
        control.poll()
        state.spawn_closed = True
        if state.cancel_signal is not None and state.spawned:
            _cancel_and_reap(state, control, identity)
        if not scheduler.mark_draining(manifest.domain, manifest.grant, identity):
            return _EXIT_REGISTRATION
        control.emit("draining", {"provisional_artifact_id": None})
        if state.problem and state.problem.code != "execution-timeout":
            return _EXIT_PROTOCOL
        return 0 if state.cancel_signal is None else 128 + state.cancel_signal
    except _StartupCancelled:
        return 128 + state.cancel_signal
    except Exception:
        # Guard is an implementation-owned process boundary. Even an unexpected
        # dependency error must not abandon its child or leak raw exception data.
        if identity is not None and control is not None and state.spawned:
            state.cancel(signal.SIGTERM)
            try:
                _cancel_and_reap(state, control, identity)
            except Exception:
                _signal_group(identity, signal.SIGKILL)
        return _EXIT_PROTOCOL
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        for fd in (control_fd, manifest_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


__all__ = ["run_guard"]
