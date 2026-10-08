"""Tier-1 test-writing guidance: the `tests` recipe, the guide wiring and the docs.

The barrier guide section is verified here, never edited. The recipe is the
full checklist behind `ptest guide tests`.
"""
from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

from ptest import checklist, render

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = files("ptest").joinpath("resources")


def _read(relative: str) -> str:
    return RESOURCES.joinpath(relative).read_text(encoding="utf-8")


def _recipe() -> str:
    return checklist.load_recipe("tests")


# -- recipe ------------------------------------------------------------------

def test_tests_recipe_is_registered_and_loads():
    assert "tests" in checklist.recipe_names()
    assert _recipe().startswith("# Writing tests\n")


def test_tests_recipe_is_not_attached_to_any_catalog_row():
    assert all(entry.recipe != "tests" for entry in checklist.CATALOG)


def test_tests_recipe_states_every_required_rule():
    text = _recipe()
    flat = " ".join(text.split()).lower()
    for needle in (
        "oracle", "mock", "copied from the implementation", "snapshot",
        "assertion", "private", "import",
        "one behavior test per requirement",
        "one repro test per bug",
        "one hostile-input table per trust boundary",
        "role x action",
        "floor, not a target",
        "@pytest.mark.parametrize", "test.each",
    ):
        assert needle in flat, needle


def test_tests_recipe_covers_each_anti_pattern_and_the_coverage_rule():
    flat = " ".join(_recipe().split()).lower()
    for needle in (
        "only that a call happened",          # mock-echo tests
        "unreviewed snapshot",
        "assertion-less",
        "private function",
        "import-only",
        "deleting dead code",
        "assertion-free tests",
    ):
        assert needle in flat, needle


def test_tests_recipe_is_bounded_and_cites_nothing_external():
    text = _recipe()
    assert len(text.encode("utf-8")) < checklist._RECIPE_MAX_BYTES
    assert not re.search(r"\d+\s*(%|percent)", text)
    assert not re.search(r"(?i)\b(benchmark|swe-bench|leaderboard|according to a study)\b", text)
    assert not re.search(r"https?://", text)
    assert "expected:" not in text


def test_tests_recipe_examples_are_balanced_code_fences():
    text = _recipe()
    assert text.count("```") % 2 == 0
    assert "```python" in text and "```" in text.split("```python", 1)[1]
    assert "test.each" in text


# -- ptest guide rendering ---------------------------------------------------

def test_render_guide_includes_tests_recipe_once_after_the_catalog():
    guide = render.render_guide()
    assert guide.count("# Writing tests\n") == 1
    assert "Doctor assessment checklist" in guide
    for entry in checklist.CATALOG:
        assert f"## {entry.id}" in guide
    assert guide.index("TIMING-001") < guide.index("# Writing tests\n")
    assert guide.endswith(_recipe().rstrip() + "\n")


def test_guide_recipes_are_known_recipe_names():
    assert checklist.GUIDE_RECIPES == ("tests",)
    assert set(checklist.GUIDE_RECIPES) <= set(checklist.recipe_names())


# -- agent-guide.md reword ---------------------------------------------------

def test_agent_guide_keeps_the_coverage_gate_and_never_pads_coverage():
    text = _read("agent-guide.md")
    flat = " ".join(text.split())
    assert "test semantics and the existing coverage gate" in flat
    assert "never add tests only to raise coverage" in flat
    assert "test inventory, coverage," not in flat
    assert flat.index("scoped `ptest` command") < flat.index("one `ptest --full` final gate")


# -- barrier guide (verified, not edited) ------------------------------------

def _section(text: str) -> str:
    start = text.index("## Writing tests\n")
    return text[start:text.index("\n## Reporting", start)]


def test_barrier_guide_copies_are_identical():
    packaged = RESOURCES.joinpath("repository-agent-guide.md").read_bytes()
    assert (ROOT / "docs" / "ptest-agent.md").read_bytes() == packaged


def test_barrier_writing_tests_section_shape_and_content():
    text = _read("repository-agent-guide.md")
    assert (text.index("## Test-quality rules") < text.index("## Writing tests")
            < text.index("## Reporting"))
    section = _section(text)
    body = [line for line in section.splitlines()[1:] if line.strip()]
    assert len(body) == 6
    assert not re.search(r"\d", section)
    for needle in (
        "oracle", "failing test that reproduces the bug",
        "Never add tests only to raise coverage", "existing coverage gate",
        "hostile-input", "`ptest guide tests`",
    ):
        assert needle in section, needle


# -- README and changelog ----------------------------------------------------

def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _flat_readme() -> str:
    return " ".join(_readme().split())


def test_readme_documents_the_opt_in_policy():
    flat = _flat_readme()
    for needle in (
        "ptest init --test-policy", "--no-test-policy",
        "ptest rules --apply --test-policy",
        "docs/ptest-test-policy.md",
        "ptest guide tests",
        "default No",
    ):
        assert needle in flat, needle
    assert re.search(r"never asks[^.]*\bCI\b[^.]*--json[^.]*--dry-run", flat)


def test_readme_documents_refresh_uninstall_and_doctor_policy_facts():
    flat = _flat_readme()
    assert "ptest uninstall" in flat and "policy file" in flat
    assert "Test policy" in flat
    assert "not inspected" in flat
    assert "sends nothing" in flat
    assert "16 scenarios" in flat


def test_readme_and_changelog_cite_no_external_benchmark():
    changelog = (ROOT / "docs" / "changelog.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## ", 2)[1]
    for text in (_readme(), unreleased):
        assert not re.search(r"(?i)swe-bench|leaderboard|benchmark(?:ed)? shows", text)


def test_changelog_newest_section_directly_under_title_documents_the_feature():
    lines = (ROOT / "docs" / "changelog.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Changelog"
    assert lines[1] == ""
    assert re.fullmatch(r"## (Unreleased|\d+\.\d+\.\d+)", lines[2]), lines[2]
    body = " ".join("\n".join(lines[3:]).split("\n## ", 1)[0].split())
    for needle in ("ptest guide tests", "--test-policy", "Test policy",
                   "coverage gate"):
        assert needle in body, needle
