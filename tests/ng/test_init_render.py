"""Pure init banner renderer contracts.

The renderer consumes completed init/rules objects only and never touches
the filesystem. Every test below must fail until src/ptest/init_render.py
exists and implements the specified banner behavior.
"""
from __future__ import annotations

from pathlib import Path

from ptest import agent_rules, contracts as C
from ptest.init_render import render_init


def _result(action=C.InitAction.CREATED, target=Path("/repo/.ptest.toml"),
            exists=True, warnings=(), details=()):
    return C.InitResult(action=action, target=target, exists=exists,
                        config=None, warnings=warnings, details=details)


def _detail(target, action, source):
    return C.ActionRecord(target=target, action=action, source=source)


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
    assert "created: .ptest.toml" in text


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


def test_repeat_init_reports_already_configured_with_unchanged_files(tmp_path):
    agent_rules.apply(tmp_path)
    unchanged = agent_rules.apply(tmp_path)
    assert unchanged.changed is False
    result = _result(action=C.InitAction.EXISTING, warnings=())
    text = render_init(result, unchanged, agents=())

    assert "ptest already configured" in text
    assert "ptest initialized\n" not in text
    assert "already present" in text


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

    assert "created" in text and "already present" in text
    assert "api/.ptest.toml" in text
    assert "web/.ptest.toml" in text
    assert "ptest api/tests/" in text


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
