"""Workspace state must stay local through inspection, review and execution."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ptest import config as config_api, contracts as C, operations, platform, scheduler
from ptest.cli import main


@pytest.fixture
def account(monkeypatch, tmp_path):
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    local = home / ".local"
    local.mkdir(mode=0o770)
    local.chmod(0o770)
    monkeypatch.setattr(platform.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_dir=str(home)))
    return home


def test_state_directory_resolves_without_touching_account_or_creating_state(
        account, tmp_path, monkeypatch):
    state = tmp_path / "local state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))

    domain = platform.domain_paths(None)

    assert domain.root == state / "coordination"
    assert domain.machine_config == state / "machine.toml"
    assert domain.ledger == state / "coordination" / "coordinator.sqlite3"
    assert domain.marker == state / "coordination" / "domain.json"
    assert not domain.fixture
    assert not state.exists()
    assert list((account / ".local").iterdir()) == []
    assert stat.S_IMODE((account / ".local").stat().st_mode) == 0o770


@pytest.mark.parametrize("value", ["", "relative", "~/state", "/", "/tmp/../state", "/tmp/state\n"])
def test_state_directory_rejects_invalid_paths(account, monkeypatch, value):
    monkeypatch.setenv("PTEST_STATE_DIR", value)
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "unsafe-path"
    assert "PTEST_STATE_DIR" in caught.value.message


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_state_directory_rejects_existing_nonprivate_root_without_chmod(
        account, tmp_path, monkeypatch, mode):
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(mode)
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "unsafe-path"
    assert stat.S_IMODE(state.stat().st_mode) == mode
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("target", ["root", "parent"])
def test_state_directory_rejects_symlink_components(
        account, tmp_path, monkeypatch, target):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    state = link if target == "root" else link / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "unsafe-path"
    assert list(real.iterdir()) == []


def test_state_directory_requires_existing_owned_parent(account, tmp_path, monkeypatch):
    state = tmp_path / "missing" / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "state-unavailable"
    assert not state.parent.exists()


def test_state_directory_rejects_writable_parent(account, tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o770)
    monkeypatch.setenv("PTEST_STATE_DIR", str(parent / "state"))
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "unsafe-path"
    assert list(parent.iterdir()) == []


def test_fixture_domain_takes_precedence_over_state_environment(case, monkeypatch):
    domain = case.domain()
    monkeypatch.setenv("PTEST_STATE_DIR", "invalid-relative-path")
    assert platform.domain_paths(domain.root) == domain


@pytest.mark.parametrize("name", ["machine.toml", "coordination", "coordination/domain.json"])
def test_state_directory_rejects_unsafe_existing_metadata(
        account, tmp_path, monkeypatch, name):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    if name.startswith("coordination/"):
        (state / "coordination").mkdir(mode=0o700)
    target = state / name
    target.write_text("foreign metadata")
    target.chmod(0o644)
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    with pytest.raises(C.Problem) as caught:
        platform.domain_paths(None)
    assert caught.value.code == "unsafe-path"
    assert target.read_text() == "foreign metadata"


def test_state_directory_scheduler_creates_only_local_private_state(
        account, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    domain = platform.domain_paths(None)
    owner = platform.process_identity(os.getpid())
    assert owner is not None
    request = C.AdmissionRequest(
        run_id="ab" * 16,
        checkout=C.CheckoutIdentity(project_id="cd" * 16,
                                    checkout_id="ef" * 16, root=tmp_path),
        owner=owner, slots=1, exclusive=False, fixture=False,
    )

    ticket = scheduler.enqueue(domain, request)
    assert scheduler.poll(domain, ticket).grant is not None
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(domain.root.stat().st_mode) == 0o700
    for path in (domain.machine_config, domain.marker, domain.ledger):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (account / ".config").exists()
    assert list((account / ".local").iterdir()) == []


def test_offline_doctor_with_local_state_is_read_only(
        account, tmp_path, monkeypatch, capsys):
    from test_doctor_init_integration import _write_db_standalone_repo

    root = tmp_path / "project"
    root.mkdir()
    _write_db_standalone_repo(root)
    state = tmp_path / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.chdir(root)

    assert main(("doctor", "--offline", "--json")) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["error"] is None
    assert document["kind"] == "agent-assessment"
    assert document["data"]["provider"]["name"] == "offline"
    assert not state.exists()
    assert not (root / "recommendations.md").exists()


def test_online_doctor_creates_local_review_cache_after_consent(
        account, tmp_path, monkeypatch, capsys):
    from test_doctor_init_integration import _prepare_review, _write_db_standalone_repo

    root = tmp_path / "project"
    root.mkdir()
    _write_db_standalone_repo(root)
    state = tmp_path / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _prepare_review(monkeypatch, root, tmp_path / "bin")

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review")) == 0
    assert (state / "coordination" / "review-models").is_dir()
    assert (root / "recommendations.md").is_file()
    assert not (account / ".config").exists()
    assert list((account / ".local").iterdir()) == []


def test_state_directory_reaches_guard_and_preserves_runner_exit_status(
        tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        '[runner]\nkind = "command"\n'
        f'launcher = {json.dumps([sys.executable, "runner.py"])}\n'
        'lifecycle = "cooperative-process-group"\n', encoding="utf-8")
    (root / "runner.py").write_text(
        "from pathlib import Path\n"
        "Path('ran').write_text('executed')\n"
        "raise SystemExit(7)\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "ptest", "--full"], cwd=root,
        capture_output=True, text=True, timeout=30)

    assert completed.returncode == 7, completed.stdout + completed.stderr
    assert (root / "ran").read_text() == "executed"
    assert (state / "machine.toml").is_file()
    assert (state / "coordination" / "coordinator.sqlite3").is_file()


def _write_trivial_command_project(root):
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + "ab" * 16 + '"\n'
        "[runner]\nkind = \"command\"\n"
        f"launcher = {json.dumps([sys.executable, '-c', 'pass'])}\n"
        "args = []\nfull_args = []\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")


def _git_init_fixture(root):
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test",
               GIT_COMMITTER_EMAIL="fixture@example.test")
    prefix = ("git", "-c", "core.hooksPath=" + os.devnull,
              "-c", "commit.gpgsign=false", "-C", str(root))
    subprocess.run((*prefix, "init"), env=env, check=True,
                   capture_output=True)
    subprocess.run((*prefix, "add", "."), env=env, check=True,
                   capture_output=True)
    subprocess.run((*prefix, "commit", "-m", "fixture"), env=env,
                   check=True, capture_output=True)


def test_state_directory_inside_checkout_refuses_before_admission(
        account, tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    _write_trivial_command_project(root)
    _git_init_fixture(root)
    state = root / ".ptest-state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    domain = platform.domain_paths(None)
    config = config_api.resolve_config(root).config
    assert config is not None

    with pytest.raises(C.Problem) as caught:
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert caught.value.code == "unsafe-path"
    assert "PTEST_STATE_DIR must be outside the repository" in caught.value.message
    assert not state.exists()

    monkeypatch.chdir(root)
    assert main(("--full",)) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert "PTEST_STATE_DIR must be outside the repository" in captured.err
    assert not state.exists()


def _write_marker_command_project(root, index=0):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + f"{index:032x}" + '"\n'
        "[runner]\nkind = \"command\"\n"
        f"launcher = {json.dumps([sys.executable, str(root / 'run.py')])}\n"
        "args = []\nfull_args = []\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    (root / "run.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('ran').write_text('executed')\n",
        encoding="utf-8")


def _write_monorepo(root, declarations=("a", "b")):
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = "
        + json.dumps(list(declarations)) + "\n",
        encoding="utf-8")
    for index, child in enumerate(declarations):
        _write_marker_command_project(root / child, index)


def test_state_directory_at_monorepo_root_refuses_before_any_child_runs(
        account, tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    _write_monorepo(root)
    _git_init_fixture(root)
    state = root / ".ptest-state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.chdir(root)

    assert main(("--full",)) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert "PTEST_STATE_DIR must be outside the repository" in captured.err
    assert not state.exists()
    assert not (root / "a" / "ran").exists()
    assert not (root / "b" / "ran").exists()


def test_state_directory_inside_later_child_refuses_before_first_child_runs(
        account, tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    _write_monorepo(root)
    _git_init_fixture(root)
    state = root / "b" / ".ptest-state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.chdir(root)

    assert main(("--full",)) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert "PTEST_STATE_DIR must be outside the repository" in captured.err
    assert not state.exists()
    assert not (root / "a" / "ran").exists()
    assert not (root / "b" / "ran").exists()


def test_state_directory_at_git_root_refuses_nested_config(
        account, tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    sub = root / "sub"
    sub.mkdir(parents=True)
    _write_trivial_command_project(sub)
    _git_init_fixture(root)
    state = root / ".ptest-state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    domain = platform.domain_paths(None)
    config = config_api.resolve_config(sub).config
    assert config is not None

    with pytest.raises(C.Problem) as caught:
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert caught.value.code == "unsafe-path"
    assert "PTEST_STATE_DIR must be outside the repository" in caught.value.message
    assert not state.exists()

    monkeypatch.chdir(sub)
    assert main(("--full",)) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert "PTEST_STATE_DIR must be outside the repository" in captured.err
    assert not state.exists()


def test_state_directory_outside_checkout_runs_without_exit_70(
        account, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write_trivial_command_project(root)
    _git_init_fixture(root)
    state = tmp_path / "state-outside"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    domain = platform.domain_paths(None)
    config = config_api.resolve_config(root).config
    assert config is not None

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))

    assert result.exit_code == 0
    assert (state / "machine.toml").is_file()
    assert (state / "coordination" / "coordinator.sqlite3").is_file()


def test_doctor_review_with_state_inside_checkout_refuses_without_writing(
        account, tmp_path, monkeypatch, capsys):
    from test_doctor_init_integration import _prepare_review, _write_db_standalone_repo

    root = tmp_path / "project"
    root.mkdir()
    _write_db_standalone_repo(root)
    state = root / ".ptest-state"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    _prepare_review(monkeypatch, root, tmp_path / "bin")

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review")) == 2
    captured = capsys.readouterr()
    assert "unsafe-path" in captured.err
    assert "PTEST_STATE_DIR must be outside the repository" in captured.err
    assert not state.exists()
    assert not (root / "recommendations.md").exists()


def test_where_and_status_show_domain_root(case, monkeypatch, capsys):
    domain = case.domain()
    root = case.project(domain, kind="command")
    monkeypatch.chdir(root)

    assert main(("--fixture-domain", str(domain.root), "where")) == 0
    text = capsys.readouterr().out
    assert f"domain: {domain.root}" in text
    assert "PTEST_STATE_DIR" not in text

    assert main(("--fixture-domain", str(domain.root), "where", "--json")) == 0
    payload = C.decode_public_document(capsys.readouterr().out).data
    assert payload["domain_root"] == str(domain.root)
    assert payload["domain_from_env"] is False

    assert main(("--fixture-domain", str(domain.root), "status")) == 0
    assert f"domain: {domain.root}" in capsys.readouterr().out

    assert main(("--fixture-domain", str(domain.root), "status", "--json")) == 0
    payload = C.decode_public_document(capsys.readouterr().out).data
    assert payload["domain_root"] == str(domain.root)
    assert payload["domain_from_env"] is False


def test_where_and_status_note_env_selected_domain(
        account, tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    _write_trivial_command_project(root)
    state = tmp_path / "state-outside"
    monkeypatch.setenv("PTEST_STATE_DIR", str(state))
    monkeypatch.chdir(root)

    assert main(("where",)) == 0
    text = capsys.readouterr().out
    assert f"domain: {state / 'coordination'}" in text
    assert "PTEST_STATE_DIR" in text

    assert main(("where", "--json")) == 0
    payload = C.decode_public_document(capsys.readouterr().out).data
    assert payload["domain_root"] == str(state / "coordination")
    assert payload["domain_from_env"] is True

    assert main(("status",)) == 0
    text = capsys.readouterr().out
    assert f"domain: {state / 'coordination'}" in text
    assert "PTEST_STATE_DIR" in text

    assert main(("status", "--json")) == 0
    payload = C.decode_public_document(capsys.readouterr().out).data
    assert payload["domain_root"] == str(state / "coordination")
    assert payload["domain_from_env"] is True
    assert not state.exists()
