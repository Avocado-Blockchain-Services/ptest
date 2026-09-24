"""Shared NG fixtures (test-only, Task0 owned).

Provides ``case`` (a :class:`CaseFactory`) and clears orchestrator control
variables from the Python test process.
"""
from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

import pytest

from support import CONTROL_VARS, CaseFactory


def _real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# Incident U2: captured at import time, before any test can monkeypatch
# pwd/HOME/PATH/argv.  The suite must never create, modify, or remove
# either of these real paths.
_REAL_INSTALL_ROOT = _real_home() / ".local" / "ptest"
_REAL_PATH_LINK = _real_home() / ".local" / "bin" / "ptest"


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
def _deny_non_tmp_self_roots(monkeypatch, tmp_path):
    """Incident U2: uninstall discovery under test stays inside tmp.

    Wraps ``ptest.uninstall.plan_self`` so a planned ``--self`` root
    outside this test's tmp tree raises instead of ever reaching
    ``apply_self``.
    """
    from ptest import uninstall as uninstall_api

    real_plan_self = uninstall_api.plan_self

    def guarded():
        plan = real_plan_self()
        if plan.root is not None:
            try:
                plan.root.relative_to(tmp_path)
            except ValueError:
                raise AssertionError(
                    "refusing --self root outside test tmp: "
                    f"{plan.root}"
                ) from None
        return plan

    monkeypatch.setattr(uninstall_api, "plan_self", guarded)


@pytest.fixture(autouse=True)
def _clear_bootstrap_control_env(monkeypatch):
    for var in CONTROL_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def case(tmp_path):
    return CaseFactory(tmp_path)
