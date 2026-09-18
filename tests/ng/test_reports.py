"""Private native terminal-report foundation abuse tests (Task 11C)."""
from __future__ import annotations

import json
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
)


RUN_ID = "12" * 16
NONCE = "34" * 32


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
    }
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
