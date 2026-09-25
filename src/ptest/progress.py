"""One small stderr status/progress helper for run output.

Used by operations (during a run) and cli (refusal lines, monorepo
totals). Plain ``ptest:``-prefixed words on stderr; runner stdout/stderr
stay untouched. Dynamic fields go through ``render.terminal_text`` so
hostile names are escaped, and variable-length segments are cut to the
terminal width so mandated suffixes (verdict, hint) always survive.
"""
from __future__ import annotations

import os
import sys

from . import contracts as C
from . import render
from .project_facts import terminal_width

HINT = "run with ptest -v for scheduling and setup details"

# A grant slower than this earns one waiting line; repeats stay this far
# apart so a queued run never becomes a spinner stream.
WAIT_FIRST_S = 1.0
WAIT_REPEAT_S = 15.0

_hint_shown = False


def reset() -> None:
    """Start one run's hint accounting (the hint fires at most once)."""
    global _hint_shown
    _hint_shown = False


def claim_hint() -> bool:
    """Take the once-per-run verbosity hint; False when already shown."""
    global _hint_shown
    if _hint_shown:
        return False
    _hint_shown = True
    return True


def format_duration(seconds: float | None) -> str:
    try:
        value = max(0.0, float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "0.0s"
    if value < 60:
        return f"{value:.1f}s"
    total = int(value)
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m{secs}s"


def format_timeout(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds:.1f}s"


def fit_text(text: str, *, fixed: int, width: int | None = None) -> str:
    """Cut a variable segment to the terminal width, keeping room for fixed text."""
    room = max(0, terminal_width(width) - fixed)
    if len(text) <= room:
        return text
    if room <= 1:
        return "…"
    return text[:room - 1] + "…"


def _prefix(*, color: bool = False) -> str:
    return render.paint("ptest:", "dim", color=color)


def _project(text: str, *, color: bool = False) -> str:
    return render.paint(text, "bold", color=color)


def format_start(*, project: str, runner: str, workers: int,
                 scope: str, full: bool, color: bool = False) -> str:
    head = f"{_prefix(color=color)} {_project(project, color=color)} · {runner}"
    if full:
        return f"{head} · full suite"
    dimmed_scope = render.paint(scope, "dim", color=color) if scope else scope
    return f"{head} · {C.plural(workers, 'worker')} · {dimmed_scope}"


def format_waiting(*, needed: int, free: int | None, limit: int | None,
                   timeout_s: float, holders: str = "",
                   elapsed_s: float | None = None,
                   position: int | None = None, hint: bool = False,
                   color: bool = False) -> str:
    if limit is None or free is None:
        capacity = "capacity unknown"
    else:
        capacity = f"{free} of {limit} free"
    need = C.plural(needed, "slot")
    waiting = render.paint("waiting", "yellow", color=color)
    if elapsed_s is not None:
        return (f"{_prefix(color=color)} still {waiting} for {need} ({capacity})"
                f" · {format_duration(elapsed_s)}")
    line = f"{_prefix(color=color)} {waiting} for {need} ({capacity})"
    if holders:
        line += f" — in use by {holders}"
    line += f" · queue timeout {format_timeout(timeout_s)}"
    if position is not None:
        line += f" (position {position})"
    if hint:
        line += f" · {HINT}"
    return line


def format_setup_start(argv: tuple[str, ...] | list[str], *, reason: str,
                       color: bool = False) -> str:
    return (f"{_prefix(color=color)} setup: "
            f"{render.terminal_text(' '.join(argv))} ({reason})")


def format_setup_done(setup_s: float | None, *, color: bool = False) -> str:
    if setup_s is None:
        return f"{_prefix(color=color)} setup done"
    return f"{_prefix(color=color)} setup done ({format_duration(setup_s)})"


def format_setup_failed(*, exit_code: int | None = None,
                        problem_code: str | None = None,
                        color: bool = False) -> str:
    if exit_code is not None:
        return f"{_prefix(color=color)} setup failed (exit {exit_code})"
    if problem_code is not None:
        return f"{_prefix(color=color)} setup failed ({problem_code})"
    return f"{_prefix(color=color)} setup failed"


def format_setup_run_start(project: str, argv: tuple[str, ...] | list[str], *,
                           color: bool = False) -> str:
    """Start line for a setup-only run: project-scoped, never a test run."""
    return (f"{_prefix(color=color)} {_project(project, color=color)} · "
            f"setup: {render.terminal_text(' '.join(argv))}")


def format_setup_run_done(duration_s: float | None, *,
                          color: bool = False) -> str:
    """End line for a passing setup-only run."""
    return (f"{_prefix(color=color)} setup done · "
            f"{render.paint(format_duration(duration_s), 'dim', color=color)}")


_VERDICTS = {
    C.Status.PASSED: "passed",
    C.Status.FAILED: "failed",
    C.Status.CANCELLED: "cancelled",
    C.Status.INCOMPLETE: "incomplete",
    C.Status.NO_TESTS_NEEDED: "no tests needed",
    C.Status.NOT_RUN: "not run",
}


def format_counts(counts: C.Counts | None, status: C.Status) -> str | None:
    if counts is None:
        return None
    failed = counts.failed or 0
    passed = counts.passed or 0
    if failed or status is C.Status.FAILED:
        return f"{failed} failed, {passed} passed"
    total = counts.collected
    if total is None:
        total = counts.executed
    if total is None:
        total = passed
    return C.plural(total, "test")


_VERDICT_STYLES = {
    C.Status.PASSED: "green",
    C.Status.FAILED: "red",
    C.Status.INCOMPLETE: "red",
    C.Status.CANCELLED: "yellow",
}


def format_end(status: C.Status, *, counts: C.Counts | None,
               duration_s: float | None, exit_code: int,
               hint: bool = False, lead: str | None = None,
               color: bool = False) -> str:
    verdict = render.paint(_VERDICTS[status], _VERDICT_STYLES.get(status, ""),
                           color=color)
    duration = render.paint(format_duration(duration_s), "dim", color=color)
    parts = [f"{_prefix(color=color)} {lead}", verdict] if lead else [
        f"{_prefix(color=color)} {verdict}"]
    segment = format_counts(counts, status)
    if segment is not None:
        parts.append(segment)
    parts.append(duration)
    line = " · ".join(parts)
    if exit_code != 0:
        line += f" (exit {exit_code})"
    if hint:
        line += f" · {HINT}"
    return line


def format_timing(timings: C.Timings | None, *,
                  color: bool = False) -> str | None:
    if timings is None:
        return None
    parts = []
    for label, value in (("queue", timings.queue_s),
                         ("setup", timings.setup_s),
                         ("run", timings.execution_s),
                         ("finish", timings.finalization_s)):
        if value is not None:
            parts.append(f"{label} {format_duration(value)}")
    if not parts:
        return None
    return f"{_prefix(color=color)} -v timing: " + " · ".join(parts)


def holder_label(pid: int, *, proc_root: str = "/proc") -> str:
    """Best-effort ``dirname (pid N)`` label; pid-only when unknown."""
    try:
        if os.name != "posix":
            raise OSError("no proc filesystem")
        name = os.path.basename(os.readlink(f"{proc_root}/{pid}/cwd"))
        if not name:
            raise OSError("empty holder directory name")
        return f"{render.terminal_text(name)} (pid {pid})"
    except (OSError, ValueError):
        return f"pid {pid}"


def emit(line: str, *, quiet: bool = False, stream=None) -> bool:
    """Write one status line to stderr; quiet suppresses it."""
    if quiet or not line:
        return False
    print(line, file=sys.stderr if stream is None else stream)
    return True


__all__ = [
    "HINT", "WAIT_FIRST_S", "WAIT_REPEAT_S",
    "reset", "claim_hint",
    "format_duration", "format_timeout", "fit_text",
    "format_start", "format_waiting",
    "format_setup_start", "format_setup_done", "format_setup_failed",
    "format_setup_run_start", "format_setup_run_done",
    "format_end", "format_timing", "holder_label", "emit",
]
