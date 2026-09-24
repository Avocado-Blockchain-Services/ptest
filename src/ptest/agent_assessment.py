"""Bounded evidence packets, provider request encoding, validation, scoring.

Three-pass shape (collect, validate, score):

1. :func:`build_packets` collects one bounded packet per declared selected
   child in manifest order. It reads regular files only, never follows
   symlinks, excludes instruction/secret/private/dependency/generated
   content, enforces 64 files / 512 KiB per child / 64 KiB per file plus
   separate 256-candidate / 2 MiB read budgets, reserves request overhead
   under the 1 MiB provider input cap, and records explicit coverage counts
   plus SHA-256 excerpt identities. Dependency provenance is static text
   only: declarations and
   authoritative locks are distinguished, the local environment is never
   executed or imported (project-local interpreter metadata is reported
   as ``installed`` facts, otherwise recorded ``uninspectable``),
   ptest's own runtime
   environment is never treated as project evidence, and anything
   unprovable stays ``unknown``.
2. :func:`plan_item_reviews` plans one focused review per checklist item
   (deterministic skips take no model call), and :func:`assemble_child`
   validates each one-row reply against its item excerpt subset, binds
   every citation to collected excerpt identity and ranges, and turns
   each failure into an ``unknown`` row. A ``not-applicable`` row is
   accepted only with a specific rationale and >=1 citation bound to
   packet excerpts; absence of code stays ``unknown``.
3. :func:`score` computes ``satisfied / (all - justified N/A)`` with
   integer floor; ``unknown`` stays in the denominator and zero applicable
   rows yield no score.

Child authority is preserved throughout: packets keep their own
declaration/project identity in manifest order, and assembly binds
each planned item review to the packet it was planned from.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from .checklist import CATALOG as _CHECKLIST_CATALOG
from .deterministic_items import DeterministicAnswer
from .checklist import SRC_DIR as _GENERIC_SRC_DIR_PATTERN
from .checklist import TEST_DIR as _GENERIC_TEST_DIR_PATTERN
from .checklist import TEST_FILE as _GENERIC_TEST_FILE_PATTERN
from .files import read_regular

_PHASE = "validation"

MAX_FILES_PER_CHILD = 64
MAX_BYTES_PER_CHILD = 512 * 1024
MAX_BYTES_PER_FILE = 64 * 1024
# Candidate reads include files later rejected for encoding or binary content.
# Keep their independent work budget finite even when no excerpt is admitted.
MAX_CANDIDATE_FILES_PER_CHILD = 256
MAX_CANDIDATE_BYTES_PER_CHILD = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
# The collected packet leaves room for the fixed policy, response contract,
# and ordinary provider schema before the shared 1 MiB stdin budget.
_REQUEST_OVERHEAD_RESERVE_BYTES = 128 * 1024
_DEFAULT_PACKET_PROMPT_BYTES = (
    MAX_PROMPT_BYTES - _REQUEST_OVERHEAD_RESERVE_BYTES)
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_WALK_ENTRIES = 20000

# Directories never descended into (doctor admission classes plus agent
# instruction/configuration locations and ptest private runtime state).
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".pipeline", ".superpowers", "graphify-out",
    ".ptest",
    ".claude", ".agents", ".codex", ".opencode", ".gemini",
    ".venv", "venv", "node_modules", "__pycache__",
    "build", "dist", "target", "coverage", ".coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".next",
    ".ssh", ".aws", ".gnupg",
})
# Agent/pipeline artifacts that are never admitted even outside excluded
# directories: patch files and ptest's own published report.
_EXCLUDED_SUFFIXES = (".diff", ".patch")
_EXCLUDED_BASENAMES = frozenset({"recommendations.md"})
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

# Evidence admission tiers: manifests and locks first so the file cap can
# never starve test configuration, then test configuration before test
# files, then CI, then imported source, then everything else.
_TIER0_BASENAMES = frozenset({
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "setup.py",
    "requirements.txt", "uv.lock", "poetry.lock", "pdm.lock",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
    "go.sum",
})
_TIER1_BASENAMES = frozenset({
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
})
_TIER1_PREFIXES = (
    "vitest.config.", "vite.config.", "jest.config.", "vitest.setup.",
    "vitest.workspace.", "setupTests.",
)
_TIER2_DIRS = frozenset({"tests", "test", "__tests__"})
_TIER2_FILE_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")
_TIER3_BASENAMES = frozenset({
    ".gitlab-ci.yml", "azure-pipelines.yml", "Jenkinsfile",
})

_PY_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([A-Za-z_][\w.]*)")
_PY_FROM_RE = re.compile(r"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]+")
_JS_REQUIRE_RE = re.compile(
    r"(?:import\s+(?:[^'\"]*?\s+from\s+)?|require\s*\(\s*|import\s*\(\s*)"
    r"['\"](\.[^'\"]*)['\"]")
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _admission_tier(path: str, tier4: frozenset = frozenset()) -> int:
    """Return the admission tier (0..5) for a child-relative path."""
    base = path.rsplit("/", 1)[-1]
    if base in _TIER0_BASENAMES or fnmatch.fnmatchcase(
            base, "requirements*.txt"):
        # Design §3.5 admits only child-root manifests and locks to tier 0;
        # nested ones rank last so they cannot starve test configuration.
        return 0 if "/" not in path else 5
    if base in _TIER1_BASENAMES or base.startswith(_TIER1_PREFIXES):
        return 1
    parts = path.split("/")
    if any(part in _TIER2_DIRS for part in parts):
        return 2
    for pattern in _TIER2_FILE_PATTERNS:
        if fnmatch.fnmatchcase(base, pattern):
            return 2
    for index, part in enumerate(parts[:-1]):
        if part == ".github" and parts[index + 1] == "workflows":
            return 3
        if part in (".circleci", ".buildkite"):
            return 3
    if base in _TIER3_BASENAMES or (
            base.startswith("cloudbuild")
            and base.endswith((".yaml", ".yml"))):
        return 3
    if path in tier4:
        return 4
    return 5


def _python_import_targets(module: str) -> list[str]:
    """Resolve ``import a.b`` / ``from a.b import`` to candidate rel paths."""
    relative = module.replace(".", "/")
    return [f"{relative}.py", f"{relative}/__init__.py",
            f"src/{relative}.py", f"src/{relative}/__init__.py"]


def _js_import_targets(from_path: str, specifier: str) -> list[str]:
    """Resolve a relative JS/TS import to candidate rel paths."""
    if "/" in from_path:
        anchor = from_path.rsplit("/", 1)[0]
    else:
        anchor = ""
    joined = f"{anchor}/{specifier}" if anchor else specifier
    normalized: list[str] = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            else:
                return []
        else:
            normalized.append(part)
    base = "/".join(normalized)
    targets = [f"{base}{ext}" for ext in _JS_EXTENSIONS]
    targets.extend(f"{base}/index{ext}" for ext in _JS_EXTENSIONS)
    return targets


def _resolve_tier4(request_texts: dict[str, str],
                   candidates: set[str]) -> frozenset:
    """Find imported-source candidates referenced by admitted test texts."""
    resolved: set[str] = set()
    for from_path, text in request_texts.items():
        for line in text.splitlines():
            match = _PY_IMPORT_RE.match(line) or _PY_FROM_RE.match(line)
            if match is not None:
                for target in _python_import_targets(match.group(1)):
                    if target in candidates:
                        resolved.add(target)
                continue
            for specifier in _JS_REQUIRE_RE.findall(line):
                for target in _js_import_targets(from_path, specifier):
                    if target in candidates:
                        resolved.add(target)
    return frozenset(resolved)

_VALID_STATUSES = frozenset({"satisfied", "gap", "unknown", "not-applicable"})

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
    """Per-child admission and candidate-read caps (all strictly positive)."""

    max_files_per_child: int = MAX_FILES_PER_CHILD
    max_bytes_per_child: int = MAX_BYTES_PER_CHILD
    max_bytes_per_file: int = MAX_BYTES_PER_FILE
    max_prompt_bytes: int = _DEFAULT_PACKET_PROMPT_BYTES
    max_candidate_files_per_child: int = MAX_CANDIDATE_FILES_PER_CHILD
    max_candidate_bytes_per_child: int = MAX_CANDIDATE_BYTES_PER_CHILD

    def __post_init__(self) -> None:
        for field in ("max_files_per_child", "max_bytes_per_child",
                      "max_bytes_per_file", "max_prompt_bytes",
                      "max_candidate_files_per_child",
                      "max_candidate_bytes_per_child"):
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
                               "unsupported", "uninspectable",
                               "installed"):
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
    label: str
    dropped_citations: int = 0

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
        if not isinstance(self.label, str) or not self.label:
            raise TypeError("row.label must be nonempty str")
        if (isinstance(self.dropped_citations, bool)
                or not isinstance(self.dropped_citations, int)):
            raise TypeError("row.dropped_citations must be int")
        if self.dropped_citations < 0:
            raise ValueError("row.dropped_citations must be >= 0")


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
        if not 0 <= self.satisfied <= 12:
            raise ValueError("score.satisfied is out of range")
        if not 1 <= self.applicable <= 12:
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
            or name in _EXCLUDED_BASENAMES
            or name.endswith(_EXCLUDED_SUFFIXES)
            or bool(_PRIVATE_NAME.search(name))
            or bool(_GENERATED_NAME.search(name)))


def _review_checkpoint(deadline: float | None,
                       progress: Callable[[], None] | None) -> None:
    """Check the review budget around one bounded unit of packet work."""
    if deadline is None and progress is None:
        return
    if deadline is not None and time.monotonic() >= deadline:
        raise C.Problem(code="review-timeout",
                        message="total review deadline expired",
                        phase="evidence", retryable=False)
    if progress is not None:
        progress()
    if deadline is not None and time.monotonic() >= deadline:
        raise C.Problem(code="review-timeout",
                        message="total review deadline expired",
                        phase="evidence", retryable=False)


def _iter_regular_files(root: Path, rel: str, entries: list,
                        *, deadline: float | None = None,
                        progress: Callable[[], None] | None = None) -> None:
    """Collect ``(relative, size)`` regular files without following links.

    Symlinks, sockets, devices, FIFOs, and excluded names are recorded in
    ``entries`` as skipped markers instead of being opened.
    """
    stack = [rel]
    seen = 0
    while stack:
        _review_checkpoint(deadline, progress)
        current = stack.pop()
        try:
            with os.scandir(root / current if current else root) as it:
                names = []
                while True:
                    _review_checkpoint(deadline, progress)
                    try:
                        entry = next(it)
                    except StopIteration:
                        break
                    _review_checkpoint(deadline, progress)
                    if seen >= MAX_WALK_ENTRIES:
                        entries.append(("skip", current))
                        return
                    seen += 1
                    names.append(entry)
        except OSError:
            _review_checkpoint(deadline, progress)
            entries.append(("skip", current))
            continue
        names.sort(key=lambda entry: entry.name)
        _review_checkpoint(deadline, progress)
        for entry in names:
            _review_checkpoint(deadline, progress)
            name = entry.name
            child = f"{current}/{name}" if current else name
            if _excluded_name(name):
                entries.append(("skip", child))
                continue
            try:
                is_link = entry.is_symlink()
            except OSError:
                _review_checkpoint(deadline, progress)
                entries.append(("skip", child))
                continue
            _review_checkpoint(deadline, progress)
            if is_link:
                entries.append(("skip", child))
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                _review_checkpoint(deadline, progress)
                entries.append(("skip", child))
                continue
            _review_checkpoint(deadline, progress)
            if is_dir:
                stack.append(child)
                continue
            try:
                stamp = entry.stat(follow_symlinks=False)
            except OSError:
                _review_checkpoint(deadline, progress)
                entries.append(("skip", child))
                continue
            _review_checkpoint(deadline, progress)
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


# Project-local environment inspection bounds. Metadata is read without
# imports or execution: ``pyvenv.cfg`` version keys, distribution directory
# names, and installed ``package.json`` version fields only. Symlinks are
# never followed out of the child root; every read is size-capped.
_PYVENV_CFG_MAX_BYTES = 4096
_NODE_PACKAGE_JSON_MAX_BYTES = 64 * 1024
_MAX_DIST_INFO_ENTRIES = 512
_MAX_ENV_SCAN_ENTRIES = 4096
_ENV_DETAIL_MAX_CHARS = 512
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._+-]+")
# Recognized test tools: small explicit allowlists, nothing else is named.
_NODE_TEST_TOOLS = frozenset({"vitest", "jest", "mocha", "@playwright/test"})
_PYTHON_TEST_TOOLS = frozenset({"pytest", "hypothesis", "coverage",
                                "pytest-cov"})


def _safe_token(value: object, max_len: int) -> str | None:
    """Return ``value`` when it is a short plain token, else None.

    Only ``[A-Za-z0-9._+-]`` tokens of bounded length may reach fact
    detail; hostile names/versions (controls, markup, backticks, overlong)
    are dropped, never reported.
    """
    if not isinstance(value, str) or not value or len(value) > max_len:
        return None
    if _SAFE_TOKEN_RE.fullmatch(value) is None:
        return None
    return value


def _is_real_dir(path: Path) -> bool:
    """True only for a directory that is not a symlink (lstat, no follow)."""
    try:
        stamp = os.lstat(path)
    except OSError:
        return False
    return (not stat.S_ISLNK(stamp.st_mode)
            and stat.S_ISDIR(stamp.st_mode))


def _read_pyvenv_version(child_root: Path, env_name: str) -> str | None:
    """Read the ``version`` (or ``version_info``) key from pyvenv.cfg.

    The file must be a regular file within the size cap; symlinks, FIFOs,
    directories, oversized, or undecodable files are ignored. Only the
    version keys are extracted; ``home`` and absolute paths never leave.
    """
    try:
        raw = read_regular(child_root, f"{env_name}/pyvenv.cfg",
                           _PYVENV_CFG_MAX_BYTES + 1)
    except C.Problem:
        return None
    if len(raw) > _PYVENV_CFG_MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fallback: str | None = None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key == "version":
            version = _safe_token(value.strip(), 32)
            if version is not None:
                return version
        elif key == "version_info" and fallback is None:
            fallback = _safe_token(value.strip(), 32)
    return fallback


def _scan_dist_info(child_root: Path, env_name: str, *,
                    deadline: float | None,
                    progress: Callable[[], None] | None
                    ) -> tuple[int, bool, list[tuple[str, str]], bool]:
    """Count ``*.dist-info`` dirs and name recognized test tools.

    Returns ``(count, lower_bound, tools, listed)`` where ``count`` is
    the number of distribution directories observed, ``lower_bound``
    reports that the scan stopped at its entry bound, and ``listed``
    reports that at least one ``python3.*/site-packages`` directory was
    actually listed. Both directory levels are iterated lazily with one
    count plus deadline checkpoint per entry, as in
    ``_iter_regular_files``. Symlinked entries are never followed; only
    validated tokens reach ``tools``.
    """
    lib = child_root / env_name / "lib"
    if not _is_real_dir(lib):
        return (0, False, [], False)
    tools: list[tuple[str, str]] = []
    seen_tools: set[str] = set()
    count = 0
    examined = 0
    listed = False
    try:
        with os.scandir(lib) as handle:
            for entry in handle:
                _review_checkpoint(deadline, progress)
                examined += 1
                if examined > _MAX_ENV_SCAN_ENTRIES:
                    return (count, True, tools, listed)
                try:
                    stamp = os.lstat(entry.path)
                except OSError:
                    continue
                if (stat.S_ISLNK(stamp.st_mode)
                        or not stat.S_ISDIR(stamp.st_mode)):
                    continue
                if not entry.name.startswith("python3."):
                    continue
                site = Path(entry.path) / "site-packages"
                if not _is_real_dir(site):
                    continue
                try:
                    site_handle = os.scandir(site)
                except OSError:
                    continue
                with site_handle:
                    listed = True
                    for item in site_handle:
                        _review_checkpoint(deadline, progress)
                        examined += 1
                        if examined > _MAX_ENV_SCAN_ENTRIES:
                            return (count, True, tools, listed)
                        try:
                            item_stamp = os.lstat(item.path)
                        except OSError:
                            continue
                        if (stat.S_ISLNK(item_stamp.st_mode)
                                or not stat.S_ISDIR(item_stamp.st_mode)):
                            continue
                        if not item.name.endswith(".dist-info"):
                            continue
                        count += 1
                        if count > _MAX_DIST_INFO_ENTRIES:
                            return (count - 1, True, tools, listed)
                        stem = item.name[:-len(".dist-info")]
                        name, dash, version = stem.rpartition("-")
                        if not dash:
                            continue
                        clean_name = _safe_token(name, 64)
                        clean_version = _safe_token(version, 32)
                        if clean_name is None or clean_version is None:
                            continue
                        normalized = re.sub(r"[-_.]+", "-",
                                            clean_name.lower())
                        if (normalized in _PYTHON_TEST_TOOLS
                                and normalized not in seen_tools):
                            seen_tools.add(normalized)
                            tools.append((clean_name, clean_version))
    except OSError:
        return (0, False, [], False)
    return (count, False, tools, listed)


def _inspect_python_env(child_root: Path, *,
                        deadline: float | None,
                        progress: Callable[[], None] | None
                        ) -> DependencyFact | None:
    """Describe a project-local ``.venv``/``venv`` without executing it.

    An ``installed`` fact needs provenance: ``pyvenv.cfg`` must have
    yielded a version and at least one ``python3.*/site-packages``
    directory must have been listed. Otherwise there is no fact, so the
    environment stays ``uninspectable``.
    """
    for env_name in (".venv", "venv"):
        if not _is_real_dir(child_root / env_name):
            continue
        version = _read_pyvenv_version(child_root, env_name)
        count, lower_bound, tools, listed = _scan_dist_info(
            child_root, env_name, deadline=deadline, progress=progress)
        if version is None or not listed:
            continue
        numbered = f"{count}+" if lower_bound else str(count)
        detail = (f"Python {version} "
                  f"project-local environment: {numbered} distributions")
        if tools:
            detail += "; " + ", ".join(
                f"{name} {tool_version}" for name, tool_version in tools)
        return DependencyFact(ecosystem="python", status="installed",
                              ref_path=None,
                              detail=detail[:_ENV_DETAIL_MAX_CHARS])
    return None


def _resolve_inside(node_modules: Path, node_real: str,
                    parts: list[str]) -> Path | None:
    """Resolve package components, following only contained symlinks.

    A symlink (pnpm layout) is followed only when its real path stays
    inside the child's ``node_modules``; an escaping link is ignored.
    """
    current = node_modules
    for part in parts:
        candidate = current / part
        try:
            is_link = os.lstat(candidate).st_mode
        except OSError:
            return None
        if stat.S_ISLNK(is_link):
            real = os.path.realpath(candidate)
            try:
                inside = os.path.commonpath([real, node_real]) == node_real
            except ValueError:
                return None
            if not inside:
                return None
            current = Path(real)
        else:
            current = candidate
    if not _is_real_dir(current):
        return None
    return current


def _inspect_node_env(child_root: Path, package_json_text: str | None, *,
                      deadline: float | None,
                      progress: Callable[[], None] | None
                      ) -> DependencyFact | None:
    """Describe installed test tools named by the admitted package.json.

    Only dependencies named in the admitted manifest that are also on the
    explicit test-tool allowlist are inspected, reading at most the
    ``version`` field of their installed ``package.json``.
    """
    if not package_json_text:
        return None
    try:
        manifest = json.loads(package_json_text)
    except ValueError:
        return None
    if not isinstance(manifest, dict):
        return None
    wanted: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        entries = manifest.get(section)
        if isinstance(entries, dict):
            for name in entries:
                if isinstance(name, str) and name in _NODE_TEST_TOOLS:
                    wanted.add(name)
    if not wanted:
        return None
    node_modules = child_root / "node_modules"
    if not _is_real_dir(node_modules):
        return None
    node_real = os.path.realpath(node_modules)
    found: list[tuple[str, str]] = []
    for name in sorted(wanted):
        _review_checkpoint(deadline, progress)
        resolved = _resolve_inside(node_modules, node_real,
                                   name.split("/"))
        if resolved is None:
            continue
        try:
            raw = read_regular(resolved, "package.json",
                               _NODE_PACKAGE_JSON_MAX_BYTES + 1)
        except C.Problem:
            continue
        if len(raw) > _NODE_PACKAGE_JSON_MAX_BYTES:
            continue
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        version = _safe_token(document.get("version"), 32)
        if version is None:
            continue
        found.append((name, version))
    if not found:
        return None
    detail = ("Node project-local environment: "
              + ", ".join(f"{name} {version}" for name, version in found))
    return DependencyFact(ecosystem="node", status="installed",
                          ref_path=None,
                          detail=detail[:_ENV_DETAIL_MAX_CHARS])


def _present_on_disk(child_root: Path | None, name: str) -> bool:
    """Disk presence of a child-root file, without following symlinks."""
    if child_root is None:
        return False
    try:
        stamp = os.lstat(child_root / name)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode)


def _dependency_facts(names: set[str], prefix: str,
                      scoped_paths: dict[str, str] | None = None, *,
                      child_root: Path | None = None,
                      package_json_text: str | None = None,
                      deadline: float | None = None,
                      progress: Callable[[], None] | None = None) -> tuple:
    """Static declaration/lock facts plus safe project-local env metadata."""
    def ref_path(name: str) -> str:
        if scoped_paths is not None:
            return scoped_paths[name]
        return f"{prefix}{name}" if prefix else name

    facts: list[DependencyFact] = []
    seen_ecosystems: set[str] = set()
    for filename, (ecosystem, _) in sorted(_DECLARATIONS.items()):
        if filename in names:
            seen_ecosystems.add(ecosystem)
            facts.append(DependencyFact(
                ecosystem=ecosystem, status="declared",
                ref_path=ref_path(filename),
                detail=f"Static declaration {filename}; provenance "
                       "declaration, content unexecuted."))
    for marker in sorted(_UNSUPPORTED_MARKERS):
        if marker in names:
            facts.append(DependencyFact(
                ecosystem="project", status="unsupported",
                ref_path=ref_path(marker),
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
                ref_path=ref_path(hit[0]),
                detail=f"Authoritative lock {hit[0]}; provenance lockfile, "
                       "content unexecuted."))
            continue
        # Lock admission is not disk presence: a lock on disk but outside
        # the packet is uninspectable, and only an absent lock is missing.
        for lock in sorted(name for name, kind in _LOCKS.items()
                           if kind == want):
            if _present_on_disk(child_root, lock):
                facts.append(DependencyFact(
                    ecosystem=want, status="uninspectable", ref_path=None,
                    detail=f"{lock} is present but was not admitted to "
                           "the review packet."))
            else:
                facts.append(DependencyFact(
                    ecosystem=want, status="missing", ref_path=None,
                    detail=f"{lock} is missing."))
    if not seen_ecosystems:
        facts.append(DependencyFact(
            ecosystem="project", status="missing", ref_path=None,
            detail="No recognized declaration file admitted in packet; "
                   "dependency provenance unknown."))
    installed: list[DependencyFact] = []
    if child_root is not None:
        _review_checkpoint(deadline, progress)
        python_fact = _inspect_python_env(
            child_root, deadline=deadline, progress=progress)
        if python_fact is not None:
            installed.append(python_fact)
        node_fact = _inspect_node_env(
            child_root, package_json_text, deadline=deadline,
            progress=progress)
        if node_fact is not None:
            installed.append(node_fact)
    facts.extend(installed)
    installed_ecosystems = {fact.ecosystem for fact in installed}
    if not installed or not seen_ecosystems <= installed_ecosystems:
        facts.append(DependencyFact(
            ecosystem="environment", status="uninspectable", ref_path=None,
            detail="Environments other than reported project-local "
                   "metadata are not inspected."))
    return tuple(facts)


def _scope_problem(message: str) -> C.Problem:
    return _fail("unsafe-path", message)


def _validate_scope_directory(root: Path, relative: str) -> None:
    """Require every selected path component to be an existing directory."""
    current = root
    try:
        stamp = os.lstat(current)
    except OSError:
        raise _scope_problem("selected evidence scope is unavailable") from None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        raise _scope_problem("selected evidence scope is unsafe")
    if relative in ("", "."):
        return
    for component in relative.split("/"):
        current = current / component
        try:
            stamp = os.lstat(current)
        except OSError:
            raise _scope_problem("selected evidence scope is unavailable") from None
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            raise _scope_problem("selected evidence scope is unsafe")


def _packet_scope_context(root: Path, workspace, repo) -> tuple:
    """Validate inspection scope provenance and return its collection root."""
    declaration = repo.declaration
    try:
        declaration = _check_relpath(declaration, "inspection.declaration")
    except (TypeError, ValueError):
        raise _scope_problem("declared evidence root is unsafe") from None
    if declaration != "." and any(
            ord(char) < 32 or ord(char) == 127 for char in declaration):
        raise _scope_problem("declared evidence root is unsafe")
    local_scope = repo.local_scope
    if local_scope is not None:
        try:
            local_scope = _check_relpath(local_scope,
                                         "inspection.local_scope")
        except (TypeError, ValueError):
            raise _scope_problem("selected evidence scope is malformed") from None
        if local_scope == "." or any(
                ord(char) < 32 or ord(char) == 127 for char in local_scope):
            raise _scope_problem("selected evidence scope is malformed")

    workspace_scope = workspace.scope
    if (not isinstance(workspace_scope, tuple)
            or any(not isinstance(path, str) for path in workspace_scope)):
        raise _scope_problem("workspace evidence scope is malformed")

    if declaration == ".":
        child_root = root
        declaration_path = ""
    else:
        child_root = root / declaration
        declaration_path = declaration
    _validate_scope_directory(root, declaration_path)

    if local_scope is None:
        scope = declaration
        scan_start = ""
    else:
        scope = (local_scope if declaration == "."
                 else f"{declaration}/{local_scope}")
        scan_start = local_scope
        if len(workspace.repositories) != 1:
            raise _scope_problem("selected evidence scope is ambiguous")
        _validate_scope_directory(child_root, local_scope)

    if local_scope is not None:
        if workspace_scope != (scope,):
            raise _scope_problem("workspace and child evidence scopes differ")
    elif workspace_scope and (
            len(workspace.repositories) != 1
            or declaration == "."
            or workspace_scope != (declaration,)):
        raise _scope_problem("workspace and child evidence scopes differ")

    return child_root, scan_start, scope


def build_packets(workspace, resolution,
                  limits: EvidenceLimits = EvidenceLimits(), *,
                  deadline: float | None = None,
                  progress: Callable[[], None] | None = None,
                  ) -> tuple[EvidencePacket, ...]:
    """Collect one bounded packet per declared selected child, in order."""
    from . import doctor as doctor_api

    if not isinstance(workspace, doctor_api.WorkspaceInspection):
        raise TypeError("workspace must be doctor.WorkspaceInspection")
    if not isinstance(resolution, C.ConfigResolution):
        raise TypeError("resolution must be ConfigResolution")
    if not isinstance(limits, EvidenceLimits):
        raise TypeError("limits must be EvidenceLimits")
    _review_checkpoint(deadline, progress)
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
    contexts = []
    for repo in workspace.repositories:
        _review_checkpoint(deadline, progress)
        contexts.append(_packet_scope_context(root, workspace, repo))
        _review_checkpoint(deadline, progress)
    for repo, scope_context in zip(workspace.repositories, contexts):
        _review_checkpoint(deadline, progress)
        packets.append(_build_one_packet(
            root, repo, resolution, limits, scope_context,
            deadline=deadline, progress=progress))
    return tuple(packets)


class _AdmissionState:
    """Mutable per-child admission caps shared across tier phases."""

    def __init__(self) -> None:
        self.excerpts: list[SourceExcerpt] = []
        self.paths: dict[str, str] = {}
        self.byte_count = 0
        self.truncated = 0
        self.skipped = 0
        self.candidate_files_read = 0
        self.candidate_bytes_read = 0
        self.tier2_texts: dict[str, str] = {}


def _admit_candidate(state: _AdmissionState, child_root: Path, rel: str,
                     prefix: str, limits: EvidenceLimits,
                     max_candidate_read: int, remaining_after: int, *,
                     deadline: float | None = None,
                     progress: Callable[[], None] | None = None) -> bool:
    """Admit one candidate file into the packet state.

    ``remaining_after`` counts the unprocessed candidates after this one and
    accounts bulk truncation when a budget is exhausted. Returns False when
    intake must stop entirely.
    """
    if len(state.excerpts) >= limits.max_files_per_child:
        state.truncated += 1
        return True
    if (state.candidate_files_read >= limits.max_candidate_files_per_child
            or state.candidate_bytes_read >= limits.max_candidate_bytes_per_child):
        state.truncated += remaining_after + 1
        return False
    read_limit = min(
        max_candidate_read,
        limits.max_candidate_bytes_per_child - state.candidate_bytes_read)
    if read_limit <= 0:
        state.truncated += remaining_after + 1
        return False
    state.candidate_files_read += 1
    # Reserve the maximum this bounded read could consume. If the read
    # raises after a partial OS read, the reservation remains conservative.
    state.candidate_bytes_read += read_limit
    try:
        # The shared no-follow reader is byte-bounded to one excerpt plus
        # one sentinel byte. Check immediately around its bounded read;
        # individual OS read calls cannot be interrupted by this layer.
        raw = read_regular(child_root, rel, read_limit)
    except C.Problem:
        _review_checkpoint(deadline, progress)
        state.skipped += 1
        return True
    state.candidate_bytes_read -= read_limit - len(raw)
    _review_checkpoint(deadline, progress)
    # A candidate-budget-limited read may be a valid prefix. Do not admit
    # it as a complete excerpt; mark this file and the unread tail partial.
    if read_limit < max_candidate_read and len(raw) == read_limit:
        state.truncated += remaining_after + 1
        return False
    chunk, was_cut = _truncate_to_valid_utf8(
        raw, limits.max_bytes_per_file)
    if was_cut and raw and not chunk:
        state.truncated += 1
        return True
    if not chunk:
        # A 0-byte file carries no evidence: never admit it with a line-1
        # bound over zero lines (which later fails as stale evidence);
        # count it as excluded instead.
        state.skipped += 1
        return True
    if state.byte_count + len(chunk) > limits.max_bytes_per_child:
        state.truncated += 1
        return True
    if b"\x00" in chunk:
        state.skipped += 1
        return True
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        state.skipped += 1
        return True
    if was_cut:
        state.truncated += 1
    lines = text.splitlines() or [""]
    excerpt_path = f"{prefix}{rel}"
    excerpt = SourceExcerpt(
        path=excerpt_path, start_line=1,
        end_line=len(lines),
        sha256=hashlib.sha256(chunk).hexdigest(), text=text)
    state.excerpts.append(excerpt)
    state.paths.setdefault(rel.rsplit("/", 1)[-1], excerpt_path)
    state.byte_count += len(chunk)
    if _admission_tier(rel) == 2:
        state.tier2_texts[rel] = text
    return True


def _build_one_packet(root: Path, repo, resolution,
                      limits: EvidenceLimits,
                      scope_context: tuple, *,
                      deadline: float | None = None,
                      progress: Callable[[], None] | None = None
                      ) -> EvidencePacket:
    declaration = repo.declaration
    child_root, scan_start, scope = scope_context
    entries: list = []
    if any(_excluded_name(part)
           for path in (declaration, scan_start)
           for part in path.split("/")):
        entries.append(("skip", scan_start or declaration))
    else:
        _iter_regular_files(child_root, scan_start, entries,
                            deadline=deadline, progress=progress)
    regular = []
    skipped = 0
    for entry in entries:
        _review_checkpoint(deadline, progress)
        if entry[0] == "file":
            regular.append((entry[1], entry[2]))
        else:
            skipped += 1
    regular.sort(key=lambda item: item[0])
    _review_checkpoint(deadline, progress)
    prefix = "" if declaration == "." else declaration + "/"

    state = _AdmissionState()
    max_candidate_read = min(limits.max_bytes_per_file,
                             limits.max_bytes_per_child) + 1
    # Admission order is (tier, path): manifests and locks first so the file
    # cap can never starve test configuration, then test configuration
    # before test files, then CI. Imported source (tier 4) resolves from the
    # admitted test texts between the two phases; everything else trails.
    early = sorted(
        ((_admission_tier(rel), rel) for rel, _size in regular)
    )
    early = [item for item in early if item[0] <= 3]
    later_all = sorted(
        rel for rel, _size in regular
        if _admission_tier(rel) > 3)
    _review_checkpoint(deadline, progress)
    early_halted = False
    for position, (_tier, rel) in enumerate(early):
        _review_checkpoint(deadline, progress)
        if not _admit_candidate(
                state, child_root, rel, prefix, limits, max_candidate_read,
                len(early) - 1 - position + len(later_all),
                deadline=deadline, progress=progress):
            early_halted = True
            later_all = []
            break
    _review_checkpoint(deadline, progress)
    if not early_halted:
        tier4 = frozenset(
            _resolve_tier4(state.tier2_texts, set(later_all)))
        late = sorted(
            ((_admission_tier(rel, tier4), rel)
             for rel in later_all))
    else:
        late = []
    for position, (_tier, rel) in enumerate(late):
        _review_checkpoint(deadline, progress)
        if not _admit_candidate(
                state, child_root, rel, prefix, limits, max_candidate_read,
                len(late) - 1 - position,
                deadline=deadline, progress=progress):
            break
    _review_checkpoint(deadline, progress)
    excerpts = state.excerpts
    paths = state.paths
    byte_count = state.byte_count
    truncated = state.truncated
    skipped += state.skipped

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
    package_json_text: str | None = None
    manifest_path = f"{prefix}package.json"
    for excerpt in excerpts:
        _review_checkpoint(deadline, progress)
        if excerpt.path == manifest_path:
            package_json_text = excerpt.text
            break
    dependencies = _dependency_facts(
        set(paths), prefix, paths if scan_start else None,
        child_root=child_root, package_json_text=package_json_text,
        deadline=deadline, progress=progress)
    _review_checkpoint(deadline, progress)

    # Enforce the prompt cap by dropping trailing excerpts; coverage counts
    # stay explicit so partial evidence is visible, not silent.
    body = _packet_body(declaration, project_id, scope, excerpts,
                        dependencies, runner_kind, skipped, truncated,
                        byte_count)
    while excerpts:
        _review_checkpoint(deadline, progress)
        body_bytes = json.dumps(
            body, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("utf-8")
        _review_checkpoint(deadline, progress)
        if len(body_bytes) <= limits.max_prompt_bytes:
            break
        dropped = excerpts.pop()
        byte_count -= len(dropped.text.encode("utf-8"))
        truncated += 1
        body = _packet_body(declaration, project_id, scope,
                            excerpts, dependencies, runner_kind, skipped,
                            truncated, byte_count)
        _review_checkpoint(deadline, progress)
    _review_checkpoint(deadline, progress)
    digest = _packet_identity(body)
    _review_checkpoint(deadline, progress)
    return EvidencePacket(
        declaration=declaration, project_id=project_id, scope=scope,
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


# --- Per-item review (one focused model call per checklist item) ---------------

ITEM_MAX_FILES = 24
ITEM_MAX_BYTES = 256 * 1024
SKIP_PREFIX = "Skipped without a model call: "
FAILED_PREFIX = "Review failed: "
PTEST_ANSWER_PREFIX = "Answered by ptest: "

_ONE_ROW_PROSE_MAX_BYTES = 2048
_ONE_ROW_EVIDENCE_MAX = 16
_SKIP_RATIONALE_MIN_NONSPACE = 24

_CATALOG_BY_ID = {entry.id: entry for entry in _CHECKLIST_CATALOG}

# Conservative dependency signals. Any hit means the item is reviewed;
# only total absence across manifests, evidence, and scanner hits skips.
# The library lists deliberately cover Python, Node, Go, Rust, and JVM
# drivers, ORMs, and caches by substring so new clients fail open to review.
_DB_LIBRARY_RE = re.compile(
    r"sqlalchemy|sqlmodel|django|alembic|psycopg|asyncpg|aiosqlite|"
    r"\bsqlite3?\b|sqlite|duckdb|peewee|tortoise|prisma|sequelize|typeorm|"
    r"knex|mongoose|mongo|pymongo|\bmotor\b|mongoengine|beanie|odmantic|"
    r"piccolo|postgres|\bpg\b|pgx|lib/pq|mysql2?|mssql|pymssql|oracledb|"
    r"cx_oracle|oracle|pyodbc|pymysql|asyncmy|drizzle|kysely|supabase|"
    r"mikro-orm|gorm|sqlx|diesel|rusqlite|cassandra|dynamo|firestore|"
    r"cockroach|jdbc|hibernate|\bjpa\b|jooq|mybatis",
    re.IGNORECASE)
_DB_USAGE_RE = re.compile(
    r"connect\s*\(|create_all|drop_database|drop_all|truncate|DATABASE_URL|"
    r"database_url|postgres(?:ql)?://|mysql://|mariadb://|mongodb://|"
    r"mssql://|sqlite:/|Column\s*\(|"
    r"create_engine|sessionmaker|\.query\s*\(|\.execute\s*\(|\.sql\s*\(",
    re.IGNORECASE)
_CACHE_LIBRARY_RE = re.compile(
    r"redis|valkey|memcach|pylibmc|pymemcache|aiocache|cachetools|"
    r"keyv|lru|node-cache|diskcache|go-cache|ehcache|caffeine|hazelcast|"
    r"cacheops",
    re.IGNORECASE)
_CACHE_USAGE_RE = re.compile(
    r"flushall|flushdb|clear_all|invalidate_all|invalidate\s*\(|CACHE_URL|"
    r"cache_url|redis://|valkey://|memcached://|\.clear\s*\(|\.delete\s*\(",
    re.IGNORECASE)

_ONE_ROW_CITATION_SCHEMA = {
    "type": "object",
    "required": ["path", "start_line", "end_line", "sha256"],
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    "additionalProperties": False,
}
_ONE_ROW_SCHEMA_OBJECT = {
    "type": "object",
    "required": ["status", "rationale", "evidence", "finding"],
    "properties": {
        "status": {"type": "string", "enum": sorted(_VALID_STATUSES)},
        "rationale": {"type": "string", "maxBytes": _ONE_ROW_PROSE_MAX_BYTES},
        "evidence": {"type": "array", "maxItems": _ONE_ROW_EVIDENCE_MAX,
                     "items": _ONE_ROW_CITATION_SCHEMA},
        "finding": {
            "type": ["object", "null"],
            "required": ["summary", "suggested_change", "evidence"],
            "properties": {
                "summary": {"type": "string",
                            "maxBytes": _ONE_ROW_PROSE_MAX_BYTES},
                "suggested_change": {"type": "string",
                                     "maxBytes": _ONE_ROW_PROSE_MAX_BYTES},
                "evidence": {"type": "array", "minItems": 1,
                             "maxItems": _ONE_ROW_EVIDENCE_MAX,
                             "items": _ONE_ROW_CITATION_SCHEMA},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

_ITEM_INSTRUCTION = (
    "Answer exactly one checklist item with one JSON object matching "
    "response_schema; return JSON only, with no markdown fence or "
    "surrounding prose. Judge the single item in this request against its "
    "criterion using only the listed excerpts; cite excerpts with their "
    "root-relative paths, line ranges, and content identities. Use "
    "satisfied only with cited evidence that the criterion holds, gap only "
    "with cited evidence plus a finding, not-applicable only with a "
    "specific rationale citing affirmative excerpt evidence that the item "
    "cannot apply, and unknown otherwise. Return gap only with a cited "
    "concrete violation; return satisfied only when the evidence shows the "
    "guaranteeing mechanism (for example a session-wide network block or "
    "per-worker port allocation); otherwise return unknown with one "
    "sentence naming the missing evidence. Absence of code is unknown, "
    "never a guess. A gap requires a finding object; "
    "any other status requires finding null. A not-applicable rationale "
    "needs at least 24 non-whitespace characters. Prose fields are plain "
    "text only: no Markdown, backticks, pipe characters, links, HTML, "
    "headings, percent figures, execution claims, or test-run claims. Never "
    "start the rationale with 'Skipped without a model call: ', 'Review "
    "failed: ', or 'Answered by ptest: '; those prefixes are ptest-owned."
)


@dataclass(frozen=True, slots=True)
class ItemReview:
    """One planned per-item review: a skip, an answer, or one request."""

    item_id: str
    label: str
    scope: str
    request: bytes | None
    schema: bytes
    excerpt_paths: tuple[str, ...]
    skip_reason: str | None
    answer: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise TypeError("review.item_id must be nonempty str")
        if not isinstance(self.label, str) or not self.label:
            raise TypeError("review.label must be nonempty str")
        object.__setattr__(self, "scope",
                           _check_relpath(self.scope, "review.scope"))
        if self.request is not None and not isinstance(
                self.request, (bytes, bytearray)):
            raise TypeError("review.request must be bytes or None")
        if self.request is not None:
            object.__setattr__(self, "request", bytes(self.request))
        if not isinstance(self.schema, (bytes, bytearray)) or not self.schema:
            raise TypeError("review.schema must be nonempty bytes")
        object.__setattr__(self, "schema", bytes(self.schema))
        excerpt_paths = self.excerpt_paths
        if not isinstance(excerpt_paths, (tuple, list)):
            raise TypeError("review.excerpt_paths must be a tuple")
        for path in tuple(excerpt_paths):
            if not isinstance(path, str) or not path:
                raise TypeError("review.excerpt_paths entries must be str")
        object.__setattr__(self, "excerpt_paths", tuple(excerpt_paths))
        if self.skip_reason is not None and (
                not isinstance(self.skip_reason, str)
                or not self.skip_reason):
            raise TypeError("review.skip_reason must be str or None")
        if self.answer is not None and not isinstance(
                self.answer, DeterministicAnswer):
            raise TypeError("review.answer must be DeterministicAnswer or None")
        set_count = ((self.request is not None)
                     + (self.skip_reason is not None)
                     + (self.answer is not None))
        if set_count != 1:
            raise ValueError(
                "review must set exactly one of request, skip_reason, answer")


def one_row_schema() -> bytes:
    """Return the one-row response schema bytes, identical for every item."""
    return json.dumps(_ONE_ROW_SCHEMA_OBJECT, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _child_relative(path: str, declaration: str) -> str:
    """Path relative to the child root (declaration prefix removed)."""
    if declaration == ".":
        return path
    lead = declaration + "/"
    return path[len(lead):] if path.startswith(lead) else path


def _manifest_excerpts(packet: EvidencePacket) -> list[SourceExcerpt]:
    """Admitted tier-0 manifest excerpts, in packet order."""
    return [excerpt for excerpt in packet.excerpts
            if _admission_tier(
                _child_relative(excerpt.path, packet.declaration)) == 0]


def _skip_reason(packet: EvidencePacket, entry,
                 code_index: dict[str, frozenset]) -> str | None:
    """Deterministic N/A rationale, or None when the item needs a call."""
    kind = entry.skip
    if kind is None:
        return None
    manifests = _manifest_excerpts(packet)
    if not manifests:
        return None
    if kind == "no-database":
        library_re, usage_re = _DB_LIBRARY_RE, _DB_USAGE_RE
        prefix, noun = "db.", "database"
    elif kind == "no-cache":
        library_re, usage_re = _CACHE_LIBRARY_RE, _CACHE_USAGE_RE
        prefix, noun = "cache.", "cache"
    else:  # pragma: no cover - catalog skip values are closed
        raise ValueError(f"checklist {entry.id} has an unknown skip rule")
    if any(library_re.search(manifest.text) for manifest in manifests):
        return None
    if any(usage_re.search(excerpt.text) for excerpt in packet.excerpts):
        return None
    for excerpt in packet.excerpts:
        if any(code.startswith(prefix)
               for code in code_index.get(excerpt.path, ())):
            return None
    names = ", ".join(sorted({manifest.path.rsplit("/", 1)[-1]
                              for manifest in manifests}))
    return (SKIP_PREFIX + f"no {noun} library in {names} and no {noun} "
            "configuration or usage in the admitted evidence.")


# Broad directory patterns that admit almost every test/src file. Ranked
# last so an excerpt matching only these never pushes out key evidence.
_GENERIC_PATH_PATTERN_STRINGS = frozenset({
    _GENERIC_TEST_DIR_PATTERN, _GENERIC_TEST_FILE_PATTERN,
    _GENERIC_SRC_DIR_PATTERN})


_CONFTEST_PATH_RE = re.compile(r"(?:^|/)conftest\.py$")
_TEST_DIR_RE = re.compile(_GENERIC_TEST_DIR_PATTERN)
_TEST_FILE_RE = re.compile(_GENERIC_TEST_FILE_PATTERN)
_FIXTURE_DEF_RE = re.compile(
    r"@pytest\.fixture(?:\([^)]*\))?\s*\ndef[ \t]+([A-Za-z_]\w*)[ \t]*\(")
_TEST_PARAMS_RE = re.compile(
    r"^[ \t]*def[ \t]+test_\w*[ \t]*\(([^)]*)\)", re.MULTILINE)


def _fixture_first_paths(routed: list[SourceExcerpt]) -> list[SourceExcerpt]:
    """Rank conftest fixture definitions used by routed tests first.

    A conftest excerpt that defines a fixture whose name appears as a
    parameter in the item's routed test excerpts outranks every other
    signal, so the guaranteeing mechanism (not just the use site) is
    always in the review subset.
    """
    params: set[str] = set()
    for excerpt in routed:
        if not (_TEST_FILE_RE.search(excerpt.path)
                or _TEST_DIR_RE.search(excerpt.path)):
            continue
        for match in _TEST_PARAMS_RE.finditer(excerpt.text):
            for chunk in match.group(1).split(","):
                name = chunk.strip().split(":")[0].split("=")[0].strip()
                name = name.lstrip("*")
                if name.isidentifier():
                    params.add(name)
    if not params:
        return routed
    first: list[SourceExcerpt] = []
    rest: list[SourceExcerpt] = []
    for excerpt in routed:
        if (_CONFTEST_PATH_RE.search(excerpt.path)
                and "@pytest.fixture" in excerpt.text
                and any(name in params
                        for name in _FIXTURE_DEF_RE.findall(excerpt.text))):
            first.append(excerpt)
        else:
            rest.append(excerpt)
    return first + rest


def _route_excerpts(packet: EvidencePacket, entry,
                    code_index: dict[str, frozenset]) -> list[SourceExcerpt]:
    """Route the item's evidence subset, ranked before capping.

    Eligibility is unchanged: a path-pattern, text-pattern, or scanner-code
    hit admits the excerpt. Rank order is conftest excerpts defining a
    fixture used by the routed tests first, then scanner-code hits, then
    text-pattern hits, then item-specific path matches (conftest,
    fixture/factory, db/migration/cache names, manifests, .ptest.toml),
    then generic test/src directory matches. Packet order is kept within
    each rank, and the ITEM_MAX_FILES / ITEM_MAX_BYTES caps apply to the
    ranked order so key evidence cannot be starved by generic matches.
    """
    specific_res = [re.compile(pattern) for pattern in entry.path_patterns
                    if pattern not in _GENERIC_PATH_PATTERN_STRINGS]
    generic_res = [re.compile(pattern) for pattern in entry.path_patterns
                   if pattern in _GENERIC_PATH_PATTERN_STRINGS]
    text_res = [re.compile(pattern) for pattern in entry.text_patterns]
    wanted = set(entry.scanner_codes)
    ranked: list[tuple[tuple[bool, bool, bool], SourceExcerpt]] = []
    for excerpt in packet.excerpts:
        codes = set(code_index.get(excerpt.path, ()))
        scanner_hit = bool(wanted & codes)
        text_hit = any(pattern.search(excerpt.text) for pattern in text_res)
        specific_hit = any(pattern.search(excerpt.path)
                           for pattern in specific_res)
        generic_hit = any(pattern.search(excerpt.path)
                          for pattern in generic_res)
        if not (scanner_hit or text_hit or specific_hit or generic_hit):
            continue
        ranked.append(((not scanner_hit, not text_hit, not specific_hit),
                       excerpt))
    # Stable sort: packet order is kept within each rank.
    ranked.sort(key=lambda item: item[0])
    ordered = _fixture_first_paths([excerpt for _, excerpt in ranked])
    routed: list[SourceExcerpt] = []
    total = 0
    for excerpt in ordered:
        if len(routed) >= ITEM_MAX_FILES:
            break
        size = len(excerpt.text.encode("utf-8"))
        if total + size > ITEM_MAX_BYTES:
            continue
        routed.append(excerpt)
        total += size
    return routed


def _encode_item_request(packet: EvidencePacket, entry, routed,
                         schema_bytes: bytes) -> tuple[bytes, list]:
    """Encode one item request; shrink excerpts until it fits its bound."""
    from .agent_providers import PROMPT_INPUT_MAX_BYTES

    schema_object = json.loads(schema_bytes.decode("utf-8"))
    chosen = list(routed)
    while True:
        payload = {
            "policy": {
                "instruction": _ITEM_INSTRUCTION,
                "item": {"id": entry.id, "label": entry.label,
                         "criterion": entry.criterion,
                         "evidence": entry.evidence,
                         "recommendation": entry.recommendation,
                         "prompt": entry.prompt},
                "response_schema": schema_object,
                "statuses": sorted(_VALID_STATUSES),
            },
            "packet": {"packet_sha256": packet.packet_sha256,
                       "scope": packet.scope,
                       "project_id": packet.project_id,
                       "declaration": packet.declaration},
            "excerpts": [{"path": excerpt.path,
                          "start_line": excerpt.start_line,
                          "end_line": excerpt.end_line,
                          "sha256": excerpt.sha256,
                          "text": excerpt.text} for excerpt in chosen],
        }
        request = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")
        if len(request) <= PROMPT_INPUT_MAX_BYTES - len(schema_bytes):
            return request, chosen
        if not chosen:
            return request, chosen
        chosen.pop()


def _plan_one(packet: EvidencePacket, entry,
              code_index: dict[str, frozenset],
              schema_bytes: bytes,
              answers: Mapping[str, object] | None = None) -> ItemReview:
    if answers is not None:
        answer = answers.get(entry.id)
        if answer is not None:
            if not isinstance(answer, DeterministicAnswer):
                raise TypeError("answers entries must be DeterministicAnswer")
            if answer.item_id != entry.id:
                raise ValueError("answer item_id does not match its entry")
            return ItemReview(item_id=entry.id, label=entry.label,
                              scope=packet.scope, request=None,
                              schema=schema_bytes, excerpt_paths=(),
                              skip_reason=None, answer=answer)
    routed = _route_excerpts(packet, entry, code_index)
    reason = _skip_reason(packet, entry, code_index)
    if reason is not None:
        return ItemReview(item_id=entry.id, label=entry.label,
                          scope=packet.scope, request=None,
                          schema=schema_bytes,
                          excerpt_paths=tuple(e.path for e in routed),
                          skip_reason=reason)
    request, chosen = _encode_item_request(packet, entry, routed,
                                           schema_bytes)
    return ItemReview(item_id=entry.id, label=entry.label,
                      scope=packet.scope, request=request,
                      schema=schema_bytes,
                      excerpt_paths=tuple(e.path for e in chosen),
                      skip_reason=None)


def plan_item_reviews(packet: EvidencePacket,
                      answers: Mapping[str, object] | None = None
                      ) -> tuple[ItemReview, ...]:
    """Plan one review per catalog item, in catalog order.

    Pure: no filesystem, no subprocess, no model. Each review is either a
    deterministic skip (no model call), a deterministic answer (no model
    call), or one bounded provider request carrying only that item's
    routed evidence subset.
    """
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    if answers is not None and not isinstance(answers, Mapping):
        raise TypeError("answers must be a mapping or None")
    from . import doctor as doctor_api

    code_index = {excerpt.path: doctor_api.match_rules(excerpt.text)
                  for excerpt in packet.excerpts}
    schema_bytes = one_row_schema()
    return tuple(_plan_one(packet, entry, code_index, schema_bytes, answers)
                 for entry in _CHECKLIST_CATALOG)


_ONE_ROW_KEYS = frozenset({"status", "rationale", "evidence", "finding"})
_ONE_ROW_FINDING_KEYS = frozenset(
    {"summary", "suggested_change", "evidence"})
_ONE_ROW_CITATION_KEYS = frozenset({
    "path", "start_line", "end_line", "sha256",
})


def _invalid_reply(message: str) -> C.Problem:
    return C.Problem(code="invalid-assessment", message=message,
                     phase=_PHASE, retryable=False)


# One enclosing markdown fence (``` with an optional json tag) around an
# otherwise valid one-row reply: this is the shape Claude serves, so it is
# unwrapped before JSON parsing. Only surrounding whitespace may sit
# outside the fence; prose, a second block, or backticks inside the JSON
# leave the text untouched so validation still rejects it as non-JSON.
_FENCE_RE = re.compile(
    r"\A[ \t\r\n]*```[ \t]*(?:[Jj][Ss][Oo][Nn])?[ \t]*\r?\n"
    r"(.*?)\r?\n[ \t]*```[ \t\r\n]*\Z",
    re.DOTALL,
)


def _unwrap_single_fence(text: str) -> str:
    """Unwrap exactly one enclosing fence, else return the text unchanged."""
    match = _FENCE_RE.match(text)
    if match is None:
        return text
    inner = match.group(1)
    if "```" in inner:
        return text
    return inner


# Leading reply markers stripped to compress a validation message down to
# its distinguishing detail for per-item failure rows ("reply is not
# JSON" becomes "not JSON"). Order matters: "reply is " precedes "reply ".
_REPLY_DETAIL_PREFIXES = ("reply is ", "reply ", "reply.")

# A specific validation detail never costs a row more than this many chars.
_INVALID_REASON_MAX_CHARS = 160


def _short_reply_detail(message: str) -> str:
    """Compress a one-row validation message to its distinguishing detail."""
    for prefix in _REPLY_DETAIL_PREFIXES:
        if message.startswith(prefix):
            return message[len(prefix):]
    return message


def _normalize_one_row_prose(text: str) -> str:
    """Strip inline-code backticks so cheap-model prose stays plain text.

    Only the backtick characters go: links, autolinks, bare URLs, HTML,
    headings, pipes, percent figures, execution-claim words, and command
    shapes are still rejected exactly as before (a command shape wrapped
    in backticks is still a command shape once they are removed).
    """
    return text.replace("`", "")


def _check_one_row_prose(text: object, ctx: str) -> str:
    if not isinstance(text, str) or not text:
        raise _invalid_reply(f"{ctx} must be nonempty prose")
    normalized = _normalize_one_row_prose(text)
    if not normalized:
        raise _invalid_reply(f"{ctx} must be nonempty prose")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid_reply(f"{ctx} is not valid UTF-8") from None
    if size > _ONE_ROW_PROSE_MAX_BYTES:
        raise _invalid_reply(f"{ctx} exceeds its bound")
    if C.aa_prose_is_untrusted(normalized):
        raise _invalid_reply(f"{ctx} carries untrusted model content")
    return normalized


def _bind_one_row_citations(items: object, subset: dict,
                            ctx: str) -> tuple[tuple, int]:
    """Bind citations, dropping invalid ones instead of failing the item.

    Returns ``(valid_citations, dropped_count)``. A citation that is
    malformed, cites evidence outside the item subset (including files
    excluded at admission, such as empty files), carries a stale
    identity, or escapes its excerpt is dropped and counted; the caller
    fails the item only when its status demands citations and none
    remain. A non-list or over-long evidence value still fails the row.
    """
    if not isinstance(items, list) or len(items) > _ONE_ROW_EVIDENCE_MAX:
        raise _invalid_reply(f"{ctx} must be a list of at most 16 citations")
    citations: list[Citation] = []
    dropped = 0
    for position, item in enumerate(items):
        entry_ctx = f"{ctx}[{position}]"
        try:
            _require_exact_keys(item, _ONE_ROW_CITATION_KEYS, entry_ctx)
            try:
                citation = Citation(path=item["path"],
                                    start_line=item["start_line"],
                                    end_line=item["end_line"],
                                    sha256=item["sha256"])
            except (TypeError, ValueError):
                raise _invalid_reply(
                    f"{entry_ctx} is not a valid citation") from None
            excerpt = subset.get(citation.path)
            if excerpt is None:
                raise _invalid_reply(
                    f"{entry_ctx} cites evidence outside the item subset")
            if citation.sha256 != excerpt.sha256:
                raise _invalid_reply(
                    f"{entry_ctx} citation identity is stale")
            if not (excerpt.start_line <= citation.start_line
                    <= citation.end_line <= excerpt.end_line):
                raise _invalid_reply(
                    f"{entry_ctx} citation escapes its excerpt")
        except C.Problem:
            dropped += 1
            continue
        citations.append(citation)
    return tuple(citations), dropped


def _validate_one_row(reply: bytes, subset: dict, entry) -> tuple:
    """Validate one one-row reply; return (AssessmentRow, Finding | None)."""
    if len(reply) > MAX_PAYLOAD_BYTES:
        raise _invalid_reply("reply exceeds its bound")
    try:
        text = reply.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid_reply("reply is not valid UTF-8") from None
    text = _unwrap_single_fence(text)
    try:
        document = json.loads(text)
    except (RecursionError, ValueError):
        raise _invalid_reply("reply is not JSON") from None
    _require_exact_keys(document, _ONE_ROW_KEYS, "reply")
    status = document["status"]
    if not isinstance(status, str) or status not in _VALID_STATUSES:
        raise _invalid_reply("reply has an unknown status")
    rationale = _check_one_row_prose(document["rationale"],
                                     "reply.rationale")
    if rationale.startswith((SKIP_PREFIX, FAILED_PREFIX,
                             PTEST_ANSWER_PREFIX)):
        raise _invalid_reply("reply carries a ptest-owned prefix")
    evidence, dropped = _bind_one_row_citations(document["evidence"],
                                                subset, "reply.evidence")
    if status in ("satisfied", "gap", "not-applicable") and not evidence:
        raise _invalid_reply("no valid citations")
    if status == "not-applicable" and sum(
            1 for char in rationale if not char.isspace()) < (
                _SKIP_RATIONALE_MIN_NONSPACE):
        raise _invalid_reply("reply needs a specific not-applicable rationale")
    raw_finding = document["finding"]
    finding = None
    if status == "gap":
        if not isinstance(raw_finding, dict):
            raise _invalid_reply("gap reply needs a finding")
        _require_exact_keys(raw_finding, _ONE_ROW_FINDING_KEYS,
                            "reply.finding")
        summary = _check_one_row_prose(raw_finding["summary"],
                                       "reply.finding.summary")
        change = _check_one_row_prose(raw_finding["suggested_change"],
                                      "reply.finding.suggested_change")
        finding_evidence, finding_dropped = _bind_one_row_citations(
            raw_finding["evidence"], subset, "reply.finding.evidence")
        if not finding_evidence:
            raise _invalid_reply("finding has no valid citations")
        dropped += finding_dropped
        finding = Finding(id=entry.id, summary=summary,
                          suggested_change=change, recipe_id=entry.recipe,
                          evidence=finding_evidence)
    elif raw_finding is not None:
        raise _invalid_reply("non-gap reply must carry finding null")
    row = AssessmentRow(id=entry.id, status=status, rationale=rationale,
                        evidence=evidence, label=entry.label,
                        dropped_citations=dropped)
    return row, finding


def _skip_child_row(packet: EvidencePacket, entry,
                    review: ItemReview) -> AssessmentRow:
    """Build the deterministic N/A row for a skipped review."""
    assert review.skip_reason is not None
    citations = tuple(
        Citation(path=manifest.path, start_line=manifest.start_line,
                 end_line=manifest.end_line, sha256=manifest.sha256)
        for manifest in _manifest_excerpts(packet))
    return AssessmentRow(id=entry.id, status="not-applicable",
                         rationale=review.skip_reason, evidence=citations,
                         label=entry.label)


def _answer_child_row(packet: EvidencePacket, entry,
                      answer: DeterministicAnswer) -> tuple:
    """Build the model-free row (and gap finding) for a deterministic answer.

    The rationale carries the ptest-owned prefix with whole-excerpt
    citations. A satisfied, gap, or not-applicable answer with no citable
    excerpt degrades to unknown naming the missing review evidence.
    """
    known = {excerpt.path: excerpt for excerpt in packet.excerpts}
    citations: list[Citation] = []
    missing = False
    for path in answer.evidence_paths:
        excerpt = known.get(path)
        if excerpt is None:
            missing = True
            break
        citations.append(Citation(path=excerpt.path,
                                  start_line=excerpt.start_line,
                                  end_line=excerpt.end_line,
                                  sha256=excerpt.sha256))
    if answer.status in ("satisfied", "gap", "not-applicable") and (
            missing or not citations):
        return (AssessmentRow(
            id=entry.id, status="unknown",
            rationale=(PTEST_ANSWER_PREFIX + answer.reason
                       + " (the ptest config is not in the review "
                       "evidence)"),
            evidence=(), label=entry.label), None)
    row = AssessmentRow(id=entry.id, status=answer.status,
                        rationale=PTEST_ANSWER_PREFIX + answer.reason,
                        evidence=tuple(citations), label=entry.label)
    finding = None
    if answer.status == "gap":
        finding = Finding(id=entry.id, summary=answer.finding_summary,
                          suggested_change=answer.finding_change,
                          recipe_id=None, evidence=tuple(citations))
    return row, finding


def assemble_child(packet: EvidencePacket, reviews: tuple[ItemReview, ...],
                   replies: tuple[bytes | str | None, ...]) -> ChildAssessment:
    """Assemble one child assessment from per-item replies.

    ``replies`` align with ``reviews``: bytes hold a normalized provider
    payload, str holds a failure reason, and None marks a skipped or
    deterministically answered review. A failing reply becomes an
    ``unknown`` row; only misaligned inputs raise, plus stale-evidence
    when the reviews belong to another packet.
    """
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    if not isinstance(reviews, tuple) or not all(
            isinstance(review, ItemReview) for review in reviews):
        raise TypeError("reviews must be a tuple of ItemReview")
    if not isinstance(replies, tuple):
        raise TypeError("replies must be a tuple")
    if len(reviews) != len(replies):
        raise ValueError("reviews and replies must align")
    known = {excerpt.path: excerpt for excerpt in packet.excerpts}
    for review in reviews:
        if review.item_id not in _CATALOG_BY_ID:
            raise ValueError(f"review {review.item_id!r} is not a catalog item")
        if review.scope != packet.scope or any(
                path not in known for path in review.excerpt_paths):
            raise C.Problem(code="stale-evidence",
                            message="item reviews belong to another packet",
                            phase=_PHASE, retryable=False)
    rows: list[AssessmentRow] = []
    findings: list[Finding] = []
    for review, reply in zip(reviews, replies):
        entry = _CATALOG_BY_ID[review.item_id]
        if review.answer is not None:
            if reply is not None:
                raise ValueError("answered reviews take no reply")
            row, finding = _answer_child_row(packet, entry, review.answer)
            rows.append(row)
            if finding is not None:
                findings.append(finding)
            continue
        if review.request is None:
            if reply is not None:
                raise ValueError("skipped reviews take no reply")
            rows.append(_skip_child_row(packet, entry, review))
            continue
        if reply is None:
            raise ValueError("planned requests take a reply")
        if isinstance(reply, str):
            rows.append(AssessmentRow(
                id=entry.id, status="unknown",
                rationale=FAILED_PREFIX + reply, evidence=(),
                label=entry.label))
            continue
        if not isinstance(reply, (bytes, bytearray)):
            raise TypeError("replies must be bytes, str, or None")
        subset = {path: known[path] for path in review.excerpt_paths}
        try:
            row, finding = _validate_one_row(bytes(reply), subset, entry)
        except C.Problem as exc:
            # Untrusted model shapes must never abort the child assembly:
            # any validation failure becomes an unknown row carrying its
            # specific reason for the terminal and report rows.
            detail = _short_reply_detail(exc.message)
            if len(detail) > _INVALID_REASON_MAX_CHARS:
                detail = detail[:_INVALID_REASON_MAX_CHARS]
            row = AssessmentRow(id=entry.id, status="unknown",
                                rationale=(FAILED_PREFIX + "invalid reply: "
                                           + detail),
                                evidence=(), label=entry.label)
            finding = None
        except (TypeError, ValueError):
            row = AssessmentRow(id=entry.id, status="unknown",
                                rationale=FAILED_PREFIX + "invalid reply",
                                evidence=(), label=entry.label)
            finding = None
        rows.append(row)
        if finding is not None:
            findings.append(finding)
    computed = score(tuple(rows))
    return ChildAssessment(
        packet_sha256=packet.packet_sha256,
        project_id=packet.project_id, scope=packet.scope,
        rows=tuple(rows), findings=tuple(findings), score=computed)


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


__all__ = [
    "EvidenceLimits", "SourceExcerpt", "DependencyFact", "EvidencePacket",
    "Citation", "AssessmentRow", "Finding", "Score", "ChildAssessment",
    "ItemReview",
    "build_packets", "score",
    "plan_item_reviews", "assemble_child", "one_row_schema",
    "MAX_FILES_PER_CHILD", "MAX_BYTES_PER_CHILD", "MAX_BYTES_PER_FILE",
    "MAX_PROMPT_BYTES",
    "ITEM_MAX_FILES", "ITEM_MAX_BYTES", "SKIP_PREFIX", "FAILED_PREFIX",
    "PTEST_ANSWER_PREFIX",
]
