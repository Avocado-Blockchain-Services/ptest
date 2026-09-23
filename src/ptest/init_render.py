"""Pure terminal renderer for successful ``ptest init``.

Consumes completed init/rules records only: one bounded string is returned
and no file is ever read. The versioned JSON document is untouched; this
renderer serves human terminals exclusively.
"""
from __future__ import annotations

import os
import textwrap
import unicodedata

from . import contracts as C
from .render import terminal_text

_WIDTH = 64
_INNER = _WIDTH - 4
_ENTRY_INDENT = "  "
_ACTION_WIDTH = 15
_MAX_BODY_LINES = 200
_CONFIG_NAME = ".ptest.toml"

_HINTS = {
    "codex": ("Codex detects repository skills automatically; restart Codex "
              "or run /skills if ptest is not visible."),
    "claude": ("Claude loads repository skills at startup; restart or refresh "
               "the skill list if ptest is not visible."),
    "opencode": ("OpenCode loads repository skills at startup; restart if "
                 "ptest is not visible."),
    "gemini": ("Gemini loads repository skills at startup; restart if ptest "
               "is not visible."),
}

_WORDMARK = (
    "██████╗ ████████╗███████╗███████╗████████╗",
    "██╔══██╗╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝",
    "██████╔╝   ██║   █████╗  ███████╗   ██║",
    "██╔═══╝    ██║   ██╔══╝  ╚════██║   ██║",
    "██║        ██║   ███████╗███████║   ██║",
    "╚═╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝",
)
_GITHUB_URL = "https://github.com/Avocado-Blockchain-Services/ptest"
_REPO_MAX_COLUMNS = 80
_COLOR_START = "\x1b[1;36m"
_COLOR_STOP = "\x1b[0m"


def _display_width(text: str) -> int:
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _take_columns(text: str, limit: int) -> str:
    kept: list[str] = []
    used = 0
    for char in text:
        width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if used + width > limit:
            break
        kept.append(char)
        used += width
    return "".join(kept)


def _bound_repo_name(repo_name: object) -> str:
    if not repo_name:
        return ""
    clean = terminal_text(repo_name)
    if _display_width(clean) > _REPO_MAX_COLUMNS:
        clean = _take_columns(clean, _REPO_MAX_COLUMNS - 1) + "…"
    return clean


def _use_color(color: bool) -> bool:
    return bool(color) and "NO_COLOR" not in os.environ


def _banner_lines(repo_name: object, color: bool) -> list[str]:
    use_color = _use_color(color)
    start = _COLOR_START if use_color else ""
    stop = _COLOR_STOP if use_color else ""
    lines = [f"{start}{row}{stop}" for row in _WORDMARK]
    lines.append(f"ptest {C.PTEST_VERSION}")
    bound = _bound_repo_name(repo_name)
    if bound:
        lines.append(bound)
    lines.append(_GITHUB_URL)
    return lines


def _clean(value: object) -> str:
    return terminal_text(value)


def _human_action(action: str) -> str:
    if action in ("existing", "already present"):
        return "unchanged"
    return action


def _wrapped(text: str, *, indent: str = "", subsequent: str | None = None) -> list[str]:
    width = _INNER - len(indent)
    chunks = textwrap.wrap(
        text, width=width, break_long_words=True, break_on_hyphens=False,
        replace_whitespace=False, drop_whitespace=True,
    )
    if not chunks:
        return [indent.rstrip()]
    lines = [indent + chunks[0]]
    pad = subsequent if subsequent is not None else indent
    lines.extend(pad + chunk for chunk in chunks[1:])
    return lines


def _entry(action: str, target: str) -> list[str]:
    head = f"{action:<{_ACTION_WIDTH}} "
    first_width = _INNER - len(_ENTRY_INDENT) - len(head)
    chunks = textwrap.wrap(
        target, width=first_width, break_long_words=True,
        break_on_hyphens=False, replace_whitespace=False,
        drop_whitespace=True,
    )
    if not chunks:
        chunks = [""]
    lines = [_ENTRY_INDENT + head + chunks[0]]
    pad = _ENTRY_INDENT + " " * len(head)
    lines.extend(pad + chunk for chunk in chunks[1:])
    return lines


def _header(result: C.InitResult, rules: object, dry_run: bool) -> str:
    if dry_run or result.action is C.InitAction.PREVIEW:
        return "ptest init preview"
    if result.action is C.InitAction.EXISTING and result.warnings:
        return "ptest init needs attention"
    if result.action is C.InitAction.CREATED:
        return "ptest initialized"
    if bool(getattr(rules, "changed", False)):
        return "ptest initialized"
    return "ptest already configured"


_NOTE_SEP = " · "
_RUN_PREFIX = "run: "
_NOT_RUNNABLE_PREFIX = "not runnable: "
_FIX_SEP = " — fix: "


def _synthesized_root_action(result: C.InitResult) -> str:
    if result.action is C.InitAction.CREATED:
        return "created"
    if result.action is C.InitAction.PREVIEW:
        return "would create"
    return "unchanged"


def _config_records(result: C.InitResult) -> list:
    """Non-note config records in detail order (already sanitized)."""
    records = []
    for item in result.details:
        if item.source != "config":
            continue
        if _clean(item.action) == "note":
            continue
        records.append((_human_action(_clean(item.action)), _clean(item.target)))
    return records


def _config_lines(result: C.InitResult) -> list[str]:
    # Each config record renders once as ``<action> <path>`` relative to
    # the repository root; the absolute typed target stays in the frozen
    # JSON document. The root line is synthesized from the result action
    # only when no detail record names it, so it always appears exactly
    # once and never in the historical ``created: .ptest.toml`` shape.
    # A child with a Projects entry renders only there (as its status
    # line), so its config action never duplicates across sections.
    records = _config_records(result)
    projects, _, others = _split_notes(result)
    noted = {project for project, _, _ in projects if project != "."}
    lines: list[str] = []
    if not any(target == _CONFIG_NAME for _, target in records):
        lines.extend(_entry(_synthesized_root_action(result), _CONFIG_NAME))
    for action, target in records:
        if any(target == f"{project}/{_CONFIG_NAME}" for project in noted):
            continue
        lines.extend(_entry(action, target))
    for target in others:
        lines.extend(_entry("note", target))
    return lines


def _split_notes(result: C.InitResult) -> tuple[list, list, list]:
    """Split executability notes into project, run, and other notes.

    Returns ``(projects, runs, others)`` where projects holds
    ``(project, runner, verdict)`` triples, runs holds verified command
    strings, and others holds sanitized free-form note targets.
    """
    projects: list = []
    runs: list = []
    others: list = []
    for item in result.details:
        if item.source != "config" or _clean(item.action) != "note":
            continue
        target = _clean(item.target)
        if target.startswith(_RUN_PREFIX):
            runs.append(target[len(_RUN_PREFIX):])
            continue
        parts = target.split(_NOTE_SEP, 2)
        if len(parts) == 3 and all(part for part in parts):
            projects.append((parts[0], parts[1], parts[2]))
        else:
            others.append(target)
    return projects, runs, others


_CAVEATS_PREFIX = "ready with caveats:"
_BULLET_PREFIX = "    - "
_BULLET_CONT = "      "


def _split_verdict(verdict: str) -> tuple[str, list[str]]:
    """Split a project verdict into a short head plus one bullet per detail.

    ``ready with caveats: c1; c2`` becomes the head ``ready with caveats``
    with one bullet per caveat; ``not runnable: reason — fix: fix`` becomes
    the head ``not runnable`` with reason and fix bullets. Anything else
    renders as a single head line, so an unknown verdict never loses text.
    """
    if verdict == "ready":
        return "ready", []
    if verdict.startswith(_CAVEATS_PREFIX):
        rest = verdict[len(_CAVEATS_PREFIX):].strip()
        bullets = [part.strip() for part in rest.split(";")]
        return "ready with caveats", [part for part in bullets if part]
    if verdict.startswith(_NOT_RUNNABLE_PREFIX):
        rest = verdict[len(_NOT_RUNNABLE_PREFIX):]
        if _FIX_SEP in rest:
            reason, fix = rest.split(_FIX_SEP, 1)
            bullets = [reason.strip(), f"fix: {fix.strip()}"]
        else:
            bullets = [rest.strip()]
        return "not runnable", [part for part in bullets if part]
    return verdict, []


def _bullet_lines(text: str) -> list[str]:
    """One caveat as a wrapped bullet; every row fits the box width."""
    width = _INNER - len(_BULLET_PREFIX)
    chunks = textwrap.wrap(
        text, width=width, break_long_words=True, break_on_hyphens=False,
        replace_whitespace=False, drop_whitespace=True,
    )
    if not chunks:
        return []
    return [_BULLET_PREFIX + chunks[0]] + [
        _BULLET_CONT + chunk for chunk in chunks[1:]]


def _project_lines(result: C.InitResult) -> list[str]:
    projects, _, _ = _split_notes(result)
    if not projects:
        return []
    lines: list[str] = []
    for project, runner, verdict in projects:
        head, bullets = _split_verdict(verdict)
        lines.extend(_wrapped(f"{project}  {runner}  {head}",
                              indent=_ENTRY_INDENT))
        for bullet in bullets:
            lines.extend(_bullet_lines(bullet))
    return lines


def _fix_for_verdict(verdict: str) -> str | None:
    if not verdict.startswith(_NOT_RUNNABLE_PREFIX):
        return None
    rest = verdict[len(_NOT_RUNNABLE_PREFIX):]
    if _FIX_SEP not in rest:
        return None
    return rest.split(_FIX_SEP, 1)[1] or None


def _guidance_lines(rules: object) -> list[str]:
    details = tuple(getattr(rules, "details", None) or ())
    lines: list[str] = []
    for item in details:
        if getattr(item, "source", None) != "guidance":
            continue
        action = _clean(getattr(item, "action", ""))
        target = _clean(getattr(item, "target", ""))
        if action == "note":
            lines.extend(_wrapped(f"note: {target}", indent=_ENTRY_INDENT))
        else:
            lines.extend(_entry(_human_action(action), target))
    if not lines:
        for action in getattr(rules, "actions", ()):
            lines.extend(_wrapped(_clean(action), indent=_ENTRY_INDENT))
    return lines


def _warning_lines(result: C.InitResult) -> list[str]:
    lines: list[str] = []
    for warning in result.warnings:
        code = _clean(getattr(warning, "code", ""))
        message = _clean(getattr(warning, "message", ""))
        lines.extend(_wrapped(f"{code}: {message}", indent=_ENTRY_INDENT))
    return lines


def _next_lines(result: C.InitResult, agents: tuple[str, ...]) -> list[str]:
    # Next steps list only verified commands: the ``run: `` notes from the
    # executability check, then the exact fix for each project that is not
    # runnable, then the unchanged agent restart hints. Nothing is invented:
    # ``ptest --full`` appears only when a run note verifies it.
    projects, runs, _ = _split_notes(result)
    steps: list[str] = list(runs)
    for project, _, verdict in projects:
        fix = _fix_for_verdict(verdict)
        if fix is not None:
            steps.append(f"fix {project}: {fix}")
    for agent in dict.fromkeys(agents):
        hint = _HINTS.get(agent)
        if hint is not None:
            steps.append(hint)
    return [item for line in steps for item in _wrapped(line)]


def render_init(result: C.InitResult, rules: object = None, *,
                dry_run: bool = False,
                agents: tuple[str, ...] = (),
                repo_name: str = "",
                color: bool = False) -> str:
    """Render one bounded banner for a successful init; never reads files.

    The human banner begins with the PTEST wordmark, ``ptest <version>``,
    the caller-supplied repository display name, and the canonical project
    URL, followed by the existing configuration box. ANSI color appears on
    the wordmark only when ``color`` is true and ``NO_COLOR`` is absent;
    the non-TTY caller passes ``color=False``.
    """
    if not isinstance(result, C.InitResult):
        raise TypeError("render_init requires InitResult")
    body: list[str] = []
    body.append("Configuration")
    body.extend(_config_lines(result))
    projects = _project_lines(result)
    if projects:
        body.append("Projects")
        body.extend(projects)
    if rules is not None:
        guidance = _guidance_lines(rules)
        if guidance:
            body.append("Guidance")
            body.extend(guidance)
    if result.warnings:
        body.append("Warnings")
        body.extend(_warning_lines(result))
    body.append("Next steps")
    body.extend(_next_lines(result, tuple(agents)))
    if len(body) > _MAX_BODY_LINES:
        omitted = len(body) - _MAX_BODY_LINES
        body = body[:_MAX_BODY_LINES] + [
            f"... [truncated: {omitted} more lines omitted]"]

    top = "┌" + "─" * (_WIDTH - 2) + "┐"
    middle = "├" + "─" * (_WIDTH - 2) + "┤"
    bottom = "└" + "─" * (_WIDTH - 2) + "┘"

    def row(text: str) -> str:
        return "│ " + text.ljust(_INNER) + " │"

    lines = _banner_lines(repo_name, color)
    lines += ["", top, row(_clean(_header(result, rules, dry_run))), middle]
    lines.extend(row(line) for line in body)
    lines.append(bottom)
    return "\n".join(lines) + "\n"
