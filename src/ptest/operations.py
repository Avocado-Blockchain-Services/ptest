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
from . import (config as config_api, executability, files, history, platform, render, reports,
               scheduler, selection, source)
from .runners import adapter_for


_PHASE = "execution"
_GUARD_SCRIPT = (
    "from ptest.guard import run_guard; "
    "raise SystemExit(run_guard(int(__import__('sys').argv[1]), "
    "int(__import__('sys').argv[2])))"
)
_FRAME_TIMEOUT_S = 2.0
_POLL_S = 0.05
# The parent-to-guard boundary strips inherited PTEST_* control variables so
# orchestrator state can never leak into the guard. The single exception is
# the opt-in doctor smoke manifest path: the smoke test runs as a runner
# grandchild of the invoking ptest process and reads only this variable.
_GUARD_ENV_ALLOWLIST = frozenset({"PTEST_DOCTOR_SMOKE_MANIFEST"})
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
    elif plan.execution == "selected":
        args += tuple(plan.files)
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


def _policy_digest(config: C.Config) -> str:
    return hashlib.sha256(repr(config.selection).encode()).hexdigest()


def _advanced_plan(config: C.Config, request: C.RunRequest,
                   snapshot: C.InputSnapshot,
                   history_view: C.HistoryView,
                   support: C.CompoundSupport) -> C.Plan:
    """Plan ordinary advanced execution from authenticated static history only."""
    if request.mode is C.Mode.FULL:
        return C.Plan(mode=request.mode, execution="full",
                      input_digest=snapshot.digest,
                      compatibility=snapshot.compatibility,
                      static_preview=False)
    if request.mode is C.Mode.SCOPED:
        return C.Plan(mode=request.mode, execution="scoped",
                      input_digest=snapshot.digest,
                      compatibility=snapshot.compatibility,
                      static_preview=False)
    if request.mode is not C.Mode.AUTOMATIC:
        raise _problem("unsupported-capability", "advanced compound mode is deferred")
    if not support.selection or support.profile is None or support.limitations:
        return _plan(request, config.runner.kind)
    return selection.choose_plan(config, snapshot, history_view, request)


def _effective_config(config: C.Config, request: C.RunRequest,
                      plan: C.Plan, grant: C.Grant) -> C.Config:
    runner = replace(config.runner, workers=grant.slots)
    if plan.execution == "scoped":
        runner = replace(runner, args=runner.args + tuple(request.argv))
    # The caller's Config remains the source/policy authority.  This private
    # copy is only for binding the one admitted command argv.
    return replace(config, runner=runner)


def _configured_pytest_command_variants(config: C.Config) -> str:
    """Encode only the configured command variants for native identity.

    Scoped caller arguments are appended to the private effective config for
    execution, but they are a declared per-attempt scope difference rather
    than a change to the configured full-gate identity.  Keep the bridge's
    digest anchored to the committed configuration so a full baseline can
    authorize a later scoped attempt.
    """
    return json.dumps([
        list(config.runner.launcher) + list(config.runner.args),
        list(config.runner.launcher) + list(config.runner.full_args)
        + list(config.runner.test_roots),
    ], separators=(",", ":"))


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


def _finish_setup(domain: C.DomainPaths, config: C.Config,
                  checkout: C.CheckoutIdentity, before: str) -> C.Reason | None:
    """Validate a completed setup: required paths, fingerprint, record.

    Shared by the execution gate and :func:`run_setup_only` so both map
    fingerprint and record failures to ``state-unavailable`` (or
    ``changed-during-run``) instead of propagating them.
    """
    issue = _required_paths_issue(config, checkout)
    if issue is not None:
        return _reason(
            "state-unavailable",
            f"declared setup completed but {issue}",
        )
    try:
        after = _setup_fingerprint(config, checkout)
    except (C.Problem, OSError):
        return _reason(
            "state-unavailable",
            "setup tool/lock fingerprint could not be revalidated",
        )
    if after != before:
        return _reason(
            "changed-during-run",
            "setup tool/lock inputs changed during setup",
        )
    try:
        _record_setup_fingerprint(domain, checkout, after)
    except (C.Problem, OSError):
        return _reason(
            "state-unavailable",
            "setup fingerprint state could not be recorded",
        )
    return None


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


def setup_blocker(domain: C.DomainPaths, config: C.Config) -> str | None:
    """Bounded missing/stale setup reason, or None when no setup run is due.

    Read-only: never executes setup. Init smoke uses it to skip with the
    exact setup command instead of installing silently.
    """
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.Config):
        raise TypeError("setup_blocker requires DomainPaths and Config")
    if config.setup is None:
        return None
    try:
        checkout = _checkout(config)
    except C.Problem:
        return "setup state cannot be determined"
    return _required_setup_state(config, checkout, domain)


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
            config.runner.kind, C.Mode.SCOPED, setup.argv,
            workers=grant.slots, provenance=("declared-setup",)),
    )


def _attempt(grant: C.Grant, checkout: C.CheckoutIdentity) -> C.AttemptIdentity:
    return _attempt_identity(grant, checkout, "a001")


def _attempt_identity(grant: C.Grant, checkout: C.CheckoutIdentity,
                      attempt_id: str, *, workers: int | None = None) -> C.AttemptIdentity:
    worker_count = grant.slots if workers is None else workers
    prefix = f"pt_{checkout.checkout_id[:8]}_{grant.run_id}_{attempt_id}_w000"
    return C.AttemptIdentity(run_id=grant.run_id, attempt_id=attempt_id,
                             resource_prefix=prefix, worker_count=worker_count)


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
                                "pytest full refuses collected-item drops after the final inventory and permits cooperative setup skips; inventory is not complete"),)
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
                    request: C.RunRequest, *, ensure_key: bool,
                    execution_tier: C.ExecutionTier = C.ExecutionTier.BASIC_SERIAL,
                    runtime_identity: str | None = None,
                    baseline: C.Baseline | None = None) -> C.InputSnapshot:
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
    if not isinstance(execution_tier, C.ExecutionTier):
        raise TypeError("execution_tier must be an ExecutionTier")
    # The execution-only basic_serial tier intentionally uses the historical
    # full-content digest. Selection policy is not an execution-tier signal.
    if (config.runner.kind is C.RunnerKind.PYTEST and request.mode is C.Mode.FULL
            and execution_tier is C.ExecutionTier.BASIC_SERIAL):
        snapshot_kwargs["pytest_full_outputs"] = True
    if runtime_identity is not None:
        snapshot_kwargs["runtime_identity"] = runtime_identity

    if ensure_key:
        try:
            source.ensure_fingerprint_key(domain)
        except (C.Problem, OSError) as exc:
            problem = exc if isinstance(exc, C.Problem) else None
            try:
                return normalize(source.snapshot(domain, config, baseline, request.base,
                                                 **snapshot_kwargs))
            except (C.Problem, OSError):
                return _unknown_snapshot(problem)
    try:
        return normalize(source.snapshot(domain, config, baseline, request.base,
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


def _source_complete(snapshot: C.InputSnapshot | None) -> bool:
    """Check one authenticated snapshot without comparing it to itself."""
    return bool(
        snapshot is not None and snapshot.digest is not None
        and snapshot.compatibility is not None and not snapshot.limitations
    )


def _probe_scope_path(config: C.Config, options: C.ProbeOptions) -> Path:
    """Resolve one declared local probe scope without following symlinks."""
    checkout = _checkout(config)
    relative = options.scope
    if (not relative or relative.startswith(("/", "\\"))
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise _problem("unsafe-path", "probe scope must be a project-relative path")
    if not any(
            relative == root or relative.startswith(root + "/")
            for root in config.runner.test_roots):
        raise _problem("unsafe-path", "probe scope is outside declared test roots")
    candidate = checkout.root / relative
    cursor = checkout.root
    for part in relative.split("/"):
        cursor = cursor / part
        try:
            stamp = cursor.lstat()
        except FileNotFoundError:
            raise _problem("unsafe-path", "probe scope does not exist") from None
        except OSError:
            raise _problem("state-unavailable", "probe scope cannot be verified") from None
        if stat.S_ISLNK(stamp.st_mode):
            raise _problem("unsafe-path", "probe scope crosses a symlink")
    if not candidate.is_file() and not candidate.is_dir():
        raise _problem("unsafe-path", "probe scope is not a file or directory")
    return candidate


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


class _CompoundFrames(_Frames):
    """Controller-side frame state for the ordered multi-attempt guard."""

    def __init__(self, peer: socket.socket, grant: C.Grant,
                 launched: C.ProcessIdentity | None,
                 expected_reports: dict[str, str],
                 expect_setup: bool = False):
        super().__init__(peer, grant, launched, None, expect_setup)
        self.expected_reports = dict(expected_reports)
        self.expected_attempts = tuple(sorted(expected_reports))
        self.ready_by_attempt: dict[str, dict] = {}
        self.facts_by_attempt: dict[str, dict] = {}
        self.phase_by_attempt: set[str] = set()
        self.decision_reasons: dict[str, C.Reason | None] = {}
        self.previous_attempt: str | None = None

    def _accept(self, frame: C.ControlFrame) -> None:
        if frame.run_id != self.grant.run_id or frame.nonce != self.grant.nonce:
            self._fail("guard frame identity does not match the grant")
            return
        if frame.kind == "registered":
            if self.registered or self.draining:
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
            attempt_id = frame.payload["attempt_id"]
            expected_index = len(self.ready_by_attempt)
            expected_id = self.expected_attempts[expected_index] if expected_index < len(self.expected_attempts) else None
            if (not self.registered or self.ready is not None and self.decision_sent
                    or self.phase or self.draining or attempt_id != expected_id
                    or frame.payload["previous_attempt_id"] != self.previous_attempt
                    or frame.payload["generation"] != self.grant.generation
                    or frame.payload["deadline_monotonic"] <= time.monotonic()
                    or frame.payload["deadline_monotonic"] > time.monotonic() + C.DEFAULT_ATTEMPT_DECISION_TIMEOUT_S + _FRAME_TIMEOUT_S):
                self._fail("attempt-ready frame does not match the compound attempt")
                return
            if self.setup_facts is not None and (
                    self.setup_facts["raw_exit_code"] != 0
                    or self.setup_facts["problem"] is not None):
                self._fail("execution attempt followed unsuccessful setup")
                return
            if self.expect_setup and (not self.setup_phase or self.setup_facts is None):
                self._fail("execution attempt preceded expected setup")
                return
            self.ready = dict(frame.payload)
            self.ready_by_attempt[attempt_id] = dict(frame.payload)
            self.decision_sent = False
            self.decision_reason = None
            self.phase = False
            self.facts = None
            return
        if frame.kind == "phase":
            phase = frame.payload["phase"]
            attempt_id = frame.payload["attempt_id"]
            if phase == "setup":
                if (not self.expect_setup or not self.registered or self.setup_phase
                        or self.setup_facts is not None or self.phase
                        or attempt_id != "a001"):
                    self._fail("setup phase frame was out of order")
                    return
                self.setup_phase = True
                self.setup_started_at = time.monotonic()
                return
            if (attempt_id not in self.ready_by_attempt or not self.decision_sent
                    or self.decision_reason is not None or self.phase
                    or self.facts is not None or phase != "execution"):
                self._fail("execution phase frame was out of order")
                return
            self.phase = True
            self.phase_by_attempt.add(attempt_id)
            return
        if frame.kind == "runner-facts":
            phase = frame.payload["phase"]
            attempt_id = frame.payload["attempt_id"]
            if phase == "setup":
                if (not self.setup_phase or self.setup_facts is not None
                        or self.phase or attempt_id != "a001"
                        or frame.payload["report_name"] is not None):
                    self._fail("setup runner facts were out of order")
                    return
                self.setup_facts = dict(frame.payload)
                self.setup_elapsed_s = max(0.0, time.monotonic() - self.setup_started_at) if self.setup_started_at is not None else None
                return
            if (attempt_id not in self.ready_by_attempt or attempt_id not in self.phase_by_attempt
                    or self.facts is not None or phase != "execution"
                    or frame.payload["report_name"] != self.expected_reports.get(attempt_id)):
                self._fail("runner facts do not match the compound attempt")
                return
            self.facts = dict(frame.payload)
            self.facts_by_attempt[attempt_id] = dict(frame.payload)
            self.phase = False
            self.previous_attempt = attempt_id
            self.decision_sent = False
            self.ready = None
            return
        if frame.kind == "draining":
            observed_all = len(self.facts_by_attempt) == len(self.expected_attempts)
            stopped = self.decision_reason is not None and self.ready is not None and not self.phase
            cancelled = self.cancel_sent and self.ready is not None and not self.phase
            setup_only = self.setup_facts is not None and not self.ready_by_attempt
            if (not (observed_all or stopped or cancelled or setup_only)
                    or self.draining or frame.payload["provisional_artifact_id"] is not None):
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
                        frame = C.decode_control_frame(raw, expected_nonce=self.grant.nonce)
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
    if isinstance(frames, _CompoundFrames):
        frames.decision_reasons[frames.ready["attempt_id"]] = reason
    return True


def _launch_guard(domain: C.DomainPaths, grant: C.Grant,
                  prepared: C.PreparedRun | tuple[C.PreparedRun, ...],
                  setup: C.PreparedRun | None = None) -> tuple[subprocess.Popen, socket.socket,
                                                               _Frames]:
    attempts = prepared if isinstance(prepared, tuple) else (prepared,)
    attempt_ids = tuple(f"a{index:03d}" for index in range(1, len(attempts) + 1))
    manifest = C.LaunchManifest(
        protocol=C.GUARD_PROTOCOL_VERSION,
        domain=domain, grant=grant, setup=setup,
        attempts=attempts, attempt_ids=attempt_ids,
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
                 if not name.startswith("PTEST_")
                 or name in _GUARD_ENV_ALLOWLIST},
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
        if len(attempts) > 1:
            expected_reports = {
                attempt_id: prepared_item.report_path.name
                for attempt_id, prepared_item in zip(attempt_ids, attempts, strict=True)
                if prepared_item.report_path is not None
            }
            return process, controller, _CompoundFrames(
                controller, grant, launched_identity, expected_reports,
                expect_setup=setup is not None,
            )
        return process, controller, _Frames(
            controller, grant, launched_identity,
            None if attempts[0].report_path is None else attempts[0].report_path.name,
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


def _shadow_outcome(
        *, raw_codes: tuple[int | None, ...], gate_count: int,
        decision_reason: C.Reason | None = None,
        report_reason: C.Reason | None = None,
        handoff_complete: bool = True,
        cancellation: int | None = None,
        guard_problem: C.Problem | None = None,
        setup_raw: int | None = None,
        setup_problem: C.Problem | None = None,
        ) -> tuple[C.Status, int, str, int | None, int | None]:
    """Apply the ordinary raw/cancel/guard precedence to a compound run.

    The returned final tuple is ``status, exit_code, origin, signal,
    runner_exit_code``.  Raw runner codes stay distinct from shell-compatible
    final exits; the earliest nonzero raw code wins, otherwise the last
    observed raw code is retained.
    """
    observed = tuple(code for code in raw_codes if code is not None)
    ordered_codes = ((setup_raw,) if setup_raw is not None else ()) + observed
    first_failure = next((code for code in ordered_codes if code != 0), None)
    last_observed = ordered_codes[-1] if ordered_codes else None
    runner_code = (first_failure if first_failure is not None else last_observed)
    if setup_raw is None and setup_problem is None:
        runner_code = first_failure if first_failure is not None else (
            observed[-1] if observed else None)
    pre_execution_invalidation = (
        gate_count == 1 and not observed and decision_reason is not None
        and decision_reason.code in {"changed-during-run", "unknown-input"})
    if pre_execution_invalidation:
        return C.Status.NOT_RUN, 2, "ptest", None, None
    incomplete = bool(
        decision_reason is not None or report_reason is not None
        or not handoff_complete or guard_problem is not None
        or setup_problem is not None
        or (not raw_codes and setup_raw is None and cancellation is None))
    raw = first_failure if first_failure is not None else last_observed
    status, exit_code, origin, signal_number = _outcome(
        raw, cancellation, setup_problem or guard_problem, incomplete)
    if ((setup_raw is not None and setup_raw != 0)
            or setup_problem is not None):
        origin = "setup" if signal_number is None else origin
    return status, exit_code, origin, signal_number, runner_code


def _shadow_report_matches_guard(evidence: C.AttemptEvidence,
                                 guard_raw: int | None) -> bool:
    """Require native terminal facts to agree with authenticated guard facts."""
    return (evidence.result.raw_exit_code == guard_raw
            and evidence.result.final_exit_code == guard_raw)


def _guard_fact_problem(facts: dict | None) -> C.Problem | None:
    payload = None if facts is None else facts.get("problem")
    if payload is None:
        return None
    try:
        return C.Problem(**payload)
    except (TypeError, ValueError):
        return _problem("state-unavailable", "guard reported invalid runner facts",
                        phase="guard")


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
                attempt,
                status=C.Status.INCOMPLETE,
                final_exit_code=(
                    128 - attempt.raw_exit_code
                    if (attempt.raw_exit_code is not None
                        and attempt.raw_exit_code < 0)
                    else code),
            )
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


def _execute_probe(domain: C.DomainPaths, config: C.Config,
                   request: C.RunRequest) -> C.RunResult:
    """Validate probe admission and fail closed until a native probe qualifies.

    Probe observations are only meaningful when the native adapter has proved
    distinct worker identities and namespaced resources.  T12b explicitly
    leaves both first-class probe profiles unsupported, so this path performs
    validation but never acquires a lease, starts setup, or fabricates a
    serial-only observation.
    """
    options = request.probe
    if options is None:
        raise _problem("invalid-config", "probe options are required")
    _probe_scope_path(config, options)
    if config.resources.probe_isolation != "run-worker-namespaced":
        raise _problem("probe-isolation-required",
                       "probe requires run-worker-namespaced isolation")
    if config.runner.kind not in {C.RunnerKind.PYTEST, C.RunnerKind.VITEST}:
        raise _problem("unsupported-capability",
                       "probe requires a qualified pytest or vitest profile")
    adapter = adapter_for(config.runner.kind)
    declared = adapter.qualified_profile(config)
    if declared is None:
        name = "Q-PY-PROBE" if config.runner.kind is C.RunnerKind.PYTEST else "Q-VT-PROBE"
        raise _problem(
            "unsupported-capability",
            f"{name} is unsupported-capability; no native worker identity qualification",
        )
    support = adapter.compound_support(config, qualified_profile=dict(declared))
    if not support.parallel_identity:
        name = "Q-PY-PROBE" if config.runner.kind is C.RunnerKind.PYTEST else "Q-VT-PROBE"
        raise _problem(
            "unsupported-capability",
            f"{name} is unsupported-capability; no native worker identity qualification",
        )
    limits = scheduler.effective_limits(domain)
    if limits.max_slots is not None and limits.max_slots < 2:
        raise _problem(
            "capacity-exceeded",
            "probe requires at least two effective worker slots")
    # A future qualified profile enters the compound executor below.  Keep
    # this explicit until its native worker/resource evidence is available;
    # no static catalog declaration is a probe observation.
    raise _problem("unsupported-capability", "native probe execution is unavailable")


def _execute_shadow(domain: C.DomainPaths, config: C.Config,
                    request: C.RunRequest) -> C.RunResult:
    """Run one authenticated selected/full comparison under one guard."""
    if request.mode is not C.Mode.AUTOMATIC:
        raise _problem("invalid-config", "shadow execution requires automatic mode")
    if config.runner.kind is not C.RunnerKind.PYTEST:
        raise _problem("unsupported-capability",
                       "shadow requires a qualified pytest selection profile")
    adapter = adapter_for(config.runner.kind)
    checkout = _checkout(config)
    history_view = history.read_history(domain, checkout)
    catalog = adapter.qualified_profile(config)
    stored = history.read_qualified_profile(domain, checkout, config.runner.kind)
    qualified = None
    if catalog is not None:
        qualified = (dict(catalog) if history_view.baseline is None
                     else (None if stored is None else {"source": "stored", **stored}))
    support = adapter.compound_support(config, qualified_profile=qualified)
    if not support.selection or support.profile is None or support.limitations:
        raise _problem("unsupported-capability",
                       "Q-PY-SELECT is unsupported-capability for this native tuple")
    planning_runtime = (history_view.baseline.runtime_identity
                        if history_view.baseline is not None else None)
    planning_snapshot = _capture_source(
        domain, config, request, ensure_key=False,
        execution_tier=C.ExecutionTier.ADVANCED,
        runtime_identity=planning_runtime,
        baseline=history_view.baseline,
    )
    plans = selection.choose_shadow_plans(
        config, planning_snapshot, history_view,
        request, support)
    if plans.selected.execution != "selected" or not plans.selected.files:
        raise _problem("unsupported-capability",
                       "shadow requires a genuine selected plan below full_ratio")

    run_id = secrets.token_hex(16)
    selected_plan = replace(plans.selected, mode=C.Mode.SHADOW)
    full_plan = replace(plans.full, mode=C.Mode.SHADOW)
    command = _summary(config, selected_plan, request, 1)
    checkout_owner = platform.process_identity(os.getpid())
    if checkout_owner is None:
        raise _problem("ownership-uncertain", "caller identity cannot be verified")
    memory = config.resources.memory_mb_per_worker or None
    owner = checkout_owner
    admission = C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, locks=config.resources.locks, memory_mb=memory,
        deadline=time.monotonic() + request.queue_timeout_s,
        fixture=domain.fixture,
    )
    ticket = scheduler.enqueue(domain, admission)
    queue_started = time.monotonic()
    signal_state = _Signals()
    previous = {signum: signal.signal(signum, signal_state.handler)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    grant = None
    bindings: list[reports.NativeReportBinding] = []
    frames: _CompoundFrames | None = None
    try:
        while True:
            state = scheduler.poll(domain, ticket)
            if signal_state.number is not None and state.state in {
                    C.LeaseState.QUEUED, C.LeaseState.GRANTED}:
                if scheduler.cancel_pending(domain, ticket, owner):
                    return _export(domain, checkout, request, _cancel_result(
                        run_id, checkout, request, selected_plan, command,
                        signal_state.number, time.monotonic() - queue_started))
                raise _problem("ownership-uncertain", "shadow cancellation could not be confirmed")
            if state.state is C.LeaseState.GRANTED:
                grant = state.grant
                break
            if state.state is C.LeaseState.CANCELLED:
                raise state.problem or _problem("queue-timeout", "shadow admission did not complete", retryable=True)
            if state.problem is not None and state.state is not C.LeaseState.QUEUED:
                raise state.problem
            if time.monotonic() >= admission.deadline:
                raise _problem("queue-timeout", "admission queue timeout", retryable=True)
            time.sleep(min(C.SCHEDULER_POLL_S,
                           max(0.0, admission.deadline - time.monotonic())))
        assert grant is not None
        if signal_state.number is not None:
            if scheduler.cancel_pending(domain, ticket, owner):
                return _export(domain, checkout, request, _cancel_result(
                    run_id, checkout, request, selected_plan, command,
                    signal_state.number, time.monotonic() - queue_started))
            raise _problem("ownership-uncertain",
                           "shadow cancellation could not be confirmed")
        try:
            resolved = config_api.resolve_config(checkout.root)
            if (resolved.problem is not None
                    or resolved.config != replace(config, checkout=None)):
                if scheduler.cancel_pending(domain, ticket, owner):
                    raise _problem(
                        "changed-during-run",
                        "pytest configuration changed while awaiting admission")
                raise _problem("ownership-uncertain",
                               "shadow configuration invalidation could not be released")
        except BaseException:
            if grant is not None:
                try:
                    scheduler.cancel_pending(domain, ticket, owner)
                except (C.Problem, OSError):
                    pass
            raise
        attempts = []
        prepared_runs = []
        for attempt_id, plan in (("a001", selected_plan), ("a002", full_plan)):
            identity = _attempt_identity(grant, checkout, attempt_id, workers=1)
            prepared = adapter.prepare_advanced(
                replace(config, runner=replace(config.runner, workers=1)),
                plan, grant, identity,
                expected_runtime_identity=(history_view.baseline.runtime_identity
                                            if history_view.baseline is not None else None),
            )
            prepared = replace(prepared, env_updates=prepared.env_updates + (
                ("PTEST_PROJECT_ID", checkout.project_id),
                ("PTEST_CHECKOUT_ID", checkout.checkout_id),
                ("PTEST_RUN_ID", grant.run_id),
                ("PTEST_ATTEMPT_ID", attempt_id),
                ("PTEST_WORKER_ID", "w000"),
                ("PTEST_RESOURCE_PREFIX", identity.resource_prefix),
                ("PTEST_PYTEST_COMMAND_VARIANTS", _configured_pytest_command_variants(config)),
            ))
            binding = reports.allocate_report(
                domain, checkout, run_id=grant.run_id, nonce=grant.nonce,
                attempt_id=attempt_id, runner=config.runner.kind,
                execution_mode=plan.execution, effective_profile="advanced",
            )
            prepared = replace(
                prepared, report_path=binding.path,
                env_updates=prepared.env_updates + (
                    ("PTEST_PYTEST_REPORT_PATH", str(binding.path)),
                    ("PTEST_GRANT_NONCE", grant.nonce),
                ),
            )
            prepared_runs.append(prepared)
            bindings.append(binding)
            attempts.append(identity)
        setup_prepared = _setup_prepared(
            config, checkout, request, prepared_runs[0], domain, grant,
            attempts[0])

        input_before = _capture_source(
            domain, config, request, ensure_key=True,
            execution_tier=C.ExecutionTier.ADVANCED,
            runtime_identity=(history_view.baseline.runtime_identity
                              if history_view.baseline is not None else None),
            baseline=history_view.baseline,
        )
        planning_invalidation = None
        if not _source_complete(planning_snapshot) or not _source_complete(input_before):
            planning_invalidation = _reason(
                "unknown-input", "shadow comparison input identity is unavailable")
        else:
            planning_invalidation = _source_invalidation(
                planning_snapshot, input_before)
        gate_count = 0
        gate_after: C.InputSnapshot | None = None

        def decide_compound() -> C.Reason | None:
            nonlocal gate_count, gate_after
            gate_count += 1
            if gate_count == 1 and planning_invalidation is not None:
                return planning_invalidation
            gate_after = _capture_source(
                domain, config, request, ensure_key=False,
                execution_tier=C.ExecutionTier.ADVANCED,
                runtime_identity=(history_view.baseline.runtime_identity
                                  if history_view.baseline is not None else None),
                baseline=history_view.baseline,
            )
            if gate_after.digest is None:
                return _reason("unknown-input", "shadow source identity is unavailable")
            return _source_invalidation(input_before, gate_after)

        raw_guard, frames, execution_s = _run_guard(
            domain, grant, tuple(prepared_runs), signal_state,
            decide_compound, setup=setup_prepared,
        )
        observed = getattr(frames, "facts_by_attempt", {})
        raw_codes = {
            attempt_id: facts["raw_exit_code"]
            for attempt_id, facts in observed.items()
        }
        setup_facts = getattr(frames, "setup_facts", None)
        setup_raw = (None if setup_facts is None
                     else setup_facts.get("raw_exit_code"))
        setup_problem = _guard_fact_problem(setup_facts)
        setup_failed = (
            setup_facts is not None
            and (setup_raw != 0 or setup_problem is not None))
        fact_problems = {
            attempt_id: problem
            for attempt_id, facts in observed.items()
            if (problem := _guard_fact_problem(facts)) is not None
        }
        handoff_complete = bool(frames.registered and frames.draining and frames.eof)
        guard_problem = (frames.invalid or setup_problem
                         or next(iter(fact_problems.values()), None))
        guard_exit_problem = False
        if (raw_guard != 0 and signal_state.number is None
                and guard_problem is None):
            guard_problem = _problem(
                "state-unavailable", "shadow guard exited before clean handoff",
                phase="guard")
            guard_exit_problem = True
        decision_reason = getattr(frames, "decision_reason", None)
        evidence: list[C.AttemptEvidence] = []
        evidence_by_id: dict[str, C.AttemptEvidence] = {}
        report_reason: C.Reason | None = None
        for index, binding in enumerate(bindings):
            attempt_id = f"a{index + 1:03d}"
            if attempt_id not in observed:
                break
            try:
                item = reports.consume_attempt_report(binding)
                if not _shadow_report_matches_guard(
                        item, observed[attempt_id].get("raw_exit_code")):
                    report_reason = _reason(
                        "report-invalid",
                        "shadow native report disagreed with guard facts")
                    break
                evidence.append(item)
                evidence_by_id[attempt_id] = item
            except (C.Problem, OSError):
                report_reason = _reason("report-invalid", "shadow native report was unavailable")
                break
        reasons: tuple[C.Reason, ...] = ()
        if setup_problem is not None:
            code = setup_problem.code if setup_problem.code in C.REASON_CODES else "state-unavailable"
            message = (setup_problem.message if code == setup_problem.code
                       else "guard reported invalid setup facts")
            reasons += (_reason(code, message),)
        for problem in fact_problems.values():
            code = problem.code if problem.code in C.REASON_CODES else "state-unavailable"
            message = (problem.message if code == problem.code
                       else "guard reported invalid runner facts")
            reasons += (_reason(code, message),)
        if decision_reason is not None:
            reasons += (decision_reason,)
        if report_reason is not None:
            reasons += (report_reason,)
        if gate_count < 2 and decision_reason is None and not setup_failed:
            reasons += (_reason("state-unavailable", "shadow guard handoff was incomplete"),)
        if guard_exit_problem:
            reasons += (_reason("state-unavailable",
                                "shadow guard exited before clean handoff"),)
        input_after = _capture_source(
            domain, config, request, ensure_key=False,
            execution_tier=C.ExecutionTier.ADVANCED,
            runtime_identity=(history_view.baseline.runtime_identity
                              if history_view.baseline is not None else None),
            baseline=history_view.baseline,
        )
        source_valid = _source_valid(input_before, input_after)
        if not source_valid:
            invalidation = _source_invalidation(input_before, input_after)
            if invalidation is not None:
                reasons += (invalidation,)
        # Report evidence starts source-invalid because the native report
        # cannot authenticate the controller's final tree.  Promote that
        # derivative only after the before/after snapshots agree; raw native
        # status, exit code and inventory completeness remain untouched.
        if evidence:
            evidence = [replace(item, result=replace(
                item.result, source_valid=source_valid)) for item in evidence]
            evidence_by_id = {item.attempt_id: item for item in evidence}
        selected_evidence = evidence_by_id.get("a001")
        full_evidence = evidence_by_id.get("a002")
        evidence_incomplete = bool(
            not handoff_complete or guard_problem is not None)
        if evidence_incomplete:
            evidence = [
                replace(item, terminal_complete=False)
                if (not handoff_complete
                    or frames.invalid is not None
                    or fact_problems.get(item.attempt_id) is not None)
                else item
                for item in evidence
            ]
            evidence_by_id = {item.attempt_id: item for item in evidence}
            selected_evidence = evidence_by_id.get("a001")
            full_evidence = evidence_by_id.get("a002")
        verdict = history.derive_shadow_verdict(selected_evidence, full_evidence)
        attempt_results = []
        if setup_facts is not None and not setup_failed:
            attempt_results.append(C.AttemptResult(
                attempt_id="a001", phase="setup", status=C.Status.PASSED,
                raw_exit_code=0, final_exit_code=0, source_valid=False,
                inventory_complete=False,
                timings=C.Timings(setup_s=getattr(frames, "setup_elapsed_s", None)),
            ))
        for index in range(2):
            attempt_id = f"a{index + 1:03d}"
            if attempt_id not in raw_codes:
                attempt_results.append(C.AttemptResult(
                    attempt_id=attempt_id, phase="execution",
                    status=C.Status.NOT_RUN, raw_exit_code=None,
                    final_exit_code=None, source_valid=False,
                    inventory_complete=False, timings=None,
                ))
                continue
            raw = raw_codes[attempt_id]
            item = evidence_by_id.get(attempt_id)
            attempt_problem = fact_problems.get(attempt_id)
            # A guard timeout still carries the child's native signal.  Keep
            # that per-attempt shell mapping (143 for raw -15); the compound
            # aggregate applies deadline precedence separately below.
            attempt_guard_problem = (
                None if raw is not None and raw < 0
                else attempt_problem or (frames.invalid if raw is None else None))
            attempt_incomplete = (
                raw is None
                or attempt_problem is not None
                or (report_reason is not None and item is None)
                or (frames.invalid is not None and item is None))
            attempt_status, attempt_exit, _, _ = _outcome(
                raw, None,
                attempt_guard_problem,
                incomplete=attempt_incomplete)
            attempt_results.append(C.AttemptResult(
                attempt_id=attempt_id, phase="execution", status=attempt_status,
                raw_exit_code=raw, final_exit_code=attempt_exit,
                source_valid=source_valid,
                inventory_complete=item is not None and item.inventory is not None
                and item.inventory.complete,
                timings=None,
            ))
        observed_codes = tuple(
            raw_codes[attempt_id] for attempt_id in ("a001", "a002")
            if attempt_id in raw_codes)
        status, exit_code, origin, signal_number, runner_code = _shadow_outcome(
            raw_codes=observed_codes, gate_count=gate_count,
            decision_reason=decision_reason, report_reason=report_reason,
            handoff_complete=handoff_complete,
            cancellation=signal_state.number,
            guard_problem=guard_problem,
            setup_raw=setup_raw,
            setup_problem=setup_problem,
        )
        if (decision_reason is not None
                and decision_reason.code in {"changed-during-run", "unknown-input"}
                and gate_count == 1 and not observed_codes):
            reasons = (_reason(
                "unsupported-capability",
                "shadow comparison was invalidated before the first attempt"),
                       *reasons)
        if verdict == "incomplete" and status is C.Status.PASSED:
            status, exit_code, origin = C.Status.INCOMPLETE, 70, "ptest"
        if setup_failed:
            setup_status, setup_exit, _, _ = _outcome(
                setup_raw, None, setup_problem,
                incomplete=setup_problem is not None)
            attempt_results = [C.AttemptResult(
                attempt_id="a001", phase="setup", status=setup_status,
                raw_exit_code=setup_raw, final_exit_code=setup_exit,
                source_valid=False, inventory_complete=False,
                timings=C.Timings(setup_s=getattr(frames, "setup_elapsed_s", None)),
            )]
            for attempt_id in ("a001", "a002"):
                attempt_results.append(C.AttemptResult(
                    attempt_id=attempt_id, phase="execution",
                    status=C.Status.NOT_RUN, raw_exit_code=None,
                    final_exit_code=None, source_valid=False,
                    inventory_complete=False, timings=None,
                ))
            origin = "setup" if signal_number is None else origin
        setup_s = getattr(frames, "setup_elapsed_s", None)
        execution_elapsed = max(0.0, execution_s - (setup_s or 0.0))
        result = C.RunResult(
            run_id=run_id, project_id=checkout.project_id,
            checkout_id=checkout.checkout_id, mode=C.Mode.SHADOW,
            status=status, phase="complete", started_at=_iso_now(),
            finished_at=_iso_now(), plan=selected_plan,
            command=command, granted_workers=grant.slots,
            memory_estimate_mb=grant.memory_estimate_mb,
            reserved_memory_mb=grant.reserved_memory_mb,
            runner_exit_code=runner_code, signal=signal_number,
            exit_code=exit_code, exit_origin=origin,
            source_valid=source_valid, full_gate_eligible=False,
            baseline_published=False,
            counts=None, timings=C.Timings(queue_s=time.monotonic() - queue_started,
                                           setup_s=setup_s,
                                           execution_s=execution_elapsed),
            attempts=tuple(attempt_results), reasons=reasons,
            limitations=_source_limitations(input_before, input_after),
            sequence=history.next_sequence(domain, checkout),
            input_before=input_before, input_after=input_after,
            policy_digest=_policy_digest(config),
            runtime_identity=(None if selected_evidence is None
                              else selected_evidence.runtime_identity),
        )
        comparison = C.ShadowComparison(
            selected=selected_evidence, full=full_evidence,
            verdict=verdict, expected_quarantine=plans.quarantine,
        )
        try:
            publication = history.publish_shadow_outcome(
                domain, checkout, result, comparison)
            transition_reasons = ()
            if verdict in {"suspected-miss", "unclassified-divergence"}:
                transition_reasons = (_reason(
                    "selection-shadow-quarantine",
                    "selection is quarantined pending a corrected shadow comparison"),)
            result = replace(
                result,
                reasons=_unique_reasons(
                    result.reasons + transition_reasons + publication.reasons),
            )
            if not publication.committed:
                result = _incomplete(result, _reason(
                    "coordinator-unavailable",
                    "shadow history could not be committed"))
                result = replace(result, reasons=_unique_reasons(result.reasons))
        except (C.Problem, OSError, ValueError, TypeError):
            result = _incomplete(result, _reason(
                "coordinator-unavailable", "shadow history could not be committed"))
        def finalize_shadow(final_result: C.RunResult) -> None:
            if frames.registered:
                proof = scheduler.begin_finalization(domain, grant)
                scheduler.finish(domain, grant, proof, C.Finalization(
                    outcome_id=None, status=final_result.status,
                    exit_code=final_result.exit_code,
                    source_valid=final_result.source_valid, committed=True,
                ))
                return
            if not scheduler.cancel_pending(domain, ticket, owner):
                raise _problem(
                    "ownership-uncertain",
                    "shadow admission could not be released")
        try:
            return _export(domain, checkout, request, result,
                           finalize=finalize_shadow)
        finally:
            for binding in bindings:
                reports.cleanup_report(binding)
    except BaseException:
        for binding in bindings:
            reports.cleanup_report(binding)
        if grant is not None:
            try:
                if frames is not None and frames.registered:
                    proof = scheduler.begin_finalization(domain, grant)
                    scheduler.finish(domain, grant, proof, C.Finalization(
                        outcome_id=None, status=C.Status.INCOMPLETE,
                        exit_code=70, source_valid=False, committed=False,
                    ))
                else:
                    scheduler.cancel_pending(domain, ticket, owner)
            except (C.Problem, OSError):
                pass
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def execute(domain: C.DomainPaths, config: C.Config,
            request: C.RunRequest) -> C.RunResult:
    """Run one admitted command and return its typed outcome."""
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.Config):
        raise TypeError("execute requires DomainPaths and Config")
    if not isinstance(request, C.RunRequest):
        raise TypeError("execute requires RunRequest")
    if request.shadow:
        return _execute_shadow(domain, config, request)
    if request.probe is not None or request.mode is C.Mode.PROBE:
        return _execute_probe(domain, config, request)
    native_pytest = config.runner.kind is C.RunnerKind.PYTEST
    native_runner = native_pytest
    adapter = adapter_for(config.runner.kind)
    catalog_profile = (adapter.qualified_profile(config) if native_runner else None)
    if (native_pytest and request.mode is C.Mode.AUTOMATIC
            and (catalog_profile is None or config.config_path is None)):
        raise _problem("unsupported-capability",
                       "pytest automatic selection requires a qualified profile")
    checkout = _checkout(config)
    needs_history = native_runner and (
        request.mode is C.Mode.AUTOMATIC or catalog_profile is not None)
    if needs_history:
        # History qualification is read-only and therefore cannot bootstrap the
        # normal account coordinator. Initialize it before the first-run read.
        scheduler.initialize(domain)
        stored_profile = history.read_qualified_profile(
            domain, checkout, config.runner.kind)
        history_view = history.read_history(domain, checkout)
    else:
        stored_profile = None
        history_view = C.HistoryView(baseline=None, limitations=())
    # A stored observation can refine, but never create, a closed catalog
    # declaration.  Configuration changes therefore revoke admission until
    # the current command matches the registry's static tuple again.
    qualified_profile = None
    if catalog_profile is not None:
        if request.mode is C.Mode.AUTOMATIC and history_view.baseline is not None:
            # Automatic selection after the first baseline must be refined by
            # the consumed authenticated profile. A catalog match alone is
            # enough to establish the first full baseline, but never enough to
            # authorize a repeat selected plan.
            qualified_profile = ({} if stored_profile is None else {
                "source": "stored", **stored_profile})
        else:
            qualified_profile = dict(catalog_profile)
    support = (adapter.compound_support(
                   config, qualified_profile=qualified_profile) if native_runner else
               C.CompoundSupport(
                   selection=False, parallel_identity=False, profile=None,
                   limitations=(_reason("unsupported-capability",
                                        "runner has no compound profile"),)))
    advanced = native_runner and support.profile is not None
    if native_pytest:
        allowed = {C.Mode.SCOPED, C.Mode.FULL}
        if advanced or catalog_profile is not None:
            # A cataloged project may have a prior baseline whose private
            # authenticated profile was removed or invalidated.  Permit the
            # automatic request to fail closed to the configured full gate;
            # only a consumed profile may enter the advanced path above.
            allowed.add(C.Mode.AUTOMATIC)
        if request.mode not in allowed:
            raise _problem("unsupported-capability", "pytest execution requires explicit scope or --full")
        if request.mode is C.Mode.FULL and request.base is not None:
            raise _problem("invalid-config", "--base is unavailable with explicit pytest full execution")
        if request.mode is C.Mode.FULL and "." in config.runner.test_roots:
            raise _problem("unsupported-capability", "pytest full execution does not support a dot test root")
    elif config.runner.kind not in (C.RunnerKind.COMMAND, C.RunnerKind.VITEST):
        raise _problem("unsupported-capability", "native profile execution is deferred")
    if request.mode is not C.Mode.SCOPED and request.argv:
        raise _problem("invalid-config", "literal command arguments require scoped mode")
    if not native_runner and not adapter.requires_exclusive(config):
        raise _problem("unsupported-capability", "command execution requires exclusive admission")
    run_id = secrets.token_hex(16)
    planning_runtime = (
        history_view.baseline.runtime_identity
        if advanced and request.mode is C.Mode.AUTOMATIC
        and history_view.baseline is not None else None)
    source_baseline = (
        history_view.baseline
        if advanced and request.mode is C.Mode.AUTOMATIC else None)
    planning_snapshot = (_capture_source(
        domain, config, request, ensure_key=False,
        execution_tier=C.ExecutionTier.ADVANCED,
        runtime_identity=planning_runtime, baseline=source_baseline)
                        if advanced else C.InputSnapshot(
                            digest=None, compatibility=None, head=None, clean=False))
    plan = (_advanced_plan(config, request, planning_snapshot,
                           history_view, support)
            if advanced else _plan(request, config.runner.kind))
    if (native_pytest and not advanced and catalog_profile is not None
            and request.mode is C.Mode.AUTOMATIC and plan.execution == "full"):
        # An unqualified repeat automatic request is a basic full gate, not
        # an automatic-mode plan handed to the basic adapter.
        plan = replace(plan, mode=C.Mode.FULL)
    # Section F: a full gate runs the project's own checked-in suite. The
    # run result and the human output carry the label built from the
    # bridge-owned attempt report; the static prediction stays out of the
    # plan so it can never share the real label's code and wording.
    if advanced and plan.execution == "none":
        # A qualified automatic selection may prove that no test is affected.
        # This is a completed policy decision, not a native attempt: do not
        # hand an empty argv to a bridge that cannot authenticate collection.
        started = _iso_now()
        no_tests_valid = _source_valid(planning_snapshot, planning_snapshot)
        try:
            result = _result(
                run_id=run_id, checkout=checkout, request=request, plan=plan,
                command=_summary(config, plan, request, 1),
                status=C.Status.NO_TESTS_NEEDED, phase="complete", started=started,
                runner_code=0, exit_code=0, origin="ptest",
                reasons=plan.reasons, limitations=_source_limitations(planning_snapshot),
                input_before=planning_snapshot, input_after=planning_snapshot,
                source_valid=no_tests_valid)
            result = replace(
                result, sequence=history.next_sequence(domain, checkout),
                policy_digest=_policy_digest(config),
            )
            publication = history.publish_outcome(domain, checkout, result, None)
            result = replace(
                result, baseline_published=publication.baseline_published,
                reasons=_unique_reasons(result.reasons + publication.reasons),
            )
            return _export(domain, checkout, request, result)
        except (C.Problem, OSError):
            return _export(domain, checkout, request, _incomplete(
                result,
                _reason("coordinator-unavailable",
                        "advanced no-tests-needed history could not be committed"),
            ))
    # The command summary is redacted and never includes token values.
    requested_slots = 1 if (native_pytest and not advanced) else min(
        config.runner.workers,
        config.runner.workers if request.workers is None else request.workers,
    )
    if advanced and not support.parallel_identity:
        requested_slots = 1
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
            slots=requested_slots, exclusive=not native_runner,
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
        if native_runner:
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
        expected_runtime_identity = (
            history_view.baseline.runtime_identity
            if (advanced and plan.execution == "selected"
                and history_view.baseline is not None)
            else None)
        try:
            prepared = (adapter.prepare_advanced(
                effective, plan, grant, attempt,
                expected_runtime_identity=expected_runtime_identity)
                        if advanced else adapter.prepare(effective, plan, grant, attempt))
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
        if advanced and native_pytest:
            prepared = replace(
                prepared,
                env_updates=prepared.env_updates + (
                    ("PTEST_PYTEST_COMMAND_VARIANTS",
                     _configured_pytest_command_variants(config)),
                ),
            )
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
        if native_runner:
            try:
                report_binding = reports.allocate_report(
                    domain, checkout,
                    run_id=grant.run_id,
                    nonce=grant.nonce,
                    attempt_id=attempt.attempt_id,
                    runner=config.runner.kind,
                    execution_mode=plan.execution,
                    effective_profile=(C.ExecutionTier.ADVANCED.value
                                       if advanced else C.ExecutionTier.BASIC_SERIAL.value),
                )
            except BaseException:
                scheduler.cancel_pending(domain, ticket, owner)
                raise
            report_env = (
                ("PTEST_GRANT_NONCE", grant.nonce),
                ("PTEST_PYTEST_ATTEMPT", attempt.attempt_id),
                ("PTEST_PYTEST_EXECUTION", plan.execution),
                ("PTEST_PYTEST_REPORT_PATH", str(report_binding.path)),
            )
            prepared = replace(prepared, report_path=report_binding.path,
                               env_updates=prepared.env_updates + report_env)
        if signals.number is not None:
            if scheduler.cancel_pending(domain, ticket, owner):
                return _export(domain, checkout, request, _cancel_result(
                    run_id, checkout, request, plan, command, signals.number, queue_s))
            raise _problem("ownership-uncertain", "pending cancellation could not be confirmed")
        # Capture the initial identity after exclusive admission and queue wait,
        # immediately before launching the admitted command.
        try:
            input_before = _capture_source(
                domain, effective, request, ensure_key=True,
                execution_tier=(C.ExecutionTier.ADVANCED if advanced
                                else C.ExecutionTier.BASIC_SERIAL),
                runtime_identity=expected_runtime_identity,
                baseline=source_baseline)
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
                setup_reason = _finish_setup(
                    domain, effective, checkout, setup_fingerprint_before)
                if setup_reason is not None:
                    return setup_reason
            gate_snapshot = _capture_source(
                domain, effective, request, ensure_key=False,
                execution_tier=(C.ExecutionTier.ADVANCED if advanced
                                else C.ExecutionTier.BASIC_SERIAL),
                runtime_identity=expected_runtime_identity,
                baseline=source_baseline)
            if (native_runner and plan.execution == "full"
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
        # Section F: the run label comes from the bridge-owned attempt
        # report consumed below, never from the static prediction above.
        reasons: tuple = ()
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
        input_after = _capture_source(
            domain, effective, request, ensure_key=False,
            execution_tier=(C.ExecutionTier.ADVANCED if advanced
                            else C.ExecutionTier.BASIC_SERIAL),
            runtime_identity=expected_runtime_identity,
            baseline=source_baseline)
        source_valid = _source_valid(input_before, input_after)
        if stopped_at_gate or (native_pytest and not advanced):
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
        if (native_runner and plan.execution == "full"
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
        native_evidence: C.AttemptEvidence | None = None
        native_report: reports.NativeTerminalReport | None = None
        report_reason: C.Reason | None = None
        if native_runner and not setup_failed:
            try:
                if advanced:
                    native_evidence = reports.consume_attempt_report(report_binding)
                    consumed_report = True
                    observed_runtime = native_evidence.runtime_identity
                    if expected_runtime_identity is not None and (
                            observed_runtime != expected_runtime_identity):
                        report_reason = _reason(
                            "report-invalid",
                            "native runtime identity differs from the qualified baseline",
                        )
                    elif (native_evidence.result.raw_exit_code != raw
                          or native_evidence.result.final_exit_code != raw):
                        report_reason = _reason(
                            "report-invalid",
                            "native terminal evidence did not authenticate the native exit",
                        )
                    elif native_evidence.inventory is None or not native_evidence.inventory.complete:
                        report_reason = _reason(
                            "report-invalid",
                            "native terminal evidence lacks a complete inventory",
                        )
                    else:
                        # The first snapshots were captured before the bridge
                        # could disclose its observed runtime facts. Re-read
                        # both sides with that digest, and require the raw
                        # source identity to remain unchanged. This is the
                        # controller-owned source/runtime cross-check; the
                        # report's source_valid bit is intentionally ignored.
                        verified_after = _capture_source(
                            domain, effective, request, ensure_key=False,
                            execution_tier=C.ExecutionTier.ADVANCED,
                            runtime_identity=observed_runtime,
                            baseline=source_baseline)
                        if (verified_after.digest != input_before.digest
                                or verified_after.digest != input_after.digest):
                            report_reason = _reason(
                                "changed-during-run",
                                "native source identity changed while runtime evidence was authenticated",
                            )
                        elif not _source_complete(verified_after):
                            report_reason = _reason(
                                "unknown-input",
                                "verified native runtime could not produce complete source identity",
                            )
                        else:
                            # The pre-launch snapshot was intentionally taken
                            # before the child disclosed its runtime facts, so
                            # it carried no compatibility MAC.  The unchanged
                            # digest proves that its source inputs are the
                            # same ones authenticated by the terminal
                            # snapshot; bind that already-captured snapshot
                            # to the now-authenticated compatibility instead
                            # of rescanning the tree a second time.
                            verified_before = replace(
                                input_before,
                                compatibility=verified_after.compatibility,
                                limitations=verified_after.limitations,
                            )
                            advanced_plan = replace(
                                result.plan,
                                input_digest=verified_after.digest,
                                compatibility=verified_after.compatibility,
                            )
                            attempts = tuple(
                                replace(
                                    item,
                                    source_valid=True,
                                    inventory_complete=True,
                                ) if item.attempt_id == native_evidence.attempt_id else item
                                for item in result.attempts)
                            result = replace(
                                result,
                                plan=advanced_plan,
                                input_before=verified_before,
                                input_after=verified_after,
                                source_valid=True,
                                runtime_identity=observed_runtime,
                                counts=C.Counts(
                                    collected=len(native_evidence.inventory.tests),
                                    executed=sum(
                                        item.outcome in {C.Outcome.PASSED, C.Outcome.FAILED}
                                        for item in native_evidence.inventory.tests),
                                    passed=sum(
                                        item.outcome is C.Outcome.PASSED
                                        for item in native_evidence.inventory.tests),
                                    failed=sum(
                                        item.outcome is C.Outcome.FAILED
                                        for item in native_evidence.inventory.tests),
                                    skipped=sum(
                                        item.outcome is C.Outcome.SKIPPED
                                        for item in native_evidence.inventory.tests),
                                    unknown=sum(
                                        item.outcome is C.Outcome.UNKNOWN
                                        for item in native_evidence.inventory.tests),
                                ),
                                attempts=attempts,
                                full_gate_eligible=bool(
                                    plan.execution == "full"
                                    and result.status is C.Status.PASSED
                                    and result.runner_exit_code == 0
                                    and native_evidence.result.inventory_complete
                                    and native_evidence.terminal_complete),
                                # Rebuild source limitations from the
                                # authenticated snapshots.  The initial
                                # result carries execution-only placeholders;
                                # once native evidence is verified, retaining
                                # that generic text would make a valid
                                # advanced result look unsupported.  Do not
                                # identify placeholders by their prose.
                                limitations=_source_limitations(verified_after),
                            )
                            if (native_evidence is not None
                                    and native_evidence.result.status is C.Status.PASSED):
                                try:
                                    history.publish_qualified_profile(
                                        domain, checkout, config.runner.kind,
                                        native_evidence,
                                        binding=report_binding)
                                except (C.Problem, OSError):
                                    report_reason = _reason(
                                        "coordinator-unavailable",
                                        "native qualification evidence could not be persisted")
                else:
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
                if advanced:
                    # A rejected advanced report is not an observation. Never
                    # let a bridge-supplied identity or source bit survive as
                    # controller-owned promotion evidence.
                    result = replace(
                        result, source_valid=False, runtime_identity=None,
                        full_gate_eligible=False,
                        attempts=tuple(replace(item, source_valid=False,
                                               inventory_complete=False)
                                       for item in result.attempts),
                    )
            # Section F "never silent": a narrowed full run is always
            # labelled. The label is built from the bridge-owned report;
            # allowed narrowing with no label means the run is incomplete,
            # never PASSED.
            if native_pytest and plan.execution == "full" and not setup_failed:
                bridge_label = None
                if advanced:
                    if native_evidence is not None and report_reason is None:
                        bridge_label = native_evidence.project_filter_label
                elif (consumed_report and report_reason is None
                        and native_report is not None):
                    bridge_label = reports.project_filter_label(
                        native_report.project_narrowing)
                if bridge_label is not None:
                    result = replace(result, reasons=result.reasons + (
                        _reason("project-filtered", bridge_label),))
                elif (result.status is C.Status.PASSED
                        and executability.full_project_filter_text(
                            checkout.root, config.runner.test_roots) is not None):
                    result = _incomplete(result, _reason(
                        "report-invalid",
                        "bridge allowed project narrowing but reported "
                        "no project filter label"))

        # Every real advanced attempt is a private history event, including a
        # native failure or a refused/malformed report. Promotion is possible
        # only when the authenticated evidence above made the controller set
        # full_gate_eligible and runtime/source identity itself.
        if advanced:
            try:
                sequence = history.next_sequence(domain, checkout)
                result = replace(
                    result,
                    sequence=sequence,
                    policy_digest=_policy_digest(config),
                    reasons=_unique_reasons(
                        result.reasons + (() if report_reason is None else (report_reason,))),
                )
                publication = history.publish_outcome(
                    domain, checkout, result,
                    None if native_evidence is None else native_evidence.inventory,
                )
                result = replace(
                    result,
                    baseline_published=publication.baseline_published,
                    reasons=_unique_reasons(result.reasons + publication.reasons),
                )
            except (C.Problem, OSError):
                result = _incomplete(
                    result,
                    _reason("coordinator-unavailable",
                            "advanced attempt history could not be committed"),
                )

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


def run_setup_only(domain: C.DomainPaths, config: C.Config, *,
                   queue_timeout_s: float,
                   fixture_domain: Path | None = None) -> C.RunResult | None:
    """Run the declared setup once, without running any tests.

    The setup executes as a literal exclusive command through
    :func:`execute`: the same argv, checkout root, and identity
    environment :func:`_setup_prepared` declares for setup phases of
    normal runs.  On a passing setup the fingerprint is recorded with
    the same required-paths check and before/after revalidation the
    execution gate performs, so a later ``no_setup`` run proceeds.
    Returns None when no setup run is owed; otherwise returns the setup
    attempt result (a non-passing result leaves the blocker in place).
    Raises ``C.Problem`` for admission or infrastructure failures.
    """
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.Config):
        raise TypeError("run_setup_only requires DomainPaths and Config")
    if config.setup is None:
        return None
    checkout = _checkout(config)
    if _required_setup_state(config, checkout, domain) is None:
        return None
    before = _setup_fingerprint(config, checkout)
    setup_config = replace(
        config,
        runner=replace(
            config.runner, kind=C.RunnerKind.COMMAND,
            launcher=tuple(config.setup.argv), args=(), full_args=()),
        setup=None)
    result = execute(
        domain, setup_config,
        C.RunRequest(mode=C.Mode.SCOPED, argv=(),
                     queue_timeout_s=queue_timeout_s,
                     fixture_domain=fixture_domain))
    if result.status is C.Status.PASSED:
        setup_reason = _finish_setup(domain, config, checkout, before)
        if setup_reason is not None:
            raise _problem(setup_reason.code, setup_reason.message,
                           phase="setup")
    return result


__all__ = ["execute", "run_setup_only"]
