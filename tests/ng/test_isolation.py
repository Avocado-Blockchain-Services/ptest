"""Isolation foundation proofs (T2 owned).

Every test here runs under the autouse ``isolated_env`` fixture from
``conftest.py``: a private HOME, XDG dirs, passwd home and git config
below a per-test ``iso`` root, with inherited orchestrator, xdist,
coverage and git control variables stripped.
"""
from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

import pytest

import conftest
from ptest import config as config_api
from ptest import platform as platform_api
from support import (
    CONTROL_VARS,
    build_isolated_env,
    git,
    init_git_repo,
    ptest_toml_text,
    write_ptest_toml,
)


def test_isolated_env_redirects_home_xdg_passwd_home_and_git_config(
        isolated_env, tmp_path):
    assert Path(os.environ["HOME"]) == isolated_env.home
    assert isolated_env.home.is_dir()
    assert stat.S_IMODE(isolated_env.home.stat().st_mode) == 0o700
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                "XDG_STATE_HOME"):
        assert Path(os.environ[var]).is_dir()
        assert isolated_env.root in Path(os.environ[var]).parents
    assert (pwd.getpwuid(os.getuid()).pw_dir
            == str(isolated_env.home))
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).is_file()
    assert (Path(os.environ["GIT_CONFIG_GLOBAL"]).parent
            == isolated_env.root)
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ.get("PTEST_STATE_DIR") is None
    assert isolated_env.state_dir.is_dir()
    # The iso root is a sibling of this test's tmp_path, never inside it.
    assert isolated_env.root.parent == tmp_path.parent
    assert isolated_env.root != tmp_path


def test_normal_domain_resolves_under_isolated_home_and_env_selects_state(
        isolated_env, monkeypatch):
    domain = platform_api.domain_paths(None)
    assert isolated_env.home in domain.root.parents
    assert platform_api.configured_state_directory() is None

    monkeypatch.setenv("PTEST_STATE_DIR", str(isolated_env.state_dir))
    assert (platform_api.configured_state_directory()
            == isolated_env.state_dir)


def test_isolated_roots_are_private_and_fresh_a(isolated_env):
    assert stat.S_IMODE(isolated_env.root.stat().st_mode) == 0o700
    assert {path.name for path in isolated_env.root.iterdir()} == {
        "home", "state", "xdg", "gitconfig"}


def test_isolated_roots_are_private_and_fresh_b(isolated_env):
    assert stat.S_IMODE(isolated_env.root.stat().st_mode) == 0o700
    assert {path.name for path in isolated_env.root.iterdir()} == {
        "home", "state", "xdg", "gitconfig"}


def test_two_built_envs_have_distinct_roots(tmp_path):
    first = build_isolated_env(tmp_path / "first")
    second = build_isolated_env(tmp_path / "second")
    assert first.root != second.root
    assert first.home != second.home
    assert first.state_dir != second.state_dir
    assert first.environ["HOME"] != second.environ["HOME"]


def test_isolated_env_strips_inherited_control_vars(isolated_env):
    # Under xdist the worker process inherits PYTEST_XDIST_WORKER and the
    # coverage tier sets COV_CORE_*; without stripping they would leak
    # into nested children.  Their absence proves the strip ran.
    stripped = (set(CONTROL_VARS) | {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
                | {"PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT"})
    for name in stripped:
        assert os.environ.get(name) is None, name
    # GIT_CONFIG_GLOBAL/GIT_CONFIG_NOSYSTEM are set BY the isolation
    # itself (the private gitconfig); nothing else GIT_* may survive.
    for name in tuple(os.environ):
        if name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            continue
        assert not name.startswith(
            ("PTEST_", "GIT_", "PYTEST_XDIST_", "COV_CORE_")), name
    # The strip predicate itself, against a synthetic inherited environ.
    inherited = {"PTEST_RUN_ID": "x", "GIT_DIR": "y",
                 "PYTEST_XDIST_WORKER": "gw0", "COV_CORE_SOURCE": "z",
                 "PYTEST_ADDOPTS": "-n0", "PYTEST_PLUGINS": "p",
                 "PATH": "/usr/bin", "HOME": "/elsewhere"}
    survivors = {name for name in inherited
                 if not (name in CONTROL_VARS
                         or name in conftest._STRIPPED_NAMES
                         or name.startswith(conftest._STRIPPED_PREFIXES))}
    assert survivors == {"PATH", "HOME"}


def test_snapshot_path_detects_creation_mtime_change_and_relink(tmp_path):
    target = tmp_path / "watched"
    assert conftest._snapshot_path(target)[0] is False

    target.write_bytes(b"v1")
    created = conftest._snapshot_path(target)
    assert created[0] is True

    stamp = target.stat()
    target.write_bytes(b"v2 longer")
    os.utime(target, ns=(stamp.st_atime_ns + 1_000_000,
                         stamp.st_mtime_ns + 1_000_000))
    changed = conftest._snapshot_path(target)
    assert changed[0] is True
    assert changed != created

    link = tmp_path / "link"
    link.symlink_to(target)
    linked = conftest._snapshot_path(link)
    assert linked[2] == str(target)
    other = tmp_path / "other"
    other.write_bytes(b"o")
    link.unlink()
    link.symlink_to(other)
    assert conftest._snapshot_path(link) != linked


def test_ptest_toml_text_parses_for_command_and_pytest(tmp_path):
    command_root = tmp_path / "command"
    command_root.mkdir()
    write_ptest_toml(command_root, kind="command")
    command = config_api.resolve_config(command_root).config
    assert command is not None
    assert command.runner.kind.value == "command"

    pytest_root = tmp_path / "pytest-proj"
    pytest_root.mkdir()
    write_ptest_toml(pytest_root, kind="pytest", launcher=("pytest",),
                     args=("-q",))
    resolved = config_api.resolve_config(pytest_root).config
    assert resolved is not None
    assert resolved.runner.kind.value == "pytest"
    assert resolved.runner.test_roots == ("tests",)
    assert "project_id" in ptest_toml_text()


def test_init_git_repo_makes_one_main_commit_with_fixture_identity(tmp_path):
    root = init_git_repo(tmp_path / "repo", files={"a.txt": "a\n"})
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(root, "rev-list", "--count", "HEAD") == "1"
    assert (git(root, "log", "--format=%an <%ae>", "-1")
            == "Fixture <fixture@example.test>")


def test_domain_factory_delegates_to_case_domains(domain_factory):
    first = domain_factory()
    assert first.marker.is_file()
    assert first.fixture is True
    second = domain_factory(slots=2, jobs=3)
    assert second.root != first.root
    with pytest.raises(TypeError):
        domain_factory(bogus_option=1)


def test_account_home_repoints_passwd_home(account_home):
    home = account_home()
    assert home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert pwd.getpwuid(os.getuid()).pw_dir == str(home)
    other = account_home(name="other")
    assert pwd.getpwuid(os.getuid()).pw_dir == str(other)
    with pytest.raises(TypeError):
        account_home(bogus_option=1)


def test_state_dir_factory_sets_private_ptest_state_dir(
        state_dir_factory, monkeypatch):
    state = state_dir_factory()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert os.environ["PTEST_STATE_DIR"] == str(state)
    monkeypatch.delenv("PTEST_STATE_DIR")
    assert platform_api.configured_state_directory() is None
    with pytest.raises(TypeError):
        state_dir_factory(bogus_option=1)


def test_session_guard_registered_on_every_xdist_worker(request, worker_id):
    # This suite always runs under `-n auto` (pyproject addopts), so a
    # gw* worker id proves the xdist context the guard must cover.
    assert worker_id.startswith("gw")
    defs = request.session._fixturemanager.getfixturedefs(
        "_guard_real_install_against_self_uninstall", request._pyfuncitem)
    assert defs
    assert any(d.scope == "session" and getattr(d, "_autouse", False)
               for d in defs)
    # The snapshot inputs are captured at import, before any test runs.
    assert conftest._REAL_INSTALL_ROOT.is_absolute()
    assert conftest._REAL_PATH_LINK.is_absolute()
