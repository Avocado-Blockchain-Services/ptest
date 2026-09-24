"""`ptest uninstall` strict-TDD suite: spec groups plus abuse-case twins.

Tests run through the CLI (`main`) with an explicit `--fixture-domain`, so
no real account domain, installer, or provider is ever touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptest import agent_rules
from ptest import contracts as C
from ptest import history as history_api
from ptest import platform as platform_api
from ptest import recommendations
from ptest import scheduler
from ptest.cli import main

PROJ = "ab" * 16
OTHER_PROJ = "cd" * 16


def _git(root: Path) -> None:
    marker = root / ".git"
    marker.mkdir(exist_ok=True)
    (marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (marker / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")


def _v1(root: Path, project_id: str = PROJ) -> None:
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n"
        "[runner]\nkind = \"command\"\nlauncher = [\"true\"]\n",
        encoding="utf-8")


def _checkout_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]


def _checkout(root: Path, project_id: str = PROJ) -> C.CheckoutIdentity:
    return C.CheckoutIdentity(
        project_id=project_id, checkout_id=_checkout_id(root), root=root)


def _uninstall(domain: C.DomainPaths, *args: str) -> int:
    return main(("--fixture-domain", str(domain.root), "uninstall", *args))


def _snapshot(root: Path) -> dict:
    """Byte-exact tree image, symlink-aware (links recorded, never followed)."""
    out: dict = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            rel = str(path.relative_to(root))
            try:
                stamp = os.lstat(path)
            except FileNotFoundError:
                continue
            import stat as _stat
            if _stat.S_ISLNK(stamp.st_mode):
                out[rel] = ("link", os.readlink(path))
            elif _stat.S_ISREG(stamp.st_mode):
                out[rel] = ("file", path.read_bytes())
            else:
                out[rel] = ("other", None)
    return out


def _report_bytes(body: bytes = b"# notes\nkept body\n") -> bytes:
    return recommendations._marker_for(body) + body


def _snapshot_case(case, domain: C.DomainPaths, root: Path,
                   project_id: str = PROJ):
    checkout = _checkout(root, project_id)
    before = case.snapshot(digest="11" * 32, compatibility="compat-v1")
    after = case.snapshot(digest="22" * 32, compatibility="compat-v1")
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=after.digest,
                  compatibility=after.compatibility)
    result = case.result(sequence=1, plan=plan, input_before=before,
                         input_after=after, policy_digest="33" * 32,
                         project_id=checkout.project_id,
                         checkout_id=checkout.checkout_id)
    inventory = case.inventory(("tests/test_a.py",))
    published = history_api.publish_outcome(domain, checkout, result, inventory)
    assert published.committed is True
    return checkout


# --- round trip -----------------------------------------------------------

def test_round_trip_restores_pre_init_tree_byte_for_byte(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = domain.root / "repo"
    (root / "api").mkdir(parents=True)
    (root / "web").mkdir()
    _git(root)
    (root / "AGENTS.md").write_text("# Agent notes\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Claude notes\n", encoding="utf-8")
    before = _snapshot(root)
    monkeypatch.chdir(root)
    assert main(("init", "--child", "api", "--runner", "pytest",
                 "--child", "web", "--runner", "pytest",
                 "--agents", "claude,codex,opencode,gemini")) == 0
    capsys.readouterr()
    assert (root / ".ptest.toml").is_file()
    assert (root / "api" / ".ptest.toml").is_file()
    assert (root / "docs" / "ptest-agent.md").is_file()
    (root / "recommendations.md").write_bytes(_report_bytes())
    (root / "api" / "recommendations.md").write_bytes(_report_bytes())
    checkout = _snapshot_case(case, domain, root)
    assert (domain.root / "checkouts" / checkout.checkout_id).is_dir()

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "remove" in out

    assert _snapshot(root) == before
    assert not (domain.root / "checkouts" / checkout.checkout_id).exists()
    if domain.ledger.exists():
        rows = sqlite3.connect(domain.ledger).execute(
            "SELECT run_id FROM jobs WHERE checkout_id=?",
            (checkout.checkout_id,)).fetchall()
        assert rows == []


def test_init_after_uninstall_is_a_clean_first_init(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    (root / "pyproject.toml").write_text(
        "[project]\ndependencies = [\"pytest>=8\"]\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    out = capsys.readouterr().out
    assert (root / ".ptest.toml").is_file()
    assert (root / "docs" / "ptest-agent.md").is_file()
    assert (root / ".agents" / "skills" / "ptest" / "SKILL.md").is_file()
    assert "already" not in out


def test_subdirectory_anchors_at_the_git_root(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = domain.root / "repo"
    (root / "api").mkdir(parents=True)
    (root / "web").mkdir()
    _git(root)
    (root / "AGENTS.md").write_text("# notes\n", encoding="utf-8")
    before = _snapshot(root)
    monkeypatch.chdir(root)
    assert main(("init", "--child", "api", "--runner", "pytest",
                 "--child", "web", "--runner", "pytest",
                 "--agents", "claude")) == 0
    capsys.readouterr()

    monkeypatch.chdir(root / "api")
    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert _snapshot(root) == before
    assert not (root / ".ptest.toml").exists()
    assert not (root / "api" / ".ptest.toml").exists()
    assert not (root / "web" / ".ptest.toml").exists()


# --- keep edited files ----------------------------------------------------

def test_edited_guide_skill_report_kept_but_edited_config_removed(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest",
                 "--agents", "claude,codex")) == 0
    capsys.readouterr()
    guide = root / "docs" / "ptest-agent.md"
    guide.write_text(guide.read_text(encoding="utf-8") + "\nuser edit\n",
                     encoding="utf-8")
    skill = root / ".agents" / "skills" / "ptest" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nuser edit\n",
                     encoding="utf-8")
    (root / "recommendations.md").write_bytes(_report_bytes())
    with (root / "recommendations.md").open("ab") as stream:
        stream.write(b"\nuser edit\n")
    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8") + "# tuned\n",
                      encoding="utf-8")

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "kept (edited)" in out
    assert "user edit" in guide.read_text(encoding="utf-8")
    assert "user edit" in skill.read_text(encoding="utf-8")
    assert "user edit" in (root / "recommendations.md").read_bytes().decode()
    assert not config.exists()


def test_previous_managed_guide_is_removed_by_uninstall(
        case, tmp_path, monkeypatch, capsys):
    """Twin: an old ptest-managed guide uninstalls as managed, not edited."""
    import hashlib

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    old = b"# old managed guide\n"
    monkeypatch.setattr(agent_rules, "_PREVIOUS_GUIDE_SHA256S",
                        frozenset({hashlib.sha256(old).hexdigest()}))
    guide_dir = root / "docs"
    guide_dir.mkdir()
    (guide_dir / "ptest-agent.md").write_bytes(old)
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert not (guide_dir / "ptest-agent.md").exists()


def test_released_3f399fb_guide_is_upgraded_by_init_and_removed_by_uninstall(
        case, tmp_path, monkeypatch, capsys):
    """Twin: a repo holding the 3f399fb guide upgrades cleanly, then uninstalls fully.

    Uses the real `_PREVIOUS_GUIDE_SHA256S` (no monkeypatch): the fixture
    bytes must hash to the registered 22babcd6… digest, `init` must upgrade
    them in place (no already-exists), and `uninstall --yes` must remove
    the upgraded guide.
    """
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    fixture = (Path(__file__).resolve().parent / "fixtures" / "previous-guides"
               / "3f399fb-ptest-agent.md")
    old = fixture.read_bytes()
    assert hashlib.sha256(old).hexdigest() == (
        "22babcd66d575ec65c481c747a8527b9b2ad0b8f206f8d83c0dafa8254c4691c")
    guide_dir = root / "docs"
    guide_dir.mkdir()
    (guide_dir / "ptest-agent.md").write_bytes(old)
    monkeypatch.chdir(root)

    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    init_out = capsys.readouterr().out
    assert "already" not in init_out
    assert (guide_dir / "ptest-agent.md").read_bytes() == agent_rules._guide()

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "edited" not in out
    assert not (guide_dir / "ptest-agent.md").exists()


def test_unmanaged_skill_content_is_kept(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    foreign = root / ".claude" / "skills" / "ptest" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("# user skill\n", encoding="utf-8")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "kept (edited)" in out
    assert foreign.read_text(encoding="utf-8") == "# user skill\n"
    assert not (root / ".ptest.toml").exists()


def test_pre_gate_skill_managed_bytes_are_removed(
        case, tmp_path, monkeypatch, capsys):
    """Twin: the long pre-fast-forward-gate skill uninstalls as managed."""
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    skill = root / ".claude" / "skills" / "ptest" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(
        "---\n"
        "name: ptest\n"
        "description: Coordinate repository testing through ptest from the repository root.\n"
        "---\n"
        "\n"
        "# ptest skill\n"
        "\n"
        "Before running or changing tests, read the repository-root guide\n"
        "`docs/ptest-agent.md`. That path is relative to the repository root,\n"
        "not to this skill directory.\n"
        "\n"
        "Run every test command through `ptest` from the repository root (the\n"
        "directory containing the root `.ptest.toml`). Never invoke pytest,\n"
        "Vitest, or another runner directly.\n"
        "\n"
        "During iteration run the smallest relevant scope, such as\n"
        "`ptest tests/<chosen-test>.py`. In a monorepo, prefix the scope with\n"
        "its declared child, such as `ptest api/tests/<chosen-test>.py`; child\n"
        "`.ptest.toml` files remain authoritative. Run the root full gate\n"
        "`ptest --full` once after the integrated change.\n".encode("utf-8"))
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "edited" not in out
    assert not skill.exists()


def test_legacy_codex_skill_managed_bytes_are_removed(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    legacy = root / ".codex" / "skills" / "ptest" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(agent_rules._legacy_provider_text("codex"))
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert not legacy.exists()
    assert not (root / ".codex").exists()


# --- unmanaged or foreign files -------------------------------------------

def test_unbalanced_markers_are_left_alone(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    agents = root / "AGENTS.md"
    agents.write_text("# notes\n<!-- ptest-agent-rules:start -->\n", encoding="utf-8")
    monkeypatch.chdir(root)

    # U4-4: unbalanced markers are reported/kept, exit stays 0.
    code = _uninstall(domain, "--yes")
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped" in out
    assert agents.read_text(encoding="utf-8") == (
        "# notes\n<!-- ptest-agent-rules:start -->\n")
    assert not (root / ".ptest.toml").exists()


def test_duplicated_markers_are_left_alone(case, tmp_path, monkeypatch):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    block = agent_rules._block("AGENTS.md")
    agents = root / "AGENTS.md"
    agents.write_text(block + "\n" + block, encoding="utf-8")
    monkeypatch.chdir(root)

    # U4-4: duplicated markers are reported/kept, exit stays 0.
    assert _uninstall(domain, "--yes") == 0
    assert agents.read_text(encoding="utf-8") == block + "\n" + block


def test_opencode_owned_files_and_foreign_skills_survive(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "opencode")) == 0
    capsys.readouterr()
    oc = root / ".opencode"
    (oc / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (oc / "node_modules").mkdir()
    (oc / "node_modules" / "keep.js").write_text("1\n", encoding="utf-8")
    other = oc / "skills" / "other" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("# other\n", encoding="utf-8")

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert (oc / "package.json").read_text(encoding="utf-8") == '{"name":"x"}\n'
    assert (oc / "node_modules" / "keep.js").is_file()
    assert other.read_text(encoding="utf-8") == "# other\n"
    assert not (oc / "skills" / "ptest").exists()


# --- symlinks ---------------------------------------------------------------

def test_symlinked_config_skill_docs_and_agent_file_are_left_alone(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    (root / "docs").mkdir(parents=True)
    outside.mkdir()
    _git(root)
    real_config = outside / ".ptest.toml"
    real_config.write_text("version = 1\n", encoding="utf-8")
    (root / ".ptest.toml").symlink_to(real_config)
    real_skill = outside / "SKILL.md"
    real_skill.write_text("# skill\n", encoding="utf-8")
    skill = root / ".claude" / "skills" / "ptest" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.symlink_to(real_skill)
    real_docs = outside / "docs"
    real_docs.mkdir()
    (real_docs / "ptest-agent.md").write_text("# guide\n", encoding="utf-8")
    (root / "docs").rmdir()
    (root / "docs").symlink_to(real_docs, target_is_directory=True)
    real_agents = outside / "AGENTS.md"
    real_agents.write_text("# agents\n", encoding="utf-8")
    (root / "AGENTS.md").symlink_to(real_agents)
    monkeypatch.chdir(root)

    # U4-4: symlinks are reported and left alone, exit stays 0.
    code = _uninstall(domain, "--yes")
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped" in out
    assert (root / ".ptest.toml").is_symlink()
    assert real_config.read_bytes() == b"version = 1\n"
    assert skill.is_symlink()
    assert real_skill.read_bytes() == b"# skill\n"
    assert (root / "docs").is_symlink()
    assert (real_docs / "ptest-agent.md").read_bytes() == b"# guide\n"
    assert (root / "AGENTS.md").is_symlink()
    assert real_agents.read_bytes() == b"# agents\n"


# --- consent ----------------------------------------------------------------

def _tty(monkeypatch, answer):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    if isinstance(answer, str):
        monkeypatch.setattr("builtins.input", lambda *args, **kwargs: answer)
    else:
        def _raise(*args, **kwargs):
            raise EOFError
        monkeypatch.setattr("builtins.input", _raise)


def test_tty_decline_changes_nothing(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    before = _snapshot(root)
    _tty(monkeypatch, "n")

    assert _uninstall(domain) != 0
    err = capsys.readouterr().err
    assert "nothing changed" in err
    assert _snapshot(root) == before


def test_tty_eof_changes_nothing(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    before = _snapshot(root)
    _tty(monkeypatch, None)

    assert _uninstall(domain) != 0
    assert _snapshot(root) == before


def test_tty_accept_removes(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    _tty(monkeypatch, "y")

    assert _uninstall(domain) == 0
    capsys.readouterr()
    assert not (root / ".ptest.toml").exists()


def test_yes_prints_only_the_result_once(case, tmp_path, monkeypatch, capsys):
    """`--yes` prints the result (plan + summary) exactly once, no pre-plan."""
    import re

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "ptest uninstall plan" not in out
    assert out.count("ptest uninstall result") == 1
    assert out.count("remove:") == 1
    assert re.search(r"removed \d+, kept \d+, skipped \d+", out) is not None


def test_interactive_yes_prints_plan_once_then_summary(
        case, tmp_path, monkeypatch, capsys):
    """TTY accept prints the plan once, then only the summary line."""
    import re

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    _tty(monkeypatch, "y")

    assert _uninstall(domain) == 0
    out = capsys.readouterr().out
    assert out.count("ptest uninstall plan") == 1
    assert "ptest uninstall result" not in out
    assert out.count("remove:") == 1
    assert re.search(r"removed \d+, kept \d+, skipped \d+", out) is not None


def test_non_tty_without_yes_shows_plan_and_changes_nothing(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    before = _snapshot(root)

    code = _uninstall(domain)
    captured = capsys.readouterr()
    assert code != 0
    assert "remove" in captured.out
    assert "--yes" in captured.err
    assert _snapshot(root) == before


def test_dry_run_changes_nothing(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    before = _snapshot(root)

    assert _uninstall(domain, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "remove" in out
    assert _snapshot(root) == before


# --- state --------------------------------------------------------------------

def test_checkout_state_removed_while_second_checkout_survives(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo-one"
    root.mkdir()
    _git(root)
    _v1(root)
    other = domain.root / "repo-two"
    other.mkdir()
    _git(other)
    _v1(other, OTHER_PROJ)
    first = _snapshot_case(case, domain, root)
    second = _snapshot_case(case, domain, other, OTHER_PROJ)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    for checkout, run_id in ((first, "ee" * 16), (second, "ff" * 16)):
        ticket = scheduler.enqueue(domain, C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=True))
        assert scheduler.cancel_pending(domain, ticket, owner) is True
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert not (domain.root / "checkouts" / first.checkout_id).exists()
    assert (domain.root / "checkouts" / second.checkout_id).is_dir()
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id").fetchall())
    assert first.checkout_id not in rows
    assert rows.get(second.checkout_id, 0) >= 1


def test_setup_fingerprint_dir_is_removed_with_checkout_state(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    checkout = _checkout(root)
    from ptest import operations
    operations._record_setup_fingerprint(domain, checkout, "ab" * 32)
    marker = domain.root / "checkouts" / checkout.checkout_id / "setup-fingerprint.json"
    assert marker.is_file()
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert not marker.exists()
    assert not (domain.root / "checkouts" / checkout.checkout_id).exists()


def test_active_run_refuses_before_any_mutation(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    checkout = _checkout(root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="aa" * 16, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=True))
    monkeypatch.chdir(root)
    before = _snapshot(root)

    code = _uninstall(domain, "--yes")
    captured = capsys.readouterr()
    assert code != 0
    assert "active" in captured.err
    assert _snapshot(root) == before
    assert (root / ".ptest.toml").is_file()


def test_foreign_terminal_ledger_rows_survive(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    mine = _checkout(root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    foreign_root = domain.root / "foreign"
    foreign_root.mkdir()
    foreign = C.CheckoutIdentity(
        project_id=OTHER_PROJ, checkout_id="00" * 16, root=foreign_root)
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="bb" * 16, checkout=foreign, owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(domain, ticket, owner) is True
    mine_ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="cc" * 16, checkout=mine, owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(domain, mine_ticket, owner) is True
    terminal = scheduler.reconcile(domain)
    assert any(item.checkout_id == foreign.checkout_id
               and item.state is C.LeaseState.CANCELLED for item in terminal)
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id").fetchall())
    assert mine.checkout_id not in rows
    assert rows.get(foreign.checkout_id, 0) >= 1


def test_shared_review_model_cache_is_kept(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    from ptest import files as files_api
    cache_dir = files_api.ensure_private_dir(domain.root, "review-models")
    files_api.publish_atomic(
        cache_dir, "codex.json",
        json.dumps({"cli_version": "x", "model": "m"}).encode("utf-8"))
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert (cache_dir / "codex.json").is_file()


# --- --self -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_self_discovery(tmp_path, monkeypatch):
    """Incident U2: fake every --self candidate source toward tmp.

    ``plan_self`` discovers roots from ``sys.argv[0]`` (the pytest
    bridge when the suite runs under an installed ptest), the running
    package's ``__file__``, ``shutil.which("ptest")``, and the passwd
    home.  Point all four at tmp so no test can ever plan the real
    install; the --self tests below wire their fixture root explicitly.
    """
    stub = tmp_path / "argv-stub"
    stub.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(stub)])
    from ptest import uninstall as uninstall_api
    monkeypatch.setattr(uninstall_api, "__file__", str(stub))
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(home)))


def _fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(home)))
    return home


def _install_fixture(root: Path, bundle_id: str = "9" * 8) -> Path:
    bundle = root / ".ptest-bundles" / bundle_id
    (bundle / "venv" / "bin").mkdir(parents=True)
    (bundle / "complete.json").write_text(json.dumps({
        "version": 1, "bundle_id": bundle_id,
        "ptest_version": "0.1.5", "python_version": "3.11.0",
        "wheel_sha256s": ["ab" * 32, "cd" * 32],
        "entrypoint": "venv/bin/ptest",
    }), encoding="utf-8")
    target = bundle / "venv" / "bin" / "ptest"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(target, 0o755)
    public = root / "ptest"
    public.symlink_to(Path(".ptest-bundles") / bundle_id / "venv" / "bin" / "ptest")
    return target


def test_self_removes_fixture_install_root_but_keeps_foreign_symlink(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ptest").symlink_to(target)
    (bindir / "other").symlink_to("/bin/true")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: str(bindir / "ptest"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    code = _uninstall(domain, "--self", "--yes")
    captured = capsys.readouterr()
    assert code == 0
    assert "ptest was uninstalled" in captured.out
    assert not inst.exists()
    assert not (bindir / "ptest").exists()
    assert (bindir / "other").is_symlink()


def test_self_detects_running_binary_inside_install_root(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    assert _uninstall(domain, "--self", "--yes") == 0
    out = capsys.readouterr().out
    assert "ptest was uninstalled" in out
    assert not inst.exists()


def test_self_plans_only_the_argv_fixture_root_never_real_paths(
        case, tmp_path, monkeypatch, capsys):
    """Incident U2 twin: argv[0] inside a fake root plans only that root.

    The real ``~/.local/ptest`` root is never a candidate here, and the
    planned root is printed before confirmation runs.
    """
    from ptest import uninstall as uninstall_api
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])

    plan = uninstall_api.plan_self()
    assert plan.root == inst
    assert plan.refused is None
    assert plan.root.is_relative_to(tmp_path)
    real_root = Path(os.environ["HOME"]) / ".local" / "ptest"
    assert plan.root != real_root
    assert (plan.refused_path or "") != str(real_root)

    domain = case.domain()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert _uninstall(domain, "--self", "--yes") == 0
    out = capsys.readouterr().out
    assert str(inst) in out
    assert not inst.exists()


def test_self_discovery_refuses_a_root_outside_test_tmp(
        tmp_path, monkeypatch):
    """Incident U2 twin: the suite guard, not the plan, owns stray roots.

    A valid install layout outside this test's tmp tree must raise out
    of ``plan_self`` (via the conftest discovery guard) instead of ever
    becoming a plan that ``apply_self`` could execute.
    """
    from ptest import uninstall as uninstall_api
    _fake_home(tmp_path, monkeypatch)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    try:
        target = _install_fixture(outside)
        monkeypatch.setattr(sys, "argv", [str(target)])
        with pytest.raises(AssertionError, match="outside test tmp"):
            uninstall_api.plan_self()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_self_refuses_a_root_without_install_layout(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    home = _fake_home(tmp_path, monkeypatch)
    bogus = home / ".local" / "ptest"
    bogus.mkdir(parents=True)
    (bogus / "notes.txt").write_text("user data\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    code = _uninstall(domain, "--self", "--yes")
    captured = capsys.readouterr()
    assert code != 0
    assert "not" in captured.err and "install" in captured.err
    assert (bogus / "notes.txt").read_text(encoding="utf-8") == "user data\n"


def test_self_prefers_a_valid_root_over_a_stale_candidate(
        case, tmp_path, monkeypatch):
    from ptest import uninstall as uninstall_api
    domain = case.domain()
    home = _fake_home(tmp_path, monkeypatch)
    inst = home / ".local" / "ptest"
    inst.mkdir(parents=True)
    _install_fixture(inst)
    stale = tmp_path / "stale"
    (stale / ".ptest-bundles" / "zzz").mkdir(parents=True)
    stale_target = stale / ".ptest-bundles" / "zzz" / "ptest"
    stale_target.write_text("x\n", encoding="utf-8")
    os.chmod(stale_target, 0o755)
    monkeypatch.setattr(sys, "argv", [str(stale_target)])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))

    plan = uninstall_api.plan_self()
    assert plan.root == inst
    assert plan.refused is None


def test_self_with_no_install_root_reports_nothing_to_remove(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    assert _uninstall(domain, "--self", "--yes") == 0
    assert "nothing to remove" in capsys.readouterr().out


# --- idempotence --------------------------------------------------------------

def test_second_run_reports_nothing_to_remove(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()
    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()

    assert _uninstall(domain, "--yes") == 0
    assert "nothing to remove" in capsys.readouterr().out


# --- JSON document ------------------------------------------------------------

def test_json_plan_and_result_are_a_public_document(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    assert main(("init", "--runner", "pytest", "--agents", "codex")) == 0
    capsys.readouterr()

    assert _uninstall(domain, "--yes", "--json") == 0
    raw = capsys.readouterr().out.encode("utf-8")
    doc = C.decode_public_document(raw)
    assert doc.kind == "uninstall"
    assert doc.error is None
    data = doc.data
    assert data["root"] == str(root)
    assert data["dry_run"] is False
    kinds = {entry["action"] for entry in data["plan"]}
    assert "remove" in kinds
    assert set(data["result"]) == {
        "removed", "kept", "skipped", "nothing_to_remove", "applied"}
    assert ".ptest.toml" in data["result"]["removed"]
    assert data["result"]["nothing_to_remove"] is False
    for entry in data["plan"]:
        assert set(entry) == {"action", "target", "detail"}
        assert entry["action"] in {"remove", "kept", "skipped"}
    assert set(data["self"]) == {
        "requested", "root", "removed", "path_symlink_removed", "kept"}


def test_json_error_keeps_the_document_contract(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    checkout = _checkout(root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="dd" * 16, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=True))
    monkeypatch.chdir(root)

    code = _uninstall(domain, "--yes", "--json")
    captured = capsys.readouterr()
    assert code != 0
    doc = C.decode_public_document(captured.out.encode("utf-8"))
    assert doc.kind == "uninstall"
    assert doc.data is None
    assert doc.error is not None


# --- help and README ----------------------------------------------------------

def test_help_uninstall_topic_and_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(("help", "uninstall")) == 0
    out = capsys.readouterr().out
    assert "--self" in out and "--yes" in out and "--dry-run" in out
    assert main(("uninstall", "--help")) == 0
    assert "--self" in capsys.readouterr().out


def test_overview_and_readme_document_uninstall(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(("help",)) == 0
    assert "uninstall" in capsys.readouterr().out
    readme = Path(__file__).resolve().parents[2] / "README.md"
    assert "ptest uninstall" in readme.read_text(encoding="utf-8")


# --- abuse twins ---------------------------------------------------------------

def test_hostile_monorepo_children_never_escape_the_root(
        case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"../evil\", \"/abs\", \"api\"]\n",
        encoding="utf-8")
    (root / "api").mkdir()
    (root / "api" / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + PROJ + "\"\n"
        "[runner]\nkind = \"command\"\nlauncher = [\"true\"]\n",
        encoding="utf-8")
    evil = tmp_path / "evil" / ".ptest.toml"
    evil.parent.mkdir()
    evil.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    # U4-4: invalid children are reported as skipped, exit stays 0.
    code = _uninstall(domain, "--yes")
    captured = capsys.readouterr()
    assert code == 0
    assert "skipped" in captured.out
    assert evil.read_bytes() == b"version = 1\n"
    assert not (root / "api" / ".ptest.toml").exists()
    assert not (root / ".ptest.toml").exists()


def test_symlinked_child_dir_is_never_entered(case, tmp_path, monkeypatch):
    domain = case.domain()
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _git(root)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\"]\n", encoding="utf-8")
    real = outside / "api"
    real.mkdir()
    (real / ".ptest.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "api").symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(root)

    # U4-4: the symlinked child is reported as skipped, exit stays 0.
    assert _uninstall(domain, "--yes") == 0
    assert (real / ".ptest.toml").read_bytes() == b"version = 1\n"


def test_block_rewrite_fails_closed_on_plan_apply_race(
        case, tmp_path, monkeypatch):
    from ptest import uninstall as uninstall_api
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    monkeypatch.chdir(root)
    block = agent_rules._block("AGENTS.md")
    (root / "AGENTS.md").write_text("# notes\n\n" + block, encoding="utf-8")
    from ptest import config as config_api
    plan = uninstall_api.plan_repo(
        config_api.repository_root(root), domain)
    assert any(entry.action == "remove" and entry.target == "AGENTS.md"
               for entry in plan.entries)
    (root / "AGENTS.md").write_text("# notes\nconcurrent edit\n",
                                    encoding="utf-8")

    with pytest.raises(C.Problem):
        uninstall_api.apply_repo(plan, domain)
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == (
        "# notes\nconcurrent edit\n")


# --- U4 audit findings ------------------------------------------------------

def test_self_keeps_user_files_and_the_root(
        case, tmp_path, monkeypatch, capsys):
    """U4-1 twin: a valid bundle plus Documents/thesis.tex.

    Only installer-created entries go; the user file and the root stay.
    """
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    docs = inst / "Documents"
    docs.mkdir()
    thesis = docs / "thesis.tex"
    thesis.write_text("\\documentclass{article}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    assert _uninstall(domain, "--self", "--yes") == 0
    capsys.readouterr()
    assert thesis.read_text(encoding="utf-8") == "\\documentclass{article}\n"
    assert inst.is_dir()
    assert not (inst / ".ptest-bundles").exists()
    assert not (inst / "ptest").exists()


def test_self_symlinked_home_still_removes_public_and_launcher_links(
        case, tmp_path, monkeypatch, capsys):
    """U5-5 twin: the passwd home sits behind a symlink.

    The default install root must be compared realpath-to-realpath, or
    neither `<root>/ptest` nor the launcher link resolves inside it.
    """
    real = tmp_path / "real-home"
    real.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(home)))
    inst = home / ".local" / "ptest"
    target = _install_fixture(inst)
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "ptest").symlink_to(target)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: str(bindir / "ptest"))
    monkeypatch.setattr(sys, "argv", ["ptest", "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    domain = case.domain()

    assert _uninstall(domain, "--self", "--yes") == 0
    capsys.readouterr()
    assert not os.path.lexists(inst / "ptest")
    assert not os.path.lexists(bindir / "ptest")
    assert not (real / ".local" / "ptest").exists()


def test_tty_self_consent_lists_kept_user_files(
        case, tmp_path, monkeypatch, capsys):
    """U5-3 twin: the interactive summary path shows --self kept files.

    A TTY `y` (no `--yes`) prints the plan before consent and only the
    summary after; the kept `Documents/thesis.tex` must be listed.
    """
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    thesis = inst / "Documents" / "thesis.tex"
    thesis.parent.mkdir()
    thesis.write_text("\\documentclass{article}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    _tty(monkeypatch, "y")

    assert _uninstall(domain, "--self") == 0
    out = capsys.readouterr().out
    assert "Documents" in out
    assert "kept user file" in out
    assert thesis.read_text(encoding="utf-8") == "\\documentclass{article}\n"


def test_active_run_in_child_refuses_uninstall(
        case, tmp_path, monkeypatch, capsys):
    """U4-2 twin: the active-run check covers every declared child id.

    State is seeded via the real ``operations._checkout`` for a child
    config, never a hand-built id.
    """
    from types import SimpleNamespace

    from ptest import operations
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\"]\n", encoding="utf-8")
    api = root / "api"
    api.mkdir()
    _v1(api)
    child = operations._checkout(SimpleNamespace(
        checkout=None, config_path=api / ".ptest.toml", project_id=PROJ))
    assert child.checkout_id == _checkout_id(api)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="ee" * 16, checkout=child, owner=owner, slots=1,
        exclusive=False, fixture=True))
    monkeypatch.chdir(root)
    before = _snapshot(root)

    code = _uninstall(domain, "--yes")
    captured = capsys.readouterr()
    assert code != 0
    assert "active" in captured.err
    assert _snapshot(root) == before
    assert (root / ".ptest.toml").is_file()
    assert (api / ".ptest.toml").is_file()


def test_child_checkout_state_and_ledger_rows_removed(
        case, tmp_path, monkeypatch, capsys):
    """U4-2 twin: each existing child id loses its state dir + ledger rows."""
    from types import SimpleNamespace

    from ptest import operations
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\"]\n", encoding="utf-8")
    api = root / "api"
    api.mkdir()
    _v1(api)
    child = operations._checkout(SimpleNamespace(
        checkout=None, config_path=api / ".ptest.toml", project_id=PROJ))
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="ef" * 16, checkout=child, owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(domain, ticket, owner) is True
    before = case.snapshot(digest="11" * 32, compatibility="compat-v1")
    after = case.snapshot(digest="22" * 32, compatibility="compat-v1")
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=after.digest,
                  compatibility=after.compatibility)
    result = case.result(sequence=1, plan=plan, input_before=before,
                         input_after=after, policy_digest="33" * 32,
                         project_id=child.project_id,
                         checkout_id=child.checkout_id)
    inventory = case.inventory(("tests/test_a.py",))
    published = history_api.publish_outcome(domain, child, result, inventory)
    assert published.committed is True
    assert (domain.root / "checkouts" / child.checkout_id).is_dir()
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    capsys.readouterr()
    assert not (domain.root / "checkouts" / child.checkout_id).exists()
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id"
    ).fetchall())
    assert child.checkout_id not in rows


def test_json_with_skipped_symlink_is_one_document(
        case, tmp_path, monkeypatch, capsys):
    """U4-3 twin: CLAUDE.md -> AGENTS.md symlink still yields one document."""
    import json as json_stdlib

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    (root / "AGENTS.md").write_text("# Agent notes\n", encoding="utf-8")
    (root / "CLAUDE.md").symlink_to(root / "AGENTS.md")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes", "--json") == 0
    raw = capsys.readouterr().out
    json_stdlib.loads(raw)
    doc = C.decode_public_document(raw.encode("utf-8"))
    assert doc.kind == "uninstall"
    assert doc.error is None
    assert any(entry["action"] == "skipped"
               for entry in doc.data["plan"])


def test_skipped_entries_exit_zero(
        case, tmp_path, monkeypatch, capsys):
    """U4-4: skipped entries are informational; safe removals still exit 0."""
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    (root / "AGENTS.md").write_text(
        "# notes\n<!-- ptest-agent-rules:start -->\nunbalanced\n",
        encoding="utf-8")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert not (root / ".ptest.toml").exists()
    assert (root / "AGENTS.md").is_file()


def test_no_artifacts_with_symlinked_guide_exits_zero(
        case, tmp_path, monkeypatch, capsys):
    """U4-4: no ptest artifacts plus a symlinked CLAUDE.md exits 0."""
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    (root / "AGENTS.md").write_text("# Agent notes\n", encoding="utf-8")
    (root / "CLAUDE.md").symlink_to(root / "AGENTS.md")
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes") == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_forget_checkouts_refuses_live_rows_and_deletes_nothing(
        case, tmp_path):
    """U4-6 twin: one live row refuses the whole forget; nothing deleted."""
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    mine = _checkout(root)
    foreign_root = domain.root / "foreign"
    foreign_root.mkdir()
    foreign = C.CheckoutIdentity(
        project_id=OTHER_PROJ, checkout_id="00" * 16, root=foreign_root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="f0" * 16, checkout=mine, owner=owner, slots=1,
        exclusive=False, fixture=True))
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="f1" * 16, checkout=foreign, owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(domain, ticket, owner) is True

    with pytest.raises(C.Problem) as caught:
        scheduler.forget_checkouts(domain, [mine.checkout_id,
                                            foreign.checkout_id])
    assert caught.value.code == "active-run"
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id"
    ).fetchall())
    assert rows.get(mine.checkout_id, 0) >= 1
    assert rows.get(foreign.checkout_id, 0) >= 1


def test_forget_checkouts_deletes_only_terminal_rows(case, tmp_path):
    """U4-6 twin: terminal rows for the ids go; foreign rows survive."""
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    mine = _checkout(root)
    foreign_root = domain.root / "foreign"
    foreign_root.mkdir()
    foreign = C.CheckoutIdentity(
        project_id=OTHER_PROJ, checkout_id="00" * 16, root=foreign_root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    for run_id, checkout in (("f2" * 16, mine), ("f3" * 16, foreign)):
        ticket = scheduler.enqueue(domain, C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=True))
        assert scheduler.cancel_pending(domain, ticket, owner) is True

    deleted = scheduler.forget_checkouts(domain, [mine.checkout_id])
    assert deleted >= 1
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id"
    ).fetchall())
    assert mine.checkout_id not in rows
    assert rows.get(foreign.checkout_id, 0) >= 1


def test_forget_checkouts_recovers_rebooted_rows_then_uninstall_applies(
        case, tmp_path, monkeypatch):
    """U5-1 twin: a row from a previous boot is recovered, not live.

    After a simulated reboot the queued row is terminal, so the locked
    forget succeeds and the uninstall applies.
    """
    monkeypatch.setattr(platform_api, "boot_identity", lambda: "boot-a")
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    mine = _checkout(root)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="e1" * 16, checkout=mine, owner=owner, slots=1,
        exclusive=False, fixture=True))
    (domain.root / "checkouts" / mine.checkout_id).mkdir(parents=True)
    monkeypatch.setattr(platform_api, "boot_identity", lambda: "boot-b")

    assert scheduler.forget_checkouts(domain, [mine.checkout_id]) >= 1
    monkeypatch.chdir(root)
    assert _uninstall(domain, "--yes") == 0
    assert not (domain.root / "checkouts" / mine.checkout_id).exists()


def test_apply_refuses_row_inserted_after_plan_for_child_without_ledger_rows(
        case, tmp_path, monkeypatch):
    """U5-2 twin: the locked forget covers every planned id.

    A child state dir exists with no ledger rows at plan time; a live
    row inserted between plan and apply still refuses, and the dir
    stays intact.
    """
    from types import SimpleNamespace

    from ptest import operations
    from ptest import uninstall as uninstall_api
    domain = case.domain(slots=2, jobs=2)
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\"]\n", encoding="utf-8")
    api = root / "api"
    api.mkdir()
    _v1(api)
    child = operations._checkout(SimpleNamespace(
        checkout=None, config_path=api / ".ptest.toml", project_id=PROJ))
    assert child.checkout_id == _checkout_id(api)
    child_dir = domain.root / "checkouts" / child.checkout_id
    child_dir.mkdir(parents=True)
    plan = uninstall_api.plan_repo(root, domain)
    assert child.checkout_id in plan.checkout_ids
    assert not any(entry.kind == "ledger" and entry.scope == child.checkout_id
                   for entry in plan.entries)
    calls: list = []
    real_forget = scheduler.forget_checkouts

    def _spy(dom, ids):
        calls.append(list(ids))
        return real_forget(dom, ids)

    monkeypatch.setattr(scheduler, "forget_checkouts", _spy)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="e2" * 16, checkout=child, owner=owner, slots=1,
        exclusive=False, fixture=True))
    with pytest.raises(C.Problem) as caught:
        uninstall_api.apply_repo(plan, domain)
    assert caught.value.code == "active-run"
    assert calls and calls[0] == list(plan.checkout_ids)
    assert child_dir.is_dir()


def test_dry_run_never_creates_or_writes_the_ledger(
        case, tmp_path, monkeypatch, capsys):
    """U4-6 guard: planning opens the ledger read-only, or not at all."""
    domain = case.domain()
    root = domain.root / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)

    assert not domain.ledger.exists()
    assert _uninstall(domain, "--dry-run") == 0
    capsys.readouterr()
    assert not domain.ledger.exists()

    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="f4" * 16, checkout=_checkout(root), owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(domain, ticket, owner) is True
    before = domain.ledger.read_bytes()
    assert _uninstall(domain, "--dry-run") == 0
    capsys.readouterr()
    assert domain.ledger.read_bytes() == before


def test_self_render_never_truncates_root_or_link(tmp_path, monkeypatch):
    """U4-8: the install root and PATH link wrap instead of truncating."""
    from ptest import uninstall as uninstall_api
    _fake_home(tmp_path, monkeypatch)
    long_root = Path("/tmp") / ("very-long-install-dir-name-" * 6)
    long_link = Path("/tmp") / ("very-long-link-dir-name-" * 6) / "ptest"
    plan = uninstall_api.RepoPlan(root=Path("/repo"),
                                  checkout_id="ab" * 16, entries=())
    self_plan = uninstall_api.SelfPlan(
        requested=True, root=long_root, path_link=str(long_link))

    out = uninstall_api.render_text(plan, width=40, self_plan=self_plan)
    assert "…" not in out
    joined = "".join(line.strip() for line in out.splitlines())
    assert str(long_root) in joined
    assert str(long_link) in joined


def test_self_result_reports_kept_user_files(
        case, tmp_path, monkeypatch, capsys):
    """U4-8/U4-1: leftover user files are reported as kept in text output."""
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    (inst / "Documents").mkdir()
    (inst / "Documents" / "thesis.tex").write_text("x\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    assert _uninstall(domain, "--self", "--yes") == 0
    out = capsys.readouterr().out
    assert "Documents" in out and "kept" in out


def test_plan_self_ignores_path_lookup_for_roots(tmp_path, monkeypatch):
    """U4-7: a root reachable only via PATH is never planned."""
    from ptest import uninstall as uninstall_api
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ptest").symlink_to(target)
    monkeypatch.setenv("PATH", str(bindir))
    stub = tmp_path / "argv-stub"
    stub.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(stub)])
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: str(bindir / "ptest"))

    plan = uninstall_api.plan_self()
    assert plan.root is None
    assert plan.refused is None


def test_plan_self_roots_come_only_from_binary_or_default(
        tmp_path, monkeypatch):
    """U4-7: PATH + the known launcher feed only the PATH-link check."""
    from ptest import uninstall as uninstall_api
    home = _fake_home(tmp_path, monkeypatch)
    inst = home / ".local" / "ptest"
    inst.mkdir(parents=True)
    _install_fixture(inst)
    other = tmp_path / "other-inst"
    other.mkdir()
    other_target = _install_fixture(other, bundle_id="8" * 8)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ptest").symlink_to(other_target)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(
        shutil, "which", lambda *args, **kwargs: str(bindir / "ptest"))
    launcher_dir = home / ".local" / "bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "ptest"
    launcher.symlink_to(inst / "ptest")
    stub = tmp_path / "argv-stub"
    stub.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(stub)])

    plan = uninstall_api.plan_self()
    assert plan.root == inst
    assert plan.refused is None
    assert plan.path_link == str(launcher)


def test_json_self_requested_reflects_the_flag(
        case, tmp_path, monkeypatch, capsys):
    """U4-5: self.requested is the real flag, asserted both ways."""
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--yes", "--json") == 0
    plain = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert plain.data["self"]["requested"] is False

    assert _uninstall(domain, "--self", "--yes", "--json") == 0
    with_self = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert with_self.data["self"]["requested"] is True


def test_json_without_yes_returns_the_plan_without_prompt(
        case, tmp_path, monkeypatch, capsys):
    """U4-9: --json without --yes/--dry-run never prompts.

    Exactly one document: the success document with applied=false,
    exit non-zero.
    """
    import json as json_stdlib

    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr("builtins.input",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("must never prompt")))

    code = _uninstall(domain, "--json")
    raw = capsys.readouterr().out
    assert code != 0
    json_stdlib.loads(raw)
    doc = C.decode_public_document(raw.encode("utf-8"))
    assert doc.error is None
    assert doc.data["result"]["applied"] is False
    assert any(entry["target"] == ".ptest.toml"
               for entry in doc.data["plan"])
    assert (root / ".ptest.toml").is_file()


def test_self_refuses_when_a_bundle_entry_is_invalid(
        case, tmp_path, monkeypatch, capsys):
    """U4-1 twin: one bad .ptest-bundles entry refuses the whole --self."""
    domain = case.domain()
    _fake_home(tmp_path, monkeypatch)
    inst = tmp_path / "inst"
    inst.mkdir()
    target = _install_fixture(inst)
    (inst / ".ptest-bundles" / "junk.txt").write_text("not a bundle\n",
                                                     encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(target), "uninstall"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    code = _uninstall(domain, "--self", "--yes")
    captured = capsys.readouterr()
    assert code != 0
    assert "bundle" in captured.err
    assert (inst / "ptest").is_symlink()
    assert (inst / ".ptest-bundles" / "99999999").is_dir()





# --- PTEST_STATE_DIR ----------------------------------------------------------

def _fake_account(tmp_path: Path, monkeypatch) -> Path:
    """Private account home whose `.local` must stay untouched by the run."""
    home = tmp_path / "account"
    home.mkdir(mode=0o700, exist_ok=True)
    local = home / ".local"
    local.mkdir(mode=0o770, exist_ok=True)
    local.chmod(0o770)
    monkeypatch.setattr(
        pwd, "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(home)))
    return home


def _workspace_domain(monkeypatch, state: Path):
    from ptest import platform as platform_api
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    return platform_api.domain_paths(None)


def test_uninstall_text_states_workspace_state_location(
        tmp_path, monkeypatch, capsys):
    """P3: the text plan names the inspected state dir + PTEST_STATE_DIR."""
    _fake_account(tmp_path, monkeypatch)
    state = tmp_path / "workspace-state"
    _workspace_domain(monkeypatch, state)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)

    assert main(("uninstall", "--dry-run")) == 0
    out = capsys.readouterr().out
    flat = "".join(line.strip() for line in out.splitlines())
    assert f"state: {state / 'coordination'} (PTEST_STATE_DIR)" in flat


def test_uninstall_json_reports_workspace_state_location(
        tmp_path, monkeypatch, capsys):
    """P3: the JSON plan carries domain_root/domain_from_env like where."""
    _fake_account(tmp_path, monkeypatch)
    state = tmp_path / "workspace-state"
    _workspace_domain(monkeypatch, state)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)

    assert main(("uninstall", "--dry-run", "--json")) == 0
    doc = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert doc.kind == "uninstall"
    assert doc.data["domain_root"] == str(state / "coordination")
    assert doc.data["domain_from_env"] is True


def test_uninstall_reports_fixture_domain_without_env_marker(
        case, tmp_path, monkeypatch, capsys):
    """P3: without PTEST_STATE_DIR the plan still states the location."""
    domain = case.domain()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root)
    _v1(root)
    monkeypatch.chdir(root)

    assert _uninstall(domain, "--dry-run") == 0
    flat = "".join(
        line.strip() for line in capsys.readouterr().out.splitlines())
    assert f"state: {domain.root}" in flat
    assert "(PTEST_STATE_DIR)" not in flat

    assert _uninstall(domain, "--dry-run", "--json") == 0
    doc = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert doc.data["domain_root"] == str(domain.root)
    assert doc.data["domain_from_env"] is False


def test_state_dir_workspace_domain_scoped_removal(
        case, tmp_path, monkeypatch, capsys):
    """P3 twin: only this checkout's entries go in the PTEST_STATE_DIR domain.

    Removed: this checkout's ``checkouts/<id>/`` dir and ledger rows.
    Kept: machine.toml, the domain marker, review-models/, another
    checkout's dir + rows, coordination/ and the state dir itself, and
    the default account (fixture) domain, untouched.
    """
    home = _fake_account(tmp_path, monkeypatch)
    state = tmp_path / "workspace-state"
    domain = _workspace_domain(monkeypatch, state)
    root = tmp_path / "repo-one"
    root.mkdir()
    _git(root)
    _v1(root)
    other_root = tmp_path / "repo-two"
    other_root.mkdir()
    _git(other_root)
    _v1(other_root, OTHER_PROJ)
    owner = platform_api.process_identity(os.getpid())
    assert owner is not None
    for checkout, run_id in ((_checkout(root), "ee" * 16),
                             (_checkout(other_root, OTHER_PROJ), "ff" * 16)):
        ticket = scheduler.enqueue(domain, C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=False))
        assert scheduler.cancel_pending(domain, ticket, owner) is True
    mine = _snapshot_case(case, domain, root)
    other = _snapshot_case(case, domain, other_root, OTHER_PROJ)
    models = domain.root / "review-models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "model.bin").write_bytes(b"cached\n")
    fixture_domain = case.domain(slots=2, jobs=2)
    fixture_repo = fixture_domain.root / "fixture-repo"
    fixture_repo.mkdir()
    _git(fixture_repo)
    sheltered = _snapshot_case(case, fixture_domain, fixture_repo)
    sheltered_ticket = scheduler.enqueue(fixture_domain, C.AdmissionRequest(
        run_id="aa" * 16, checkout=sheltered, owner=owner, slots=1,
        exclusive=False, fixture=True))
    assert scheduler.cancel_pending(
        fixture_domain, sheltered_ticket, owner) is True
    monkeypatch.chdir(root)

    assert main(("uninstall", "--yes")) == 0
    flat = "".join(
        line.strip() for line in capsys.readouterr().out.splitlines())
    assert f"state: {domain.root} (PTEST_STATE_DIR)" in flat

    assert not (domain.root / "checkouts" / mine.checkout_id).exists()
    rows = dict(sqlite3.connect(domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id"
    ).fetchall())
    assert mine.checkout_id not in rows
    assert rows.get(other.checkout_id, 0) >= 1
    assert (domain.root / "checkouts" / other.checkout_id).is_dir()
    assert (state / "machine.toml").is_file()
    assert domain.marker.is_file()
    assert (models / "model.bin").is_file()
    assert domain.root.is_dir()
    assert state.is_dir()
    assert (fixture_domain.root / "checkouts" / sheltered.checkout_id
            ).is_dir()
    sheltered_rows = dict(sqlite3.connect(fixture_domain.ledger).execute(
        "SELECT checkout_id, COUNT(*) FROM jobs GROUP BY checkout_id"
    ).fetchall())
    assert sheltered_rows.get(sheltered.checkout_id, 0) >= 1
    assert list((home / ".local").iterdir()) == []


def test_help_and_readme_name_state_dir_for_uninstall(
        tmp_path, monkeypatch, capsys):
    """P3 docs: help + README say to reuse the run's PTEST_STATE_DIR."""
    monkeypatch.chdir(tmp_path)
    assert main(("help", "uninstall")) == 0
    assert "PTEST_STATE_DIR" in capsys.readouterr().out
    assert main(("uninstall", "--help")) == 0
    assert "PTEST_STATE_DIR" in capsys.readouterr().out
    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "ptest uninstall" in text
    assert "PTEST_STATE_DIR" in text
