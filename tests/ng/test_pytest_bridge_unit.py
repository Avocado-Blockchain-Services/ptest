"""Unit tests for the bridge frozen pytest-cov/coverage tuple gate.

These execute in the default gate (no provisioned interpreter): the
version probe is stubbed, so the missing/off-table refusal branches and
the exact-match pass branch all run here.
"""
from __future__ import annotations

import pytest

from ptest.runtime import pytest_bridge


def _stub_versions(monkeypatch, mapping):
    def fake_version(name):
        if name not in mapping:
            raise pytest_bridge.importlib.metadata.PackageNotFoundError(name)
        return mapping[name]

    monkeypatch.setattr(
        pytest_bridge.importlib.metadata, "version", fake_version)


def test_coverage_tuple_exact_pair_passes(monkeypatch):
    _stub_versions(monkeypatch, {"pytest-cov": "7.1.0", "coverage": "7.15.0"})

    assert pytest_bridge._coverage_tuple() == ("7.1.0", "7.15.0")


def test_coverage_tuple_missing_package_refuses(monkeypatch):
    _stub_versions(monkeypatch, {})

    with pytest.raises(pytest_bridge.BridgeRefusal) as error:
        pytest_bridge._coverage_tuple()
    assert error.value.code == "unsupported-capability"
    assert "unavailable" in error.value.message


def test_coverage_tuple_off_table_pair_refuses(monkeypatch):
    _stub_versions(monkeypatch, {"pytest-cov": "7.0.0", "coverage": "7.16.1"})

    with pytest.raises(pytest_bridge.BridgeRefusal) as error:
        pytest_bridge._coverage_tuple()
    assert error.value.code == "unsupported-capability"
    assert "outside the frozen qualification tuple" in error.value.message


def test_coverage_tuple_half_present_refuses(monkeypatch):
    _stub_versions(monkeypatch, {"pytest-cov": "7.1.0"})

    with pytest.raises(pytest_bridge.BridgeRefusal) as error:
        pytest_bridge._coverage_tuple()
    assert error.value.code == "unsupported-capability"
