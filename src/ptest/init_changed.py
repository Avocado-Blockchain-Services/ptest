"""`ptest init` follow-up: offer `ptest --changed` setup per pytest project.

After the smoke step, init asks once per pytest project whose environment
holds the frozen pytest-cov/coverage pair whether to enable test
selection. The ``[selection]`` draft itself comes from ``doctor_fix``'s
planner (no duplicate logic here); this module only ensures the coverage
argv the planner keys on, asks the question, and reports. Existing
configs are never rewritten here: they point at ``ptest doctor --fix``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from . import doctor_fix
from . import executability as executability_api
from .runtime.pytest_bridge import _COVERAGE_TUPLE

#: Accepted answers for the setup question; the default is ``later``.
CHOICES = ("now", "later", "no")
DEFAULT_CHOICE = "later"

#: Coverage argv init writes before asking the planner for its draft.
COV_ARGV = ("--cov", "--cov-report", "term")

QUESTION = (
    "Set up ptest --changed for {project}? Adds coverage to test runs; "
    "needs one full run as a baseline. [now/later/no] (default: later)"
)

NEEDS_COV_LINE = (
    "{project}: ptest --changed needs pytest-cov (add it to the test deps)"
)

LATER_LINE = (
    "{project}: selection drafted; the baseline is recorded on the first "
    "passing --full/--changed on a clean tree"
)

NOW_LINE = "{project}: selection drafted; running ptest --full for the baseline"

EXISTING_LINE = (
    "{project}: config already exists; run `ptest doctor --fix` "
    "to set up ptest --changed"
)

DRY_RUN_LINE = (
    "would set up ptest --changed for {project}: write --cov/--cov-report "
    "plus the [selection] draft; the baseline is recorded on the first "
    "passing --full/--changed on a clean tree"
)


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="init")


def parse_choice(value: str) -> str:
    """Validate one ``--changed-setup`` value, or raise."""
    if value in CHOICES:
        return value
    raise _problem("unsupported-capability",
                   "changed-setup value is not supported (now, later, or no)")


def ask_choice(project: str) -> str:
    """Ask the setup question once on the terminal; Enter takes the default."""
    from .render import terminal_text
    print(QUESTION.format(project=terminal_text(project)), file=sys.stderr)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return DEFAULT_CHOICE
    if not answer:
        return DEFAULT_CHOICE
    return answer if answer in CHOICES else DEFAULT_CHOICE


def has_frozen_pair(config: C.Config) -> bool:
    """True when the project environment holds the frozen cov pair."""
    try:
        found, problem = executability_api.coverage_environment_tuple(config)
    except Exception:
        return False
    return problem is None and tuple(found) == tuple(_COVERAGE_TUPLE)


def _config_rel(declaration: str) -> str:
    return ".ptest.toml" if declaration == "." else f"{declaration}/.ptest.toml"


def ensure_cov_argv(root: Path, declaration: str) -> bool:
    """Append the coverage argv to a fresh config; True when written.

    Uses ``doctor_fix``'s line surgery so unmanaged settings stay
    byte-identical. Returns False when ``--cov`` is already present.
    """
    rel = _config_rel(declaration)
    raw, identity = doctor_fix._read_text(root, rel)
    parsed = doctor_fix._parse_table(raw, rel)
    runner = parsed.get("runner", {})
    current = ()
    if isinstance(runner, dict) and isinstance(runner.get("args"), list):
        current = tuple(
            item for item in runner["args"] if isinstance(item, str))
    if doctor_fix._has_cov(current):
        return False
    updated = doctor_fix._apply_changes(
        raw, [doctor_fix.FieldChange("runner", "args",
                                     tuple(current) + COV_ARGV)])
    doctor_fix._atomic_write_text(root, rel, updated, expect=raw,
                                  identity=identity)
    return True


@dataclass(frozen=True, slots=True)
class SetupTarget:
    """One project the question applies to."""

    declaration: str
    config: C.Config


def apply_setup(root: Path, target: SetupTarget) -> None:
    """Write coverage argv plus the planner's ``[selection]`` draft.

    ``doctor_fix.plan_project`` owns the draft; this only ensures the
    ``--cov`` precondition it keys on, then applies its plan.
    """
    ensure_cov_argv(root, target.declaration)
    planned = doctor_fix.plan_project(root, target.declaration,
                                      target.config)
    if planned is not None:
        doctor_fix.apply_plan(
            root, doctor_fix.FixPlan(files=(planned,), refusals=()))


def selection_enabled(config: C.Config) -> bool:
    """True when the resolved config already enables selection."""
    selection = getattr(config, "selection", None)
    return bool(selection is not None and selection.enabled is True)
