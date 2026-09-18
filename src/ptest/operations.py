"""The admitted command execution lifecycle.

This module is deliberately small in the first execution slice.  A generic
``command`` profile is the only profile that may cross the admission barrier;
native adapters, setup, selection, reports and probes remain explicit
unsupported capabilities.  The scheduler and guard are the authorities for
ownership and process-group quiescence respectively.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import os
import select
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import contracts as C
from . import platform, scheduler
from .runners import adapter_for


_PHASE = "execution"
_GUARD_SCRIPT = (
    "from ptest.guard import run_guard; "
    "raise SystemExit(run_guard(int(__import__('sys').argv[1]), "
    "int(__import__('sys').argv[2])))"
)
_FRAME_TIMEOUT_S = 2.0
_POLL_S = 0.05


def _problem(code: str, message: str, *, phase: str = _PHASE,
             retryable: bool = False) -> C.Problem:
    return C.Problem(code=code, message=message, phase=phase,
                     retryable=retryable)


def _reason(code: str, message: str) -> C.Reason:
    # Public reasons are an intentionally closed vocabulary.  A private guard
    # problem is never copied verbatim into a public result.
    if code not in C.REASON_CODES:
        code = "unknown-input"
    return C.Reason(code=code, message=message)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkout(config: C.Config) -> C.CheckoutIdentity:
    root = (config.checkout.root if config.checkout is not None else
            config.config_path.parent if config.config_path is not None else None)
    if root is None:
        raise _problem("invalid-config", "project root is unavailable", phase="config")
    checkout_id = hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]
    return C.CheckoutIdentity(project_id=config.project_id,
                              checkout_id=checkout_id, root=root)


def _project_root(config: C.Config) -> Path:
    return _checkout(config).root


def _summary(config: C.Config, plan: C.Plan, request: C.RunRequest,
             workers: int) -> C.CommandSummary:
    args = tuple(config.runner.launcher) + tuple(config.runner.args)
    if plan.execution == "full":
        args += tuple(config.runner.full_args)
    elif plan.execution == "scoped":
        args += tuple(request.argv)
    return C.summarize_command(config.runner.kind, plan.mode, args,
                               workers=workers,
                               provenance=("literal-exclusive-command",))


def _plan(request: C.RunRequest) -> C.Plan:
    if request.mode is C.Mode.SCOPED:
        return C.Plan(mode=C.Mode.SCOPED, execution="scoped", static_preview=False)
    # Automatic command mode is a real full command, never a guessed selected
    # subset.  The source/selection lifecycle is intentionally deferred.
    return C.Plan(mode=request.mode, execution="full",
                  reasons=(C.Reason(code="selection-disabled",
                                    message="command profiles execute the configured full gate"),),
                  static_preview=False)


def _effective_config(config: C.Config, request: C.RunRequest,
                      plan: C.Plan, grant: C.Grant) -> C.Config:
    runner = replace(config.runner, workers=grant.slots)
    if plan.execution == "scoped":
        runner = replace(runner, args=runner.args + tuple(request.argv))
    # The caller's Config remains the source/policy authority.  This private
    # copy is only for binding the one admitted command argv.
    return replace(config, runner=runner)


def _attempt(grant: C.Grant, checkout: C.CheckoutIdentity) -> C.AttemptIdentity:
    prefix = f"pt_{checkout.checkout_id[:8]}_{grant.run_id}_a001_w000"
    return C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                             resource_prefix=prefix,
                             worker_count=grant.slots)


def _result(*, run_id: str, checkout: C.CheckoutIdentity, request: C.RunRequest,
            plan: C.Plan, command: C.CommandSummary, status: C.Status,
            phase: str, started: str, runner_code: int | None,
            exit_code: int, origin: str, signal_number: int | None = None,
            granted: C.Grant | None = None, reasons: tuple = (),
            attempt: C.AttemptResult | None = None,
            queue_s: float | None = None, execution_s: float | None = None,
            finalization_s: float | None = None) -> C.RunResult:
    attempts = () if attempt is None else (attempt,)
    timings = C.Timings(queue_s=queue_s, execution_s=execution_s,
                         finalization_s=finalization_s)
    return C.RunResult(
        run_id=run_id, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=request.mode, status=status,
        phase=phase, started_at=started, finished_at=_iso_now(), plan=plan,
        command=command,
        granted_workers=None if granted is None else granted.slots,
        memory_estimate_mb=None if granted is None else granted.memory_estimate_mb,
        reserved_memory_mb=None if granted is None else granted.reserved_memory_mb,
        runner_exit_code=runner_code, exit_code=exit_code,
        exit_origin=origin, signal=signal_number, source_valid=False,
        full_gate_eligible=False, baseline_published=False, counts=None,
        timings=timings, attempts=attempts, reasons=tuple(reasons),
        limitations=(_reason("unsupported-capability",
                             "command execution has no inventory or source identity"),),
    )


class _Signals:
    def __init__(self) -> None:
        self.number: int | None = None

    def handler(self, signum, _frame) -> None:
        if self.number is None:
            self.number = int(signum)


class _Frames:
    """Receive and authenticate one bounded guard frame at a time."""

    def __init__(self, peer: socket.socket, grant: C.Grant,
                 launched: C.ProcessIdentity | None):
        self.peer = peer
        self.grant = grant
        self.launched = launched
        self.pending = bytearray()
        self.eof = False
        self.invalid: C.Problem | None = None
        self.registered = False
        self.phase = False
        self.facts: dict | None = None
        self.draining = False

    def _fail(self, message: str) -> None:
        if self.invalid is None:
            self.invalid = _problem("protocol-mismatch", message, phase="guard")

    def _accept(self, frame: C.ControlFrame) -> None:
        if frame.run_id != self.grant.run_id or frame.nonce != self.grant.nonce:
            self._fail("guard frame identity does not match the grant")
            return
        if frame.kind == "registered":
            if self.registered or self.phase or self.facts is not None or self.draining:
                self._fail("registered frame was duplicated or out of order")
                return
            try:
                identity = C.ProcessIdentity(**frame.payload["guard"])
            except (TypeError, ValueError):
                self._fail("registered guard identity is invalid")
                return
            if self.launched is None or identity != self.launched:
                self._fail("registered guard identity does not match the launch")
                return
            self.registered = True
            return
        if frame.kind == "phase":
            if not self.registered or self.phase or self.facts is not None or self.draining:
                self._fail("phase frame was out of order")
                return
            if (frame.payload["phase"] != "execution" or
                    frame.payload["attempt_id"] != "a001"):
                self._fail("phase frame does not match the command attempt")
                return
            self.phase = True
            return
        if frame.kind == "runner-facts":
            if not self.phase or self.facts is not None or self.draining:
                self._fail("runner facts were duplicated or out of order")
                return
            if (frame.payload["attempt_id"] != "a001" or
                    frame.payload["phase"] != "execution" or
                    frame.payload["report_name"] is not None):
                self._fail("runner facts do not match the command attempt")
                return
            self.facts = dict(frame.payload)
            return
        if frame.kind == "draining":
            if self.facts is None or self.draining:
                self._fail("draining frame was duplicated or out of order")
                return
            self.draining = True
            return
        self._fail("caller-only control frame received from guard")

    def drain(self) -> None:
        if self.eof or self.invalid is not None:
            return
        try:
            while True:
                piece = self.peer.recv(65536)
                if not piece:
                    self.eof = True
                    if self.pending:
                        self._fail("guard control frame was truncated")
                    return
                self.pending.extend(piece)
                while len(self.pending) >= 4:
                    size = int.from_bytes(self.pending[:4], "big")
                    if size > C.CONTROL_FRAME_MAX_BYTES:
                        self._fail("guard control frame exceeds its bound")
                        return
                    if len(self.pending) < size + 4:
                        break
                    raw = bytes(self.pending[:size + 4])
                    del self.pending[:size + 4]
                    try:
                        frame = C.decode_control_frame(raw,
                                                       expected_nonce=self.grant.nonce)
                    except C.Problem:
                        self._fail("guard control frame failed authentication")
                        return
                    self._accept(frame)
                    if self.invalid is not None:
                        return
        except (BlockingIOError, InterruptedError):
            return
        except (OSError, ValueError, TypeError):
            self._fail("guard control channel failed")


def _send_cancel(peer: socket.socket, grant: C.Grant, signum: int) -> bool:
    try:
        frame = C.ControlFrame(protocol=1, run_id=grant.run_id,
                               nonce=grant.nonce, kind="cancel",
                               payload={"signal": signum})
        peer.sendall(C.encode_control_frame(frame))
        return True
    except (OSError, C.Problem):
        return False


def _launch_guard(domain: C.DomainPaths, grant: C.Grant,
                  prepared: C.PreparedRun) -> tuple[subprocess.Popen, socket.socket,
                                                    _Frames]:
    manifest = C.LaunchManifest(
        protocol=1, domain=domain, grant=grant, setup=None,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=C.DEFAULT_SETUP_TIMEOUT_S,
        attempt_timeout_s=C.DEFAULT_ATTEMPT_TIMEOUT_S,
        compound_timeout_s=C.MAX_COMPOUND_TIMEOUT_S,
    )
    guard_peer, controller = socket.socketpair()
    manifest_read, manifest_write = os.pipe()
    process: subprocess.Popen | None = None
    try:
        launched = platform.process_identity
        process = subprocess.Popen(
            (os.fspath(Path(sys.executable)), "-c", _GUARD_SCRIPT,
             str(guard_peer.fileno()), str(manifest_read)),
            close_fds=True, pass_fds=(guard_peer.fileno(), manifest_read),
            start_new_session=True,
        )
        guard_peer.close()
        os.close(manifest_read)
        manifest_read = -1
        payload = C.encode_launch_manifest(manifest)
        view = memoryview(payload)
        while view:
            written = os.write(manifest_write, view)
            view = view[written:]
        os.close(manifest_write)
        manifest_write = -1
        controller.setblocking(False)
        launched_identity = launched(process.pid)
        return process, controller, _Frames(controller, grant, launched_identity)
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        guard_peer.close()
        controller.close()
        for fd in (manifest_read, manifest_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        raise


def _run_guard(domain: C.DomainPaths, grant: C.Grant,
               prepared: C.PreparedRun, signals: _Signals) -> tuple[int, _Frames,
                                                                      float]:
    process, control, frames = _launch_guard(domain, grant, prepared)
    started = time.monotonic()
    cancel_sent = False
    try:
        while process.poll() is None:
            if signals.number is not None and not cancel_sent:
                cancel_sent = _send_cancel(control, grant, signals.number)
            wait = _POLL_S
            ready, _, _ = select.select([control], [], [], wait)
            if ready:
                frames.drain()
            if frames.invalid is not None and not cancel_sent:
                cancel_sent = _send_cancel(control, grant, signal.SIGTERM)
            if time.monotonic() - started > C.MAX_COMPOUND_TIMEOUT_S and not cancel_sent:
                cancel_sent = _send_cancel(control, grant, signal.SIGTERM)
        process.wait()
        # The guard closes its end in finally.  Drain already queued frames and
        # require EOF/truncation evidence before finalization.
        deadline = time.monotonic() + _FRAME_TIMEOUT_S
        while not frames.eof and time.monotonic() < deadline:
            ready, _, _ = select.select([control], [], [], min(_POLL_S, deadline - time.monotonic()))
            if ready:
                frames.drain()
            elif process.poll() is not None:
                frames.drain()
        return int(process.returncode), frames, time.monotonic() - started
    finally:
        control.close()


def _cancel_result(run_id: str, checkout: C.CheckoutIdentity,
                   request: C.RunRequest, plan: C.Plan, command: C.CommandSummary,
                   signal_number: int, queue_s: float) -> C.RunResult:
    return _result(run_id=run_id, checkout=checkout, request=request, plan=plan,
                   command=command, status=C.Status.CANCELLED, phase="admission",
                   started=_iso_now(), runner_code=None,
                   exit_code=128 + signal_number, origin="signal",
                   signal_number=signal_number, queue_s=queue_s)


def execute(domain: C.DomainPaths, config: C.Config,
            request: C.RunRequest) -> C.RunResult:
    """Run one admitted generic command and return its typed outcome."""
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.Config):
        raise TypeError("execute requires DomainPaths and Config")
    if not isinstance(request, C.RunRequest):
        raise TypeError("execute requires RunRequest")
    if config.runner.kind is not C.RunnerKind.COMMAND:
        raise _problem("unsupported-capability", "native profile execution is deferred")
    if config.setup is not None or request.shadow or request.probe is not None:
        raise _problem("unsupported-capability", "setup, shadow and probe execution are deferred")
    adapter = adapter_for(config.runner.kind)
    if not adapter.requires_exclusive(config):
        raise _problem("unsupported-capability", "command execution requires exclusive admission")

    checkout = _checkout(config)
    run_id = secrets.token_hex(16)
    plan = _plan(request)
    # The command summary is redacted and never includes token values.
    requested_slots = min(config.runner.workers,
                          config.runner.workers if request.workers is None else request.workers)
    command = _summary(config, plan, request, requested_slots)
    started = _iso_now()
    signals = _Signals()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, signals.handler)
    enqueued_at = time.monotonic()
    ticket = None
    grant = None
    try:
        owner = platform.process_identity(os.getpid())
        if owner is None:
            raise _problem("ownership-uncertain", "caller identity cannot be verified")
        memory = config.resources.memory_mb_per_worker or None
        admission = C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner,
            slots=requested_slots, exclusive=True,
            locks=config.resources.locks, memory_mb=memory,
            deadline=time.monotonic() + request.queue_timeout_s,
            fixture=domain.fixture,
        )
        ticket = scheduler.enqueue(domain, admission)
        while True:
            state = scheduler.poll(domain, ticket)
            if signals.number is not None and state.state in {
                    C.LeaseState.QUEUED, C.LeaseState.GRANTED}:
                if scheduler.cancel_pending(domain, ticket, owner):
                    return _cancel_result(run_id, checkout, request, plan, command,
                                          signals.number, time.monotonic() - enqueued_at)
            if state.state is C.LeaseState.GRANTED:
                grant = state.grant
                break
            if state.state is C.LeaseState.CANCELLED:
                if state.problem is not None:
                    raise state.problem
                raise _problem("queue-timeout", "admission did not complete", retryable=True)
            if state.problem is not None and state.state is not C.LeaseState.QUEUED:
                raise state.problem
            if time.monotonic() >= admission.deadline:
                raise _problem("queue-timeout", "admission queue timeout", retryable=True)
            time.sleep(min(C.SCHEDULER_POLL_S, max(0, admission.deadline - time.monotonic())))
        if grant is None:
            raise _problem("ownership-uncertain", "scheduler grant was incomplete")
        # A signal delivered in the narrow GRANTED window must cancel before
        # any guard is spawned.  If the CAS loses to registration, the guard
        # receives the authenticated cancellation frame immediately.
        if signals.number is not None and scheduler.cancel_pending(domain, ticket, owner):
            return _cancel_result(run_id, checkout, request, plan, command,
                                  signals.number, time.monotonic() - enqueued_at)
        attempt = _attempt(grant, checkout)
        effective = _effective_config(config, request, plan, grant)
        prepared = adapter.prepare(effective, plan, grant, attempt)
        prepared = replace(prepared, env_updates=prepared.env_updates + (
            ("PTEST_PROJECT_ID", checkout.project_id),
            ("PTEST_CHECKOUT_ID", checkout.checkout_id),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_ATTEMPT_ID", attempt.attempt_id),
            ("PTEST_WORKER_ID", "w000"),
            ("PTEST_RESOURCE_PREFIX", attempt.resource_prefix),
        ))
        raw_guard, frames, execution_s = _run_guard(domain, grant, prepared, signals)
        raw = None if frames.facts is None else frames.facts["raw_exit_code"]
        protocol_valid = (frames.invalid is None and frames.registered and
                          frames.phase and frames.facts is not None and frames.draining and
                          frames.eof)
        if not protocol_valid:
            attempt_result = C.AttemptResult(
                attempt_id="a001", phase="execution", status=C.Status.INCOMPLETE,
                raw_exit_code=raw, final_exit_code=70, source_valid=False,
                inventory_complete=False,
                timings=C.Timings(execution_s=execution_s),
            )
            return _result(run_id=run_id, checkout=checkout, request=request,
                           plan=plan, command=command, status=C.Status.INCOMPLETE,
                           phase="execution", started=started, runner_code=raw,
                           exit_code=(raw if raw not in (None, 0) else 70),
                           origin="runner" if raw not in (None, 0) else "ptest",
                           granted=grant,
                           reasons=(_reason("protocol-mismatch", "guard handoff was incomplete"),),
                           attempt=attempt_result,
                           queue_s=time.monotonic() - enqueued_at,
                           execution_s=execution_s)
        # begin_finalization is intentionally after both the authenticated
        # in-band handoff and guard reaping; finish is always last.
        proof = scheduler.begin_finalization(domain, grant)
        signal_number = None
        final_code = raw
        status = C.Status.PASSED
        origin = "runner"
        if signals.number is not None:
            signal_number = signals.number
            final_code = 128 + signal_number
            status = C.Status.CANCELLED
            origin = "signal"
        elif raw is None:
            final_code = 127
            status = C.Status.FAILED
        elif raw < 0:
            signal_number = -raw
            final_code = 128 + signal_number
            status = C.Status.FAILED
            origin = "signal"
        elif raw != 0:
            final_code = raw
            status = C.Status.FAILED
        attempt_result = C.AttemptResult(
            attempt_id="a001", phase="execution", status=status,
            raw_exit_code=raw, final_exit_code=final_code, source_valid=False,
            inventory_complete=False, timings=C.Timings(execution_s=execution_s),
        )
        result = _result(run_id=run_id, checkout=checkout, request=request,
                         plan=plan, command=command, status=status,
                         phase="complete", started=started, runner_code=raw,
                         exit_code=final_code if final_code is not None else 70,
                         origin=origin, signal_number=signal_number, granted=grant,
                         attempt=attempt_result, queue_s=time.monotonic() - enqueued_at,
                         execution_s=execution_s)
        scheduler.finish(domain, grant, proof, C.Finalization(
            outcome_id=None, status=status, exit_code=result.exit_code,
            source_valid=False, committed=True,
        ))
        return result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


__all__ = ["execute"]
