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


def test_provider_skill_preview_and_apply_are_repository_local_and_idempotent(tmp_path):
    plan = preview(tmp_path, agents=("codex", "opencode"))

    assert "create .codex/skills/ptest/SKILL.md" in plan.actions
    assert "create .opencode/skills/ptest/SKILL.md" in plan.actions
    assert not (tmp_path / ".codex").exists()

    result = apply(tmp_path, agents=("codex", "opencode"))
    repeat = apply(tmp_path, agents=("codex", "opencode"))

    assert result.changed is True
    assert repeat.changed is False
    assert "docs/ptest-agent.md" in (
        tmp_path / ".codex/skills/ptest/SKILL.md").read_text()
    assert not (tmp_path.parent / ".codex").exists()


def test_provider_skill_conflict_is_rejected_before_any_rules_write(tmp_path):
    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user skill\n", encoding="utf-8")

    with pytest.raises(Problem, match="already exists"):
        apply(tmp_path, agents=("claude",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_repository_guide_states_assessment_only_authority():
    """Repair authority must never be inferred from a doctor or prompt request."""
    from importlib.resources import files

    guide = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert "assessment authority only" in guide
    assert "separate user instruction" in guide


def test_provider_skill_unsafe_parent_is_rejected_before_any_rules_write(tmp_path):
    provider = tmp_path / ".codex"
    provider.mkdir(mode=0o777)
    provider.chmod(0o777)

    with pytest.raises(Problem, match="unsafe"):
        apply(tmp_path, agents=("codex",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
