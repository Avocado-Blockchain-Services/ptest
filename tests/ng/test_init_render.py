"""Init render redesign: terminal-width layout, facts, compact footer.

The renderer consumes completed init/rules objects plus plain facts dicts
and never touches the filesystem.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from ptest import contracts as C
from ptest.init_render import render_init, render_init_footer

_GITHUB_URL = "https://github.com/Avocado-Blockchain-Services/ptest"


def _result(action=C.InitAction.CREATED, target=Path("/repo/.ptest.toml"),
            exists=True, warnings=(), details=()):
    return C.InitResult(action=action, target=target, exists=exists,
                        config=None, warnings=warnings, details=details)


def _detail(target, action, source):
    return C.ActionRecord(target=target, action=action, source=source)


def _note(target):
    return C.ActionRecord(target=target, action="note", source="config")


def _rules(*details):
    return SimpleNamespace(details=tuple(details), changed=True)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _api_facts(**overrides):
    facts = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": "uv sync --locked",
        "full_suite": 'your pytest config: -m "not extended_migration"',
        "full_blocked": None,
    }
    facts.update(overrides)
    return facts


def _web_facts(**overrides):
    facts = {
        "project": "web", "runner": "vitest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "inside vitest (its own workers)",
        "parallel_short": "inside vitest", "parallel_fix": None,
        "setup": "npm ci", "full_suite": None, "full_blocked": None,
    }
    facts.update(overrides)
    return facts


# --- header ---------------------------------------------------------------


def test_header_phrases_with_repo():
    cases = [
        (C.InitAction.CREATED, True, False, "ptest initialized · shop"),
        (C.InitAction.PREVIEW, False, True, "ptest init preview · shop"),
        (C.InitAction.EXISTING, True, False, "ptest already configured · shop"),
    ]
    for action, exists, dry_run, phrase in cases:
        result = _result(action=action, exists=exists)
        text = render_init(result, None, dry_run=dry_run, repo_name="shop",
                           width=80)
        assert phrase in text.splitlines()
    warnings = (C.Reason(code="scan-limit", message="m"),)
    result = _result(action=C.InitAction.EXISTING, warnings=warnings)
    text = render_init(result, None, repo_name="shop", width=80)
    assert "ptest init needs attention · shop" in text.splitlines()


def test_header_without_repo_is_phrase_alone():
    result = _result()
    text = render_init(result, None, width=80)
    assert "ptest initialized" in text.splitlines()


def test_only_wordmark_and_version_precede_header():
    result = _result()
    text = render_init(result, None, repo_name="shop", width=80)
    lines = text.splitlines()
    header_at = lines.index("ptest initialized · shop")
    assert header_at == 7  # 6 wordmark lines + ptest <version>
    assert lines[6].startswith("ptest ")
    assert "┌" not in text and "│" not in text and "┘" not in text


def test_no_fixed_box_characters():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, _rules(), width=80)
    assert "┌" not in text and "┐" not in text and "└" not in text
    assert "┤" not in text and "├" not in text


# --- golden persea shape ---------------------------------------------------


def _persea_result():
    return _result(details=(
        _detail(".ptest.toml", "already present", "config"),
        _detail("api/.ptest.toml", "already present", "config"),
        _detail("web/.ptest.toml", "already present", "config"),
    ))


def _persea_rules():
    return _rules(
        _detail("docs/ptest-agent.md", "created", "guidance"),
        _detail(".claude/skills/ptest/SKILL.md", "created", "guidance"),
        _detail(".agents/skills/ptest/SKILL.md", "created", "guidance"),
        _detail(".opencode/skills/ptest/SKILL.md", "created", "guidance"),
        _detail(".gemini/skills/ptest/SKILL.md", "created", "guidance"),
        _detail("AGENTS.md", "updated", "guidance"),
        _detail("CLAUDE.md", "updated", "guidance"),
    )


def test_golden_persea_shape_at_width_80():
    """Exact O.3 target shape at width 80 (spec requirements O.3).

    Only the wordmark and the version line precede the header. There
    are no section headings; blocks are separated by blank lines; the
    smoke row aligns with the file-action grid; the repeated guidance
    group leaves its label blank. The long guidance line wraps between
    atoms: it cannot fit 80 columns without breaking an atom.
    """
    from ptest import init_smoke

    text = render_init(_persea_result(), _persea_rules(), repo_name="shop",
                       facts=(_api_facts(), _web_facts()), width=80)
    lines = text.splitlines()
    assert lines[6] == f"ptest {C.PTEST_VERSION}"
    assert lines[7] == "ptest initialized · shop"
    body = "\n".join(lines[7:]) + "\n"
    assert body == (
        "ptest initialized · shop\n"
        "\n"
        "  api   pytest  runs: yes · parallel: 4 workers · setup: uv sync --locked\n"
        "                full suite = your pytest config: -m \"not extended_migration\"\n"
        "  web   vitest  runs: yes · parallel: inside vitest · setup: npm ci\n"
        "\n"
        "  config     unchanged  .ptest.toml, api/.ptest.toml, web/.ptest.toml\n"
        "  guidance   created    docs/ptest-agent.md, ptest skill for claude\n"
        "                        ptest skill for codex, ptest skill for opencode\n"
        "                        ptest skill for gemini\n"
        "             updated    AGENTS.md, CLAUDE.md\n"
    )
    assert "Projects" not in lines
    assert "ready with caveats" not in text
    assert "expected:" not in text
    assert "fingerprint" not in text

    smoke = (
        init_smoke.SmokeResult(project="api", status="passed",
                               command="ptest api/x.py", duration_s=2.2,
                               exit_code=None, lines=(), reason=None),
        init_smoke.SmokeResult(project="web", status="passed",
                               command="ptest web/a.test.ts", duration_s=1.8,
                               exit_code=None, lines=(), reason=None),
    )
    footer = render_init_footer(_persea_result(), _persea_rules(), smoke=smoke,
                                facts=(_api_facts(), _web_facts()), width=80)
    assert footer == (
        "  smoke      api ✓ 2.2s   web ✓ 1.8s\n"
        "\n"
        "Restart your coding agents to load the new ptest skill.\n"
    )
    assert "Next steps" not in footer
    combined = text + "\n" + footer
    assert "  smoke      api ✓ 2.2s   web ✓ 1.8s\n" in combined
    assert combined.endswith(
        "Restart your coding agents to load the new ptest skill.\n")


# --- restart line ----------------------------------------------------------


def test_no_restart_on_rerun_with_all_guidance_unchanged():
    result = _result(action=C.InitAction.EXISTING)
    rules = _rules(
        _detail("docs/ptest-agent.md", "already present", "guidance"),
        _detail(".claude/skills/ptest/SKILL.md", "already present", "guidance"),
    )
    footer = render_init_footer(result, rules, width=80)
    assert "Restart your coding agents" not in footer


def test_restart_updated_wording_when_every_skill_updated():
    result = _result()
    rules = _rules(
        _detail(".claude/skills/ptest/SKILL.md", "updated", "guidance"),
        _detail(".agents/skills/ptest/SKILL.md", "updated", "guidance"),
    )
    footer = render_init_footer(result, rules, width=80)
    assert footer.count("Restart your coding agents") == 1
    assert "updated ptest skill" in footer


def test_no_restart_for_preview():
    result = _result(action=C.InitAction.PREVIEW, exists=False)
    rules = _rules(
        _detail(".claude/skills/ptest/SKILL.md", "would create", "guidance"),
    )
    footer = render_init_footer(result, rules, dry_run=True, width=80)
    assert "Restart your coding agents" not in footer


def test_no_restart_without_rules():
    assert "Restart" not in render_init_footer(_result(), None, width=80)


# --- next steps ------------------------------------------------------------


def test_no_next_steps_when_nothing_actionable():
    footer = render_init_footer(_result(), None,
                                facts=(_api_facts(), _web_facts()), width=80)
    assert footer == ""


def test_each_next_step_kind():
    from ptest import init_smoke
    from ptest.init_smoke import SmokePlan

    facts = (
        _api_facts(runs=False, runs_reason="no tests found",
                   runs_fix="add a test file", parallel=None,
                   parallel_short=None, parallel_fix=None),
        _web_facts(parallel="no — --dist each is not supported; ptest runs "
                            "serially",
                   parallel_short="no",
                   parallel_fix="use --dist loadgroup in your pytest addopts"),
    )
    smoke = (
        init_smoke.SmokeResult(project="web", status="failed",
                               command="ptest web/a.test.ts", duration_s=None,
                               exit_code=1, lines=("boom",), reason=None),
    )
    plans = (
        SmokePlan(project="api", config=None, candidate="tests/test_a.py",
                  skip_reason=None, setup_argv=("uv", "sync")),
    )
    footer = render_init_footer(_result(), None, smoke=smoke, plans=plans,
                                facts=facts, width=80)
    assert "api  not runnable → add a test file" in footer
    assert ("web  parallel off → use --dist loadgroup in your pytest "
            "addopts") in footer
    assert ("api  setup pending → run: ptest api/tests/test_a.py "
            "(runs uv sync first)") in footer
    assert "web  smoke failed → see the runner output above" in footer


def test_setup_pending_skipped_for_passed_smoke():
    from ptest import init_smoke
    from ptest.init_smoke import SmokePlan

    smoke = (
        init_smoke.SmokeResult(project="api", status="passed",
                               command="ptest api/x.py", duration_s=1.0,
                               exit_code=None, lines=(), reason=None),
    )
    plans = (
        SmokePlan(project="api", config=None, candidate="tests/test_a.py",
                  skip_reason=None, setup_argv=("uv", "sync")),
    )
    footer = render_init_footer(_result(), None, smoke=smoke, plans=plans,
                                facts=(_api_facts(),), width=80)
    assert "setup pending" not in footer


# --- widths, atoms, color ---------------------------------------------------


def test_width_clamp_and_atom_integrity():
    facts = (_api_facts(setup="uv sync --locked --extra test --reinstall"),)
    for width in (60, 110):
        text = render_init(_result(), None, facts=facts, width=width)
        for line in text.splitlines():
            if "uv sync" in line:
                assert "uv sync --locked --extra test --reinstall" in line
        footer = render_init_footer(
            _result(), None, facts=(_api_facts(
                runs=False, runs_reason="r", runs_fix="fix it now"),),
            width=width)
        assert "api  not runnable → fix it now" in footer


def test_no_ansi_under_no_color_or_without_color_flag(monkeypatch):
    result = _result()
    plain = render_init(result, None, repo_name="shop", width=80)
    assert "\x1b" not in plain
    colored = render_init(result, None, repo_name="shop", width=80, color=True)
    assert "\x1b[1;36m" in colored
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in render_init(result, None, repo_name="shop",
                                     width=80, color=True)
    assert _GITHUB_URL not in plain


def test_init_paints_projects_and_repo_on_tty_only(monkeypatch):
    colored = render_init(_result(), None, facts=(_api_facts(),),
                          repo_name="shop", width=80, color=True)
    assert "\x1b[1mshop\x1b[0m" in colored
    assert "\x1b[1mapi" in colored

    plain = render_init(_result(), None, facts=(_api_facts(),),
                        repo_name="shop", width=80)
    assert "\x1b" not in plain

    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in render_init(
        _result(), None, facts=(_api_facts(),), repo_name="shop",
        width=80, color=True)


def test_render_init_without_facts_uses_note_projects():
    result = _result(details=(
        _note("api · pytest · ready"),
        _note(". · vitest · ready"),
    ))
    text = render_init(result, None, repo_name="shop", width=80)
    assert "ptest initialized · shop" in text.splitlines()
    assert "  api  pytest" in text
    assert "  .  vitest" in text
    assert "runs:" not in text


def test_warnings_rendered_and_generic_run_notes_dropped():
    warnings = (C.Reason(code="scan-limit", message="old keys kept"),)
    result = _result(warnings=warnings, details=(
        _note("run: ptest --full"),
        _note("api · pytest · ready"),
    ))
    text = render_init(result, None, facts=(_api_facts(),), width=80)
    assert "scan-limit: old keys kept" in text
    assert "run: ptest --full" not in text
    assert "Next steps" not in text


def test_not_runnable_project_line():
    facts = (_api_facts(runs=False, runs_reason="no tests found",
                        runs_fix="add a test file", parallel=None,
                        parallel_short=None, parallel_fix=None,
                        full_blocked='test_roots is "."',
                        full_suite=None),)
    text = render_init(_result(), None, facts=facts, width=80)
    assert "runs: no — no tests found → add a test file" in text
    assert 'full suite: not available — test_roots is "."' in text


def test_long_project_names_keep_runner_gap_and_align():
    """Project names longer than 6 chars keep a gap before the runner."""
    facts = (_api_facts(project="services/api"),
             _web_facts(project="frontend"))
    for width in (60, 110):
        text = render_init(_result(), None, facts=facts, width=width)
        lines = text.splitlines()
        api_line = next(line for line in lines
                        if "services/api" in line and "pytest" in line)
        fe_line = next(line for line in lines
                       if "frontend" in line and "vitest" in line)
        assert "services/apipytest" not in text
        assert "frontendvitest" not in text
        assert "services/api  pytest" in api_line
        assert "  vitest" in fe_line
        assert api_line.index("pytest") == fe_line.index("vitest")


def test_render_init_rejects_wrong_types():
    try:
        render_init("nope", None)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")
    try:
        render_init_footer("nope", None)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")


def test_hostile_fact_characters_never_reach_terminal_raw():
    """C1 CSI and bidi overrides from config-derived facts stay inert.

    check_facts rejects them, so the terminal never sees the raw
    characters; even a hostile-but-printable payload keeps its text
    visible without emitting control characters.
    """
    bidi = chr(0x202E)
    csi = chr(0x9B)
    facts = (_api_facts(setup="uv sync " + bidi + "KCOL" + csi + "31m"),)
    text = render_init(_result(), None, facts=facts, width=80)
    assert bidi not in text
    assert csi not in text
