"""Bounded, non-executing repository diagnostics.

The doctor deliberately treats source as untrusted bytes.  It neither imports
repository modules nor resolves native configuration, starts subprocesses,
contacts services, or writes state.  Findings are static hypotheses, never a
parallel-safety certification.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import fields
import heapq
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from . import contracts as C
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
_DOCUMENT_RESERVE = 65536  # Fixed report envelope, scope, readiness, limits, and usage.
_PHYSICAL_NEWLINE = re.compile(r"\r\n|\r|\n")
_TIMING_MISSING = "Timing unavailable: inspect has no checkout identity or per-test history timing input."

# code, pattern, severity, consequence, remediation, verification
_RULES = (
    ("db.per-test-initialization", re.compile(r"\b(?:sqlite3\.)?connect\s*\(|\bcreate_all\s*\("), "medium", "Repeated database creation can dominate tests and hide shared ownership.", "Create expensive database/schema state once per run or worker; reset records per test.", "Run the scoped suite twice with distinct run/worker database identities."),
    ("db.cleanup-ownership", re.compile(r"\b(?:drop_database|drop_all|truncate_all)\s*\("), "medium", "Broad cleanup can destroy a neighbor's database.", "Record the exact database owner before cleanup and only remove that owned namespace.", "Keep a neighbor sentinel database and assert it survives teardown."),
    ("cache.global-flush", re.compile(r"\bflush(?:all|db)\s*\("), "high", "A global cache flush can erase another worker or run.", "Namespace cache keys by run and worker; delete only that namespace.", "Keep a neighbor cache key and assert it survives cleanup."),
    ("resource.fixed-name", re.compile(r"(?:open|Path)\s*\(\s*[\"'][^\"']+[\"']"), "low", "A fixed mutable path can collide across parallel workers.", "Derive files from a run/worker-owned temporary namespace.", "Run two workers concurrently and assert their paths differ."),
    ("network.fixed-port", re.compile(r"\b(?:port\s*=\s*\d{2,5}\b|bind\s*\([^,\n)]{0,200},\s*\d{2,5}\b)"), "medium", "A fixed port can collide with another test or process.", "Ask the OS for an ephemeral port or allocate a run/worker-owned port.", "Run parallel workers and assert no bind collision occurs."),
    ("time.blocking-sleep", re.compile(r"\b(?:time\.)?sleep\s*\("), "low", "Wall-clock sleeps make timing and cancellation nondeterministic.", "Use a fake clock or deterministic synchronization boundary.", "Exercise the scoped test without waiting on wall-clock time."),
    ("network.live-target", re.compile(r"https?://|\b(?:requests|httpx)\.(?:get|post|request)\s*\("), "medium", "A live target makes results dependent on external availability and data.", "Use a local fake or an explicitly declared isolated test service.", "Run with network denial and assert the test uses the declared fake."),
    ("process.detached-child", re.compile(r"(?:start_new_session\s*=\s*True|setsid\s*\(|detach\s*\()"), "high", "Detached children can outlive the admitted process group.", "Keep children in the owned foreground process group and reap them.", "Cancel the scoped run and assert no owned descendant remains."),
    ("fixture.shared-mutation", re.compile(r"@pytest\.fixture\s*\([^\n]*scope\s*=\s*[\"'](?:session|module)[\"']"), "medium", "Long-lived fixtures may leak mutable state between tests.", "Use factories or reset mutable fixture state for each test.", "Run order permutations and assert each test receives clean state."),
    ("selection.unknown-input", re.compile(r"\b(?:os\.environ|subprocess\.(?:run|Popen)|Path\.glob)\b"), "low", "Dynamic inputs may invalidate source-to-test selection evidence.", "Declare the input or make automatic selection fall back to the full gate.", "Change the input and verify the plan widens to full."),
)


def _reason(code: str, message: str, *paths: str) -> C.Reason:
    return C.Reason(code=code, message=message, paths=tuple(paths))


def _safe_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not _CONTROL.search(value)


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


def _readiness(findings: tuple[C.Finding, ...], limitations: tuple[C.Reason, ...]) -> tuple[C.Readiness, ...]:
    parallel_reasons = [_reason("static-evidence-insufficient", "Static inspection cannot prove run/worker isolation.")]
    if findings:
        parallel_reasons.append(_reason("static-evidence-insufficient", "Static findings require repository review before parallel execution."))
    if limitations:
        parallel_reasons.extend(limitations[:1])
    return (
        C.Readiness(area="execution", state="unknown", reasons=(_reason("static-evidence-insufficient", "Doctor does not execute repository code."),)),
        C.Readiness(area="parallel", state="unknown", reasons=tuple(parallel_reasons)),
        C.Readiness(area="selection", state="unknown", reasons=(_reason("unknown-input", "Static scan cannot establish complete selection inputs."),)),
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

    def __init__(self, root: Path, limits: C.ScanLimits):
        self.root, self.limits = root, limits
        self.payload_limit = limits.output_bytes - min(_DOCUMENT_RESERVE, limits.output_bytes // 4)
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
                if pattern.search(line):
                    finding = C.Finding(code=code, severity=severity, confidence="medium", path=rel, line=line_number, evidence_type="static-pattern", consequence=consequence, remediation=remediation, verification=verification)
                    size = _finding_bytes(finding)
                    if len(self.findings) >= limits.findings:
                        self.limit("Doctor finding limit reached.")
                        return
                    if self.output_bytes + size + _OUTPUT_LIMIT_BYTES > self.payload_limit:
                        self._hit_output_limit()
                        return
                    self.findings.append(finding)
                    self.output_bytes += size


def inspect(domain: C.DomainPaths, config: C.ConfigResolution, limits: C.ScanLimits,
            scope: str | None) -> C.DoctorReport:
    """Return bounded static hypotheses without executing code or reading state.

    The frozen interface lacks the checkout identity and per-test duration
    observations needed for domain-scoped timing lookup. Do not infer either.
    """
    if not isinstance(domain, C.DomainPaths) or not isinstance(config, C.ConfigResolution) or not isinstance(limits, C.ScanLimits):
        raise TypeError("inspect requires DomainPaths, ConfigResolution and ScanLimits")
    if any(getattr(limits, field.name) > getattr(C.MAX_SCAN_LIMITS, field.name) for field in fields(C.ScanLimits)):
        raise C.Problem(code="invalid-bound", message="doctor scan limit exceeds the finite maximum", phase=_PHASE)
    root = Path(config.root)
    display_scope = _safe_scope(scope)
    scan = _Scan(root, limits)
    priority = []
    if scope is None and config.config is not None:
        for item in config.config.runner.test_roots:
            try:
                _safe_scope(item)
            except C.Problem:
                scan.skip("Doctor rejected an unsafe priority root.")
                continue
            priority.append(item)
    candidates = scan.discover(display_scope or "", tuple(priority))
    try:
        for relative, size in candidates:
            scan.source(relative, size)
    except _Deadline:
        pass
    scan._add_limitation(_reason("static-evidence-insufficient", "Static inspection cannot certify parallel safety."))
    scan._add_limitation(_reason("static-evidence-insufficient", _TIMING_MISSING))
    limitations = tuple(scan.limitations)
    findings = tuple(scan.findings)
    usage = C.ScanUsage(entries=scan.entries, files=scan.files, file_bytes=scan.total_bytes,
                        total_bytes=scan.total_bytes, findings=len(findings), output_bytes=scan.output_bytes,
                        elapsed_s=time.monotonic() - scan.began, skipped=scan.skipped, truncated=scan.truncated)
    return C.DoctorReport(scope=(() if display_scope is None else (display_scope,)),
                          readiness=_readiness(findings, limitations), findings=findings,
                          limits=limits, usage=usage, limitations=limitations)
