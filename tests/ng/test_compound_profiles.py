"""Task 12b compound-profile contracts (negative twins first)."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import hashlib
import json

import pytest

from ptest import contracts as C
from ptest import history
from ptest import operations
from ptest.adapters import pytest as pytest_adapter
from ptest.adapters import vitest as vitest_adapter
from ptest.reports import allocate_report, consume_attempt_report
from ptest.runtime.pytest_bridge import AdvancedPlugin, BridgeRefusal, _write_attempt_report
from ptest.runtime import pytest_bridge
from ptest.runners import adapter_for


def _config(kind: C.RunnerKind, *, args=(), full_args=(), workers=1):
    launcher = ("python",) if kind is C.RunnerKind.PYTEST else ("node",)
    return C.Config(
        runner=C.RunnerConfig(kind=kind, launcher=launcher, args=tuple(args),
                              full_args=tuple(full_args), test_roots=("tests",),
                              workers=workers),
        setup=None, resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=True, closed_inputs=True),
        project_id="ab" * 16, config_path=Path("/tmp/project/.ptest.toml"),
    )


@pytest.mark.parametrize("kind", [C.RunnerKind.PYTEST, C.RunnerKind.VITEST,
                                   C.RunnerKind.COMMAND, C.RunnerKind.GO,
                                   C.RunnerKind.CARGO])
def test_compound_support_is_not_inferred_from_runner_kind(kind):
    support = adapter_for(kind).compound_support(_config(kind))
    assert support.selection is False
    assert support.parallel_identity is False
    assert support.profile is None


def test_compound_support_rejects_a_configured_parallel_control():
    config = _config(C.RunnerKind.PYTEST, args=("-n", "2"))
    support = pytest_adapter.compound_support(config)
    assert support.selection is False
    assert support.parallel_identity is False
    assert any(item.code == "unsupported-capability" for item in support.limitations)


def test_profile_promotion_requires_consumed_complete_evidence(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    before = pytest_adapter.compound_support(_config(C.RunnerKind.PYTEST))
    assert before.profile is None
    run_id, nonce = "cd" * 16, "ef" * 32
    binding = allocate_report(domain, checkout, run_id=run_id, nonce=nonce,
                              attempt_id="a001", runner="pytest",
                              execution_mode="selected", effective_profile="advanced")
    facts = {
        "runner": "pytest", "version": "9.1.1", "python": "fixture",
        "implementation": "cpython", "cache_tag": "cpython-313",
        "roots": ["tests"], "profile": "advanced", "plugins": [],
        "dependencies": {}, "hooks": [], "effective_options": {},
        "command_variants": [["python", "tests"], ["python", "tests"]],
        "coverage": ["fixture-cov"], "reporters": ["fixture-reporter"],
        "platform": {"system": "linux"},
    }
    runtime_identity = hashlib.sha256(json.dumps(
        facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prefix = f"pt_{checkout.checkout_id[:8]}_{run_id}_a001_w000"
    _write_attempt_report(
        binding.path, {"run_id": run_id, "nonce": nonce, "attempt_id": "a001",
                       "execution_mode": "selected", "effective_profile": "advanced"},
        runtime="9.1.1", native_exit=0, bridge_exit=0, complete=True,
        problem=None, runtime_identity=runtime_identity, runtime_facts=facts,
        inventory=[{"id": "tests/test.py::test_ok", "file": "tests/test.py",
                    "outcome": "passed", "setup_s": 0.0, "call_s": 0.0,
                    "teardown_s": 0.0}],
        workers=[{"worker_id": "w000", "resource_prefix": prefix}],
        coverage_complete=True, reporters_complete=True)
    evidence = consume_attempt_report(binding)
    history.publish_qualified_profile(domain, checkout, C.RunnerKind.PYTEST, evidence,
                                      binding=binding)
    qualified = history.read_qualified_profile(domain, checkout, C.RunnerKind.PYTEST)
    assert qualified is not None
    support = pytest_adapter.compound_support(
        _config(C.RunnerKind.PYTEST), qualified_profile=qualified)
    assert support.profile == "pytest-advanced-v1"
    assert support.selection is True
    substituted = replace(evidence, runtime_identity="0" * 64)
    with pytest.raises(C.Problem, match="consumed authenticated"):
        history.publish_qualified_profile(
            domain, checkout, C.RunnerKind.PYTEST, substituted, binding=binding)


def test_forged_or_incomplete_evidence_cannot_promote_profile(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    record = C.TestRecord(id="tests/test.py::test_ok", file="tests/test.py",
                          outcome=C.Outcome.PASSED)
    inventory = C.Inventory(adapter="pytest", version="9.1.1", complete=True,
                            tests=(record,), digest="aa" * 32)
    attempt = C.AttemptResult(attempt_id="a001", phase="execution",
                              status=C.Status.PASSED, raw_exit_code=0,
                              final_exit_code=0, inventory_complete=False)
    evidence = C.AttemptEvidence(attempt_id="a001", result=attempt,
                                 inventory=inventory, terminal_complete=True,
                                 parallel_identity=False,
                                 runtime_identity="bb" * 32)
    with pytest.raises((C.Problem, TypeError), match="qualification evidence|consumed authenticated"):
        history.publish_qualified_profile(domain, checkout,
                                          C.RunnerKind.PYTEST, evidence, binding=None)
    assert history.read_qualified_profile(domain, checkout,
                                          C.RunnerKind.PYTEST) is None


def test_vitest_compound_support_requires_an_exact_local_profile():
    support = vitest_adapter.compound_support(
        _config(C.RunnerKind.VITEST, args=("--pool", "threads")))
    assert support.selection is False
    assert support.parallel_identity is False
    assert support.profile is None


def test_catalog_declaration_requires_explicit_instrumentation():
    config = _config(C.RunnerKind.PYTEST,
                     args=("--cov=project", "--cov-report=term"))
    catalog = pytest_adapter.qualified_profile(config)
    assert catalog is not None
    support = pytest_adapter.compound_support(config)
    assert support.profile == "pytest-advanced-v1"
    assert pytest_adapter.qualified_profile(_config(C.RunnerKind.PYTEST)) is None


def test_advanced_report_consumer_requires_complete_native_evidence(tmp_path):
    # The binding type is deliberately supplied by the executor.  A missing
    # report must not be promoted to AttemptEvidence or selection support.
    with pytest.raises((C.Problem, TypeError)):
        consume_attempt_report(object())


def test_pytest_binding_rejects_execution_label_mismatch(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    monkeypatch.setenv("PTEST_PYTEST_REPORT_PATH",
                       str(report_dir / ("native-a001-" + "ab" * 16 + ".json")))
    monkeypatch.setenv("PTEST_RUN_ID", "cd" * 16)
    monkeypatch.setenv("PTEST_GRANT_NONCE", "ef" * 32)
    monkeypatch.setenv("PTEST_PYTEST_ATTEMPT", "a001")
    monkeypatch.setenv("PTEST_PYTEST_EXECUTION", "scoped")
    monkeypatch.setenv("PTEST_EXECUTION", "full")
    with pytest.raises(BridgeRefusal, match="execution binding"):
        pytest_bridge._report_binding()


def test_basic_prepare_does_not_promote_selected_execution():
    config = _config(C.RunnerKind.PYTEST)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/test_one.py",))
    grant = C.Grant(run_id="cd" * 16, nonce="ef" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)
    with pytest.raises(C.Problem, match="unsupported-capability"):
        pytest_adapter.prepare(config, plan, grant, attempt)


def test_pytest_full_content_shortcut_tracks_basic_execution_tier(monkeypatch):
    config = _config(C.RunnerKind.PYTEST)
    request = C.RunRequest(mode=C.Mode.FULL)
    seen = {}

    def snapshot(*args, **kwargs):
        seen.update(kwargs)
        return C.InputSnapshot(
            digest="ab" * 32, compatibility=None, head=None, clean=True,
        )

    monkeypatch.setattr(operations.source, "snapshot", snapshot)
    operations._capture_source(
        object(), config, request, ensure_key=False,
        execution_tier=C.ExecutionTier.BASIC_SERIAL,
    )
    assert seen["pytest_full_outputs"] is True


def test_advanced_full_never_uses_execution_only_content_shortcut(monkeypatch):
    config = _config(C.RunnerKind.PYTEST)
    request = C.RunRequest(mode=C.Mode.FULL)
    seen = {}

    def snapshot(*args, **kwargs):
        seen.update(kwargs)
        return C.InputSnapshot(
            digest="ab" * 32, compatibility="compat", head=None, clean=True,
        )

    monkeypatch.setattr(operations.source, "snapshot", snapshot)
    operations._capture_source(
        object(), config, request, ensure_key=False,
        execution_tier=C.ExecutionTier.ADVANCED,
    )
    assert seen.get("pytest_full_outputs", False) is False


def test_unproved_parallel_worker_identity_is_not_synthesized():
    plugin = AdvancedPlugin(2)
    with pytest.raises(Exception, match="worker identity"):
        plugin.worker_identities()


def test_worker_identity_binds_checkout_run_attempt_and_prefix(monkeypatch):
    checkout = "ab" * 16
    run = "cd" * 16
    attempt = "a001"
    monkeypatch.setenv("PTEST_CHECKOUT_ID", checkout)
    monkeypatch.setenv("PTEST_RUN_ID", run)
    monkeypatch.setenv("PTEST_PYTEST_ATTEMPT", attempt)
    monkeypatch.setenv("PTEST_WORKER_ID", "w000")
    monkeypatch.setenv("PTEST_RESOURCE_PREFIX", f"pt_{checkout[:8]}_{run}_{attempt}_w000")
    assert AdvancedPlugin(1).worker_identities() == [{
        "worker_id": "w000",
        "resource_prefix": f"pt_{checkout[:8]}_{run}_{attempt}_w000",
    }]
    monkeypatch.setenv("PTEST_RESOURCE_PREFIX", f"pt_{checkout[:8]}_{run}_{attempt}_w001")
    with pytest.raises(Exception, match="worker identity"):
        AdvancedPlugin(1).worker_identities()


def test_prepared_advanced_selection_capability_is_not_a_qualification_claim():
    config = _config(C.RunnerKind.PYTEST)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/alpha.py",))
    grant = C.Grant(run_id="cd" * 16, nonce="ef" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)
    prepared = pytest_adapter.prepare_advanced(config, plan, grant, attempt)
    assert prepared.capability is not None
    assert prepared.capability.selection is False


def test_attempt_report_writer_honours_native_report_size_descriptor(tmp_path):
    path = tmp_path / "native.json"
    records = [
        {"id": f"tests/test_{index}.py::test_body", "file": "tests/test.py",
         "outcome": "passed", "setup_s": 0.0, "call_s": 0.0,
         "teardown_s": 0.0}
        for index in range(3000)
    ]
    runtime_facts = {
        "runner": "pytest", "version": "9.1.1", "python": "fixture",
        "implementation": "cpython", "cache_tag": "cpython-313",
        "roots": ["tests"], "profile": "advanced", "plugins": [],
        "dependencies": {}, "hooks": [], "effective_options": {},
        "command_variants": [["python", "tests"], ["python", "tests"]],
        "coverage": True, "reporters": True, "platform": {"system": "linux"},
    }
    runtime_identity = hashlib.sha256(json.dumps(
        runtime_facts, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    _write_attempt_report(
        path,
        {"run_id": "ab" * 16, "nonce": "cd" * 32, "attempt_id": "a001",
         "execution_mode": "full", "effective_profile": "advanced"},
        runtime="9.1.1", native_exit=0, bridge_exit=0, complete=True,
        problem=None, runtime_identity=runtime_identity, runtime_facts=runtime_facts,
        inventory=records,
        workers=[{"worker_id": "w000", "resource_prefix": "pt_aabbccdd_" + "ab" * 16 + "_a001_w000"}],
        coverage_complete=True, reporters_complete=True,
    )
    assert path.stat().st_size > 64 * 1024


def test_attempt_report_writer_refuses_descriptor_test_and_event_overflow(tmp_path, monkeypatch):
    records = [{
        "id": "tests/test.py::test_body", "file": "tests/test.py",
        "outcome": "passed", "setup_s": 0.0, "call_s": 0.0,
        "teardown_s": 0.0,
    }] * 2
    runtime_facts = {
        "runner": "pytest", "version": "9.1.1", "python": "fixture",
        "implementation": "cpython", "cache_tag": "cpython-313",
        "roots": ["tests"], "profile": "advanced", "plugins": [],
        "dependencies": {}, "hooks": [], "effective_options": {},
        "command_variants": [["python", "tests"], ["python", "tests"]],
        "coverage": True, "reporters": True, "platform": {"system": "linux"},
    }
    runtime_identity = hashlib.sha256(json.dumps(
        runtime_facts, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    monkeypatch.setattr(pytest_bridge, "_report_limits", lambda: (1_000_000, 1, 1, 4096))
    with pytest.raises(BridgeRefusal, match="exceeds its bound"):
        _write_attempt_report(
            tmp_path / "native.json",
            {"run_id": "ab" * 16, "nonce": "cd" * 32,
             "attempt_id": "a001", "execution_mode": "full",
             "effective_profile": "advanced"},
            runtime="9.1.1", native_exit=0, bridge_exit=0, complete=True,
            problem=None, runtime_identity=runtime_identity,
            runtime_facts=runtime_facts, inventory=records,
            workers=[{"worker_id": "w000", "resource_prefix":
                       "pt_aabbccdd_" + "ab" * 16 + "_a001_w000"}],
            coverage_complete=True, reporters_complete=True,
        )


def test_advanced_prepare_preserves_exact_selected_files_and_grant_identity():
    config = _config(C.RunnerKind.PYTEST)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/alpha.py", "tests/beta.py"))
    grant = C.Grant(run_id="cd" * 16, nonce="ef" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)
    prepared = pytest_adapter.prepare_advanced(config, plan, grant, attempt)
    assert prepared.capability is not None
    assert prepared.capability.execution is C.ExecutionTier.ADVANCED
    assert prepared.capability.selection is False
    assert prepared.argv[-2:] == plan.files
    assert dict(prepared.env_updates)["PTEST_PYTEST_PROFILE"] == "advanced"


def test_advanced_prepare_rejects_a_forged_expected_runtime_digest():
    config = _config(C.RunnerKind.PYTEST)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected", files=("tests/a.py",))
    grant = C.Grant(run_id="cd" * 16, nonce="ef" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)
    with pytest.raises(C.Problem, match="runtime identity"):
        pytest_adapter.prepare_advanced(config, plan, grant, attempt,
                                        expected_runtime_identity="bad")


def test_vitest_advanced_prepare_refuses_before_node_launch():
    config = _config(C.RunnerKind.VITEST)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/alpha.test.mjs",))
    grant = C.Grant(run_id="cd" * 16, nonce="ef" * 32, slots=2,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=2)
    with pytest.raises(C.Problem, match="advanced native qualification is unavailable"):
        vitest_adapter.prepare_advanced(config, plan, grant, attempt)
