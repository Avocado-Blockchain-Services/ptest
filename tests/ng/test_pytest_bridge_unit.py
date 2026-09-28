"""Unit tests for the bridge frozen pytest-cov/coverage tuple gate.

These execute in the default gate (no provisioned interpreter): the
version probe is stubbed, so the missing/off-table refusal branches and
the exact-match pass branch all run here.
"""
from __future__ import annotations

import importlib.util

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


def _load_hook(tmp_path, source: str, name: str = "probe_hook_mod"):
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PERSEA_TESTNODEDOWN = (
    "import pytest\n"
    "_active = set()\n"
    "@pytest.hookimpl(optionalhook=True)\n"
    "def pytest_testnodedown(node, error):\n"
    "    worker_id = getattr(getattr(node, 'gateway', None), 'id', None)\n"
    "    if worker_id is not None:\n"
    "        _active.discard(worker_id)\n"
    "    workeroutput = getattr(node, 'workeroutput', None) or {}\n"
    "    profile = workeroutput.get('real_commit_cleanup_profile')\n"
    "    if profile is not None:\n"
    "        print('cleanup-profile:', profile)\n"
)


def test_observation_only_persea_shaped_hook_passes(tmp_path):
    """The persea-shaped observer reads parameters but writes nothing."""
    module = _load_hook(tmp_path, _PERSEA_TESTNODEDOWN)
    assert pytest_bridge._is_observation_only(
        module.pytest_testnodedown, ("node", "error")) is True


def test_observation_only_workeroutput_write_is_refused(tmp_path):
    """Assigning into parameter-derived worker output is a write."""
    module = _load_hook(
        tmp_path,
        "def pytest_testnodedown(node, error):\n"
        "    node.workeroutput['ptest_bridge'] = {}\n")
    assert pytest_bridge._is_observation_only(
        module.pytest_testnodedown, ("node", "error")) is False


def test_observation_only_collection_pop_is_refused(tmp_path):
    """A mutating ``ids`` call is a write even without reordering flags."""
    module = _load_hook(
        tmp_path,
        "def pytest_xdist_node_collection_finished(node, ids):\n"
        "    if len(ids) > 1:\n"
        "        ids.pop()\n")
    assert pytest_bridge._is_observation_only(
        module.pytest_xdist_node_collection_finished,
        ("node", "ids")) is False


def test_observation_only_read_only_collection_observer_passes(tmp_path):
    """Reading ``ids`` without writing is observation-only."""
    module = _load_hook(
        tmp_path,
        "def pytest_xdist_node_collection_finished(node, ids):\n"
        "    print('node collected', len(ids))\n")
    assert pytest_bridge._is_observation_only(
        module.pytest_xdist_node_collection_finished,
        ("node", "ids")) is True


def test_observation_only_missing_source_is_refused():
    """A hook with no retrievable source fails closed."""
    assert pytest_bridge._is_observation_only(len, ("node",)) is False


def _long_id(prefix: str, size: int) -> str:
    body = prefix + "x" * (size - len(prefix.encode("utf-8")))
    assert len(body.encode("utf-8")) == size
    return body


def test_bridge_normalize_test_id_matches_contracts_on_real_shapes():
    """The dependency-light bridge must normalise exactly like contracts.

    The bridge cannot import ptest, so the algorithm is duplicated; this
    pins the two implementations together on the real persea shapes
    (8,315 and 262,259 bytes) plus the byte boundary.
    """
    from ptest import contracts as C

    samples = [
        "tests/test_a.py::test_a[param]",
        "t" * C.TEST_ID_MAX_BYTES,
        _long_id("tests/test_channel_previews.py::test_case[https://cdn.example/", 8315),
        _long_id("tests/test_rss_news.py::test_case[<rss>", 262259),
    ]
    for raw in samples:
        assert (pytest_bridge._normalize_test_id(raw, C.TEST_ID_MAX_BYTES)
                == C.normalize_test_id(raw))


def test_bridge_worker_record_rejects_unbounded_outcome_ids():
    """Worker outcome keys longer than the bound stay refused, fail-closed."""
    from ptest import contracts as C

    record = {
        "worker_id": "gw0", "refused": False,
        "protocol_seen": 1, "failures": 0, "collection_errors": 0,
        "dropped": False, "conftest_hooks": [], "notes": [],
        "outcomes": {"t" * (C.TEST_ID_MAX_BYTES + 1): "passed"},
    }
    assert pytest_bridge._valid_worker_record(record, "gw0") is False


def test_installed_pytest_cov_module_alone_is_not_coverage():
    """pytest-cov's entry-point module is registered whenever it is installed.

    fullon2 installs pytest-cov without passing --cov; its parallel runs
    were refused by the frozen-tuple gate meant for coverage runs.
    """
    import types
    module = types.ModuleType("pytest_cov.plugin")
    assert pytest_bridge._coverage_plugin([("pytest_cov", module)]) is None


def test_cov_controller_plugin_counts_as_coverage():
    import types
    module = types.ModuleType("pytest_cov.plugin")
    controller = type("CovPlugin", (), {"cov_controller": object()})()
    assert pytest_bridge._coverage_plugin(
        [("pytest_cov", module), ("_cov", controller)]) is controller
