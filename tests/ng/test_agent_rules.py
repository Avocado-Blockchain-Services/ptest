"""Repository-local coding-agent rule installation."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from ptest.agent_rules import _legacy_provider_text, apply, preview
from ptest.contracts import Problem

FAST_FORWARD_GATE_RULE = (
    "If a merge is fast-forward and the exact tip commit already passed the "
    "required ptest gate, do not rerun ptest solely because of the merge. A "
    "merge commit, new changes, or an untested tip still requires the "
    "applicable ptest gate."
)


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
    assert "repository root" in guide
    assert "ptest api/" in guide
    assert "-n 0" in guide
    assert "vitest run" in guide
    assert FAST_FORWARD_GATE_RULE not in guide
    assert "graphify" not in guide
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

    assert "create .agents/skills/ptest/SKILL.md" in plan.actions
    assert "create .opencode/skills/ptest/SKILL.md" in plan.actions
    assert not (tmp_path / ".agents").exists()

    result = apply(tmp_path, agents=("codex", "opencode"))
    repeat = apply(tmp_path, agents=("codex", "opencode"))

    assert result.changed is True
    assert repeat.changed is False
    assert "docs/ptest-agent.md" in (
        tmp_path / ".agents/skills/ptest/SKILL.md").read_text()
    assert not (tmp_path.parent / ".agents").exists()


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
    flat = " ".join(guide.split())
    assert "assessment authority only" in guide
    assert "separate user instruction" in guide
    assert FAST_FORWARD_GATE_RULE not in guide
    assert "graphify" not in guide
    assert "Run `ptest --full` once after the integrated change" in guide
    assert "one cheap-model call per checklist item that needs one" in flat
    assert "timing, selection and parallel execution" in flat
    assert "items skip the model" in flat
    assert "`ptest doctor --offline` is static" in guide
    assert len(guide.splitlines()) <= 45


def test_local_repair_guide_has_no_repo_internal_workflow():
    from importlib.resources import files

    guide = files("ptest").joinpath(
        "resources", "agent-guide.md").read_text(encoding="utf-8")
    assert FAST_FORWARD_GATE_RULE not in guide
    assert "graphify" not in guide
    assert "run the scoped `ptest` command" in guide
    assert "run one `ptest --full` final gate" in guide


def test_provider_skill_unsafe_parent_is_rejected_before_any_rules_write(tmp_path):
    provider = tmp_path / ".agents"
    provider.mkdir(mode=0o777)
    provider.chmod(0o777)

    with pytest.raises(Problem, match="unsafe"):
        apply(tmp_path, agents=("codex",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_skill_targets_use_canonical_paths_with_front_matter(tmp_path, monkeypatch):
    from ptest.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(("init", "--runner", "pytest", "--agents", "codex,claude")) == 0
    for directory in (".agents", ".claude"):
        text = (tmp_path / directory / "skills/ptest/SKILL.md").read_text()
        assert text.startswith("---\nname: ptest\n")
        assert "description:" in text.split("---", 2)[1]
    assert not (tmp_path / ".codex/skills/ptest/SKILL.md").exists()


def test_every_generated_skill_has_valid_front_matter_and_root_paths(tmp_path):
    result = apply(tmp_path, agents=("claude", "codex", "opencode", "gemini"))

    assert result.changed is True
    seen = set()
    for relative in (".claude/skills/ptest/SKILL.md",
                     ".agents/skills/ptest/SKILL.md",
                     ".opencode/skills/ptest/SKILL.md",
                     ".gemini/skills/ptest/SKILL.md"):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        assert "name: ptest" in head
        assert "description:" in head
        description = next(line for line in head.splitlines()
                           if line.startswith("description:"))
        assert len(description.split(":", 1)[1].strip()) > 0
        assert "docs/ptest-agent.md" in text
        assert "run tests only through `ptest` from the repository root" in text.lower()
        seen.add(text)
    assert len(seen) >= 2


def test_exact_released_template_upgrades_to_front_matter_format(tmp_path):
    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    legacy = _legacy_provider_text("claude")
    assert b"---" not in legacy
    (target / "SKILL.md").write_bytes(legacy)

    result = apply(tmp_path, agents=("claude",))

    assert result.changed is True
    text = (target / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: ptest\n")
    assert "docs/ptest-agent.md" in text


def test_edited_skill_conflicts_before_any_rules_write(tmp_path):
    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# custom skill\n", encoding="utf-8")

    with pytest.raises(Problem, match="already exists"):
        apply(tmp_path, agents=("claude",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# custom skill\n"


def test_legacy_codex_template_is_preserved_with_migration_note(tmp_path):
    legacy_dir = tmp_path / ".codex" / "skills" / "ptest"
    legacy_dir.mkdir(parents=True)
    legacy_bytes = _legacy_provider_text("codex")
    (legacy_dir / "SKILL.md").write_bytes(legacy_bytes)

    result = apply(tmp_path, agents=("codex",))

    assert result.changed is True
    assert (legacy_dir / "SKILL.md").read_bytes() == legacy_bytes
    canonical = (tmp_path / ".agents" / "skills" / "ptest" / "SKILL.md")
    assert canonical.read_text(encoding="utf-8").startswith("---\nname: ptest\n")
    assert any(".codex/skills/ptest/SKILL.md" in action
               for action in result.actions)


def test_legacy_codex_user_content_aborts_before_any_write(tmp_path):
    legacy_dir = tmp_path / ".codex" / "skills" / "ptest"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "SKILL.md").write_text("user-owned codex skill\n", encoding="utf-8")

    with pytest.raises(Problem, match="already exists"):
        apply(tmp_path, agents=("codex",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".agents").exists()
    assert (legacy_dir / "SKILL.md").read_text() == "user-owned codex skill\n"


def test_legacy_codex_symlink_aborts_before_any_write(tmp_path):
    outside = tmp_path.parent / "outside-codex-skill.md"
    outside.write_text("outside\n", encoding="utf-8")
    legacy_dir = tmp_path / ".codex" / "skills" / "ptest"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "SKILL.md").symlink_to(outside)

    with pytest.raises(Problem, match="unsafe"):
        apply(tmp_path, agents=("codex",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_mid_write_failure_rolls_back_owned_guidance(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    real_create = rules_module._create_leaf
    calls = []

    def failing_create(parent_fd, name, data, **kwargs):
        calls.append(name)
        if len(calls) >= 3:
            raise Problem(code="state-unavailable", message="injected write failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_create_leaf", failing_create)

    with pytest.raises(Problem, match="injected write failure"):
        apply(tmp_path, agents=("claude",))

    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".claude").exists()


def test_rollback_never_overwrites_a_concurrent_edit(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
    real_create = rules_module._create_leaf

    def clobbering_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            # A concurrent editor lands between our recorded AGENTS.md
            # update and this failure; rollback must leave it alone.
            (tmp_path / "AGENTS.md").write_text(
                "concurrent editor content\n", encoding="utf-8")
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_create_leaf", clobbering_create)

    with pytest.raises(Problem, match="restor"):
        apply(tmp_path, agents=("claude",))

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "concurrent editor content\n"
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / ".claude").exists()


def test_rollback_leaves_same_byte_created_file_replacement(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    real_create = rules_module._create_leaf

    def swapping_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            # A concurrent process replaces the created guide with a new
            # file holding the identical public bytes; rollback must leave
            # the replacement alone instead of deleting it.
            guide = tmp_path / "docs" / "ptest-agent.md"
            payload = guide.read_bytes()
            guide.unlink()
            guide.write_bytes(payload)
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_create_leaf", swapping_create)

    with pytest.raises(Problem, match="restor"):
        apply(tmp_path, agents=("claude",))

    guide = tmp_path / "docs" / "ptest-agent.md"
    assert guide.is_file()
    assert b"ptest --full" in guide.read_bytes()


def test_rollback_leaves_same_byte_updated_file_replacement(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
    real_create = rules_module._create_leaf

    def swapping_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            # Same-byte replacement of the updated file under a new inode;
            # rollback must not restore over it.
            target = tmp_path / "AGENTS.md"
            payload = target.read_bytes()
            target.unlink()
            target.write_bytes(payload)
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_create_leaf", swapping_create)

    with pytest.raises(Problem, match="restor"):
        apply(tmp_path, agents=("claude",))

    content = (tmp_path / "AGENTS.md").read_bytes()
    assert b"ptest-agent-rules:start" in content
    assert b"# Existing rules" in content


def test_rollback_leaves_raced_concurrent_directory(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    real_mkdir = os.mkdir
    real_create = rules_module._create_leaf

    def racing_mkdir(path, *args, **kwargs):
        # A concurrent creator wins the race: the directory already exists
        # when our own mkdir runs, so it is foreign and must survive.
        try:
            real_mkdir(path, *args, **kwargs)
        except FileExistsError:
            pass
        return real_mkdir(path, *args, **kwargs)

    def failing_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    monkeypatch.setattr(rules_module, "_create_leaf", failing_create)

    with pytest.raises(Problem):
        apply(tmp_path, agents=("claude",))

    assert (tmp_path / "docs").is_dir()
    assert (tmp_path / ".claude").is_dir()


def test_ancestor_substitution_cannot_redirect_replace(tmp_path, monkeypatch):
    from ptest import agent_rules

    chain = tmp_path / ".agents" / "skills" / "ptest"
    chain.mkdir(parents=True)
    leaf = chain / "SKILL.md"
    leaf.write_text("original\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-ancestor-target"
    outside.mkdir(exist_ok=True)
    real_open = os.open
    swapped: list[bool] = []

    def sneaky_open(path, flags, *args, **kwargs):
        text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else None
        if (not swapped and (flags & os.O_DIRECTORY)
                and (text == str(chain) or text == ".agents")):
            shutil.rmtree(tmp_path / ".agents")
            (tmp_path / ".agents").symlink_to(outside)
            swapped.append(True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", sneaky_open)

    with pytest.raises(Problem, match="unsafe"):
        agent_rules._replace(tmp_path, leaf, "new content\n", mode=0o644)

    assert swapped == [True]
    assert not (outside / "SKILL.md").exists()
    assert not any(outside.iterdir())


def test_symlink_before_chmod_does_not_change_external_mode(tmp_path, monkeypatch):
    secret = tmp_path.parent / "outside-mode-target"
    secret.write_text("secret\n", encoding="utf-8")
    secret.chmod(0o644)
    target = tmp_path / "AGENTS.md"
    target.write_text("# Existing rules\n", encoding="utf-8")
    target.chmod(0o600)
    real_chmod = os.chmod
    swapped: list[bool] = []

    def sneaky_chmod(path, mode, *args, **kwargs):
        if not swapped and Path(path).name == "AGENTS.md":
            target.unlink()
            target.symlink_to(secret)
            swapped.append(True)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", sneaky_chmod)

    result = apply(tmp_path)

    assert result.changed is True
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644
    assert secret.read_text(encoding="utf-8") == "secret\n"


def test_fchmod_failure_during_update_leaves_original_intact(tmp_path, monkeypatch):
    import errno

    target = tmp_path / "AGENTS.md"
    original = "# Existing rules\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o600)
    real_fchmod = os.fchmod
    failed: list[bool] = []

    def failing_fchmod(fd, mode, *args, **kwargs):
        if not failed and not stat.S_ISDIR(os.fstat(fd).st_mode):
            failed.append(True)
            raise OSError(errno.EIO, "injected fchmod failure")
        return real_fchmod(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fchmod", failing_fchmod)

    with pytest.raises(OSError, match="injected fchmod failure"):
        apply(tmp_path)

    assert failed == [True]
    assert target.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (tmp_path / "docs").exists()


def test_rollback_leaves_file_swapped_between_read_and_replace(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
    real_current = rules_module._current_file
    real_create = rules_module._create_leaf

    def swapping_current(root, parts):
        current, identity = real_current(root, parts)
        if parts[-1] == "AGENTS.md":
            # A concurrent replacement lands after the ownership read but
            # before rollback publishes the restoration; it must survive.
            target = tmp_path / "AGENTS.md"
            target.unlink()
            target.write_bytes(b"attacker rewrite\n")
        return current, identity

    def failing_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_current_file", swapping_current)
    monkeypatch.setattr(rules_module, "_create_leaf", failing_create)

    with pytest.raises(Problem, match="restor"):
        apply(tmp_path, agents=("claude",))

    assert (tmp_path / "AGENTS.md").read_bytes() == b"attacker rewrite\n"
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / ".claude").exists()


def test_fsync_failure_during_update_leaves_original_intact(tmp_path, monkeypatch):
    import errno

    import ptest.agent_rules as rules_module

    target = tmp_path / "AGENTS.md"
    original = "# Existing rules\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o600)
    guide = tmp_path / "docs" / "ptest-agent.md"
    guide.parent.mkdir(parents=True)
    guide.write_bytes(rules_module._guide())
    real_fsync = os.fsync
    failed: list[bool] = []

    def failing_fsync(fd, *args, **kwargs):
        if not failed and not stat.S_ISDIR(os.fstat(fd).st_mode):
            failed.append(True)
            raise OSError(errno.EIO, "injected fsync failure")
        return real_fsync(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        apply(tmp_path)

    assert failed == [True]
    assert target.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    leftovers = [name for name in os.listdir(tmp_path)
                 if name.startswith(".AGENTS.md.ptest-")]
    assert leftovers == []


def test_rollback_leaves_same_byte_file_swapped_between_read_and_replace(
        tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
    real_current = rules_module._current_file
    real_create = rules_module._create_leaf

    def swapping_current(root, parts):
        current, identity = real_current(root, parts)
        if parts[-1] == "AGENTS.md":
            # A concurrent replacement lands after the ownership read but
            # before rollback publishes the restoration, holding the
            # identical bytes under a new inode; it must survive.
            target = tmp_path / "AGENTS.md"
            payload = target.read_bytes()
            target.unlink()
            target.write_bytes(payload)
        return current, identity

    def failing_create(parent_fd, name, data, **kwargs):
        if name == "SKILL.md":
            raise Problem(code="state-unavailable", message="injected late failure",
                          phase="agent-rules")
        return real_create(parent_fd, name, data, **kwargs)

    monkeypatch.setattr(rules_module, "_current_file", swapping_current)
    monkeypatch.setattr(rules_module, "_create_leaf", failing_create)

    with pytest.raises(Problem, match="restor"):
        apply(tmp_path, agents=("claude",))

    content = (tmp_path / "AGENTS.md").read_bytes()
    assert b"ptest-agent-rules:start" in content
    assert b"# Existing rules" in content
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / ".claude").exists()


def test_unreadable_legacy_codex_artifact_is_not_silently_missing(tmp_path, monkeypatch):
    import ptest.files as files_module

    legacy = tmp_path / ".codex" / "skills" / "ptest"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_bytes(_legacy_provider_text("codex"))
    real_read = files_module.read_regular

    def denied_read(root, relative, limit):
        if relative == ".codex/skills/ptest/SKILL.md":
            raise Problem(code="state-unavailable", message="injected denial",
                          phase="files")
        return real_read(root, relative, limit)

    monkeypatch.setattr(files_module, "read_regular", denied_read)

    with pytest.raises(Problem, match="denial"):
        preview(tmp_path, agents=("codex",))


def test_generated_skill_is_a_short_pointer_without_duplicated_guidance(tmp_path):
    result = apply(tmp_path, agents=("claude", "codex", "opencode", "gemini"))

    assert result.changed is True
    for relative in (".claude/skills/ptest/SKILL.md",
                     ".agents/skills/ptest/SKILL.md",
                     ".opencode/skills/ptest/SKILL.md",
                     ".gemini/skills/ptest/SKILL.md"):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        assert text.startswith("---\nname: ptest\n")
        head = text.split("---", 2)[1]
        assert "description:" in head
        body = text.split("---", 2)[2].strip("\n").splitlines()
        assert len(body) <= 4
        assert "docs/ptest-agent.md" in text
        assert "run tests only through `ptest` from the repository root" in text.lower()
        # Merge/graphify guidance lives only in the guide, never in skills.
        assert "graphify" not in text
        assert "fast-forward" not in text
        assert "ptest --full" not in text


def test_previous_managed_skill_upgrades_in_place(tmp_path):
    from ptest.agent_rules import _previous_provider_text, _provider_text

    target = tmp_path / ".claude" / "skills" / "ptest"
    target.mkdir(parents=True)
    previous = _previous_provider_text("claude")
    assert b"graphify" in previous
    (target / "SKILL.md").write_bytes(previous)

    plan = preview(tmp_path, agents=("claude",))
    assert "update .claude/skills/ptest/SKILL.md" in plan.actions

    result = apply(tmp_path, agents=("claude",))

    assert result.changed is True
    assert (target / "SKILL.md").read_bytes() == _provider_text("claude")
    assert ("updated", ".claude/skills/ptest/SKILL.md") in [
        (item.action, item.target) for item in result.details]
    repeat = apply(tmp_path, agents=("claude",))
    assert repeat.changed is False


def test_previous_managed_guide_upgrades_in_place(tmp_path, monkeypatch):
    import hashlib

    import ptest.agent_rules as rules_module

    sentinel = b"# old managed guide\n"
    monkeypatch.setattr(rules_module, "_PREVIOUS_GUIDE_SHA256S",
                        frozenset({hashlib.sha256(sentinel).hexdigest()}))
    guide_dir = tmp_path / "docs"
    guide_dir.mkdir()
    (guide_dir / "ptest-agent.md").write_bytes(sentinel)

    plan = preview(tmp_path)
    assert "update docs/ptest-agent.md" in plan.actions

    result = apply(tmp_path)

    assert result.changed is True
    assert (guide_dir / "ptest-agent.md").read_bytes() == rules_module._guide()
    assert ("updated", "docs/ptest-agent.md") in [
        (item.action, item.target) for item in result.details]
    repeat = apply(tmp_path)
    assert repeat.changed is False


def test_previous_hashes_cover_main_pre_change_guide():
    """The guide as shipped on main before the G1 cleanup must upgrade.

    ``0b2ea2...`` is the sha256 of ``main:src/ptest/resources/
    repository-agent-guide.md`` (byte-identical to what init writes);
    ``72f2a5...`` is the older base-commit guide. The current guide
    itself must never classify as previous.
    """
    import hashlib

    import ptest.agent_rules as rules_module

    assert "0b2ea261830578734a9f724e134a1207c651baa160d60b05fe0f025438dd96c6" in rules_module._PREVIOUS_GUIDE_SHA256S
    assert "72f2a5bbfcafc9b74cc2d1a7e621fe6784f67f701315d0503eef06e866989e68" in rules_module._PREVIOUS_GUIDE_SHA256S
    current = rules_module._guide()
    assert hashlib.sha256(current).hexdigest() not in rules_module._PREVIOUS_GUIDE_SHA256S
    assert rules_module._guide_kind(current.decode("utf-8"), current) == "current"


def test_init_upgrades_old_guide_in_place(tmp_path, monkeypatch):
    """Twin: `ptest init` upgrades a previous-managed guide in place.

    The user-edited half of the twin is ``test_user_edited_guide_still_conflicts_before_any_write``.
    """
    import hashlib

    from ptest.cli import main
    import ptest.agent_rules as rules_module

    old = b"# old managed guide\n"
    monkeypatch.setattr(rules_module, "_PREVIOUS_GUIDE_SHA256S",
                        frozenset({hashlib.sha256(old).hexdigest()}))
    monkeypatch.chdir(tmp_path)
    guide_dir = tmp_path / "docs"
    guide_dir.mkdir()
    (guide_dir / "ptest-agent.md").write_bytes(old)

    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    assert (guide_dir / "ptest-agent.md").read_bytes() == rules_module._guide()


def test_user_edited_guide_still_conflicts_before_any_write(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ptest-agent.md").write_text(
        "# user guide\n", encoding="utf-8")

    with pytest.raises(Problem, match="already exists"):
        apply(tmp_path)

    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "docs" / "ptest-agent.md").read_text(
        encoding="utf-8") == "# user guide\n"


def test_replace_rejects_in_place_edit_before_publish(tmp_path, monkeypatch):
    import ptest.agent_rules as rules_module

    target = tmp_path / "AGENTS.md"
    original = b"# Existing rules\n"
    edited = b"# Edited inplace\n"
    assert len(edited) == len(original)
    target.write_bytes(original)
    before = (target.stat().st_dev, target.stat().st_ino)
    real_fsync = os.fsync
    injected: list[bool] = []

    def sneaky_fsync(fd, *args, **kwargs):
        try:
            stamp = os.fstat(fd)
        except OSError:
            return real_fsync(fd, *args, **kwargs)
        if not injected and stat.S_ISREG(stamp.st_mode):
            injected.append(True)
            writer = os.open(target, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                os.write(writer, edited)
                real_fsync(writer)
            finally:
                os.close(writer)
        return real_fsync(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", sneaky_fsync)

    with pytest.raises(Problem, match="changed during apply"):
        rules_module._replace(tmp_path, target, "new content\n", mode=0o644,
                              expect_bytes=original)

    assert injected == [True]
    assert target.read_bytes() == edited
    assert (target.stat().st_dev, target.stat().st_ino) == before
    leftovers = [name for name in os.listdir(tmp_path)
                 if name.startswith(".AGENTS.md.ptest-")]
    assert leftovers == []
