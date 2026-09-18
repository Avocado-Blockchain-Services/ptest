"""Shared NG fixtures (test-only, Task0 owned).

Provides ``case`` (a :class:`CaseFactory`) and clears temporary pre-T13
bootstrap hygiene variables, including ``PTEST_CONFIG``, from the Python test
process. Dedicated tests preserve NG no-legacy-read invariance and verify that
explicit child-environment overrides are applied after fixture cleanup.
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
