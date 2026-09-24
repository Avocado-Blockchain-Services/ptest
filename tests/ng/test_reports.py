"""Private native terminal-report foundation abuse tests (Task 11C)."""
from __future__ import annotations

import json
import hashlib
import os
import stat
from dataclasses import replace

import pytest

from ptest import contracts as C
from ptest import files as F
from ptest.reports import (
    NativeTerminalReport,
    allocate_report,
    cleanup_report,
    consume_report,
    consume_attempt_report,
)


RUN_ID = "12" * 16
NONCE = "34" * 32


def _runtime_facts():
    return {
        "runner": "pytest", "version": "9.1.1", "python": "fixture",
        "implementation": "cpython", "cache_tag": "cpython-313",
        "roots": ["tests"], "profile": "advanced", "plugins": [],
        "dependencies": {}, "hooks": [], "effective_options": {},
        "command_variants": [["python", "tests"], ["python", "tests"]],
        "coverage": True, "reporters": True, "platform": {"system": "linux"},
    }


def _checkout(case, domain):
    return case.checkout(domain)


def _allocate(case):
    domain = case.domain()
    checkout = _checkout(case, domain)
    binding = allocate_report(
        domain,
        checkout,
        run_id=RUN_ID,
        nonce=NONCE,
        attempt_id="a001",
        runner="pytest",
        execution_mode="scoped",
        effective_profile="advanced",
    )
    return domain, checkout, binding


def _payload(**overrides):
    value = {
        "protocol": 1,
        "run_id": RUN_ID,
        "nonce": NONCE,
        "attempt_id": "a001",
        "runner": "pytest",
        "observed_runtime_version": "9.1.1",
        "execution_mode": "scoped",
        "effective_profile": "advanced",
        "terminal_complete": True,
        "native_exit_code": 0,
        "bridge_exit_code": 0,
        "problem": None,
        "project_narrowing": {
            "narrowing": None, "conftest_hooks": [], "notes": []},
    }
    value.update(overrides)
    return value


def _advanced_payload(**overrides):
    value = _payload(effective_profile="advanced")
    runtime_facts = _runtime_facts()
    runtime_identity = hashlib.sha256(json.dumps(
        runtime_facts, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    value.update({
        "runtime_identity": runtime_identity,
        "runtime_facts": runtime_facts,
        "inventory": {
            "adapter": "pytest", "version": "9.1.1", "complete": True,
            "tests": [{"id": "tests/test_a.py::test_a", "file": "tests/test_a.py",
                        "outcome": "passed", "setup_s": 0.01, "call_s": 0.02,
                        "teardown_s": 0.01}],
            "digest": hashlib.sha256(json.dumps([{
                "id": "tests/test_a.py::test_a", "file": "tests/test_a.py",
                "outcome": "passed", "setup_s": 0.01, "call_s": 0.02,
                "teardown_s": 0.01,
            }], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        },
        "workers": [{"worker_id": "w000", "resource_prefix": "run_a001_w000"}],
        "coverage": {"complete": True}, "reporters": {"complete": True},
    })
    value.update(overrides)
    return value


def _write(binding, payload):
    return F.create_exclusive(
        binding.report_directory,
        binding.report_name,
        (json.dumps(payload, separators=(",", ":")) + "\n").encode(),
    )


def test_allocate_report_binds_private_checkout_target_without_overwrite(case):
    domain, checkout, binding = _allocate(case)
    assert binding.path == binding.report_directory / binding.report_name
    assert binding.path.parent == domain.root / "checkouts" / checkout.checkout_id / "reports"
    assert binding.path.is_absolute()
    assert not binding.path.exists()
    assert stat.S_IMODE(binding.report_directory.stat().st_mode) == 0o700
    assert binding.report_name.startswith("native-a001-")
    assert "/" not in binding.report_name

    _write(binding, _payload())
    with pytest.raises(C.Problem, match="already-exists"):
        _write(binding, _payload())
    binding2 = allocate_report(
        domain,
        checkout,
        run_id=RUN_ID,
        nonce=NONCE,
        attempt_id="a001",
        runner="pytest",
        execution_mode="scoped",
        effective_profile="advanced",
    )
    assert binding2.path != binding.path


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("run_id"),
    lambda value: value.update(extra="deny"),
    lambda value: value.update(native_exit_code="0"),
    lambda value: value.update(terminal_complete=False),
    lambda value: value.update(run_id="56" * 16),
    lambda value: value.update(nonce="78" * 32),
    lambda value: value.update(attempt_id="a002"),
    lambda value: value.update(runner="vitest"),
    lambda value: value.update(execution_mode="selected"),
    lambda value: value.update(effective_profile="basic_serial"),
    lambda value: value.update(native_exit_code=0, bridge_exit_code=1),
    lambda value: value.pop("project_narrowing"),
    lambda value: value.update(project_narrowing={
        "narrowing": None, "conftest_hooks": []}),
])
def test_consume_rejects_forged_or_incomplete_terminal_records(case, mutation):
    _, _, binding = _allocate(case)
    value = _payload()
    mutation(value)
    _write(binding, value)
    original = binding.path.read_bytes()
    with pytest.raises(C.Problem) as error:
        consume_report(binding)
    assert error.value.code == "report-invalid"
    assert "run_id" not in error.value.message
    assert str(binding.path) not in error.value.message
    cleanup_report(binding)
    assert binding.path.read_bytes() == original


@pytest.mark.parametrize("key", tuple(_payload()))
def test_consume_rejects_every_repeated_key_even_with_the_same_value(case, key):
    _, _, binding = _allocate(case)
    value = _payload()
    duplicate = json.dumps({key: value[key]})[:-1] + "," + json.dumps(value)[1:]
    F.create_exclusive(binding.report_directory, binding.report_name, duplicate.encode())
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.read_bytes() == duplicate.encode()


@pytest.mark.parametrize("contradiction", [
    {"terminal_complete": False, "native_exit_code": None,
     "bridge_exit_code": 70, "problem": "bridge-refused"},
    {"native_exit_code": 1, "bridge_exit_code": 1, "problem": "native-failure"},
], ids=["refusal-to-pass", "failure-to-pass"])
def test_consume_rejects_duplicate_keys_that_flip_terminal_failure_to_pass(case, contradiction):
    _, _, binding = _allocate(case)
    raw = (json.dumps(contradiction)[:-1] + "," + json.dumps(_payload())[1:]).encode()
    F.create_exclusive(binding.report_directory, binding.report_name, raw)
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.read_bytes() == raw


@pytest.mark.parametrize("protocol", [True, 1.0, "1", 2])
def test_binding_requires_the_exact_integer_protocol(case, protocol):
    _, _, binding = _allocate(case)
    with pytest.raises(TypeError, match="unsupported protocol"):
        replace(binding, protocol=protocol)


@pytest.mark.parametrize("protocol", [True, 1.0, "1", 2])
def test_terminal_record_requires_the_exact_integer_protocol(protocol):
    with pytest.raises(TypeError, match="unsupported protocol"):
        NativeTerminalReport(**_payload(protocol=protocol))


@pytest.mark.parametrize("protocol", [True, 1.0, "1", 2])
def test_consume_rejects_nonexact_protocol_without_consuming_target(case, protocol):
    _, _, binding = _allocate(case)
    _write(binding, _payload(protocol=protocol))
    original = binding.path.read_bytes()
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("run_id", 12), ("nonce", False), ("attempt_id", 1),
    ("runner", []), ("observed_runtime_version", {}),
    ("execution_mode", []), ("effective_profile", {}),
    ("terminal_complete", 1),
    ("native_exit_code", True), ("native_exit_code", 0.0),
    ("bridge_exit_code", True), ("bridge_exit_code", 0.0),
    ("bridge_exit_code", "0"), ("bridge_exit_code", None),
    ("problem", []),
])
def test_consume_rejects_bad_field_types_without_consuming_target(case, field, value):
    _, _, binding = _allocate(case)
    _write(binding, _payload(**{field: value}))
    original = binding.path.read_bytes()
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.read_bytes() == original


@pytest.mark.parametrize("payload", [
    b"",
    b"{}\n",
    b"{}\ntrailing\n",
    b"[]\n",
    b"not-json\n",
])
def test_consume_rejects_empty_malformed_and_trailing_payloads(case, payload):
    _, _, binding = _allocate(case)
    F.create_exclusive(binding.report_directory, binding.report_name, payload)
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)


def test_absent_report_does_not_consume_binding_or_create_target(case):
    _, _, binding = _allocate(case)
    with pytest.raises(C.Problem) as error:
        consume_report(binding)
    assert error.value.code == "state-unavailable"
    assert error.value.message == "native report could not be read"
    cleanup_report(binding)
    assert not binding.path.exists()
    _write(binding, _payload())
    assert consume_report(binding).terminal_complete is True


def test_invalid_first_report_can_be_corrected_and_consumed_once(case):
    _, _, binding = _allocate(case)
    F.create_exclusive(binding.report_directory, binding.report_name, b"not-json")
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.read_bytes() == b"not-json"
    binding.path.write_bytes(json.dumps(_payload()).encode())
    assert consume_report(binding).terminal_complete is True
    cleanup_report(binding)
    assert not binding.path.exists()


def test_consume_accepts_one_exact_typed_record(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload())
    report = consume_report(binding)
    assert isinstance(report, NativeTerminalReport)
    assert report.protocol == 1
    assert report.run_id == RUN_ID
    assert report.nonce == NONCE
    assert report.runner == "pytest"
    assert report.terminal_complete is True
    assert report.native_exit_code == report.bridge_exit_code == 0


def test_successful_binding_rejects_replay_and_retains_cleanup_identity(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload())
    consume_report(binding)
    original = binding.path.stat()
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    remaining = binding.path.stat()
    assert (remaining.st_dev, remaining.st_ino) == (original.st_dev, original.st_ino)
    cleanup_report(binding)
    assert not binding.path.exists()
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)


def test_replay_with_replacement_cannot_reassign_cleanup_identity(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload())
    consume_report(binding)
    saved = binding.path.with_suffix(".consumed")
    binding.path.rename(saved)
    _write(binding, _payload())
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)
    cleanup_report(binding)
    assert binding.path.exists()
    binding.path.unlink()
    saved.rename(binding.path)
    cleanup_report(binding)
    assert not binding.path.exists()


@pytest.mark.parametrize("name,make_target", [
    ("symlink", lambda path, tmp: path.symlink_to(tmp / "outside")),
    ("fifo", lambda path, tmp: os.mkfifo(path)),
    ("directory", lambda path, tmp: path.mkdir()),
])
def test_consume_rejects_non_regular_report_targets(case, tmp_path, name, make_target):
    _, _, binding = _allocate(case)
    make_target(binding.path, tmp_path)
    with pytest.raises(C.Problem, match="unsafe-path"):
        consume_report(binding)


def test_consume_rejects_hard_linked_report_target(case, tmp_path):
    _, _, binding = _allocate(case)
    source = tmp_path / "source"
    source.write_bytes(json.dumps(_payload()).encode())
    os.chmod(source, 0o600)
    os.link(source, binding.path)
    with pytest.raises(C.Problem, match="unsafe-path"):
        consume_report(binding)


def test_cleanup_only_removes_the_consumed_inode_after_replacement(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload())
    consume_report(binding)
    original = binding.path.stat().st_ino
    binding.path.rename(binding.path.with_suffix(".consumed"))
    replacement = _payload(problem="bridge-refused", terminal_complete=False,
                            bridge_exit_code=70)
    _write(binding, replacement)
    assert binding.path.stat().st_ino != original
    cleanup_report(binding)
    assert binding.path.exists()


def test_bridge_refusal_is_typed_and_never_a_pass(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload(
        terminal_complete=False,
        native_exit_code=0,
        bridge_exit_code=70,
        problem="bridge-refused",
    ))
    report = consume_report(binding)
    assert report.problem == "bridge-refused"
    assert report.terminal_complete is False


def test_native_runner_failure_is_complete_and_distinct_from_bridge_refusal(case):
    _, _, binding = _allocate(case)
    _write(binding, _payload(
        native_exit_code=1,
        bridge_exit_code=1,
        problem="native-failure",
    ))
    report = consume_report(binding)
    assert report.terminal_complete is True
    assert report.problem == "native-failure"
    assert report.bridge_exit_code != 70


def test_consume_rejects_oversized_report_before_decode(case):
    _, _, binding = _allocate(case)
    F.create_exclusive(
        binding.report_directory,
        binding.report_name,
        b"{" + b"x" * (16 * 1024 * 1024) + b"}",
    )
    with pytest.raises(C.Problem) as error:
        consume_report(binding)
    assert error.value.code == "capacity-exceeded"
    assert error.value.message == "native report could not be read"
    cleanup_report(binding)
    assert binding.path.exists()


def test_consume_attempt_report_requires_and_returns_complete_advanced_evidence(case):
    _, _, binding = _allocate(case)
    prefix = f"pt_{binding.report_directory.parent.name[:8]}_{RUN_ID}_a001_w000"
    _write(binding, _advanced_payload(
        workers=[{"worker_id": "w000", "resource_prefix": prefix}]))
    evidence = consume_attempt_report(binding)
    assert isinstance(evidence, C.AttemptEvidence)
    assert evidence.attempt_id == "a001"
    assert evidence.inventory is not None and evidence.inventory.complete
    assert evidence.runtime_identity == hashlib.sha256(json.dumps(
        _runtime_facts(),
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert evidence.result.status is C.Status.PASSED
    cleanup_report(binding)


def test_consume_attempt_report_does_not_trust_bridge_source_validity(case):
    _, _, binding = _allocate(case)
    prefix = f"pt_{binding.report_directory.parent.name[:8]}_{RUN_ID}_a001_w000"
    _write(binding, _advanced_payload(
        workers=[{"worker_id": "w000", "resource_prefix": prefix}]))
    evidence = consume_attempt_report(binding)
    assert evidence.result.source_valid is False
    cleanup_report(binding)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(terminal_complete=False, native_exit_code=None,
                               bridge_exit_code=70, problem="bridge-refused"),
    lambda value: value.pop("inventory"),
    lambda value: value.update(runtime_identity="not-a-digest"),
    lambda value: value["runtime_facts"].update(version="9.1.0"),
    lambda value: value.pop("runtime_facts"),
    lambda value: value.update(workers=[]),
    lambda value: value.update(coverage={"complete": False}),
    lambda value: value.update(reporters={"complete": False}),
    lambda value: value.update(workers=[{"worker_id": "w064",
                                        "resource_prefix": "pt_" + RUN_ID + "_a001_w064"}]),
    lambda value: value.update(workers=[{"worker_id": "w000",
                                        "resource_prefix": "pt_" + RUN_ID + "_a001_bad"}]),
    lambda value: value["inventory"].update(adapter="vitest"),
    lambda value: value["inventory"].update(version="9.1.0"),
    lambda value: value.pop("coverage"),
    lambda value: value.pop("reporters"),
    lambda value: value.pop("project_narrowing"),
    lambda value: value["inventory"]["tests"].append(value["inventory"]["tests"][0].copy()),
])
def test_consume_attempt_report_refuses_incomplete_or_ambiguous_evidence(case, mutation):
    _, _, binding = _allocate(case)
    prefix = f"pt_{binding.report_directory.parent.name[:8]}_{RUN_ID}_a001_w000"
    value = _advanced_payload(
        workers=[{"worker_id": "w000", "resource_prefix": prefix}])
    mutation(value)
    _write(binding, value)
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_attempt_report(binding)
    cleanup_report(binding)
