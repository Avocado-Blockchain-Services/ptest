"""Shared NG fixtures (test-only).

* ``isolated_env`` (autouse): every test runs with HOME, XDG_*, the passwd
  account home and the git global config redirected into a private
  per-test directory, after inherited orchestrator (including
  PTEST_STATE_DIR), xdist, coverage and git control variables are
  removed.  Nothing is shared between tests or xdist workers except the
  read-mostly tool caches in ``support.REAL_TOOL_ENV``.
* ``case``: a :class:`support.CaseFactory` rooted at the test's tmp_path.
* ``_guard_real_install_against_self_uninstall`` (session, autouse): runs
  once per xdist worker (once in a serial run) and fails the run if the
  real install root or PATH link changed.
* Topic factory plugins ``factories_*`` are registered from
  ``pytest_configure`` when present (never via ``pytest_plugins``).
"""
from __future__ import annotations

import importlib.util
import os
import pwd
import stat
from pathlib import Path

import pytest

from support import (
    CONTROL_VARS,
    CaseFactory,
    IsolatedEnv,
    build_isolated_env,
    patch_account_home,
)

# Topic factory plugins.  Registered from pytest_configure, NOT through a
# module-level ``pytest_plugins``: a full run anchors collection on
# ``tests`` (the .ptest.toml test_roots), so this conftest is not an initial
# conftest there and pytest rejects any ``pytest_plugins`` attribute in it.
# pytest_configure is historic, so it runs whether this conftest loads at
# startup (scoped runs) or later during collection (full runs).
_FACTORY_PLUGINS = (
    "factories_domain", "factories_repo", "factories_exec",
    "factories_agents",
)


def pytest_configure(config):
    for name in _FACTORY_PLUGINS:
        if (config.pluginmanager.get_plugin(name) is None
                and importlib.util.find_spec(name) is not None):
            config.pluginmanager.import_plugin(name)


def _real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# Incident U2: captured at import time, before any test can monkeypatch
# pwd/HOME/PATH/argv.  The suite must never create, modify, or remove
# either of these real paths.
_REAL_INSTALL_ROOT = _real_home() / ".local" / "ptest"
_REAL_PATH_LINK = _real_home() / ".local" / "bin" / "ptest"

# Inherited process state that must never reach a test or its children.
_STRIPPED_PREFIXES = ("PTEST_", "GIT_", "PYTEST_XDIST_", "COV_CORE_")
_STRIPPED_NAMES = frozenset({"PYTEST_ADDOPTS", "PYTEST_PLUGINS"})


def _snapshot_path(path: Path):
    """(exists, (inode, mtime_ns) or None, link target or None)."""
    try:
        stamp = os.lstat(path)
    except FileNotFoundError:
        return (False, None, None)
    except OSError:
        return (True, None, None)
    target = None
    if stat.S_ISLNK(stamp.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = None
    return (True, (stamp.st_ino, stamp.st_mtime_ns), target)


@pytest.fixture(scope="session", autouse=True)
def _guard_real_install_against_self_uninstall():
    """Incident U2: fail the session if the real install changed.

    Records the real install root and the real ``~/.local/bin/ptest``
    link (existence, inode, mtime -- neither is required to exist) and
    aborts the session via ``pytest.exit`` if either differs at teardown.
    Under pytest-xdist this runs once in every worker.
    """
    before = (_snapshot_path(_REAL_INSTALL_ROOT),
              _snapshot_path(_REAL_PATH_LINK))
    yield
    after = (_snapshot_path(_REAL_INSTALL_ROOT),
             _snapshot_path(_REAL_PATH_LINK))
    if before != after:
        pytest.exit(
            "real ptest install changed during the test session: "
            f"root={_REAL_INSTALL_ROOT} link={_REAL_PATH_LINK}; "
            "a --self test escaped its tmp isolation",
            returncode=1,
        )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path_factory) -> IsolatedEnv:
    """Per-test private HOME, XDG, passwd home and git config."""
    for name in tuple(os.environ):
        if (name in CONTROL_VARS or name in _STRIPPED_NAMES
                or name.startswith(_STRIPPED_PREFIXES)):
            monkeypatch.delenv(name, raising=False)
    env = build_isolated_env(tmp_path_factory.mktemp("iso"))
    for name, value in env.environ.items():
        monkeypatch.setenv(name, value)
    patch_account_home(monkeypatch, env.home)
    return env


@pytest.fixture(autouse=True)
def _deny_non_tmp_self_roots(monkeypatch, tmp_path, isolated_env):
    """Incident U2: uninstall discovery under test stays inside tmp.

    Wraps ``ptest.uninstall.plan_self`` so a planned ``--self`` root
    outside this test's tmp tree (or its isolated home) raises instead of
    ever reaching ``apply_self``.
    """
    from ptest import uninstall as uninstall_api

    real_plan_self = uninstall_api.plan_self
    allowed = (tmp_path, isolated_env.root)

    def guarded():
        plan = real_plan_self()
        if plan.root is not None:
            for base in allowed:
                try:
                    plan.root.relative_to(base)
                except ValueError:
                    continue
                break
            else:
                raise AssertionError(
                    "refusing --self root outside test tmp: "
                    f"{plan.root}"
                )
        return plan

    monkeypatch.setattr(uninstall_api, "plan_self", guarded)


@pytest.fixture
def case(tmp_path):
    return CaseFactory(tmp_path)
