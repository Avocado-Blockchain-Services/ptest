"""Shared width/wrapping and plain-language fact helpers.

Used by init_render, render and recommendations. This module never imports
``ptest.executability``: it works on plain dicts whose keys are a subset of
``FACT_KEYS`` (a literal mirror of ``executability.FACT_KEYS``).
"""
from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence

MIN_WIDTH = 60
MAX_WIDTH = 110

FACT_KEYS: tuple[str, ...] = (
    "project", "runner", "runs", "runs_reason", "runs_fix",
    "parallel", "parallel_short", "parallel_fix",
    "setup", "full_suite", "full_blocked",
)

_RUNNERS = frozenset({"pytest", "vitest", "command", "unknown"})


def terminal_width(width: int | None = None) -> int:
    """Terminal width clamped to [MIN_WIDTH, MAX_WIDTH]."""
    if width is None:
        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except (OSError, ValueError):
            width = 80
    try:
        columns = int(width)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        columns = 80
    return max(MIN_WIDTH, min(MAX_WIDTH, columns))


def _is_clean_text(value: object, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not value and not allow_empty:
        return False
    if len(value.encode("utf-8")) > 512:
        return False
    # isprintable rejects C0 controls, DEL, C1 controls (U+0080–U+009F,
    # e.g. the single-byte CSI), bidi overrides and other format
    # characters, so config-derived facts cannot carry terminal escapes
    # or display-spoofing characters past validation.
    return value.isprintable()


def check_facts(value: object) -> dict | None:
    """Validated copy of a facts dict, or None when invalid.

    Keys must be a subset of FACT_KEYS; ``runs`` is a bool; ``project``
    and ``runner`` (when present) are clean nonempty text with ``runner``
    in the known set; every other present value is clean text or None.
    """
    if not isinstance(value, Mapping):
        return None
    cleaned: dict = {}
    for key, item in value.items():
        if key not in FACT_KEYS:
            return None
        if key == "runs":
            if not isinstance(item, bool):
                return None
            cleaned[key] = item
        elif key in ("project", "runner"):
            if not _is_clean_text(item):
                return None
            if key == "runner" and item not in _RUNNERS:
                return None
            cleaned[key] = item
        else:
            if item is not None and not _is_clean_text(item):
                return None
            cleaned[key] = item
    return cleaned


def _runs_atom(facts: Mapping[str, object]) -> str:
    if facts.get("runs"):
        return "runs: yes"
    reason = facts.get("runs_reason")
    fix = facts.get("runs_fix")
    if isinstance(reason, str) and reason and isinstance(fix, str) and fix:
        return f"runs: no — {reason} → {fix}"
    if isinstance(reason, str) and reason:
        return f"runs: no — {reason}"
    return "runs: no"


def summary_atoms(facts: Mapping[str, object]) -> list[str]:
    """One-line atoms: runs, parallel (short), setup."""
    atoms = [_runs_atom(facts)]
    short = facts.get("parallel_short")
    if short is None:
        short = facts.get("parallel")
    if isinstance(short, str) and short:
        atoms.append(f"parallel: {short}")
    setup = facts.get("setup")
    if isinstance(setup, str) and setup:
        atoms.append(f"setup: {setup}")
    return atoms


def detail_lines(facts: Mapping[str, object]) -> list[str]:
    """Full-suite detail lines (empty when neither is set)."""
    suite = facts.get("full_suite")
    if isinstance(suite, str) and suite:
        return [f"full suite = {suite}"]
    blocked = facts.get("full_blocked")
    if isinstance(blocked, str) and blocked:
        return [f"full suite: not available — {blocked}"]
    return []


def detail_atoms(detail: str) -> list[str]:
    """Split one detail line into unbreakable atoms at ', ' boundaries.

    ``full suite = a, b`` becomes ``["full suite = a,", "b"]`` so both
    terminal renderers wrap the same fact between atoms — never inside
    a quoted command — with a shared implementation. Anything else is
    already one atom.
    """
    if detail.startswith("full suite = ") and ", " in detail:
        head, rest = detail.split(" = ", 1)
        parts = rest.split(", ")
        atoms = []
        for index, part in enumerate(parts):
            prefix = f"{head} = " if index == 0 else ""
            suffix = "," if index < len(parts) - 1 else ""
            atoms.append(f"{prefix}{part}{suffix}")
        return atoms
    return [detail]


def long_lines(facts: Mapping[str, object]) -> list[str]:
    """Report bullets: runs, parallel (long), setup, full suite."""
    lines = [_runs_atom(facts)]
    parallel = facts.get("parallel")
    if isinstance(parallel, str) and parallel:
        lines.append(f"parallel: {parallel}")
    setup = facts.get("setup")
    if isinstance(setup, str) and setup:
        lines.append(f"setup: {setup} (ptest runs it when needed)")
    lines.extend(detail_lines(facts))
    return lines


def wrap_atoms(atoms: Sequence[str], width: int | None, *, indent: str = "",
               hang: str | None = None, sep: str = " · ") -> list[str]:
    """Join atoms with ``sep``, breaking only between atoms.

    Atoms are unbreakable: an atom wider than the line overflows rather
    than splitting. Continuation lines use ``hang`` (default ``indent``).
    """
    width = terminal_width(width)
    cont = indent if hang is None else hang
    lines: list[str] = []
    current = indent
    first = True
    for atom in atoms:
        piece = atom if first else sep + atom
        if first:
            current = indent + atom
            first = False
            continue
        if len(current) + len(piece) <= width:
            current += piece
        else:
            lines.append(current)
            current = cont + atom
    lines.append(current)
    return lines


def wrap_words(text: str, width: int | None, *, indent: str = "",
               hang: str | None = None) -> list[str]:
    """Wrap prose at spaces; overlong words overflow, never split."""
    width = terminal_width(width)
    cont = indent if hang is None else hang
    chunks = text.split()
    if not chunks:
        return [indent.rstrip()] if indent else [""]
    lines: list[str] = []
    current = indent + chunks[0]
    for word in chunks[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = cont + word
    lines.append(current)
    return lines
