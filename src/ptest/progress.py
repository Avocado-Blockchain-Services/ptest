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


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def format_start(*, project: str, runner: str, workers: int,
                 scope: str, full: bool) -> str:
    head = f"ptest: {project} · {runner}"
    if full:
        return f"{head} · full suite"
    return f"{head} · {_plural(workers, 'worker')} · {scope}"


def format_waiting(*, needed: int, free: int | None, limit: int | None,
                   timeout_s: float, holders: str = "",
                   elapsed_s: float | None = None,
                   position: int | None = None, hint: bool = False) -> str:
    if limit is None or free is None:
        capacity = "capacity unknown"
    else:
        capacity = f"{free} of {limit} free"
    need = _plural(needed, "slot")
    if elapsed_s is not None:
        return (f"ptest: still waiting for {need} ({capacity})"
                f" · {format_duration(elapsed_s)}")
    line = f"ptest: waiting for {need} ({capacity})"
    if holders:
        line += f" — in use by {holders}"
    line += f" · queue timeout {format_timeout(timeout_s)}"
    if position is not None:
        line += f" (position {position})"
    if hint:
        line += f" · {HINT}"
    return line


def format_setup_start(argv: tuple[str, ...] | list[str], *, reason: str) -> str:
    return f"ptest: setup: {render.terminal_text(' '.join(argv))} ({reason})"


def format_setup_done(setup_s: float | None) -> str:
    if setup_s is None:
        return "ptest: setup done"
    return f"ptest: setup done ({format_duration(setup_s)})"


def format_changed_selected(*, selected: int, total: int,
                            changed_files: int) -> str:
    """Changed-mode plan segment for a selected run.

    ``selected``/``total`` count test files (the plan subset over the
    baseline inventory); ``changed_files`` counts distinct changed paths.
    """
    files = _plural(changed_files, "file") + " changed"
    return f"changed: {selected} of {total} tests ({files})"


def explain_changed_full_reason(reason: C.Reason | None, *,
                                config_name: str | None = None,
                                changed_path: str | None = None) -> str:
    """Plain words for a changed-mode full-suite selection reason code."""
    code = reason.code if reason is not None else ""
    message = reason.message if reason is not None else ""
    if code == "no-baseline":
        return "no baseline yet (this run records one if it passes on a clean tree)"
    if code == "selection-disabled":
        return f"selection is off in {config_name or 'the config'} (ptest doctor --fix)"
    if code == "policy-changed":
        if changed_path is not None:
            return f"{changed_path} is a full trigger"
        return "a full trigger changed"
    if code == "unknown-input":
        if changed_path is not None:
            return f"{changed_path} is outside the selection map"
        return "changed inputs could not be classified"
    if code == "incompatible-baseline":
        if "policy" in message or "compatibility" in message:
            return "policy changed"
        return "baseline is not an ancestor of HEAD"
    if code == "policy-invalid":
        return "policy changed"
    if code == "selection-shadow-quarantine":
        return "selection is quarantined (full suite required)"
    if code == "prior-failure":
        return "a failed test requires a full run"
    if code == "incomplete-inventory":
        return "test inventory is incomplete"
    if code == "full-gate-obligation":
        return "a full run is required"
    return "a full run is required"


def _no_baseline_detail(result: C.RunResult) -> str:
    """Plain words for why a full run recorded no baseline."""
    counts = result.counts
    failed = counts.failed if counts is not None else None
    if result.status is not C.Status.PASSED:
        if result.status is C.Status.FAILED:
            if failed:
                return _plural(failed, "failure")
            return "failed"
        return "incomplete results"
    before, after = result.input_before, result.input_after
    if before is None or after is None:
        return "incomplete results"
    if before.digest != after.digest:
        return "files changed during the run"
    if not before.clean or not after.clean:
        return "uncommitted changes"
    if any(reason.code == "incomplete-inventory" for reason in result.reasons):
        return "incomplete results"
    if any(reason.code == "changed-during-run" for reason in result.reasons):
        return "files changed during the run"
    if any(reason.code == "unknown-input" for reason in result.reasons):
        return "uncommitted changes"
    return "not eligible for a baseline"


def format_baseline_note(result: C.RunResult) -> str:
    """End-line note for a full run: baseline recorded, or why not."""
    if result.baseline_published:
        return "ptest: baseline recorded"
    return f"ptest: no baseline recorded: {_no_baseline_detail(result)}"


def format_setup_failed(*, exit_code: int | None = None,
                        problem_code: str | None = None) -> str:
    if exit_code is not None:
        return f"ptest: setup failed (exit {exit_code})"
    if problem_code is not None:
        return f"ptest: setup failed ({problem_code})"
    return "ptest: setup failed"


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
    return f"{total} tests"


def format_end(status: C.Status, *, counts: C.Counts | None,
               duration_s: float | None, exit_code: int,
               hint: bool = False, lead: str | None = None) -> str:
    parts = [f"ptest: {lead}", _VERDICTS[status]] if lead else [
        f"ptest: {_VERDICTS[status]}"]
    segment = format_counts(counts, status)
    if segment is not None:
        parts.append(segment)
    parts.append(format_duration(duration_s))
    line = " · ".join(parts)
    if exit_code != 0:
        line += f" (exit {exit_code})"
    if hint:
        line += f" · {HINT}"
    return line


def format_timing(timings: C.Timings | None) -> str | None:
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
    return "ptest: -v timing: " + " · ".join(parts)


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
    "format_changed_selected", "explain_changed_full_reason",
    "format_baseline_note",
    "format_setup_start", "format_setup_done", "format_setup_failed",
    "format_end", "format_timing", "holder_label", "emit",
]
