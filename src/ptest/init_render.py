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


def _config_lines(result: C.InitResult) -> list[str]:
    # The configuration result keeps the historical one-line shape
    # (``created: .ptest.toml``), shown relative to the repository root;
    # the absolute typed target stays in the frozen JSON document.
    lines = [f"{_human_action(result.action.value)}: {_CONFIG_NAME}"]
    lines = [item for line in lines for item in _wrapped(line)]
    for item in result.details:
        if item.source != "config":
            continue
        lines.extend(_entry(_human_action(_clean(item.action)),
                             _clean(item.target)))
    return lines


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
    lines = [
        "ptest --full  run the integrated gate once the change is integrated",
    ]
    children = [
        item.target[: -len("/" + _CONFIG_NAME)]
        for item in result.details
        if item.source == "config"
        and item.target != _CONFIG_NAME
        and item.target.endswith("/" + _CONFIG_NAME)
    ]
    if children:
        lines.insert(
            0,
            f"ptest {children[0]}/tests/<scope>.py  run one focused child scope",
        )
    for agent in dict.fromkeys(agents):
        hint = _HINTS.get(agent)
        if hint is not None:
            lines.append(hint)
    return [item for line in lines for item in _wrapped(line)]


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
