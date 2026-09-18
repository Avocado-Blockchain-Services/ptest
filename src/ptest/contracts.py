"""Frozen shared records, codecs, defaults and schema descriptors (Task0 owned).

Single source of truth for every public record/enum/default, the eight public
JSON documents, the private guard ControlFrame/LaunchManifest framing and the
dependency-free bridge protocol descriptor. Later tasks consume these types
and the generated ``docs/schemas/v1/*.json`` / ``runtime/protocol-v1.json``;
no parallel hand-maintained schemas exist.

Record construction validates strictly (``TypeError`` for wrong Python types,
``ValueError`` for out-of-domain values). Codec entry points translate those
into :class:`Problem` with stable machine codes.
"""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PTEST_VERSION = "0.1.0"
SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1

PUBLIC_KINDS = (
    "init", "register", "plan", "where",
    "status", "history", "doctor", "run",
)


class Problem(Exception):
    """Typed failure with a stable machine code and no raw details."""

    def __init__(self, *, code: str, message: str, phase: str,
                 retryable: bool = False) -> None:
        if not isinstance(code, str) or not code:
            raise TypeError("Problem code must be a nonempty string")
        if not isinstance(message, str) or not message:
            raise TypeError("Problem message must be a nonempty string")
        if not isinstance(phase, str) or not phase:
            raise TypeError("Problem phase must be a nonempty string")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.phase = phase
        self.retryable = bool(retryable)


class RunnerKind(str, Enum):
    PYTEST = "pytest"
    VITEST = "vitest"
    GO = "go"
    CARGO = "cargo"
    COMMAND = "command"


class ExecutionTier(str, Enum):
    ADVANCED = "advanced"
    BASIC_SERIAL = "basic_serial"
    BOUNDED_NATIVE = "bounded_native"
    EXCLUSIVE_COMMAND = "exclusive_command"
    UNAVAILABLE = "unavailable"


class Outcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAIL = "xfail"
    XPASS = "xpass"
    UNKNOWN = "unknown"


class Mode(str, Enum):
    AUTOMATIC = "automatic"
    SCOPED = "scoped"
    FULL = "full"
    SHADOW = "shadow"
    PROBE = "probe"


class Status(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"
    NO_TESTS_NEEDED = "no-tests-needed"
    NOT_RUN = "not-run"


class LeaseState(str, Enum):
    QUEUED = "QUEUED"
    GRANTED = "GRANTED"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    FINALIZING = "FINALIZING"
    RELEASED = "RELEASED"
    CANCELLED = "CANCELLED"
    CANCELLING = "CANCELLING"
    UNCERTAIN = "UNCERTAIN"


class InitAction(str, Enum):
    CREATED = "created"
    PREVIEW = "preview"
    EXISTING = "existing"


MAX_PROMPT_BYTES = 65536
DEFAULT_QUEUE_TIMEOUT_S = 1800.0
MAX_QUEUE_TIMEOUT_S = 86400.0
DEFAULT_SETUP_TIMEOUT_S = 300.0
DEFAULT_ATTEMPT_TIMEOUT_S = 30.0
MAX_COMPOUND_TIMEOUT_S = 600.0
CANCEL_GRACE_S = 3.0
SCHEDULER_POLL_S = 0.25
CONTROL_FRAME_MAX_BYTES = 65536
CONTROL_FRAME_MAX_NESTING = 16
MANIFEST_MAX_BYTES = 4194304
MANIFEST_MAX_NESTING = 32
NATIVE_REPORT_MAX_BYTES = 16777216
NATIVE_REPORT_MAX_TESTS = 100000
TEST_ID_MAX_BYTES = 4096
LINE_MAX_BYTES = 65536
PROTOCOL_MAX_DEPTH = 32
PROTOCOL_MAX_EVENTS = 500000
HISTORY_MAX_BYTES = 134217728
HISTORY_MAX_SUMMARIES = 200
HISTORY_RETAIN_DAYS = 30
SHARED_STATE_MAX_BYTES = 1073741824
MAX_PENDING_JOBS = 256
MAX_TERMINAL_SUMMARIES = 256
INPUT_ENVELOPE_MAX_FILES = 100000
INPUT_FILE_MAX_BYTES = 16777216
INPUT_TOTAL_MAX_BYTES = 536870912
INPUT_MAX_ELAPSED_S = 10.0
STATIC_PLAN_INVENTORY_CAP = 10000

REASON_CODES = frozenset({
    "initialization-required", "unsupported-platform", "unsupported-capability",
    "invalid-config", "unsafe-path",
    "missing-executable", "nested-invocation", "queue-timeout",
    "coordinator-unavailable", "coordinator-corrupt", "protocol-mismatch",
    "capacity-exceeded", "ownership-uncertain",
    "unsupported-detached-descendant", "no-baseline", "incompatible-baseline",
    "unknown-input", "policy-invalid", "policy-changed", "prior-failure",
    "full-gate-obligation", "changed-during-run", "incomplete-inventory",
    "report-invalid", "state-unavailable", "no-tests-needed",
    "selection-disabled", "scan-limit", "static-evidence-insufficient",
    "probe-isolation-required", "unredacted-command-disclosure",
    "already-exists", "invalid-bound",
})

FINDING_CODES = frozenset({
    "db.per-test-initialization", "db.cleanup-ownership", "cache.global-flush",
    "resource.fixed-name", "network.fixed-port", "time.blocking-sleep",
    "network.live-target", "process.detached-child", "fixture.shared-mutation",
    "timing.slow-test", "selection.unknown-input",
})

REQUIRED_ACTIONS = frozenset({
    "initialize", "choose-runner", "author-command",
    "review-existing-config",
})

RUN_PHASES = frozenset({
    "admission", "setup", "discovery", "execution", "finalization", "complete",
})

EXIT_ORIGINS = frozenset({"ptest", "setup", "runner", "signal"})

READINESS_AREAS = frozenset({"execution", "parallel", "selection", "timing"})
READINESS_STATES = frozenset({
    "ready-for-declared-capability", "blocked", "unknown",
})

CONTROL_KINDS = frozenset({
    "cancel", "parent-closing", "registered", "phase",
    "runner-facts", "draining",
})

LOCK_PATTERN = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
GROUP_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ATTEMPT_ID_PATTERN = re.compile(r"a(00[1-9]|010)")
HEX_LOWER = frozenset("0123456789abcdef")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_str(name: str, value: object, *, allow_empty: bool = False,
               max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be nonempty")
    if max_len is not None and len(value) > max_len:
        raise ValueError(f"{name} exceeds {max_len} characters")
    return value


def _check_int(name: str, value: object, *, lo: int | None = None,
               hi: int | None = None) -> int:
    if not _is_int(value):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if lo is not None and value < lo:
        raise ValueError(f"{name} {value} below minimum {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{name} {value} above maximum {hi}")
    return value


def _check_float(name: str, value: object, *, lo: float | None = None,
                 hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if lo is not None and result < lo:
        raise ValueError(f"{name} {result} below minimum {lo}")
    if hi is not None and result > hi:
        raise ValueError(f"{name} {result} above maximum {hi}")
    return result


def _check_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, got {type(value).__name__}")
    return value


def _check_enum(name: str, value: object, cls: type) -> Enum:
    if isinstance(value, cls):
        return value
    if isinstance(value, str):
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"{name} {value!r} is not a {cls.__name__}") from None
    raise TypeError(f"{name} must be {cls.__name__} or str")


def _check_tuple(name: str, value: object, *, elem=None) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be tuple, got {type(value).__name__}")
    items = tuple(value)
    if elem is not None:
        for item in items:
            elem(item)
    return items


def _check_path(name: str, value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    raise TypeError(f"{name} must be a nonempty path")


def _check_hex(name: str, value: object, length: int) -> str:
    _check_str(name, value)
    if len(value) != length or any(c not in HEX_LOWER for c in value):
        raise ValueError(f"{name} must be {length} lowercase hex characters")
    return value


def _check_reason_code(value: object) -> str:
    _check_str("reason.code", value)
    if value not in REASON_CODES:
        raise ValueError(f"unknown reason code {value!r}")
    return value


def _as_str_tuple(name: str, value: object) -> tuple:
    def _one(item: object) -> None:
        _check_str(f"{name}[]", item)
    return _check_tuple(name, value, elem=_one)


def _check_argv_tokens(name: str, value: object, *, allow_empty: bool) -> tuple:
    tokens = _check_tuple(name, value)
    if not allow_empty and not tokens:
        raise ValueError(f"{name} must be nonempty")
    if len(tokens) > 256:
        raise ValueError(f"{name} exceeds 256 tokens")
    total = 0
    for token in tokens:
        _check_str(f"{name}[]", token, allow_empty=True, max_len=16384)
        if "\x00" in token:
            raise ValueError(f"{name}[] must not contain NUL")
        total += len(token.encode("utf-8"))
    if total > 131072:
        raise ValueError(f"{name} exceeds 128 KiB total")
    return tokens


@dataclass(frozen=True, kw_only=True)
class Reason:
    code: str
    message: str
    paths: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _check_reason_code(self.code))
        object.__setattr__(self, "message", _check_str("reason.message", self.message, allow_empty=True))
        object.__setattr__(self, "paths", _as_str_tuple("reason.paths", self.paths))


@dataclass(frozen=True, kw_only=True)
class Capability:
    execution: ExecutionTier
    selection: bool
    lifecycle: str
    limitations: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution", _check_enum("capability.execution", self.execution, ExecutionTier))
        object.__setattr__(self, "selection", _check_bool("capability.selection", self.selection))
        if self.lifecycle != "cooperative-process-group":
            raise ValueError("capability.lifecycle must be cooperative-process-group")
        def _check_limitation(item: object) -> None:
            if not isinstance(item, Reason):
                raise TypeError("capability.limitations entries must be Reason")
        object.__setattr__(self, "limitations",
                           _check_tuple("capability.limitations", self.limitations,
                                        elem=_check_limitation))


@dataclass(frozen=True, kw_only=True)
class CommandSummary:
    kind: RunnerKind
    mode: Mode
    argument_count: int
    generated_options: tuple = ()
    workers: int | None = None
    provenance: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _check_enum("command.kind", self.kind, RunnerKind))
        object.__setattr__(self, "mode", _check_enum("command.mode", self.mode, Mode))
        object.__setattr__(self, "argument_count", _check_int("command.argument_count", self.argument_count, lo=0))
        for option in _as_str_tuple("command.generated_options", self.generated_options):
            if not re.fullmatch(r"[A-Za-z0-9_.+=:,-]{1,64}", option):
                raise ValueError(f"generated option {option!r} is not ptest-generated")
        if self.workers is not None:
            _check_int("command.workers", self.workers, lo=1, hi=64)
        object.__setattr__(self, "provenance", _as_str_tuple("command.provenance", self.provenance))


def summarize_command(kind, mode, argv, *, generated_options=(),
                      workers=None, provenance=()) -> CommandSummary:
    """Build a redacted command summary: only the count survives from argv."""
    if not isinstance(argv, (tuple, list)):
        raise TypeError("argv must be a sequence")
    for token in argv:
        if not isinstance(token, str):
            raise TypeError("argv tokens must be str")
    return CommandSummary(
        kind=_check_enum("kind", kind, RunnerKind),
        mode=_check_enum("mode", mode, Mode),
        argument_count=len(argv),
        generated_options=tuple(generated_options),
        workers=workers,
        provenance=tuple(provenance),
    )


@dataclass(frozen=True, kw_only=True)
class Group:
    name: str
    sources: tuple
    tests: tuple

    def __post_init__(self) -> None:
        _check_str("group.name", self.name, max_len=64)
        if not GROUP_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"group name {self.name!r} is not safe")
        object.__setattr__(self, "sources", _as_str_tuple("group.sources", self.sources))
        object.__setattr__(self, "tests", _as_str_tuple("group.tests", self.tests))
        if not self.sources or not self.tests:
            raise ValueError("group sources and tests must be nonempty")


@dataclass(frozen=True, kw_only=True)
class RunnerConfig:
    kind: RunnerKind
    launcher: tuple
    args: tuple = ()
    full_args: tuple = ()
    test_roots: tuple = ()
    workers: int = 1
    lifecycle: str = "cooperative-process-group"

    def __post_init__(self) -> None:
        kind = _check_enum("runner.kind", self.kind, RunnerKind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "launcher", _check_argv_tokens("runner.launcher", self.launcher, allow_empty=False))
        object.__setattr__(self, "args", _check_argv_tokens("runner.args", self.args, allow_empty=True))
        object.__setattr__(self, "full_args", _check_argv_tokens("runner.full_args", self.full_args, allow_empty=True))
        object.__setattr__(self, "test_roots", _as_str_tuple("runner.test_roots", self.test_roots))
        if kind in (RunnerKind.PYTEST, RunnerKind.VITEST, RunnerKind.GO, RunnerKind.CARGO):
            if not self.test_roots:
                raise ValueError("first-class profiles require nonempty test_roots")
        object.__setattr__(self, "workers", _check_int("runner.workers", self.workers, lo=1, hi=64))
        if self.lifecycle != "cooperative-process-group":
            raise ValueError("runner.lifecycle must be cooperative-process-group")


@dataclass(frozen=True, kw_only=True)
class SetupConfig:
    argv: tuple
    required_paths: tuple
    network: bool
    lifecycle_scripts: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _check_argv_tokens("setup.argv", self.argv, allow_empty=False))
        object.__setattr__(self, "required_paths", _as_str_tuple("setup.required_paths", self.required_paths))
        if not self.required_paths:
            raise ValueError("setup.required_paths must be nonempty")
        object.__setattr__(self, "network", _check_bool("setup.network", self.network))
        object.__setattr__(self, "lifecycle_scripts", _check_bool("setup.lifecycle_scripts", self.lifecycle_scripts))


@dataclass(frozen=True, kw_only=True)
class ResourceConfig:
    locks: tuple = ()
    memory_mb_per_worker: int = 0
    probe_isolation: str = "undeclared"

    def __post_init__(self) -> None:
        locks = _as_str_tuple("resources.locks", self.locks)
        if len(locks) > 32:
            raise ValueError("resources.locks exceeds 32 entries")
        if len(set(locks)) != len(locks):
            raise ValueError("resources.locks must be unique")
        for lock in locks:
            if not LOCK_PATTERN.fullmatch(lock):
                raise ValueError(f"lock name {lock!r} is not safe")
        object.__setattr__(self, "locks", locks)
        object.__setattr__(self, "memory_mb_per_worker",
                           _check_int("resources.memory_mb_per_worker", self.memory_mb_per_worker, lo=0, hi=1048576))
        if self.probe_isolation not in ("undeclared", "run-worker-namespaced"):
            raise ValueError("resources.probe_isolation must be undeclared or run-worker-namespaced")


@dataclass(frozen=True, kw_only=True)
class SelectionPolicy:
    enabled: bool
    closed_inputs: bool
    input_roots: tuple = ()
    ignored_inputs: tuple = ()
    environment: tuple = ()
    full_triggers: tuple = ()
    always: tuple = ()
    no_tests: tuple = ()
    non_input_outputs: tuple = ()
    full_ratio: float = 0.70
    groups: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _check_bool("selection.enabled", self.enabled))
        object.__setattr__(self, "closed_inputs", _check_bool("selection.closed_inputs", self.closed_inputs))
        for field in ("input_roots", "ignored_inputs", "environment", "full_triggers",
                      "always", "no_tests", "non_input_outputs"):
            object.__setattr__(self, field, _as_str_tuple(f"selection.{field}", getattr(self, field)))
        object.__setattr__(self, "full_ratio", _check_float("selection.full_ratio", self.full_ratio, lo=0.1, hi=1.0))
        object.__setattr__(self, "groups", _check_tuple("selection.groups", self.groups))
        for group in self.groups:
            if not isinstance(group, Group):
                raise TypeError("selection.groups entries must be Group")


@dataclass(frozen=True, kw_only=True)
class CheckoutIdentity:
    project_id: str
    checkout_id: str
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _check_hex("checkout.project_id", self.project_id, 32))
        object.__setattr__(self, "checkout_id", _check_hex("checkout.checkout_id", self.checkout_id, 32))
        object.__setattr__(self, "root", _check_path("checkout.root", self.root))


@dataclass(frozen=True, kw_only=True)
class Config:
    runner: RunnerConfig
    setup: SetupConfig | None
    resources: ResourceConfig
    selection: SelectionPolicy
    project_id: str
    checkout: CheckoutIdentity | None = None
    config_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.runner, RunnerConfig):
            raise TypeError("config.runner must be RunnerConfig")
        if self.setup is not None and not isinstance(self.setup, SetupConfig):
            raise TypeError("config.setup must be SetupConfig or None")
        if not isinstance(self.resources, ResourceConfig):
            raise TypeError("config.resources must be ResourceConfig")
        if not isinstance(self.selection, SelectionPolicy):
            raise TypeError("config.selection must be SelectionPolicy")
        object.__setattr__(self, "project_id", _check_hex("config.project_id", self.project_id, 32))
        if self.checkout is not None and not isinstance(self.checkout, CheckoutIdentity):
            raise TypeError("config.checkout must be CheckoutIdentity or None")
        if self.config_path is not None:
            object.__setattr__(self, "config_path", _check_path("config.config_path", self.config_path))


@dataclass(frozen=True, kw_only=True)
class ConfigSummary:
    project_id: str
    runner_kind: RunnerKind
    workers: int
    commands: tuple = ()
    setup_configured: bool = False
    setup_network: bool = False
    setup_lifecycle_scripts: bool = False
    selection_enabled: bool = False
    closed_inputs: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _check_hex("config_summary.project_id", self.project_id, 32))
        object.__setattr__(self, "runner_kind", _check_enum("config_summary.runner_kind", self.runner_kind, RunnerKind))
        object.__setattr__(self, "workers", _check_int("config_summary.workers", self.workers, lo=1, hi=64))
        items = _check_tuple("config_summary.commands", self.commands)
        if len(items) != 2:
            raise ValueError("config_summary.commands must hold exactly scoped then full summaries")
        for item in items:
            if not isinstance(item, CommandSummary):
                raise TypeError("config_summary.commands entries must be CommandSummary")
        scoped, full = items
        if scoped.mode != Mode.SCOPED or full.mode != Mode.FULL:
            raise ValueError("config_summary.commands must be scoped then full")
        if scoped.kind is not self.runner_kind or full.kind is not self.runner_kind:
            raise ValueError("config_summary.commands must match runner_kind")
        object.__setattr__(self, "commands", items)
        for field in ("setup_configured", "setup_network",
                      "setup_lifecycle_scripts", "selection_enabled",
                      "closed_inputs"):
            object.__setattr__(self, field, _check_bool(f"config_summary.{field}", getattr(self, field)))


@dataclass(frozen=True, kw_only=True)
class EffectiveLimits:
    max_slots: int | None = None
    max_jobs: int | None = None
    memory_mb: int | None = None
    repo_workers: int | None = None

    def __post_init__(self) -> None:
        if self.max_slots is not None:
            _check_int("limits.max_slots", self.max_slots, lo=1, hi=64)
        if self.max_jobs is not None:
            _check_int("limits.max_jobs", self.max_jobs, lo=1, hi=64)
        if (self.max_slots is None) != (self.max_jobs is None):
            raise ValueError("limits.max_slots/max_jobs are both known or both null")
        if (self.max_slots is not None and self.max_jobs is not None
                and self.max_jobs > self.max_slots):
            raise ValueError("limits.max_jobs must not exceed max_slots")
        if self.memory_mb is not None:
            _check_int("limits.memory_mb", self.memory_mb, lo=64, hi=1048576)
        if self.repo_workers is not None:
            _check_int("limits.repo_workers", self.repo_workers, lo=1, hi=64)


def summarize_config(config: Config, *, scoped: CommandSummary,
                     full: CommandSummary) -> ConfigSummary:
    """Build the allowlisted public config summary; raw argv/env never cross."""
    if not isinstance(config, Config):
        raise TypeError("summarize_config requires Config")
    setup = config.setup
    return ConfigSummary(
        project_id=config.project_id,
        runner_kind=config.runner.kind,
        workers=config.runner.workers,
        commands=(scoped, full),
        setup_configured=setup is not None,
        setup_network=bool(setup is not None and setup.network),
        setup_lifecycle_scripts=bool(
            setup is not None and setup.lifecycle_scripts),
        selection_enabled=config.selection.enabled,
        closed_inputs=config.selection.closed_inputs,
    )


@dataclass(frozen=True, kw_only=True)
class DomainPaths:
    root: Path
    machine_config: Path
    ledger: Path
    marker: Path
    fixture: bool
    domain_id: str | None

    def __post_init__(self) -> None:
        for field in ("root", "machine_config", "ledger", "marker"):
            object.__setattr__(self, field, _check_path(f"domain.{field}", getattr(self, field)))
        object.__setattr__(self, "fixture", _check_bool("domain.fixture", self.fixture))
        if self.domain_id is not None:
            object.__setattr__(self, "domain_id", _check_hex("domain.domain_id", self.domain_id, 32))


@dataclass(frozen=True, kw_only=True)
class ProcessIdentity:
    pid: int
    birth: float
    uid: int
    pgid: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pid", _check_int("process.pid", self.pid, lo=1))
        object.__setattr__(self, "birth", _check_float("process.birth", self.birth, lo=0))
        object.__setattr__(self, "uid", _check_int("process.uid", self.uid, lo=0))
        object.__setattr__(self, "pgid", _check_int("process.pgid", self.pgid, lo=1))


@dataclass(frozen=True, kw_only=True)
class GroupObservation:
    exists: bool | None
    permission: bool
    checked_at: float

    def __post_init__(self) -> None:
        if self.exists is not None:
            object.__setattr__(self, "exists", _check_bool("group.exists", self.exists))
        object.__setattr__(self, "permission", _check_bool("group.permission", self.permission))
        object.__setattr__(self, "checked_at", _check_float("group.checked_at", self.checked_at, lo=0))


@dataclass(frozen=True, kw_only=True)
class ScanLimits:
    entries: int
    files: int
    file_bytes: int
    total_bytes: int
    findings: int
    output_bytes: int
    elapsed_s: float
    depth: int
    ast_nodes: int

    def __post_init__(self) -> None:
        for field in ("entries", "files", "file_bytes", "total_bytes",
                      "findings", "output_bytes", "depth", "ast_nodes"):
            _check_int(f"scan.{field}", getattr(self, field), lo=0)
        _check_float("scan.elapsed_s", self.elapsed_s, lo=0)


@dataclass(frozen=True, kw_only=True)
class ScanUsage:
    entries: int
    files: int
    file_bytes: int
    total_bytes: int
    findings: int
    output_bytes: int
    elapsed_s: float
    skipped: int
    truncated: bool

    def __post_init__(self) -> None:
        for field in ("entries", "files", "file_bytes", "total_bytes",
                      "findings", "output_bytes", "skipped"):
            _check_int(f"usage.{field}", getattr(self, field), lo=0)
        _check_float("usage.elapsed_s", self.elapsed_s, lo=0)
        _check_bool("usage.truncated", self.truncated)


DEFAULT_SCAN_LIMITS = ScanLimits(
    entries=20000, files=2000, file_bytes=262144, total_bytes=16777216,
    findings=200, output_bytes=262144, elapsed_s=8.0, depth=256,
    ast_nodes=50000,
)

MAX_SCAN_LIMITS = ScanLimits(
    entries=100000, files=10000, file_bytes=1048576, total_bytes=67108864,
    findings=200, output_bytes=262144, elapsed_s=8.0, depth=256,
    ast_nodes=50000,
)


@dataclass(frozen=True, kw_only=True)
class ProbeOptions:
    scope: str
    repeat: int = 2
    workers: int = 2
    attempt_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        _check_str("probe.scope", self.scope)
        _check_int("probe.repeat", self.repeat, lo=1, hi=5)
        _check_int("probe.workers", self.workers, lo=1, hi=64)
        _check_float("probe.attempt_timeout_s", self.attempt_timeout_s, lo=1, hi=120)


@dataclass(frozen=True, kw_only=True)
class Change:
    old: str | None
    new: str | None
    kind: str

    def __post_init__(self) -> None:
        if self.old is not None:
            _check_str("change.old", self.old, allow_empty=True)
        if self.new is not None:
            _check_str("change.new", self.new, allow_empty=True)
        _check_str("change.kind", self.kind)


@dataclass(frozen=True, kw_only=True)
class FileFingerprint:
    path: str
    digest: str
    mode: int
    size: int

    def __post_init__(self) -> None:
        _check_str("fingerprint.path", self.path)
        _check_hex("fingerprint.digest", self.digest, 64)
        _check_int("fingerprint.mode", self.mode, lo=0)
        _check_int("fingerprint.size", self.size, lo=0)


@dataclass(frozen=True, kw_only=True)
class InputSnapshot:
    digest: str | None
    compatibility: str | None
    head: str | None
    clean: bool
    changes: tuple = ()
    limitations: tuple = ()
    files: tuple = ()

    def __post_init__(self) -> None:
        if self.digest is not None:
            _check_hex("snapshot.digest", self.digest, 64)
        if self.compatibility is not None:
            _check_str("snapshot.compatibility", self.compatibility, allow_empty=True)
        if self.head is not None:
            _check_str("snapshot.head", self.head)
        object.__setattr__(self, "clean", _check_bool("snapshot.clean", self.clean))
        for field, cls in (("changes", Change), ("limitations", Reason),
                           ("files", FileFingerprint)):
            items = _check_tuple(f"snapshot.{field}", getattr(self, field))
            for item in items:
                if not isinstance(item, cls):
                    raise TypeError(f"snapshot.{field} entries must be {cls.__name__}")
            object.__setattr__(self, field, items)


@dataclass(frozen=True, kw_only=True)
class TestRecord:
    id: str
    file: str
    outcome: Outcome
    setup_s: float | None = None
    call_s: float | None = None
    teardown_s: float | None = None

    def __post_init__(self) -> None:
        _check_str("test.id", self.id, max_len=4096)
        _check_str("test.file", self.file)
        object.__setattr__(self, "outcome", _check_enum("test.outcome", self.outcome, Outcome))
        for field in ("setup_s", "call_s", "teardown_s"):
            value = getattr(self, field)
            if value is not None:
                _check_float(f"test.{field}", value, lo=0)


@dataclass(frozen=True, kw_only=True)
class Inventory:
    adapter: str
    version: str
    complete: bool
    tests: tuple = ()
    digest: str = ""

    def __post_init__(self) -> None:
        _check_str("inventory.adapter", self.adapter)
        _check_str("inventory.version", self.version)
        object.__setattr__(self, "complete", _check_bool("inventory.complete", self.complete))
        items = _check_tuple("inventory.tests", self.tests)
        for item in items:
            if not isinstance(item, TestRecord):
                raise TypeError("inventory.tests entries must be TestRecord")
        object.__setattr__(self, "tests", items)
        _check_hex("inventory.digest", self.digest, 64)


@dataclass(frozen=True, kw_only=True)
class Baseline:
    run_id: str
    head: str
    input_digest: str
    compatibility: str
    inventory: Inventory
    policy_digest: str
    created_at: str

    def __post_init__(self) -> None:
        _check_hex("baseline.run_id", self.run_id, 32)
        _check_str("baseline.head", self.head)
        _check_hex("baseline.input_digest", self.input_digest, 64)
        _check_str("baseline.compatibility", self.compatibility, allow_empty=True)
        if not isinstance(self.inventory, Inventory):
            raise TypeError("baseline.inventory must be Inventory")
        _check_hex("baseline.policy_digest", self.policy_digest, 64)
        _check_str("baseline.created_at", self.created_at)


@dataclass(frozen=True, kw_only=True)
class Obligation:
    file: str | None
    test_id: str | None
    sequence: int
    source_digest: str | None
    compatibility: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.file is not None:
            _check_str("obligation.file", self.file)
        if self.test_id is not None:
            _check_str("obligation.test_id", self.test_id)
        _check_int("obligation.sequence", self.sequence, lo=0)
        if self.source_digest is not None:
            _check_hex("obligation.source_digest", self.source_digest, 64)
        if self.compatibility is not None:
            _check_str("obligation.compatibility", self.compatibility, allow_empty=True)
        _check_str("obligation.reason", self.reason)


@dataclass(frozen=True, kw_only=True)
class HistoryView:
    baseline: Baseline | None
    obligations: tuple = ()
    selection_disabled: bool = False
    limitations: tuple = ()

    def __post_init__(self) -> None:
        if self.baseline is not None and not isinstance(self.baseline, Baseline):
            raise TypeError("history.baseline must be Baseline or None")
        items = _check_tuple("history.obligations", self.obligations)
        for item in items:
            if not isinstance(item, Obligation):
                raise TypeError("history.obligations entries must be Obligation")
        object.__setattr__(self, "obligations", items)
        object.__setattr__(self, "selection_disabled",
                           _check_bool("history.selection_disabled", self.selection_disabled))
        items = _check_tuple("history.limitations", self.limitations)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("history.limitations entries must be Reason")
        object.__setattr__(self, "limitations", items)


@dataclass(frozen=True, kw_only=True)
class Plan:
    mode: Mode
    execution: str
    files: tuple = ()
    reasons: tuple = ()
    input_digest: str | None = None
    compatibility: str | None = None
    baseline_run_id: str | None = None
    static_preview: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _check_enum("plan.mode", self.mode, Mode))
        if self.execution not in ("full", "selected", "scoped", "none"):
            raise ValueError("plan.execution must be full, selected, scoped or none")
        object.__setattr__(self, "files", _as_str_tuple("plan.files", self.files))
        items = _check_tuple("plan.reasons", self.reasons)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("plan.reasons entries must be Reason")
        object.__setattr__(self, "reasons", items)
        if self.input_digest is not None:
            _check_hex("plan.input_digest", self.input_digest, 64)
        if self.compatibility is not None:
            _check_str("plan.compatibility", self.compatibility, allow_empty=True)
        if self.baseline_run_id is not None:
            _check_hex("plan.baseline_run_id", self.baseline_run_id, 32)
        object.__setattr__(self, "static_preview", _check_bool("plan.static_preview", self.static_preview))


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    mode: Mode
    argv: tuple = ()
    base: str | None = None
    workers: int | None = None
    queue_timeout_s: float = 1800.0
    no_setup: bool = False
    shadow: bool = False
    result_path: str | None = None
    fixture_domain: Path | None = None
    probe: ProbeOptions | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _check_enum("request.mode", self.mode, Mode))
        object.__setattr__(self, "argv", _check_argv_tokens("request.argv", self.argv, allow_empty=True))
        if self.base is not None:
            _check_str("request.base", self.base)
        if self.workers is not None:
            _check_int("request.workers", self.workers, lo=1, hi=64)
        object.__setattr__(self, "queue_timeout_s",
                           _check_float("request.queue_timeout_s", self.queue_timeout_s, lo=1, hi=86400))
        object.__setattr__(self, "no_setup", _check_bool("request.no_setup", self.no_setup))
        object.__setattr__(self, "shadow", _check_bool("request.shadow", self.shadow))
        if self.result_path is not None:
            _check_str("request.result_path", self.result_path)
        if self.fixture_domain is not None:
            object.__setattr__(self, "fixture_domain", _check_path("request.fixture_domain", self.fixture_domain))
        if self.probe is not None and not isinstance(self.probe, ProbeOptions):
            raise TypeError("request.probe must be ProbeOptions or None")


@dataclass(frozen=True, kw_only=True)
class AdmissionRequest:
    run_id: str
    checkout: CheckoutIdentity
    owner: ProcessIdentity
    slots: int
    exclusive: bool
    locks: tuple = ()
    memory_mb: int | None = None
    deadline: float = 0.0
    fixture: bool = False

    def __post_init__(self) -> None:
        _check_hex("admission.run_id", self.run_id, 32)
        if not isinstance(self.checkout, CheckoutIdentity):
            raise TypeError("admission.checkout must be CheckoutIdentity")
        if not isinstance(self.owner, ProcessIdentity):
            raise TypeError("admission.owner must be ProcessIdentity")
        _check_int("admission.slots", self.slots, lo=1, hi=64)
        object.__setattr__(self, "exclusive", _check_bool("admission.exclusive", self.exclusive))
        locks = _as_str_tuple("admission.locks", self.locks)
        for lock in locks:
            if not LOCK_PATTERN.fullmatch(lock):
                raise ValueError(f"lock name {lock!r} is not safe")
        object.__setattr__(self, "locks", locks)
        if self.memory_mb is not None:
            if not _is_int(self.memory_mb) or self.memory_mb <= 0:
                raise ValueError("admission.memory_mb must be a positive per-worker estimate or None")
        object.__setattr__(self, "deadline", _check_float("admission.deadline", self.deadline, lo=0))
        object.__setattr__(self, "fixture", _check_bool("admission.fixture", self.fixture))


@dataclass(frozen=True, kw_only=True)
class Ticket:
    run_id: str
    sequence: int

    def __post_init__(self) -> None:
        _check_hex("ticket.run_id", self.run_id, 32)
        _check_int("ticket.sequence", self.sequence, lo=0)


@dataclass(frozen=True, kw_only=True)
class Grant:
    run_id: str
    nonce: str
    slots: int
    memory_estimate_mb: int | None
    reserved_memory_mb: int | None
    generation: int
    domain_id: str

    def __post_init__(self) -> None:
        _check_hex("grant.run_id", self.run_id, 32)
        _check_hex("grant.nonce", self.nonce, 64)
        _check_int("grant.slots", self.slots, lo=1, hi=64)
        for field in ("memory_estimate_mb", "reserved_memory_mb"):
            value = getattr(self, field)
            if value is not None:
                _check_int(f"grant.{field}", value, lo=0)
        _check_int("grant.generation", self.generation, lo=0)
        _check_hex("grant.domain_id", self.domain_id, 32)


@dataclass(frozen=True, kw_only=True)
class AttemptIdentity:
    run_id: str
    attempt_id: str
    resource_prefix: str
    worker_count: int

    def __post_init__(self) -> None:
        _check_hex("attempt.run_id", self.run_id, 32)
        _check_str("attempt.attempt_id", self.attempt_id)
        if not ATTEMPT_ID_PATTERN.fullmatch(self.attempt_id):
            raise ValueError("attempt.attempt_id must match a001-a010")
        _check_str("attempt.resource_prefix", self.resource_prefix, max_len=54)
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.resource_prefix):
            raise ValueError("attempt.resource_prefix must be [A-Za-z0-9_]+ within 54 ASCII")
        _check_int("attempt.worker_count", self.worker_count, lo=1, hi=64)


@dataclass(frozen=True, kw_only=True)
class PreparedRun:
    argv: tuple
    cwd: Path
    env_updates: tuple = ()
    report_path: Path | None = None
    capability: Capability | None = None
    summary: CommandSummary | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", _check_argv_tokens("prepared.argv", self.argv, allow_empty=False))
        object.__setattr__(self, "cwd", _check_path("prepared.cwd", self.cwd))
        updates = _check_tuple("prepared.env_updates", self.env_updates)
        for update in updates:
            if not isinstance(update, (tuple, list)) or len(update) != 2:
                raise TypeError("prepared.env_updates entries must be (name, value) pairs")
            _check_str("prepared.env_updates[].name", update[0])
            _check_str("prepared.env_updates[].value", update[1], allow_empty=True)
        object.__setattr__(self, "env_updates", tuple((k, v) for k, v in updates))
        if self.report_path is not None:
            object.__setattr__(self, "report_path", _check_path("prepared.report_path", self.report_path))
        if self.capability is not None and not isinstance(self.capability, Capability):
            raise TypeError("prepared.capability must be Capability or None")
        if self.summary is not None and not isinstance(self.summary, CommandSummary):
            raise TypeError("prepared.summary must be CommandSummary or None")


@dataclass(frozen=True, kw_only=True)
class Counts:
    collected: int | None = None
    executed: int | None = None
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    unknown: int | None = None

    def __post_init__(self) -> None:
        for field in ("collected", "executed", "passed", "failed", "skipped", "unknown"):
            value = getattr(self, field)
            if value is not None:
                _check_int(f"counts.{field}", value, lo=0)


@dataclass(frozen=True, kw_only=True)
class Timings:
    queue_s: float | None = None
    setup_s: float | None = None
    collection_s: float | None = None
    execution_s: float | None = None
    finalization_s: float | None = None

    def __post_init__(self) -> None:
        for field in ("queue_s", "setup_s", "collection_s", "execution_s", "finalization_s"):
            value = getattr(self, field)
            if value is not None:
                _check_float(f"timings.{field}", value, lo=0)


@dataclass(frozen=True, kw_only=True)
class AttemptResult:
    attempt_id: str
    phase: str
    status: Status
    raw_exit_code: int | None = None
    final_exit_code: int | None = None
    source_valid: bool = True
    inventory_complete: bool = False
    timings: Timings | None = None

    def __post_init__(self) -> None:
        _check_str("attempt.attempt_id", self.attempt_id)
        if not ATTEMPT_ID_PATTERN.fullmatch(self.attempt_id):
            raise ValueError("attempt.attempt_id must match a001-a010")
        if self.phase not in RUN_PHASES:
            raise ValueError(f"attempt.phase {self.phase!r} is not a run phase")
        object.__setattr__(self, "status", _check_enum("attempt.status", self.status, Status))
        for field in ("raw_exit_code", "final_exit_code"):
            value = getattr(self, field)
            if value is not None and not _is_int(value):
                raise TypeError(f"attempt.{field} must be int or None")
        object.__setattr__(self, "source_valid", _check_bool("attempt.source_valid", self.source_valid))
        object.__setattr__(self, "inventory_complete",
                           _check_bool("attempt.inventory_complete", self.inventory_complete))
        if self.timings is not None and not isinstance(self.timings, Timings):
            raise TypeError("attempt.timings must be Timings or None")


@dataclass(frozen=True, kw_only=True)
class RunResult:
    run_id: str
    project_id: str
    checkout_id: str
    mode: Mode
    status: Status
    phase: str
    started_at: str
    finished_at: str
    plan: Plan
    command: CommandSummary
    granted_workers: int | None = None
    memory_estimate_mb: int | None = None
    reserved_memory_mb: int | None = None
    runner_exit_code: int | None = None
    exit_code: int = 0
    exit_origin: str = "runner"
    signal: int | None = None
    source_valid: bool = True
    full_gate_eligible: bool = False
    baseline_published: bool = False
    counts: Counts | None = None
    timings: Timings | None = None
    attempts: tuple = ()
    reasons: tuple = ()
    limitations: tuple = ()
    artifact_id: str | None = None
    sequence: int = 0
    input_before: InputSnapshot | None = None
    input_after: InputSnapshot | None = None
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        _check_hex("result.run_id", self.run_id, 32)
        _check_hex("result.project_id", self.project_id, 32)
        _check_hex("result.checkout_id", self.checkout_id, 32)
        object.__setattr__(self, "mode", _check_enum("result.mode", self.mode, Mode))
        object.__setattr__(self, "status", _check_enum("result.status", self.status, Status))
        if self.phase not in RUN_PHASES:
            raise ValueError(f"result.phase {self.phase!r} is not a run phase")
        _check_str("result.started_at", self.started_at)
        _check_str("result.finished_at", self.finished_at)
        if not isinstance(self.plan, Plan):
            raise TypeError("result.plan must be Plan")
        if not isinstance(self.command, CommandSummary):
            raise TypeError("result.command must be CommandSummary")
        if self.granted_workers is not None:
            _check_int("result.granted_workers", self.granted_workers, lo=1, hi=64)
        for field in ("memory_estimate_mb", "reserved_memory_mb"):
            value = getattr(self, field)
            if value is not None:
                _check_int(f"result.{field}", value, lo=0)
        for field in ("runner_exit_code", "signal"):
            value = getattr(self, field)
            if value is not None and not _is_int(value):
                raise TypeError(f"result.{field} must be int or None")
        _check_int("result.exit_code", self.exit_code)
        if self.exit_origin not in EXIT_ORIGINS:
            raise ValueError("result.exit_origin must be ptest, setup, runner or signal")
        object.__setattr__(self, "source_valid", _check_bool("result.source_valid", self.source_valid))
        object.__setattr__(self, "full_gate_eligible",
                           _check_bool("result.full_gate_eligible", self.full_gate_eligible))
        object.__setattr__(self, "baseline_published",
                           _check_bool("result.baseline_published", self.baseline_published))
        if self.counts is not None and not isinstance(self.counts, Counts):
            raise TypeError("result.counts must be Counts or None")
        if self.timings is not None and not isinstance(self.timings, Timings):
            raise TypeError("result.timings must be Timings or None")
        items = _check_tuple("result.attempts", self.attempts)
        for item in items:
            if not isinstance(item, AttemptResult):
                raise TypeError("result.attempts entries must be AttemptResult")
        object.__setattr__(self, "attempts", items)
        for field in ("reasons", "limitations"):
            items = _check_tuple(f"result.{field}", getattr(self, field))
            for item in items:
                if not isinstance(item, Reason):
                    raise TypeError(f"result.{field} entries must be Reason")
            object.__setattr__(self, field, items)
        if self.artifact_id is not None:
            _check_str("result.artifact_id", self.artifact_id)
        _check_int("result.sequence", self.sequence, lo=0)
        for field in ("input_before", "input_after"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, InputSnapshot):
                raise TypeError(f"result.{field} must be InputSnapshot or None")
        if self.policy_digest is not None:
            _check_hex("result.policy_digest", self.policy_digest, 64)


def serialize_run_result(result: RunResult) -> dict:
    """Explicit allowlisted public run payload; internal fields never leak."""
    if not isinstance(result, RunResult):
        raise TypeError("serialize_run_result requires RunResult")
    command = result.command
    plan = result.plan
    return {
        "run_id": result.run_id,
        "project_id": result.project_id,
        "checkout_id": result.checkout_id,
        "mode": result.mode.value if isinstance(result.mode, Mode) else result.mode,
        "status": result.status.value,
        "phase": result.phase,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "plan": _plan_dict(plan),
        "command": _command_dict(command),
        "granted_workers": result.granted_workers,
        "memory_estimate_mb": result.memory_estimate_mb,
        "reserved_memory_mb": result.reserved_memory_mb,
        "runner_exit_code": result.runner_exit_code,
        "exit_code": result.exit_code,
        "exit_origin": result.exit_origin,
        "signal": result.signal,
        "source_valid": result.source_valid,
        "full_gate_eligible": result.full_gate_eligible,
        "baseline_published": result.baseline_published,
        "counts": _counts_dict(result.counts),
        "timings": _timings_dict(result.timings),
        "attempts": [_attempt_dict(item) for item in result.attempts],
        "reasons": [_reason_dict(item) for item in result.reasons],
        "limitations": [_reason_dict(item) for item in result.limitations],
        "artifact_id": result.artifact_id,
    }


def _reason_dict(reason: Reason) -> dict:
    return {
        "code": reason.code,
        "message": reason.message,
        "paths": list(reason.paths),
    }


def _command_dict(command: CommandSummary) -> dict:
    """Shared CommandSummary converter: serializer and fixtures use this."""
    return {
        "kind": command.kind.value,
        "mode": command.mode.value,
        "argument_count": command.argument_count,
        "generated_options": list(command.generated_options),
        "workers": command.workers,
        "provenance": list(command.provenance),
    }


def _plan_dict(plan: Plan) -> dict:
    """Shared Plan converter: serializer and fixtures use this."""
    return {
        "mode": plan.mode.value,
        "execution": plan.execution,
        "files": list(plan.files),
        "reasons": [_reason_dict(item) for item in plan.reasons],
        "input_digest": plan.input_digest,
        "compatibility": plan.compatibility,
        "baseline_run_id": plan.baseline_run_id,
        "static_preview": plan.static_preview,
    }


def _capability_dict(capability: Capability | None) -> dict | None:
    """Shared Capability converter: fixtures and manifest codec use this."""
    if capability is None:
        return None
    return {
        "execution": capability.execution.value,
        "selection": capability.selection,
        "lifecycle": capability.lifecycle,
        "limitations": [_reason_dict(item) for item in capability.limitations],
    }


def _counts_dict(counts: Counts | None) -> dict | None:
    """Shared Counts converter: serializer and fixtures use this."""
    if counts is None:
        return None
    return {
        "collected": counts.collected,
        "executed": counts.executed,
        "passed": counts.passed,
        "failed": counts.failed,
        "skipped": counts.skipped,
        "unknown": counts.unknown,
    }


def _timings_dict(timings: Timings | None) -> dict | None:
    """Shared Timings converter: serializer and fixtures use this."""
    if timings is None:
        return None
    return {
        "queue": timings.queue_s,
        "setup": timings.setup_s,
        "collection": timings.collection_s,
        "execution": timings.execution_s,
        "finalization": timings.finalization_s,
    }


def _attempt_dict(item: AttemptResult) -> dict:
    """Shared attempt-record converter: serializer and fixtures use this."""
    return {
        "attempt_id": item.attempt_id,
        "phase": item.phase,
        "status": item.status.value,
        "raw_exit_code": item.raw_exit_code,
        "final_exit_code": item.final_exit_code,
        "source_valid": item.source_valid,
        "inventory_complete": item.inventory_complete,
        "timings": _timings_dict(item.timings),
    }


def _readiness_dict(item: Readiness) -> dict:
    """Shared Readiness converter for fixtures."""
    return {
        "area": item.area,
        "state": item.state,
        "reasons": [_reason_dict(entry) for entry in item.reasons],
    }


def _finding_dict(item: Finding) -> dict:
    """Shared Finding converter for fixtures."""
    return {
        "code": item.code,
        "severity": item.severity,
        "confidence": item.confidence,
        "path": item.path,
        "line": item.line,
        "evidence_type": item.evidence_type,
        "consequence": item.consequence,
        "remediation": item.remediation,
        "verification": item.verification,
    }


def _lease_dict(item: LeaseView) -> dict:
    """Shared LeaseView converter for fixtures."""
    return {
        "run_id": item.run_id,
        "checkout_id": item.checkout_id,
        "state": item.state.value,
        "sequence": item.sequence,
        "requested_slots": item.requested_slots,
        "slots": item.slots,
        "memory_estimate_mb": item.memory_estimate_mb,
        "reserved_memory_mb": item.reserved_memory_mb,
        "phase": item.phase,
        "age_s": item.age_s,
        "queue_wait_s": item.queue_wait_s,
        "ownership": item.ownership,
        "fixture": item.fixture,
        "reasons": [_reason_dict(entry) for entry in item.reasons],
    }


def _obligation_dict(item: Obligation) -> dict:
    """Shared Obligation converter for fixtures."""
    return {
        "file": item.file,
        "test_id": item.test_id,
        "sequence": item.sequence,
        "source_digest": item.source_digest,
        "compatibility": item.compatibility,
        "reason": item.reason,
    }


def _scan_limits_dict(limits: ScanLimits) -> dict:
    """Shared ScanLimits converter for fixtures."""
    return {
        "entries": limits.entries,
        "files": limits.files,
        "file_bytes": limits.file_bytes,
        "total_bytes": limits.total_bytes,
        "findings": limits.findings,
        "output_bytes": limits.output_bytes,
        "elapsed_s": limits.elapsed_s,
        "depth": limits.depth,
        "ast_nodes": limits.ast_nodes,
    }


def _scan_usage_dict(usage: ScanUsage) -> dict:
    """Shared ScanUsage converter for fixtures."""
    return {
        "entries": usage.entries,
        "files": usage.files,
        "file_bytes": usage.file_bytes,
        "total_bytes": usage.total_bytes,
        "findings": usage.findings,
        "output_bytes": usage.output_bytes,
        "elapsed_s": usage.elapsed_s,
        "skipped": usage.skipped,
        "truncated": usage.truncated,
    }


def _config_summary_dict(summary: ConfigSummary | None) -> dict | None:
    """Shared ConfigSummary converter: serializer and fixtures use this."""
    if summary is None:
        return None
    return {
        "project_id": summary.project_id,
        "runner_kind": summary.runner_kind.value,
        "workers": summary.workers,
        "commands": [_command_dict(item) for item in summary.commands],
        "setup_configured": summary.setup_configured,
        "setup_network": summary.setup_network,
        "setup_lifecycle_scripts": summary.setup_lifecycle_scripts,
        "selection_enabled": summary.selection_enabled,
        "closed_inputs": summary.closed_inputs,
    }


def _effective_limits_dict(limits: EffectiveLimits) -> dict:
    """Shared EffectiveLimits converter: serializer and fixtures use this."""
    if not isinstance(limits, EffectiveLimits):
        raise TypeError("_effective_limits_dict requires EffectiveLimits")
    return {
        "max_slots": limits.max_slots,
        "max_jobs": limits.max_jobs,
        "memory_mb": limits.memory_mb,
        "repo_workers": limits.repo_workers,
    }


@dataclass(frozen=True, kw_only=True)
class Readiness:
    area: str
    state: str
    reasons: tuple = ()

    def __post_init__(self) -> None:
        if self.area not in READINESS_AREAS:
            raise ValueError("readiness.area must be execution, parallel, selection or timing")
        if self.state not in READINESS_STATES:
            raise ValueError("readiness.state must be a closed readiness state")
        items = _check_tuple("readiness.reasons", self.reasons)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("readiness.reasons entries must be Reason")
        object.__setattr__(self, "reasons", items)


@dataclass(frozen=True, kw_only=True)
class Finding:
    code: str
    severity: str
    confidence: str
    path: str | None
    line: int | None
    evidence_type: str
    consequence: str
    remediation: str
    verification: str

    def __post_init__(self) -> None:
        _check_str("finding.code", self.code)
        if self.code not in FINDING_CODES:
            raise ValueError(f"unknown finding code {self.code!r}")
        if self.severity not in ("low", "medium", "high"):
            raise ValueError("finding.severity must be low, medium or high")
        if self.confidence not in ("low", "medium", "high"):
            raise ValueError("finding.confidence must be low, medium or high")
        if self.path is not None:
            _check_str("finding.path", self.path)
        if self.line is not None:
            _check_int("finding.line", self.line, lo=1)
        for field in ("evidence_type", "consequence", "remediation", "verification"):
            _check_str(f"finding.{field}", getattr(self, field), max_len=4096)


@dataclass(frozen=True, kw_only=True)
class DoctorReport:
    scope: tuple = ()
    readiness: tuple = ()
    findings: tuple = ()
    limits: ScanLimits | None = None
    usage: ScanUsage | None = None
    limitations: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _as_str_tuple("doctor.scope", self.scope))
        items = _check_tuple("doctor.readiness", self.readiness)
        for item in items:
            if not isinstance(item, Readiness):
                raise TypeError("doctor.readiness entries must be Readiness")
        object.__setattr__(self, "readiness", items)
        items = _check_tuple("doctor.findings", self.findings)
        for item in items:
            if not isinstance(item, Finding):
                raise TypeError("doctor.findings entries must be Finding")
        object.__setattr__(self, "findings", items)
        if self.limits is not None and not isinstance(self.limits, ScanLimits):
            raise TypeError("doctor.limits must be ScanLimits or None")
        if self.usage is not None and not isinstance(self.usage, ScanUsage):
            raise TypeError("doctor.usage must be ScanUsage or None")
        items = _check_tuple("doctor.limitations", self.limitations)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("doctor.limitations entries must be Reason")
        object.__setattr__(self, "limitations", items)


@dataclass(frozen=True, kw_only=True)
class InitOptions:
    runner: RunnerKind | None
    dry_run: bool
    reveal_command: bool

    def __post_init__(self) -> None:
        if self.runner is not None:
            object.__setattr__(self, "runner", _check_enum("init.runner", self.runner, RunnerKind))
        for field in ("dry_run", "reveal_command"):
            object.__setattr__(self, field, _check_bool(f"init.{field}", getattr(self, field)))


@dataclass(frozen=True, kw_only=True)
class InitResult:
    action: InitAction
    target: Path
    exists: bool
    config: ConfigSummary | None
    warnings: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _check_enum("init.action", self.action, InitAction))
        object.__setattr__(self, "target", _check_path("init.target", self.target))
        object.__setattr__(self, "exists", _check_bool("init.exists", self.exists))
        if (self.action is InitAction.PREVIEW) == self.exists:
            raise ValueError(
                "init.action/exists mismatch: preview needs exists=false, "
                "created/existing need exists=true")
        if self.config is not None and not isinstance(self.config, ConfigSummary):
            raise TypeError("init.config must be ConfigSummary or None")
        items = _check_tuple("init.warnings", self.warnings)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("init.warnings entries must be Reason")
        object.__setattr__(self, "warnings", items)


def select_init_action(*, target_exists: bool, dry_run: bool,
                       created: bool) -> tuple:
    """Frozen init precedence: existing wins even dry-run, then creation, then preview."""
    target_exists = _check_bool("init.target_exists", target_exists)
    dry_run = _check_bool("init.dry_run", dry_run)
    created = _check_bool("init.created", created)
    if target_exists:
        return (InitAction.EXISTING, True)
    if created:
        return (InitAction.CREATED, True)
    if dry_run:
        return (InitAction.PREVIEW, False)
    raise ValueError(
        "absent target without dry-run or creation uses the error "
        "envelope, not a success action")


def serialize_init_result(result: InitResult) -> dict:
    """Explicit allowlisted public init payload; Config never serialized raw."""
    if not isinstance(result, InitResult):
        raise TypeError("serialize_init_result requires InitResult")
    return {
        "action": result.action.value,
        "target": str(result.target),
        "exists": result.exists,
        "warnings": [_reason_dict(item) for item in result.warnings],
        "config": _config_summary_dict(result.config),
    }


@dataclass(frozen=True, kw_only=True)
class ConfigResolution:
    root: Path
    path: Path | None
    config: Config | None
    provenance: tuple = ()
    warnings: tuple = ()
    problem: Problem | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _check_path("resolution.root", self.root))
        if self.path is not None:
            object.__setattr__(self, "path", _check_path("resolution.path", self.path))
        if self.config is not None and not isinstance(self.config, Config):
            raise TypeError("resolution.config must be Config or None")
        object.__setattr__(self, "provenance", _as_str_tuple("resolution.provenance", self.provenance))
        items = _check_tuple("resolution.warnings", self.warnings)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("resolution.warnings entries must be Reason")
        object.__setattr__(self, "warnings", items)
        if self.problem is not None and not isinstance(self.problem, Problem):
            raise TypeError("resolution.problem must be Problem or None")


@dataclass(frozen=True, kw_only=True)
class RegisterPreview:
    root: str
    initialized: bool
    proposed_runner: RunnerKind | None
    commands: tuple = ()
    required_actions: tuple = ()
    warnings: tuple = ()

    def __post_init__(self) -> None:
        _check_str("register.root", self.root)
        object.__setattr__(self, "initialized", _check_bool("register.initialized", self.initialized))
        if self.proposed_runner is not None:
            object.__setattr__(self, "proposed_runner",
                               _check_enum("register.proposed_runner", self.proposed_runner, RunnerKind))
        items = _check_tuple("register.commands", self.commands)
        for item in items:
            if not isinstance(item, CommandSummary):
                raise TypeError("register.commands entries must be CommandSummary")
        object.__setattr__(self, "commands", items)
        actions = _as_str_tuple("register.required_actions", self.required_actions)
        for action in actions:
            if action not in REQUIRED_ACTIONS:
                raise ValueError(f"unknown required action {action!r}")
        object.__setattr__(self, "required_actions", actions)
        items = _check_tuple("register.warnings", self.warnings)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("register.warnings entries must be Reason")
        object.__setattr__(self, "warnings", items)


@dataclass(frozen=True, kw_only=True)
class PublishResult:
    committed: bool
    baseline_published: bool
    selection_disabled: bool
    reasons: tuple = ()

    def __post_init__(self) -> None:
        for field in ("committed", "baseline_published", "selection_disabled"):
            object.__setattr__(self, field, _check_bool(f"publish.{field}", getattr(self, field)))
        items = _check_tuple("publish.reasons", self.reasons)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("publish.reasons entries must be Reason")
        object.__setattr__(self, "reasons", items)


@dataclass(frozen=True, kw_only=True)
class QuiescenceProof:
    run_id: str
    generation: int
    pgid: int
    checked_at: float
    group_absent: bool
    escaped_survivors: bool

    def __post_init__(self) -> None:
        _check_hex("proof.run_id", self.run_id, 32)
        _check_int("proof.generation", self.generation, lo=0)
        _check_int("proof.pgid", self.pgid, lo=1)
        _check_float("proof.checked_at", self.checked_at, lo=0)
        object.__setattr__(self, "group_absent", _check_bool("proof.group_absent", self.group_absent))
        object.__setattr__(self, "escaped_survivors",
                           _check_bool("proof.escaped_survivors", self.escaped_survivors))


@dataclass(frozen=True, kw_only=True)
class Finalization:
    outcome_id: str | None
    status: Status
    exit_code: int
    source_valid: bool
    committed: bool

    def __post_init__(self) -> None:
        if self.outcome_id is not None:
            _check_str("finalization.outcome_id", self.outcome_id)
        object.__setattr__(self, "status", _check_enum("finalization.status", self.status, Status))
        _check_int("finalization.exit_code", self.exit_code)
        object.__setattr__(self, "source_valid", _check_bool("finalization.source_valid", self.source_valid))
        object.__setattr__(self, "committed", _check_bool("finalization.committed", self.committed))


@dataclass(frozen=True, kw_only=True)
class AdmissionState:
    state: LeaseState
    grant: Grant | None = None
    problem: Problem | None = None
    position: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _check_enum("admission.state", self.state, LeaseState))
        if self.grant is not None and not isinstance(self.grant, Grant):
            raise TypeError("admission.grant must be Grant or None")
        if self.problem is not None and not isinstance(self.problem, Problem):
            raise TypeError("admission.problem must be Problem or None")
        if self.position is not None:
            _check_int("admission.position", self.position, lo=0)


@dataclass(frozen=True, kw_only=True)
class LeaseView:
    run_id: str
    checkout_id: str
    state: LeaseState
    sequence: int
    requested_slots: int
    slots: int
    memory_estimate_mb: int | None
    reserved_memory_mb: int | None
    phase: str
    age_s: float
    queue_wait_s: float
    ownership: str
    fixture: bool
    reasons: tuple = ()

    def __post_init__(self) -> None:
        _check_hex("lease.run_id", self.run_id, 32)
        _check_hex("lease.checkout_id", self.checkout_id, 32)
        object.__setattr__(self, "state", _check_enum("lease.state", self.state, LeaseState))
        _check_int("lease.sequence", self.sequence, lo=0)
        _check_int("lease.requested_slots", self.requested_slots, lo=1, hi=64)
        _check_int("lease.slots", self.slots, lo=0, hi=64)
        for field in ("memory_estimate_mb", "reserved_memory_mb"):
            value = getattr(self, field)
            if value is not None:
                _check_int(f"lease.{field}", value, lo=0)
        _check_str("lease.phase", self.phase)
        _check_float("lease.age_s", self.age_s, lo=0)
        _check_float("lease.queue_wait_s", self.queue_wait_s, lo=0)
        if self.ownership not in ("certain", "uncertain", "gone"):
            raise ValueError("lease.ownership must be certain, uncertain or gone")
        object.__setattr__(self, "fixture", _check_bool("lease.fixture", self.fixture))
        items = _check_tuple("lease.reasons", self.reasons)
        for item in items:
            if not isinstance(item, Reason):
                raise TypeError("lease.reasons entries must be Reason")
        object.__setattr__(self, "reasons", items)


@dataclass(frozen=True, kw_only=True)
class ControlFrame:
    protocol: int
    run_id: str
    nonce: str
    kind: str
    payload: dict

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_VERSION:
            raise ValueError("control.protocol must be 1")
        _check_hex("control.run_id", self.run_id, 32)
        _check_hex("control.nonce", self.nonce, 64)
        if not _is_known(self.kind, CONTROL_KINDS):
            raise ValueError("control.kind is not a guard kind")
        if not isinstance(self.payload, dict):
            raise TypeError("control.payload must be a dict")
        _require_frame_keys(self.kind, self.payload)
        object.__setattr__(self, "payload", dict(self.payload))


def _require_frame_keys(kind: str, payload: dict) -> None:
    required = {
        "cancel": ("signal",),
        "parent-closing": (),
        "registered": ("guard",),
        "phase": ("phase", "attempt_id"),
        "runner-facts": ("attempt_id", "phase", "raw_exit_code",
                         "report_name", "problem"),
        "draining": ("provisional_artifact_id",),
    }[kind]
    for key in required:
        if key not in payload:
            raise ValueError(f"control frame {kind!r} requires {key!r}")
    for key in payload:
        if key not in required:
            raise ValueError(
                f"control frame {kind!r} carries an unknown field")
    if kind == "cancel" and payload["signal"] not in (2, 15):
        raise ValueError("cancel signal must be 2 or 15")
    if kind == "registered":
        guard = payload["guard"]
        if not isinstance(guard, dict):
            raise TypeError("registered guard must be an object")
        for key in ("pid", "birth", "uid", "pgid"):
            if key not in guard:
                raise ValueError(f"registered guard requires {key!r}")
        for key in guard:
            if key not in ("pid", "birth", "uid", "pgid"):
                raise ValueError(
                    "registered guard carries an unknown field")
        _check_int("registered.guard.pid", guard["pid"], lo=1)
        _check_float("registered.guard.birth", guard["birth"], lo=0)
        _check_int("registered.guard.uid", guard["uid"], lo=0)
        _check_int("registered.guard.pgid", guard["pgid"], lo=1)
    if kind == "phase":
        if payload["phase"] not in ("setup", "discovery", "execution",
                                    "finalization"):
            raise ValueError("phase payload has an unknown phase")
        if payload["attempt_id"] is not None and not isinstance(
                payload["attempt_id"], str):
            raise TypeError("phase attempt_id must be a string or null")
    if kind == "runner-facts":
        if not isinstance(payload["attempt_id"], str):
            raise TypeError("runner-facts attempt_id must be a string")
        if payload["phase"] not in ("setup", "execution"):
            raise ValueError("runner-facts phase must be setup or execution")
        for key in ("raw_exit_code",):
            if payload[key] is not None and not _is_int(payload[key]):
                raise TypeError(f"runner-facts {key} must be an integer or null")
        if payload["report_name"] is not None and not isinstance(
                payload["report_name"], str):
            raise TypeError("runner-facts report_name must be a string or null")
        if payload["problem"] is not None and not isinstance(
                payload["problem"], dict):
            raise TypeError("runner-facts problem must be an object or null")
        if isinstance(payload["problem"], dict):
            for key in payload["problem"]:
                if key not in ("code", "message", "phase", "retryable"):
                    raise ValueError(
                        "runner-facts problem carries an unknown field")
    if kind == "draining" and payload["provisional_artifact_id"] is not None:
        if not isinstance(payload["provisional_artifact_id"], str):
            raise TypeError(
                "draining provisional_artifact_id must be a string or null")


@dataclass(frozen=True, kw_only=True)
class LaunchManifest:
    protocol: int
    domain: DomainPaths
    grant: Grant
    setup: PreparedRun | None
    attempts: tuple
    attempt_ids: tuple
    setup_timeout_s: float
    attempt_timeout_s: float | None = None
    compound_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.protocol != PROTOCOL_VERSION:
            raise ValueError("manifest.protocol must be 1")
        if not isinstance(self.domain, DomainPaths):
            raise TypeError("manifest.domain must be DomainPaths")
        if not isinstance(self.grant, Grant):
            raise TypeError("manifest.grant must be Grant")
        if self.setup is not None and not isinstance(self.setup, PreparedRun):
            raise TypeError("manifest.setup must be PreparedRun or None")
        attempts = _check_tuple("manifest.attempts", self.attempts)
        if not 1 <= len(attempts) <= 10:
            raise ValueError("manifest.attempts must hold 1-10 entries")
        for item in attempts:
            if not isinstance(item, PreparedRun):
                raise TypeError("manifest.attempts entries must be PreparedRun")
        object.__setattr__(self, "attempts", attempts)
        attempt_ids = _as_str_tuple("manifest.attempt_ids", self.attempt_ids)
        if len(attempt_ids) != len(attempts):
            raise ValueError("manifest.attempt_ids must match attempts in length")
        for attempt_id in attempt_ids:
            if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
                raise ValueError("manifest.attempt_ids must match a001-a010")
        object.__setattr__(self, "attempt_ids", attempt_ids)
        object.__setattr__(self, "setup_timeout_s",
                           _check_float("manifest.setup_timeout_s", self.setup_timeout_s, lo=0.1))
        for field in ("attempt_timeout_s", "compound_timeout_s"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _check_float(f"manifest.{field}", value, lo=0.1))


@dataclass(frozen=True, kw_only=True)
class PublicDocument:
    kind: str
    ptest_version: str
    domain: dict | None
    data: dict | None
    error: Problem | None

    def __post_init__(self) -> None:
        if self.kind not in PUBLIC_KINDS:
            raise ValueError(f"document kind {self.kind!r} is not public")
        _check_str("document.ptest_version", self.ptest_version)
        if self.domain is not None and not isinstance(self.domain, dict):
            raise TypeError("document.domain must be a dict or None")
        if self.data is not None and not isinstance(self.data, dict):
            raise TypeError("document.data must be a dict or None")
        if self.error is not None and not isinstance(self.error, Problem):
            raise TypeError("document.error must be Problem or None")
        if self.error is not None and self.data is not None:
            raise ValueError("error documents carry a null payload")


def encode_public_document(kind: str, data: dict | None, *,
                           error: Problem | None = None,
                           domain: dict | None = None) -> bytes:
    """Serialize one public JSON document with explicit allowlisted fields.

    Success payloads run through the same validate-then-project authority
    the consumer uses, so the first emitted bytes already carry exactly the
    descriptor field set; additive unknowns (including argv/env-like keys)
    are dropped, never published. Invalid payload content raises Problem.
    """
    if kind not in PUBLIC_KINDS:
        raise ValueError(f"unknown public kind {kind!r}")
    if error is not None and not isinstance(error, Problem):
        raise TypeError("error must be Problem or None")
    if error is not None and data is not None:
        raise ValueError("error documents carry a null payload")
    domain = _validate_domain(domain)
    if error is None:
        if not isinstance(data, dict):
            raise TypeError("data must be a dict for success documents")
        _PAYLOAD_VALIDATORS[kind](data)
        data = _PROJECTORS[kind](data)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "ptest_version": PTEST_VERSION,
        "domain": domain,
        "data": data,
        "error": None if error is None else {
            "code": error.code,
            "message": error.message,
            "phase": error.phase,
            "retryable": error.retryable,
        },
    }
    return (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _invalid(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="contracts", retryable=False)


def _need_str(data: dict, name: str, *, allow_none: bool = False,
              allow_empty: bool = False):
    if name not in data:
        raise _invalid("report-invalid", f"missing required field {name!r}")
    value = data[name]
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid("report-invalid", f"field {name!r} must be a nonempty string")
    return value


def _need_int(data: dict, name: str, *, allow_none: bool = False):
    if name not in data:
        raise _invalid("report-invalid", f"missing required field {name!r}")
    value = data[name]
    if allow_none and value is None:
        return None
    if not _is_int(value):
        raise _invalid("report-invalid", f"field {name!r} must be an integer")
    return value


def _need_bool(data: dict, name: str):
    if name not in data:
        raise _invalid("report-invalid", f"missing required field {name!r}")
    value = data[name]
    if not isinstance(value, bool):
        raise _invalid("report-invalid", f"field {name!r} must be a boolean")
    return value


def _need_list(data: dict, name: str) -> list:
    if name not in data:
        raise _invalid("report-invalid", f"missing required field {name!r}")
    value = data[name]
    if not isinstance(value, list):
        raise _invalid("report-invalid", f"field {name!r} must be a list")
    return value


def _check_reason_dict(item: object) -> None:
    if not isinstance(item, dict):
        raise _invalid("report-invalid", "reason entries must be objects")
    for key in ("code", "message", "paths"):
        if key not in item:
            raise _invalid("report-invalid", f"reason is missing {key!r}")
    for key in item:
        if key not in ("code", "message", "paths"):
            raise _invalid("report-invalid",
                           "reason carries an unknown field")
    if not isinstance(item["code"], str) or not item["code"]:
        raise _invalid("report-invalid", "reason.code must be a nonempty string")
    if item["code"] not in REASON_CODES:
        raise _invalid("report-invalid", "reason.code is unknown")
    if not isinstance(item["message"], str):
        raise _invalid("report-invalid", "reason.message must be a string")
    if not isinstance(item["paths"], list) or any(
            not isinstance(path, str) for path in item["paths"]):
        raise _invalid("report-invalid", "reason.paths must be a string list")


def _check_required_keys(item: object, allowed: frozenset, ctx: str) -> dict:
    """Require every known field; additive unknowns are dropped by projection."""
    if not isinstance(item, dict):
        raise _invalid("report-invalid", f"{ctx} must be an object")
    for key in allowed:
        if key not in item:
            raise _invalid("report-invalid", f"{ctx} is missing {key!r}")
    return item


def _check_str_list(value: object, ctx: str) -> list:
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise _invalid("report-invalid", f"{ctx} must be a string list")
    return value


def _check_number_field(item: dict, name: str, ctx: str, *,
                        allow_none: bool = False, lo: float | None = None):
    value = item[name]
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid("report-invalid", f"{ctx} field {name!r} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise _invalid("report-invalid", f"{ctx} field {name!r} must be finite")
    if lo is not None and result < lo:
        raise _invalid("report-invalid", f"{ctx} field {name!r} is out of range")
    return value


def _check_int_field(item: dict, name: str, ctx: str, *,
                     allow_none: bool = False, lo: int | None = None,
                     hi: int | None = None):
    value = item[name]
    if allow_none and value is None:
        return None
    if not _is_int(value):
        raise _invalid("report-invalid", f"{ctx} field {name!r} must be an integer")
    if lo is not None and value < lo:
        raise _invalid("report-invalid", f"{ctx} field {name!r} is out of range")
    if hi is not None and value > hi:
        raise _invalid("report-invalid", f"{ctx} field {name!r} is out of range")
    return value


def _check_hex_field(item: dict, name: str, ctx: str, length: int, *,
                     allow_none: bool = False):
    value = item[name]
    if allow_none and value is None:
        return None
    if (not isinstance(value, str) or len(value) != length
            or any(char not in HEX_LOWER for char in value)):
        raise _invalid("report-invalid", f"{ctx} field {name!r} must be hex")
    return value


_COMMAND_FIELDS = frozenset({
    "kind", "mode", "argument_count", "generated_options", "workers",
    "provenance",
})


def _is_known(value: object, allowed: frozenset) -> bool:
    """Closed-enum membership that maps unhashable input to unknown."""
    return isinstance(value, str) and value in allowed


def _check_command_dict(item: object, ctx: str = "run.command") -> None:
    _check_required_keys(item, _COMMAND_FIELDS, ctx)
    if not _is_known(item["kind"],
                     frozenset(entry.value for entry in RunnerKind)):
        raise _invalid("report-invalid", f"{ctx} has an unknown kind")
    if not _is_known(item["mode"],
                     frozenset(entry.value for entry in Mode)):
        raise _invalid("report-invalid", f"{ctx} has an unknown mode")
    _check_int_field(item, "argument_count", ctx, lo=0)
    _check_str_list(item["generated_options"], f"{ctx}.generated_options")
    _check_int_field(item, "workers", ctx, allow_none=True, lo=1, hi=64)
    _check_str_list(item["provenance"], f"{ctx}.provenance")


_CAPABILITY_FIELDS = frozenset({
    "execution", "selection", "lifecycle", "limitations",
})


def _check_capability_dict(value: object) -> None:
    if value is None:
        return
    _check_required_keys(value, _CAPABILITY_FIELDS, "where.capability")
    if not _is_known(value["execution"],
                     frozenset(entry.value for entry in ExecutionTier)):
        raise _invalid("report-invalid", "where.capability has an unknown tier")
    if not isinstance(value["selection"], bool):
        raise _invalid("report-invalid", "where.capability.selection must be boolean")
    if value["lifecycle"] != "cooperative-process-group":
        raise _invalid("report-invalid", "where.capability has an unknown lifecycle")
    if not isinstance(value["limitations"], list):
        raise _invalid("report-invalid", "where.capability.limitations must be a list")
    for entry in value["limitations"]:
        _check_reason_dict(entry)


_COUNTS_FIELDS = frozenset({
    "collected", "executed", "passed", "failed", "skipped", "unknown",
})


def _check_counts_dict(value: object) -> None:
    if value is None:
        return
    _check_required_keys(value, _COUNTS_FIELDS, "run.counts")
    for name in ("collected", "executed", "passed", "failed", "skipped",
                 "unknown"):
        _check_int_field(value, name, "run.counts", allow_none=True, lo=0)


_TIMINGS_FIELDS = frozenset({
    "queue", "setup", "collection", "execution", "finalization",
})


def _check_timings_dict(value: object, ctx: str = "run.timings") -> None:
    if value is None:
        return
    _check_required_keys(value, _TIMINGS_FIELDS, ctx)
    for name in ("queue", "setup", "collection", "execution", "finalization"):
        _check_number_field(value, name, ctx, allow_none=True, lo=0)


_ATTEMPT_FIELDS = frozenset({
    "attempt_id", "phase", "status", "raw_exit_code", "final_exit_code",
    "source_valid", "inventory_complete", "timings",
})


def _check_attempt_dict(item: object) -> None:
    _check_required_keys(item, _ATTEMPT_FIELDS, "run.attempts entry")
    _check_str_list([item["attempt_id"]], "run.attempts entry.attempt_id")
    if not ATTEMPT_ID_PATTERN.fullmatch(item["attempt_id"]):
        raise _invalid("report-invalid", "run.attempts entry has a bad attempt_id")
    if not _is_known(item["phase"], RUN_PHASES):
        raise _invalid("report-invalid", "run.attempts entry has an unknown phase")
    if not _is_known(item["status"],
                     frozenset(entry.value for entry in Status)):
        raise _invalid("report-invalid", "run.attempts entry has an unknown status")
    _check_int_field(item, "raw_exit_code", "run.attempts entry", allow_none=True)
    _check_int_field(item, "final_exit_code", "run.attempts entry", allow_none=True)
    for name in ("source_valid", "inventory_complete"):
        if not isinstance(item[name], bool):
            raise _invalid(
                "report-invalid",
                f"run.attempts entry field {name!r} must be boolean")
    if "timings" not in item:
        raise _invalid("report-invalid", "run.attempts entry is missing 'timings'")
    _check_timings_dict(item["timings"], "run.attempts entry.timings")


_READINESS_FIELDS = frozenset({"area", "state", "reasons"})


def _check_readiness_dict(item: object) -> None:
    _check_required_keys(item, _READINESS_FIELDS, "doctor.readiness entry")
    if not _is_known(item["area"], READINESS_AREAS):
        raise _invalid("report-invalid", "doctor.readiness entry has an unknown area")
    if not _is_known(item["state"], READINESS_STATES):
        raise _invalid("report-invalid", "doctor.readiness entry has an unknown state")
    if not isinstance(item["reasons"], list):
        raise _invalid("report-invalid", "doctor.readiness entry reasons must be a list")
    for entry in item["reasons"]:
        _check_reason_dict(entry)


_FINDING_FIELDS = frozenset({
    "code", "severity", "confidence", "path", "line", "evidence_type",
    "consequence", "remediation", "verification",
})


def _check_finding_dict(item: object) -> None:
    _check_required_keys(item, _FINDING_FIELDS, "doctor.findings entry")
    if not _is_known(item["code"], FINDING_CODES):
        raise _invalid("report-invalid", "doctor.findings entry has an unknown code")
    if item["severity"] not in ("low", "medium", "high"):
        raise _invalid("report-invalid", "doctor.findings entry has a bad severity")
    if item["confidence"] not in ("low", "medium", "high"):
        raise _invalid("report-invalid", "doctor.findings entry has a bad confidence")
    if item["path"] is not None and not isinstance(item["path"], str):
        raise _invalid("report-invalid", "doctor.findings entry path must be str/null")
    _check_int_field(item, "line", "doctor.findings entry", allow_none=True, lo=1)
    for name in ("evidence_type", "consequence", "remediation", "verification"):
        if not isinstance(item[name], str):
            raise _invalid(
                "report-invalid",
                f"doctor.findings entry field {name!r} must be a string")


_LEASE_FIELDS = frozenset({
    "run_id", "checkout_id", "state", "sequence", "requested_slots", "slots",
    "memory_estimate_mb", "reserved_memory_mb", "phase", "age_s",
    "queue_wait_s", "ownership", "fixture", "reasons",
})


def _check_lease_dict(item: object, ctx: str) -> None:
    _check_required_keys(item, _LEASE_FIELDS, ctx)
    _check_hex_field(item, "run_id", ctx, 32)
    _check_hex_field(item, "checkout_id", ctx, 32)
    if not _is_known(item["state"],
                     frozenset(entry.value for entry in LeaseState)):
        raise _invalid("report-invalid", f"{ctx} has an unknown state")
    _check_int_field(item, "sequence", ctx, lo=0)
    _check_int_field(item, "requested_slots", ctx, lo=1, hi=64)
    _check_int_field(item, "slots", ctx, lo=0, hi=64)
    _check_int_field(item, "memory_estimate_mb", ctx, allow_none=True, lo=0)
    _check_int_field(item, "reserved_memory_mb", ctx, allow_none=True, lo=0)
    if not isinstance(item["phase"], str) or not item["phase"]:
        raise _invalid("report-invalid", f"{ctx} phase must be a nonempty string")
    _check_number_field(item, "age_s", ctx, lo=0)
    _check_number_field(item, "queue_wait_s", ctx, lo=0)
    if item["ownership"] not in ("certain", "uncertain", "gone"):
        raise _invalid("report-invalid", f"{ctx} has an unknown ownership")
    if not isinstance(item["fixture"], bool):
        raise _invalid("report-invalid", f"{ctx} fixture must be boolean")
    if not isinstance(item["reasons"], list):
        raise _invalid("report-invalid", f"{ctx} reasons must be a list")
    for entry in item["reasons"]:
        _check_reason_dict(entry)


_OBLIGATION_FIELDS = frozenset({
    "file", "test_id", "sequence", "source_digest", "compatibility", "reason",
})


def _check_obligation_dict(item: object) -> None:
    _check_required_keys(item, _OBLIGATION_FIELDS, "history.obligations entry")
    if item["file"] is not None and not isinstance(item["file"], str):
        raise _invalid("report-invalid", "history.obligations entry file bad")
    if item["test_id"] is not None and not isinstance(item["test_id"], str):
        raise _invalid("report-invalid", "history.obligations entry test_id bad")
    _check_int_field(item, "sequence", "history.obligations entry", lo=0)
    _check_hex_field(item, "source_digest", "history.obligations entry", 64,
                     allow_none=True)
    if item["compatibility"] is not None and not isinstance(
            item["compatibility"], str):
        raise _invalid("report-invalid", "history.obligations entry compat bad")
    if not isinstance(item["reason"], str):
        raise _invalid("report-invalid", "history.obligations entry reason bad")


_SCAN_LIMITS_FIELDS = frozenset({
    "entries", "files", "file_bytes", "total_bytes", "findings",
    "output_bytes", "elapsed_s", "depth", "ast_nodes",
})


def _check_scan_limits_dict(item: object) -> None:
    _check_required_keys(item, _SCAN_LIMITS_FIELDS, "doctor.limits")
    for name in ("entries", "files", "file_bytes", "total_bytes", "findings",
                 "output_bytes", "depth", "ast_nodes"):
        _check_int_field(item, name, "doctor.limits", lo=0)
    _check_number_field(item, "elapsed_s", "doctor.limits", lo=0)


_SCAN_USAGE_FIELDS = frozenset({
    "entries", "files", "file_bytes", "total_bytes", "findings",
    "output_bytes", "elapsed_s", "skipped", "truncated",
})


def _check_scan_usage_dict(item: object) -> None:
    _check_required_keys(item, _SCAN_USAGE_FIELDS, "doctor.usage")
    for name in ("entries", "files", "file_bytes", "total_bytes", "findings",
                 "output_bytes", "skipped"):
        _check_int_field(item, name, "doctor.usage", lo=0)
    _check_number_field(item, "elapsed_s", "doctor.usage", lo=0)
    if not isinstance(item["truncated"], bool):
        raise _invalid("report-invalid", "doctor.usage truncated must be boolean")


_CONFIG_SUMMARY_FIELDS = frozenset({
    "project_id", "runner_kind", "workers", "commands",
    "setup_configured", "setup_network", "setup_lifecycle_scripts",
    "selection_enabled", "closed_inputs",
})


def _check_config_summary_dict(value: object, ctx: str) -> None:
    if value is None:
        return
    _check_required_keys(value, _CONFIG_SUMMARY_FIELDS, ctx)
    _check_hex_field(value, "project_id", ctx, 32)
    if not _is_known(value["runner_kind"],
                     frozenset(item.value for item in RunnerKind)):
        raise _invalid("report-invalid", f"{ctx} has an unknown runner_kind")
    _check_int_field(value, "workers", ctx, lo=1, hi=64)
    commands = value["commands"]
    if not isinstance(commands, list) or len(commands) != 2:
        raise _invalid(
            "report-invalid", f"{ctx}.commands must hold scoped then full")
    _check_command_dict(commands[0], f"{ctx}.commands[0]")
    _check_command_dict(commands[1], f"{ctx}.commands[1]")
    if commands[0]["mode"] != "scoped" or commands[1]["mode"] != "full":
        raise _invalid(
            "report-invalid", f"{ctx}.commands must be scoped then full")
    if (commands[0]["kind"] != value["runner_kind"]
            or commands[1]["kind"] != value["runner_kind"]):
        raise _invalid(
            "report-invalid", f"{ctx}.commands must match runner_kind")
    for name in ("setup_configured", "setup_network",
                 "setup_lifecycle_scripts", "selection_enabled",
                 "closed_inputs"):
        if not isinstance(value[name], bool):
            raise _invalid(
                "report-invalid", f"{ctx} field {name!r} must be boolean")


_EFFECTIVE_LIMITS_FIELDS = frozenset({
    "max_slots", "max_jobs", "memory_mb", "repo_workers",
})


def _check_effective_limits_dict(value: object, ctx: str) -> None:
    _check_required_keys(value, _EFFECTIVE_LIMITS_FIELDS, ctx)
    _check_int_field(value, "max_slots", ctx, allow_none=True, lo=1, hi=64)
    _check_int_field(value, "max_jobs", ctx, allow_none=True, lo=1, hi=64)
    if (value["max_slots"] is None) != (value["max_jobs"] is None):
        raise _invalid(
            "report-invalid", f"{ctx} slot/job unknowns must pair")
    if (value["max_slots"] is not None and value["max_jobs"] is not None
            and value["max_jobs"] > value["max_slots"]):
        raise _invalid(
            "report-invalid", f"{ctx} max_jobs exceeds max_slots")
    _check_int_field(value, "memory_mb", ctx, allow_none=True,
                     lo=64, hi=1048576)
    _check_int_field(value, "repo_workers", ctx, allow_none=True,
                     lo=1, hi=64)


def _check_closed(data: dict, name: str, allowed: frozenset,
                  *, allow_none: bool = False):
    value = _need_str(data, name, allow_none=allow_none, allow_empty=True)
    if value is None:
        return None
    if value not in allowed:
        raise _invalid("report-invalid",
                       f"field {name!r} has an unknown value")
    return value


def _validate_run_payload(data: dict) -> None:
    for name in ("run_id", "project_id", "checkout_id"):
        _need_str(data, name)
        _check_hex_field(data, name, "run", 32)
    for name in ("started_at", "finished_at"):
        _need_str(data, name)
    _check_closed(data, "mode", frozenset(item.value for item in Mode))
    _check_closed(data, "status", frozenset(item.value for item in Status))
    _check_closed(data, "phase", RUN_PHASES)
    if "plan" not in data or not isinstance(data["plan"], dict):
        raise _invalid("report-invalid", "run.plan must be an object")
    _validate_plan_payload(data["plan"])
    if "command" not in data or not isinstance(data["command"], dict):
        raise _invalid("report-invalid", "run.command must be an object")
    _check_command_dict(data["command"])
    _need_int(data, "exit_code")
    _check_closed(data, "exit_origin", EXIT_ORIGINS)
    for name in ("granted_workers", "memory_estimate_mb", "reserved_memory_mb",
                 "runner_exit_code", "signal"):
        _need_int(data, name, allow_none=True)
    _check_int_field(data, "granted_workers", "run", allow_none=True,
                     lo=1, hi=64)
    _check_int_field(data, "memory_estimate_mb", "run", allow_none=True,
                     lo=0)
    _check_int_field(data, "reserved_memory_mb", "run", allow_none=True,
                     lo=0)
    for name in ("source_valid", "full_gate_eligible", "baseline_published"):
        _need_bool(data, name)
    for name in ("counts", "timings"):
        if name not in data:
            raise _invalid("report-invalid", f"missing required field {name!r}")
    _need_str(data, "artifact_id", allow_none=True)
    _check_counts_dict(data["counts"])
    _check_timings_dict(data["timings"])
    attempts = _need_list(data, "attempts")
    for attempt in attempts:
        _check_attempt_dict(attempt)
    for name in ("reasons", "limitations"):
        for item in _need_list(data, name):
            _check_reason_dict(item)


def _validate_plan_payload(data: dict) -> None:
    _check_closed(data, "mode", frozenset(item.value for item in Mode))
    execution = data.get("execution")
    if execution not in ("full", "selected", "scoped", "none"):
        raise _invalid("report-invalid", "plan.execution has an unknown value")
    files = _need_list(data, "files")
    if any(not isinstance(item, str) for item in files):
        raise _invalid("report-invalid", "plan.files must be a string list")
    for item in _need_list(data, "reasons"):
        _check_reason_dict(item)
    for name in ("input_digest", "compatibility", "baseline_run_id"):
        if name not in data:
            raise _invalid("report-invalid", f"missing required field {name!r}")
        if data[name] is not None and not isinstance(data[name], str):
            raise _invalid("report-invalid", f"plan field {name!r} must be str/null")
    _need_bool(data, "static_preview")


def _validate_where_payload(data: dict) -> None:
    _need_str(data, "root")
    if "config_path" not in data:
        raise _invalid("report-invalid", "missing required field 'config_path'")
    if data["config_path"] is not None and not isinstance(data["config_path"], str):
        raise _invalid("report-invalid", "where.config_path must be a string or null")
    _need_bool(data, "initialized")
    _check_closed(data, "runner_kind", frozenset(item.value for item in RunnerKind),
                  allow_none=True)
    if "capability" not in data:
        raise _invalid("report-invalid", "missing required field 'capability'")
    _check_capability_dict(data["capability"])
    for entry in _need_list(data, "commands"):
        _check_command_dict(entry, "where.commands entry")
    if "effective_limits" not in data or not isinstance(
            data["effective_limits"], dict):
        raise _invalid("report-invalid", "where.effective_limits must be an object")
    _check_effective_limits_dict(
        data["effective_limits"], "where.effective_limits")
    _check_str_list(_need_list(data, "provenance"), "where.provenance")
    for item in _need_list(data, "warnings"):
        _check_reason_dict(item)


def _validate_status_payload(data: dict) -> None:
    if "effective_limits" not in data or not isinstance(
            data["effective_limits"], dict):
        raise _invalid("report-invalid", "status.effective_limits must be an object")
    _check_effective_limits_dict(
        data["effective_limits"], "status.effective_limits")
    for entry in _need_list(data, "queued"):
        _check_lease_dict(entry, "status.queued entry")
    for entry in _need_list(data, "active"):
        _check_lease_dict(entry, "status.active entry")


def _validate_history_payload(data: dict) -> None:
    for entry in _need_list(data, "summaries"):
        if not isinstance(entry, dict):
            raise _invalid(
                "report-invalid", "history.summaries entries must be objects")
        _validate_run_payload(entry)
    for entry in _need_list(data, "obligations"):
        _check_obligation_dict(entry)


def _validate_init_payload(data: dict) -> None:
    if not _is_known(data.get("action"), _INIT_ACTIONS):
        raise _invalid("report-invalid", "init.action is unknown")
    _need_str(data, "target")
    _need_bool(data, "exists")
    if (data["action"] == "preview") == data["exists"]:
        raise _invalid("report-invalid", "init.action/exists mismatch")
    for item in _need_list(data, "warnings"):
        _check_reason_dict(item)
    if "config" not in data:
        raise _invalid("report-invalid", "missing required field 'config'")
    _check_config_summary_dict(data["config"], "init.config")


def _validate_doctor_payload(data: dict) -> None:
    _check_str_list(_need_list(data, "scope"), "doctor.scope")
    for entry in _need_list(data, "readiness"):
        _check_readiness_dict(entry)
    for entry in _need_list(data, "findings"):
        _check_finding_dict(entry)
    if "limits" not in data:
        raise _invalid("report-invalid", "missing required field 'limits'")
    _check_scan_limits_dict(data["limits"])
    if "usage" not in data:
        raise _invalid("report-invalid", "missing required field 'usage'")
    _check_scan_usage_dict(data["usage"])
    for item in _need_list(data, "limitations"):
        _check_reason_dict(item)


def _validate_register_payload(data: dict) -> None:
    _need_str(data, "root")
    _need_bool(data, "initialized")
    if "proposed_runner" not in data:
        raise _invalid("report-invalid", "missing required field 'proposed_runner'")
    if (data["proposed_runner"] is not None
            and not _is_known(data["proposed_runner"], frozenset(
                item.value for item in RunnerKind))):
        raise _invalid("report-invalid", "register.proposed_runner is unknown")
    for entry in _need_list(data, "commands"):
        _check_command_dict(entry, "register.commands entry")
    actions = _need_list(data, "required_actions")
    for action in actions:
        if not _is_known(action, REQUIRED_ACTIONS):
            raise _invalid("report-invalid",
                           "register.required_actions has an unknown action")
    for item in _need_list(data, "warnings"):
        _check_reason_dict(item)


_PAYLOAD_VALIDATORS = {
    "run": _validate_run_payload,
    "plan": _validate_plan_payload,
    "where": _validate_where_payload,
    "status": _validate_status_payload,
    "history": _validate_history_payload,
    "init": _validate_init_payload,
    "doctor": _validate_doctor_payload,
    "register": _validate_register_payload,
}


_INIT_ACTIONS = frozenset(item.value for item in InitAction)


def _project_reason(item: dict) -> dict:
    return {"code": item["code"], "message": item["message"],
            "paths": list(item["paths"])}


def _project_command(item: dict) -> dict:
    return {"kind": item["kind"], "mode": item["mode"],
            "argument_count": item["argument_count"],
            "generated_options": list(item["generated_options"]),
            "workers": item["workers"],
            "provenance": list(item["provenance"])}


def _project_plan(item: dict) -> dict:
    return {"mode": item["mode"], "execution": item["execution"],
            "files": list(item["files"]),
            "reasons": [_project_reason(entry)
                        for entry in item["reasons"]],
            "input_digest": item["input_digest"],
            "compatibility": item["compatibility"],
            "baseline_run_id": item["baseline_run_id"],
            "static_preview": item["static_preview"]}


def _project_capability(value: object) -> dict | None:
    if value is None:
        return None
    return {"execution": value["execution"],
            "selection": value["selection"],
            "lifecycle": value["lifecycle"],
            "limitations": [_project_reason(entry)
                            for entry in value["limitations"]]}


def _project_counts(value: object) -> dict | None:
    if value is None:
        return None
    return {name: value[name] for name in _COUNTS_FIELDS}


def _project_timings(value: object) -> dict | None:
    if value is None:
        return None
    return {name: value[name] for name in _TIMINGS_FIELDS}


def _project_attempt(item: dict) -> dict:
    return {"attempt_id": item["attempt_id"], "phase": item["phase"],
            "status": item["status"],
            "raw_exit_code": item["raw_exit_code"],
            "final_exit_code": item["final_exit_code"],
            "source_valid": item["source_valid"],
            "inventory_complete": item["inventory_complete"],
            "timings": _project_timings(item["timings"])}


def _project_readiness(item: dict) -> dict:
    return {"area": item["area"], "state": item["state"],
            "reasons": [_project_reason(entry)
                        for entry in item["reasons"]]}


def _project_finding(item: dict) -> dict:
    return {name: item[name] for name in _FINDING_FIELDS}


def _project_lease(item: dict) -> dict:
    return {
        "run_id": item["run_id"], "checkout_id": item["checkout_id"],
        "state": item["state"], "sequence": item["sequence"],
        "requested_slots": item["requested_slots"],
        "slots": item["slots"],
        "memory_estimate_mb": item["memory_estimate_mb"],
        "reserved_memory_mb": item["reserved_memory_mb"],
        "phase": item["phase"], "age_s": item["age_s"],
        "queue_wait_s": item["queue_wait_s"],
        "ownership": item["ownership"], "fixture": item["fixture"],
        "reasons": [_project_reason(entry)
                    for entry in item["reasons"]],
    }


def _project_obligation(item: dict) -> dict:
    return {name: item[name] for name in _OBLIGATION_FIELDS}


def _project_scan_limits(item: dict) -> dict:
    return {name: item[name] for name in _SCAN_LIMITS_FIELDS}


def _project_scan_usage(item: dict) -> dict:
    return {name: item[name] for name in _SCAN_USAGE_FIELDS}


def _project_config_summary(value: object) -> dict | None:
    if value is None:
        return None
    return {
        "project_id": value["project_id"],
        "runner_kind": value["runner_kind"],
        "workers": value["workers"],
        "commands": [_project_command(entry)
                     for entry in value["commands"]],
        "setup_configured": value["setup_configured"],
        "setup_network": value["setup_network"],
        "setup_lifecycle_scripts": value["setup_lifecycle_scripts"],
        "selection_enabled": value["selection_enabled"],
        "closed_inputs": value["closed_inputs"],
    }


def _project_effective_limits(item: dict) -> dict:
    return {name: item[name] for name in _EFFECTIVE_LIMITS_FIELDS}


def _project_run(item: dict) -> dict:
    return {
        "run_id": item["run_id"], "project_id": item["project_id"],
        "checkout_id": item["checkout_id"], "mode": item["mode"],
        "status": item["status"], "phase": item["phase"],
        "started_at": item["started_at"],
        "finished_at": item["finished_at"],
        "plan": _project_plan(item["plan"]),
        "command": _project_command(item["command"]),
        "granted_workers": item["granted_workers"],
        "memory_estimate_mb": item["memory_estimate_mb"],
        "reserved_memory_mb": item["reserved_memory_mb"],
        "runner_exit_code": item["runner_exit_code"],
        "exit_code": item["exit_code"],
        "exit_origin": item["exit_origin"], "signal": item["signal"],
        "source_valid": item["source_valid"],
        "full_gate_eligible": item["full_gate_eligible"],
        "baseline_published": item["baseline_published"],
        "counts": _project_counts(item["counts"]),
        "timings": _project_timings(item["timings"]),
        "attempts": [_project_attempt(entry)
                     for entry in item["attempts"]],
        "reasons": [_project_reason(entry)
                    for entry in item["reasons"]],
        "limitations": [_project_reason(entry)
                        for entry in item["limitations"]],
        "artifact_id": item["artifact_id"],
    }


def _project_where_payload(data: dict) -> dict:
    return {
        "root": data["root"], "config_path": data["config_path"],
        "initialized": data["initialized"],
        "runner_kind": data["runner_kind"],
        "capability": _project_capability(data["capability"]),
        "commands": [_project_command(entry)
                     for entry in data["commands"]],
        "effective_limits": _project_effective_limits(
            data["effective_limits"]),
        "provenance": list(data["provenance"]),
        "warnings": [_project_reason(entry)
                     for entry in data["warnings"]],
    }


def _project_status_payload(data: dict) -> dict:
    return {
        "effective_limits": _project_effective_limits(
            data["effective_limits"]),
        "queued": [_project_lease(entry) for entry in data["queued"]],
        "active": [_project_lease(entry) for entry in data["active"]],
    }


def _project_history_payload(data: dict) -> dict:
    return {
        "summaries": [_project_run(entry)
                      for entry in data["summaries"]],
        "obligations": [_project_obligation(entry)
                        for entry in data["obligations"]],
    }


def _project_init_payload(data: dict) -> dict:
    return {
        "action": data["action"], "target": data["target"],
        "exists": data["exists"],
        "warnings": [_project_reason(entry)
                     for entry in data["warnings"]],
        "config": _project_config_summary(data["config"]),
    }


def _project_doctor_payload(data: dict) -> dict:
    return {
        "scope": list(data["scope"]),
        "readiness": [_project_readiness(entry)
                      for entry in data["readiness"]],
        "findings": [_project_finding(entry)
                     for entry in data["findings"]],
        "limits": _project_scan_limits(data["limits"]),
        "usage": _project_scan_usage(data["usage"]),
        "limitations": [_project_reason(entry)
                        for entry in data["limitations"]],
    }


def _project_register_payload(data: dict) -> dict:
    return {
        "root": data["root"], "initialized": data["initialized"],
        "proposed_runner": data["proposed_runner"],
        "commands": [_project_command(entry)
                     for entry in data["commands"]],
        "required_actions": list(data["required_actions"]),
        "warnings": [_project_reason(entry)
                     for entry in data["warnings"]],
    }


_PROJECTORS: dict = {
    "run": _project_run,
    "plan": _project_plan,
    "where": _project_where_payload,
    "status": _project_status_payload,
    "history": _project_history_payload,
    "init": _project_init_payload,
    "doctor": _project_doctor_payload,
    "register": _project_register_payload,
}


def _project_domain(value: object) -> dict | None:
    if value is None:
        return None
    return {"id": value["id"], "fixture": value["fixture"]}


def _validate_domain(value: object) -> dict | None:
    """Validate a public envelope domain and project onto known fields."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("report-invalid", "domain must be an object or null")
    if set(("id", "fixture")) - set(value):
        raise _invalid("report-invalid", "domain requires id and fixture")
    try:
        _check_hex("domain.id", value["id"], 32)
    except (TypeError, ValueError):
        raise _invalid("report-invalid", "domain.id must be 32 hex") from None
    if not isinstance(value["fixture"], bool):
        raise _invalid("report-invalid", "domain.fixture must be boolean")
    return _project_domain(value)


def decode_public_document(raw: bytes | str | bytearray) -> PublicDocument:
    """Parse and strictly validate one public JSON document."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise _invalid("report-invalid", "document is not UTF-8") from None
    elif isinstance(raw, str):
        text = raw
    else:
        raise TypeError("document must be bytes or str")
    try:
        envelope = json.loads(text)
    except (RecursionError, ValueError):
        raise _invalid("report-invalid", "document is not JSON") from None
    if not isinstance(envelope, dict):
        raise _invalid("report-invalid", "document must be a JSON object")
    version = envelope.get("schema_version")
    if not _is_int(version):
        raise _invalid("report-invalid", "schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise _invalid("protocol-mismatch",
                       "unsupported schema major version")
    kind = envelope.get("kind")
    if kind not in PUBLIC_KINDS:
        raise _invalid("report-invalid", "unknown document kind")
    ptest_version = envelope.get("ptest_version")
    if not isinstance(ptest_version, str) or not ptest_version:
        raise _invalid("report-invalid", "ptest_version must be a nonempty string")
    domain = _validate_domain(envelope.get("domain"))
    error_raw = envelope.get("error")
    error = None
    if error_raw is not None:
        if not isinstance(error_raw, dict):
            raise _invalid("report-invalid", "error must be an object or null")
        for key in ("code", "message", "phase", "retryable"):
            if key not in error_raw:
                raise _invalid("report-invalid", f"error is missing {key!r}")
        if not isinstance(error_raw["code"], str) or not error_raw["code"]:
            raise _invalid("report-invalid", "error.code must be nonempty")
        if not isinstance(error_raw["message"], str):
            raise _invalid("report-invalid", "error.message must be a string")
        if not isinstance(error_raw["phase"], str) or not error_raw["phase"]:
            raise _invalid("report-invalid", "error.phase must be nonempty")
        if not isinstance(error_raw["retryable"], bool):
            raise _invalid("report-invalid", "error.retryable must be boolean")
        error = Problem(code=error_raw["code"], message=error_raw["message"],
                        phase=error_raw["phase"], retryable=error_raw["retryable"])
    data = envelope.get("data")
    if error is not None:
        if data is not None:
            raise _invalid("report-invalid", "error documents carry a null payload")
    else:
        if not isinstance(data, dict):
            raise _invalid("report-invalid", "success documents carry an object payload")
        _PAYLOAD_VALIDATORS[kind](data)
        data = _PROJECTORS[kind](data)
    return PublicDocument(kind=kind, ptest_version=ptest_version,
                          domain=domain, data=data, error=error)


def _check_nesting(value: object, limit: int, depth: int = 0) -> None:
    """Reject nesting past ``limit`` without recursing (parser-safe)."""
    stack = [(value, depth)]
    while stack:
        node, at = stack.pop()
        if isinstance(node, dict):
            items = node.values()
        elif isinstance(node, (list, tuple)):
            items = node
        else:
            continue
        if at >= limit:
            raise _invalid("protocol-mismatch", "frame nesting exceeds the bound")
        for item in items:
            stack.append((item, at + 1))


def _frame_object(frame: ControlFrame) -> dict:
    return {
        "protocol": frame.protocol,
        "run_id": frame.run_id,
        "nonce": frame.nonce,
        "kind": frame.kind,
        "payload": frame.payload,
    }


def encode_control_frame(frame: ControlFrame) -> bytes:
    """Length-prefixed private guard frame; rejects oversize before send."""
    if not isinstance(frame, ControlFrame):
        raise TypeError("encode_control_frame requires ControlFrame")
    _check_nesting(_frame_object(frame), CONTROL_FRAME_MAX_NESTING)
    body = json.dumps(_frame_object(frame), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if len(body) > CONTROL_FRAME_MAX_BYTES:
        raise _invalid("protocol-mismatch", "control frame exceeds 65536 bytes")
    return struct.pack(">I", len(body)) + body


def decode_control_frame(data: bytes | bytearray, *,
                         expected_nonce: str | None = None) -> ControlFrame:
    """Parse one length-prefixed frame; truncated/oversize/foreign rejected."""
    raw = bytes(data)
    if len(raw) < 4:
        raise _invalid("protocol-mismatch", "control frame truncation: no length prefix")
    (length,) = struct.unpack(">I", raw[:4])
    if length > CONTROL_FRAME_MAX_BYTES:
        raise _invalid("protocol-mismatch", "control frame declares oversize length")
    body = raw[4:]
    if len(body) < length:
        raise _invalid("protocol-mismatch", "control frame truncation: short body")
    if len(body) > length:
        raise _invalid("protocol-mismatch", "control frame has trailing data")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise _invalid("protocol-mismatch", "control frame is not JSON") from None
    if not isinstance(obj, dict):
        raise _invalid("protocol-mismatch", "control frame must be an object")
    for key in obj:
        if key not in ("protocol", "run_id", "nonce", "kind", "payload"):
            raise _invalid("protocol-mismatch",
                           "control frame carries an unknown field")
    if obj.get("protocol") != PROTOCOL_VERSION:
        raise _invalid("protocol-mismatch", "control frame has the wrong protocol")
    try:
        _check_hex("control.run_id", obj.get("run_id"), 32)
        _check_hex("control.nonce", obj.get("nonce"), 64)
    except (TypeError, ValueError):
        raise _invalid("protocol-mismatch", "control frame has a bad run or nonce") from None
    kind = obj.get("kind")
    if not _is_known(kind, CONTROL_KINDS):
        raise _invalid("protocol-mismatch", "control frame kind is unknown")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise _invalid("protocol-mismatch", "control frame payload must be an object")
    if expected_nonce is not None and obj["nonce"] != expected_nonce:
        raise _invalid("protocol-mismatch", "control frame has the wrong nonce")
    _check_nesting(payload, CONTROL_FRAME_MAX_NESTING)
    try:
        return ControlFrame(protocol=1, run_id=obj["run_id"], nonce=obj["nonce"],
                            kind=kind, payload=payload)
    except (TypeError, ValueError):
        raise _invalid("protocol-mismatch",
                       "control frame payload failed validation") from None


def _prepared_dict(prepared: PreparedRun) -> dict:
    return {
        "argv": list(prepared.argv),
        "cwd": str(prepared.cwd),
        "env_updates": [[key, value] for key, value in prepared.env_updates],
        "report_path": None if prepared.report_path is None else str(prepared.report_path),
        "capability": _capability_dict(prepared.capability),
        "summary": (None if prepared.summary is None
                    else _command_dict(prepared.summary)),
    }


def _domain_dict(domain: DomainPaths) -> dict:
    return {
        "root": str(domain.root),
        "machine_config": str(domain.machine_config),
        "ledger": str(domain.ledger),
        "marker": str(domain.marker),
        "fixture": domain.fixture,
        "domain_id": domain.domain_id,
    }


def _grant_dict(grant: Grant) -> dict:
    return {
        "run_id": grant.run_id,
        "nonce": grant.nonce,
        "slots": grant.slots,
        "memory_estimate_mb": grant.memory_estimate_mb,
        "reserved_memory_mb": grant.reserved_memory_mb,
        "generation": grant.generation,
        "domain_id": grant.domain_id,
    }


def _manifest_object(manifest: LaunchManifest) -> dict:
    setup = manifest.setup
    return {
        "protocol": manifest.protocol,
        "domain": _domain_dict(manifest.domain),
        "grant": _grant_dict(manifest.grant),
        "setup": None if setup is None else _prepared_dict(setup),
        "attempts": [_prepared_dict(item) for item in manifest.attempts],
        "attempt_ids": list(manifest.attempt_ids),
        "setup_timeout_s": manifest.setup_timeout_s,
        "attempt_timeout_s": manifest.attempt_timeout_s,
        "compound_timeout_s": manifest.compound_timeout_s,
    }


def encode_launch_manifest(manifest: LaunchManifest) -> bytes:
    """Serialize a private launch manifest with explicit path strings."""
    if not isinstance(manifest, LaunchManifest):
        raise TypeError("encode_launch_manifest requires LaunchManifest")
    _check_nesting(_manifest_object(manifest), MANIFEST_MAX_NESTING)
    body = json.dumps(_manifest_object(manifest), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if len(body) > MANIFEST_MAX_BYTES:
        raise _invalid("protocol-mismatch", "launch manifest exceeds 4 MiB")
    return struct.pack(">I", len(body)) + body


def _build_domain(raw: object) -> DomainPaths:
    if not isinstance(raw, dict):
        raise _invalid("protocol-mismatch", "manifest domain must be an object")
    for key in raw:
        if key not in ("root", "machine_config", "ledger", "marker",
                       "fixture", "domain_id"):
            raise _invalid("protocol-mismatch",
                           "manifest domain carries an unknown field")
    try:
        return DomainPaths(
            root=raw["root"], machine_config=raw["machine_config"],
            ledger=raw["ledger"], marker=raw["marker"],
            fixture=raw["fixture"], domain_id=raw["domain_id"],
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("protocol-mismatch",
                       "manifest domain failed validation") from None


def _build_grant(raw: object) -> Grant:
    if not isinstance(raw, dict):
        raise _invalid("protocol-mismatch", "manifest grant must be an object")
    for key in raw:
        if key not in ("run_id", "nonce", "slots", "memory_estimate_mb",
                       "reserved_memory_mb", "generation", "domain_id"):
            raise _invalid("protocol-mismatch",
                           "manifest grant carries an unknown field")
    try:
        return Grant(
            run_id=raw["run_id"], nonce=raw["nonce"], slots=raw["slots"],
            memory_estimate_mb=raw["memory_estimate_mb"],
            reserved_memory_mb=raw["reserved_memory_mb"],
            generation=raw["generation"], domain_id=raw["domain_id"],
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("protocol-mismatch",
                       "manifest grant failed validation") from None


def _build_prepared(raw: object) -> PreparedRun:
    if not isinstance(raw, dict):
        raise _invalid("protocol-mismatch", "manifest attempt must be an object")
    for key in raw:
        if key not in ("argv", "cwd", "env_updates", "report_path",
                       "capability", "summary"):
            raise _invalid("protocol-mismatch",
                           "manifest attempt carries an unknown field")
    try:
        capability = None
        if raw.get("capability") is not None:
            cap = raw["capability"]
            if not isinstance(cap, dict):
                raise _invalid("protocol-mismatch",
                               "manifest capability must be an object")
            for key in cap:
                if key not in ("execution", "selection", "lifecycle",
                               "limitations"):
                    raise _invalid(
                        "protocol-mismatch",
                        "manifest capability carries an unknown field")
            entries = []
            for item in cap.get("limitations", []):
                if not isinstance(item, dict):
                    raise _invalid("protocol-mismatch",
                                   "manifest limitation must be an object")
                for key in item:
                    if key not in ("code", "message", "paths"):
                        raise _invalid(
                            "protocol-mismatch",
                            "manifest limitation carries an unknown field")
                entries.append(item)
            capability = Capability(
                execution=cap["execution"], selection=cap["selection"],
                lifecycle=cap["lifecycle"],
                limitations=tuple(
                    Reason(code=item["code"], message=item["message"],
                           paths=tuple(item["paths"]))
                    for item in entries
                ),
            )
        summary = None
        if raw.get("summary") is not None:
            node = raw["summary"]
            if not isinstance(node, dict):
                raise _invalid("protocol-mismatch",
                               "manifest summary must be an object")
            for key in node:
                if key not in ("kind", "mode", "argument_count",
                               "generated_options", "workers", "provenance"):
                    raise _invalid("protocol-mismatch",
                                   "manifest summary carries an unknown field")
            summary = CommandSummary(
                kind=node["kind"], mode=node["mode"],
                argument_count=node["argument_count"],
                generated_options=tuple(node.get("generated_options", ())),
                workers=node.get("workers"),
                provenance=tuple(node.get("provenance", ())),
            )
        return PreparedRun(
            argv=tuple(raw["argv"]), cwd=raw["cwd"],
            env_updates=tuple((key, value) for key, value in raw.get("env_updates", [])),
            report_path=raw.get("report_path"),
            capability=capability, summary=summary,
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("protocol-mismatch",
                       "manifest attempt failed validation") from None


def decode_launch_manifest(data: bytes | bytearray) -> LaunchManifest:
    """Parse a length-prefixed manifest; shape violations are rejected."""
    raw = bytes(data)
    if len(raw) < 4:
        raise _invalid("protocol-mismatch", "manifest truncation: no length prefix")
    (length,) = struct.unpack(">I", raw[:4])
    if length > MANIFEST_MAX_BYTES:
        raise _invalid("protocol-mismatch", "manifest declares oversize length")
    body = raw[4:]
    if len(body) < length:
        raise _invalid("protocol-mismatch", "manifest truncation: short body")
    if len(body) > length:
        raise _invalid("protocol-mismatch", "manifest has trailing data")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise _invalid("protocol-mismatch", "manifest is not JSON") from None
    if not isinstance(obj, dict) or obj.get("protocol") != PROTOCOL_VERSION:
        raise _invalid("protocol-mismatch", "manifest has the wrong protocol")
    for key in obj:
        if key not in ("protocol", "domain", "grant", "setup", "attempts",
                       "attempt_ids", "setup_timeout_s", "attempt_timeout_s",
                       "compound_timeout_s"):
            raise _invalid("protocol-mismatch",
                           "manifest carries an unknown field")
    _check_nesting(obj, MANIFEST_MAX_NESTING)
    try:
        setup = None
        if obj.get("setup") is not None:
            setup = _build_prepared(obj["setup"])
        attempts = tuple(_build_prepared(item) for item in obj["attempts"])
        return LaunchManifest(
            protocol=1, domain=_build_domain(obj["domain"]),
            grant=_build_grant(obj["grant"]), setup=setup,
            attempts=attempts, attempt_ids=tuple(obj["attempt_ids"]),
            setup_timeout_s=obj["setup_timeout_s"],
            attempt_timeout_s=obj.get("attempt_timeout_s"),
            compound_timeout_s=obj.get("compound_timeout_s"),
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("protocol-mismatch",
                       "manifest body failed validation") from None


def _envelope_schema(kind: str, data_schema: dict) -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"ptest public document: {kind}",
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
            "kind": {"type": "string", "const": kind},
            "ptest_version": {"type": "string"},
            "domain": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                    "fixture": {"type": "boolean"},
                },
                "required": ["id", "fixture"],
            },
            "data": data_schema,
            "error": {
                "type": ["object", "null"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "phase": {"type": "string"},
                    "retryable": {"type": "boolean"},
                },
                "required": ["code", "message", "phase", "retryable"],
            },
        },
        "required": ["schema_version", "kind", "ptest_version", "domain",
                     "data", "error"],
    }


def _reason_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["code", "message", "paths"],
    }


def _command_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [item.value for item in RunnerKind]},
            "mode": {"type": "string", "enum": [item.value for item in Mode]},
            "argument_count": {"type": "integer", "minimum": 0},
            "generated_options": {"type": "array", "items": {"type": "string"}},
            "workers": {"type": ["integer", "null"], "minimum": 1, "maximum": 64},
            "provenance": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["kind", "mode", "argument_count", "generated_options",
                     "workers", "provenance"],
    }


def _plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": [item.value for item in Mode]},
            "execution": {"type": "string",
                          "enum": ["full", "selected", "scoped", "none"]},
            "files": {"type": "array", "items": {"type": "string"}},
            "reasons": {"type": "array", "items": _reason_schema()},
            "input_digest": {"type": ["string", "null"]},
            "compatibility": {"type": ["string", "null"]},
            "baseline_run_id": {"type": ["string", "null"]},
            "static_preview": {"type": "boolean"},
        },
        "required": ["mode", "execution", "files", "reasons", "input_digest",
                     "compatibility", "baseline_run_id", "static_preview"],
    }


def _capability_schema() -> dict:
    """Shared Capability descriptor: single authority for schema + decode."""
    return {
        "type": ["object", "null"],
        "properties": {
            "execution": {"type": "string",
                          "enum": [item.value for item in ExecutionTier]},
            "selection": {"type": "boolean"},
            "lifecycle": {"type": "string",
                          "const": "cooperative-process-group"},
            "limitations": {"type": "array", "items": _reason_schema()},
        },
        "required": ["execution", "selection", "lifecycle", "limitations"],
    }


def _counts_schema() -> dict:
    """Shared Counts descriptor: single authority for schema + decode."""
    return {
        "type": ["object", "null"],
        "properties": {
            name: {"type": ["integer", "null"], "minimum": 0}
            for name in ("collected", "executed", "passed", "failed",
                         "skipped", "unknown")
        },
        "required": ["collected", "executed", "passed", "failed", "skipped",
                     "unknown"],
    }


def _timings_schema() -> dict:
    """Shared Timings descriptor: single authority for schema + decode."""
    return {
        "type": ["object", "null"],
        "properties": {
            name: {"type": ["number", "null"], "minimum": 0}
            for name in ("queue", "setup", "collection", "execution",
                         "finalization")
        },
        "required": ["queue", "setup", "collection", "execution",
                     "finalization"],
    }


def _attempt_schema() -> dict:
    """Shared attempt-record descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "attempt_id": {"type": "string", "pattern": "^a(00[1-9]|010)$"},
            "phase": {"type": "string", "enum": sorted(RUN_PHASES)},
            "status": {"type": "string",
                       "enum": [item.value for item in Status]},
            "raw_exit_code": {"type": ["integer", "null"]},
            "final_exit_code": {"type": ["integer", "null"]},
            "source_valid": {"type": "boolean"},
            "inventory_complete": {"type": "boolean"},
            "timings": _timings_schema(),
        },
        "required": ["attempt_id", "phase", "status", "raw_exit_code",
                     "final_exit_code", "source_valid", "inventory_complete",
                     "timings"],
    }


def _readiness_schema() -> dict:
    """Shared Readiness descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "area": {"type": "string", "enum": sorted(READINESS_AREAS)},
            "state": {"type": "string", "enum": sorted(READINESS_STATES)},
            "reasons": {"type": "array", "items": _reason_schema()},
        },
        "required": ["area", "state", "reasons"],
    }


def _finding_schema() -> dict:
    """Shared Finding descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "code": {"type": "string", "enum": sorted(FINDING_CODES)},
            "severity": {"type": "string",
                         "enum": ["low", "medium", "high"]},
            "confidence": {"type": "string",
                           "enum": ["low", "medium", "high"]},
            "path": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"], "minimum": 1},
            "evidence_type": {"type": "string"},
            "consequence": {"type": "string"},
            "remediation": {"type": "string"},
            "verification": {"type": "string"},
        },
        "required": ["code", "severity", "confidence", "path", "line",
                     "evidence_type", "consequence", "remediation",
                     "verification"],
    }


def _lease_schema() -> dict:
    """Shared LeaseView descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "checkout_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "state": {"type": "string",
                      "enum": [item.value for item in LeaseState]},
            "sequence": {"type": "integer", "minimum": 0},
            "requested_slots": {"type": "integer", "minimum": 1, "maximum": 64},
            "slots": {"type": "integer", "minimum": 0, "maximum": 64},
            "memory_estimate_mb": {"type": ["integer", "null"], "minimum": 0},
            "reserved_memory_mb": {"type": ["integer", "null"], "minimum": 0},
            "phase": {"type": "string"},
            "age_s": {"type": "number", "minimum": 0},
            "queue_wait_s": {"type": "number", "minimum": 0},
            "ownership": {"type": "string",
                          "enum": ["certain", "uncertain", "gone"]},
            "fixture": {"type": "boolean"},
            "reasons": {"type": "array", "items": _reason_schema()},
        },
        "required": ["run_id", "checkout_id", "state", "sequence",
                     "requested_slots", "slots", "memory_estimate_mb",
                     "reserved_memory_mb", "phase", "age_s", "queue_wait_s",
                     "ownership", "fixture", "reasons"],
    }


def _obligation_schema() -> dict:
    """Shared Obligation descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "file": {"type": ["string", "null"]},
            "test_id": {"type": ["string", "null"]},
            "sequence": {"type": "integer", "minimum": 0},
            "source_digest": {"type": ["string", "null"],
                              "pattern": "^([0-9a-f]{64})$"},
            "compatibility": {"type": ["string", "null"]},
            "reason": {"type": "string"},
        },
        "required": ["file", "test_id", "sequence", "source_digest",
                     "compatibility", "reason"],
    }


def _scan_limits_schema() -> dict:
    """Shared ScanLimits descriptor: single authority for schema + decode."""
    fields = {
        name: {"type": "integer", "minimum": 0}
        for name in ("entries", "files", "file_bytes", "total_bytes",
                     "findings", "output_bytes", "depth", "ast_nodes")
    }
    fields["elapsed_s"] = {"type": "number", "minimum": 0}
    return {
        "type": "object",
        "properties": fields,
        "required": ["entries", "files", "file_bytes", "total_bytes",
                     "findings", "output_bytes", "elapsed_s", "depth",
                     "ast_nodes"],
    }


def _scan_usage_schema() -> dict:
    """Shared ScanUsage descriptor: single authority for schema + decode."""
    fields = {
        name: {"type": "integer", "minimum": 0}
        for name in ("entries", "files", "file_bytes", "total_bytes",
                     "findings", "output_bytes", "skipped")
    }
    fields["elapsed_s"] = {"type": "number", "minimum": 0}
    fields["truncated"] = {"type": "boolean"}
    return {
        "type": "object",
        "properties": fields,
        "required": ["entries", "files", "file_bytes", "total_bytes",
                     "findings", "output_bytes", "elapsed_s", "skipped",
                     "truncated"],
    }


def _run_data_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "project_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "checkout_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "mode": {"type": "string", "enum": [item.value for item in Mode]},
            "status": {"type": "string", "enum": [item.value for item in Status]},
            "phase": {"type": "string",
                      "enum": sorted(RUN_PHASES)},
            "started_at": {"type": "string"},
            "finished_at": {"type": "string"},
            "plan": _plan_schema(),
            "command": _command_schema(),
            "granted_workers": {"type": ["integer", "null"], "minimum": 1},
            "memory_estimate_mb": {"type": ["integer", "null"], "minimum": 0},
            "reserved_memory_mb": {"type": ["integer", "null"], "minimum": 0},
            "runner_exit_code": {"type": ["integer", "null"]},
            "exit_code": {"type": "integer"},
            "exit_origin": {"type": "string", "enum": sorted(EXIT_ORIGINS)},
            "signal": {"type": ["integer", "null"]},
            "source_valid": {"type": "boolean"},
            "full_gate_eligible": {"type": "boolean"},
            "baseline_published": {"type": "boolean"},
            "counts": _counts_schema(),
            "timings": _timings_schema(),
            "attempts": {"type": "array", "items": _attempt_schema()},
            "reasons": {"type": "array", "items": _reason_schema()},
            "limitations": {"type": "array", "items": _reason_schema()},
            "artifact_id": {"type": ["string", "null"]},
        },
        "required": ["run_id", "project_id", "checkout_id", "mode", "status",
                     "phase", "started_at", "finished_at", "plan", "command",
                     "granted_workers", "memory_estimate_mb",
                     "reserved_memory_mb", "runner_exit_code", "exit_code",
                     "exit_origin", "signal", "source_valid",
                     "full_gate_eligible", "baseline_published", "counts",
                     "timings", "attempts", "reasons", "limitations",
                     "artifact_id"],
    }


def _config_summary_schema() -> dict:
    """Shared ConfigSummary descriptor: single authority for schema + decode."""
    return {
        "type": ["object", "null"],
        "properties": {
            "project_id": {"type": "string",
                           "pattern": "^[0-9a-f]{32}$"},
            "runner_kind": {"type": "string",
                            "enum": [item.value for item in RunnerKind]},
            "workers": {"type": "integer", "minimum": 1, "maximum": 64},
            "commands": {"type": "array", "items": _command_schema(),
                         "minItems": 2, "maxItems": 2},
            "setup_configured": {"type": "boolean"},
            "setup_network": {"type": "boolean"},
            "setup_lifecycle_scripts": {"type": "boolean"},
            "selection_enabled": {"type": "boolean"},
            "closed_inputs": {"type": "boolean"},
        },
        "required": ["project_id", "runner_kind", "workers", "commands",
                     "setup_configured", "setup_network",
                     "setup_lifecycle_scripts", "selection_enabled",
                     "closed_inputs"],
    }


def _effective_limits_schema() -> dict:
    """Shared EffectiveLimits descriptor: single authority for schema + decode."""
    return {
        "type": "object",
        "properties": {
            "max_slots": {"type": ["integer", "null"],
                          "minimum": 1, "maximum": 64},
            "max_jobs": {"type": ["integer", "null"],
                         "minimum": 1, "maximum": 64},
            "memory_mb": {"type": ["integer", "null"],
                          "minimum": 64, "maximum": 1048576},
            "repo_workers": {"type": ["integer", "null"],
                             "minimum": 1, "maximum": 64},
        },
        "required": ["max_slots", "max_jobs", "memory_mb", "repo_workers"],
    }


PUBLIC_SCHEMAS: dict = {
    "run": _envelope_schema("run", _run_data_schema()),
    "plan": _envelope_schema("plan", _plan_schema()),
    "where": _envelope_schema("where", {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "config_path": {"type": ["string", "null"]},
            "initialized": {"type": "boolean"},
            "runner_kind": {"type": ["string", "null"],
                            "enum": [item.value for item in RunnerKind] + [None]},
            "capability": _capability_schema(),
            "commands": {"type": "array", "items": _command_schema()},
            "effective_limits": _effective_limits_schema(),
            "provenance": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": _reason_schema()},
        },
        "required": ["root", "config_path", "initialized", "runner_kind",
                     "capability", "commands", "effective_limits",
                     "provenance", "warnings"],
    }),
    "status": _envelope_schema("status", {
        "type": "object",
        "properties": {
            "effective_limits": _effective_limits_schema(),
            "queued": {"type": "array", "items": _lease_schema()},
            "active": {"type": "array", "items": _lease_schema()},
        },
        "required": ["effective_limits", "queued", "active"],
    }),
    "history": _envelope_schema("history", {
        "type": "object",
        "properties": {
            "summaries": {"type": "array",
                          "items": _run_data_schema()},
            "obligations": {"type": "array", "items": _obligation_schema()},
        },
        "required": ["summaries", "obligations"],
    }),
    "init": _envelope_schema("init", {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["created", "preview", "existing"]},
            "target": {"type": "string"},
            "exists": {"type": "boolean"},
            "warnings": {"type": "array", "items": _reason_schema()},
            "config": _config_summary_schema(),
        },
        "required": ["action", "target", "exists", "warnings", "config"],
    }),
    "doctor": _envelope_schema("doctor", {
        "type": "object",
        "properties": {
            "scope": {"type": "array", "items": {"type": "string"}},
            "readiness": {"type": "array", "items": _readiness_schema()},
            "findings": {"type": "array", "items": _finding_schema()},
            "limits": _scan_limits_schema(),
            "usage": _scan_usage_schema(),
            "limitations": {"type": "array", "items": _reason_schema()},
        },
        "required": ["scope", "readiness", "findings", "limits", "usage",
                     "limitations"],
    }),
    "register": _envelope_schema("register", {
        "type": "object",
        "properties": {
            "root": {"type": "string"},
            "initialized": {"type": "boolean"},
            "proposed_runner": {
                "type": ["string", "null"],
                "enum": [item.value for item in RunnerKind] + [None],
            },
            "commands": {"type": "array", "items": _command_schema()},
            "required_actions": {"type": "array",
                                 "items": {"type": "string",
                                           "enum": sorted(REQUIRED_ACTIONS)}},
            "warnings": {"type": "array", "items": _reason_schema()},
        },
        "required": ["root", "initialized", "proposed_runner", "commands",
                     "required_actions", "warnings"],
    }),
}

PROTOCOL_V1_DESCRIPTOR: dict = {
    "protocol": PROTOCOL_VERSION,
    "control_frame": {
        "prefix": "uint32-big-endian-byte-length",
        "encoding": "utf-8-json-object",
        "max_bytes": CONTROL_FRAME_MAX_BYTES,
        "max_nesting": CONTROL_FRAME_MAX_NESTING,
        "required": ["protocol", "run_id", "nonce", "kind", "payload"],
        "kinds": {
            "cancel": {"signal": [2, 15]},
            "parent-closing": {},
            "registered": {"guard": "ProcessIdentity"},
            "phase": {"phase": "setup|discovery|execution|finalization",
                      "attempt_id": "string|null"},
            "runner-facts": {
                "attempt_id": "string",
                "phase": "setup|execution",
                "raw_exit_code": "integer|null",
                "report_name": "string|null",
                "problem": "Problem|null",
            },
            "draining": {"provisional_artifact_id": "string|null"},
        },
    },
    "launch_manifest": {
        "prefix": "uint32-big-endian-byte-length",
        "encoding": "utf-8-json-object",
        "max_bytes": MANIFEST_MAX_BYTES,
        "max_nesting": MANIFEST_MAX_NESTING,
        "required": ["protocol", "domain", "grant", "setup", "attempts",
                     "attempt_ids", "setup_timeout_s", "attempt_timeout_s",
                     "compound_timeout_s"],
        "attempts": {"min_items": 1, "max_items": 10},
    },
    "bridge_event": {
        "encoding": "utf-8-json-lines",
        "protocol": PROTOCOL_VERSION,
        "required": ["protocol", "run_id", "nonce", "event"],
        "events": ["inventory", "test-outcome", "terminal"],
        "max_events": PROTOCOL_MAX_EVENTS,
        "max_line_bytes": LINE_MAX_BYTES,
        "max_depth": PROTOCOL_MAX_DEPTH,
    },
    "limits": {
        "max_prompt_bytes": MAX_PROMPT_BYTES,
        "native_report_max_bytes": NATIVE_REPORT_MAX_BYTES,
        "native_report_max_tests": NATIVE_REPORT_MAX_TESTS,
        "test_id_max_bytes": TEST_ID_MAX_BYTES,
    },
}
