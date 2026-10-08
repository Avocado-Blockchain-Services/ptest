"""T2 Test policy rendering: terminal block and Markdown section.

Strict TDD: written before ``src/ptest/policy_render.py``; failed at
collection until the module existed.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest


def _scan(*lines):
    items = tuple(SimpleNamespace(path=name, line=number, text=text)
                  for name, number, text in lines)
    return SimpleNamespace(lines=items, skipped=(), truncated=False)


def _facts(project=".", **overrides):
    from ptest import policy_facts
    base = dict(project=project, gates=(), branch=False, branch_sources=(),
                omit=(), omit_total=0, pragma_count=0, pragma_complete=True,
                vitest_not_inspected=False,
                instructions=policy_facts.InstructionScan(), notes=())
    base.update(overrides)
    return policy_facts.ProjectPolicyFacts(**base)


def _report(projects, **overrides):
    from ptest import policy_facts
    base = dict(projects=tuple(projects), root_instructions=None,
                policy_installed=True)
    base.update(overrides)
    return policy_facts.PolicyReport(**base)


def _gate(source="pyproject.toml [tool.coverage.report] fail_under",
          value="85"):
    from ptest import policy_facts
    return policy_facts.CoverageGate(source, value)


# ---- terminal shape ----

def test_empty_report_renders_empty():
    from ptest import policy_render
    report = _report(())
    assert policy_render.render_terminal(report) == ""


def test_terminal_shape():
    from ptest import policy_render
    text = policy_render.render_terminal(_report([_facts()]))
    assert text.startswith("\n")
    assert text.endswith("\n")
    first = next(line for line in text.split("\n") if line.strip())
    assert first == "Test policy"


def test_no_gate_fact_line():
    from ptest import policy_render
    text = policy_render.render_terminal(_report([_facts()]))
    assert "coverage gate: none found" in text


def test_gate_and_branch_facts():
    from ptest import policy_render
    facts = _facts(gates=(_gate(),), branch=True,
                   branch_sources=("pyproject.toml [tool.coverage.run] branch",))
    text = policy_render.render_terminal(_report([facts]))
    assert "85" in text and "fail_under" in text
    assert "branch" in text.lower()


def test_vitest_fixed_line():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(vitest_not_inspected=True)]))
    assert ("vitest coverage thresholds: not inspected (config is code)"
            in text)


def test_vitest_absent_no_line():
    from ptest import policy_render
    text = policy_render.render_terminal(_report([_facts()]))
    assert "vitest coverage thresholds" not in text


def test_risks_print_only_when_they_apply():
    from ptest import policy_render
    clean = policy_render.render_terminal(_report([_facts()]))
    assert "line-only" not in clean
    assert "Omit patterns" not in clean
    assert "pragma: no cover" not in clean
    line_only = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),))]))
    assert "counts lines that ran" in line_only
    omitted = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),), omit=("gen/*",), omit_total=1)]))
    assert "Omit patterns hide those files" in omitted
    pragmad = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),), pragma_count=3)]))
    assert "3 lines are marked `pragma: no cover`" in pragmad


def test_suggestion_block_and_opt_in_pointer():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),))], policy_installed=False))
    assert ("Keep the existing coverage gate as a floor; do not raise it "
            "by adding tests." in text)
    assert "never with assertion-free tests." in text
    assert "Consider branch coverage" in text
    assert "ptest rules --apply --test-policy" in text


def test_opt_in_pointer_absent_when_installed():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),))], policy_installed=True))
    assert "ptest rules --apply --test-policy" not in text


def test_instruction_lines_listed_with_conflict_header():
    from ptest import policy_render
    facts = _facts(gates=(_gate(),),
                   instructions=_scan(("AGENTS.md", 12, "Keep coverage above 90%.")))
    text = policy_render.render_terminal(_report([facts]))
    assert "ptest never edits them" in text
    assert "AGENTS.md:12: Keep coverage above 90%." in text


def test_terminal_neutralizes_hostile_text():
    from ptest import policy_render
    hostile = "Keep coverage above 90%.\x1b[31mred\u202e!"
    facts = _facts(instructions=_scan(("AGENTS.md", 1, hostile)))
    text = policy_render.render_terminal(_report([facts]))
    assert "\x1b" not in text
    assert "\u202e" not in text


def test_terminal_no_ansi_without_color():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),))]), color=False)
    assert "\x1b" not in text


def test_terminal_ascii_fallback():
    from ptest import policy_render
    facts = _facts(gates=(_gate(),), omit=("src/caf\u00e9/*",), omit_total=1)
    text = policy_render.render_terminal(_report([facts]), encoding="ascii")
    text.encode("ascii")


def test_terminal_partial_pragma():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),), pragma_count=7,
                        pragma_complete=False)]))
    assert "at least 7" in text


# ---- markdown ----

def test_markdown_shape_and_neutralization():
    from ptest import policy_render
    hostile = "`x`](http://evil) <b>|pipe\n```fence"
    facts = _facts(gates=(_gate(value=hostile),),
                   instructions=_scan(("AGENTS.md", 1, hostile)))
    text = policy_render.render_markdown(_report([facts]))
    assert text.startswith("## Test policy")
    # Backslash-escaped brackets cannot form a Markdown link.
    assert re.search(r"(?<!\\)\]\(http://", text) is None
    assert "<b>" not in text
    assert "```" not in text
    assert re.search(r"(?<!\\)\|pipe", text) is None


def test_markdown_empty_report():
    from ptest import policy_render
    assert policy_render.render_markdown(_report(())) == ""


# ---- gate-aware wording and plurals ----

def test_pragma_risk_without_gate_never_mentions_a_gate():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(pragma_count=3)]))
    assert "3 lines are marked `pragma: no cover`" in text
    assert "against the gate" not in text
    assert "existing coverage gate" not in text


def test_no_gate_suggestions_do_not_claim_an_existing_gate():
    from ptest import policy_render
    for render in (policy_render.render_terminal,
                   policy_render.render_markdown):
        text = render(_report([_facts(pragma_count=2)]))
        assert "Keep the existing coverage gate" not in text
        assert "no coverage gate was found" in text.lower()


def test_gate_suggestion_wording_is_unchanged_when_a_gate_exists():
    from ptest import policy_render
    text = policy_render.render_terminal(
        _report([_facts(gates=(_gate(),), pragma_count=2)]))
    assert "that code never counts against the gate" in text
    assert "Keep the existing coverage gate as a floor" in text


def test_single_pragma_line_is_singular():
    from ptest import policy_render
    for facts in (_facts(gates=(_gate(),), pragma_count=1), _facts(pragma_count=1)):
        text = policy_render.render_terminal(_report([facts]))
        assert "1 line is marked `pragma: no cover`" in text
        assert "1 lines" not in text
        assert "pragma no cover: 1 line\n" in text + "\n"
