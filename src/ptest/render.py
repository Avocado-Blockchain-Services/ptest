"""Pure public renderers for NG documents and doctor assessment guidance."""
from __future__ import annotations

import importlib.resources
import json
import os
import re

from . import checklist as checklist_api
from . import contracts as C
from .agent_assessment import FAILED_PREFIX, SKIP_PREFIX
from .project_facts import (
    check_facts,
    detail_atoms,
    detail_lines,
    terminal_width,
    wrap_atoms,
    wrap_words,
)

def terminal_text(value: object) -> str:
    """Bound one ptest-owned display field to 1024 UTF-8 bytes; escape controls.

    Keep this at the human presentation boundary, never on typed JSON values
    or child stdout/stderr. isprintable also excludes Unicode direction/control
    characters, line separators and undecodable filesystem surrogates.
    """
    marker = "[truncated]"
    room = 1024 - len(marker)
    parts = []
    used = 0
    for character in str(value):
        piece = character if character.isprintable() else ascii(character)[1:-1]
        size = len(piece.encode("utf-8"))
        if used + size > room:
            return "".join(parts) + marker
        parts.append(piece)
        used += size
    return "".join(parts)


_AGENT_ASSESSMENT_MAX_BYTES = 256 * 1024


_STYLES = {
    "green": "32",
    "red": "31",
    "yellow": "33",
    "cyan": "36",
    "dim": "2",
    "bold": "1",
}


def colors_enabled(tty: bool) -> bool:
    """TTY color gate: explicit TTY, without NO_COLOR, TERM not dumb."""
    return (bool(tty) and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb")


def paint(text: str, style: str, *, color: bool = False) -> str:
    """Wrap already-sanitized text in one ANSI style when color is on.

    Paint only after ``terminal_text`` (or prose derived from it): the
    wrapper adds no display width and untrusted text cannot inject
    codes. Unknown styles return the text unchanged.
    """
    if not colors_enabled(color):
        return text
    code = _STYLES.get(style)
    if code is None:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"

# Literal mirror of agent_assessment.PTEST_ANSWER_PREFIX (T3 never imports
# it; T5 asserts equality). This module only reads the review-flow
# rationale prefixes.
PTEST_ANSWER_PREFIX = "Answered by ptest: "
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);")

# Checklist rows rendered as a visible "parallel safety" group, with
# PARALLEL-001 (added by the deterministic-items task) trailing it.
# Single-sourced from checklist (the canonical catalog owner).
_PARALLEL_SAFETY_IDS = frozenset(checklist_api.PARALLEL_SAFETY_IDS)
_PARALLEL_ITEM_ID = checklist_api.PARALLEL_ITEM_ID


def _agent_assessment_prose(value: object) -> str:
    """Render bounded model prose as inert terminal text.

    HTML entities are neutralized rather than emitted: terminal output must
    never carry ``&#``, ``&lt;``, ``&gt;`` or ``&amp;`` sequences.
    """
    text = terminal_text(value)
    text = re.sub(r"\[([^\]\n]*)\]\([^\)\n]*\)", r"\1", text)
    text = re.sub(r"<(?:https?://[^<>\s]*|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)>",
                  "[link omitted]", text)
    text = re.sub(r"</?[A-Za-z][^<>\n]*>", "", text)
    text = re.sub(r"(?i)(?:https?://|www\.)\S+", "[URL omitted]", text)
    text = _ENTITY_RE.sub("[entity omitted]", text)
    text = "".join(char for char in text if ord(char) not in {
        0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D,
        0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
    })
    return text.replace("|", "/").replace("`", "'")


def _agent_dependency_detail_lines(children, width: int) -> list[str]:
    """Dependency details wrapped to the terminal width like the rest."""
    statuses = {
        "dependency-missing": "missing",
        "dependency-unsupported": "unsupported",
        "dependency-uninspectable": "uninspectable",
    }
    lines = []
    for child in children:
        scope = _agent_assessment_prose(child["scope"])
        for limitation in child["limitations"]:
            status = statuses.get(limitation["code"])
            if status is None:
                continue
            detail = _agent_assessment_prose(limitation["message"])
            paths = limitation.get("paths", [])
            location = ""
            if paths:
                safe_paths = ", ".join(_agent_assessment_prose(path)
                                         for path in paths)
                location = f" (root-relative paths: {safe_paths})"
            lines.extend(wrap_words(
                f"{scope}: {status} prerequisite: {detail}{location}",
                width, indent="- ", hang="  "))
    return lines


def _word_cut(head: str, room: int) -> str:
    """Cut ``head`` back to its last whitespace, unless that guts it.

    Spaceless text has no word boundary to honor, so a cut that would
    keep less than half of ``head`` falls back to the hard cut: the
    marker still shows the line continues. The comparison is in
    characters, not bytes, so multibyte words still take the branch.
    """
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    if cut > len(head) // 2:
        return head[:cut]
    return head


def _truncate_utf8_bytes(text: str, limit: int,
                         marker: str = "…") -> str:
    """Cut text to at most limit UTF-8 bytes at a word boundary.

    The cut lands on the last whitespace within budget so terminal lines
    never end mid-word; the full text stays in recommendations.md.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    room = limit - len(marker.encode("utf-8"))
    head = encoded[:room].decode("utf-8", errors="ignore")
    return _word_cut(head, room).rstrip() + marker


def _joined_bytes(sections: list[str]) -> int:
    """Byte cost of sections joined with blank lines plus trailing newline."""
    if not sections:
        return 1
    return (sum(len(section.encode("utf-8")) for section in sections)
            + 2 * (len(sections) - 1) + 1)


def _fit_lines_with_omission(lines: list[str], budget: int,
                             marker_fn) -> list[str]:
    """Keep whole lines within a byte budget and name the omitted count."""
    cost = sum(len(line.encode("utf-8")) for line in lines)
    cost += max(0, len(lines) - 1)
    if cost <= budget:
        return list(lines)
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        line_cost = len(line.encode("utf-8"))
        separator_cost = 1 if kept else 0
        remaining = len(lines) - index - 1
        if remaining:
            marker = marker_fn(len(lines) - index)
            marker_cost = len(marker.encode("utf-8"))
            if used + separator_cost + line_cost + 1 + marker_cost > budget:
                kept.append(marker_fn(len(lines) - index))
                break
        elif used + separator_cost + line_cost > budget:
            kept.append(marker_fn(1))
            break
        kept.append(line)
        used += separator_cost + line_cost
    return kept


_GRID_STATUS_WORDS = {
    "satisfied": "ok",
    "gap": "gap",
    "unknown": "unknown",
    "not-applicable": "n/a",
}


def _grid_ascii(encoding: str | None) -> bool:
    """True when stdout cannot carry UTF-8 box drawing and glyphs."""
    if encoding is None:
        return False
    return encoding.lower().replace("_", "-") not in ("utf-8", "utf8")


def _grid_glyphs(*, bracket: bool) -> dict:
    """Cell icons: glyphs, or bracket words without color/UTF-8."""
    if bracket:
        return {"satisfied": "[ok]", "gap": "[gap]", "unknown": "[?]",
                "not-applicable": "[n/a]"}
    return {"satisfied": "✓", "gap": "✗", "unknown": "?",
            "not-applicable": "–"}


def _grid_borders(*, ascii_only: bool) -> dict:
    """Box-drawing table borders, or ASCII when stdout is not UTF-8."""
    if ascii_only:
        return {"tl": "+", "tm": "+", "tr": "+", "ml": "+", "mm": "+",
                "mr": "+", "bl": "+", "bm": "+", "br": "+", "h": "-",
                "v": "|"}
    return {"tl": "╭", "tm": "┬", "tr": "╮", "ml": "├", "mm": "┼",
            "mr": "┤", "bl": "╰", "bm": "┴", "br": "╯", "h": "─",
            "v": "│"}


def _grid_status_key(status: object) -> str:
    """Normalize any row status to a known grid cell key."""
    return status if status in _GRID_STATUS_WORDS else "unknown"


def _grid_cell(status: object, glyphs: dict, *, bracket: bool) -> str:
    """One grid cell: glyph plus word, or the bracket icon alone."""
    key = _grid_status_key(status)
    if bracket:
        return glyphs[key]
    return f"{glyphs[key]} {_GRID_STATUS_WORDS[key]}"


def _grid_text(value: object) -> str:
    """Terminal-safe header text that cannot add table cells."""
    return terminal_text(value).replace("|", "/")


def _assessment_runner(scope: str, workspace) -> str:
    """Resolve the declared runner kind for one child scope, or unknown."""
    repositories = (tuple(workspace.repositories)
                    if workspace is not None else ())
    standalone = [item for item in repositories
                  if item.declaration == "."]
    if len(standalone) == 1 and len(repositories) == 1:
        repository = standalone[0]
    else:
        matches = [item for item in repositories
                   if item.declaration != "."
                   and (scope == item.declaration
                        or scope.startswith(item.declaration + "/"))]
        repository = matches[0] if len(matches) == 1 else None
    config = None if repository is None else repository.config
    if config is None:
        return "unknown"
    return config.runner.kind.value


def _child_facts(child) -> dict | None:
    """Validated facts dict for one child, or None to use ``execution``."""
    if not isinstance(child, dict):
        return None
    return check_facts(child.get("facts")) or None


def _grid_duration(seconds: object) -> str | None:
    """Short duration for the grid header, or None when unusable."""
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not 0 <= seconds < 1e9 or seconds != seconds):
        return None
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _grid_runs_atom(runnable: bool, glyphs: dict, *, ascii_only: bool,
                    detail: str = "", fix: str = "") -> str:
    """One runs atom: ``runs ✓`` or ``runs ✗`` with an optional reason."""
    mark = glyphs["satisfied"] if runnable else glyphs["gap"]
    atom = f"runs {mark}"
    if runnable:
        return atom
    if detail:
        atom += f" {'-' if ascii_only else '—'} {detail}"
    if fix:
        atom += f" {'->' if ascii_only else '→'} {fix}"
    return atom


def _grid_facts_atoms(child, runner: str, glyphs: dict, *,
                      ascii_only: bool) -> list[str]:
    """Facts atoms for one grid facts line: runner, runs, parallel, setup."""
    facts = _child_facts(child)
    if facts is None:
        execution = child.get("execution") if isinstance(child, dict) else None
        detail = ""
        fix = ""
        runnable = True
        if isinstance(execution, dict):
            runnable = execution.get("status") != "not-executable"
            raw_detail = execution.get("detail", "")
            detail = _agent_assessment_prose(
                raw_detail if isinstance(raw_detail, str) else "")
            raw_fix = execution.get("fix", "")
            fix = _agent_assessment_prose(
                raw_fix if isinstance(raw_fix, str) else "")
        return [_grid_text(runner),
                _grid_runs_atom(runnable, glyphs, ascii_only=ascii_only,
                                detail=detail, fix=fix)]
    runner_name = facts.get("runner") or runner
    atoms = [_grid_text(runner_name),
             _grid_runs_atom(facts.get("runs", True), glyphs,
                             ascii_only=ascii_only,
                             detail=_agent_assessment_prose(
                                 facts.get("runs_reason") or ""),
                             fix=_agent_assessment_prose(
                                 facts.get("runs_fix") or ""))]
    short = facts.get("parallel_short")
    if short is None:
        short = facts.get("parallel")
    if isinstance(short, str) and short:
        atoms.append(_agent_assessment_prose(short))
    setup = facts.get("setup")
    if isinstance(setup, str) and setup:
        atoms.append(f"setup: {_agent_assessment_prose(setup)}")
    return atoms


def _child_limitation_suffix(child) -> tuple[bool, bool]:
    """(partial_evidence, not_runnable_from_execution)."""
    limitations = (child.get("limitations", [])
                   if isinstance(child, dict) else [])
    if not isinstance(limitations, list):
        limitations = []
    partial = any(isinstance(item, dict)
                  and item.get("code") == "partial-evidence"
                  for item in limitations)
    execution = child.get("execution") if isinstance(child, dict) else None
    not_runnable = (isinstance(execution, dict)
                    and execution.get("status") == "not-executable")
    return partial, not_runnable


def _grid_row_ids(children: list) -> tuple[list[str], list[str], list[str]]:
    """(general, safety, parallel) row ids in checklist catalog order."""
    catalog = [entry.id for entry in checklist_api.CATALOG]
    rank = {row_id: index for index, row_id in enumerate(catalog)}
    seen: list[str] = []
    for child in children:
        rows = child.get("rows", []) if isinstance(child, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if isinstance(row_id, str) and row_id not in seen:
                seen.append(row_id)
    general = [row_id for row_id in seen
               if row_id not in _PARALLEL_SAFETY_IDS
               and row_id != _PARALLEL_ITEM_ID]
    general.sort(key=lambda row_id: (rank.get(row_id, len(rank)), row_id))
    safety = [row_id for row_id in catalog
              if row_id in _PARALLEL_SAFETY_IDS and row_id in seen]
    parallel = [_PARALLEL_ITEM_ID] if _PARALLEL_ITEM_ID in seen else []
    return general, safety, parallel


def _grid_child_info(child) -> dict:
    """Scope, per-id statuses/labels and findings for one grid column."""
    scope = (child.get("scope", "unknown")
             if isinstance(child, dict) else "unknown")
    rows = child.get("rows", []) if isinstance(child, dict) else []
    if not isinstance(rows, list):
        rows = []
    statuses: dict = {}
    labels: dict = {}
    ordered: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or row_id in statuses:
            continue
        statuses[row_id] = row.get("status")
        labels[row_id] = _row_label(row)
        ordered.append(row)
    findings = child.get("findings", []) if isinstance(child, dict) else []
    by_id = {}
    if isinstance(findings, list):
        for finding in findings:
            if (isinstance(finding, dict)
                    and finding.get("id") not in by_id):
                by_id[finding["id"]] = finding
    partial, _ = _child_limitation_suffix(child)
    return {"scope": scope, "statuses": statuses, "labels": labels,
            "ordered": ordered, "by_id": by_id, "partial": partial}


def _dropped_suffix(row: dict) -> str:
    """Short sanitized note for a row's dropped invalid-citation count.

    The suffix carries only a validated integer and fixed words, so it
    needs no prose sanitization; anything missing, zero, negative or
    non-integer yields no suffix and the status line is unchanged.
    """
    dropped = (row.get("dropped_citations", 0)
               if isinstance(row, dict) else 0)
    if (isinstance(dropped, bool) or not isinstance(dropped, int)
            or dropped <= 0):
        return ""
    noun = "citation" if dropped == 1 else "citations"
    return f" ({dropped} {noun} dropped)"


_FIRST_SENTENCE_RE = re.compile(r"[.?!](?=\s|$)")


def _first_sentence(text: str) -> str:
    """The model's first sentence, or the whole stripped text."""
    match = _FIRST_SENTENCE_RE.search(text.strip())
    if match is None:
        return text.strip()
    return text[:match.end()].strip()


def _unknown_reason(row: dict) -> str:
    """Full reason for one unknown row; wrapped by the caller, never cut."""
    rationale = row.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""
    if rationale.startswith(PTEST_ANSWER_PREFIX):
        return _agent_assessment_prose(rationale[len(PTEST_ANSWER_PREFIX):])
    if rationale.startswith(FAILED_PREFIX):
        return _agent_assessment_prose(
            "review failed: " + rationale[len(FAILED_PREFIX):])
    return _first_sentence(_agent_assessment_prose(rationale))


def _na_reason(row: dict) -> str:
    """Full reason for one n/a row; wrapped by the caller, never cut."""
    rationale = row.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""
    for prefix in (SKIP_PREFIX, PTEST_ANSWER_PREFIX):
        if rationale.startswith(prefix):
            rationale = rationale[len(prefix):]
            break
    return _agent_assessment_prose(rationale)


def _paint_runs_mark(line: str, *, color: bool) -> str:
    """Tint the runs mark green when runnable, red when not.

    Runs on laid-out lines so ANSI codes never disturb the columns;
    bracket icons without color stay untouched.
    """
    if "runs ✓" in line:
        return line.replace("runs ✓",
                            "runs " + paint("✓", "green", color=color), 1)
    if "runs ✗" in line:
        return line.replace("runs ✗",
                            "runs " + paint("✗", "red", color=color), 1)
    return line


def _grid_scope(child) -> str:
    """Terminal-safe project scope for facts lines and section heads."""
    scope = (child.get("scope", "unknown")
             if isinstance(child, dict) else "unknown")
    return _grid_text(scope)


def _grid_facts_lines(children, runners, glyphs: dict, *,
                      ascii_only: bool, width: int, color: bool) -> list[str]:
    """Wrapped facts lines per project with aligned scopes.

    Project names share one column; the full-suite detail (``full suite
    = your pytest config …``) rides a second, dim facts line under its
    own project. The runs mark is tinted after layout.
    """
    scopes = [_grid_scope(child) for child in children]
    scope_w = max([len(scope) for scope in scopes] or [0])
    separator = " - " if ascii_only else " · "
    block = []
    for child, runner, scope in zip(children, runners, scopes):
        atoms = _grid_facts_atoms(child, runner, glyphs,
                                  ascii_only=ascii_only)
        indent = f"{scope.ljust(scope_w)}  "
        hang = " " * (scope_w + 2)
        lines = [_paint_runs_mark(line, color=color)
                 for line in wrap_atoms(atoms, width, indent=indent,
                                        hang=hang, sep=separator)]
        facts = _child_facts(child)
        if facts is not None:
            for detail in detail_lines(facts):
                detail_atoms_list = [_agent_assessment_prose(atom)
                                     for atom in detail_atoms(detail)]
                lines.extend(paint(line, "dim", color=color)
                             for line in wrap_atoms(
                                 detail_atoms_list, width, indent=hang,
                                 hang=hang, sep=" "))
        if _child_limitation_suffix(child)[0]:
            lines[-1] += "   (partial evidence)"
        block.extend(lines)
    return block


def _shrink_text(text: str, room: int, *, ascii_only: bool) -> str:
    """Cut a label to ``room`` characters, marking the cut explicitly."""
    if len(text) <= room:
        return text
    marker = "..." if ascii_only else "…"
    if room <= len(marker):
        return marker[:room] if room > 0 else ""
    return text[:room - len(marker)] + marker


_ICON_STYLES = {
    "satisfied": "green",
    "gap": "red",
    "unknown": "yellow",
    "not-applicable": "dim",
}


def _grid_tally(statuses: dict, glyphs: dict) -> str:
    """One compact project tally: ``2 ✓  2 ✗  3 ?`` (``1 –`` for n/a).

    Zero counts are omitted, unless every count is zero. Narrow cells
    keep narrow status columns narrow.
    """
    counts = {"satisfied": 0, "gap": 0, "unknown": 0, "not-applicable": 0}
    for status in statuses.values():
        counts[_grid_status_key(status)] += 1
    pairs = [f"{counts[key]} {glyphs[key]}"
             for key in ("satisfied", "gap", "unknown")
             if counts[key]]
    if counts["not-applicable"]:
        pairs.append(f"{counts['not-applicable']} "
                     f"{glyphs['not-applicable']}")
    if not pairs:
        pairs = [f"0 {glyphs[key]}"
                 for key in ("satisfied", "gap", "unknown")]
    return "  ".join(pairs)


def _paint_tally(tally: str, room: int, styles: dict, *,
                 color: bool) -> str:
    """Tint one laid-out tally pair by pair, like the status cells."""
    tinted = []
    for pair in tally.split("  "):
        icon = pair.split(" ", 1)[1] if " " in pair else ""
        tinted.append(paint(pair, styles.get(icon, ""), color=color))
    return "  ".join(tinted) + " " * max(0, room - len(tally))


def _grid_table_lines(sections, labels, columns, glyphs, borders, *,
                      bracket: bool = False, color: bool = False,
                      ascii_only: bool = False, width: int = 80,
                      shrink: bool = True) -> list[str]:
    """One bordered table: rows are checks, columns are projects.

    ``sections`` pairs an optional spanning group label with row ids;
    ``columns`` holds ``{"scope", "statuses"}`` per project. Labels and
    scopes shrink with an explicit marker when the table must fit (pass
    ``shrink=False`` to measure the natural width for stacking);
    verdict cells are never truncated. Layout math runs on plain text
    and ANSI styles land on fixed parts afterwards.
    """
    vertical = paint(borders["v"], "dim", color=color)

    def plain_cell(statuses: dict, row_id: str) -> str:
        status = statuses.get(row_id)
        if status is None:
            return ""
        return _grid_cell(status, glyphs, bracket=bracket)

    ids = [row_id for _, group in sections for row_id in group]
    tallies = [_grid_tally(column["statuses"], glyphs)
               for column in columns]
    tally_styles = {glyphs["satisfied"]: "green", glyphs["gap"]: "red",
                    glyphs["unknown"]: "yellow",
                    glyphs["not-applicable"]: "dim"}
    label_w = max([len("check")] + [len(labels[row_id]) for row_id in ids])
    cell_floors = []
    proj_w = []
    for column, tally in zip(columns, tallies):
        floor = max([len(tally), len(column["scope"])]
                    + [len(plain_cell(column["statuses"], row_id))
                       for row_id in ids])
        cell_floors.append(floor)
        proj_w.append(max(len(column["scope"]), floor))
    widths = [label_w, *proj_w]
    if shrink and sum(w + 2 for w in widths) + len(widths) + 1 > width:
        over = sum(w + 2 for w in widths) + len(widths) + 1 - width
        cut = min(over, label_w - 4)
        label_w = label_w - cut
        over -= cut
        for index in range(len(proj_w)):
            if over <= 0:
                break
            cut = min(over, proj_w[index] - cell_floors[index])
            proj_w[index] -= cut
            over -= cut
        widths = [label_w, *proj_w]
    shown_labels = {row_id: _shrink_text(labels[row_id], label_w,
                                         ascii_only=ascii_only)
                    for row_id in ids}
    shown_scopes = [_shrink_text(column["scope"], room, ascii_only=ascii_only)
                    for column, room in zip(columns, proj_w)]

    def show(text: str, room: int, style: str | None) -> str:
        if text and style:
            return paint(text, style, color=color) + " " * (room - len(text))
        return text.ljust(room)

    def row_line(cells: list[str], styles: list) -> str:
        parts = [vertical]
        for text, room, style in zip(cells, widths, styles):
            parts.append(" " + show(text, room, style) + " ")
            parts.append(vertical)
        return "".join(parts)

    def border(left: str, middle: str, right: str) -> str:
        return paint(left + middle.join(borders["h"] * (room + 2)
                                        for room in widths) + right,
                     "dim", color=color)

    def divider(label: str | None) -> str:
        """A rule row: plain, or carrying the group label dim inside it."""
        if label is None:
            return border(borders["ml"], borders["mm"], borders["mr"])
        room = widths[0] + 2
        shown = _shrink_text(label, max(0, room - 4),
                             ascii_only=ascii_only)
        head = f"{borders['h']} {shown} "
        segment = head + borders["h"] * max(0, room - len(head))
        rest = borders["mm"].join(borders["h"] * (room_w + 2)
                                  for room_w in widths[1:])
        return paint(borders["ml"] + segment
                     + (borders["mm"] + rest if widths[1:] else "")
                     + borders["mr"], "dim", color=color)

    lines = [border(borders["tl"], borders["tm"], borders["tr"]),
             row_line(["check", *shown_scopes],
                      ["dim", *["bold"] * len(columns)]),
             border(borders["ml"], borders["mm"], borders["mr"])]
    for span, group in sections:
        if not group:
            continue
        if span is not None:
            # A single-item group needs no duplicate header row: the
            # divider alone separates it from the group above.
            lines.append(divider(span if len(group) > 1 else None))
        for row_id in group:
            cells = [shown_labels[row_id]]
            styles: list = [None]
            for column in columns:
                status = column["statuses"].get(row_id)
                if status is None:
                    cells.append("")
                    styles.append(None)
                else:
                    cells.append(plain_cell(column["statuses"], row_id))
                    styles.append(_ICON_STYLES[_grid_status_key(status)])
            lines.append(row_line(cells, styles))
    lines.append(divider(None))
    foot = [vertical, " " + " " * widths[0] + " ", vertical]
    for tally_text, room in zip(tallies, proj_w):
        foot.append(" " + _paint_tally(tally_text, room, tally_styles,
                                       color=color) + " ")
        foot.append(vertical)
    lines.append("".join(foot))
    lines.append(border(borders["bl"], borders["bm"], borders["br"]))
    return lines


def _paint_gap_head(head: str, icon: str, *, color: bool) -> str:
    """Tint a laid-out gap head: red icon, bold project and label."""
    if not head.startswith(icon + " "):
        return head
    rest = head[len(icon) + 1:]
    return (paint(icon, "red", color=color) + " "
            + paint(rest, "bold", color=color))


def _grid_gap_lines(infos: list, glyphs: dict, *, ascii_only: bool,
                    width: int, color: bool) -> list[str]:
    """Per-gap blocks: red ✗ plus bold project and label, the finding in
    normal weight, then the fix with a tinted arrow.

    Findings wrap whole and are never truncated; the full text stays on
    the terminal exactly as it appears in recommendations.md. Blocks are
    separated by one blank line; layout runs on plain text and paint
    lands afterwards so hanging indents stay aligned.
    """
    mid = " - " if ascii_only else " · "
    arrow = "->" if ascii_only else "→"
    icon = glyphs["gap"]
    blocks = []
    for info in infos:
        scope = _grid_text(info["scope"])
        for row in info["ordered"]:
            if _grid_status_key(row.get("status")) != "gap":
                continue
            head = (f"{icon} {scope}{mid}{_row_label(row)}"
                    f"{_dropped_suffix(row)}")
            block = [_paint_gap_head(head, icon, color=color)]
            finding = info["by_id"].get(row.get("id"))
            if finding is None:
                block.append("  no finding recorded; "
                             "see recommendations.md.")
                blocks.append(block)
                continue
            summary = _agent_assessment_prose(finding.get("summary", ""))
            change = _agent_assessment_prose(
                finding.get("suggested_change", ""))
            if summary:
                block.extend(wrap_words(summary, width, indent="  ",
                                        hang="  "))
            if change:
                for line in wrap_words(f"{arrow} {change}", width,
                                       indent="  ", hang="  "):
                    if line.lstrip().startswith(arrow):
                        line = line.replace(
                            arrow, paint(arrow, "cyan", color=color), 1)
                    block.append(line)
            blocks.append(block)
    lines = []
    for block in blocks:
        if lines:
            lines.append("")
        lines.extend(block)
    return lines


def _grouped_reason_lines(infos: list, glyphs: dict, status: str, *,
                          ascii_only: bool, width: int,
                          color: bool) -> list[str]:
    """One wrapped line per project and reason for unknowns or n/a rows.

    Identical reasons collapse into one line listing the checks
    (``api: Database setup reuse, Cache isolation — <reason>``).
    Continuation lines hang under the reason start. Unknown lines carry
    a yellow bullet, a bold head and a dim reason; n/a lines are dim.
    """
    dash = "-" if ascii_only else "—"
    bullet = glyphs["unknown" if status == "unknown" else "not-applicable"]
    reason_of = _unknown_reason if status == "unknown" else _na_reason
    groups: dict = {}
    order: list = []
    for info in infos:
        scope = _grid_text(info["scope"])
        for row in info["ordered"]:
            if _grid_status_key(row.get("status")) != status:
                continue
            reason = reason_of(row)
            key = (scope, reason)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(_row_label(row))
    lines = []
    for scope, reason in order:
        head = f"{scope}: {', '.join(groups[(scope, reason)])}"
        prefix = f"{bullet} "
        sep = f" {dash} " if reason else ""
        if reason and len(prefix + head + sep) > width // 3:
            # A wide prefix would leave a thin reason column: the head
            # keeps its own line and the reason wraps from four spaces.
            for line in wrap_words(prefix + head, width, indent="",
                                   hang=" " * len(prefix)):
                lines.append(_paint_grouped_head(line, prefix, bullet,
                                                 status, color=color))
            lines.extend(paint(line, "dim", color=color)
                         for line in wrap_words(reason, width, indent="    ",
                                               hang="    "))
            continue
        hang = " " * len(prefix + head + sep) if reason else " " * len(prefix)
        for line in wrap_words(prefix + head + sep + reason, width,
                               indent="", hang=hang):
            lines.append(_paint_grouped_line(line, prefix, head, sep,
                                             bullet, status, color=color))
    return lines


def _paint_grouped_head(line: str, prefix: str, bullet: str, status: str,
                        *, color: bool) -> str:
    """Tint one laid-out head line whose reason moved to the next line."""
    if status != "unknown":
        return paint(line, "dim", color=color)
    if line.startswith(prefix):
        return (paint(bullet, "yellow", color=color) + " "
                + paint(line[len(prefix):], "bold", color=color))
    return paint(line, "bold", color=color)


def _paint_grouped_line(line: str, prefix: str, head: str, sep: str,
                        bullet: str, status: str, *, color: bool) -> str:
    """Tint one laid-out grouped line after wrapping kept it aligned."""
    if status != "unknown":
        return paint(line, "dim", color=color)
    if not line.startswith(prefix):
        return line
    rest = line[len(prefix):]
    tinted_bullet = paint(bullet, "yellow", color=color)
    if sep and rest.startswith(head + sep):
        return (tinted_bullet + " " + paint(head, "bold", color=color)
                + paint(sep, "dim", color=color)
                + paint(rest[len(head + sep):], "dim", color=color))
    if not sep and rest == head:
        return tinted_bullet + " " + paint(head, "bold", color=color)
    if not sep:
        return tinted_bullet + " " + paint(rest, "bold", color=color)
    return tinted_bullet + " " + paint(rest, "dim", color=color)


def _row_label(row: dict) -> str:
    raw_label = row.get("label") or row.get("id")
    label = _agent_assessment_prose(
        raw_label if isinstance(raw_label, str) else "")
    return label or "unknown"


_STATIC_AREAS = ("execution", "parallel", "selection", "timing")


def _offline_static_report(workspace, provider: str | None):
    """The static doctor report for offline grids, else None.

    Offline terminal output shares the grid renderer, but the static
    rule findings, readiness table and review worksheet it replaced must
    keep showing: they render as grid-styled sections below the table.
    Online output never carries them.
    """
    effective = provider if provider is not None else "offline"
    if effective != "offline":
        return None
    return getattr(workspace, "aggregate", None)


def _static_coverage_line(usage) -> str:
    """One scan-coverage line from the static report usage, or unavailable."""
    if usage is None:
        return "Scan coverage: unavailable."
    files = getattr(usage, "files", 0)
    if getattr(usage, "truncated", False):
        return ("Scan coverage: incomplete; "
                f"{C.plural(files, 'file')} inspected, "
                f"{C.plural(getattr(usage, 'skipped', 0), 'entry')} skipped.")
    return (f"Scan coverage: complete; "
            f"{C.plural(files, 'file')} inspected.")


def _static_readiness_lines(workspace, aggregate, *, ascii_only: bool,
                            width: int) -> list[str]:
    """Per-repository readiness states plus the scan-coverage line."""
    mid = " - " if ascii_only else " · "
    lines = [_static_coverage_line(getattr(aggregate, "usage", None))]
    repositories = getattr(workspace, "repositories", None) or ()
    shown = False
    for repo in repositories:
        report = getattr(repo, "report", None)
        if report is None:
            continue
        states = {item.area: item.state
                  for item in (getattr(report, "readiness", None) or ())}
        cells = [f"{area} {_display_state(states.get(area, 'unknown'))}"
                 for area in _STATIC_AREAS]
        lines.extend(wrap_words(
            f"{_grid_text(getattr(repo, 'declaration', 'unknown'))}: "
            + mid.join(cells), width, indent="", hang="  "))
        shown = True
    if not shown:
        states = {item.area: item.state
                  for item in (getattr(aggregate, "readiness", None) or ())}
        cells = [f"{area} {_display_state(states.get(area, 'unknown'))}"
                 for area in _STATIC_AREAS]
        lines.extend(wrap_words(mid.join(cells), width, indent="",
                                hang="  "))
    return lines


def _static_finding_lines(workspace, aggregate, *, ascii_only: bool,
                          width: int) -> list[str]:
    """Static rule findings grouped by severity and code, per repository."""
    findings = list(getattr(aggregate, "findings", None) or ())
    if not findings:
        return ["Findings: none found (static review cannot certify "
                "parallel safety)."]
    severity_order = ("high", "medium", "low")
    counts: dict = {}
    groups: dict = {}
    for finding in findings:
        severity = getattr(finding, "severity", "unknown")
        code = getattr(finding, "code", "unknown")
        counts[severity] = counts.get(severity, 0) + 1
        groups.setdefault((severity, code), []).append(finding)
    summary = ", ".join(f"{counts[severity]} {severity}"
                        for severity in severity_order
                        if counts.get(severity))
    summary += "".join(f", {counts[severity]} {terminal_text(severity)}"
                       for severity in sorted(counts)
                       if severity not in severity_order)
    lines = [f"Findings: {len(findings)} total ({summary})"]
    repo_groups: dict = {}
    for (severity, code), items in groups.items():
        bucket: dict = {}
        for finding in items:
            bucket.setdefault(
                _finding_repo_label(getattr(finding, "path", None),
                                    workspace), []).append(finding)
        for repo_label, bucket_items in bucket.items():
            repo_groups[(repo_label, severity, code)] = bucket_items
    ordered = sorted(
        repo_groups.items(),
        key=lambda item: (str(item[0][0] or ""),
                          severity_order.index(item[0][1])
                          if item[0][1] in severity_order
                          else len(severity_order),
                          item[0][1], item[0][2]),
    )
    for (repo_label, severity, code), items in ordered:
        example = items[0]
        location = "unknown location"
        if getattr(example, "path", None) is not None:
            location = terminal_text(example.path)
            if getattr(example, "line", None) is not None:
                location += f":{example.line}"
        noun = "finding" if len(items) == 1 else "findings"
        prefix = ("" if repo_label is None
                  else f"[{terminal_text(repo_label)}] ")
        lines.extend(wrap_words(
            f"- {prefix}{terminal_text(severity)} {terminal_text(code)}: "
            f"{len(items)} {noun} (e.g. {location})",
            width, indent="", hang="  "))
    return lines


def _static_diagnostic_lines(workspace, *, width: int) -> list[str]:
    """Per-repository configuration diagnostics, wrapped like the rest."""
    lines = []
    for repo in getattr(workspace, "repositories", None) or ():
        problem = getattr(repo, "config_problem", None)
        if problem is None:
            continue
        lines.extend(wrap_words(
            f"- {_grid_text(getattr(repo, 'declaration', 'unknown'))}: "
            f"{terminal_text(getattr(problem, 'code', 'unknown'))}: "
            f"{terminal_text(getattr(problem, 'message', ''))}",
            width, indent="", hang="  "))
    return lines


def _static_limitation_lines(aggregate, *, width: int) -> list[str]:
    """Aggregate non-scan limitations, identical messages counted once."""
    reasons = [item for item in (getattr(aggregate, "limitations", None)
                                 or ())
               if getattr(item, "code", None) != "scan-limit"]
    groups: dict = {}
    order: list = []
    for item in reasons:
        key = (getattr(item, "code", ""), getattr(item, "message", ""))
        if key not in groups:
            groups[key] = 0
            order.append(key)
        groups[key] += 1
    lines = []
    usage = getattr(aggregate, "usage", None)
    if usage is not None and getattr(usage, "truncated", False):
        lines.extend(wrap_words(
            "Scan stopped at configured bounds; detailed limit notices "
            "suppressed.", width, indent="", hang="  "))
    for key in order:
        description = terminal_text(key[1])
        if groups[key] > 1:
            description += f" ({C.plural(groups[key], 'occurrence')})"
        lines.extend(wrap_words(f"- {description}", width, indent="",
                                hang="  "))
    return lines


def _static_worksheet_lines(*, ascii_only: bool, width: int) -> list[str]:
    """The review worksheet as one wrapped catalog line, still per check."""
    dash = "-" if ascii_only else "—"
    catalog = ", ".join(entry.id for entry in checklist_api.CATALOG)
    return wrap_words(
        f"Review worksheet (reviewer fills one copy per repository): "
        f"{catalog} {dash} all unknown (review not yet performed)",
        width, indent="", hang="  ")


def render_agent_assessment(children, workspace, *, report_path: str,
                            publication_status: str,
                            width: int | None = None,
                            color: bool = False, repo: str | None = None,
                            provider: str | None = None,
                            duration_s: float | None = None,
                            calls: int | None = None,
                            encoding: str | None = None) -> str:
    """Render the doctor grid: one checklist table across projects.

    Columns are projects, rows are checklist items grouped general, then
    a parallel-safety group, then Parallel execution. Aligned facts lines
    per project (plus a dim full-suite detail line) sit above the table;
    per-gap findings, unknowns and n/a notes grouped by reason, the
    single next command and the report pointer sit below. Offline grids
    additionally carry the static readiness, rule findings, worksheet,
    diagnostics and limitations sections in the same visual language.
    Terminal output never carries Markdown tables or HTML entities.
    """
    resolved = terminal_width(width)
    ascii_only = _grid_ascii(encoding)
    bracket = ascii_only or "NO_COLOR" in os.environ
    glyphs = _grid_glyphs(bracket=bracket)
    borders = _grid_borders(ascii_only=ascii_only)
    mid = " - " if ascii_only else " · "
    dash = "-" if ascii_only else "—"
    children = list(children)
    infos = [_grid_child_info(child) for child in children]
    runners = [_assessment_runner(info["scope"], workspace) for info in infos]

    tail = []
    tail.append(_grid_text(provider) if provider is not None else "offline")
    tail.append(C.plural(sum(len(info["statuses"]) for info in infos),
                         "check"))
    if isinstance(calls, int) and not isinstance(calls, bool):
        tail.append(C.plural(calls, "call"))
    duration = _grid_duration(duration_s)
    if duration is not None:
        tail.append(duration)
    if repo is not None:
        header = (paint(_grid_text(repo), "bold", color=color)
                  + paint(mid + mid.join(tail), "dim", color=color))
    else:
        header = paint(mid.join(tail), "dim", color=color)

    facts_block = _grid_facts_lines(children, runners, glyphs,
                                    ascii_only=ascii_only, width=resolved,
                                    color=color)

    general, safety, parallel = _grid_row_ids(children)
    labels: dict = {}
    for info in infos:
        for row_id, label in info["labels"].items():
            labels.setdefault(row_id, label)
    columns = [{"scope": _grid_text(info["scope"]),
                "statuses": info["statuses"]} for info in infos]
    groups = [(None, general)]
    if safety:
        groups.append(("parallel safety", safety))
    if parallel:
        groups.append(("Parallel execution", parallel))
    def make_table(cols, *, shrink=True, tint=color) -> list[str]:
        return _grid_table_lines(groups, labels, cols, glyphs, borders,
                                 bracket=bracket, color=tint,
                                 ascii_only=ascii_only, width=resolved,
                                 shrink=shrink)

    tables = []
    if columns:
        if len(columns) == 1:
            tables.append(make_table(columns))
        else:
            # Measure the fit on plain text: ANSI styles add no display
            # width, so measuring painted lines would stack every TTY.
            natural = make_table(columns, shrink=False, tint=False)
            if max(len(line) for line in natural) <= resolved:
                tables.append(make_table(columns))
            else:
                for column in columns:
                    tables.append(make_table([column]))

    gap_lines = _grid_gap_lines(infos, glyphs, ascii_only=ascii_only,
                                  width=resolved, color=color)
    unknown_lines = _grouped_reason_lines(infos, glyphs, "unknown",
                                          ascii_only=ascii_only,
                                          width=resolved, color=color)
    na_lines = _grouped_reason_lines(infos, glyphs, "not-applicable",
                                     ascii_only=ascii_only, width=resolved,
                                     color=color)
    dependency_details = _agent_dependency_detail_lines(children, resolved)
    static_report = _offline_static_report(workspace, provider)
    static_sections: list[tuple[str, list[str], object]] = []
    if static_report is not None:
        static_sections = [
            ("Readiness", _static_readiness_lines(
                workspace, static_report, ascii_only=ascii_only,
                width=resolved),
             lambda count: ("[readiness detail omitted; "
                            "see ptest doctor --json]")),
            ("Static findings", _static_finding_lines(
                workspace, static_report, ascii_only=ascii_only,
                width=resolved),
             lambda count: (f"[{C.plural(count, 'static finding')} "
                            "omitted; see ptest doctor --json]")),
            ("Worksheet", _static_worksheet_lines(ascii_only=ascii_only,
                                                  width=resolved),
             lambda count: ("[worksheet detail omitted; "
                            "see ptest doctor --json]")),
            ("Diagnostics", _static_diagnostic_lines(
                workspace, width=resolved),
             lambda count: ("[diagnostic detail omitted; "
                            "see ptest doctor --json]")),
            ("Limitations", _static_limitation_lines(
                static_report, width=resolved),
             lambda count: ("[limitation detail omitted; "
                            "see ptest doctor --json]")),
        ]
    any_gap = any(_grid_status_key(status) == "gap"
                  for info in infos
                  for status in info["statuses"].values())
    next_command = "ptest doctor --fix" if any_gap else "ptest --full"
    next_line = "Next: " + paint(next_command, "bold", color=color)
    trailer = (f"Report: {terminal_text(report_path)} "
               f"({terminal_text(publication_status)}) {dash} citations, "
               f"fixes and verification steps.")

    # Header, facts, next and trailer are mandatory; tables keep every
    # row but share the byte bound whole: the facts lines above already
    # name every scope, so a table omitted here loses no project, only
    # its per-check cells. Gaps, unknowns, n/a notes, dependency details
    # and static sections share whatever the bound leaves over. Sections
    # join with "\n\n" and the output ends with "\n".
    sections = [header]
    if facts_block:
        sections.append("\n".join(facts_block))
    omitted_tables = 0
    for table in tables:
        trial = [*sections, "\n".join(table), next_line, trailer]
        if (_joined_bytes(trial) <= _AGENT_ASSESSMENT_MAX_BYTES):
            sections.append("\n".join(table))
        else:
            omitted_tables += 1
    if omitted_tables:
        sections.append(
            f"[{C.plural(omitted_tables, 'project table')} omitted; "
            "narrow --scope or see ptest doctor --json]")
    committed = (sum(len(section.encode("utf-8")) for section in sections)
                 + len(next_line.encode("utf-8"))
                 + len(trailer.encode("utf-8"))
                 + 2 * (len(sections) + 1) + 1)
    variable = []
    if gap_lines:
        variable.append(
            ([paint("Gaps", "bold", color=color), *gap_lines],
             lambda count: f"  [{C.plural(count, 'finding')} omitted; "
                           "see recommendations.md]"))
    if unknown_lines:
        variable.append(
            ([paint("Unknowns", "bold", color=color), *unknown_lines],
             lambda count: f"[{C.plural(count, 'unknown')} omitted; "
                           "see recommendations.md]"))
    if na_lines:
        variable.append(
            ([paint("Not applicable", "bold", color=color), *na_lines],
             lambda count: f"[{C.plural(count, 'n/a note')} omitted; "
                           "see recommendations.md]"))
    if dependency_details:
        variable.append(
            ([paint("Dependencies:", "bold", color=color),
              *dependency_details],
             lambda count: (f"[{C.plural(count, 'dependency detail')} "
                            "omitted; see recommendations.md for full "
                            "limitations]")))
    for title, body, marker in static_sections:
        if body:
            variable.append(
                ([paint(title, "bold", color=color), *body], marker))
    share = max(0, _AGENT_ASSESSMENT_MAX_BYTES - committed)
    share //= max(1, len(variable))
    for lines, marker in variable:
        sections.append("\n".join(_fit_lines_with_omission(lines, share,
                                                           marker)))
    sections.extend((next_line, trailer))
    text = "\n\n".join(sections) + "\n"
    if ascii_only:
        text = text.encode("ascii", errors="backslashreplace").decode("ascii")
    return text


def render_json(document: C.PublicDocument) -> bytes:
    """Encode one already-projected public document through the shared codec."""
    if not isinstance(document, C.PublicDocument):
        raise TypeError("render_json requires PublicDocument")
    return C.encode_public_document(
        document.kind, document.data, error=document.error, domain=document.domain,
    )


def _reason(reason: C.Reason) -> dict:
    return {"code": reason.code, "message": reason.message,
            "paths": list(reason.paths)}


def _doctor_data(report: C.DoctorReport) -> dict:
    if not isinstance(report, C.DoctorReport):
        raise TypeError("doctor renderer requires DoctorReport")
    return {
        "scope": list(report.scope),
        "readiness": [
            {"area": item.area, "state": item.state,
             "reasons": [_reason(reason) for reason in item.reasons]}
            for item in report.readiness
        ],
        "findings": [
            {"code": item.code, "severity": item.severity,
             "confidence": item.confidence, "path": item.path,
             "line": item.line, "evidence_type": item.evidence_type,
             "consequence": item.consequence,
             "remediation": item.remediation,
             "verification": item.verification}
            for item in report.findings
        ],
        "limits": None if report.limits is None else {
            "entries": report.limits.entries, "files": report.limits.files,
            "file_bytes": report.limits.file_bytes,
            "total_bytes": report.limits.total_bytes,
            "findings": report.limits.findings,
            "output_bytes": report.limits.output_bytes,
            "elapsed_s": report.limits.elapsed_s,
            "depth": report.limits.depth, "ast_nodes": report.limits.ast_nodes,
        },
        "usage": None if report.usage is None else {
            "entries": report.usage.entries, "files": report.usage.files,
            "file_bytes": report.usage.file_bytes,
            "total_bytes": report.usage.total_bytes,
            "findings": report.usage.findings,
            "output_bytes": report.usage.output_bytes,
            "elapsed_s": report.usage.elapsed_s,
            "skipped": report.usage.skipped,
            "truncated": report.usage.truncated,
        },
        "limitations": [_reason(reason) for reason in report.limitations],
    }


def render_doctor_json(report: C.DoctorReport, *, domain: dict | None = None) -> bytes:
    return render_json(C.PublicDocument(
        kind="doctor", ptest_version=C.PTEST_VERSION, domain=domain,
        data=_doctor_data(report), error=None,
    ))


_DISPLAY_STATE = {
    "ready-for-declared-capability": "ready",
    "blocked": "not ready",
    "unknown": "unknown",
}
_DISPLAY_AREA = {
    "execution": "Execution",
    "parallel": "Parallelism",
    "selection": "Selection",
    "timing": "Timing",
}
_AREA_ORDER = ("execution", "parallel", "selection", "timing")
_HUMAN_OUTPUT_MARKER = "[doctor output truncated at the configured bound]"
_WORKSHEET_UNKNOWN_REASON = "review not yet performed"


def _display_state(state: str) -> str:
    return _DISPLAY_STATE.get(state, terminal_text(state))


def _workspace_repositories(report: C.DoctorReport, workspace) -> tuple:
    """Per-repository views in declaration order, or one direct-report row."""
    if workspace is None:
        label = list(report.scope)[0] if len(report.scope) == 1 else "."
        return ((label, None, report, None),)
    return tuple((repo.declaration, repo.local_scope, repo.report, repo.config_problem)
                 for repo in workspace.repositories)


def _readiness_cell(states: dict[str, str], area: str) -> str:
    return _display_state(states.get(area, "unknown"))


def _readiness_table(report: C.DoctorReport, workspace) -> tuple[list[str], bool]:
    """Numbered per-repository readiness rows; every declaration stays listed."""
    lines = [
        "| # | Repository | Execution | Parallelism | Selection | Timing |",
        "|---|------------|-----------|-------------|-----------|--------|",
    ]
    truncated_label = False
    for index, (label, _local, repo_report, _problem) in enumerate(
            _workspace_repositories(report, workspace), 1):
        # Labels originate in repository configuration. Escape the Markdown
        # cell delimiter after terminal sanitization so they cannot add cells.
        shown = terminal_text(label).replace("|", "\\|")
        encoded = shown.encode("utf-8")
        if len(encoded) > 96:
            shown = encoded[:93].decode("utf-8", "ignore") + "..."
            truncated_label = True
        if shown.endswith("[truncated]"):
            truncated_label = True
        states = {item.area: item.state for item in repo_report.readiness}
        lines.append(
            f"| {index} | {shown} | {_readiness_cell(states, 'execution')} | "
            f"{_readiness_cell(states, 'parallel')} | "
            f"{_readiness_cell(states, 'selection')} | "
            f"{_readiness_cell(states, 'timing')} |"
        )
    return lines, truncated_label


def _worksheet_lines() -> list[str]:
    lines = [
        "Review worksheet (Reviewer fills one copy per repository)",
        "| ID | Criterion | Status | Unknown reason |",
        "|----|-----------|--------|----------------|",
    ]
    for entry in checklist_api.CATALOG:
        lines.append(
            f"| {entry.id} | {entry.criterion} | unknown | {_WORKSHEET_UNKNOWN_REASON} |"
        )
    return lines


def _finding_repo_label(path: str | None, workspace) -> str | None:
    if workspace is None or path is None:
        return None
    for repo in workspace.repositories:
        if path == repo.declaration or path.startswith(repo.declaration + "/"):
            return repo.declaration
    return None


def render_doctor(report: C.DoctorReport, workspace=None) -> str:
    """Render a concise, non-executing doctor summary for interactive terminals.

    The versioned assessment document is available via ``doctor --json``.
    Terminal output intentionally groups repeated static hypotheses
    so a scan cap cannot turn routine diagnosis into an unreadable stream of
    identical messages.
    """
    if not isinstance(report, C.DoctorReport):
        raise TypeError("doctor renderer requires DoctorReport")

    severity_order = ("high", "medium", "low")
    severity_counts = {severity: 0 for severity in severity_order}
    groups: dict[tuple[str, str], list[C.Finding]] = {}
    for finding in report.findings:
        severity_counts.setdefault(finding.severity, 0)
        severity_counts[finding.severity] += 1
        groups.setdefault((finding.severity, finding.code), []).append(finding)

    usage = report.usage
    if usage is None:
        coverage = "Scan coverage: unavailable."
    elif usage.truncated:
        coverage = ("Scan coverage: incomplete; "
                    f"{C.plural(usage.files, 'file')} inspected, "
                    f"{C.plural(usage.skipped, 'entry')} skipped.")
    else:
        coverage = (f"Scan coverage: complete; "
                    f"{C.plural(usage.files, 'file')} inspected.")

    lines = [
        "ptest doctor",
        "Static review only: no tests, services, or network calls ran.",
    ]
    table, label_truncated = _readiness_table(report, workspace)
    lines.extend(table)
    if label_truncated:
        lines.append("Note: a repository label is shown truncated; "
                     "see report limitations for this condition.")
    lines.extend((coverage, "", *_worksheet_lines()))

    if report.findings:
        summary = ", ".join(
            f"{severity_counts[severity]} {severity}"
            for severity in severity_order if severity_counts.get(severity)
        )
        extra_severities = sorted(
            severity for severity in severity_counts if severity not in severity_order
            and severity_counts[severity]
        )
        summary += "".join(
            f", {severity_counts[severity]} {terminal_text(severity)}"
            for severity in extra_severities
        )
        lines.extend(("", "Static hypotheses",
                      f"Findings: {len(report.findings)} total ({summary})"))
        repo_groups: dict[tuple[str | None, str, str], list[C.Finding]] = {}
        for (severity, code), findings in groups.items():
            bucket: dict[str | None, list[C.Finding]] = {}
            for finding in findings:
                bucket.setdefault(_finding_repo_label(finding.path, workspace), []).append(finding)
            for repo_label, items in bucket.items():
                repo_groups[(repo_label, severity, code)] = items
        ordered_groups = sorted(
            repo_groups.items(),
            key=lambda item: (str(item[0][0] or ""),
                              severity_order.index(item[0][1])
                              if item[0][1] in severity_order else len(severity_order),
                              item[0][1], item[0][2]),
        )
        for (repo_label, severity, code), findings in ordered_groups:
            example = findings[0]
            location = "unknown location"
            if example.path is not None:
                location = terminal_text(example.path)
                if example.line is not None:
                    location += f":{example.line}"
            noun = "finding" if len(findings) == 1 else "findings"
            prefix = "" if repo_label is None else f"[{terminal_text(repo_label)}] "
            lines.append(
                f"- {prefix}{terminal_text(severity):<5} {terminal_text(code)}: "
                f"{len(findings)} {noun} (e.g. {location})"
            )
    else:
        lines.extend(("", "Static hypotheses",
                      "Static hypotheses: none found (static review cannot certify "
                      "parallel safety)."))

    diagnostics = [
        (label, problem)
        for label, _local, _repo_report, problem
        in _workspace_repositories(report, workspace)
        if problem is not None
    ]
    if diagnostics:
        lines.append("Configuration diagnostics:")
        for label, problem in diagnostics:
            lines.append(f"- {terminal_text(label)}: {terminal_text(problem.code)}: "
                         f"{terminal_text(problem.message)}")
    non_scan_limitations: dict[tuple[str, str], int] = {}
    for reason in report.limitations:
        if reason.code != "scan-limit":
            key = (reason.code, reason.message)
            non_scan_limitations[key] = non_scan_limitations.get(key, 0) + 1
    if usage is not None and usage.truncated:
        lines.append("Scan stopped at configured bounds; detailed limit notices suppressed.")
    if non_scan_limitations:
        descriptions = []
        for (_, message), count in non_scan_limitations.items():
            description = terminal_text(message)
            if count > 1:
                description += f" ({C.plural(count, 'occurrence')})"
            descriptions.append(description)
        lines.append("Limitations: " + "; ".join(descriptions))
    lines.extend(("", "Next: ptest doctor --json"))
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > C.MAX_PROMPT_BYTES:
        room = C.MAX_PROMPT_BYTES - len(_HUMAN_OUTPUT_MARKER.encode("utf-8"))
        piece = text.encode("utf-8")[:room].decode("utf-8", errors="ignore")
        return piece + _HUMAN_OUTPUT_MARKER
    return text


def _guide() -> str:
    # Export must be the actual bundled resource; absent data is a packaging
    # error, never permission to substitute another instruction set.
    try:
        return importlib.resources.files("ptest").joinpath(
            "resources", "agent-guide.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise C.Problem(code="state-unavailable", message="bundled agent guide is unavailable",
                        phase="render") from None


def render_guide() -> str:
    lines = [_guide().rstrip(), "", "Doctor assessment checklist"]
    for entry in checklist_api.CATALOG:
        lines.extend(("", f"## {entry.id}", entry.criterion,
                      f"Evidence: {entry.evidence}",
                      f"Recommendation: {entry.recommendation}",
                      f"Verification: {entry.verification}"))
        if entry.recipe is not None:
            lines.extend(("Example:", checklist_api.load_recipe(entry.recipe).rstrip()))
    return "\n".join(lines) + "\n"


__all__ = [
    "render_json", "render_doctor_json", "render_doctor",
    "render_guide", "terminal_text", "colors_enabled", "paint",
]
