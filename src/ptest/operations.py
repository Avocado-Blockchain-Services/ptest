"""The admitted command execution lifecycle.

This module admits the bounded execution slice.  Generic ``command`` profiles
retain their exclusive lifecycle, while Pytest basic-serial scoped runs use one
nonexclusive slot and an executor-owned terminal report.  The scheduler and
guard are the authorities for ownership and process-group quiescence.
"""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
import select
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import contracts as C
from . import config as config_api, files, platform, render, reports, scheduler, source
from .runners import adapter_for


_PHASE = "execution"
_GUARD_SCRIPT = (
    "from ptest.guard import run_guard; "
    "raise SystemExit(run_guard(int(__import__('sys').argv[1]), "
    "int(__import__('sys').argv[2])))"
)
_FRAME_TIMEOUT_S = 2.0
_POLL_S = 0.05
_SETUP_MARKER_NAME = "setup-fingerprint.json"
_SETUP_MARKER_MAX_BYTES = 8192
_SETUP_INPUTS = (
    "uv.lock", "pyproject.toml", "package-lock.json", "package.json",
    "pnpm-lock.yaml", "yarn.lock",
)


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
        if config.runner.kind is C.RunnerKind.PYTEST:
            args += tuple(config.runner.test_roots)
    elif plan.execution == "scoped":
        args += tuple(request.argv)
    return C.summarize_command(config.runner.kind, plan.mode, args,
                               workers=workers,
                               provenance=(("pytest-native-bridge",)
                                           if config.runner.kind is C.RunnerKind.PYTEST
                                           else ("literal-exclusive-command",)))


def _plan(request: C.RunRequest,
          runner_kind: C.RunnerKind | None = None) -> C.Plan:
    if request.mode is C.Mode.SCOPED:
        return C.Plan(mode=C.Mode.SCOPED, execution="scoped", static_preview=False)
    if request.mode is C.Mode.FULL and runner_kind is C.RunnerKind.PYTEST:
        return C.Plan(mode=C.Mode.FULL, execution="full", static_preview=False)
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


def _required_paths_issue(config: C.Config,
                          checkout: C.CheckoutIdentity) -> str | None:
    setup = config.setup
    if setup is None:
        return None
    for relative in setup.required_paths:
        path = checkout.root / relative
        try:
            path.lstat()
        except FileNotFoundError:
            return f"required setup path is missing: {relative}"
        except OSError:
            return f"required setup path cannot be verified: {relative}"
        try:
            path.stat()
        except FileNotFoundError:
            return f"required setup path is missing: {relative}"
        except OSError:
            return f"required setup path cannot be verified: {relative}"
    return None


def _setup_fingerprint(config: C.Config,
                       checkout: C.CheckoutIdentity) -> str:
    """Digest the declared setup and local tool/lock inputs, boundedly."""
    setup = config.setup
    if setup is None:
        raise _problem("invalid-config", "setup fingerprint requires a declaration",
                       phase="setup")
    tool_token = setup.argv[0]
    tool_path = None
    if os.path.isabs(tool_token) or "/" in tool_token:
        tool_path = (Path(tool_token) if os.path.isabs(tool_token)
                     else checkout.root / tool_token)
    else:
        for directory in os.get_exec_path(os.environ):
            candidate = Path(directory) / tool_token
            try:
                stamp = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
                tool_path = candidate
                break
    tool = {"token": tool_token, "path": None, "digest": None, "size": None}
    if tool_path is not None:
        resolved = Path(os.path.realpath(tool_path))
        tool["path"] = str(resolved)
        try:
            stamp = resolved.lstat()
            data = files.read_regular(resolved.parent, resolved.name, 64 * 1024 * 1024)
            tool["digest"] = hashlib.sha256(data).hexdigest()
            tool["size"] = stamp.st_size
        except (C.Problem, OSError):
            # The guard remains the authority for launchability.  A missing or
            # unreadable tool has a distinct identity and cannot look current.
            tool["digest"] = "<unavailable>"
    inputs = []
    for relative in _SETUP_INPUTS:
        path = checkout.root / relative
        try:
            stamp = path.lstat()
        except FileNotFoundError:
            continue
        read_root = checkout.root
        read_relative = relative
        if stat.S_ISLNK(stamp.st_mode):
            resolved = Path(os.path.realpath(path))
            try:
                target_stamp = resolved.lstat()
            except (FileNotFoundError, OSError):
                raise _problem("state-unavailable",
                               "setup tool/lock fingerprint is unavailable",
                               phase="setup") from None
            if not stat.S_ISREG(target_stamp.st_mode):
                continue
            read_root = resolved.parent
            read_relative = resolved.name
        elif not stat.S_ISREG(stamp.st_mode):
            continue
        try:
            data = files.read_regular(read_root, read_relative, 16 * 1024 * 1024)
        except (C.Problem, OSError):
            raise _problem("state-unavailable",
                           "setup tool/lock fingerprint is unavailable",
                           phase="setup") from None
        inputs.append({"path": relative, "digest": hashlib.sha256(data).hexdigest(),
                       "size": len(data)})
    payload = {
        "argv": list(setup.argv),
        "required_paths": list(setup.required_paths),
        "tool": tool,
        "inputs": inputs,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _setup_marker_dir(domain: C.DomainPaths,
                      checkout: C.CheckoutIdentity,
                      *, create: bool) -> Path | None:
    parent = Path(domain.root) / "checkouts"
    if create:
        parent = files.ensure_private_dir(Path(domain.root), "checkouts")
        return files.ensure_private_dir(parent, checkout.checkout_id)
    try:
        parent.lstat()
    except FileNotFoundError:
        return None
    files.validate_private_dir(parent)
    directory = parent / checkout.checkout_id
    try:
        directory.lstat()
    except FileNotFoundError:
        return None
    files.validate_private_dir(directory)
    return directory


def _stored_setup_fingerprint(domain: C.DomainPaths,
                              checkout: C.CheckoutIdentity) -> str | None:
    directory = _setup_marker_dir(domain, checkout, create=False)
    if directory is None:
        return None
    marker = directory / _SETUP_MARKER_NAME
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    files.validate_private_file(marker)
    try:
        value = json.loads(files.read_regular(
            directory, _SETUP_MARKER_NAME, _SETUP_MARKER_MAX_BYTES).decode("utf-8"))
    except (C.Problem, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return "<invalid>"
    if (not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("checkout_id") != checkout.checkout_id
            or value.get("project_id") != checkout.project_id
            or not isinstance(value.get("fingerprint"), str)
            or len(value["fingerprint"]) != 64):
        return "<invalid>"
    return value["fingerprint"]


def _record_setup_fingerprint(domain: C.DomainPaths,
                              checkout: C.CheckoutIdentity,
                              fingerprint: str) -> None:
    directory = _setup_marker_dir(domain, checkout, create=True)
    assert directory is not None
    payload = json.dumps({
        "version": 1,
        "checkout_id": checkout.checkout_id,
        "project_id": checkout.project_id,
        "fingerprint": fingerprint,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    files.publish_atomic(directory, _SETUP_MARKER_NAME, payload)


def _required_setup_state(config: C.Config,
                          checkout: C.CheckoutIdentity,
                          domain: C.DomainPaths) -> str | None:
    """Return a bounded missing/stale reason, or None when setup is current."""
    if config.setup is None:
        return None
    issue = _required_paths_issue(config, checkout)
    current = _setup_fingerprint(config, checkout)
    stored = _stored_setup_fingerprint(domain, checkout)
    if issue is not None:
        return issue
    if stored is None:
        return "required setup fingerprint baseline is stale or absent"
    if stored == "<invalid>" or stored != current:
        return "required setup paths are stale for the current tool/lock inputs"
    return None


def _setup_prepared(config: C.Config, checkout: C.CheckoutIdentity,
                    request: C.RunRequest, prepared: C.PreparedRun,
                    domain: C.DomainPaths,
                    grant: C.Grant, attempt: C.AttemptIdentity) -> C.PreparedRun | None:
    """Build the declared setup child, if required, without running it."""
    if config.setup is None:
        return None
    reason = _required_setup_state(config, checkout, domain)
    if reason is None:
        return None
    if request.no_setup:
        raise _problem("unsupported-capability",
                       f"{reason}; --no-setup forbids required setup",
                       phase="setup")
    setup = config.setup
    identity = (
        ("PTEST_PROJECT_ID", checkout.project_id),
        ("PTEST_CHECKOUT_ID", checkout.checkout_id),
        ("PTEST_RUN_ID", grant.run_id),
        ("PTEST_ATTEMPT_ID", attempt.attempt_id),
        ("PTEST_WORKER_ID", "w000"),
        ("PTEST_RESOURCE_PREFIX", attempt.resource_prefix),
    )
    return C.PreparedRun(
        argv=setup.argv,
        cwd=checkout.root,
        env_updates=identity,
        capability=prepared.capability,
        summary=C.summarize_command(
            C.RunnerKind.PYTEST, C.Mode.SCOPED, setup.argv,
            workers=grant.slots, provenance=("declared-setup",)),
    )


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
            attempts_extra: tuple = (),
            limitations: tuple = (), input_before: C.InputSnapshot | None = None,
            input_after: C.InputSnapshot | None = None,
            source_valid: bool = False,
            queue_s: float | None = None, setup_s: float | None = None,
            execution_s: float | None = None,
            finalization_s: float | None = None) -> C.RunResult:
    attempts = tuple(attempts_extra) + (() if attempt is None else (attempt,))
    timings = C.Timings(queue_s=queue_s, execution_s=execution_s,
                         setup_s=setup_s, finalization_s=finalization_s)
    return C.RunResult(
        run_id=run_id, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=request.mode, status=status,
        phase=phase, started_at=started, finished_at=_iso_now(), plan=plan,
        command=command,
        granted_workers=None if granted is None else granted.slots,
        memory_estimate_mb=None if granted is None else granted.memory_estimate_mb,
        reserved_memory_mb=None if granted is None else granted.reserved_memory_mb,
        runner_exit_code=runner_code, exit_code=exit_code,
        exit_origin=origin, signal=signal_number, source_valid=source_valid,
        full_gate_eligible=False, baseline_published=False, counts=None,
        timings=timings, attempts=attempts, reasons=tuple(reasons),
        limitations=(_reason("unsupported-capability",
                             "command execution has no inventory or verified runtime identity"),
                     *((_reason("unsupported-capability",
                                "pytest full permits cooperative collection-finish mutation and setup skips; inventory is not complete"),)
                       if request.mode is C.Mode.FULL and command.kind is C.RunnerKind.PYTEST else ()),
                     *tuple(limitations)),
        input_before=input_before, input_after=input_after,
    )


def _unknown_snapshot(problem: C.Problem | None = None) -> C.InputSnapshot:
    code = "unknown-input" if problem is None else problem.code
    return C.InputSnapshot(
        digest=None, compatibility=None, head=None, clean=False,
        limitations=(_reason(code, "source snapshot is unavailable"),),
    )


def _capture_source(domain: C.DomainPaths, config: C.Config,
                    request: C.RunRequest, *, ensure_key: bool) -> C.InputSnapshot:
    """Capture typed source evidence without making it an execution blocker."""
    def normalize(snapshot_item: C.InputSnapshot) -> C.InputSnapshot:
        if snapshot_item.limitations:
            return snapshot_item
        if snapshot_item.digest is None or snapshot_item.compatibility is None:
            return replace(
                snapshot_item,
                limitations=(_reason("unknown-input", "source identity is incomplete"),),
            )
        return snapshot_item

    snapshot_kwargs = {}
    if config.runner.kind is C.RunnerKind.PYTEST and request.mode is C.Mode.FULL:
        snapshot_kwargs["pytest_full_outputs"] = True

    if ensure_key:
        try:
            source.ensure_fingerprint_key(domain)
        except (C.Problem, OSError) as exc:
            problem = exc if isinstance(exc, C.Problem) else None
            try:
                return normalize(source.snapshot(domain, config, None, request.base,
                                                 **snapshot_kwargs))
            except (C.Problem, OSError):
                return _unknown_snapshot(problem)
    try:
        return normalize(source.snapshot(domain, config, None, request.base,
                                         **snapshot_kwargs))
    except (C.Problem, OSError):
        return _unknown_snapshot()


def _source_limitations(*snapshots: C.InputSnapshot | None) -> tuple[C.Reason, ...]:
    return _unique_reasons(tuple(limitation for item in snapshots if item is not None
                                 for limitation in item.limitations))


def _unique_reasons(reasons: tuple[C.Reason, ...]) -> tuple[C.Reason, ...]:
    seen = set()
    result = []
    for reason in reasons:
        marker = (reason.code, reason.message, reason.paths)
        if marker not in seen:
            seen.add(marker)
            result.append(reason)
    return tuple(result)


def _changed_path_classes(before: C.InputSnapshot, after: C.InputSnapshot) -> tuple[str, ...]:
    """Summarize recorded changes only; never rescan or disclose their paths."""
    old = {item.path: item for item in before.files}
    new = {item.path: item for item in after.files}
    changed = {path for path in old.keys() | new.keys() if old.get(path) != new.get(path)}
    classified, classes = set(), set()
    for snapshot_item in (before, after):
        for change in snapshot_item.changes:
            paths = {change.old, change.new} & changed
            if not paths:
                continue
            if change.kind in {"untracked", "ignored"}:
                classes.add(change.kind)
            elif change.kind in {"added", "modified", "deleted", "renamed", "mode", "raw"}:
                classes.add("tracked")
            else:
                classes.add("unclassified")
            classified.update(paths)
    # Declared ignored inputs and non-file influences need not have Change
    # records. Missing class evidence is uncertainty, never a guessed class.
    if changed - classified or not classes:
        classes.add("unclassified")
    return tuple(sorted(classes))


def _source_invalidation(before: C.InputSnapshot,
                         after: C.InputSnapshot) -> C.Reason | None:
    if before.digest is None:
        return None
    if after.digest is None or (before.compatibility is not None and after.compatibility is None):
        return _reason("unknown-input",
                       "final source identity is unavailable; verify the final input state")
    if (before.digest != after.digest or
            (before.compatibility is not None and before.compatibility != after.compatibility)):
        classes = ", ".join(_changed_path_classes(before, after))
        return _reason("changed-during-run",
                       "relevant source inputs changed during execution; "
                       f"path classes: {classes}; verify the final input state")
    return None


def _source_valid(before: C.InputSnapshot | None,
                  after: C.InputSnapshot | None) -> bool:
    if before is None or after is None:
        return False
    return bool(
        before.digest is not None and after.digest is not None
        and before.compatibility is not None and after.compatibility is not None
        and before.compatibility == after.compatibility
        and before.digest == after.digest
        and not before.limitations and not after.limitations
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
                 launched: C.ProcessIdentity | None,
                 expected_report_name: str | None = None,
                 expect_setup: bool = False):
        self.peer = peer
        self.grant = grant
        self.launched = launched
        self.expected_report_name = expected_report_name
        self.expect_setup = expect_setup
        self.pending = bytearray()
        self.eof = False
        self.invalid: C.Problem | None = None
        self.registered = False
        self.ready: dict | None = None
        self.decision_sent = False
        self.decision_reason: C.Reason | None = None
        self.setup_phase = False
        self.setup_facts: dict | None = None
        self.phase = False
        self.facts: dict | None = None
        self.draining = False
        self.cancel_sent = False
        self.setup_started_at: float | None = None
        self.setup_elapsed_s: float | None = None

    def _fail(self, message: str) -> None:
        if self.invalid is None:
            self.invalid = _problem("protocol-mismatch", message, phase="guard")

    def _accept(self, frame: C.ControlFrame) -> None:
        if frame.run_id != self.grant.run_id or frame.nonce != self.grant.nonce:
            self._fail("guard frame identity does not match the grant")
            return
        if frame.kind == "registered":
            if (self.registered or self.ready is not None or self.phase
                    or self.setup_phase or self.setup_facts is not None
                    or self.facts is not None or self.draining):
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
        if frame.kind == "attempt-ready":
            if (not self.registered or self.ready is not None or self.phase
                    or self.facts is not None or self.draining):
                self._fail("attempt-ready frame was duplicated or out of order")
                return
            if (self.setup_facts is not None and
                    (self.setup_facts["phase"] != "setup"
                     or self.setup_facts["attempt_id"] != "a001"
                     or self.setup_facts["raw_exit_code"] != 0
                     or self.setup_facts["problem"] is not None)):
                self._fail("execution attempt followed unsuccessful setup")
                return
            if self.setup_phase and self.setup_facts is None:
                self._fail("execution attempt preceded setup facts")
                return
            if self.expect_setup and (not self.setup_phase or self.setup_facts is None):
                self._fail("execution attempt preceded expected setup")
                return
            if (frame.payload["attempt_id"] != "a001"
                    or frame.payload["previous_attempt_id"] is not None
                    or frame.payload["generation"] != self.grant.generation
                    or frame.payload["deadline_monotonic"] <= time.monotonic()
                    or frame.payload["deadline_monotonic"] > (
                        time.monotonic()
                        + C.DEFAULT_ATTEMPT_DECISION_TIMEOUT_S + _FRAME_TIMEOUT_S)):
                self._fail("attempt-ready frame does not match the command attempt")
                return
            self.ready = dict(frame.payload)
            return
        if frame.kind == "phase":
            phase = frame.payload["phase"]
            if phase == "setup":
                if (not self.expect_setup or not self.registered or self.setup_phase
                        or self.setup_facts is not None or self.phase
                        or self.facts is not None or self.draining
                        or frame.payload["attempt_id"] != "a001"):
                    self._fail("setup phase frame was out of order")
                    return
                self.setup_phase = True
                self.setup_started_at = time.monotonic()
                return
            if (self.ready is None or not self.decision_sent or self.phase
                    or self.facts is not None or self.draining
                    or self.decision_reason is not None):
                self._fail("phase frame was out of order")
                return
            if (phase != "execution" or frame.payload["attempt_id"] != "a001"):
                self._fail("phase frame does not match the command attempt")
                return
            self.phase = True
            return
        if frame.kind == "runner-facts":
            phase = frame.payload["phase"]
            if phase == "setup":
                if (not self.setup_phase or self.setup_facts is not None
                        or self.phase or self.facts is not None or self.draining
                        or frame.payload["attempt_id"] != "a001"
                        or frame.payload["report_name"] is not None):
                    self._fail("setup runner facts were out of order")
                    return
                self.setup_facts = dict(frame.payload)
                if self.setup_started_at is not None:
                    self.setup_elapsed_s = max(
                        0.0, time.monotonic() - self.setup_started_at)
                return
            if not self.phase or self.facts is not None or self.draining:
                self._fail("runner facts were duplicated or out of order")
                return
            if (frame.payload["attempt_id"] != "a001"
                    or phase != "execution"
                    or frame.payload["report_name"] != self.expected_report_name):
                self._fail("runner facts do not match the command attempt")
                return
            self.facts = dict(frame.payload)
            return
        if frame.kind == "draining":
            handoff_complete = (
                self.facts is not None
                or (self.setup_facts is not None and not self.phase)
                or (self.cancel_sent and self.ready is not None
                    and not self.phase and self.facts is None)
                or (self.ready is not None and self.decision_sent
                    and self.decision_reason is not None and not self.phase)
            )
            if (not handoff_complete or self.draining or
                    frame.payload["provisional_artifact_id"] is not None):
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
        frame = C.ControlFrame(protocol=C.GUARD_PROTOCOL_VERSION,
                               run_id=grant.run_id,
                               nonce=grant.nonce, kind="cancel",
                               payload={"signal": signum})
        peer.sendall(C.encode_control_frame(frame))
        return True
    except (OSError, C.Problem):
        return False


def _send_attempt_decision(peer: socket.socket, grant: C.Grant,
                           frames: _Frames,
                           reason: C.Reason | None) -> bool:
    if frames.ready is None or frames.decision_sent:
        return False
    action = "continue" if reason is None else "stop"
    payload = {
        "attempt_id": frames.ready["attempt_id"],
        "generation": frames.ready["generation"],
        "gate_token": frames.ready["gate_token"],
        "action": action,
        "reason": None if reason is None else reason.code,
    }
    try:
        frame = C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION,
            run_id=grant.run_id,
            nonce=grant.nonce,
            kind="attempt-decision",
            payload=payload,
        )
        peer.sendall(C.encode_control_frame(frame))
    except (OSError, C.Problem, TypeError, ValueError):
        frames._fail("attempt decision could not be delivered")
        return False
    frames.decision_sent = True
    frames.decision_reason = reason
    return True


def _launch_guard(domain: C.DomainPaths, grant: C.Grant,
                  prepared: C.PreparedRun,
                  setup: C.PreparedRun | None = None) -> tuple[subprocess.Popen, socket.socket,
                                                               _Frames]:
    manifest = C.LaunchManifest(
        protocol=C.GUARD_PROTOCOL_VERSION,
        domain=domain, grant=grant, setup=setup,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=C.DEFAULT_SETUP_TIMEOUT_S,
        attempt_timeout_s=None,
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
            env={name: value for name, value in os.environ.items()
                 if not name.startswith("PTEST_")},
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
        return process, controller, _Frames(
            controller, grant, launched_identity,
            None if prepared.report_path is None else prepared.report_path.name,
            expect_setup=setup is not None,
        )
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
               prepared: C.PreparedRun, signals: _Signals,
               decide: Callable[[], C.Reason | None],
               setup: C.PreparedRun | None = None) -> tuple[int, _Frames,
                                                              float]:
    process, control, frames = _launch_guard(domain, grant, prepared, setup)
    started = time.monotonic()
    cancel_sent = False
    try:
        while process.poll() is None:
            if signals.number is not None and not cancel_sent:
                cancel_sent = _send_cancel(control, grant, signals.number)
                frames.cancel_sent = cancel_sent
            wait = _POLL_S
            ready, _, _ = select.select([control], [], [], wait)
            if ready:
                frames.drain()
            if (frames.ready is not None and not frames.decision_sent
                    and frames.invalid is None and signals.number is None):
                try:
                    reason = decide()
                except BaseException:
                    reason = _reason(
                        "state-unavailable",
                        "source identity could not be revalidated at launch")
                _send_attempt_decision(control, grant, frames, reason)
            if frames.invalid is not None and not cancel_sent:
                cancel_sent = _send_cancel(control, grant, signal.SIGTERM)
        process.wait()
        # The guard closes its end in finally.  Drain already queued frames and
        # require EOF/truncation evidence before finalization.
        deadline = time.monotonic() + _FRAME_TIMEOUT_S
        while not frames.eof and frames.invalid is None and time.monotonic() < deadline:
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


def _outcome(raw: int | None, cancellation: int | None,
             guard_problem: C.Problem | None, incomplete: bool
             ) -> tuple[C.Status, int, str, int | None]:
    """One precedence rule for all post-launch results, including partial ones."""
    if raw is not None and raw > 0:
        return (C.Status.INCOMPLETE if incomplete else C.Status.FAILED,
                raw, "runner", None)
    if raw is not None and raw < 0 and guard_problem is None:
        return (C.Status.INCOMPLETE if incomplete else C.Status.FAILED,
                128 - raw, "signal", -raw)
    if guard_problem is not None and guard_problem.code == "missing-executable" and raw is None:
        return (C.Status.INCOMPLETE if incomplete else C.Status.FAILED,
                127, "runner", None)
    if cancellation is not None:
        return (C.Status.INCOMPLETE if incomplete else C.Status.CANCELLED,
                128 + cancellation, "signal", cancellation)
    if incomplete or raw is None:
        return C.Status.INCOMPLETE, 70, "ptest", None
    return C.Status.PASSED, 0, "runner", None


def _incomplete(result: C.RunResult, reason: C.Reason) -> C.RunResult:
    # The result already contains the centralized native/signal precedence.
    # A later publication/release failure may only replace a zero exit.
    code = result.exit_code or 70
    return replace(
        result, status=C.Status.INCOMPLETE, phase="finalization",
        exit_code=code, exit_origin=result.exit_origin if result.exit_code else "ptest",
        reasons=result.reasons + (reason,),
        attempts=tuple(
            attempt if (attempt.status is C.Status.NOT_RUN
                        or attempt.phase == "setup")
            else replace(
                attempt, status=C.Status.INCOMPLETE, final_exit_code=code)
            for attempt in result.attempts),
    )


def _export(domain: C.DomainPaths, checkout: C.CheckoutIdentity,
            request: C.RunRequest, result: C.RunResult,
            finalize: Callable[[C.RunResult], None] | None = None) -> C.RunResult:
    finalizing = False

    def finish() -> None:
        nonlocal finalizing
        finalizing = True
        finalize(result)

    try:
        if request.result_path is not None:
            files.create_exclusive(
                checkout.root, request.result_path,
                render.render_json(C.PublicDocument(
                    kind="run", ptest_version=C.PTEST_VERSION,
                    domain=None if domain.domain_id is None else {
                        "id": domain.domain_id, "fixture": domain.fixture},
                    data=C.serialize_run_result(result), error=None,
                )), private=False, after_write=finish if finalize is not None else None,
            )
        elif finalize is not None:
            finish()
    except (C.Problem, OSError):
        if finalizing:
            result = _incomplete(result, _reason("ownership-uncertain", "lease finalization could not be confirmed"))
            # The exclusive writer rolled back only its own export. Publish
            # the incomplete facts without retrying finish; a raced target
            # remains protected by exclusive creation on this second attempt.
            return _export(domain, checkout, request, result)
        result = _incomplete(result, _reason("state-unavailable", "requested result export failed"))
        if finalize is not None:
            try:
                finalize(result)
            except (C.Problem, OSError):
                result = _incomplete(result, _reason("ownership-uncertain", "lease finalization could not be confirmed"))
    return result


def execute(domain: C.DomainPaths, config: C.Config,
            request: C.RunRequest) -> C.RunResult:
    """Run one admitted command and return its typed outcome."""
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.Config):
        raise TypeError("execute requires DomainPaths and Config")
    if not isinstance(request, C.RunRequest):
        raise TypeError("execute requires RunRequest")
    native_pytest = config.runner.kind is C.RunnerKind.PYTEST
    if native_pytest:
        if request.mode not in {C.Mode.SCOPED, C.Mode.FULL}:
            raise _problem("unsupported-capability", "pytest execution requires explicit scope or --full")
        if request.mode is C.Mode.FULL and request.base is not None:
            raise _problem("invalid-config", "--base is unavailable with explicit pytest full execution")
        if request.mode is C.Mode.FULL and "." in config.runner.test_roots:
            raise _problem("unsupported-capability", "pytest full execution does not support a dot test root")
        if request.shadow or request.probe is not None:
            raise _problem("unsupported-capability", "pytest shadow and probe are unavailable")
    elif config.runner.kind is not C.RunnerKind.COMMAND:
        raise _problem("unsupported-capability", "native profile execution is deferred")
    if not native_pytest and (config.setup is not None or request.shadow or request.probe is not None):
        raise _problem("unsupported-capability", "setup, shadow and probe execution are deferred")
    if request.mode is not C.Mode.SCOPED and request.argv:
        raise _problem("invalid-config", "literal command arguments require scoped mode")
    adapter = adapter_for(config.runner.kind)
    if not native_pytest and not adapter.requires_exclusive(config):
        raise _problem("unsupported-capability", "command execution requires exclusive admission")

    checkout = _checkout(config)
    run_id = secrets.token_hex(16)
    plan = _plan(request, config.runner.kind)
    # The command summary is redacted and never includes token values.
    requested_slots = 1 if native_pytest else min(
        config.runner.workers,
        config.runner.workers if request.workers is None else request.workers,
    )
    command = _summary(config, plan, request, requested_slots)
    started = _iso_now()
    signals = _Signals()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, signals.handler)
    enqueued_at = time.monotonic()
    queue_s = None
    ticket = None
    grant = None
    report_binding = None
    try:
        owner = platform.process_identity(os.getpid())
        if owner is None:
            raise _problem("ownership-uncertain", "caller identity cannot be verified")
        memory = config.resources.memory_mb_per_worker or None
        admission = C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner,
            slots=requested_slots, exclusive=not native_pytest,
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
                    return _export(domain, checkout, request, _cancel_result(
                        run_id, checkout, request, plan, command,
                        signals.number, time.monotonic() - enqueued_at))
                raise _problem("ownership-uncertain", "pending cancellation could not be confirmed")
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
        queue_s = time.monotonic() - enqueued_at
        # A signal delivered in the narrow GRANTED window must cancel before
        # any guard is spawned. A failed CAS cannot authorize another launch.
        if signals.number is not None:
            if scheduler.cancel_pending(domain, ticket, owner):
                return _export(domain, checkout, request, _cancel_result(
                    run_id, checkout, request, plan, command,
                    signals.number, time.monotonic() - enqueued_at))
            raise _problem("ownership-uncertain", "pending cancellation could not be confirmed")
        if native_pytest:
            # The queue can outlive edits to the command, resource locks, or
            # project identity. Never launch the previously resolved config
            # under a grant that was obtained for different inputs.
            try:
                resolved = config_api.resolve_config(checkout.root)
                if (resolved.problem is not None
                        or resolved.config != replace(config, checkout=None)):
                    raise _problem("changed-during-run",
                                   "pytest configuration changed while awaiting admission")
            except BaseException:
                scheduler.cancel_pending(domain, ticket, owner)
                raise
        attempt = _attempt(grant, checkout)
        effective = _effective_config(config, request, plan, grant)
        try:
            prepared = adapter.prepare(effective, plan, grant, attempt)
        except BaseException:
            # No guard exists yet, so an adapter rejection must not leave a
            # never-registered GRANTED lease charging the checkout.
            scheduler.cancel_pending(domain, ticket, owner)
            raise
        prepared = replace(prepared, env_updates=prepared.env_updates + (
            ("PTEST_PROJECT_ID", checkout.project_id),
            ("PTEST_CHECKOUT_ID", checkout.checkout_id),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_ATTEMPT_ID", attempt.attempt_id),
            ("PTEST_WORKER_ID", "w000"),
            ("PTEST_RESOURCE_PREFIX", attempt.resource_prefix),
        ))
        try:
            setup_prepared = _setup_prepared(
                effective, checkout, request, prepared, domain, grant, attempt)
            setup_fingerprint_before = (
                None if setup_prepared is None
                else _setup_fingerprint(effective, checkout))
        except BaseException:
            # No guard exists yet, so a no-setup refusal or setup declaration
            # rejection must release the granted lease without a child.
            scheduler.cancel_pending(domain, ticket, owner)
            raise
        if native_pytest:
            try:
                report_binding = reports.allocate_report(
                    domain, checkout,
                    run_id=grant.run_id,
                    nonce=grant.nonce,
                    attempt_id=attempt.attempt_id,
                    runner=C.RunnerKind.PYTEST,
                    execution_mode=plan.execution,
                    effective_profile=C.ExecutionTier.BASIC_SERIAL.value,
                )
            except BaseException:
                scheduler.cancel_pending(domain, ticket, owner)
                raise
            prepared = replace(
                prepared,
                report_path=report_binding.path,
                env_updates=prepared.env_updates + (
                    ("PTEST_GRANT_NONCE", grant.nonce),
                    ("PTEST_PYTEST_ATTEMPT", attempt.attempt_id),
                    ("PTEST_PYTEST_EXECUTION", plan.execution),
                    ("PTEST_PYTEST_REPORT_PATH", str(report_binding.path)),
                ),
            )
        if signals.number is not None:
            if scheduler.cancel_pending(domain, ticket, owner):
                return _export(domain, checkout, request, _cancel_result(
                    run_id, checkout, request, plan, command, signals.number, queue_s))
            raise _problem("ownership-uncertain", "pending cancellation could not be confirmed")
        # Capture the initial identity after exclusive admission and queue wait,
        # immediately before launching the admitted command.
        try:
            input_before = _capture_source(domain, effective, request, ensure_key=True)
        except BaseException:
            # Capture has the same pre-launch ownership obligation as prepare.
            scheduler.cancel_pending(domain, ticket, owner)
            raise
        if signals.number is not None:
            if scheduler.cancel_pending(domain, ticket, owner):
                return _export(domain, checkout, request, _cancel_result(
                    run_id, checkout, request, plan, command, signals.number, queue_s))
            raise _problem("ownership-uncertain", "pending cancellation could not be confirmed")
        gate_snapshot: C.InputSnapshot | None = None

        def decide_attempt() -> C.Reason | None:
            nonlocal gate_snapshot
            if setup_prepared is not None:
                setup_issue = _required_paths_issue(effective, checkout)
                if setup_issue is not None:
                    return _reason(
                        "state-unavailable",
                        f"declared setup completed but {setup_issue}",
                    )
                try:
                    setup_fingerprint_after = _setup_fingerprint(effective, checkout)
                except (C.Problem, OSError):
                    return _reason(
                        "state-unavailable",
                        "setup tool/lock fingerprint could not be revalidated",
                    )
                if setup_fingerprint_after != setup_fingerprint_before:
                    return _reason(
                        "changed-during-run",
                        "setup tool/lock inputs changed during setup",
                    )
                try:
                    _record_setup_fingerprint(
                        domain, checkout, setup_fingerprint_after)
                except (C.Problem, OSError):
                    return _reason(
                        "state-unavailable",
                        "setup fingerprint state could not be recorded",
                    )
            gate_snapshot = _capture_source(
                domain, effective, request, ensure_key=False)
            if (native_pytest and plan.execution == "full"
                    and (input_before.digest is None
                         or gate_snapshot.digest is None)):
                return _reason(
                    "unknown-input",
                    "required pytest content identity is unavailable")
            return _source_invalidation(input_before, gate_snapshot)

        try:
            raw_guard, frames, execution_s = _run_guard(
                domain, grant, prepared, signals, decide_attempt,
                setup_prepared)
        except (C.Problem, OSError):
            # A launch failure before registration is still cancellable.  Once
            # registration wins the CAS, cancellation deliberately retains the
            # live lease for scheduler recovery instead of guessing release.
            reasons = (_reason("state-unavailable", "guard execution could not be completed"),)
            try:
                scheduler.cancel_pending(domain, ticket, owner)
            except C.Problem:
                reasons += (_reason("ownership-uncertain", "pending grant remains unconfirmed"),)
            status, code, origin, number = _outcome(None, signals.number, None, True)
            return _export(domain, checkout, request, _result(
                run_id=run_id, checkout=checkout, request=request, plan=plan,
                command=command, status=status, phase="execution", started=started,
                runner_code=None, exit_code=code, origin=origin, signal_number=number,
                granted=grant, reasons=reasons,
                limitations=_source_limitations(input_before),
                input_before=input_before, queue_s=queue_s))
        setup_raw = (None if frames.setup_facts is None
                     else frames.setup_facts["raw_exit_code"])
        setup_problem = (
            None if frames.setup_facts is None
            or frames.setup_facts["problem"] is None
            else C.Problem(**frames.setup_facts["problem"])
        )
        setup_failed = (
            frames.setup_facts is not None
            and (setup_raw != 0 or setup_problem is not None)
        )
        raw = None if frames.facts is None else frames.facts["raw_exit_code"]
        guard_problem = (None if frames.facts is None or frames.facts["problem"] is None
                         else C.Problem(**frames.facts["problem"]))
        stopped_at_gate = frames.decision_reason is not None
        continued_handoff = (
            frames.ready is not None and frames.decision_sent
            and frames.phase and frames.facts is not None)
        stopped_handoff = (
            stopped_at_gate and frames.ready is not None
            and frames.decision_sent and not frames.phase
            and frames.facts is None)
        cancelled_handoff = (
            frames.cancel_sent and frames.ready is not None
            and not frames.phase and frames.facts is None)
        setup_handoff = (
            setup_failed and frames.setup_phase and frames.setup_facts is not None
            and not frames.phase and frames.draining and frames.eof)
        protocol_valid = (
            frames.invalid is None and frames.registered
            and (continued_handoff or stopped_handoff or cancelled_handoff
                 or setup_handoff)
            and frames.draining and frames.eof)
        reasons = ()
        incomplete = not protocol_valid or stopped_at_gate or cancelled_handoff
        if incomplete:
            if not protocol_valid:
                reasons += (_reason(
                    "protocol-mismatch", "guard handoff was incomplete"),)
        if frames.decision_reason is not None:
            reasons += (frames.decision_reason,)
        if setup_failed:
            if setup_problem is not None:
                if setup_problem.code == "missing-executable":
                    reasons += (_reason("missing-executable", "setup could not be launched"),)
                else:
                    incomplete = True
                    reasons += (_reason("state-unavailable", "declared setup failed in the guard"),)
            if not setup_handoff:
                incomplete = True
            raw = setup_raw
            guard_problem = setup_problem
        elif guard_problem is not None:
            if guard_problem.code == "missing-executable":
                incomplete = incomplete or raw is not None
                reasons += (_reason("missing-executable", "runner could not be launched"),)
            else:
                incomplete = True
                reasons += (_reason("state-unavailable", "guard execution failed or exceeded its deadline"),)
        elif ((raw is None and not stopped_at_gate)
              or (raw_guard != 0 and signals.number is None)):
            incomplete = True
            reasons += (_reason("state-unavailable", "guard execution did not complete normally"),)
        status, final_code, origin, signal_number = _outcome(
            raw, signals.number, guard_problem, incomplete)
        if setup_failed:
            origin = "setup"
        setup_s = frames.setup_elapsed_s
        execution_elapsed = (
            None if setup_failed else max(
                0.0, execution_s - (setup_s or 0.0)))
        setup_attempts = ()
        if frames.setup_facts is not None and not setup_failed:
            setup_attempts = (C.AttemptResult(
                attempt_id="a001", phase="setup", status=C.Status.PASSED,
                raw_exit_code=0, final_exit_code=0, source_valid=False,
                inventory_complete=False,
                timings=C.Timings(setup_s=setup_s),
            ),)
        execution_not_run = stopped_handoff or cancelled_handoff
        attempt_result = C.AttemptResult(
            attempt_id="a001", phase="setup" if setup_failed else "execution",
            status=C.Status.NOT_RUN if execution_not_run else status,
            raw_exit_code=None if execution_not_run else raw,
            final_exit_code=None if execution_not_run else final_code,
            source_valid=False, inventory_complete=False,
            timings=(C.Timings(setup_s=setup_s) if setup_failed else
                     (None if execution_not_run else C.Timings(
                         execution_s=execution_elapsed))),
        )
        result = _result(run_id=run_id, checkout=checkout, request=request,
                         plan=plan, command=command, status=status,
                         phase="complete", started=started,
                         runner_code=None if setup_failed else raw,
                         exit_code=final_code if final_code is not None else 70,
                         origin=origin, signal_number=signal_number, granted=grant,
                         reasons=reasons, limitations=_source_limitations(input_before),
                         input_before=input_before, attempt=attempt_result,
                         attempts_extra=setup_attempts,
                         queue_s=queue_s, setup_s=setup_s,
                         execution_s=execution_elapsed)
        if not protocol_valid:
            if not frames.registered:
                # A missing notification is not proof of no registration. The
                # scheduler CAS alone decides whether this grant can be revoked.
                try:
                    scheduler.cancel_pending(domain, ticket, owner)
                except C.Problem:
                    result = _incomplete(result, _reason("ownership-uncertain", "pending grant remains unconfirmed"))
            return _export(domain, checkout, request, result)
        # Only authenticated DRAINING plus guard reap permits this proof. Take
        # the post-run snapshot while the lease is held, before finalization.
        input_after = _capture_source(domain, effective, request, ensure_key=False)
        source_valid = _source_valid(input_before, input_after)
        if stopped_at_gate or native_pytest:
            source_valid = False
        result = replace(
            result,
            source_valid=source_valid,
            input_after=input_after,
            limitations=_unique_reasons(
                result.limitations + _source_limitations(input_after)),
            attempts=tuple(replace(item, source_valid=source_valid)
                           for item in result.attempts),
        )
        invalidation = _source_invalidation(input_before, input_after)
        if (native_pytest and plan.execution == "full"
                and (input_before.digest is None or input_after.digest is None)):
            result = _incomplete(result, _reason(
                "unknown-input", "pytest full content evidence is unavailable"))
        elif invalidation is not None:
            if plan.execution == "full":
                result = _incomplete(result, invalidation)
            else:
                result = replace(result, reasons=result.reasons + (invalidation,))
        finalization_started = time.monotonic()
        try:
            proof = scheduler.begin_finalization(domain, grant)
        except (C.Problem, OSError):
            return _export(domain, checkout, request, _incomplete(
                result, _reason("ownership-uncertain", "guard quiescence could not be confirmed")))
        result = replace(result, timings=replace(result.timings, finalization_s=(
            time.monotonic() - finalization_started)))
        consumed_report = False
        if native_pytest and not setup_failed:
            report_reason = None
            try:
                native_report = reports.consume_report(report_binding)
                consumed_report = True
                if (native_report.bridge_exit_code != raw
                        or (native_report.terminal_complete and native_report.native_exit_code != raw)):
                    report_reason = _reason(
                        "report-invalid",
                        "native terminal report did not authenticate the native exit",
                    )
                elif not native_report.terminal_complete:
                    report_reason = _reason("unsupported-capability",
                                            "pytest bridge refused test execution")
                    # An authenticated bridge refusal is not a native test
                    # failure. Retain the observed child code for diagnosis.
                    result = replace(result, exit_origin="ptest")
            except C.Problem as problem:
                code = (problem.code if problem.code in {
                    "capacity-exceeded", "report-invalid", "unsafe-path",
                } else "state-unavailable")
                report_reason = _reason(code, "native terminal report was unavailable")
            if report_reason is not None:
                # Without matching terminal evidence the child code is still
                # preserved, but cannot certify a completed native test run.
                result = _incomplete(result, report_reason)

        def finalize(exported: C.RunResult) -> None:
            scheduler.finish(domain, grant, proof, C.Finalization(
                outcome_id=None, status=exported.status, exit_code=exported.exit_code,
                source_valid=exported.source_valid, committed=True,
            ))
            if consumed_report:
                reports.cleanup_report(report_binding)

        result = _export(domain, checkout, request, result, finalize=finalize)
        return replace(
            result,
            timings=replace(result.timings, finalization_s=(
                time.monotonic() - finalization_started)),
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


__all__ = ["execute"]
