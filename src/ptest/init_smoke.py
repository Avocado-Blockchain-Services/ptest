"""Init smoke run: one small real test per project through the scoped runner.

After init writes the configuration and the deterministic executability
check passes, init may execute a single small real test per project with
ptest's own scoped runner (the same in-process path as ``ptest <path>``:
isolation, timeouts, exit codes). A smoke failure never rolls back the
written configuration and never changes init's exit status.

Candidate choice is deterministic: the smallest test file under the
project's test roots whose static scan shows no database, network, or
service fixture. The cheap-model pick from the requirements is deferred
(see the section-E amendment); only the deterministic choice exists.
"""
from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as config_api
from . import contracts as C
from . import executability as _exec_check
from . import operations
from .doctor import match_rules
from .render import terminal_text

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

SMOKE_QUESTION = "Run a quick smoke test to confirm ptest works? [Y/n]"

_MAX_FILE_BYTES = 64 * 1024
_MAX_ENTRIES = 2000
_MAX_DEPTH = 6
_MAX_SCAN = 32
_MAX_LINES = 5
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__"})
_VITEST_TEST_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mts|cts|mjs|cjs)$")
# Database, network, or service library names, any language. Conservative:
# a hit only disqualifies one file, never the project.
_TAINT_NAMES = re.compile(
    r"\b(sqlalchemy|django|psycopg2?|pg8000|asyncpg|aiopg|pymongo|motor"
    r"|redis|sqlite3|socket|requests|httpx|urllib3?|aiohttp|docker"
    r"|testcontainers|boto3|botocore|grpc|kafka|celery|pika|kombu"
    r"|mysql|postgres|mongodb|mssql|ldap|smtplib|ftplib|paramiko)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SmokeResult:
    project: str
    status: str  # STATUS_PASSED | STATUS_FAILED | STATUS_SKIPPED
    command: str  # scoped command that ran, or the project label
    duration_s: float | None  # set for passed runs only
    exit_code: int | None  # set for failed runs only
    lines: tuple[str, ...]  # first useful lines for failed runs
    reason: str | None  # set for skipped runs only


@dataclass(frozen=True, slots=True)
class SmokePlan:
    project: str
    config: C.Config | None
    candidate: str | None  # project-relative test path, or None
    skip_reason: str | None  # set iff the project will not execute


def parse_consent(answer: str) -> bool:
    """``[Y/n]`` semantics: empty or an explicit yes runs, anything else skips."""
    return answer.strip().lower() in ("", "y", "yes")


def _name_ok(name: str, kind: C.RunnerKind) -> bool:
    if kind is C.RunnerKind.PYTEST:
        return (name.startswith("test_") and name.endswith(".py")) \
            or name.endswith("_test.py")
    if kind is C.RunnerKind.VITEST:
        return _VITEST_TEST_RE.search(name) is not None
    return False


def _safe_root(test_root: str) -> bool:
    if test_root in (".", ""):
        return False
    if test_root.startswith(("-", "@", "/")) or "\\" in test_root \
            or "::" in test_root:
        return False
    return not any(part in {"", ".", ".."} for part in test_root.split("/"))


def _collect(root: Path, test_roots: tuple[str, ...],
             kind: C.RunnerKind) -> list[tuple[int, str]]:
    """(size, project-relative path) of candidate test files, bounded."""
    found: list[tuple[int, str]] = []
    budget = [_MAX_ENTRIES]

    def walk(start: Path, depth: int) -> None:
        try:
            entries = sorted(os.scandir(start), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
            except OSError:
                continue
            if name in _SKIP_DIRS or name.startswith("."):
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            if is_dir:
                if depth > 0:
                    walk(path, depth - 1)
                continue
            if not _name_ok(name, kind):
                continue
            try:
                rel = path.relative_to(root).as_posix()
                size = entry.stat(follow_symlinks=False).st_size
            except (ValueError, OSError):
                continue
            found.append((size, rel))

    for test_root in test_roots:
        if not _safe_root(test_root):
            continue
        base = root / test_root
        try:
            stamp = os.lstat(base)
        except OSError:
            continue
        if not stat.S_ISDIR(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
            continue
        walk(base, _MAX_DEPTH)
    return found


def _has_test_marker(text: str, kind: C.RunnerKind) -> bool:
    if kind is C.RunnerKind.PYTEST:
        return "def test_" in text
    return "test(" in text or "it(" in text


def _tainted(text: str) -> bool:
    """True when the static scanner sees a database, network, or service fixture."""
    try:
        codes = match_rules(text)
    except (TypeError, ValueError):
        return True
    for code in codes:
        if code.split(".", 1)[0] in ("db", "network", "process"):
            return True
    return _TAINT_NAMES.search(text) is not None


def _read_text(root: Path, rel: str) -> str | None:
    try:
        stamp = os.lstat(root / rel)
    except OSError:
        return None
    if not stat.S_ISREG(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
        return None
    if stamp.st_size > _MAX_FILE_BYTES or stamp.st_size == 0:
        return None
    try:
        with open(root / rel, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def choose_candidate(root: Path, kind: C.RunnerKind,
                     test_roots: tuple[str, ...]) -> str | None:
    """Smallest fixture-free test file under the test roots, or None.

    Pure static inspection: no model, no subprocess, no project import.
    Files with no test definition sort after files that define a test, so
    the smoke run executes a real test instead of an empty collection.
    """
    if kind not in (C.RunnerKind.PYTEST, C.RunnerKind.VITEST):
        return None
    if not isinstance(root, Path):
        raise TypeError("choose_candidate requires a Path root")
    entries = sorted(_collect(root, tuple(test_roots), kind))
    scanned = 0
    deferred: list[str] = []
    for _size, rel in entries:
        if scanned >= _MAX_SCAN:
            break
        text = _read_text(root, rel)
        if text is None:
            continue
        scanned += 1
        if _tainted(text):
            continue
        if _has_test_marker(text, kind):
            return rel
        deferred.append(rel)
    return deferred[0] if deferred else None


def display_command(project: str, candidate: str | None) -> str:
    """Scoped command label for a plan (``ptest <prefix><candidate>``)."""
    if candidate is None:
        return "ptest" if project == "." else f"ptest {project}"
    if project == ".":
        return f"ptest {candidate}"
    return f"ptest {project}/{candidate}"


def plan_resolution(resolution: C.ConfigResolution,
                    domain: C.DomainPaths) -> tuple[SmokePlan, ...]:
    """One smoke plan per configured project, in manifest order.

    Read-only: resolves configs and inspects files, never executes.
    """
    manifest = getattr(resolution, "monorepo", None)
    children = tuple(getattr(manifest, "children", ()) or ()) \
        if manifest is not None else ()
    if children:
        plans: list[SmokePlan] = []
        for declaration in children:
            child = config_api.resolve_config(resolution.root / declaration)
            if child.config is None or child.problem is not None:
                plans.append(SmokePlan(
                    project=declaration, config=None, candidate=None,
                    skip_reason="child configuration is missing or invalid"))
                continue
            plans.append(_plan_for_config(
                declaration, child.config, child.root, domain))
        return tuple(plans)
    if resolution.config is None:
        return ()
    return (_plan_for_config(".", resolution.config, resolution.root, domain),)


def _plan_for_config(project: str, config: C.Config, root: Path,
                     domain: C.DomainPaths) -> SmokePlan:
    item = _exec_check.check_config(config, project=project)
    if item.status == _exec_check.STATUS_NOT_EXECUTABLE:
        return SmokePlan(project=project, config=config, candidate=None,
                         skip_reason=item.reason or "not runnable")
    blocker = operations.setup_blocker(domain, config)
    if blocker is not None:
        argv = " ".join(config.setup.argv) if config.setup is not None else ""
        return SmokePlan(
            project=project, config=config, candidate=None,
            skip_reason=f"setup not done; run: {argv}".rstrip())
    candidate = choose_candidate(
        root, config.runner.kind, config.runner.test_roots)
    if candidate is None:
        roots = ", ".join(config.runner.test_roots) or "."
        return SmokePlan(
            project=project, config=config, candidate=None,
            skip_reason=f"no fixture-free smoke candidate under {roots}")
    return SmokePlan(project=project, config=config, candidate=candidate,
                     skip_reason=None)


def _useful_lines(result: C.RunResult, exit_code: int) -> tuple[str, ...]:
    lines = [f"{reason.code}: {reason.message}" for reason in result.reasons
             if getattr(reason, "message", None)]
    if not lines:
        lines = [f"no detail (exit {exit_code})"]
    return tuple(lines[:_MAX_LINES])


def run_plan(domain: C.DomainPaths, plan: SmokePlan, *,
             fixture_domain: Path | None = None) -> SmokeResult:
    """Execute one plan in-process through the scoped runner.

    Never raises for runner outcomes: provider/test failures become
    ``failed``, infrastructure problems become ``skipped``. The written
    configuration is never touched and init's exit status stays
    configuration-based upstream.
    """
    command = display_command(plan.project, plan.candidate)
    if plan.skip_reason is not None or plan.config is None \
            or plan.candidate is None:
        return SmokeResult(
            project=plan.project, status=STATUS_SKIPPED, command=command,
            duration_s=None, exit_code=None, lines=(),
            reason=plan.skip_reason or "smoke unavailable")
    started = time.monotonic()
    try:
        result = operations.execute(
            domain, plan.config,
            C.RunRequest(mode=C.Mode.SCOPED, argv=(plan.candidate,),
                         fixture_domain=fixture_domain))
    except C.Problem as problem:
        return SmokeResult(
            project=plan.project, status=STATUS_SKIPPED, command=command,
            duration_s=None, exit_code=None, lines=(),
            reason=problem.message)
    except Exception as error:  # never break init on smoke infrastructure
        return SmokeResult(
            project=plan.project, status=STATUS_SKIPPED, command=command,
            duration_s=None, exit_code=None, lines=(),
            reason=f"smoke error: {type(error).__name__}")
    duration = time.monotonic() - started
    if result.status is C.Status.PASSED:
        return SmokeResult(
            project=plan.project, status=STATUS_PASSED, command=command,
            duration_s=duration, exit_code=None, lines=(), reason=None)
    exit_code = result.exit_code if isinstance(result.exit_code, int) else 1
    return SmokeResult(
        project=plan.project, status=STATUS_FAILED, command=command,
        duration_s=None, exit_code=exit_code,
        lines=_useful_lines(result, exit_code), reason=None)


def format_smoke(results: tuple[SmokeResult, ...]) -> str:
    """Render one bounded ``Smoke`` section; every field is sanitized."""
    if not results:
        return ""
    lines = ["Smoke"]
    for item in results:
        if item.status == STATUS_PASSED:
            duration = (f"{item.duration_s:.1f}s"
                        if isinstance(item.duration_s, (int, float)) else "?")
            lines.append(
                f"  passed: {terminal_text(item.command)} ({duration})")
        elif item.status == STATUS_FAILED:
            lines.append(
                f"  failed: {terminal_text(item.command)} "
                f"(exit {item.exit_code})")
            for detail in item.lines[:_MAX_LINES]:
                lines.append(f"    {terminal_text(detail)}")
        else:
            lines.append(
                f"  skipped: {terminal_text(item.command)} "
                f"({terminal_text(item.reason or 'skipped')})")
    return "\n".join(lines) + "\n"
