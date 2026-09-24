"""Pure terminal renderer for ``ptest init``.

Consumes completed init/rules records plus plain project-facts dicts
(``project_facts.FACT_KEYS``). One bounded string is returned and no file
is ever read. The versioned JSON document is untouched; this renderer
serves human terminals exclusively.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping, Sequence

from . import contracts as C
from .project_facts import (
    check_facts,
    detail_atoms,
    detail_lines,
    summary_atoms,
    terminal_width,
    wrap_atoms,
    wrap_words,
)
from .render import terminal_text

_MAX_BODY_LINES = 200
_CONFIG_NAME = ".ptest.toml"

_WORDMARK = (
    "██████╗ ████████╗███████╗███████╗████████╗",
    "██╔══██╗╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝",
    "██████╔╝   ██║   █████╗  ███████╗   ██║",
    "██╔═══╝    ██║   ██╔══╝  ╚════██║   ██║",
    "██║        ██║   ███████╗███████║   ██║",
    "╚═╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝",
)
_REPO_MAX_COLUMNS = 80
_COLOR_START = "\x1b[1;36m"
_COLOR_STOP = "\x1b[0m"

# Repository skill paths mapped to their agent label.
_SKILL_AGENTS = (
    (".claude/skills/ptest/SKILL.md", "claude"),
    (".agents/skills/ptest/SKILL.md", "codex"),
    (".opencode/skills/ptest/SKILL.md", "opencode"),
    (".gemini/skills/ptest/SKILL.md", "gemini"),
)

_RESTART_NEW = "Restart your coding agents to load the new ptest skill."
_RESTART_UPDATED = "Restart your coding agents to load the updated ptest skill."


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
    return lines


def _clean(value: object) -> str:
    return terminal_text(value)


def _human_action(action: str) -> str:
    if action in ("existing", "already present"):
        return "unchanged"
    return action


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


def _header_line(result: C.InitResult, rules: object, dry_run: bool,
                 repo_name: str) -> str:
    phrase = _header(result, rules, dry_run)
    bound = _bound_repo_name(repo_name)
    if bound:
        return f"{phrase} · {bound}"
    return phrase


def _synthesized_root_action(result: C.InitResult) -> str:
    if result.action is C.InitAction.CREATED:
        return "created"
    if result.action is C.InitAction.PREVIEW:
        return "would create"
    return "unchanged"


def _skill_label(target: str) -> str | None:
    for suffix, agent in _SKILL_AGENTS:
        if target == suffix or target.endswith("/" + suffix):
            return f"ptest skill for {agent}"
    return None


def _is_skill_target(target: str) -> bool:
    return _skill_label(target) is not None


def _file_groups(result: C.InitResult, rules: object,
                 ) -> list[tuple[str, str, list[str]]]:
    """(source, action, targets) groups in first-appearance order."""
    groups: list[tuple[str, str, list[str]]] = []
    index: dict[tuple[str, str], int] = {}

    def add(source: str, action: str, target: str) -> None:
        key = (source, action)
        slot = index.get(key)
        if slot is None:
            index[key] = len(groups)
            groups.append((source, action, [target]))
        elif target not in groups[slot][2]:
            groups[slot][2].append(target)

    for item in result.details:
        if item.source != "config":
            continue
        action = _human_action(_clean(item.action))
        if action == "note":
            continue
        add("config", action, _clean(item.target))
    if not any(target == _CONFIG_NAME
               for _, _, targets in groups for target in targets):
        groups.insert(0, ("config", _synthesized_root_action(result),
                          [_CONFIG_NAME]))
    details = tuple(getattr(rules, "details", None) or ())
    for item in details:
        if getattr(item, "source", None) != "guidance":
            continue
        action = _human_action(_clean(getattr(item, "action", "")))
        target = _clean(getattr(item, "target", ""))
        if action == "note":
            continue
        add("guidance", action, _skill_label(target) or target)
    if not groups:
        for action in getattr(rules, "actions", ()):
            add("guidance", _human_action(_clean(action)), "")
    return groups


def _file_lines(result: C.InitResult, rules: object, width: int) -> list[str]:
    lines: list[str] = []
    last_source: str | None = None
    for source, action, targets in _file_groups(result, rules):
        shown_source = "" if source == last_source else source
        last_source = source
        shown = [target for target in targets if target]
        if not shown:
            lines.append(f"  {shown_source:<10} {action}")
            continue
        prefix = f"{shown_source:<10} {action:<10} "
        hang = " " * len(prefix)
        lines.extend(wrap_atoms(shown, width, indent="  " + prefix,
                                hang="  " + hang, sep=", "))
    return lines


def _note_projects(result: C.InitResult) -> list[tuple[str, str]]:
    """(project, runner) pairs from init notes, in note order."""
    projects: list[tuple[str, str]] = []
    for item in result.details:
        if item.source != "config" or _clean(item.action) != "note":
            continue
        parts = _clean(item.target).split(" · ", 2)
        if len(parts) == 3 and parts[0] and parts[1]:
            projects.append((parts[0], parts[1]))
    return projects


def _project_fact_lines(facts: Mapping[str, object], width: int,
                        name_width: int = 6) -> list[str]:
    project = terminal_text(facts.get("project", "."))
    runner = terminal_text(facts.get("runner", "unknown"))
    prefix = f"  {project:<{name_width}}{runner}  "
    hang = " " * len(prefix)
    atoms = [terminal_text(atom) for atom in summary_atoms(facts)]
    lines = wrap_atoms(atoms, width, indent=prefix, hang=hang)
    for detail in detail_lines(facts):
        atoms = [terminal_text(atom) for atom in detail_atoms(detail)]
        lines.extend(wrap_atoms(atoms, width, indent=hang, hang=hang,
                                sep=" "))
    return lines


def _project_lines(result: C.InitResult,
                   facts: Sequence[Mapping[str, object]],
                   width: int) -> list[str]:
    validated = [check_facts(item) for item in facts]
    validated = [item for item in validated if item is not None]
    if validated:
        longest = max(len(terminal_text(item.get("project", ".")))
                      for item in validated)
        name_width = max(6, longest + 2)
        lines: list[str] = []
        for item in validated:
            lines.extend(_project_fact_lines(item, width, name_width))
        return lines
    return [f"  {terminal_text(project)}  {terminal_text(runner)}"
            for project, runner in _note_projects(result)]


def _warning_lines(result: C.InitResult, width: int) -> list[str]:
    lines: list[str] = []
    for warning in result.warnings:
        code = _clean(getattr(warning, "code", ""))
        message = _clean(getattr(warning, "message", ""))
        lines.extend(wrap_words(f"{code}: {message}", width, indent="  ",
                                hang="    "))
    return lines


def render_init(result: C.InitResult, rules: object = None, *,
                dry_run: bool = False,
                agents: tuple[str, ...] = (),
                repo_name: str = "",
                color: bool = False,
                facts: Sequence[Mapping[str, object]] = (),
                width: int | None = None) -> str:
    """Render the init summary: wordmark, header, projects, files, warnings.

    No smoke and no next steps; those belong to :func:`render_init_footer`.
    ANSI color appears on the wordmark only when ``color`` is true and
    ``NO_COLOR`` is absent.
    """
    if not isinstance(result, C.InitResult):
        raise TypeError("render_init requires InitResult")
    resolved = terminal_width(width)
    lines = _banner_lines(repo_name, color)
    lines.append(_header_line(result, rules, dry_run, repo_name))
    blocks: list[list[str]] = []
    projects = _project_lines(result, facts, resolved)
    if projects:
        blocks.append(projects)
    if rules is not None:
        files = _file_lines(result, rules, resolved)
        if files:
            blocks.append(files)
    if result.warnings:
        blocks.append(_warning_lines(result, resolved))
    for block in blocks:
        lines.append("")
        lines.extend(block)
    if len(lines) > _MAX_BODY_LINES:
        omitted = len(lines) - _MAX_BODY_LINES
        lines = lines[:_MAX_BODY_LINES] + [
            f"... [truncated: {omitted} more lines omitted]"]
    return "\n".join(lines) + "\n"


def _next_steps(facts: Sequence[Mapping[str, object]], smoke: Sequence[object],
                plans: Sequence[object], width: int) -> list[str]:
    del width  # steps are single unbreakable lines; atoms never split.
    steps: list[str] = []
    passed = {getattr(item, "project", None) for item in smoke
              if getattr(item, "status", None) == "passed"}
    for item in facts:
        valid = check_facts(item)
        if valid is None:
            continue
        project = str(valid.get("project", "."))
        if not valid.get("runs"):
            fix = valid.get("runs_fix")
            if isinstance(fix, str) and fix:
                steps.append(f"{project}  not runnable → {fix}")
            continue
        parallel_fix = valid.get("parallel_fix")
        if isinstance(parallel_fix, str) and parallel_fix:
            steps.append(f"{project}  parallel off → {parallel_fix}")
    for plan in plans:
        setup_argv = getattr(plan, "setup_argv", None)
        if not setup_argv:
            continue
        project = str(getattr(plan, "project", "."))
        if project in passed:
            continue
        candidate = getattr(plan, "candidate", None)
        try:
            from .init_smoke import display_command
            command = display_command(project, candidate)
        except Exception:
            command = f"ptest {project}" if project != "." else "ptest"
        setup = " ".join(str(part) for part in setup_argv)
        steps.append(f"{project}  setup pending → run: {command} "
                     f"(runs {setup} first)")
    for item in smoke:
        if getattr(item, "status", None) == "failed":
            project = terminal_text(getattr(item, "project", "."))
            steps.append(f"{project}  smoke failed → "
                         f"see the runner output above")
    return [terminal_text(step) for step in steps]


def _restart_line(result: C.InitResult, rules: object,
                  dry_run: bool) -> str | None:
    if dry_run or result.action is C.InitAction.PREVIEW:
        return None
    details = tuple(getattr(rules, "details", None) or ())
    actions = [_human_action(_clean(getattr(item, "action", "")))
               for item in details
               if getattr(item, "source", None) == "guidance"
               and _is_skill_target(_clean(getattr(item, "target", "")))]
    actions = [action for action in actions
               if action in ("created", "updated")]
    if not actions:
        return None
    if all(action == "updated" for action in actions):
        return _RESTART_UPDATED
    return _RESTART_NEW


def render_init_footer(result: C.InitResult, rules: object = None, *,
                       dry_run: bool = False,
                       smoke: Sequence[object] = (),
                       plans: Sequence[object] = (),
                       facts: Sequence[Mapping[str, object]] = (),
                       width: int | None = None) -> str:
    """Render smoke, actionable next steps, and at most one restart line.

    Returns ``""`` when there is nothing to say.
    """
    if not isinstance(result, C.InitResult):
        raise TypeError("render_init_footer requires InitResult")
    from .init_smoke import SmokeResult, format_smoke
    resolved = terminal_width(width)
    lines: list[str] = []
    smoke_items = tuple(smoke)
    if smoke_items:
        for item in smoke_items:
            if not isinstance(item, SmokeResult):
                raise TypeError("render_init_footer smoke requires "
                                "SmokeResult rows")
        block = format_smoke(smoke_items, width=resolved)
        if block:
            lines.append(block.rstrip("\n"))
    steps = _next_steps(facts, smoke_items, tuple(plans), resolved)
    if steps:
        if lines:
            lines.append("")
        lines.extend(f"  {step}" for step in steps)
    restart = _restart_line(result, rules, dry_run)
    if restart is not None:
        if lines:
            lines.append("")
        lines.append(restart)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
