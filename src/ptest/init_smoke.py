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
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from . import executability as _exec_check
from . import files as _files
from . import monorepo as _monorepo
from . import operations
from .doctor import match_rules
from .render import colors_enabled, paint, terminal_text

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

SMOKE_QUESTION = "Run a quick smoke test to confirm ptest works? [Y/n]"
SETUP_QUESTION = ("ptest needs to run setup ({argv}) once before the smoke "
                  "test; run it? [Y/n]")

# Smoke never waits on a busy admission queue: a queue timeout is a skip.
SMOKE_QUEUE_TIMEOUT_S = 60

_MAX_FILE_BYTES = 64 * 1024
_MAX_ENTRIES = 2000
_MAX_SCAN = 32
_MAX_LINES = 5
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
    setup_argv: tuple[str, ...] | None = None  # setup owed at skip time


@dataclass(frozen=True, slots=True)
class SmokePlan:
    project: str
    config: C.Config | None
    candidate: str | None  # project-relative test path, or None
    skip_reason: str | None  # set iff the project will not execute
    setup_argv: tuple[str, ...] | None = None  # setup owed before smoke


def parse_consent(answer: str) -> bool:
    """``[Y/n]`` semantics: empty or an explicit yes runs, anything else skips."""
    return answer.strip().lower() in ("", "y", "yes")


def _safe_root(test_root: str) -> bool:
    # A dot root is safe: the shared selection walks src/__tests__ first
    # under it, so the bounded walk reaches real tests instead of
    # alphabetically-first tooling directories.
    if test_root in (".", ""):
        return True
    if test_root.startswith(("-", "@", "/")) or "\\" in test_root \
            or "::" in test_root:
        return False
    return not any(part in {"", ".", ".."} for part in test_root.split("/"))


def _collect(root: Path, test_roots: tuple[str, ...],
             kind: C.RunnerKind) -> list[tuple[int, str]]:
    """(size, project-relative path) of candidate test files, bounded.

    Uses the shared executability selection, so the smoke candidate and
    the executability example pick from the same filtered traversal; only
    selected candidates pay for a size stat.
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    budget = [_MAX_ENTRIES]
    for test_root in test_roots:
        if not _safe_root(test_root):
            continue
        for rel in _exec_check.iter_candidates(root, test_root, kind, budget):
            if rel in seen:
                continue
            seen.add(rel)
            try:
                size = os.lstat(root / rel).st_size
            except OSError:
                continue
            found.append((size, rel))
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
    """Bounded no-follow read shared with the rest of ptest's static scans."""
    try:
        raw = _files.read_regular(root, rel, _MAX_FILE_BYTES + 1)
    except C.Problem:
        return None
    if len(raw) > _MAX_FILE_BYTES or len(raw) == 0:
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
    Monorepo children resolve through ``preflight_children``, the same
    path ``ptest <path>`` uses; a preflight Problem skips every child.
    """
    manifest = getattr(resolution, "monorepo", None)
    children = tuple(getattr(manifest, "children", ()) or ()) \
        if manifest is not None else ()
    if children:
        try:
            targets = _monorepo.preflight_children(
                resolution.root, manifest)
        except C.Problem as problem:
            return tuple(
                SmokePlan(project=declaration, config=None, candidate=None,
                          skip_reason=problem.message)
                for declaration in children)
        return tuple(
            _plan_for_config(
                target.declaration, target.config, target.directory,
                domain)
            for target in targets)
    if resolution.config is None:
        return ()
    return (_plan_for_config(".", resolution.config, resolution.root, domain),)


def _plan_for_config(project: str, config: C.Config, root: Path,
                     domain: C.DomainPaths) -> SmokePlan:
    item = _exec_check.check_config(config, project=project)
    if item.status == _exec_check.STATUS_NOT_EXECUTABLE:
        return SmokePlan(project=project, config=config, candidate=None,
                         skip_reason=item.reason or "not runnable")
    setup_argv = None
    if operations.setup_blocker(domain, config) is not None \
            and config.setup is not None:
        # Setup would run (missing paths or no recorded fingerprint): the
        # plan stays runnable so TTY init can offer to run setup first.
        setup_argv = tuple(config.setup.argv)
    candidate = choose_candidate(
        root, config.runner.kind, config.runner.test_roots)
    if candidate is None:
        roots = ", ".join(config.runner.test_roots) or "."
        return SmokePlan(
            project=project, config=config, candidate=None,
            skip_reason=f"no fixture-free smoke candidate under {roots}")
    return SmokePlan(project=project, config=config, candidate=candidate,
                     skip_reason=None, setup_argv=setup_argv)


def setup_advice(plan: SmokePlan) -> str:
    """Working advice for a setup-owed plan that will not run setup."""
    argv = " ".join(plan.setup_argv or ())
    return (f"setup baseline not recorded; run: "
            f"{display_command(plan.project, plan.candidate)} "
            f"(runs {argv} first)")


def skip_result(plan: SmokePlan, reason: str) -> SmokeResult:
    """One skipped row for a plan that never executes."""
    return SmokeResult(
        project=plan.project, status=STATUS_SKIPPED,
        command=display_command(plan.project, plan.candidate),
        duration_s=None, exit_code=None, lines=(), reason=reason,
        setup_argv=plan.setup_argv)


def run_setup(domain: C.DomainPaths, plan: SmokePlan, *,
              fixture_domain: Path | None = None) -> str | None:
    """Run declared setup once through the setup-only path; None when ready.

    Setup executes without running any tests, and records the setup
    fingerprint on success. The later smoke run then uses
    ``no_setup=True`` and executes the candidate exactly once.
    A Problem or infrastructure error becomes a skip reason; so does a
    setup run that leaves the baseline unrecorded.
    """
    if plan.config is None or plan.candidate is None \
            or plan.setup_argv is None:
        return "smoke unavailable"
    try:
        result = operations.run_setup_only(
            domain, plan.config, queue_timeout_s=SMOKE_QUEUE_TIMEOUT_S,
            fixture_domain=fixture_domain)
    except C.Problem as problem:
        return problem.message
    except Exception as error:  # never break init on smoke infrastructure
        return f"smoke error: {type(error).__name__}"
    if ((result is not None and result.status is not C.Status.PASSED)
            or operations.setup_blocker(domain, plan.config) is not None):
        return setup_advice(plan)
    return None


def _announce(project: str, candidate: str | None) -> None:
    """Print the smoke line before executing, so TTY output stays ordered.

    The setup run needs no announce: its own
    ``ptest: <project> · setup: <argv>`` start line says it.
    """
    target = terminal_text(candidate) if candidate else "full suite"
    sys.stdout.write(f"ptest: {terminal_text(project)} · smoke: {target}\n")
    sys.stdout.flush()


def _useful_lines(result: C.RunResult) -> tuple[str, ...]:
    lines = [f"{reason.code}: {reason.message}" for reason in result.reasons
             if getattr(reason, "message", None)]
    if not lines:
        # The runner streams test output above this line; there is no
        # further machine detail to repeat.
        lines = ["see runner output above"]
    return tuple(lines[:_MAX_LINES])


def run_plan(domain: C.DomainPaths, plan: SmokePlan, *,
             fixture_domain: Path | None = None) -> SmokeResult:
    """Execute one plan in-process through the scoped runner.

    Never raises for runner outcomes: provider/test failures become
    ``failed``, infrastructure problems become ``skipped``. The smoke
    request always opts out of setup (``no_setup=True``): owed setup runs
    first through :func:`run_setup`, and anything still owed here refuses
    and becomes a skip. The written configuration is never touched and
    init's exit status stays configuration-based upstream.
    """
    command = display_command(plan.project, plan.candidate)
    if plan.skip_reason is not None or plan.config is None \
            or plan.candidate is None:
        return skip_result(plan, plan.skip_reason or "smoke unavailable")
    _announce(plan.project, plan.candidate)
    started = time.monotonic()
    try:
        result = operations.execute(
            domain, plan.config,
            C.RunRequest(mode=C.Mode.SCOPED, argv=(plan.candidate,),
                         queue_timeout_s=SMOKE_QUEUE_TIMEOUT_S,
                         no_setup=True, fixture_domain=fixture_domain))
    except C.Problem as problem:
        return skip_result(plan, problem.message)
    except Exception as error:  # never break init on smoke infrastructure
        return skip_result(plan, f"smoke error: {type(error).__name__}")
    duration = time.monotonic() - started
    if result.status is C.Status.PASSED:
        return SmokeResult(
            project=plan.project, status=STATUS_PASSED, command=command,
            duration_s=duration, exit_code=None, lines=(), reason=None)
    exit_code = result.exit_code if isinstance(result.exit_code, int) else 1
    return SmokeResult(
        project=plan.project, status=STATUS_FAILED, command=command,
        duration_s=None, exit_code=exit_code,
        lines=_useful_lines(result), reason=None)


def _smoke_cell(item: SmokeResult, *, color: bool = False) -> str:
    """One compact cell per smoke result; every field is sanitized."""
    bracket = "NO_COLOR" in os.environ
    project = terminal_text(item.project)
    if item.status == STATUS_PASSED:
        duration = (f"{item.duration_s:.1f}s"
                    if isinstance(item.duration_s, (int, float)) else "?")
        mark = "[ok]" if bracket else paint("✓", "green", color=color)
        return f"{project} {mark} {duration}"
    if item.status == STATUS_FAILED:
        mark = "[fail]" if bracket else paint("✗", "red", color=color)
        return f"{project} {mark} exit {item.exit_code}"
    return f"{project} – {terminal_text(item.reason or 'skipped')}"


def format_smoke(results: tuple[SmokeResult, ...], *,
                 width: int | None = None, color: bool = False) -> str:
    """Render one compact ``smoke`` row; ``""`` when empty.

    The row shares the file-action grid (``  smoke      <cells>``) so it
    aligns with the grouped file lines; cells share one line (wrapped
    between cells only); each failed cell contributes at most
    ``_MAX_LINES`` indented detail lines below. The ``smoke`` label is
    dimmed on color terminals, after layout so columns never shift.
    """
    from .project_facts import wrap_atoms
    if not results:
        return ""
    cells = [_smoke_cell(item, color=color) for item in results]
    indent = f"  {'smoke':<10} "
    lines = list(wrap_atoms(cells, width, indent=indent,
                            hang=" " * len(indent), sep="   "))
    if colors_enabled(color):
        dimmed = paint("smoke", "dim", color=True)
        lines = [line.replace("  smoke     ", f"  {dimmed}     ", 1)
                 if line.startswith("  smoke     ") else line
                 for line in lines]
    for item in results:
        if item.status == STATUS_FAILED:
            for detail in item.lines[:_MAX_LINES]:
                lines.append(f"  {terminal_text(detail)}")
    return "\n".join(lines) + "\n"
