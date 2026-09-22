"""Bounded, non-executing repository diagnostics.

The doctor deliberately treats source as untrusted bytes.  It neither imports
repository modules nor resolves native configuration, starts subprocesses,
contacts services, or writes state.  Findings are static hypotheses, never a
parallel-safety certification.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import fields, replace
import heapq
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from . import render
from .files import _open_dir, _walk_to_parent, read_regular

_PHASE = "doctor"
_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                        "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                        "coverage", ".coverage", ".next", "target", "generated", "graphify-out",
                        ".ssh", ".aws", ".gnupg"})
_PRIVATE_FILES = frozenset({".npmrc", ".netrc", ".pypirc"})
_GENERATED_NAME = re.compile(r"\.(?:min|generated)\.")
_PRIVATE_NAME = re.compile(r"(?:^\.env(?:\.|$)|\.(?:pem|key|p12|pfx)$|^id_|^(?:secrets?|credentials?)(?:[._-]|$))", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIXTURE_DIRS = frozenset({"fixtures", "fixture", "setup", "__fixtures__"})
_CONFIG_NAMES = frozenset({".ptest.toml", "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "package.json"})
_LINE_CHARS = 4096  # A fixed work bound, including regex input; never raised by callers.
_PATH_BYTES = 4096  # Bounds caller/config path evidence before it can enter a report.
# Below this explicit input floor even a useful pathless report has too little
# room for the public envelope plus deterministic truncation evidence.
MIN_DOCTOR_OUTPUT_BYTES = 4096
_PHYSICAL_NEWLINE = re.compile(r"\r\n|\r|\n")
_TIMING_MISSING = "Timing unavailable: inspect has no checkout identity or per-test history timing input."
_CACHE_CLEAR_CALL = re.compile(
    r"(?<![A-Za-z0-9_$])(?P<receiver>[A-Za-z_$][A-Za-z0-9_$]{0,255})\s*\.\s*"
    r"(?:clear|clearAll|clear_all|invalidateAll|invalidate_all)\s*\("
)


def _cache_clear_call(line: str) -> bool:
    """Recognize bounded cache-named receiver clears without type inference."""
    for match in _CACHE_CLEAR_CALL.finditer(line):
        receiver = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", match.group("receiver"))
        if {token.casefold() for token in re.split(r"[_\s]+", receiver)} & {"cache", "caches"}:
            return True
    return False

# code, pattern, severity, consequence, remediation, verification
_RULES = (
    ("db.per-test-initialization", re.compile(r"\b(?:sqlite3\.)?connect\s*\(|\bcreate_all\s*\("), "medium", "Repeated database creation can dominate tests and hide shared ownership.", "Create expensive database/schema state once per run or worker; reset records per test.", "Run the scoped suite twice with distinct run/worker database identities."),
    ("db.cleanup-ownership", re.compile(r"\b(?:drop_database|drop_all|truncate_all)\s*\("), "medium", "Broad cleanup can destroy a neighbor's database.", "Record the exact database owner before cleanup and only remove that owned namespace.", "Keep a neighbor sentinel database and assert it survives teardown."),
    ("cache.global-flush", re.compile(r"\bflush(?:all|db)\s*\(", re.I), "high", "A cache-wide flush or clear may erase another worker/run's entries.", "Verify receiver lifetime and ownership; use checkout/run/worker key namespaces and delete only owned keys, or demonstrate an exclusively owned disposable cache.", "Preserve a neighboring run/worker sentinel during cleanup."),
    ("resource.fixed-name", re.compile(r"(?:open|Path)\s*\(\s*[\"'][^\"']+[\"']"), "low", "A fixed mutable path can collide across parallel workers.", "Derive files from a run/worker-owned temporary namespace.", "Run two workers concurrently and assert their paths differ."),
    ("network.fixed-port", re.compile(r"\b(?:port\s*=\s*\d{2,5}\b|bind\s*\([^,\n)]{0,200},\s*\d{2,5}\b)"), "medium", "A fixed port can collide with another test or process.", "Ask the OS for an ephemeral port or allocate a run/worker-owned port.", "Run parallel workers and assert no bind collision occurs."),
    ("time.blocking-sleep", re.compile(r"\b(?:time\.)?sleep\s*\("), "low", "Wall-clock sleeps make timing and cancellation nondeterministic.", "Use a fake clock or deterministic synchronization boundary.", "Exercise the scoped test without waiting on wall-clock time."),
    ("network.live-target", re.compile(r"https?://|\b(?:requests|httpx)\.(?:get|post|request)\s*\("), "medium", "A live target makes results dependent on external availability and data.", "Use a local fake or an explicitly declared isolated test service.", "Run with network denial and assert the test uses the declared fake."),
    ("process.detached-child", re.compile(r"(?:start_new_session\s*=\s*True|setsid\s*\(|detach\s*\()"), "high", "Detached children can outlive the admitted process group.", "Keep children in the owned foreground process group and reap them.", "Cancel the scoped run and assert no owned descendant remains."),
    ("fixture.shared-mutation", re.compile(r"@pytest\.fixture\s*\([^\n]*scope\s*=\s*[\"'](?:session|module)[\"']"), "medium", "Long-lived fixtures may leak mutable state between tests.", "Use factories or reset mutable fixture state for each test.", "Run order permutations and assert each test receives clean state."),
    ("selection.unknown-input", re.compile(r"\b(?:os\.environ|subprocess\.(?:run|Popen)|Path\.glob)\b"), "low", "Dynamic inputs may invalidate source-to-test selection evidence.", "Declare the input or make automatic selection fall back to the full gate.", "Change the input and verify the plan widens to full."),
)


def _reason(code: str, message: str, *paths: str) -> C.Reason:
    bounded = tuple(path for value in paths if (path := _report_path(value)) is not None)
    return C.Reason(code=code, message=message, paths=bounded)


def _safe_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not _CONTROL.search(value)


def _report_path(value: str) -> str | None:
    """Return bounded, encodable path evidence or omit it without approximation."""
    if (not isinstance(value, str) or not value or not _safe_text(value)
            or len(value.encode("utf-8")) > _PATH_BYTES):
        return None
    return value


def _safe_scope(scope: str | None) -> str | None:
    if scope is None:
        return None
    if (not isinstance(scope, str) or not scope or scope.startswith("/")
            or not _safe_text(scope) or len(scope.encode("utf-8")) > _PATH_BYTES):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path", phase=_PHASE)
    parts = scope.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path", phase=_PHASE)
    return scope


def _readiness(has_findings: bool, has_limitations: bool, *, selection_enabled: bool = True,
               configured: bool = True, declaration: str | None = None) -> tuple[C.Readiness, ...]:
    """Static readiness: unknown unless configuration evidence blocks an area.

    Static inspection ran nothing, so it never reports ready. Selection is
    blocked when disabled or invalid; a missing configuration blocks execution
    and selection. ``declaration`` prefixes repository-relative reason paths.
    """
    repo_paths = () if declaration is None else tuple(
        path for value in (declaration,) if (path := _report_path(value)) is not None)
    if not configured:
        return (
            C.Readiness(area="execution", state="blocked", reasons=(
                _reason("initialization-required",
                        "Project configuration is required; doctor cannot assess execution readiness.",
                        *repo_paths),)),
            C.Readiness(area="parallel", state="unknown", reasons=(
                _reason("static-evidence-insufficient", "Static inspection cannot prove run/worker isolation."),)),
            C.Readiness(area="selection", state="blocked", reasons=(
                _reason("initialization-required",
                        "Project configuration is required; doctor cannot assess selection readiness.",
                        *repo_paths),)),
            C.Readiness(area="timing", state="unknown", reasons=(_reason("static-evidence-insufficient", _TIMING_MISSING),)),
        )
    parallel_reasons = [_reason("static-evidence-insufficient", "Static inspection cannot prove run/worker isolation.")]
    if has_findings:
        parallel_reasons.append(_reason("static-evidence-insufficient", "Static findings require repository review before parallel execution."))
    if has_limitations:
        # Readiness stays actionable without duplicating caller-controlled path
        # evidence from limitations outside the output ledger.
        parallel_reasons.append(_reason(
            "static-evidence-insufficient",
            "Static inspection has limitations; review report limitations.",
        ))
    if selection_enabled:
        selection = C.Readiness(area="selection", state="unknown", reasons=(
            _reason("unknown-input", "Static scan cannot establish complete selection inputs."),))
    else:
        selection = C.Readiness(area="selection", state="blocked", reasons=(
            _reason("selection-disabled",
                    "Automatic selection is disabled or its policy is invalid; "
                    "static inspection cannot establish complete selection inputs.",
                    *repo_paths),))
    return (
        C.Readiness(area="execution", state="unknown", reasons=(
            _reason("static-evidence-insufficient", "Doctor does not execute repository code.",
                    *repo_paths),)),
        C.Readiness(area="parallel", state="unknown", reasons=tuple(parallel_reasons)),
        selection,
        C.Readiness(area="timing", state="unknown", reasons=(_reason("static-evidence-insufficient", _TIMING_MISSING),)),
    )


def timing_bucket(duration_s: float) -> str:
    """Classify an observed duration; 2 seconds starts optimize, 3 remains there.

    This is guidance only. No source marker supplies an observed duration.
    """
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or not math.isfinite(duration_s) or duration_s < 0:
        raise ValueError("duration must be finite and nonnegative")
    if duration_s < 0.5:
        return "healthy"
    if duration_s < 2:
        return "inspect"
    return "optimize" if duration_s <= 3 else "investigate"


def _lines_for_python(text: str, limits: C.ScanLimits, check_deadline) -> set[int] | None:
    try:
        tree = ast.parse(text, mode="exec")
    except (SyntaxError, UnicodeDecodeError, RecursionError, MemoryError, ValueError):
        return None
    pending = [(tree, 0)]
    lines = set()
    count = 0
    while pending:
        check_deadline()
        node, depth = pending.pop()
        count += 1
        if count > limits.ast_nodes or depth > limits.depth:
            return None
        if getattr(node, "lineno", 0):
            lines.add(node.lineno)
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return lines


def _finding_bytes(finding: C.Finding) -> int:
    """UTF-8 bytes for one compact public finding object plus a separator."""
    payload = {
        "code": finding.code, "severity": finding.severity,
        "confidence": finding.confidence, "path": finding.path,
        "line": finding.line, "evidence_type": finding.evidence_type,
        "consequence": finding.consequence, "remediation": finding.remediation,
        "verification": finding.verification,
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def _reason_bytes(reason: C.Reason) -> int:
    """UTF-8 bytes for one compact public limitation object plus a separator."""
    payload = {"code": reason.code, "message": reason.message, "paths": reason.paths}
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


_OUTPUT_LIMIT_REASON = _reason("scan-limit", "Doctor diagnostic output limit reached.")
_OUTPUT_LIMIT_BYTES = _reason_bytes(_OUTPUT_LIMIT_REASON)


def _compact_report_bytes(report: C.DoctorReport) -> bytes:
    """Exact public doctor JSON bytes governed by ``output_bytes``.

    The public codec owns escaping and its envelope, so sizing a hand-rolled
    data object could undercount multi-byte metadata after JSON escaping.
    """
    # CLI fixture domains are the largest public domain envelope: a fixed
    # 32-character identifier plus the fixture flag.  Reserve that exact
    # shape while collecting evidence so either fixture or normal CLI output
    # remains within the requested cap; no filesystem path enters public JSON.
    return render.render_doctor_json(
        report, domain={"id": "0" * 32, "fixture": True})


def _usage_envelope(limits: C.ScanLimits) -> C.ScanUsage:
    """Widest bounded usage representation reserved before payload admission."""
    return C.ScanUsage(
        entries=limits.entries,
        files=limits.files,
        file_bytes=limits.total_bytes,
        total_bytes=limits.total_bytes,
        findings=limits.findings,
        output_bytes=limits.output_bytes,
        elapsed_s=1.7976931348623157e308,
        skipped=999_999_999_999_999_999,
        truncated=True,
    )


def _scope_tuple(scope: str | None) -> tuple[str, ...]:
    return () if scope is None else (scope,)


def _report_envelope_bytes(limits: C.ScanLimits, scope: str | None) -> int:
    """Conservative serialized envelope size, excluding ledger payload items."""
    report = C.DoctorReport(
        scope=_scope_tuple(scope),
        readiness=_readiness(True, True),
        findings=(),
        limits=limits,
        usage=_usage_envelope(limits),
        limitations=(),
    )
    return len(_compact_report_bytes(report))


def _minimum_document_bytes(limits: C.ScanLimits, scope: str | None) -> int:
    """Smallest safe capped document for this exact limits/scope envelope."""
    report = C.DoctorReport(
        scope=_scope_tuple(scope),
        readiness=_readiness(False, True),
        findings=(),
        limits=limits,
        usage=_usage_envelope(limits),
        limitations=(_OUTPUT_LIMIT_REASON,),
    )
    return len(_compact_report_bytes(report))


class _Deadline(Exception):
    pass


@contextmanager
def _directory(root: Path, relative: str):
    """Use shared no-follow traversal for every component, including root."""
    absolute = root.absolute()
    anchor = _open_dir(Path(absolute.anchor))
    opened = None
    try:
        parts = [*absolute.parts[1:], *(relative.split("/") if relative else ())]
        # _walk_to_parent opens everything except its final (unused) component.
        opened = _walk_to_parent(anchor, [*parts, "_doctor_unused"])
        yield opened
    finally:
        if opened is not None and opened != anchor:
            os.close(opened)
        os.close(anchor)


def _excluded(relative: str) -> bool:
    return any(part in _SKIP_DIRS or part in _PRIVATE_FILES or _PRIVATE_NAME.search(part)
               or _GENERATED_NAME.search(part) for part in relative.split("/"))


def _priority(relative: str, test_roots: tuple[str, ...]) -> tuple[int, str]:
    if any(relative == root or relative.startswith(root + "/") for root in test_roots):
        return 0, relative
    parts = relative.split("/")
    name = parts[-1]
    if any(part in _FIXTURE_DIRS for part in parts) or name in {"conftest.py", "setup.py"} or name.startswith(("conftest.", "setup.")):
        return 1, relative
    if name in _CONFIG_NAMES or name.startswith(("vitest.config.", "vite.config.", "jest.config.")):
        return 2, relative
    return 3, relative


class _Scan:
    """Mutable accounting for one bounded, read-only scan."""

    def __init__(self, root: Path, limits: C.ScanLimits, scope: str | None,
                 deadline: float | None = None, admit_output: bool = True):
        self.root, self.limits = root, limits
        self.payload_limit = max(0, limits.output_bytes - _report_envelope_bytes(limits, scope))
        self.deadline = deadline
        # Workspace child scans debit count/byte/time budgets locally while the
        # global ledger alone admits serialized output; standalone scans keep
        # local output admission.
        self.admit_output = admit_output
        self.began = time.monotonic()
        self.entries = self.files = self.total_bytes = self.skipped = self.output_bytes = 0
        self.truncated = False
        self.findings: list[C.Finding] = []
        self.limitations: list[C.Reason] = []
        self._limitation_set: set[C.Reason] = set()
        self._output_limited = False

    def _hit_output_limit(self):
        self.truncated = True
        if self._output_limited:
            return
        self._output_limited = True
        if not self.admit_output:
            return
        size = _OUTPUT_LIMIT_BYTES
        # A caller may supply a payload budget smaller than any Reason. In
        # that degenerate case truncated remains the only representable cap
        # evidence; never exceed the requested budget to describe the cap.
        if self.output_bytes + size <= self.payload_limit:
            self.limitations.append(_OUTPUT_LIMIT_REASON)
            self._limitation_set.add(_OUTPUT_LIMIT_REASON)
            self.output_bytes += size

    def _add_limitation(self, reason: C.Reason):
        if reason in self._limitation_set or self._output_limited:
            return
        if not self.admit_output:
            self.limitations.append(reason)
            self._limitation_set.add(reason)
            return
        size = _reason_bytes(reason)
        reserve = _OUTPUT_LIMIT_BYTES
        if self.output_bytes + size + reserve > self.payload_limit:
            self._hit_output_limit()
            return
        self.limitations.append(reason)
        self._limitation_set.add(reason)
        self.output_bytes += size

    def check_deadline(self):
        if time.monotonic() - self.began >= self.limits.elapsed_s:
            self.limit("Doctor elapsed-time limit reached.")
            raise _Deadline
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.limit("Doctor elapsed-time limit reached.")
            raise _Deadline

    def limit(self, message: str, *paths: str):
        self.truncated = True
        self._add_limitation(_reason("scan-limit", message, *paths))

    def skip(self, message: str, *paths: str, code: str = "unsafe-path"):
        self.skipped += 1
        self._add_limitation(_reason(code, message, *paths))

    def discover(self, start: str, priority: tuple[str, ...]) -> list[tuple[str, int]]:
        # No whole-directory sorted(scandir(...)): materialize at most the entry
        # budget, then sort those bounded names. Unknown remainder is reported.
        queue = [(*_priority(rel, priority), 0) for rel in dict.fromkeys((*priority, start))]
        heapq.heapify(queue)
        pending_priority = set(priority)
        candidates = []
        seen = set()
        try:
            while queue:
                self.check_deadline()
                if self.entries >= self.limits.entries:
                    self.limit("Doctor entry limit reached.")
                    break
                _, relative, depth = heapq.heappop(queue)
                if depth > self.limits.depth or len(relative.split("/")) > self.limits.depth + 1:
                    self.limit("Doctor directory-depth limit reached.")
                    self.skipped += 1
                    continue
                if _excluded(relative):
                    self.skip("Doctor skipped private or generated content.", code="static-evidence-insufficient")
                    continue
                is_priority = relative in pending_priority
                try:
                    with _directory(self.root, relative) as descriptor:
                        stamp = os.fstat(descriptor)
                        identity = (stamp.st_dev, stamp.st_ino)
                        pending_priority.discard(relative)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        children = []
                        with os.scandir(descriptor) as iterator:
                            while self.entries < self.limits.entries:
                                self.check_deadline()
                                try:
                                    item = next(iterator)
                                except StopIteration:
                                    break
                                self.entries += 1
                                self.check_deadline()
                                if not _safe_text(item.name):
                                    self.skip("Doctor skipped an unencodable or control-character name.")
                                    continue
                                rel = relative + "/" + item.name if relative else item.name
                                if _excluded(rel):
                                    self.skip("Doctor skipped private or generated content.", code="static-evidence-insufficient")
                                    continue
                                try:
                                    child_stamp = item.stat(follow_symlinks=False)
                                except OSError:
                                    self.skip("Doctor could not stat an entry safely.", rel)
                                    continue
                                children.append((rel, child_stamp.st_mode, child_stamp.st_size))
                            else:
                                self.limit("Doctor entry limit reached; directory discovery is incomplete.")
                        for rel, mode, size in sorted(children):
                            self.check_deadline()
                            if stat.S_ISDIR(mode):
                                heapq.heappush(queue, (*_priority(rel, priority), depth + 1))
                            elif stat.S_ISREG(mode):
                                candidates.append((rel, size))
                            else:
                                self.skip("Doctor skipped a symbolic link or non-regular file.", rel)
                except (OSError, C.Problem) as exc:
                    if is_priority:
                        absent = isinstance(exc, C.Problem) and exc.code == "state-unavailable"
                        # Shared helpers use unsafe-path for missing intermediate
                        # components too; never echo their untrusted exception text.
                        label = "absent or unsafe" if not absent else "absent"
                        self.skip(f"Doctor priority root is {label}.", relative)
                        pending_priority.discard(relative)
                    else:
                        self.skip("Doctor could not inspect a directory safely.")
        except _Deadline:
            pass
        for relative in sorted(pending_priority):
            if _safe_text(relative) and not _excluded(relative):
                self.limit("Doctor priority root was unreached within scan bounds.", relative)
            else:
                self.limit("Doctor excluded a private or unsafe priority root.")
        return sorted(candidates, key=lambda item: _priority(item[0], priority))

    def source(self, rel: str, size: int):
        self.check_deadline()
        limits = self.limits
        if self.files >= limits.files or size > limits.file_bytes or self.total_bytes + size > limits.total_bytes:
            self.limit("Doctor file-byte or file-count limit reached.", rel)
            self.skipped += 1
            return
        try:
            # Reopened component-wise: a rename/symlink swap after enumeration
            # cannot switch this read to an outside tree.
            raw = read_regular(self.root, rel, min(limits.file_bytes, limits.total_bytes - self.total_bytes) + 1)
        except C.Problem:
            self.skip("Doctor skipped an unsafe file.", rel)
            return
        if len(raw) > limits.file_bytes or self.total_bytes + len(raw) > limits.total_bytes:
            self.limit("Doctor skipped an oversized file.", rel)
            self.skipped += 1
            return
        self.files += 1
        self.total_bytes += len(raw)
        self.check_deadline()
        if b"\x00" in raw:
            self.skip("Doctor skipped binary content.", code="static-evidence-insufficient")
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.skip("Doctor skipped non-UTF-8 source.", rel, code="static-evidence-insufficient")
            return
        syntax_lines = _lines_for_python(text, limits, self.check_deadline) if rel.endswith(".py") else None
        if rel.endswith(".py") and syntax_lines is None:
            self.skip("Doctor could not safely analyze source syntax.", rel, code="static-evidence-insufficient")
            return
        for line_number, line in enumerate(_PHYSICAL_NEWLINE.split(text), 1):
            self.check_deadline()
            if line_number > limits.ast_nodes:
                self.limit("Doctor source-line limit reached.", rel)
                break
            if len(line) > _LINE_CHARS:
                self.limit("Doctor skipped source line exceeding the fixed line-work bound.", rel)
                continue
            if syntax_lines is not None and line_number not in syntax_lines:
                continue
            for code, pattern, severity, consequence, remediation, verification in _RULES:
                self.check_deadline()
                if pattern.search(line) or (code == "cache.global-flush" and _cache_clear_call(line)):
                    finding = C.Finding(code=code, severity=severity, confidence="medium", path=_report_path(rel), line=line_number, evidence_type="static-pattern", consequence=consequence, remediation=remediation, verification=verification)
                    size = _finding_bytes(finding)
                    if len(self.findings) >= limits.findings:
                        self.limit("Doctor finding limit reached.")
                        return
                    if self.admit_output and (
                            self.output_bytes + size + _OUTPUT_LIMIT_BYTES > self.payload_limit):
                        self._hit_output_limit()
                        return
                    self.findings.append(finding)
                    self.output_bytes += size


def _assemble_direct(
    scan: _Scan,
    scope: tuple[str, ...] | str | None,
    readiness: tuple[C.Readiness, ...],
    findings: tuple[C.Finding, ...],
    limitations: tuple[C.Reason, ...],
    elapsed_s: float,
) -> C.DoctorReport:
    """Build a report whose usage byte count equals its compact serialization."""
    scope_tuple = scope if isinstance(scope, tuple) else _scope_tuple(scope)
    output_bytes = 0
    while True:
        usage = C.ScanUsage(
            entries=scan.entries,
            files=scan.files,
            file_bytes=scan.total_bytes,
            total_bytes=scan.total_bytes,
            findings=len(findings),
            output_bytes=output_bytes,
            elapsed_s=elapsed_s,
            skipped=scan.skipped,
            truncated=scan.truncated,
        )
        report = C.DoctorReport(
            scope=scope_tuple,
            readiness=readiness,
            findings=findings,
            limits=scan.limits,
            usage=usage,
            limitations=limitations,
        )
        serialized_bytes = len(_compact_report_bytes(report))
        if serialized_bytes == output_bytes:
            return report
        output_bytes = serialized_bytes


def _assemble_report(
    scan: _Scan,
    scope: str | None,
    findings: tuple[C.Finding, ...],
    limitations: tuple[C.Reason, ...],
    elapsed_s: float,
    *,
    selection_enabled: bool = True,
    configured: bool = True,
    declaration: str | None = None,
) -> C.DoctorReport:
    """Build a report whose usage byte count equals its compact serialization."""
    return _assemble_direct(
        scan,
        scope,
        _readiness(bool(findings), bool(limitations),
                   selection_enabled=selection_enabled, configured=configured,
                   declaration=declaration),
        findings,
        limitations,
        elapsed_s,
    )


def _evict_to_fit(scan: _Scan, scope: tuple[str, ...],
                  readiness: tuple[C.Readiness, ...] | None,
                  selection_enabled: bool = True, configured: bool = True,
                  declaration: str | None = None) -> C.DoctorReport:
    """Enforce the complete-document cap even if envelope fields evolve."""
    findings = list(scan.findings)
    limitations = list(scan.limitations)
    elapsed_s = time.monotonic() - scan.began
    while True:
        if readiness is None:
            report = _assemble_report(
                scan,
                scope,
                tuple(findings),
                tuple(limitations),
                elapsed_s,
                selection_enabled=selection_enabled,
                configured=configured,
                declaration=declaration,
            )
        else:
            report = _assemble_direct(
                scan, scope, readiness, tuple(findings), tuple(limitations), elapsed_s)
        if report.usage.output_bytes <= scan.limits.output_bytes:
            return report
        scan.truncated = True
        if _OUTPUT_LIMIT_REASON not in limitations:
            limitations.append(_OUTPUT_LIMIT_REASON)
        if findings:
            findings.pop()
            continue
        removable = next(
            (index for index in range(len(limitations) - 1, -1, -1)
             if limitations[index] != _OUTPUT_LIMIT_REASON),
            None,
        )
        if removable is not None:
            limitations.pop(removable)
            continue
        raise C.Problem(
            code="invalid-bound",
            message="doctor output limit cannot hold the report envelope",
            phase=_PHASE,
        )


def _finalize_report(scan: _Scan, scope: str | None, *, selection_enabled: bool = True,
                     configured: bool = True, declaration: str | None = None) -> C.DoctorReport:
    """Enforce the complete-document cap even if envelope fields evolve."""
    return _evict_to_fit(scan, _scope_tuple(scope), None,
                         selection_enabled=selection_enabled, configured=configured,
                         declaration=declaration)


def _run_scan(scan: _Scan, start: str, priority: tuple[str, ...]) -> None:
    """Run bounded discovery and source matching, then append static caveats."""
    candidates = scan.discover(start, priority)
    try:
        for relative, size in candidates:
            scan.source(relative, size)
    except _Deadline:
        pass
    scan._add_limitation(_reason("static-evidence-insufficient", "Static inspection cannot certify parallel safety."))
    scan._add_limitation(_reason("static-evidence-insufficient", _TIMING_MISSING))


def _check_limits(limits: C.ScanLimits) -> None:
    if any(getattr(limits, field.name) > getattr(C.MAX_SCAN_LIMITS, field.name) for field in fields(C.ScanLimits)):
        raise C.Problem(code="invalid-bound", message="doctor scan limit exceeds the finite maximum", phase=_PHASE)
    if limits.output_bytes < MIN_DOCTOR_OUTPUT_BYTES:
        raise C.Problem(code="invalid-bound", message="doctor output limit is below the finite minimum", phase=_PHASE)


def inspect(domain: C.DomainPaths, config: C.ConfigResolution, limits: C.ScanLimits,
            scope: str | None) -> C.DoctorReport:
    """Return bounded static hypotheses without executing code or reading state.

    The frozen interface lacks the checkout identity and per-test duration
    observations needed for domain-scoped timing lookup. Do not infer either.
    """
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.ConfigResolution) or not isinstance(limits, C.ScanLimits):
        raise TypeError("inspect requires DomainPaths, ConfigResolution and ScanLimits")
    _check_limits(limits)
    root = Path(config.root)
    display_scope = _safe_scope(scope)
    if limits.output_bytes < _minimum_document_bytes(limits, display_scope):
        raise C.Problem(code="invalid-bound", message="doctor output limit cannot hold the report envelope", phase=_PHASE)
    scan = _Scan(root, limits, display_scope)
    priority = []
    if scope is None and config.config is not None:
        for item in config.config.runner.test_roots:
            try:
                _safe_scope(item)
            except C.Problem:
                scan.skip("Doctor rejected an unsafe priority root.")
                continue
            priority.append(item)
    _run_scan(scan, display_scope or "", tuple(priority))
    resolved = config.config
    return _finalize_report(
        scan, display_scope,
        selection_enabled=True if resolved is None else bool(resolved.selection.enabled),
        configured=resolved is not None,
    )


@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    declaration: str       # "." or root-relative v2 declaration
    local_scope: str | None
    report: C.DoctorReport
    config_problem: C.Problem | None


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    scope: tuple[str, ...]
    repositories: tuple[RepositoryInspection, ...]
    aggregate: C.DoctorReport


_DRIVE_QUALIFIED = re.compile(r"^[A-Za-z]:")


def _workspace_scope_value(scope: str | None) -> str | None:
    """Validate an explicit scope without echoing hostile input."""
    if scope is None:
        return None
    if (not isinstance(scope, str) or not scope or scope.startswith("/")
            or "\\" in scope or "\x00" in scope or not _safe_text(scope)
            or len(scope.encode("utf-8")) > _PATH_BYTES
            or _DRIVE_QUALIFIED.match(scope)):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path",
                        phase=_PHASE)
    parts = scope.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path",
                        phase=_PHASE)
    return scope


def _match_declaration(declarations: tuple[str, ...], scope: str) -> str | None:
    """Match an exact declaration or ``declaration + "/"`` prefix."""
    for declaration in declarations:
        if scope == declaration or scope.startswith(declaration + "/"):
            return declaration
    return None


def _rebase_path(declaration: str, value: str | None) -> str | None:
    """Prefix a child-relative path; omit it when it cannot stay bounded."""
    if value is None:
        return None
    if declaration == ".":
        return _report_path(value)
    return _report_path(declaration + "/" + value)


def _rebase_reason(declaration: str, reason: C.Reason) -> C.Reason:
    return C.Reason(
        code=reason.code,
        message=reason.message,
        paths=tuple(path for value in reason.paths
                    if (path := _rebase_path(declaration, value)) is not None),
    )


def _share(remaining: int, slots: int) -> int:
    """Fair per-child consumable share: ceil(R / K), capped by R."""
    if slots <= 0 or remaining <= 0:
        return 0
    return min(remaining, -(-remaining // slots))


def _admit_finding(ledger: _Scan, finding: C.Finding) -> bool:
    """Admit one rebased finding to the global output ledger."""
    size = _finding_bytes(finding)
    if len(ledger.findings) >= ledger.limits.findings:
        ledger.limit("Doctor finding limit reached.")
        return False
    if ledger.output_bytes + size + _OUTPUT_LIMIT_BYTES > ledger.payload_limit:
        ledger._hit_output_limit()
        return False
    ledger.findings.append(finding)
    ledger.output_bytes += size
    return True


def _diagnostic_report(scan: _Scan, scope: tuple[str, ...], declaration: str,
                       code: str, message: str) -> C.DoctorReport:
    """Visible row for an unscanned child: execution/selection blocked."""
    paths = tuple(path for value in (declaration,)
                  if (path := _report_path(value)) is not None)
    readiness = (
        C.Readiness(area="execution", state="blocked",
                    reasons=(_reason(code, message, *paths),)),
        C.Readiness(area="parallel", state="unknown", reasons=(
            _reason("static-evidence-insufficient",
                    "Static inspection cannot prove run/worker isolation."),)),
        C.Readiness(area="selection", state="blocked",
                    reasons=(_reason(code, message, *paths),)),
        C.Readiness(area="timing", state="unknown",
                    reasons=(_reason("static-evidence-insufficient", _TIMING_MISSING),)),
    )
    limitations = (_reason(code, message, *paths),)
    return _assemble_direct(scan, scope, readiness, (), limitations, 0.0)


def _deadline_report(scan: _Scan, scope: tuple[str, ...], declaration: str) -> C.DoctorReport:
    """Visible, non-conclusive row for a child not inspected before expiry."""
    reason = _reason("scan-limit", "Declared child was not inspected before the workspace deadline.", declaration)
    readiness = tuple(C.Readiness(area=area, state="unknown", reasons=(reason,))
                      for area in ("execution", "parallel", "selection", "timing"))
    return _assemble_direct(scan, scope, readiness, (), (reason,), 0.0)


def _config_budget_report(scan: _Scan, scope: tuple[str, ...], declaration: str) -> C.DoctorReport:
    """Visible unknown row when no complete config fits its fair byte share."""
    reason = _reason(
        "scan-limit",
        "Declared child configuration was not inspected within the workspace byte budget.",
        declaration,
    )
    readiness = tuple(C.Readiness(area=area, state="unknown", reasons=(reason,))
                      for area in ("execution", "parallel", "selection", "timing"))
    return _assemble_direct(scan, scope, readiness, (), (reason,), 0.0)


def _aggregate_readiness(
        repositories: tuple[RepositoryInspection, ...]) -> tuple[C.Readiness, ...]:
    """Area-wise worst evidenced state in repository declaration order."""
    areas = ("execution", "parallel", "selection", "timing")
    aggregated = []
    for area in areas:
        states = [next(item.state for item in repo.report.readiness if item.area == area)
                  for repo in repositories]
        if states and all(state == "ready-for-declared-capability" for state in states):
            state = "ready-for-declared-capability"
        elif any(state == "blocked" for state in states):
            state = "blocked"
        else:
            state = "unknown"
        reasons: list[C.Reason] = []
        for repo in repositories:
            for item in repo.report.readiness:
                if item.area == area:
                    for reason in item.reasons:
                        if reason not in reasons:
                            reasons.append(reason)
        aggregated.append(C.Readiness(area=area, state=state, reasons=tuple(reasons)))
    return tuple(aggregated)


def inspect_workspace(domain: C.DomainPaths, resolution: C.ConfigResolution,
                      limits: C.ScanLimits, scope: str | None) -> WorkspaceInspection:
    """Inspect one standalone repository or declared v2 children in order.

    Only children explicitly listed by the resolved root v2 manifest are
    considered; the monorepo root is never scanned as an implicit project.
    The public JSON stays one aggregate DoctorReport.
    """
    from . import monorepo as monorepo_api

    if not isinstance(domain, C.DomainPaths) or not isinstance(resolution, C.ConfigResolution) \
            or not isinstance(limits, C.ScanLimits):
        raise TypeError("inspect_workspace requires DomainPaths, ConfigResolution and ScanLimits")
    _check_limits(limits)
    requested = _workspace_scope_value(scope)
    manifest = resolution.monorepo
    is_monorepo = manifest is not None
    if resolution.problem is not None:
        unconfigured = (not is_monorepo and resolution.config is None
                        and resolution.problem.code == "initialization-required")
        if not unconfigured:
            raise resolution.problem
    root = Path(resolution.root)
    ledger_scope = requested
    if limits.output_bytes < _minimum_document_bytes(limits, ledger_scope):
        raise C.Problem(code="invalid-bound",
                        message="doctor output limit cannot hold the report envelope", phase=_PHASE)
    if not is_monorepo:
        report = inspect(domain, resolution, limits, requested)
        declaration = "."
        inspection = RepositoryInspection(
            declaration=declaration, local_scope=requested, report=report,
            config_problem=(resolution.problem if resolution.config is None else None),
        )
        workspace_scope = report.scope
        return WorkspaceInspection(scope=workspace_scope, repositories=(inspection,),
                                   aggregate=report)
    declarations = tuple(manifest.children)
    if requested is None:
        selected = tuple((declaration, None) for declaration in declarations)
        display: tuple[str, ...] = ()
    else:
        hit = _match_declaration(declarations, requested)
        if hit is None:
            raise C.Problem(code="invalid-config",
                            message="doctor scope does not select a declared monorepo child",
                            phase=_PHASE)
        local = None if requested == hit else requested[len(hit) + 1:]
        selected = ((hit, local),)
        display = (requested,)
    # Start the global deadline before child diagnostics; every slice below
    # debits this one workspace-wide budget.
    ledger = _Scan(root, limits, requested)
    global_deadline = ledger.began + limits.elapsed_s
    repositories: list[RepositoryInspection] = []
    for index, (declaration, local) in enumerate(selected):
        child_scope = (declaration,) if local is None else (requested or declaration,)
        if time.monotonic() >= global_deadline:
            report = _deadline_report(_Scan(root, limits, requested), child_scope, declaration)
            ledger.truncated = True
            for reason in report.limitations:
                ledger._add_limitation(reason)
            repositories.append(RepositoryInspection(
                declaration=declaration, local_scope=local, report=report, config_problem=None))
            continue
        # Allocate config reads before diagnosis so malformed configurations
        # cannot consume unreported workspace bytes or starve later children.
        slots = len(selected) - index
        config_allowance = _share(max(0, limits.total_bytes - ledger.total_bytes), slots)
        diagnosis = monorepo_api.diagnose_child(
            root, declaration, global_deadline, config_allowance)
        ledger.total_bytes += diagnosis.config_bytes
        if diagnosis.directory is not None and local is not None:
            try:
                monorepo_api.validate_child_scope(diagnosis.directory, local)
            except C.Problem:
                raise C.Problem(code="unsafe-path", message="doctor scope selects an unsafe monorepo child",
                                phase=_PHASE) from None
        if diagnosis.kind == "deadline":
            report = _deadline_report(_Scan(root, limits, requested), child_scope, declaration)
            ledger.truncated = True
            for reason in report.limitations:
                ledger._add_limitation(reason)
            repositories.append(RepositoryInspection(
                declaration=declaration, local_scope=local, report=report, config_problem=None))
            continue
        if diagnosis.kind == "budget":
            report = _config_budget_report(_Scan(root, limits, requested), child_scope, declaration)
            ledger.truncated = True
            for reason in report.limitations:
                ledger._add_limitation(reason)
            repositories.append(RepositoryInspection(
                declaration=declaration, local_scope=local, report=report, config_problem=None))
            continue
        # A reachable child remains useful static evidence even when its
        # native manifest is missing, malformed, or unsafe.  It cannot establish
        # execution/selection readiness and receives no config priorities,
        # but doctor must not suppress its source findings.  Unsafe and
        # unavailable directories remain unscannable.
        scannable_without_config = (
            diagnosis.kind in {"missing", "invalid-config", "unsafe"}
            and diagnosis.directory is not None)
        if diagnosis.kind != "ok" and not scannable_without_config:
            if requested is not None and diagnosis.kind == "unsafe":
                raise C.Problem(code="unsafe-path",
                                message="doctor scope selects an unsafe monorepo child",
                                phase=_PHASE)
            if diagnosis.kind == "unsafe":
                code, message = "unsafe-path", "Declared child is unsafe; it was not scanned."
            elif diagnosis.kind == "invalid-config":
                code, message = "invalid-config", \
                    "Declared child configuration is invalid; it was not scanned."
            else:
                code, message = "state-unavailable", \
                    "Declared child is unavailable; it was not scanned."
            fresh = _Scan(root, limits, requested)
            report = _diagnostic_report(fresh, child_scope, declaration, code, message)
            ledger.truncated = True
            for reason in report.limitations:
                ledger._add_limitation(reason)
            repositories.append(RepositoryInspection(
                declaration=declaration, local_scope=local, report=report,
                config_problem=diagnosis.problem))
            continue
        # Remaining scannable slots without extra I/O: every not-yet
        # visited selection is assumed scannable until its own diagnosis.
        # Every declaration that has not yet been processed retains a fair
        # share. A missing/invalid earlier declaration consumed diagnostic
        # output only, not its later siblings' scan allowance.
        used_entries = ledger.entries
        used_files = ledger.files
        used_bytes = ledger.total_bytes
        # Child findings are admitted to the public ledger only after paths
        # are rebased, but their quota is consumed at discovery time.  Count
        # previously observed child findings here so a later child cannot be
        # handed allowance that an earlier child already used.
        used_findings = sum(len(item.report.findings) for item in repositories)
        child_limits = replace(
            limits,
            entries=_share(limits.entries - used_entries, slots),
            files=_share(limits.files - used_files, slots),
            total_bytes=_share(limits.total_bytes - used_bytes, slots),
            findings=_share(limits.findings - used_findings, slots),
            elapsed_s=max(0.0, min(global_deadline - time.monotonic(),
                                   (global_deadline - time.monotonic()) / slots if slots else 0.0)),
        )
        child = _Scan(diagnosis.directory, child_limits,
                      requested if local is None else local,
                      deadline=global_deadline, admit_output=False)
        priority: tuple[str, ...] = ()
        if local is None and diagnosis.config is not None:
            priorities = []
            for item in diagnosis.config.runner.test_roots:
                try:
                    _safe_scope(item)
                except C.Problem:
                    child.skip("Doctor rejected an unsafe priority root.")
                    continue
                priorities.append(item)
            priority = tuple(priorities)
        if diagnosis.kind != "ok":
            child._add_limitation(_reason(
                "invalid-config" if diagnosis.kind == "invalid-config" else
                "unsafe-path" if diagnosis.kind == "unsafe" else "state-unavailable",
                "Declared child configuration is invalid; execution and selection are blocked."
                if diagnosis.kind == "invalid-config"
                else "Declared child configuration is unsafe; execution and selection are blocked."
                if diagnosis.kind == "unsafe"
                else "Declared child configuration is unavailable; execution and selection are blocked.",
            ))
        _run_scan(child, local or "", priority)
        rebased_findings = tuple(
            replace(finding, path=_rebase_path(declaration, finding.path))
            for finding in child.findings)
        rebased_limitations = tuple(
            _rebase_reason(declaration, reason) for reason in child.limitations)
        if child.truncated:
            # A capped child stays visible with unknown affected readiness
            # and a root-relative scan-limit; later children are not omitted.
            capped = _reason(
                "scan-limit",
                "Declared child reached its fair scan share; later children "
                "continue from the global remainder.", declaration)
            if capped not in rebased_limitations:
                rebased_limitations = (*rebased_limitations, capped)
        readiness = _readiness(
            bool(rebased_findings), bool(rebased_limitations),
            selection_enabled=bool(diagnosis.config and diagnosis.config.selection.enabled),
            configured=diagnosis.kind == "ok", declaration=declaration)
        report = _assemble_direct(child, child_scope, readiness, rebased_findings,
                                  rebased_limitations, time.monotonic() - child.began)
        ledger.entries += child.entries
        ledger.files += child.files
        ledger.total_bytes += child.total_bytes
        ledger.skipped += child.skipped
        ledger.truncated = ledger.truncated or child.truncated
        repositories.append(RepositoryInspection(
            declaration=declaration, local_scope=local, report=report,
            config_problem=None if diagnosis.kind == "ok" else diagnosis.problem))
    limitation_seed: list[C.Reason] = []
    if requested is None:
        # An ordinary bounded limitation: reason codes stay in the frozen
        # schema set, so the scope note reuses a static-evidence code.
        limitation_seed.append(_reason(
            "static-evidence-insufficient",
            "Only declared children were inspected; monorepo root content was excluded."))
    def human_label_truncated(declaration: str) -> bool:
        shown = render.terminal_text(declaration).replace("|", "\\|")
        return (len(shown.encode("utf-8")) > 96
                or shown.endswith("[truncated]"))

    if any(human_label_truncated(repo.declaration) for repo in repositories):
        limitation_seed.append(_reason(
            "static-evidence-insufficient",
            "Repository label was truncated in human output; see report limitations for this condition."))
    for repo in repositories:
        for reason in repo.report.limitations:
            if reason not in limitation_seed:
                limitation_seed.append(reason)
    for repo in repositories:
        for finding in repo.report.findings:
            if not _admit_finding(ledger, finding):
                break
        else:
            continue
        break
    for reason in limitation_seed:
        ledger._add_limitation(reason)
    aggregate = _evict_to_fit(ledger, display, _aggregate_readiness(tuple(repositories)))
    return WorkspaceInspection(scope=display, repositories=tuple(repositories), aggregate=aggregate)
