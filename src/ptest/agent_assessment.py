"""Bounded evidence packets, strict single-packet validation, floor scoring.

Three-pass shape (collect, validate, score):

1. :func:`build_packets` collects one bounded packet per declared selected
   child in manifest order. It reads regular files only, never follows
   symlinks, excludes instruction/secret/private/dependency/generated
   content, enforces 64 files / 512 KiB per child / 64 KiB per file / 1 MiB
   prompt caps, and records explicit coverage counts plus SHA-256 excerpt
   identities. Dependency provenance is static text only: declarations and
   authoritative locks are distinguished, the local environment is never
   executed or imported (recorded ``uninspectable``), ptest's own runtime
   environment is never treated as project evidence, and anything
   unprovable stays ``unknown``.
2. :func:`parse_assessment` validates one normalized model-prose payload
   strictly against one packet, reusing the frozen ``PublicDocument``
   contract (``ptest.agent-assessment/v1``) instead of a duplicate schema,
   then binds every citation to collected excerpt identity and ranges,
   and rejects stale packets, model-supplied commands/scores, raw
   provider/publication fields, and every ``not-applicable`` row.
   Raw N/A is unsupported in v1 (a quoted line proves nothing about
   applicability; leave the row ``unknown``). Only pure :func:`score`
   keeps N/A support for a future authoritative path.
3. :func:`score` computes ``satisfied / (all - justified N/A)`` with
   integer floor; ``unknown`` stays in the denominator and zero applicable
   rows yield no score.

Child authority is preserved throughout: packets keep their own
declaration/project identity in manifest order, and validation binds
exactly one payload to exactly one packet.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from .files import read_regular

_PHASE = "validation"

MAX_FILES_PER_CHILD = 64
MAX_BYTES_PER_CHILD = 512 * 1024
MAX_BYTES_PER_FILE = 64 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_WALK_ENTRIES = 20000

# Directories never descended into (doctor admission classes plus agent
# instruction/configuration locations and ptest private runtime state).
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".pipeline", "graphify-out", ".ptest",
    ".claude", ".agents", ".codex", ".opencode", ".gemini",
    ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "target", "coverage", ".coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".next",
    ".ssh", ".aws", ".gnupg",
})
# Files never admitted at any level: agent instructions plus the private
# credential names doctor already recognizes.
_EXCLUDED_FILES = frozenset({
    "AGENTS.md", "CLAUDE.md", "GEMINI.md",
    ".npmrc", ".netrc", ".pypirc",
})
_PRIVATE_NAME = re.compile(
    r"(?:^\.env(?:\.|$)|\.(?:pem|key|p12|pfx)$|^id_|"
    r"^(?:secrets?|credentials?)(?:[._-]|$))", re.I)
_GENERATED_NAME = re.compile(r"\.(?:min|generated)\.")

# Dependency declaration/lock filenames observed statically (presence and
# bounded text only; never installed, executed, or imported).
_DECLARATIONS = {
    "pyproject.toml": ("python", "declared"),
    "package.json": ("node", "declared"),
    "Cargo.toml": ("rust", "declared"),
    "go.mod": ("go", "declared"),
}
_LOCKS = {
    "uv.lock": "python-lock",
    "poetry.lock": "python-lock",
    "pdm.lock": "python-lock",
    "package-lock.json": "node-lock",
    "pnpm-lock.yaml": "node-lock",
    "yarn.lock": "node-lock",
    "Cargo.lock": "rust-lock",
    "go.sum": "go-lock",
}
_LOCK_WANT = {
    "python": "python-lock", "node": "node-lock",
    "rust": "rust-lock", "go": "go-lock",
}
_UNSUPPORTED_MARKERS = frozenset({
    "Gemfile", "CMakeLists.txt", "meson.build", "build.gradle",
    "setup.py", "setup.cfg",
})

_VALID_STATUSES = frozenset({"satisfied", "gap", "unknown", "not-applicable"})

# Raw payload keys the model must never supply. The public codec projects
# additive unknowns away silently; this layer rejects them so a smuggled
# command, headline, execution proof, or score override cannot pass as a
# validated assessment.
_FORBIDDEN_KEYS = frozenset({
    "command", "commands", "observed_command", "observed_output",
    "headline", "execution_proof", "exit_code", "exit_status",
    "score_override", "raw_output", "shell", "argv",
})

# Exact raw model-response shapes: model prose only (rationale, findings,
# suggested changes, recipe IDs, citations, limitations). The raw boundary
# enforces these BEFORE public projection so an unknown field anywhere is
# rejected, never projected away. The raw assessment deliberately omits
# ``provider`` and ``publication`` (ptest-owned: the CLI attaches the
# actual selected provider metadata and the actual report publication
# result to the final PublicDocument) and the raw child omits ``score``
# (ptest computes it after validation, before constructing the validated
# document).
_RAW_ENVELOPE_FIELDS = frozenset({
    "schema_version", "kind", "ptest_version", "domain", "data", "error",
})
_RAW_ASSESSMENT_FIELDS = frozenset({
    "schema", "children", "limitations",
})
_RAW_CHILD_FIELDS = frozenset({
    "project_id", "scope", "packet_sha256", "rows", "findings",
    "limitations",
})
_RAW_ROW_FIELDS = frozenset({
    "id", "status", "rationale", "evidence",
})
_RAW_CITATION_FIELDS = frozenset({
    "path", "start_line", "end_line", "sha256",
})
_RAW_FINDING_FIELDS = frozenset({
    "id", "summary", "suggested_change", "recipe_id", "evidence",
})
_RAW_LIMITATION_FIELDS = frozenset({
    "code", "message", "paths",
})

# ptest-owned placeholders injected ONLY to satisfy the frozen public
# contract during internal validation. Never taken from model input and
# never returned: parse_assessment yields ChildAssessment (which carries
# neither field).
_ASSESSMENT_PROVIDER = {
    "name": "claude",
    "cli_version": "0.0.0-ptest-internal",
    "profile": "ptest-internal",
}
_ASSESSMENT_PUBLICATION = {
    "status": "created",
    "path": "recommendations.md",
    "sha256": "00" * 32,
}

_LINK_RE = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
_AUTOLINK_RE = re.compile(
    r"<(?:https?://[^<>\s]*|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)>")
_HTML_RE = re.compile(r"<!--|</?[A-Za-z][^<>\n]*>")
_HEADLINE_RE = re.compile(r"(?m)^[ \t]*#{1,6}(?:\s|$)")
_PERCENT_RE = re.compile(r"\d\s*%")
_EXEC_CLAIM_RE = re.compile(
    r"exit\s*code|exit\s*status|test\s*output|observed|\bpytest\b"
    r"|\bptest\b|\bpassed\b|\bfailed\b|\bexecuted\b|\bverified\b",
    re.IGNORECASE)


def _fail(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE,
                     retryable=False)


def _check_relpath(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a nonempty string")
    if value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError(f"{name} must be root-relative")
    if re.match(r"[A-Za-z]:", value):
        raise ValueError(f"{name} must be root-relative")
    if value != ".":
        for part in value.split("/"):
            if part in ("", ".", ".."):
                raise ValueError(f"{name} is not normalized")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} is not valid UTF-8") from None
    return value


def _check_hex(value: object, name: str, length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if len(value) != length or any(
            c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be {length} lowercase hex")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceLimits:
    """Per-child admission caps (spec defaults, all strictly positive)."""

    max_files_per_child: int = MAX_FILES_PER_CHILD
    max_bytes_per_child: int = MAX_BYTES_PER_CHILD
    max_bytes_per_file: int = MAX_BYTES_PER_FILE
    max_prompt_bytes: int = MAX_PROMPT_BYTES

    def __post_init__(self) -> None:
        for field in ("max_files_per_child", "max_bytes_per_child",
                      "max_bytes_per_file", "max_prompt_bytes"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"limits.{field} must be int")
            if value <= 0:
                raise ValueError(f"limits.{field} must be positive")


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    """One admitted bounded text span with content identity."""

    path: str
    start_line: int
    end_line: int
    sha256: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path",
                           _check_relpath(self.path, "excerpt.path"))
        for field in ("start_line", "end_line"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"excerpt.{field} must be int")
            if value < 1:
                raise ValueError(f"excerpt.{field} must be >= 1")
        if self.start_line > self.end_line:
            raise ValueError("excerpt has an inverted line interval")
        object.__setattr__(self, "sha256",
                           _check_hex(self.sha256, "excerpt.sha256", 64))
        if not isinstance(self.text, str):
            raise TypeError("excerpt.text must be str")


@dataclass(frozen=True, slots=True)
class DependencyFact:
    """One static dependency observation; unprovable stays unknown."""

    ecosystem: str
    status: str
    ref_path: str | None
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.ecosystem, str) or not self.ecosystem:
            raise TypeError("dependency.ecosystem must be nonempty str")
        if self.status not in ("declared", "locked", "missing",
                               "unsupported", "uninspectable"):
            raise ValueError("dependency.status is unknown")
        if self.ref_path is not None:
            object.__setattr__(self, "ref_path",
                               _check_relpath(self.ref_path,
                                              "dependency.ref_path"))
        if not isinstance(self.detail, str) or not self.detail:
            raise TypeError("dependency.detail must be nonempty str")


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Bounded, identity-bound source evidence for one child."""

    declaration: str
    project_id: str
    scope: str
    packet_sha256: str
    excerpts: tuple
    dependencies: tuple
    runner_kind: str
    excluded_count: int
    truncated_count: int
    file_count: int
    byte_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "declaration",
                           _check_relpath(self.declaration,
                                          "packet.declaration"))
        object.__setattr__(self, "project_id",
                           _check_hex(self.project_id,
                                      "packet.project_id", 32))
        object.__setattr__(self, "scope",
                           _check_relpath(self.scope, "packet.scope"))
        object.__setattr__(self, "packet_sha256",
                           _check_hex(self.packet_sha256,
                                      "packet.packet_sha256", 64))
        excerpts = self.excerpts
        if not isinstance(excerpts, (tuple, list)):
            raise TypeError("packet.excerpts must be a tuple")
        for excerpt in tuple(excerpts):
            if not isinstance(excerpt, SourceExcerpt):
                raise TypeError("packet.excerpts entries must be "
                                "SourceExcerpt")
        object.__setattr__(self, "excerpts", tuple(excerpts))
        dependencies = self.dependencies
        if not isinstance(dependencies, (tuple, list)):
            raise TypeError("packet.dependencies must be a tuple")
        for fact in tuple(dependencies):
            if not isinstance(fact, DependencyFact):
                raise TypeError("packet.dependencies entries must be "
                                "DependencyFact")
        object.__setattr__(self, "dependencies", tuple(dependencies))
        if not isinstance(self.runner_kind, str) or not self.runner_kind:
            raise TypeError("packet.runner_kind must be nonempty str")
        for field in ("excluded_count", "truncated_count", "file_count",
                      "byte_count"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"packet.{field} must be int")
            if value < 0:
                raise ValueError(f"packet.{field} must be >= 0")


@dataclass(frozen=True, slots=True)
class Citation:
    path: str
    start_line: int
    end_line: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path",
                           _check_relpath(self.path, "citation.path"))
        for field in ("start_line", "end_line"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"citation.{field} must be int")
            if value < 1:
                raise ValueError(f"citation.{field} must be >= 1")
        if self.start_line > self.end_line:
            raise ValueError("citation has an inverted line interval")
        object.__setattr__(self, "sha256",
                           _check_hex(self.sha256, "citation.sha256", 64))


@dataclass(frozen=True, slots=True)
class AssessmentRow:
    id: str
    status: str
    rationale: str
    evidence: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise TypeError("row.id must be nonempty str")
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"row status {self.status!r} is unknown")
        if not isinstance(self.rationale, str) or not self.rationale:
            raise TypeError("row.rationale must be nonempty str")
        evidence = self.evidence
        if not isinstance(evidence, (tuple, list)):
            raise TypeError("row.evidence must be a tuple")
        for citation in tuple(evidence):
            if not isinstance(citation, Citation):
                raise TypeError("row.evidence entries must be Citation")
        object.__setattr__(self, "evidence", tuple(evidence))


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    summary: str
    suggested_change: str
    recipe_id: str | None
    evidence: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise TypeError("finding.id must be nonempty str")
        for field in ("summary", "suggested_change"):
            if not isinstance(getattr(self, field), str) or not getattr(
                    self, field):
                raise TypeError(f"finding.{field} must be nonempty str")
        if self.recipe_id is not None and (
                not isinstance(self.recipe_id, str) or not self.recipe_id):
            raise TypeError("finding.recipe_id must be str or None")
        evidence = self.evidence
        if not isinstance(evidence, (tuple, list)):
            raise TypeError("finding.evidence must be a tuple")
        for citation in tuple(evidence):
            if not isinstance(citation, Citation):
                raise TypeError("finding.evidence entries must be Citation")
        object.__setattr__(self, "evidence", tuple(evidence))


@dataclass(frozen=True, slots=True)
class Score:
    satisfied: int
    applicable: int
    percent: int

    def __post_init__(self) -> None:
        for field in ("satisfied", "applicable", "percent"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"score.{field} must be int")
        if not 0 <= self.satisfied <= 11:
            raise ValueError("score.satisfied is out of range")
        if not 1 <= self.applicable <= 11:
            raise ValueError("score.applicable is out of range")
        if not 0 <= self.percent <= 100:
            raise ValueError("score.percent is out of range")


@dataclass(frozen=True, slots=True)
class ChildAssessment:
    packet_sha256: str
    project_id: str
    scope: str
    rows: tuple
    findings: tuple
    score: Score | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "packet_sha256",
                           _check_hex(self.packet_sha256,
                                      "assessment.packet_sha256", 64))
        object.__setattr__(self, "project_id",
                           _check_hex(self.project_id,
                                      "assessment.project_id", 32))
        object.__setattr__(self, "scope",
                           _check_relpath(self.scope, "assessment.scope"))
        rows = self.rows
        if not isinstance(rows, (tuple, list)):
            raise TypeError("assessment.rows must be a tuple")
        for row in tuple(rows):
            if not isinstance(row, AssessmentRow):
                raise TypeError("assessment.rows entries must be "
                                "AssessmentRow")
        object.__setattr__(self, "rows", tuple(rows))
        findings = self.findings
        if not isinstance(findings, (tuple, list)):
            raise TypeError("assessment.findings must be a tuple")
        for finding in tuple(findings):
            if not isinstance(finding, Finding):
                raise TypeError("assessment.findings entries must be "
                                "Finding")
        object.__setattr__(self, "findings", tuple(findings))
        if self.score is not None and not isinstance(self.score, Score):
            raise TypeError("assessment.score must be Score or None")


def _excluded_name(name: str) -> bool:
    return (name in _EXCLUDED_DIRS or name in _EXCLUDED_FILES
            or bool(_PRIVATE_NAME.search(name))
            or bool(_GENERATED_NAME.search(name)))


def _iter_regular_files(root: Path, rel: str, entries: list) -> None:
    """Collect ``(relative, size)`` regular files without following links.

    Symlinks, sockets, devices, FIFOs, and excluded names are recorded in
    ``entries`` as skipped markers instead of being opened.
    """
    stack = [rel]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(root / current if current else root) as it:
                names = sorted((entry.name, entry) for entry in it)
        except OSError:
            entries.append(("skip", current))
            continue
        for name, entry in names:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                entries.append(("skip", current))
                return
            child = f"{current}/{name}" if current else name
            if _excluded_name(name):
                entries.append(("skip", child))
                continue
            try:
                is_link = entry.is_symlink()
            except OSError:
                entries.append(("skip", child))
                continue
            if is_link:
                entries.append(("skip", child))
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                entries.append(("skip", child))
                continue
            if is_dir:
                stack.append(child)
                continue
            try:
                stamp = entry.stat(follow_symlinks=False)
            except OSError:
                entries.append(("skip", child))
                continue
            if not stat.S_ISREG(stamp.st_mode):
                entries.append(("skip", child))
                continue
            entries.append(("file", child, stamp.st_size))


def _truncate_to_valid_utf8(raw: bytes, cap: int) -> tuple[bytes, bool]:
    """Cut ``raw`` to ``cap`` bytes without splitting a code point."""
    if len(raw) <= cap:
        return raw, False
    head = raw[:cap]
    for tail in range(min(3, len(head)), -1, -1):
        try:
            head[:len(head) - tail].decode("utf-8")
            return head[:len(head) - tail], True
        except UnicodeDecodeError:
            continue
    return b"", True


def _packet_identity(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _packet_body(declaration: str, project_id: str, scope: str,
                 excerpts: list[SourceExcerpt],
                 dependencies: tuple[DependencyFact, ...],
                 runner_kind: str, excluded: int, truncated: int,
                 byte_count: int) -> dict:
    return {
        "declaration": declaration,
        "project_id": project_id,
        "scope": scope,
        "excerpts": [{"path": e.path, "start_line": e.start_line,
                      "end_line": e.end_line, "sha256": e.sha256,
                      "text": e.text} for e in excerpts],
        "dependencies": [{"ecosystem": d.ecosystem, "status": d.status,
                          "ref_path": d.ref_path, "detail": d.detail}
                         for d in dependencies],
        "runner_kind": runner_kind,
        "excluded_count": excluded,
        "truncated_count": truncated,
        "file_count": len(excerpts),
        "byte_count": byte_count,
    }


def _dependency_facts(names: set[str], prefix: str) -> tuple:
    """Static declaration/lock facts; environment stays uninspectable."""
    facts: list[DependencyFact] = []
    seen_ecosystems: set[str] = set()
    for filename, (ecosystem, _) in sorted(_DECLARATIONS.items()):
        if filename in names:
            seen_ecosystems.add(ecosystem)
            facts.append(DependencyFact(
                ecosystem=ecosystem, status="declared",
                ref_path=f"{prefix}{filename}" if prefix else filename,
                detail=f"Static declaration {filename}; provenance "
                       "declaration, content unexecuted."))
    for marker in sorted(_UNSUPPORTED_MARKERS):
        if marker in names:
            facts.append(DependencyFact(
                ecosystem="project", status="unsupported",
                ref_path=f"{prefix}{marker}" if prefix else marker,
                detail=f"{marker} is not a supported declaration; "
                       "prerequisite unknown."))
    present_locks = {name for name in names if name in _LOCKS}
    for ecosystem in sorted(seen_ecosystems):
        want = _LOCK_WANT[ecosystem]
        hit = sorted(name for name in present_locks
                     if _LOCKS[name] == want)
        if hit:
            facts.append(DependencyFact(
                ecosystem=want, status="locked",
                ref_path=f"{prefix}{hit[0]}" if prefix else hit[0],
                detail=f"Authoritative lock {hit[0]}; provenance lockfile, "
                       "content unexecuted."))
        else:
            facts.append(DependencyFact(
                ecosystem=want, status="missing", ref_path=None,
                detail=f"No authoritative {ecosystem} lockfile admitted; "
                       "lock provenance unknown."))
    if not seen_ecosystems:
        facts.append(DependencyFact(
            ecosystem="project", status="missing", ref_path=None,
            detail="No recognized declaration file admitted in packet; "
                   "dependency provenance unknown."))
    facts.append(DependencyFact(
        ecosystem="environment", status="uninspectable", ref_path=None,
        detail="Local environments are not executed, imported, or "
               "installed; the ptest runtime environment is never project "
               "evidence, so installed prerequisites stay unknown."))
    return tuple(facts)


def build_packets(workspace, resolution,
                  limits: EvidenceLimits = EvidenceLimits()
                  ) -> tuple[EvidencePacket, ...]:
    """Collect one bounded packet per declared selected child, in order."""
    from . import doctor as doctor_api

    if not isinstance(workspace, doctor_api.WorkspaceInspection):
        raise TypeError("workspace must be doctor.WorkspaceInspection")
    if not isinstance(resolution, C.ConfigResolution):
        raise TypeError("resolution must be ConfigResolution")
    if not isinstance(limits, EvidenceLimits):
        raise TypeError("limits must be EvidenceLimits")
    root = Path(resolution.root)
    packets: list[EvidencePacket] = []
    if len(workspace.repositories) > 256:
        raise _fail("invalid-bound",
                    "workspace declares more than 256 children")
    for repo in workspace.repositories:
        if repo.declaration != "." and not isinstance(repo.config, C.Config):
            raise _fail("invalid-config",
                        "declared child configuration is unavailable; "
                        "assessment packets cannot be built")
    for repo in workspace.repositories:
        packets.append(_build_one_packet(root, repo, resolution, limits))
    return tuple(packets)


def _build_one_packet(root: Path, repo, resolution,
                      limits: EvidenceLimits) -> EvidencePacket:
    declaration = repo.declaration
    child_rel = "" if declaration == "." else declaration
    child_root = root if declaration == "." else root / declaration
    entries: list = []
    _iter_regular_files(child_root, "", entries)
    regular = [(rel, size) for kind, *rest in entries
               if kind == "file" for rel, size in [tuple(rest)]]
    skipped = sum(1 for entry in entries if entry[0] == "skip")
    regular.sort(key=lambda item: item[0])
    prefix = "" if declaration == "." else declaration + "/"

    excerpts: list[SourceExcerpt] = []
    names: set[str] = set()
    byte_count = 0
    truncated = 0
    for rel, _size in regular:
        if len(excerpts) >= limits.max_files_per_child:
            truncated += 1
            continue
        try:
            raw = read_regular(
                child_root, rel,
                min(limits.max_bytes_per_file,
                    limits.max_bytes_per_child) + 1)
        except C.Problem:
            skipped += 1
            continue
        chunk, was_cut = _truncate_to_valid_utf8(
            raw, limits.max_bytes_per_file)
        if byte_count + len(chunk) > limits.max_bytes_per_child:
            truncated += 1
            continue
        if b"\x00" in chunk:
            skipped += 1
            continue
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        if was_cut:
            truncated += 1
        lines = text.splitlines() or [""]
        excerpt = SourceExcerpt(
            path=f"{prefix}{rel}", start_line=1,
            end_line=len(lines),
            sha256=hashlib.sha256(chunk).hexdigest(), text=text)
        excerpts.append(excerpt)
        names.add(rel.rsplit("/", 1)[-1])
        byte_count += len(chunk)

    config = repo.config
    if declaration == "." and config is None:
        # Preserve compatibility for standalone WorkspaceInspection values
        # created by existing callers before RepositoryInspection exposed it.
        config = resolution.config
    if config is not None:
        runner_kind = config.runner.kind.value
        project_id = config.project_id
    else:
        runner_kind = "unknown"
        project_id = "0" * 32
    dependencies = _dependency_facts(names, prefix)

    # Enforce the prompt cap by dropping trailing excerpts; coverage counts
    # stay explicit so partial evidence is visible, not silent.
    body = _packet_body(declaration, project_id, declaration, excerpts,
                        dependencies, runner_kind, skipped, truncated,
                        byte_count)
    while (len(json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8"))
            > limits.max_prompt_bytes and len(excerpts) > 1):
        dropped = excerpts.pop()
        byte_count -= len(dropped.text.encode("utf-8"))
        truncated += 1
        body = _packet_body(declaration, project_id, declaration,
                            excerpts, dependencies, runner_kind, skipped,
                            truncated, byte_count)
    digest = _packet_identity(body)
    return EvidencePacket(
        declaration=declaration, project_id=project_id, scope=declaration,
        packet_sha256=digest, excerpts=tuple(excerpts),
        dependencies=dependencies, runner_kind=runner_kind,
        excluded_count=skipped, truncated_count=truncated,
        file_count=len(excerpts), byte_count=byte_count)


def score(rows: tuple[AssessmentRow, ...]) -> Score | None:
    """Floor score: satisfied / (all - justified N/A); None when empty."""
    if not isinstance(rows, (tuple, list)):
        raise TypeError("rows must be a tuple of AssessmentRow")
    rows = tuple(rows)
    for row in rows:
        if not isinstance(row, AssessmentRow):
            raise TypeError("rows entries must be AssessmentRow")
    justified_na = sum(1 for row in rows
                       if row.status == "not-applicable")
    applicable = len(rows) - justified_na
    if applicable == 0:
        return None
    satisfied = sum(1 for row in rows if row.status == "satisfied")
    return Score(satisfied=satisfied, applicable=applicable,
                 percent=(100 * satisfied) // applicable)


def _reject_forbidden_keys(node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_KEYS:
                raise _fail("invalid-assessment",
                            "assessment carries a model-supplied field")
            _reject_forbidden_keys(value)
    elif isinstance(node, list):
        for value in node:
            _reject_forbidden_keys(value)


def _require_exact_keys(item: object, allowed: frozenset,
                        ctx: str) -> None:
    """Require exactly the allowed keys: missing and unknown both fail."""
    if not isinstance(item, dict):
        raise _fail("invalid-assessment", f"{ctx} must be an object")
    for key in allowed:
        if key not in item:
            raise _fail("invalid-assessment",
                        f"{ctx} is missing {key!r}")
    for key in item:
        if key not in allowed:
            raise _fail("invalid-assessment",
                        f"{ctx} carries an unknown field")


def _reject_extra_raw_keys(envelope: object) -> None:
    """Enforce exact raw shapes recursively before public projection."""
    _require_exact_keys(envelope, _RAW_ENVELOPE_FIELDS, "assessment")
    data = envelope["data"]
    _require_exact_keys(data, _RAW_ASSESSMENT_FIELDS, "assessment")
    children = data["children"]
    if not isinstance(children, list):
        raise _fail("invalid-assessment",
                    "assessment.children must be a list")
    for position, child in enumerate(children):
        ctx = f"assessment.children[{position}]"
        if not isinstance(child, dict):
            raise _fail("invalid-assessment", f"{ctx} must be an object")
        if "score" in child:
            raise _fail("invalid-assessment",
                        "assessment carries a model-supplied field")
        _require_exact_keys(child, _RAW_CHILD_FIELDS, ctx)
        rows = child["rows"]
        if not isinstance(rows, list):
            raise _fail("invalid-assessment",
                        f"{ctx}.rows must be a list")
        for index, entry in enumerate(rows):
            row_ctx = f"{ctx}.rows[{index}]"
            _require_exact_keys(entry, _RAW_ROW_FIELDS, row_ctx)
            evidence = entry.get("evidence")
            if not isinstance(evidence, list):
                raise _fail("invalid-assessment",
                            f"{row_ctx}.evidence must be a list")
            for number, citation in enumerate(evidence):
                _require_exact_keys(citation, _RAW_CITATION_FIELDS,
                                    f"{row_ctx}.evidence[{number}]")
        findings = child["findings"]
        if not isinstance(findings, list):
            raise _fail("invalid-assessment",
                        f"{ctx}.findings must be a list")
        for index, entry in enumerate(findings):
            finding_ctx = f"{ctx}.findings[{index}]"
            _require_exact_keys(entry, _RAW_FINDING_FIELDS, finding_ctx)
            evidence = entry.get("evidence")
            if not isinstance(evidence, list):
                raise _fail("invalid-assessment",
                            f"{finding_ctx}.evidence must be a list")
            for number, citation in enumerate(evidence):
                _require_exact_keys(citation, _RAW_CITATION_FIELDS,
                                    f"{finding_ctx}.evidence[{number}]")
        limitations = child["limitations"]
        if not isinstance(limitations, list):
            raise _fail("invalid-assessment",
                        f"{ctx}.limitations must be a list")
        for index, entry in enumerate(limitations):
            _require_exact_keys(entry, _RAW_LIMITATION_FIELDS,
                                f"{ctx}.limitations[{index}]")
    limitations = data["limitations"]
    if not isinstance(limitations, list):
        raise _fail("invalid-assessment",
                    "assessment.limitations must be a list")
    for index, entry in enumerate(limitations):
        _require_exact_keys(entry, _RAW_LIMITATION_FIELDS,
                            f"assessment.limitations[{index}]")


def _provisional_score(rows: object) -> dict | None:
    """Compute the ptest score from raw row statuses for injection.

    Malformed rows yield a placeholder; the post-computation public
    validation rejects the shape with its own contract error.
    """
    if not isinstance(rows, list):
        return None
    na_count = sum(1 for row in rows
                   if isinstance(row, dict)
                   and row.get("status") == "not-applicable")
    satisfied = sum(1 for row in rows
                   if isinstance(row, dict)
                   and row.get("status") == "satisfied")
    applicable = len(rows) - na_count
    if applicable <= 0:
        return None
    return {"satisfied": satisfied, "applicable": applicable,
            "percent": (100 * satisfied) // applicable}


def _reject_untrusted_prose(text: str, ctx: str) -> None:
    if (_LINK_RE.search(text) or _AUTOLINK_RE.search(text)
            or _HTML_RE.search(text) or "|" in text or "`" in text
            or _HEADLINE_RE.search(text) or _PERCENT_RE.search(text)
            or _EXEC_CLAIM_RE.search(text)):
        raise _fail("invalid-assessment",
                    f"{ctx} carries untrusted model content")


def parse_assessment(payload: bytes,
                     packet: EvidencePacket) -> ChildAssessment:
    """Validate one normalized payload strictly against one packet."""
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    if isinstance(payload, bytearray):
        payload = bytes(payload)
    if not isinstance(payload, (bytes, str)):
        raise TypeError("payload must be bytes")
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise _fail("invalid-assessment", "assessment exceeds its bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("invalid-assessment",
                    "assessment is not valid UTF-8") from None
    try:
        envelope = json.loads(text)
    except (RecursionError, ValueError):
        raise _fail("invalid-assessment",
                    "assessment is not JSON") from None
    if not isinstance(envelope, dict):
        raise _fail("invalid-assessment", "assessment must be an object")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise _fail("invalid-assessment",
                    "error documents are not assessments")
    _reject_forbidden_keys(data)
    # Exact raw shapes first: unknowns are rejected here, never projected
    # away, and any model-supplied score key fails before validation.
    _reject_extra_raw_keys(envelope)

    # ptest attaches its own provider/publication placeholders plus the
    # computed score, then the frozen public contract validates the
    # completed post-computation envelope; it never sees raw input.
    children = data["children"]
    completed = json.loads(text)
    completed["data"]["provider"] = dict(_ASSESSMENT_PROVIDER)
    completed["data"]["publication"] = dict(_ASSESSMENT_PUBLICATION)
    if (isinstance(children, list) and len(children) == 1
            and isinstance(children[0], dict)):
        completed["data"]["children"][0]["score"] = _provisional_score(
            children[0].get("rows"))
    completed_raw = json.dumps(completed).encode("utf-8")
    try:
        document = C.decode_public_document(completed_raw)
    except C.Problem:
        raise
    except (TypeError, ValueError) as exc:
        raise _fail("invalid-assessment",
                    f"assessment failed validation: {exc}") from None
    if document.kind != "agent-assessment" or document.data is None:
        raise _fail("invalid-assessment",
                    "error documents are not assessments")
    children = document.data["children"]
    if len(children) != 1:
        raise _fail("invalid-assessment",
                    "assessment must bind exactly one packet")
    child = children[0]
    if child["packet_sha256"] != packet.packet_sha256:
        raise C.Problem(code="stale-evidence",
                        message="assessment binds a stale packet",
                        phase=_PHASE, retryable=False)
    if child["project_id"] != packet.project_id:
        raise _fail("invalid-assessment",
                    "assessment project identity does not match the packet")
    if child["scope"] != packet.scope:
        raise _fail("invalid-assessment",
                    "assessment scope does not match the packet")

    expected = C.AGENT_ASSESSMENT_CHECKLIST_IDS
    rows_data = child["rows"]
    if len(rows_data) != len(expected):
        raise _fail("invalid-assessment",
                    "assessment must hold all 11 checklist rows")
    index = {excerpt.path: excerpt for excerpt in packet.excerpts}
    rows: list[AssessmentRow] = []
    for position, entry in enumerate(rows_data):
        if entry["id"] != expected[position]:
            raise _fail("invalid-assessment",
                        "assessment breaks canonical checklist order")
        citations = tuple(
            Citation(path=item["path"], start_line=item["start_line"],
                     end_line=item["end_line"], sha256=item["sha256"])
            for item in entry["evidence"])
        for citation in citations:
            excerpt = index.get(citation.path)
            if excerpt is None:
                raise _fail("invalid-assessment",
                            "assessment cites evidence outside the packet")
            if citation.sha256 != excerpt.sha256:
                raise _fail("invalid-assessment",
                            "assessment citation identity is stale")
            if not (excerpt.start_line <= citation.start_line
                    <= citation.end_line <= excerpt.end_line):
                raise _fail("invalid-assessment",
                            "assessment citation escapes its excerpt")
        _reject_untrusted_prose(entry["rationale"],
                                f"rows[{position}].rationale")
        if entry["status"] == "not-applicable":
            raise _fail("invalid-assessment",
                        "not-applicable is unsupported in v1 model "
                        "input; leave the row unknown")
        rows.append(AssessmentRow(id=entry["id"], status=entry["status"],
                                  rationale=entry["rationale"],
                                  evidence=citations))

    findings: list[Finding] = []
    for position, entry in enumerate(child["findings"]):
        citations = tuple(
            Citation(path=item["path"], start_line=item["start_line"],
                     end_line=item["end_line"], sha256=item["sha256"])
            for item in entry["evidence"])
        for citation in citations:
            excerpt = index.get(citation.path)
            if excerpt is None or citation.sha256 != excerpt.sha256 or not (
                    excerpt.start_line <= citation.start_line
                    <= citation.end_line <= excerpt.end_line):
                raise _fail("invalid-assessment",
                            "finding cites evidence outside the packet")
        _reject_untrusted_prose(entry["summary"],
                                f"findings[{position}].summary")
        _reject_untrusted_prose(entry["suggested_change"],
                                f"findings[{position}].suggested_change")
        findings.append(Finding(
            id=entry["id"], summary=entry["summary"],
            suggested_change=entry["suggested_change"],
            recipe_id=entry["recipe_id"], evidence=citations))

    computed = score(tuple(rows))
    reported = child["score"]
    if (reported is None) != (computed is None):
        raise _fail("invalid-assessment",
                    "assessment score must be ptest-computed")
    if reported is not None and (
            reported["satisfied"] != computed.satisfied
            or reported["applicable"] != computed.applicable
            or reported["percent"] != computed.percent):
        raise _fail("invalid-assessment",
                    "assessment score must be ptest-computed")
    return ChildAssessment(
        packet_sha256=child["packet_sha256"],
        project_id=child["project_id"], scope=child["scope"],
        rows=tuple(rows), findings=tuple(findings), score=computed)


__all__ = [
    "EvidenceLimits", "SourceExcerpt", "DependencyFact", "EvidencePacket",
    "Citation", "AssessmentRow", "Finding", "Score", "ChildAssessment",
    "build_packets", "parse_assessment", "score",
    "MAX_FILES_PER_CHILD", "MAX_BYTES_PER_CHILD", "MAX_BYTES_PER_FILE",
    "MAX_PROMPT_BYTES",
]
