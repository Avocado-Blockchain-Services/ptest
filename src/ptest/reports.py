"""Private, bounded native terminal-report allocation and consumption.

This module is deliberately separate from the public document contracts.  A
binding is created before a native process is launched; the process later
creates the bound file exclusively.  Only a complete, identity-matching,
strictly shaped record is admitted as terminal evidence.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from . import files as F


PROTOCOL_VERSION = 1
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
_OUTCOMES = frozenset({"passed", "failed", "error", "skipped", "xfail", "xpass", "unknown"})
_FIELDS = frozenset({
    "protocol", "run_id", "nonce", "attempt_id", "runner",
    "observed_runtime_version", "execution_mode", "effective_profile",
    "terminal_complete", "native_exit_code", "bridge_exit_code", "problem",
    "test_counts",
})
_ATTEMPT_FIELDS = _FIELDS | frozenset({
    "runtime_identity", "runtime_facts", "inventory", "workers", "coverage", "reporters",
})


def _descriptor_limits() -> tuple[int, int, int, int]:
    """Read the frozen descriptor used by both native bridge writers.

    The private parser must reject against the same bounds as the bridge,
    rather than silently retaining a second monolithic-report limit. Missing
    or malformed descriptor data is state-unavailable and therefore fail
    closed before any report bytes are admitted.
    """
    descriptor = Path(__file__).with_name("runtime") / "protocol-v1.json"
    try:
        value = json.loads(descriptor.read_text(encoding="utf-8"))
        limits = value["limits"]
        max_bytes = limits["native_report_max_bytes"]
        max_tests = limits["native_report_max_tests"]
        max_id_bytes = limits["test_id_max_bytes"]
        max_events = value["bridge_event"]["max_events"]
        if any(type(item) is not int or item <= 0
               for item in (max_bytes, max_tests, max_id_bytes, max_events)):
            raise ValueError
        return max_bytes, max_tests, max_events, max_id_bytes
    except (OSError, UnicodeError, ValueError, KeyError, TypeError,
            json.JSONDecodeError):
        raise _problem("state-unavailable", "native report descriptor is unavailable") from None


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
    checkout_root: Path | None = None
    _created_identity: tuple[int, int] | None = field(
        default=None, init=False, repr=False, compare=False,
    )
    _evidence_digest: str | None = field(
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
        if self.checkout_root is not None and (
                not isinstance(self.checkout_root, Path)
                or not self.checkout_root.is_absolute()):
            raise TypeError("report binding checkout root must be absolute")
        if not isinstance(self.report_name, str) or _REPORT_NAME_RE.fullmatch(self.report_name) is None:
            raise TypeError("report binding has an invalid report name")
        if not self.report_name.startswith(f"native-{self.attempt_id}-"):
            raise TypeError("report binding report name does not match its attempt")

    @property
    def path(self) -> Path:
        return self.report_directory / self.report_name


@dataclass(frozen=True, kw_only=True)
class ProjectNarrowing:
    """Bridge-owned record of allowed full-mode narrowing.

    The single source of truth behind the ``full (project-filtered: ...)``
    label: the effective checked-in narrowing text, every accepted
    conftest-hook file, and non-default collection ini/conftest notes.
    """

    narrowing: str | None = None
    conftest_hooks: tuple = ()
    notes: tuple = ()

    def __post_init__(self) -> None:
        if self.narrowing is not None:
            if not isinstance(self.narrowing, str) or not self.narrowing:
                raise TypeError("project narrowing text must be a nonempty string or None")
            if len(self.narrowing) > 1024:
                raise ValueError("project narrowing text exceeds its bound")
        for name in ("conftest_hooks", "notes"):
            items = getattr(self, name)
            if not isinstance(items, (list, tuple)):
                raise TypeError(f"project narrowing {name} must be a list or tuple")
            if (len(items) > 64
                    or any(not isinstance(item, str) or not item or len(item) > 512
                           for item in items)):
                raise ValueError(f"project narrowing {name} exceeds its bound")
            for item in items:
                if "\\" in item or item.startswith("/") or item == ".." \
                        or item.startswith("../") or "\x00" in item:
                    raise ValueError(f"project narrowing {name} carries an unsafe path")
            object.__setattr__(self, name, tuple(items))


def _check_narrowing(value: object) -> ProjectNarrowing:
    if isinstance(value, ProjectNarrowing):
        return value
    if not isinstance(value, dict) or set(value) != {"narrowing", "conftest_hooks", "notes"}:
        raise TypeError("project narrowing must carry exactly its three fields")
    return ProjectNarrowing(**value)


def project_filter_label(narrowing: ProjectNarrowing) -> str | None:
    """Render the run label from the bridge-owned narrowing report, if any."""
    if not isinstance(narrowing, ProjectNarrowing):
        raise TypeError("project filter label needs a ProjectNarrowing report")
    parts: list[str] = []
    if narrowing.narrowing:
        parts.append(narrowing.narrowing)
    if narrowing.conftest_hooks:
        parts.append("conftest collection hook")
    parts.extend(narrowing.notes)
    if not parts:
        return None
    return "full (project-filtered: " + "; ".join(parts) + ")"


_TEST_COUNT_FIELDS = frozenset({
    "collected", "executed", "passed", "failed", "skipped", "unknown",
})


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
    test_counts: dict | None = None
    project_narrowing: ProjectNarrowing = field(
        default_factory=lambda: ProjectNarrowing(
            narrowing=None, conftest_hooks=(), notes=()))

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_narrowing",
                           _check_narrowing(self.project_narrowing))
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
        if self.test_counts is not None:
            if (not isinstance(self.test_counts, dict)
                    or set(self.test_counts) != _TEST_COUNT_FIELDS):
                raise TypeError("terminal test counts have invalid fields")
            for field in _TEST_COUNT_FIELDS:
                value = self.test_counts[field]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise TypeError("terminal test counts must be nonnegative integers")
            object.__setattr__(self, "test_counts", dict(self.test_counts))


@dataclass(frozen=True, kw_only=True)
class NativeAttemptReport:
    """Authenticated advanced native evidence emitted by a bridge.

    The basic terminal record intentionally remains a smaller, execution-only
    contract.  Advanced qualification consumes this strict extension and never
    infers inventory, coverage, or worker identity from text output.
    """

    terminal: NativeTerminalReport
    runtime_identity: str
    runtime_facts: dict
    inventory: C.Inventory
    workers: tuple[tuple[str, str], ...]
    coverage_complete: bool
    reporters_complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.terminal, NativeTerminalReport):
            raise TypeError("attempt report terminal must be NativeTerminalReport")
        if self.terminal.effective_profile != C.ExecutionTier.ADVANCED.value:
            raise ValueError("advanced attempt report requires the advanced profile")
        if not isinstance(self.runtime_identity, str) or re.fullmatch(r"[0-9a-f]{64}", self.runtime_identity) is None:
            raise ValueError("attempt report runtime identity must be a 64-hex digest")
        if not isinstance(self.runtime_facts, dict):
            raise ValueError("advanced attempt report runtime facts are unavailable")
        facts = json.dumps(
            self.runtime_facts, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        if hashlib.sha256(facts).hexdigest() != self.runtime_identity:
            raise ValueError("advanced attempt report runtime identity does not match its facts")
        if not isinstance(self.inventory, C.Inventory) or not self.inventory.complete:
            raise ValueError("advanced attempt report requires a complete inventory")
        if (self.inventory.adapter != self.terminal.runner
                or self.inventory.version != self.terminal.observed_runtime_version):
            raise ValueError("advanced attempt report inventory is not bound to the terminal")
        seen_ids: set[str] = set()
        seen_workers: set[str] = set()
        seen_prefixes: set[str] = set()
        for item in self.inventory.tests:
            if item.id in seen_ids:
                raise ValueError("advanced attempt report contains duplicate test IDs")
            seen_ids.add(item.id)
            if not item.file:
                raise ValueError("advanced attempt report contains an unbound test ID")
            if item.outcome is C.Outcome.UNKNOWN:
                raise ValueError("advanced attempt report contains an unknown outcome")
        for worker_id, prefix in self.workers:
            if (not isinstance(worker_id, str)
                    or not re.fullmatch(r"w(?:0[0-5][0-9]|06[0-3])", worker_id)
                    or not isinstance(prefix, str)
                    or not re.fullmatch(r"[A-Za-z0-9_]+", prefix)
                    or not prefix.startswith("pt_")
                    or self.terminal.run_id not in prefix
                    or self.terminal.attempt_id not in prefix
                    or not prefix.endswith(f"_{worker_id}")
                    or len(prefix) > 54):
                raise ValueError("advanced attempt report has invalid worker identity")
            if worker_id in seen_workers or prefix in seen_prefixes:
                raise ValueError("advanced attempt report contains duplicate worker identities")
            seen_workers.add(worker_id)
            seen_prefixes.add(prefix)
        if not self.workers:
            raise ValueError("advanced attempt report has no worker identities")
        if not isinstance(self.coverage_complete, bool) or not self.coverage_complete:
            raise ValueError("advanced attempt report lacks complete coverage evidence")
        if not isinstance(self.reporters_complete, bool) or not self.reporters_complete:
            raise ValueError("advanced attempt report lacks complete reporter evidence")


def evidence_content_digest(evidence: C.AttemptEvidence) -> str:
    """Digest the exact typed evidence consumed from one native report."""
    if not isinstance(evidence, C.AttemptEvidence):
        raise TypeError("evidence content digest requires AttemptEvidence")
    inventory = None if evidence.inventory is None else {
        "adapter": evidence.inventory.adapter,
        "version": evidence.inventory.version,
        "complete": evidence.inventory.complete,
        "digest": evidence.inventory.digest,
        "tests": [
            {"id": item.id, "file": item.file, "outcome": item.outcome.value,
             "setup_s": item.setup_s, "call_s": item.call_s,
             "teardown_s": item.teardown_s}
            for item in evidence.inventory.tests
        ],
    }
    payload = {
        "attempt_id": evidence.attempt_id,
        "result": C._attempt_dict(evidence.result),
        "inventory": inventory,
        "terminal_complete": evidence.terminal_complete,
        "parallel_identity": evidence.parallel_identity,
        "runtime_identity": evidence.runtime_identity,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


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
        checkout_root=checkout.root,
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
                checkout_root=checkout.root,
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
    # The narrowing field is required: no legacy reports without it exist,
    # so a missing field stays rejected instead of decoding as unfiltered.
    if set(value) != _FIELDS | {"project_narrowing"}:
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


def _inventory(value: object, binding: NativeReportBinding) -> C.Inventory:
    if not isinstance(value, dict):
        _reject()
    if set(value) != {"adapter", "version", "complete", "tests", "digest"}:
        _reject()
    tests = value.get("tests")
    _, max_tests, max_events, max_id_bytes = _descriptor_limits()
    if (not isinstance(tests, list)
            or len(tests) > max_tests
            or len(tests) + 1 > max_events):
        _reject("capacity-exceeded")
    records: list[C.TestRecord] = []
    seen: set[str] = set()
    for item in tests:
        if not isinstance(item, dict) or set(item) != {
                "id", "file", "outcome", "setup_s", "call_s", "teardown_s"}:
            _reject()
        if (not isinstance(item["id"], str)
                or len(item["id"].encode("utf-8")) > max_id_bytes
                or item["id"] in seen):
            _reject()
        if not isinstance(item["outcome"], str) or item["outcome"] not in _OUTCOMES:
            _reject()
        try:
            record = C.TestRecord(
                id=item["id"], file=item["file"],
                outcome=C.Outcome(item["outcome"]),
                setup_s=item["setup_s"], call_s=item["call_s"],
                teardown_s=item["teardown_s"],
            )
        except (TypeError, ValueError):
            _reject()
        if (binding.checkout_root is None or os.path.isabs(record.file)
                or "\x00" in record.file or record.file == ".."
                or record.file.startswith("../")
                or any(part in {"", ".", ".."} for part in record.file.split("/"))):
            _reject()
        try:
            root = os.path.realpath(binding.checkout_root)
            collected = os.path.realpath(os.path.join(root, record.file))
            if os.path.commonpath((root, collected)) != root:
                _reject()
        except (OSError, ValueError):
            _reject()
        seen.add(record.id)
        records.append(record)
    try:
        inventory = C.Inventory(adapter=value["adapter"], version=value["version"],
                                complete=value["complete"], tests=tuple(records),
                                digest=value["digest"])
    except (TypeError, ValueError):
        _reject()
    encoded = json.dumps(tests, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != inventory.digest:
        _reject()
    return inventory


def _decode_attempt(binding: NativeReportBinding, raw: bytes) -> NativeAttemptReport:
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
    if set(value) != _ATTEMPT_FIELDS | {"project_narrowing"}:
        _reject()
    try:
        terminal_fields = {key: value.get(key) for key in _FIELDS} | {
            "project_narrowing": value["project_narrowing"]}
        terminal = _decode(binding, json.dumps(terminal_fields,
                                               separators=(",", ":")).encode())
    except (TypeError, ValueError, C.Problem):
        _reject()
    runtime = value.get("runtime_identity")
    if not isinstance(runtime, str) or re.fullmatch(r"[0-9a-f]{64}", runtime) is None:
        _reject()
    runtime_facts = value.get("runtime_facts")
    if not isinstance(runtime_facts, dict):
        _reject()
    required_runtime_facts = (
        {"runner", "version", "python", "implementation", "cache_tag", "roots",
         "profile", "plugins", "dependencies", "hooks", "effective_options",
         "command_variants", "coverage", "reporters", "platform"}
        if terminal.runner == "pytest" else
        {"runner", "version", "node", "profile", "pool", "fileParallelism",
         "maxWorkers", "minWorkers", "maxConcurrency", "coverage", "reporters",
         "plugins", "dependencies", "command_variants", "platform"}
    )
    if (set(runtime_facts) != required_runtime_facts
            or runtime_facts.get("runner") != terminal.runner
            or runtime_facts.get("version") != terminal.observed_runtime_version
            or runtime_facts.get("profile") != "advanced"
            or not isinstance(runtime_facts.get("command_variants"), list)):
        _reject()
    inventory = _inventory(value.get("inventory"), binding)
    workers = value.get("workers")
    if not isinstance(workers, list) or len(workers) > 64:
        _reject()
    identities: list[tuple[str, str]] = []
    checkout_id = binding.report_directory.parent.name
    if re.fullmatch(r"[0-9a-f]{32}", checkout_id) is None:
        _reject()
    for item in workers:
        if not isinstance(item, dict) or set(item) != {"worker_id", "resource_prefix"}:
            _reject()
        worker_id, prefix = item["worker_id"], item["resource_prefix"]
        if not isinstance(worker_id, str) or not isinstance(prefix, str):
            _reject()
        expected_prefix = (
            f"pt_{checkout_id[:8]}_{terminal.run_id}_{terminal.attempt_id}_{worker_id}")
        if prefix != expected_prefix:
            _reject()
        identities.append((worker_id, prefix))
    coverage = value.get("coverage")
    reporters = value.get("reporters")
    if (not isinstance(coverage, dict) or set(coverage) != {"complete"}
            or not isinstance(coverage["complete"], bool)
            or not isinstance(reporters, dict) or set(reporters) != {"complete"}
            or not isinstance(reporters["complete"], bool)):
        _reject()
    try:
        return NativeAttemptReport(
            terminal=terminal, runtime_identity=runtime, runtime_facts=runtime_facts,
            inventory=inventory, workers=tuple(identities),
            coverage_complete=coverage["complete"],
            reporters_complete=reporters["complete"],
        )
    except (TypeError, ValueError):
        _reject()
    raise AssertionError("unreachable")


def _read_report(binding: NativeReportBinding) -> tuple[bytes, tuple[int, int]]:
    binding = _binding(binding)
    if binding._created_identity is not None:
        _reject()
    try:
        F.validate_private_file(binding.path)
        before = _identity(binding.path)
        max_bytes, _, _, _ = _descriptor_limits()
        raw = F.read_regular(binding.report_directory, binding.report_name, max_bytes + 1)
        if len(raw) > max_bytes:
            _reject("capacity-exceeded")
        F.validate_private_file(binding.path)
        after = _identity(binding.path)
        if before != after:
            _reject("unsafe-path")
    except C.Problem as exc:
        raise _problem(exc.code, "native report could not be read") from None
    except (FileNotFoundError, OSError):
        _reject("state-unavailable")
    return raw, before


def consume_report(binding: NativeReportBinding) -> NativeTerminalReport:
    """Consume a binding once, after quiescence, through bounded no-follow access."""
    binding = _binding(binding)
    raw, before = _read_report(binding)
    report = _decode(binding, raw)
    object.__setattr__(binding, "_created_identity", before)
    return report


def consume_attempt_report(binding: NativeReportBinding) -> C.AttemptEvidence:
    """Consume one complete advanced report and return typed attempt evidence."""
    raw, before = _read_report(binding)
    report = _decode_attempt(binding, raw)
    terminal = report.terminal
    if not terminal.terminal_complete:
        # Advanced promotion is reserved for a complete terminal handoff.  A
        # bridge refusal remains diagnosable through the ordinary terminal
        # consumer, but it is never selectable attempt evidence.
        _reject()
    status = C.Status.PASSED if terminal.native_exit_code == 0 else C.Status.FAILED
    final = terminal.native_exit_code
    result = C.AttemptResult(
        attempt_id=terminal.attempt_id, phase="execution", status=status,
        raw_exit_code=terminal.native_exit_code,
        final_exit_code=final, source_valid=False,
        inventory_complete=report.inventory.complete,
    )
    try:
        evidence = C.AttemptEvidence(
            attempt_id=terminal.attempt_id, result=result,
            inventory=report.inventory, terminal_complete=terminal.terminal_complete,
            parallel_identity=(len(report.workers) > 1 and
                               len({worker for worker, _ in report.workers}) == len(report.workers)),
            runtime_identity=report.runtime_identity,
            project_filter_label=project_filter_label(terminal.project_narrowing),
        )
    except (TypeError, ValueError):
        _reject()
    object.__setattr__(binding, "_evidence_digest", evidence_content_digest(evidence))
    object.__setattr__(binding, "_created_identity", before)
    return evidence


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
