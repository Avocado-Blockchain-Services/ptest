"""Opt-in test policy: install, consent, dry-run, conflicts, rollback, uninstall.

Covers the ``test_policy`` flag on ``agent_rules.preview``/``apply``, the
``ptest init --test-policy`` consent prompt, the ``ptest rules --test-policy``
flows, the ``ptest guide TOPIC`` grammar, and uninstall of both block
variants. Abuse twins (symlinked policy target, non-regular policy target,
oversized instruction file, user-edited policy copy, hand-edited block)
live here too.
"""
from __future__ import annotations

import sys

import pytest

from ptest import agent_rules
from ptest import contracts as C
from ptest.cli import main, parse_argv

POLICY_REL = "docs/ptest-test-policy.md"


def _answers(monkeypatch, *replies):
    """Feed ``input()`` replies, declining anything unexpected."""
    iterator = iter(replies)

    def fake_input(*args, **kwargs):
        return next(iterator, "")

    monkeypatch.setattr("builtins.input", fake_input)


def _tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)


def _init(*extra):
    return ("init", "--runner", "pytest", "--agents", "all",
            "--no-smoke", "--no-doctor", *extra)


def test_policy_resource_matches_design_without_digits():
    from importlib.resources import files

    text = files("ptest").joinpath(
        "resources", "test-policy.md").read_text(encoding="utf-8")
    assert text.startswith("# ptest test policy\n")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert not any(char.isdigit() for char in text)
    assert "`ptest guide tests`" in text
    assert "floor, not a target" in text
    assert agent_rules._test_policy() == text.encode("utf-8")


def test_previous_test_policy_hash_set_starts_empty():
    assert agent_rules._PREVIOUS_TEST_POLICY_SHA256S == frozenset()


def test_base_block_is_byte_identical_to_today():
    assert agent_rules._block("AGENTS.md") == (
        "<!-- ptest-agent-rules:start -->\n"
        "Before running or changing tests, read `docs/ptest-agent.md`.\n"
        "<!-- ptest-agent-rules:end -->\n"
    )
    assert agent_rules._block("CLAUDE.md") == (
        "<!-- ptest-agent-rules:start -->\n"
        "@docs/ptest-agent.md\n"
        "<!-- ptest-agent-rules:end -->\n"
    )
    assert agent_rules._block("GEMINI.md") == (
        "<!-- ptest-agent-rules:start -->\n"
        "@docs/ptest-agent.md\n"
        "<!-- ptest-agent-rules:end -->\n"
    )


def test_policy_block_adds_exactly_one_reference_line():
    assert agent_rules._block("AGENTS.md", test_policy=True) == (
        "<!-- ptest-agent-rules:start -->\n"
        "Before running or changing tests, read `docs/ptest-agent.md`.\n"
        "Before writing or changing tests, read `docs/ptest-test-policy.md`.\n"
        "<!-- ptest-agent-rules:end -->\n"
    )
    for name in ("CLAUDE.md", "GEMINI.md"):
        assert agent_rules._block(name, test_policy=True) == (
            "<!-- ptest-agent-rules:start -->\n"
            "@docs/ptest-agent.md\n"
            "@docs/ptest-test-policy.md\n"
            "<!-- ptest-agent-rules:end -->\n"
        )


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "GEMINI.md"])
def test_block_variant_recognises_exactly_two_variants(tmp_path, name):
    base = agent_rules._block(name)
    policy = agent_rules._block(name, test_policy=True)
    assert agent_rules.block_variant(f"# notes\n\n{base}", name) == "base"
    assert agent_rules.block_variant(f"# notes\n\n{policy}", name) == "policy"
    assert agent_rules.block_variant("# notes\n", name) is None


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "GEMINI.md"])
def test_block_variant_rejects_anything_else(tmp_path, name):
    base = agent_rules._block(name)
    policy = agent_rules._block(name, test_policy=True)
    with pytest.raises(C.Problem) as error:
        agent_rules.block_variant("# notes\n<!-- ptest-agent-rules:start -->\n", name)
    assert error.value.code == "invalid-config"
    with pytest.raises(C.Problem):
        agent_rules.block_variant(f"# notes\n\n{base}\n{base}", name)
    tampered = policy.replace("ptest-test-policy", "ptest-other-policy")
    with pytest.raises(C.Problem):
        agent_rules.block_variant(f"# notes\n\n{tampered}", name)
    extra = policy.replace("<!-- ptest-agent-rules:end -->",
                           "extra line\n<!-- ptest-agent-rules:end -->")
    with pytest.raises(C.Problem):
        agent_rules.block_variant(f"# notes\n\n{extra}", name)


def test_apply_test_policy_creates_policy_file_and_variant_block(tmp_path):
    result = agent_rules.apply(tmp_path, test_policy=True)

    assert result.changed is True
    assert "create docs/ptest-test-policy.md" in result.actions
    assert ((tmp_path / "docs" / "ptest-test-policy.md").read_bytes()
            == agent_rules._test_policy())
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agent_rules.block_variant(agents, "AGENTS.md") == "policy"

    second = agent_rules.apply(tmp_path, test_policy=True)
    assert second.changed is False
    assert "already present docs/ptest-test-policy.md" in second.actions


def test_apply_without_flag_never_installs_policy(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")

    result = agent_rules.apply(tmp_path)

    assert result.changed is True
    assert not (tmp_path / "docs" / "ptest-test-policy.md").exists()
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agent_rules.block_variant(agents, "AGENTS.md") == "base"
    assert all("test-policy" not in action and "test_policy" not in action
               for action in result.actions)


def test_apply_without_flag_converges_when_policy_recorded(tmp_path):
    agent_rules.apply(tmp_path, test_policy=True)
    (tmp_path / "docs" / "ptest-test-policy.md").unlink()

    result = agent_rules.apply(tmp_path)

    assert result.changed is True
    assert ((tmp_path / "docs" / "ptest-test-policy.md").read_bytes()
            == agent_rules._test_policy())
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agent_rules.block_variant(agents, "AGENTS.md") == "policy"


def test_preview_lists_exactly_the_files_that_change(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Existing\n\n" + agent_rules._block("AGENTS.md"), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*")
              if path.is_file()}

    plan = agent_rules.preview(tmp_path, test_policy=True)

    assert "create docs/ptest-test-policy.md" in plan.actions
    assert "add test-policy reference to AGENTS.md" in plan.actions
    assert {path: path.read_bytes() for path in tmp_path.rglob("*")
            if path.is_file()} == before

    base_plan = agent_rules.preview(tmp_path)
    assert all("test-policy" not in action and "test_policy" not in action
               for action in base_plan.actions)


def test_test_policy_installed_tracks_the_policy_variant(tmp_path):
    assert agent_rules.test_policy_installed(tmp_path) is False
    agent_rules.apply(tmp_path, test_policy=True)
    assert agent_rules.test_policy_installed(tmp_path) is True
    agent_rules.apply(tmp_path)
    assert agent_rules.test_policy_installed(tmp_path) is True


def test_test_policy_installed_is_false_on_any_problem(tmp_path):
    assert agent_rules.test_policy_installed(tmp_path / "missing") is False
    (tmp_path / "AGENTS.md").write_text(
        "# Existing\n<!-- ptest-agent-rules:start -->\n", encoding="utf-8")
    assert agent_rules.test_policy_installed(tmp_path) is False


def test_previous_policy_hash_upgrades_in_place(tmp_path, monkeypatch):
    agent_rules.apply(tmp_path, test_policy=True)
    target = tmp_path / "docs" / "ptest-test-policy.md"
    old = b"# old policy stance\n"
    target.write_bytes(old)
    import hashlib
    monkeypatch.setattr(
        agent_rules, "_PREVIOUS_TEST_POLICY_SHA256S",
        frozenset({hashlib.sha256(old).hexdigest()}))

    assert agent_rules.guidance_outdated(tmp_path) is True
    result = agent_rules.apply(tmp_path, test_policy=True)

    assert result.changed is True
    assert target.read_bytes() == agent_rules._test_policy()


def test_refresh_updates_previous_policy_without_touching_blocks(
        tmp_path, monkeypatch):
    agent_rules.apply(tmp_path, test_policy=True)
    target = tmp_path / "docs" / "ptest-test-policy.md"
    old = b"# old policy stance\n"
    target.write_bytes(old)
    import hashlib
    monkeypatch.setattr(
        agent_rules, "_PREVIOUS_TEST_POLICY_SHA256S",
        frozenset({hashlib.sha256(old).hexdigest()}))
    before = (tmp_path / "AGENTS.md").read_bytes()

    refreshed = agent_rules.refresh(tmp_path)

    assert refreshed.changed is True
    assert target.read_bytes() == agent_rules._test_policy()
    assert (tmp_path / "AGENTS.md").read_bytes() == before


def test_refresh_never_adds_policy_when_not_installed(tmp_path):
    agent_rules.apply(tmp_path)

    assert agent_rules.refresh(tmp_path).changed is False
    assert not (tmp_path / "docs" / "ptest-test-policy.md").exists()


def test_user_edited_policy_blocks_apply_before_any_write(tmp_path):
    agent_rules.apply(tmp_path, test_policy=True)
    target = tmp_path / "docs" / "ptest-test-policy.md"
    target.write_text("# my own stance\n", encoding="utf-8")
    agents_before = (tmp_path / "AGENTS.md").read_bytes()

    with pytest.raises(C.Problem) as error:
        agent_rules.apply(tmp_path, test_policy=True)

    assert error.value.code == "already-exists"
    assert target.read_text(encoding="utf-8") == "# my own stance\n"
    assert (tmp_path / "AGENTS.md").read_bytes() == agents_before
    assert agent_rules.refresh(tmp_path).changed is False
    assert agent_rules.guidance_outdated(tmp_path) is False


def test_symlinked_policy_target_is_refused_before_any_write(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ptest-test-policy.md").symlink_to(outside)

    with pytest.raises(C.Problem) as error:
        agent_rules.apply(tmp_path, test_policy=True)

    assert error.value.code == "unsafe-path"
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_non_regular_policy_target_is_refused_before_any_write(
        tmp_path, kind):
    import os
    import stat

    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "ptest-test-policy.md"
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()

    with pytest.raises(C.Problem) as error:
        agent_rules.apply(tmp_path, test_policy=True)

    assert error.value.code == "unsafe-path"
    with pytest.raises(C.Problem) as preview_error:
        agent_rules.preview(tmp_path, test_policy=True)

    assert preview_error.value.code == "unsafe-path"
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (docs / "ptest-agent.md").exists()
    stamp = os.lstat(target)
    if kind == "fifo":
        assert stat.S_ISFIFO(stamp.st_mode)
    else:
        assert stat.S_ISDIR(stamp.st_mode)


def test_oversized_instruction_file_is_refused_before_any_write(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"# Existing\n" + b"y" * 262145)

    with pytest.raises(C.Problem) as error:
        agent_rules.apply(tmp_path, test_policy=True)

    assert error.value.code == "invalid-bound"
    assert not (tmp_path / "docs").exists()


def test_hand_edited_block_refuses_apply_without_writing(tmp_path):
    agent_rules.apply(tmp_path, test_policy=True)
    target = tmp_path / "AGENTS.md"
    edited = target.read_text(encoding="utf-8").replace(
        "<!-- ptest-agent-rules:end -->",
        "# hand edit\n<!-- ptest-agent-rules:end -->")
    target.write_text(edited, encoding="utf-8")

    with pytest.raises(C.Problem) as error:
        agent_rules.apply(tmp_path, test_policy=True)

    assert error.value.code == "invalid-config"
    assert target.read_text(encoding="utf-8") == edited


def test_mid_apply_failure_rolls_back_policy_writes(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    (tmp_path / "GEMINI.md").write_text("# Gemini notes\n", encoding="utf-8")
    real_replace = agent_rules._replace
    calls = []

    def failing_replace(root, path, text, **kwargs):
        calls.append(path.name)
        if path.name == "GEMINI.md":
            raise C.Problem(code="state-unavailable",
                            message="synthetic write failure",
                            phase="agent-rules")
        return real_replace(root, path, text, **kwargs)

    monkeypatch.setattr(agent_rules, "_replace", failing_replace)
    with pytest.raises(C.Problem):
        agent_rules.apply(tmp_path, test_policy=True)

    assert not (tmp_path / "docs" / "ptest-test-policy.md").exists()
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# Existing\n"
    assert (tmp_path / "GEMINI.md").read_text(encoding="utf-8") == "# Gemini notes\n"
    assert "GEMINI.md" in calls


def test_init_parser_accepts_test_policy_modes():
    assert parse_argv(("init", "--test-policy")).test_policy is True
    assert parse_argv(("init", "--no-test-policy")).test_policy is False
    assert parse_argv(("init",)).test_policy is None


@pytest.mark.parametrize("argv", [
    ("init", "--test-policy", "--no-test-policy"),
    ("init", "--no-test-policy", "--test-policy"),
    ("init", "--test-policy", "--test-policy"),
    ("init", "--no-test-policy", "--no-test-policy"),
])
def test_init_parser_rejects_combined_or_repeated_policy_modes(argv):
    with pytest.raises(C.Problem) as error:
        parse_argv(argv)
    assert error.value.code == "invalid-config"
    assert (error.value.message
            == "init test-policy modes cannot be combined or repeated")


def test_rules_parser_accepts_apply_and_test_policy_in_any_order():
    assert parse_argv(("rules",)).apply_rules is False
    parsed = parse_argv(("rules", "--apply", "--test-policy"))
    assert parsed.apply_rules is True and parsed.test_policy is True
    parsed = parse_argv(("rules", "--test-policy", "--apply"))
    assert parsed.apply_rules is True and parsed.test_policy is True
    assert parse_argv(("rules", "--test-policy")).test_policy is True


@pytest.mark.parametrize("argv", [
    ("rules", "--apply", "--json"),
    ("rules", "--test-policy", "--json"),
    ("rules", "--apply", "--apply"),
    ("rules", "--frobnicate"),
])
def test_rules_parser_rejects_anything_else(argv):
    with pytest.raises(C.Problem) as error:
        parse_argv(argv)
    assert error.value.code == "invalid-config"


def test_init_prompt_installs_policy_on_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _answers(monkeypatch, "y")

    assert main(_init()) == 0
    captured = capsys.readouterr()

    assert "Also install the stricter test policy? It changes:" in captured.err
    assert "[y/N]:" in captured.err
    assert (tmp_path / POLICY_REL).read_bytes() == agent_rules._test_policy()
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agent_rules.block_variant(agents, "AGENTS.md") == "policy"


def test_init_prompt_defaults_to_no_on_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _answers(monkeypatch, "")

    assert main(_init()) == 0

    assert not (tmp_path / POLICY_REL).exists()
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agent_rules.block_variant(agents, "AGENTS.md") == "base"


@pytest.mark.parametrize("consent", ["YES", " y ", "yes"])
def test_init_prompt_accepts_only_yes_spellings(
        tmp_path, monkeypatch, capsys, consent):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _answers(monkeypatch, consent)

    assert main(_init()) == 0
    capsys.readouterr()

    assert (tmp_path / POLICY_REL).is_file()


@pytest.mark.parametrize("consent", ["n", "no", "yeah", "yep"])
def test_init_prompt_declines_anything_but_yes(
        tmp_path, monkeypatch, capsys, consent):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _answers(monkeypatch, consent)

    assert main(_init()) == 0
    capsys.readouterr()

    assert not (tmp_path / POLICY_REL).exists()


def test_init_prompt_declines_on_eof(tmp_path, monkeypatch, capsys):
    import builtins

    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)

    def raise_eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)

    assert main(_init()) == 0
    capsys.readouterr()

    assert not (tmp_path / POLICY_REL).exists()


def test_piped_consent_never_installs_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _answers(monkeypatch, "y")

    assert main(_init()) == 0
    captured = capsys.readouterr()

    assert "stricter test policy" not in captured.err
    assert not (tmp_path / POLICY_REL).exists()


def test_ci_on_tty_never_prompts_or_installs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("prompted in CI"))

    assert main(_init()) == 0
    captured = capsys.readouterr()

    assert "stricter test policy" not in captured.err
    assert not (tmp_path / POLICY_REL).exists()


def test_no_test_policy_suppresses_prompt_without_removing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, agents=("claude",), test_policy=True)
    _tty(monkeypatch)
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("prompted with --no-test-policy"))

    assert main(_init("--no-test-policy")) == 0
    captured = capsys.readouterr()

    assert "stricter test policy" not in captured.err
    assert (tmp_path / POLICY_REL).is_file()


def test_agents_none_never_prompts_for_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    prompts = []
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: prompts.append(1) or "n")

    assert main(("init", "--runner", "pytest", "--agents", "none",
                 "--no-smoke", "--no-doctor")) == 0
    capsys.readouterr()

    assert prompts == []
    assert not (tmp_path / POLICY_REL).exists()


def test_fully_interactive_init_never_prompts_for_policy(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    answers = iter(("all",))
    inputs = []

    def strict_input(*args, **kwargs):
        answer = next(answers)
        inputs.append(answer)
        return answer

    monkeypatch.setattr("builtins.input", strict_input)

    assert main(("init", "--runner", "pytest",
                 "--no-smoke", "--no-doctor")) == 0
    captured = capsys.readouterr()

    assert "stricter test policy" not in captured.err
    assert inputs == ["all"]
    assert not (tmp_path / POLICY_REL).exists()


def test_recorded_policy_never_prompts_again(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, agents=("claude",), test_policy=True)
    _tty(monkeypatch)
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("prompted when recorded"))

    assert main(_init()) == 0
    captured = capsys.readouterr()

    assert "stricter test policy" not in captured.err
    assert "already installed" in captured.err


def test_init_test_policy_lists_changes_on_stderr_before_writing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "# Existing\n\n" + agent_rules._block("AGENTS.md"), encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(_init("--test-policy")) == 0
    captured = capsys.readouterr()

    assert ("ptest: test policy: will change: create docs/ptest-test-policy.md"
            in captured.err)
    assert "add one reference line to AGENTS.md" in captured.err
    assert (tmp_path / POLICY_REL).is_file()


def test_init_dry_run_reports_would_change_and_writes_nothing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*")
              if path.is_file()}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(_init("--test-policy", "--dry-run")) == 0
    captured = capsys.readouterr()

    assert ("ptest: test policy: would change: create docs/ptest-test-policy.md"
            in captured.err)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*")
            if path.is_file()} == before
    assert "preview" in captured.out


def test_init_json_test_policy_keeps_schema_with_policy_commit_path(
        tmp_path, monkeypatch, capsys):
    _git(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("JSON init prompted"))

    assert main(_init("--test-policy", "--json")) == 0
    captured = capsys.readouterr()

    document = C.decode_public_document(captured.out)
    assert document.kind == "init"
    assert document.error is None
    assert set(document.data) == {"action", "target", "exists", "warnings",
                                  "config", "commit_paths"}
    assert POLICY_REL in document.data["commit_paths"]
    assert "ptest: test policy: will change:" in captured.err


def test_conflict_lines_are_listed_and_never_edited(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "# Existing\nKeep coverage above 90 percent.\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = (tmp_path / "AGENTS.md").read_bytes()

    assert main(_init("--test-policy")) == 0
    captured = capsys.readouterr()

    header = ("These lines in your instruction files name a coverage percentage;"
              " ptest never edits them. Review them yourself:")
    assert header in captured.out
    assert "AGENTS.md:2:" in captured.out
    assert "90 percent" in captured.out
    agents = tmp_path / "AGENTS.md"
    assert agents.read_bytes().startswith(before)
    assert "Keep coverage above 90 percent." in agents.read_text(encoding="utf-8")


def test_conflict_listing_sanitizes_hostile_text(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    hostile = "Keep coverage \x1b[31mabove 90%\x07 alive.\n"
    (tmp_path / "AGENTS.md").write_text(f"# Existing\n{hostile}",
                                        encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(_init("--test-policy")) == 0
    captured = capsys.readouterr()

    assert "\x1b[31m" not in captured.out
    assert "\x07" not in captured.out
    assert "AGENTS.md:2:" in captured.out


def test_rules_test_policy_previews_without_writing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    before = (tmp_path / "AGENTS.md").read_bytes()

    assert main(("rules", "--test-policy")) == 0
    captured = capsys.readouterr()

    assert "preview:" in captured.out
    assert "create docs/ptest-test-policy.md" in captured.out
    assert (tmp_path / "AGENTS.md").read_bytes() == before
    assert not (tmp_path / "docs" / "ptest-test-policy.md").exists()


def test_rules_apply_test_policy_reports_will_change_then_applied(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")

    assert main(("rules", "--apply", "--test-policy")) == 0
    captured = capsys.readouterr()

    assert "will change: create docs/ptest-test-policy.md" in captured.out
    assert "applied:" in captured.out
    assert (captured.out.index("will change:")
            < captured.out.index("applied:"))
    assert (tmp_path / POLICY_REL).is_file()


def test_rules_apply_lists_appended_instruction_file_before_writing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude notes\n", encoding="utf-8")
    claude_before = (tmp_path / "CLAUDE.md").read_bytes()

    assert main(("rules", "--apply", "--test-policy")) == 0
    captured = capsys.readouterr()

    assert captured.out.index("will change:") < captured.out.index("applied:")
    will_change = captured.out.split("will change:")[1].split("applied:")[0]
    assert "create docs/ptest-test-policy.md" in will_change
    assert "add one reference line to AGENTS.md" in will_change
    assert "append managed reference to CLAUDE.md" in will_change
    assert "append managed reference to CLAUDE.md" in captured.out.split(
        "applied:")[1]
    assert (tmp_path / "CLAUDE.md").read_bytes() != claude_before
    assert agent_rules.block_variant(
        (tmp_path / "CLAUDE.md").read_text(encoding="utf-8"),
        "CLAUDE.md") == "policy"


def test_rules_apply_test_policy_reports_already_installed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, test_policy=True)

    assert main(("rules", "--apply", "--test-policy")) == 0
    captured = capsys.readouterr()

    assert "test policy: already installed" in captured.out
    assert "will change:" not in captured.out


def test_rules_apply_without_flag_converges_recorded_policy(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, test_policy=True)
    (tmp_path / POLICY_REL).unlink()
    assert main(("rules", "--apply")) == 0
    captured = capsys.readouterr()

    assert "applied:" in captured.out
    assert "will change:" not in captured.out
    assert (tmp_path / POLICY_REL).is_file()


def test_rules_lists_conflicts_for_recorded_policy_without_flag(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, test_policy=True)
    with open(tmp_path / "AGENTS.md", "a", encoding="utf-8") as stream:
        stream.write("Keep coverage above 80 percent.\n")

    assert main(("rules",)) == 0
    captured = capsys.readouterr()

    assert "preview:" in captured.out
    assert ("These lines in your instruction files name a coverage percentage;"
            in captured.out)
    assert "AGENTS.md:" in captured.out


def test_init_lists_conflicts_for_recorded_policy_without_flag(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, agents=("claude",), test_policy=True)
    with open(tmp_path / "AGENTS.md", "a", encoding="utf-8") as stream:
        stream.write("Keep coverage above 80 percent.\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(_init()) == 0
    captured = capsys.readouterr()

    assert ("These lines in your instruction files name a coverage percentage;"
            in captured.out)


def _init_no_agents(*extra):
    return ("init", "--runner", "pytest",
            "--no-smoke", "--no-doctor", *extra)


def test_noninteractive_init_with_recorded_policy_keeps_edited_policy(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, agents=("claude",), test_policy=True)
    target = tmp_path / POLICY_REL
    target.write_text("# my own stance\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert main(_init_no_agents()) == 0
    captured = capsys.readouterr()

    assert target.read_text(encoding="utf-8") == "# my own stance\n"
    assert "will change" not in captured.err


def test_noninteractive_init_with_recorded_policy_leaves_blocks_alone(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    agent_rules.apply(tmp_path, agents=("claude",), test_policy=True)
    (tmp_path / "GEMINI.md").write_text("# Gemini notes\n", encoding="utf-8")
    gemini_before = (tmp_path / "GEMINI.md").read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)

    assert main(_init_no_agents()) == 0
    captured = capsys.readouterr()

    assert (tmp_path / "GEMINI.md").read_bytes() == gemini_before
    assert "will change" not in captured.err


def test_rules_without_flag_keeps_base_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(("rules",)) == 0
    captured = capsys.readouterr()

    assert "create docs/ptest-agent.md" in captured.out
    assert "ptest-test-policy" not in captured.out
    assert not (tmp_path / "docs").exists()


def test_test_policy_outputs_is_best_effort_without_doctor_modules(tmp_path):
    from pathlib import Path

    from ptest import cli

    resolution = C.ConfigResolution(root=Path(tmp_path), path=None, config=None)
    report, text = cli._test_policy_outputs(resolution)
    assert (report, text) == (None, "")


def _recipe_topics():
    from ptest import checklist

    names = getattr(checklist, "recipe_names", None)
    if callable(names):
        return tuple(names())
    return tuple(checklist._RECIPE_FILES)


@pytest.mark.parametrize("topic", [
    "factories", "databases", "cache", "files-ports", "processes",
    "time-network",
])
def test_guide_topic_prints_exactly_the_recipe(
        tmp_path, monkeypatch, capsys, topic):
    from ptest import checklist

    monkeypatch.chdir(tmp_path)

    assert main(("guide", topic)) == 0
    captured = capsys.readouterr()

    assert captured.out == checklist.load_recipe(topic)
    assert captured.err == ""


_HAS_TESTS_RECIPE = "tests" in _recipe_topics()


@pytest.mark.skipif(not _HAS_TESTS_RECIPE,
                    reason="tests recipe arrives with T1")
def test_guide_tests_topic_waits_for_the_tests_recipe(tmp_path, monkeypatch, capsys):
    from ptest import checklist

    monkeypatch.chdir(tmp_path)

    assert main(("guide", "tests")) == 0
    captured = capsys.readouterr()

    assert captured.out == checklist.load_recipe("tests")


@pytest.mark.parametrize("topic", ["unknown", "../x", "/etc/passwd", "tests/../x"])
def test_guide_unknown_topic_exits_two_with_fixed_list(
        tmp_path, monkeypatch, capsys, topic):
    monkeypatch.chdir(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*")
              if path.is_file()}

    assert main(("guide", topic)) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "unknown guide topic; topics:" in captured.err
    assert topic not in captured.err or topic == "unknown"
    for name in _recipe_topics():
        assert name in captured.err
    assert {path: path.read_bytes() for path in tmp_path.rglob("*")
            if path.is_file()} == before
    assert not (tmp_path / "x").exists()


def test_guide_topic_with_write_exits_two_without_writing(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(("guide", "factories", "--write", "guide.md")) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err != ""
    assert not (tmp_path / "guide.md").exists()


def test_guide_without_topic_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(("guide",)) == 0
    captured = capsys.readouterr()

    assert "Doctor assessment checklist" in captured.out
    assert captured.err == ""


def _git(root):
    marker = root / ".git"
    marker.mkdir(exist_ok=True)
    (marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (marker / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")


def _uninstall(domain, *args):
    return main(("--fixture-domain", str(domain.root), "uninstall", *args))


def test_uninstall_removes_owned_policy_and_restores_base_block(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    (root / "AGENTS.md").write_text("# Agent notes\n", encoding="utf-8")
    agent_rules.apply(root, test_policy=True)
    assert agent_rules.block_variant(
        (root / "AGENTS.md").read_text(encoding="utf-8"), "AGENTS.md") == "policy"
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out

    assert POLICY_REL in out
    assert not (root / POLICY_REL).exists()
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "# Agent notes\n"
    assert not (root / "docs").exists()


def test_uninstall_unlinks_init_created_block_only_files(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    agent_rules.apply(root, test_policy=True)
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == (
        agent_rules._block("AGENTS.md", test_policy=True))
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert not (root / "AGENTS.md").exists()
    assert not (root / POLICY_REL).exists()


def test_uninstall_keeps_a_hand_edited_policy_file(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    agent_rules.apply(root, test_policy=True)
    (root / POLICY_REL).write_text("# my own stance\n", encoding="utf-8")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out

    assert "edited" in out
    assert (root / POLICY_REL).read_text(encoding="utf-8") == "# my own stance\n"


def test_uninstall_removes_previous_hash_policy_file(
        case, tmp_path, monkeypatch, capsys):
    import hashlib

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    agent_rules.apply(root, test_policy=True)
    old = b"# old policy stance\n"
    (root / POLICY_REL).write_bytes(old)
    monkeypatch.setattr(
        agent_rules, "_PREVIOUS_TEST_POLICY_SHA256S",
        frozenset({hashlib.sha256(old).hexdigest()}))
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert not (root / POLICY_REL).exists()


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_uninstall_skips_non_regular_policy_target(
        case, tmp_path, monkeypatch, capsys, kind):
    import os
    import stat

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    agent_rules.apply(root, test_policy=True)
    (root / POLICY_REL).unlink()
    target = root / POLICY_REL
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out

    assert POLICY_REL in out
    assert "skipped" in out
    stamp = os.lstat(target)
    if kind == "fifo":
        assert stat.S_ISFIFO(stamp.st_mode)
    else:
        assert stat.S_ISDIR(stamp.st_mode)


def test_uninstall_never_rewrites_a_hand_edited_block(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    agent_rules.apply(root, test_policy=True)
    target = root / "AGENTS.md"
    edited = target.read_text(encoding="utf-8").replace(
        "<!-- ptest-agent-rules:end -->",
        "# hand edit\n<!-- ptest-agent-rules:end -->")
    target.write_text(edited, encoding="utf-8")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert target.read_text(encoding="utf-8") == edited
