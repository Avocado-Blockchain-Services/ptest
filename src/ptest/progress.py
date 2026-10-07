"""One small stderr status/progress helper for run output.

Used by operations (during a run) and cli (refusal lines, monorepo
totals). Plain ``ptest:``-prefixed words on stderr; runner stdout/stderr
stay untouched. Dynamic fields go through ``render.terminal_text`` so
hostile names are escaped, and variable-length segments are cut to the
terminal width so mandated suffixes (verdict, hint) always survive.
"""
from __future__ import annotations

from pathlib import Path

import os
import sys

from . import contracts as C
from . import render
from .project_facts import terminal_width

HINT = "run with ptest -v for scheduling and setup details"

# A grant slower than this earns one waiting line; repeats start this far
# apart and double up to WAIT_REPEAT_MAX_S, so even a 4 h queue wait prints
# a few dozen lines, never a spinner stream.
WAIT_FIRST_S = 1.0
WAIT_REPEAT_S = 15.0
WAIT_REPEAT_MAX_S = 300.0


def next_wait_gap(gap: float) -> float:
    """The interval before the next repeated waiting line."""
    return min(gap * 2, WAIT_REPEAT_MAX_S)

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
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
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
                   blocker: str | None = None,
                   color: bool = False) -> str:
    """Queue wait line. ``blocker`` replaces the slot wording when slots are
    free and something else (an exclusive run, the job limit, a lock, the
    same checkout, earlier queued runs) is what the run waits for."""
    if limit is None or free is None:
        capacity = "capacity unknown"
    else:
        capacity = f"{free} of {limit} free"
    need = C.plural(needed, "slot")
    waiting = render.paint("waiting", "yellow", color=color)
    reason = (f": {render.terminal_text(blocker)}" if blocker
              else f" for {need} ({capacity})")
    if elapsed_s is not None:
        return (f"{_prefix(color=color)} still {waiting}{reason}"
                f" · {format_duration(elapsed_s)}")
    line = f"{_prefix(color=color)} {waiting}{reason}"
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
            f"{render.terminal_text(C.display_setup(argv))} ({reason})")


def format_setup_done(setup_s: float | None, *, color: bool = False) -> str:
    if setup_s is None:
        return f"{_prefix(color=color)} setup done"
    return f"{_prefix(color=color)} setup done ({format_duration(setup_s)})"


def format_changed_selected(*, selected: int, total: int,
                            changed_files: int) -> str:
    """Changed-mode plan segment for a selected run.

    ``selected``/``total`` count test files (the plan subset over the
    distinct files of the baseline inventory); ``changed_files`` counts
    distinct changed paths.
    """
    files = C.plural(changed_files, "file") + " changed"
    return f"changed: {selected} of {total} test files ({files})"


def format_changed_start(*, project: str, runner: str, segment: str,
                         color: bool = False) -> str:
    """Changed-mode start line: styled head like ``format_start``."""
    return (f"{_prefix(color=color)} {_project(project, color=color)} · "
            f"{runner} · {segment}")


def format_no_changes(declaration: str, *, color: bool = False) -> str:
    """Skip line for a monorepo child with no changes."""
    return (f"{_prefix(color=color)} {_project(declaration, color=color)} "
            "· no changes")


#: End-line hint after a narrowed pass: the graph is a subset, not the gate.
NEXT_FULL = "next: ptest --full before handoff"
#: End-line hint after any changed-mode failure.
NEXT_FIX = "fix the code under test, then rerun ptest"


def next_step(status: C.Status, narrowed: bool) -> str | None:
    """One next-step hint for a changed-mode end line, or None."""
    if status is C.Status.FAILED:
        return NEXT_FIX
    if narrowed and status in (C.Status.PASSED, C.Status.NO_TESTS_NEEDED):
        return NEXT_FULL
    return None


def format_impact(project: str, note: str, *, color: bool = False) -> str:
    """Changed-mode start line: project plus the prebuilt blast-radius note."""
    return f"{_prefix(color=color)} {_project(project, color=color)} · {note}"


def format_nothing_changed(label: str, *, color: bool = False,
                           no_green_run: bool = False) -> str:
    """Line for a changed-mode run where nothing changed anywhere."""
    suffix = " (no green run yet)" if no_green_run else ""
    return (f"{_prefix(color=color)} no changes vs {label}{suffix}"
            " — nothing to test "
            "· ptest --full runs everything")


def format_no_changes_anywhere(*, color: bool = False) -> str:
    """Nothing changed in any project, whatever each project compares against."""
    return (f"{_prefix(color=color)} no changes — nothing to test"
            " · ptest --full runs everything")


def format_no_changes_under(declaration: str, scope: str, *,
                            color: bool = False) -> str:
    """Line for a folder run whose change reaches no tests under it."""
    return (f"{_prefix(color=color)} {_project(declaration, color=color)} "
            f"· no changes under {scope} — nothing to test · "
            f"ptest --full {scope} runs all of them")


def format_no_green_changes(*, color: bool = False) -> str:
    """Line for a run where nothing changed since the last green run."""
    return (f"{_prefix(color=color)} no changes since last green run"
            " — nothing to test · ptest --full runs everything")


def format_already_verified(short_sha: str, age_s: float,
                            *, color: bool = False) -> str:
    """Skip line for a full run whose baseline already covers the inputs."""
    return (f"{_prefix(color=color)} already verified at {short_sha} "
            f"({format_duration(age_s)} ago) — ptest --full --again to rerun")


def _shorten_where(where: str) -> str:
    """Shorten a checkout path to ~/… exactly like format_verified_elsewhere."""
    home = str(Path.home())
    return ("~" + where[len(home):]
            if where == home or where.startswith(home + "/") else where)


def format_verified_elsewhere(short_sha: str, age_s: float, where: str,
                              *, color: bool = False) -> str:
    """Skip line for a full run another checkout of the project already passed."""
    shown = _shorten_where(where)
    return (f"{_prefix(color=color)} already verified at {short_sha} in "
            f"{render.terminal_text(shown)} ({format_duration(age_s)} ago) — "
            f"ptest --full --again to rerun")


def format_child_unchanged(declaration: str, short_sha: str, age_s: float,
                           where: str | None = None, *,
                           color: bool = False) -> str:
    """Skip line for a monorepo child whose inputs match its last green."""
    head = (f"{_prefix(color=color)} "
            f"{_project(render.terminal_text(declaration), color=color)} "
            f"· unchanged since green at {short_sha}")
    if where is not None:
        head += f" in {render.terminal_text(_shorten_where(where))}"
    return (f"{head} ({format_duration(age_s)} ago) · skipped — "
            f"ptest --full --again to rerun")


def format_child_skip_inputs(declaration: str, triggers: tuple[str, ...], *,
                             color: bool = False) -> str:
    """Verbose inputs behind a child skip: the child dir plus root inputs."""
    shown = ", ".join(render.terminal_text(item) for item in triggers)
    return (f"{_prefix(color=color)} -v "
            f"{_project(render.terminal_text(declaration), color=color)} "
            f"inputs: {render.terminal_text(declaration)}/ + {shown} — "
            f"declare other cross-child inputs in full_triggers")


def format_skipped_children(count: int) -> str:
    """'1 child skipped (unchanged)' / 'N children skipped (unchanged)'."""
    if count == 1:
        return "1 child skipped (unchanged)"
    return f"{count} children skipped (unchanged)"


def format_joined_full_run(pid: int, *, color: bool = False) -> str:
    """Join line for a full run that coalesced onto an admitted one."""
    return f"{_prefix(color=color)} joined the running full run (pid {pid})"


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
    if code == "selection-not-closed":
        return (f"selection is on but not closed in {config_name or 'the config'}"
                " — run ptest doctor --fix")
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
                return C.plural(failed, "failure")
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


def format_baseline_note(result: C.RunResult, *,
                       color: bool = False) -> str:
    """End-line note for a full run: baseline recorded, or why not."""
    if result.baseline_published:
        return f"{_prefix(color=color)} baseline recorded"
    return (f"{_prefix(color=color)} no baseline recorded: "
            f"{_no_baseline_detail(result)}")


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
               color: bool = False, next_step: str | None = None,
               note: str | None = None) -> str:
    verdict = render.paint(_VERDICTS[status], _VERDICT_STYLES.get(status, ""),
                           color=color)
    duration = render.paint(format_duration(duration_s), "dim", color=color)
    parts = [f"{_prefix(color=color)} {lead}", verdict] if lead else [
        f"{_prefix(color=color)} {verdict}"]
    segment = format_counts(counts, status)
    if segment is not None:
        parts.append(segment)
    if note is not None:
        parts.append(note)
    parts.append(duration)
    line = " · ".join(parts)
    if exit_code != 0:
        line += f" (exit {exit_code})"
    if next_step is not None:
        line += f" · {next_step}"
    elif hint:
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


def format_workers_cap(workers: int, project_workers: int) -> str:
    """The one line naming a --workers cap below the project's own count."""
    return (f"ptest: --workers {workers} caps this run below the project's "
            f"{project_workers} workers; the queue already shares the machine, "
            f"so plain `ptest` needs no --workers")


def summarize_units(units) -> str:
    """Join changed-unit counts in design 2.8 order for the start line."""
    words = (("function", "function", " changed"), ("name", "name", " changed"),
             ("module", "module", " changed"),
             ("data", "data file", " changed"),
             ("file", "test file", " changed"),
             ("unrecorded", "unrecorded test file", ""))
    counts: dict = {}
    for unit in units or ():
        kind = getattr(unit, "kind", None)
        counts[kind] = counts.get(kind, 0) + 1
    parts = []
    for kind, word, suffix in words:
        count = counts.get(kind, 0)
        if count:
            parts.append(f"{C.plural(count, word)}{suffix}")
    return ", ".join(parts) if parts else "dependencies changed"


def _format_store_bytes(size: int) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "0 B"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def format_store_bytes(size: int) -> str:
    """Human byte count for store sizes (``4.0 KB``, ``1.2 MB``)."""
    return _format_store_bytes(size)


def format_selection_store(declaration: str | None, nodes: int,
                           size_bytes: int,
                           newest_age_s: float | None) -> str:
    """Human-only status line for one selection store (JSON unchanged)."""
    head = ("selection store" if not declaration
            else f"selection store {declaration}")
    age = ("unknown" if newest_age_s is None
           else format_duration(max(0.0, newest_age_s)))
    return (f"{head}: {C.plural(int(nodes), 'test')} recorded · "
            f"{_format_store_bytes(size_bytes)} · newest {age} ago")


def format_selection_audit(misses: int) -> str:
    """The always-printed self-audit line for a full run with misses."""
    return (f"ptest: selection audit: {C.plural(misses, 'failing test')} "
            f"would not have been selected — they now run whenever a "
            f"change statically reaches them")


def _plural_miss(misses: int) -> str:
    """``1 miss`` / ``N misses`` (``C.plural`` only handles y-plurals)."""
    return f"{misses} miss" if misses == 1 else f"{misses} misses"


def _plural_process(processes: int) -> str:
    """``1 process`` / ``N processes`` (``C.plural`` only handles y-plurals)."""
    return f"{processes} process" if processes == 1 else \
        f"{processes} processes"


def format_selection_vaudit(checked: int, misses: int) -> str:
    """The ``-v`` self-audit summary, printed even with no misses."""
    return (f"ptest: -v selection audit: {C.plural(checked, 'failing test')} "
            f"checked · {_plural_miss(misses)}")


def format_selection_recorded(tests: int, processes: int) -> str:
    """The ``-v`` line for a run whose dependencies were recorded."""
    return (f"ptest: -v selection: recorded {C.plural(tests, 'test')} "
            f"from {_plural_process(processes)}")


def format_selection_not_recorded(reason: str) -> str:
    """The ``-v`` line for a run that recorded nothing."""
    return f"ptest: -v selection: not recorded: {reason}"


def emit(line: str, *, quiet: bool = False, stream=None) -> bool:
    """Write one status line to stderr; quiet suppresses it."""
    if quiet or not line:
        return False
    print(line, file=sys.stderr if stream is None else stream)
    return True


__all__ = [
    "HINT", "WAIT_FIRST_S", "WAIT_REPEAT_MAX_S", "WAIT_REPEAT_S", "next_wait_gap",
    "reset", "claim_hint",
    "format_duration", "format_timeout", "fit_text",
    "format_start", "format_waiting",
    "format_changed_selected", "format_changed_start", "format_no_changes",
    "NEXT_FULL", "NEXT_FIX", "next_step", "format_impact",
    "format_nothing_changed", "format_no_green_changes",
    "format_already_verified", "format_verified_elsewhere",
    "format_child_unchanged", "format_child_skip_inputs",
    "format_skipped_children", "format_joined_full_run",
    "explain_changed_full_reason",
    "format_baseline_note",
    "format_setup_start", "format_setup_done", "format_setup_failed",
    "format_setup_run_start", "format_setup_run_done",
    "format_end", "format_timing", "holder_label", "emit",
    "summarize_units", "format_store_bytes", "format_selection_store",
    "format_selection_audit", "format_selection_vaudit",
    "format_selection_recorded", "format_selection_not_recorded",
]
