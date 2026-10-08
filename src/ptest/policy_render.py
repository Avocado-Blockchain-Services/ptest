"""Doctor Test policy terminal and Markdown rendering (T2).

Plain-words ``Test policy`` section built from ``policy_facts`` only.
Every repository-derived string is sanitized at this boundary.
"""
from __future__ import annotations

import unicodedata

from . import render as render_mod

__all__ = ["render_terminal", "render_markdown"]

_CONFLICT_HEADER = ("These lines in your instruction files name a coverage "
                    "percentage; ptest never edits them. Review them yourself:")

_ASCII_FALLBACKS = {
    "\u2014": "--", "\u2013": "-", "\u2026": "...", "\u2022": "*",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
}


def _clean(value: object) -> str:
    return render_mod.terminal_text(value)


def _ascii(text: str) -> str:
    for source, target in _ASCII_FALLBACKS.items():
        text = text.replace(source, target)
    return unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode("ascii")


def _encode_safe(text: str, encoding: str | None) -> str:
    if encoding is None:
        return text
    try:
        text.encode(encoding)
    except LookupError:
        return text
    except UnicodeEncodeError:
        text = _ascii(text)
    return text


def _md(value: object) -> str:
    """Neutralize repository text for Markdown code spans."""
    out: list[str] = []
    for character in str(value):
        category = unicodedata.category(character)
        if category.startswith("C") or category in ("Zl", "Zp"):
            continue
        if character in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff":
            continue
        if character == "`":
            out.append("'")
        elif character == "<":
            out.append("&lt;")
        elif character == ">":
            out.append("&gt;")
        elif character in "[]":
            out.append("\\" + character)
        elif character == "|":
            out.append("\\|")
        elif character in "\\":
            out.append("\\\\")
        else:
            out.append(character)
    return "".join(out)


def _instruction_lines(scan) -> list:
    return list(getattr(scan, "lines", ()) or ())


def _has_risk(report) -> bool:
    return any(_project_risks(project) for project in report.projects)


def _project_risks(project) -> list[str]:
    risks: list[str] = []
    if project.gates and not project.branch:
        risks.append("A line-only percentage counts lines that ran, not "
                     "behavior that was checked; tests can raise it without "
                     "asserting anything.")
    if project.omit_total:
        risks.append("Omit patterns hide those files from the number "
                     "completely.")
    if project.pragma_count:
        risks.append(f"{project.pragma_count} lines are marked "
                     "`pragma: no cover`; that code never counts against "
                     "the gate.")
    if _instruction_lines(project.instructions):
        risks.append("An instruction that names a coverage percentage "
                     "invites tests written to reach the number instead of "
                     "to check behavior.")
    return risks


def _project_lines(project, *, markdown: bool) -> list[str]:
    if markdown:
        quote = _md
    else:
        quote = _clean
    lines: list[str] = []
    if project.gates:
        for gate in project.gates:
            lines.append(f"coverage gate: {quote(gate.value)} "
                         f"({quote(gate.source)})")
        branch_text = ("on" if project.branch else "off")
        detail = ""
        if project.branch and project.branch_sources:
            detail = f" ({', '.join(quote(str(s)) for s in project.branch_sources)})"
        lines.append(f"branch coverage: {branch_text}{detail}")
    else:
        lines.append("coverage gate: none found")
    if project.omit_total:
        shown = ", ".join(quote(str(item)) for item in project.omit)
        extra = ""
        if project.omit_total > len(project.omit):
            extra = f" and {project.omit_total - len(project.omit)} more"
        lines.append(f"omit patterns ({project.omit_total}): "
                     f"{shown}{extra}")
    if project.pragma_count or not project.pragma_complete:
        count_text = (f"at least {project.pragma_count}"
                      if not project.pragma_complete
                      else str(project.pragma_count))
        lines.append(f"pragma no cover: {count_text} lines")
    if project.vitest_not_inspected:
        lines.append("vitest coverage thresholds: not inspected "
                     "(config is code)")
    found = _instruction_lines(project.instructions)
    if found:
        lines.append(_CONFLICT_HEADER)
        for item in found:
            lines.append(f"  {quote(item.path)}:{item.line}: "
                         f"{quote(item.text)}")
    for name, reason in list(getattr(project.instructions, "skipped", ())):
        lines.append(f"  {quote(name)}: not inspected ({quote(reason)})")
    if getattr(project.instructions, "truncated", False):
        lines.append("  [more matching lines exist]")
    for note in project.notes:
        lines.append(f"note: {quote(note)}")
    return lines


def _suggestion_lines(report) -> list[str]:
    lines = [
        "Keep the existing coverage gate as a floor; do not raise it "
        "by adding tests.",
        "Fix a failing gate by testing real behavior or deleting dead "
        "code, never with assertion-free tests.",
    ]
    if any(project.gates and not project.branch
           for project in report.projects):
        lines.append("Consider branch coverage: coverage.py `branch = true` "
                     "or pytest-cov `--cov-branch`.")
    if any(_instruction_lines(project.instructions)
           for project in report.projects):
        lines.append("Edit the instruction lines listed above yourself; "
                     "ptest never changes them.")
    if not report.policy_installed:
        lines.append("Stricter stance, opt-in: "
                     "`ptest rules --apply --test-policy`.")
    return lines


def _wrap_code(text: str) -> str:
    return f"`{text}`"


def render_terminal(report, *, color: bool = False,
                    encoding: str | None = None,
                    width: int | None = None) -> str:
    """Human ``Test policy`` block; ``""`` when there are no projects."""
    del width  # accepted for API stability; terminal_text bounds lines.
    if not report.projects:
        return ""
    paint = render_mod.paint
    heading = "Test policy"
    out: list[str] = []
    out.append("")
    out.append(paint(heading, "bold", color=color) if color else heading)
    out.append("")
    show_headers = len(report.projects) > 1 or report.projects[0].project != "."
    for project in report.projects:
        if show_headers:
            out.append(_clean(project.project))
        for line in _project_lines(project, markdown=False):
            if show_headers and not line.startswith("  "):
                out.append("  " + line)
            else:
                out.append(line)
        risks = _project_risks(project)
        if risks:
            out.append("Risks:" if show_headers else "risks:")
            for risk in risks:
                out.append(f"  {_clean(risk)}")
        out.append("")
    if _has_risk(report):
        out.append("Suggestion:")
        for line in _suggestion_lines(report):
            out.append(f"  {_clean(line)}")
        out.append("")
    if (report.root_instructions is not None
            and _instruction_lines(report.root_instructions)):
        out.append("repository root:")
        out.append(f"  {_CONFLICT_HEADER}")
        for item in _instruction_lines(report.root_instructions):
            out.append(f"    {_clean(item.path)}:{item.line}: "
                       f"{_clean(item.text)}")
        out.append("")
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return _encode_safe(text, encoding)


def render_markdown(report) -> str:
    """``## Test policy`` section for recommendations.md; ``""`` when empty."""
    if not report.projects:
        return ""
    out: list[str] = ["## Test policy", ""]
    show_headers = len(report.projects) > 1 or report.projects[0].project != "."
    for project in report.projects:
        if show_headers:
            out.append(f"### {_md(project.project)}")
            out.append("")
        for line in _project_lines(project, markdown=True):
            if line.startswith("  ") and ": " in line:
                # Already neutralized by _project_lines(markdown=True);
                # never escape twice.
                head, _, tail = line.strip().partition(": ")
                if head and tail:
                    out.append(f"- {head}: {_wrap_code(tail)}")
                    continue
            if line == _CONFLICT_HEADER:
                out.append(line)
                continue
            out.append(f"- {_wrap_code(line)}" if not line.startswith("  ")
                       else f"  {_wrap_code(line.strip())}")
        risks = _project_risks(project)
        if risks:
            out.append("")
            out.append("Risks:")
            out.append("")
            for risk in risks:
                out.append(f"- {risk}")
        out.append("")
    if _has_risk(report):
        out.append("Suggestion:")
        out.append("")
        for line in _suggestion_lines(report):
            out.append(f"- {line}")
        out.append("")
    if (report.root_instructions is not None
            and _instruction_lines(report.root_instructions)):
        out.append("Repository root:")
        out.append("")
        out.append(_CONFLICT_HEADER)
        out.append("")
        for item in _instruction_lines(report.root_instructions):
            out.append(f"- {_md(item.path)}:{item.line}: "
                       f"{_wrap_code(_md(item.text))}")
        out.append("")
    return "\n".join(out) + "\n"
