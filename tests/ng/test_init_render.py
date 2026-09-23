"""Init render v2: per-project status and verified next steps.

The renderer consumes completed init/rules objects only and never touches
the filesystem. Project status arrives as executability notes in
``InitResult.details`` (``action="note"``, ``source="config"``):

- project note: ``"<project> · <runner> · <verdict>"``
- run note: ``"run: <verified command>"``
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from ptest import agent_rules, contracts as C
from ptest.init_render import render_init

_WORDMARK_LINES = (
    "██████╗ ████████╗███████╗███████╗████████╗",
    "██╔══██╗╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝",
    "██████╔╝   ██║   █████╗  ███████╗   ██║",
    "██╔═══╝    ██║   ██╔══╝  ╚════██║   ██║",
    "██║        ██║   ███████╗███████║   ██║",
    "╚═╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝",
)
_GITHUB_URL = "https://github.com/Avocado-Blockchain-Services/ptest"


def _dwidth(text: str) -> int:
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _result(action=C.InitAction.CREATED, target=Path("/repo/.ptest.toml"),
            exists=True, warnings=(), details=()):
    return C.InitResult(action=action, target=target, exists=exists,
                        config=None, warnings=warnings, details=details)


def _detail(target, action, source):
    return C.ActionRecord(target=target, action=action, source=source)


def _note(target):
    return C.ActionRecord(target=target, action="note", source="config")


def _cell(line: str) -> str:
    cell = line.strip()
    if cell.startswith("│"):
        cell = cell[1:]
    if cell.endswith("│"):
        cell = cell[:-1]
    return cell


def _root_config_lines(text: str):
    # The stable contract is the cell content; the box borders are stripped
    # before matching, mirroring how a terminal reader sees the row.
    found = []
    for line in text.splitlines():
        match = re.match(
            r"\s*(created|updated|unchanged|would create)\s+\.ptest\.toml\s*$",
            _cell(line))
        if match:
            found.append(match.group(1))
    return found


def test_created_banner_has_box_wordmark_and_ordered_sections(tmp_path):
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
    ))
    rules = agent_rules.apply(tmp_path)
    text = render_init(result, rules, agents=())

    assert "ptest initialized" in text
    assert "┌" in text and "┘" in text
    config_at = text.index("Configuration")
    guidance_at = text.index("Guidance")
    next_at = text.index("Next steps")
    assert config_at < guidance_at < next_at
    assert _root_config_lines(text) == ["created"]
    assert ": .ptest.toml" not in text


def test_preview_header_uses_would_verbs_and_never_created(tmp_path):
    result = _result(action=C.InitAction.PREVIEW,
                     target=Path("/repo/.ptest.toml"), exists=False,
                     details=(_detail(".ptest.toml", "would create", "config"),))
    plan = agent_rules.preview(tmp_path)
    text = render_init(result, plan, dry_run=True, agents=())

    assert "ptest init preview" in text
    assert "would create" in text
    assert "created:" not in text
    assert "updated" not in text


def test_repeat_init_reports_unchanged_config_and_guidance_paths(tmp_path):
    agent_rules.apply(tmp_path)
    unchanged = agent_rules.apply(tmp_path)
    assert unchanged.changed is False
    result = _result(action=C.InitAction.EXISTING, warnings=())
    text = render_init(result, unchanged, agents=())

    assert "ptest already configured" in text
    assert "ptest initialized\n" not in text
    assert _root_config_lines(text) == ["unchanged"]
    assert "already present" not in text
    assert re.search(r"unchanged +docs/ptest-agent\.md", text)
    assert re.search(r"unchanged +AGENTS\.md", text)


def test_existing_standalone_with_empty_details_synthesizes_root_once():
    result = _result(action=C.InitAction.EXISTING, warnings=())
    text = render_init(result, None, agents=())

    assert _root_config_lines(text) == ["unchanged"]


def test_invalid_existing_config_renders_attention_header(tmp_path):
    warnings = (C.Reason(code="invalid-config",
                         message="existing project configuration could not be used",
                         paths=()),)
    result = _result(action=C.InitAction.EXISTING, warnings=warnings)
    text = render_init(result, None, agents=())

    assert "attention" in text.lower()
    assert "ptest initialized" not in text
    assert "ptest already configured" not in text
    assert "invalid-config" in text


def test_renderer_never_reads_files_and_bounds_hostile_values():
    evil = "bad\x1b[2J\r\n\x00net" + "界" * 2000
    warnings = (C.Reason(code="invalid-config", message=evil, paths=()),)
    result = _result(action=C.InitAction.EXISTING, warnings=warnings,
                     details=(_detail(evil, "created", "config"),))
    text = render_init(result, None, agents=("codex",))

    assert "\x1b" not in text and "\r" not in text and "\x00" not in text
    assert "[truncated]" in text
    assert len(text.encode("utf-8")) < 32768


def test_renderer_reports_exact_per_child_config_actions():
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _detail("api/.ptest.toml", "created", "config"),
        _detail("web/.ptest.toml", "already present", "config"),
    ))
    text = render_init(result, None, agents=())

    assert "created" in text and "unchanged" in text
    assert "already present" not in text
    assert "api/.ptest.toml" in text
    assert re.search(r"unchanged +web/\.ptest\.toml", text)
    # No invented next steps: without executability notes there are no
    # verified commands, so neither a child scope nor the full gate appears.
    assert "ptest api/tests/" not in text
    assert "ptest --full" not in text


def test_projects_section_renders_notes_in_section_order():
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _detail("api/.ptest.toml", "created", "config"),
        _note("api · pytest · ready"),
        _note("run: ptest api/tests/test_api.py"),
    ))
    text = render_init(result, None, agents=())

    config_at = text.index("Configuration")
    projects_at = text.index("Projects")
    next_at = text.index("Next steps")
    assert config_at < projects_at < next_at
    assert "api" in text and "pytest" in text and "ready" in text
    assert "ptest api/tests/test_api.py" in text


def test_next_steps_lists_verified_commands_and_fixes_only():
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _detail("api/.ptest.toml", "created", "config"),
        _note("api · pytest · not runnable: pytest addopts enable xdist, "
              "which ptest runs serially — fix: add \"-n\", \"0\" to [runner] "
              "args in api/.ptest.toml"),
        _note("web · vitest · ready with caveats: exclusive: Vitest runs as "
              "one command and manages its own workers"),
        _note("run: ptest web/src/a.test.ts"),
    ))
    text = render_init(result, None, agents=())

    assert "ptest web/src/a.test.ts" in text
    assert "fix api:" in text
    assert "add \"-n\", \"0\" to [runner] args in api/.ptest.toml" in text
    # The generic full-gate line appears only with a verified run note.
    assert "run the integrated gate" not in text


def test_next_steps_shows_full_gate_only_with_verified_run_note():
    without = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _note(". · pytest · ready"),
    ))
    assert "ptest --full" not in render_init(without, None, agents=())

    with_full = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _note(". · pytest · ready"),
        _note("run: ptest --full"),
    ))
    assert "ptest --full" in render_init(with_full, None, agents=())


def test_other_config_notes_render_unchanged():
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _note("declared child 'web' has no configuration"),
    ))
    text = render_init(result, None, agents=())

    assert "declared child" in text
    assert "Projects" not in text


def test_hostile_project_note_is_escaped_and_bounded():
    evil = "bad\x1b[2J\r\n\x00\x07\u202e" + "界" * 2000
    result = _result(details=(
        _detail(".ptest.toml", "created", "config"),
        _note(f"{evil} · pytest · ready"),
        _note("run: ptest tests/test_x.py"),
    ))
    text = render_init(result, None, agents=())

    assert "\x1b" not in text and "\r" not in text and "\x00" not in text
    assert "\u202e" not in text
    assert "[truncated]" in text
    # The box geometry is unchanged: every drawn row is 64 chars wide.
    for line in text.splitlines():
        stripped = _strip_ansi(line)
        if stripped and stripped[0] in "┌├└│":
            assert len(stripped) == 64
    assert len(text.encode("utf-8")) < 32768


def test_renderer_never_claims_unselected_providers(tmp_path):
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    rules = agent_rules.apply(tmp_path)
    text = render_init(result, rules, agents=())

    assert ".claude" not in text
    assert ".agents" not in text


def test_codex_hint_names_restart_or_skills_command():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=("codex",))

    assert "/skills" in text or "restart" in text.lower()


def test_plain_output_shows_wordmark_version_repo_and_url():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name="my-repo")

    for line in _WORDMARK_LINES:
        assert line in text
    assert f"ptest {C.PTEST_VERSION}" in text
    assert "my-repo" in text
    assert _GITHUB_URL in text
    assert "\x1b" not in text


def test_wordmark_precedes_version_repo_url_and_box():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name="my-repo")

    wordmark_at = text.index(_WORDMARK_LINES[0])
    version_at = text.index(f"ptest {C.PTEST_VERSION}")
    repo_at = text.index("my-repo")
    url_at = text.index(_GITHUB_URL)
    box_at = text.index("┌")
    assert wordmark_at < version_at < repo_at < url_at < box_at
    assert text.startswith(_WORDMARK_LINES[0])


def test_default_output_has_no_ansi_and_omits_empty_repo():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None)

    assert "\x1b" not in text
    assert f"ptest {C.PTEST_VERSION}" in text
    assert _GITHUB_URL in text
    assert "None" not in text


def test_no_color_suppresses_explicit_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name="my-repo", color=True)

    assert "\x1b" not in text
    for line in _WORDMARK_LINES:
        assert line in text


def test_explicit_color_keeps_identical_glyphs(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    colored = render_init(result, None, agents=(), repo_name="my-repo", color=True)
    plain = render_init(result, None, agents=(), repo_name="my-repo", color=False)

    assert "\x1b[" in colored
    assert "\x1b" not in plain
    assert _strip_ansi(colored) == plain


def test_hostile_repo_name_cannot_alter_terminal():
    evil = "bad\x1b[2J\r\n\x00\u202e" + "界" * 2000
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name=evil)

    assert "\x1b" not in text
    assert "\r" not in text
    assert "\x00" not in text
    assert "\u202e" not in text
    repo_lines = [line for line in text.splitlines() if "bad" in line]
    assert len(repo_lines) == 1
    assert _dwidth(repo_lines[0]) <= 80
    assert len(text.encode("utf-8")) < 32768


def test_long_repo_name_bounded_to_80_columns():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name="x" * 500)

    candidates = [line for line in text.splitlines() if "x" * 10 in line]
    assert len(candidates) == 1
    assert _dwidth(candidates[0]) <= 80


def test_no_row_or_box_overflow():
    result = _result(details=(_detail(".ptest.toml", "created", "config"),))
    text = render_init(result, None, agents=(), repo_name="my-repo")

    for line in text.splitlines():
        stripped = _strip_ansi(line)
        if stripped and stripped[0] in "┌├└│":
            assert _dwidth(stripped) == 64
    for line in _WORDMARK_LINES:
        assert _dwidth(line) <= 64
    for line in text.splitlines():
        assert _dwidth(_strip_ansi(line)) <= 80


def test_preview_and_existing_layout_preserved_with_banner(tmp_path):
    preview = _result(action=C.InitAction.PREVIEW,
                      target=Path("/repo/.ptest.toml"), exists=False,
                      details=(_detail(".ptest.toml", "would create", "config"),))
    plan = agent_rules.preview(tmp_path)
    preview_text = render_init(preview, plan, dry_run=True, agents=(),
                               repo_name="my-repo")

    assert _WORDMARK_LINES[0] in preview_text
    assert "ptest init preview" in preview_text
    assert "would create" in preview_text
    assert "created:" not in preview_text

    agent_rules.apply(tmp_path)
    unchanged = agent_rules.apply(tmp_path)
    existing = _result(action=C.InitAction.EXISTING, warnings=())
    existing_text = render_init(existing, unchanged, agents=(),
                                repo_name="my-repo")

    assert _WORDMARK_LINES[0] in existing_text
    assert "ptest already configured" in existing_text
    assert _root_config_lines(existing_text) == ["unchanged"]
    assert "already present" not in existing_text
    assert re.search(r"unchanged +docs/ptest-agent\.md", existing_text)
