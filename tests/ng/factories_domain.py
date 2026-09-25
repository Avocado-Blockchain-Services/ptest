"""Topic factory plugin: domain/state/account fixtures (T2 owned).

Registered from ``tests/ng/conftest.py::pytest_configure`` (never via
``pytest_plugins``).  Every fixture here is function-scoped, builds only
below the test's ``tmp_path``, and imports only ``support``, ``ptest``
and the stdlib -- never another ``factories_*`` module.

Fixture names honour the frozen prefixes: ``domain_*``, ``account_*``,
``state_*``.
"""
from __future__ import annotations

import pytest

from ptest import files as F
from support import CaseFactory, patch_account_home


@pytest.fixture
def domain_factory(tmp_path):
    """Factory for fixture marker domains: ``domain_factory(slots=1, jobs=1)``."""
    factory = CaseFactory(tmp_path)

    def make(slots=1, jobs=1):
        return factory.domain(slots=slots, jobs=jobs)

    return make


@pytest.fixture
def account_home(tmp_path, monkeypatch):
    """Factory for fresh account homes: ``account_home(name="home")``.

    Creates ``tmp_path/name`` at 0700 and repoints
    ``pwd.getpwuid(os.getuid()).pw_dir`` at it, so in-process normal
    domain resolution lands in tmp.
    """
    def make(name="home"):
        return patch_account_home(monkeypatch, tmp_path / name)

    return make


@pytest.fixture
def state_dir_factory(tmp_path, monkeypatch):
    """Factory for explicit state dirs: ``state_dir_factory(name="state")``.

    Creates a private 0700 directory and exports it as PTEST_STATE_DIR.
    This is the only way T2 tests set PTEST_STATE_DIR for a valid dir;
    tests whose subject is the env value itself (invalid paths,
    placement inside a checkout, laziness) keep a direct monkeypatched
    setenv with a comment.
    """
    def make(name="state"):
        state = F.ensure_private_dir(tmp_path, name)
        monkeypatch.setenv("PTEST_STATE_DIR", str(state))
        return state

    return make
