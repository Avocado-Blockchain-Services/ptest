"""Tests for project_facts: width clamp, atom wrapping, fact lines."""
from __future__ import annotations

import os

from ptest import project_facts as PF


def test_fact_keys_literal():
    assert PF.FACT_KEYS == (
        "project", "runner", "runs", "runs_reason", "runs_fix",
        "parallel", "parallel_short", "parallel_fix",
        "setup", "full_suite", "full_blocked",
    )
    assert PF.MIN_WIDTH == 60
    assert PF.MAX_WIDTH == 110


def test_terminal_width_clamp():
    assert PF.terminal_width(80) == 80
    assert PF.terminal_width(10) == 60
    assert PF.terminal_width(500) == 110
    assert PF.terminal_width(None) is not None
    assert 60 <= PF.terminal_width(None) <= 110


def test_check_facts_valid_and_invalid():
    good = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": "uv sync --locked",
        "full_suite": 'your pytest config: -m "not extended_migration"',
        "full_blocked": None,
    }
    assert PF.check_facts(good) == good
    assert PF.check_facts({"project": ".", "runner": "vitest", "runs": True}) == {
        "project": ".", "runner": "vitest", "runs": True}
    assert PF.check_facts(None) is None
    assert PF.check_facts([]) is None
    assert PF.check_facts({"bogus": 1}) is None
    assert PF.check_facts({"project": "api", "runs": "yes"}) is None
    assert PF.check_facts({"project": "api", "runs": True,
                           "runner": "bazooka"}) is None
    assert PF.check_facts({"project": "api", "runs": True,
                           "parallel": "x\ny"}) is None
    assert PF.check_facts({"project": "", "runs": True}) is None


def test_check_facts_rejects_c1_controls_and_bidi_overrides():
    """Terminal-escape and display-spoofing characters fail validation.

    C1 controls (U+0080–U+009F, e.g. the single-byte CSI U+009B) and
    bidi overrides (U+202E) survive the old C0/DEL-only check but are
    not printable, so facts carrying them are invalid.
    """
    base = {"project": "api", "runner": "pytest", "runs": True}
    bidi = chr(0x202E)  # right-to-left override: display spoofing
    csi = chr(0x9B)  # single-byte CSI: terminal escape injection
    assert PF.check_facts(
        {**base, "setup": "uv sync " + bidi + "KCOL"}) is None
    assert PF.check_facts(
        {**base, "setup": "uv sync " + csi + "31m"}) is None
    assert PF.check_facts(
        {**base, "full_suite": "suite " + bidi + " reversed"}) is None
    assert PF.check_facts({**base, "setup": "uv sync"}) is None
    # printable Unicode prose (em dash, arrow, middle dot) still passes
    assert PF.check_facts(
        {**base, "runs": False, "runs_reason": "no tests — empty",
         "runs_fix": "add a test → run"}) is not None


def test_summary_atoms():
    facts = {"project": "api", "runner": "pytest", "runs": True,
             "parallel_short": "4 workers", "setup": "uv sync --locked"}
    assert PF.summary_atoms(facts) == [
        "runs: yes", "parallel: 4 workers", "setup: uv sync --locked"]
    facts = {"project": "api", "runner": "pytest", "runs": False,
             "runs_reason": "no tests found", "runs_fix": "add a test",
             "parallel_short": None}
    assert PF.summary_atoms(facts) == ["runs: no — no tests found → add a test"]
    facts = {"project": ".", "runner": "command", "runs": True}
    assert PF.summary_atoms(facts) == ["runs: yes"]


def test_detail_and_long_lines():
    facts = {"project": "api", "runs": True,
             "parallel": "4 workers (xdist, --dist loadgroup)",
             "parallel_short": "4 workers",
             "setup": "uv sync --locked",
             "full_suite": "your pytest config: -m \"not slow\""}
    assert PF.detail_lines(facts) == [
        'full suite = your pytest config: -m "not slow"']
    assert PF.long_lines(facts) == [
        "runs: yes",
        "parallel: 4 workers (xdist, --dist loadgroup)",
        "setup: uv sync --locked (ptest runs it when needed)",
        'full suite = your pytest config: -m "not slow"',
    ]
    blocked = {"project": "api", "runs": False, "runs_reason": "r",
               "runs_fix": "f", "full_blocked": 'test_roots is "."'}
    assert PF.detail_lines(blocked) == [
        'full suite: not available — test_roots is "."']
    assert PF.long_lines(blocked)[0] == "runs: no — r → f"
    assert PF.detail_lines({"project": ".", "runs": True}) == []
    assert PF.long_lines({"project": ".", "runs": True}) == ["runs: yes"]


def test_wrap_atoms_never_splits_atom():
    atoms = ["runs: yes", "parallel: 4 workers",
             "setup: uv sync --locked --extra test"]
    lines = PF.wrap_atoms(atoms, 60)
    assert lines == ["runs: yes · parallel: 4 workers",
                     "setup: uv sync --locked --extra test"]
    # every atom appears whole on exactly one line
    for atom in atoms:
        assert sum(atom in line for line in lines) == 1
    # hanging indent on continuation
    lines = PF.wrap_atoms(atoms, 60, indent="  ", hang="    ")
    assert lines[0].startswith("  runs: yes")
    assert lines[1].startswith("    setup:")
    # oversize atom overflows rather than breaking
    long_atom = "setup: /very/long/path/" + "x" * 100
    assert PF.wrap_atoms([long_atom], 60) == [long_atom]


def test_wrap_words_word_boundary_and_hang():
    text = ("full suite = your pytest config with a long marker value "
            "-m \"not extended_migration\" plus conftest.py hooks")
    lines = PF.wrap_words(text, 60, indent="    ")
    assert lines[0].startswith("    full suite = your")
    for line in lines:
        assert len(line) <= 60 or " " not in line.strip()
    assert " ".join(line.strip() for line in lines).split() == text.split()
    lines = PF.wrap_words(text, 60, indent="  ", hang="    ")
    assert lines[0].startswith("  full")
    assert lines[1].startswith("    ")
    # sub-minimum widths clamp to 60 rather than splitting harder
    assert PF.wrap_words("a b c", 10) == ["a b c"]
    # NO_COLOR has no effect on wrapping itself
    os.environ.pop("NO_COLOR", None)
    assert PF.wrap_words("a b c", 60) == ["a b c"]
