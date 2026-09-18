"""Bounded, non-executing repository diagnostics.

The doctor deliberately treats source as untrusted bytes.  It neither imports
repository modules nor resolves native configuration, starts subprocesses,
contacts services, or writes state.  Findings are static hypotheses, never a
parallel-safety certification.
"""
from __future__ import annotations

import ast
import os
import re
import stat
import time
from collections import deque
from pathlib import Path

from . import contracts as C
from .files import read_regular

_PHASE = "doctor"
_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__"})
_DOTENV = re.compile(r"(?:^|/)\.env(?:\.|$)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# code, pattern, severity, consequence, remediation, verification
_RULES = (
    ("db.per-test-initialization", re.compile(r"\b(?:sqlite3\.)?connect\s*\(|\bcreate_all\s*\("), "medium", "Repeated database creation can dominate tests and hide shared ownership.", "Create expensive database/schema state once per run or worker; reset records per test.", "Run the scoped suite twice with distinct run/worker database identities."),
    ("db.cleanup-ownership", re.compile(r"\b(?:drop_database|drop_all|truncate_all)\s*\("), "medium", "Broad cleanup can destroy a neighbor's database.", "Record the exact database owner before cleanup and only remove that owned namespace.", "Keep a neighbor sentinel database and assert it survives teardown."),
    ("cache.global-flush", re.compile(r"\bflush(?:all|db)\s*\("), "high", "A global cache flush can erase another worker or run.", "Namespace cache keys by run and worker; delete only that namespace.", "Keep a neighbor cache key and assert it survives cleanup."),
    ("resource.fixed-name", re.compile(r"(?:open|Path)\s*\(\s*[\"'][^\"']+[\"']"), "low", "A fixed mutable path can collide across parallel workers.", "Derive files from a run/worker-owned temporary namespace.", "Run two workers concurrently and assert their paths differ."),
    ("network.fixed-port", re.compile(r"(?:port\s*=\s*|bind\s*\([^\n]*[, :]\s*)\d{2,5}\b"), "medium", "A fixed port can collide with another test or process.", "Ask the OS for an ephemeral port or allocate a run/worker-owned port.", "Run parallel workers and assert no bind collision occurs."),
    ("time.blocking-sleep", re.compile(r"\b(?:time\.)?sleep\s*\("), "low", "Wall-clock sleeps make timing and cancellation nondeterministic.", "Use a fake clock or deterministic synchronization boundary.", "Exercise the scoped test without waiting on wall-clock time."),
    ("network.live-target", re.compile(r"https?://|\b(?:requests|httpx)\.(?:get|post|request)\s*\("), "medium", "A live target makes results dependent on external availability and data.", "Use a local fake or an explicitly declared isolated test service.", "Run with network denial and assert the test uses the declared fake."),
    ("process.detached-child", re.compile(r"(?:start_new_session\s*=\s*True|setsid\s*\(|detach\s*\()"), "high", "Detached children can outlive the admitted process group.", "Keep children in the owned foreground process group and reap them.", "Cancel the scoped run and assert no owned descendant remains."),
    ("fixture.shared-mutation", re.compile(r"@pytest\.fixture\s*\([^\n]*scope\s*=\s*[\"'](?:session|module)[\"']"), "medium", "Long-lived fixtures may leak mutable state between tests.", "Use factories or reset mutable fixture state for each test.", "Run order permutations and assert each test receives clean state."),
    ("timing.slow-test", re.compile(r"@pytest\.mark\.(?:slow|integration)\b"), "low", "A marked slow test needs measured timing and an explicit budget.", "Record setup/call/teardown timing; investigate tests over 3 seconds.", "Run the scoped test and inspect its recorded duration."),
    ("selection.unknown-input", re.compile(r"\b(?:os\.environ|subprocess\.(?:run|Popen)|Path\.glob)\b"), "low", "Dynamic inputs may invalidate source-to-test selection evidence.", "Declare the input or make automatic selection fall back to the full gate.", "Change the input and verify the plan widens to full."),
)


def _reason(code: str, message: str, *paths: str) -> C.Reason:
    return C.Reason(code=code, message=message, paths=tuple(paths))


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_scope(root: Path, scope: str | None) -> tuple[Path, str | None]:
    if scope is None:
        return root, None
    if not isinstance(scope, str) or not scope or scope.startswith("/") or _CONTROL.search(scope):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path", phase=_PHASE)
    parts = scope.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise C.Problem(code="unsafe-path", message="doctor scope is not a safe relative path", phase=_PHASE)
    return root.joinpath(*parts), scope


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
        C.Readiness(area="timing", state="unknown", reasons=(_reason("static-evidence-insufficient", "No persisted timing evidence was read."),)),
    )


def _lines_for_python(raw: bytes, limit: int) -> set[int] | None:
    try:
        tree = ast.parse(raw.decode("utf-8"), mode="exec")
    except (SyntaxError, UnicodeDecodeError):
        return None
    count = sum(1 for _ in ast.walk(tree))
    if count > limit:
        return None
    return {getattr(node, "lineno", 0) for node in ast.walk(tree) if getattr(node, "lineno", 0)}


def _finding_bytes(finding: C.Finding) -> int:
    """Conservative UTF-8 budget for the renderer-owned finding payload."""
    fields = (finding.code, finding.severity, finding.confidence, finding.path or "",
              finding.evidence_type, finding.consequence, finding.remediation,
              finding.verification)
    return sum(len(field.encode("utf-8")) for field in fields) + 32


def inspect(domain: C.DomainPaths, config: C.ConfigResolution, limits: C.ScanLimits,
            scope: str | None) -> C.DoctorReport:
    """Return bounded static hypotheses for ``config.root`` without side effects."""
    del domain  # Explicit input prevents accidental normal-domain resolution; static doctor reads no state.
    if not isinstance(config, C.ConfigResolution) or not isinstance(limits, C.ScanLimits):
        raise TypeError("inspect requires ConfigResolution and ScanLimits")
    for field in ("entries", "files", "file_bytes", "total_bytes"):
        if getattr(limits, field) > getattr(C.MAX_SCAN_LIMITS, field):
            raise C.Problem(code="invalid-bound", message="doctor scan limit exceeds the finite maximum", phase=_PHASE)
    root = Path(config.root)
    start, display_scope = _safe_scope(root, scope)
    began = time.monotonic()
    entries = files = file_bytes = total_bytes = skipped = 0
    truncated = False
    limitations: list[C.Reason] = []
    findings: list[C.Finding] = []
    output_bytes = 0
    # Inspect declared test roots before the broad project walk.  Config already
    # validates these as project-relative paths; duplicates are suppressed by
    # the device/inode set below.
    priority = []
    if scope is None and config.config is not None:
        priority = [root.joinpath(*item.split("/")) for item in config.config.runner.test_roots]
    queue: deque[tuple[Path, int]] = deque((path, 0) for path in priority)
    queue.append((start, 0))
    seen: set[tuple[int, int]] = set()

    while queue:
        if time.monotonic() - began >= limits.elapsed_s:
            truncated = True
            limitations.append(_reason("scan-limit", "Doctor elapsed-time limit reached."))
            break
        directory, depth = queue.popleft()
        if depth > limits.depth:
            skipped += 1
            truncated = True
            limitations.append(_reason("scan-limit", "Doctor directory-depth limit reached.", _relative(root, directory)))
            continue
        try:
            stamp = os.lstat(directory)
        except OSError:
            skipped += 1
            limitations.append(_reason("unsafe-path", "Doctor could not inspect a directory safely."))
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            skipped += 1
            limitations.append(_reason("unsafe-path", "Doctor scope is not a regular directory."))
            continue
        identity = (stamp.st_dev, stamp.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            skipped += 1
            limitations.append(_reason("unsafe-path", "Doctor could not enumerate a directory safely."))
            continue
        for item in children:
            if entries >= limits.entries:
                truncated = True
                limitations.append(_reason("scan-limit", "Doctor entry limit reached."))
                queue.clear()
                break
            entries += 1
            path = Path(item.path)
            if _CONTROL.search(item.name):
                skipped += 1
                limitations.append(_reason("unsafe-path", "Doctor skipped a control-character path."))
                continue
            rel = _relative(root, path)
            try:
                child_stamp = item.stat(follow_symlinks=False)
            except OSError:
                skipped += 1
                limitations.append(_reason("unsafe-path", "Doctor could not stat an entry safely.", rel))
                continue
            if stat.S_ISLNK(child_stamp.st_mode):
                skipped += 1
                limitations.append(_reason("unsafe-path", "Doctor skipped a symbolic link.", rel))
                continue
            if stat.S_ISDIR(child_stamp.st_mode):
                if item.name not in _SKIP_DIRS:
                    queue.append((path, depth + 1))
                continue
            if not stat.S_ISREG(child_stamp.st_mode):
                skipped += 1
                limitations.append(_reason("unsafe-path", "Doctor skipped a non-regular file.", rel))
                continue
            if _DOTENV.search(rel):
                skipped += 1
                limitations.append(_reason("static-evidence-insufficient", "Doctor skipped dotenv-like content.", rel))
                continue
            if files >= limits.files or child_stamp.st_size > limits.file_bytes or total_bytes + child_stamp.st_size > limits.total_bytes:
                skipped += 1
                truncated = True
                limitations.append(_reason("scan-limit", "Doctor file-byte or file-count limit reached.", rel))
                continue
            try:
                raw = read_regular(root, rel, limits.file_bytes + 1)
            except C.Problem:
                skipped += 1
                limitations.append(_reason("unsafe-path", "Doctor skipped an unsafe file.", rel))
                continue
            if len(raw) > limits.file_bytes:
                skipped += 1
                truncated = True
                limitations.append(_reason("scan-limit", "Doctor skipped an oversized file.", rel))
                continue
            files += 1
            file_bytes += len(raw)
            total_bytes += len(raw)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                limitations.append(_reason("static-evidence-insufficient", "Doctor skipped non-UTF-8 source.", rel))
                continue
            if rel.endswith(".py"):
                syntax_lines = _lines_for_python(raw, limits.ast_nodes)
            elif rel.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")):
                syntax_lines = set(range(1, text.count("\n") + 2)) if text.count("\n") + 1 <= limits.ast_nodes else None
            else:
                syntax_lines = set(range(1, text.count("\n") + 2))
            if syntax_lines is None:
                skipped += 1
                limitations.append(_reason("static-evidence-insufficient", "Doctor could not safely analyze source syntax.", rel))
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line_number not in syntax_lines:
                    continue
                for code, pattern, severity, consequence, remediation, verification in _RULES:
                    if len(findings) >= limits.findings:
                        truncated = True
                        limitations.append(_reason("scan-limit", "Doctor finding limit reached."))
                        break
                    if pattern.search(line):
                        finding = C.Finding(code=code, severity=severity, confidence="medium", path=rel, line=line_number, evidence_type="static-pattern", consequence=consequence, remediation=remediation, verification=verification)
                        if output_bytes + _finding_bytes(finding) > limits.output_bytes:
                            truncated = True
                            limitations.append(_reason("scan-limit", "Doctor output-byte limit reached."))
                            break
                        findings.append(finding)
                        output_bytes += _finding_bytes(finding)
                if len(findings) >= limits.findings:
                    break

    # The absence of patterns is not evidence that concurrent resource
    # ownership is correct; make that limitation durable in every report.
    limitations.append(_reason("static-evidence-insufficient", "Static inspection cannot certify parallel safety."))
    unique_limitations = tuple(dict.fromkeys(limitations))
    usage = C.ScanUsage(entries=entries, files=files, file_bytes=file_bytes, total_bytes=total_bytes,
                        findings=len(findings), output_bytes=output_bytes, elapsed_s=time.monotonic() - began,
                        skipped=skipped, truncated=truncated)
    return C.DoctorReport(scope=(() if display_scope is None else (display_scope,)),
                          readiness=_readiness(tuple(findings), unique_limitations), findings=tuple(findings),
                          limits=limits, usage=usage, limitations=unique_limitations)
