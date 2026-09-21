"""Repository-local coding-agent rule installation."""
from __future__ import annotations

import pytest

from ptest.agent_rules import apply, preview
from ptest.contracts import Problem


def test_preview_is_read_only_and_lists_agent_targets(tmp_path):
    plan = preview(tmp_path)

    assert plan.actions == (
        "create docs/ptest-agent.md",
        "create AGENTS.md",
    )
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_apply_preserves_existing_agent_files_and_is_idempotent(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Existing Claude rules\n", encoding="utf-8")

    first = apply(tmp_path)
    second = apply(tmp_path)

    assert first.changed is True
    assert second.changed is False
    guide = (tmp_path / "docs" / "ptest-agent.md").read_text(encoding="utf-8")
    assert "one database per worker per run" in guide
    assert "Never use global cache flush" in guide
    assert "ptest --full" in guide
    assert "monorepo root" in guide
    assert "ptest api/" in guide
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Existing rules\n")
    assert agents.count("ptest-agent-rules:start") == 1
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith("# Existing Claude rules\n")
    assert claude.count("@docs/ptest-agent.md") == 1


def test_apply_rejects_agent_symlink_without_creating_any_rules(tmp_path):
    outside = tmp_path.parent / "outside-agent-rules.md"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to(outside)

    with pytest.raises(Problem, match="symlink"):
        apply(tmp_path)

    assert not (tmp_path / "docs").exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_apply_supports_only_the_in_repo_agents_to_claude_alias(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Shared rules\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")

    result = apply(tmp_path)

    assert result.changed is True
    assert (tmp_path / "AGENTS.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").count(
        "ptest-agent-rules:start") == 1


def test_apply_rejects_malformed_managed_block_without_touching_existing_text(tmp_path):
    target = tmp_path / "AGENTS.md"
    original = "# Existing\n<!-- ptest-agent-rules:start -->\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(Problem, match="malformed"):
        apply(tmp_path)

    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / "docs").exists()
