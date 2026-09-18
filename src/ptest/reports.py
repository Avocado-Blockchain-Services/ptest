"""Private, bounded native terminal-report allocation and consumption.

This module is deliberately separate from the public document contracts.  A
binding is created before a native process is launched; the process later
creates the bound file exclusively.  Only a complete, identity-matching,
strictly shaped record is admitted as terminal evidence.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from . import files as F


PROTOCOL_VERSION = 1
REPORT_MAX_BYTES = C.NATIVE_REPORT_MAX_BYTES
_PHASE = "reports"
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_NONCE_RE = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_RE = re.compile(r"a(00[1-9]|010)")
_REPORT_NAME_RE = re.compile(r"native-a(00[1-9]|010)-[0-9a-f]{32}\.json")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}")
_RUNNERS = frozenset({"pytest", "vitest"})
_EXECUTION_MODES = frozenset({"full", "selected", "scoped"})
_PROFILES = frozenset({"advanced", "basic_serial"})
_PROBLEMS = frozenset({"bridge-refused", "native-failure"})
_FIELDS = frozenset({
    "protocol", "run_id", "nonce", "attempt_id", "runner",
    "observed_runtime_version", "execution_mode", "effective_profile",
    "terminal_complete", "native_exit_code", "bridge_exit_code", "problem",
})


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE, retryable=False)


def _reject(code: str = "report-invalid") -> None:
    raise _problem(code, "native terminal report was rejected")


def _check_hex(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _reject()
    return value


def _check_name(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or not value or value not in allowed:
        _reject()
    return value


def _check_version(value: object) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        _reject()
    return value


def _check_exit(value: object, *, allow_none: bool) -> int | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not -255 <= value <= 255:
        _reject()
    return value


def _identity(path: Path) -> tuple[int, int]:
    stamp = os.lstat(path)
    return stamp.st_dev, stamp.st_ino


@dataclass(frozen=True, kw_only=True)
class NativeReportBinding:
    """Executor-owned identity and path for one native report attempt."""

    protocol: int
    run_id: str
    nonce: str
    attempt_id: str
    runner: str
    execution_mode: str
    effective_profile: str
    report_directory: Path
    report_name: str
    _created_identity: tuple[int, int] | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.protocol) is not int or self.protocol != PROTOCOL_VERSION:
            raise TypeError("report binding has an unsupported protocol")
        object.__setattr__(self, "run_id", _check_hex(self.run_id, _RUN_ID_RE))
        object.__setattr__(self, "nonce", _check_hex(self.nonce, _NONCE_RE))
        if not isinstance(self.attempt_id, str) or _ATTEMPT_RE.fullmatch(self.attempt_id) is None:
            raise TypeError("report binding has an invalid attempt")
        object.__setattr__(self, "runner", _check_name(self.runner, _RUNNERS))
        object.__setattr__(
            self, "execution_mode", _check_name(self.execution_mode, _EXECUTION_MODES),
        )
        object.__setattr__(
            self, "effective_profile", _check_name(self.effective_profile, _PROFILES),
        )
        if not isinstance(self.report_directory, Path) or not self.report_directory.is_absolute():
            raise TypeError("report binding directory must be an absolute path")
        if not isinstance(self.report_name, str) or _REPORT_NAME_RE.fullmatch(self.report_name) is None:
            raise TypeError("report binding has an invalid report name")
        if not self.report_name.startswith(f"native-{self.attempt_id}-"):
            raise TypeError("report binding report name does not match its attempt")

    @property
    def path(self) -> Path:
        return self.report_directory / self.report_name


@dataclass(frozen=True, kw_only=True)
class NativeTerminalReport:
    """The complete, private terminal record admitted by :func:`consume_report`."""

    protocol: int
    run_id: str
    nonce: str
    attempt_id: str
    runner: str
    observed_runtime_version: str
    execution_mode: str
    effective_profile: str
    terminal_complete: bool
    native_exit_code: int | None
    bridge_exit_code: int
    problem: str | None = None

    def __post_init__(self) -> None:
        if type(self.protocol) is not int or self.protocol != PROTOCOL_VERSION:
            raise TypeError("terminal report has an unsupported protocol")
        object.__setattr__(self, "run_id", _check_hex(self.run_id, _RUN_ID_RE))
        object.__setattr__(self, "nonce", _check_hex(self.nonce, _NONCE_RE))
        if not isinstance(self.attempt_id, str) or _ATTEMPT_RE.fullmatch(self.attempt_id) is None:
            raise TypeError("terminal report has an invalid attempt")
        object.__setattr__(self, "runner", _check_name(self.runner, _RUNNERS))
        object.__setattr__(self, "observed_runtime_version", _check_version(self.observed_runtime_version))
        object.__setattr__(
            self, "execution_mode", _check_name(self.execution_mode, _EXECUTION_MODES),
        )
        object.__setattr__(
            self, "effective_profile", _check_name(self.effective_profile, _PROFILES),
        )
        if not isinstance(self.terminal_complete, bool):
            raise TypeError("terminal report completeness must be boolean")
        object.__setattr__(
            self, "native_exit_code", _check_exit(self.native_exit_code, allow_none=True),
        )
        bridge_exit = _check_exit(self.bridge_exit_code, allow_none=False)
        assert bridge_exit is not None
        object.__setattr__(self, "bridge_exit_code", bridge_exit)
        if self.problem is not None and self.problem not in _PROBLEMS:
            raise TypeError("terminal report has an unknown problem")
        if self.terminal_complete:
            if self.native_exit_code is None or self.bridge_exit_code != self.native_exit_code:
                raise ValueError("terminal report has inconsistent exit codes")
            if self.problem == "bridge-refused":
                raise ValueError("complete terminal report cannot be bridge-refused")
            if self.problem == "native-failure" and self.native_exit_code == 0:
                raise ValueError("native-failure requires a nonzero native exit")
        else:
            if self.problem != "bridge-refused" or self.bridge_exit_code == 0:
                raise ValueError("incomplete terminal report requires bridge refusal")
            if self.native_exit_code not in (None, 0):
                raise ValueError("bridge refusal cannot carry a native failure")


def _binding(binding: object) -> NativeReportBinding:
    if not isinstance(binding, NativeReportBinding):
        raise TypeError("report binding is required")
    return binding


def allocate_report(
    domain: C.DomainPaths,
    checkout: C.CheckoutIdentity,
    *,
    run_id: str,
    nonce: str,
    attempt_id: str,
    runner: str | C.RunnerKind,
    execution_mode: str,
    effective_profile: str,
) -> NativeReportBinding:
    """Reserve a private, absent report pathname for one admitted attempt."""
    if not isinstance(domain, C.DomainPaths) or not isinstance(checkout, C.CheckoutIdentity):
        raise TypeError("allocate_report requires DomainPaths and CheckoutIdentity")
    if isinstance(runner, C.RunnerKind):
        runner = runner.value
    if not isinstance(attempt_id, str) or _ATTEMPT_RE.fullmatch(attempt_id) is None:
        raise TypeError("report binding has an invalid attempt")
    candidate_name = f"native-{attempt_id}-{secrets.token_hex(16)}.json"
    candidate = NativeReportBinding(
        protocol=PROTOCOL_VERSION,
        run_id=run_id,
        nonce=nonce,
        attempt_id=attempt_id,
        runner=runner,
        execution_mode=execution_mode,
        effective_profile=effective_profile,
        report_directory=domain.root / "checkouts" / checkout.checkout_id / "reports",
        report_name=candidate_name,
    )
    try:
        F.validate_private_dir(domain.root)
        checkouts = F.ensure_private_dir(domain.root, "checkouts")
        checkout_dir = F.ensure_private_dir(checkouts, checkout.checkout_id)
        reports = F.ensure_private_dir(checkout_dir, "reports")
    except C.Problem as exc:
        raise _problem(exc.code, "native report state is unavailable") from None
    for _ in range(8):
        name = candidate.report_name if _ == 0 else f"native-{candidate.attempt_id}-{secrets.token_hex(16)}.json"
        try:
            F.validate_single_name(name)
            os.lstat(reports / name)
        except FileNotFoundError:
            return NativeReportBinding(
                protocol=candidate.protocol,
                run_id=candidate.run_id,
                nonce=candidate.nonce,
                attempt_id=candidate.attempt_id,
                runner=candidate.runner,
                execution_mode=candidate.execution_mode,
                effective_profile=candidate.effective_profile,
                report_directory=reports,
                report_name=name,
            )
        except C.Problem as exc:
            raise _problem(exc.code, "native report target is unsafe") from None
    _reject("already-exists")
    raise AssertionError("unreachable")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate report field")
    return dict(pairs)


def _decode(binding: NativeReportBinding, raw: bytes) -> NativeTerminalReport:
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder(
            object_pairs_hook=_unique_fields,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        value, end = decoder.raw_decode(text)
        if text[end:].strip() or not isinstance(value, dict):
            raise ValueError
    except (UnicodeDecodeError, ValueError, RecursionError):
        _reject()
    if set(value) != _FIELDS:
        _reject()
    try:
        report = NativeTerminalReport(**value)
    except (TypeError, ValueError):
        _reject()
    if (
        report.run_id != binding.run_id
        or report.nonce != binding.nonce
        or report.attempt_id != binding.attempt_id
        or report.runner != binding.runner
        or report.execution_mode != binding.execution_mode
        or report.effective_profile != binding.effective_profile
    ):
        _reject()
    return report


def consume_report(binding: NativeReportBinding) -> NativeTerminalReport:
    """Consume a binding once, after quiescence, through bounded no-follow access."""
    binding = _binding(binding)
    if binding._created_identity is not None:
        _reject()
    try:
        F.validate_private_file(binding.path)
        before = _identity(binding.path)
        raw = F.read_regular(binding.report_directory, binding.report_name, REPORT_MAX_BYTES + 1)
        if len(raw) > REPORT_MAX_BYTES:
            _reject("capacity-exceeded")
        F.validate_private_file(binding.path)
        after = _identity(binding.path)
        if before != after:
            _reject("unsafe-path")
    except C.Problem as exc:
        raise _problem(exc.code, "native report could not be read") from None
    except (FileNotFoundError, OSError):
        _reject("state-unavailable")
    report = _decode(binding, raw)
    object.__setattr__(binding, "_created_identity", before)
    return report


def cleanup_report(binding: NativeReportBinding) -> None:
    """Remove only the exact regular file successfully consumed by this binding."""
    binding = _binding(binding)
    expected = binding._created_identity
    if expected is None:
        return
    try:
        F.validate_private_file(binding.path)
        if _identity(binding.path) != expected:
            return
        os.unlink(binding.path)
    except (C.Problem, FileNotFoundError, OSError):
        return


# Explicit aliases keep the seam discoverable without creating alternate behavior.
ReportBinding = NativeReportBinding
NativeReport = NativeTerminalReport
allocate_report_target = allocate_report
read_report = consume_report
