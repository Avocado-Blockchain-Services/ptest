"""Shared NG fixtures (test-only, Task0 owned).

Provides ``case`` (a :class:`CaseFactory`) and clears orchestrator control
variables from the Python test process.
"""
from __future__ import annotations

import pytest

from support import CONTROL_VARS, CaseFactory


@pytest.fixture(autouse=True)
def _clear_bootstrap_control_env(monkeypatch):
    for var in CONTROL_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def case(tmp_path):
    return CaseFactory(tmp_path)
