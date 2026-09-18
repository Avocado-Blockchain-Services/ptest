"""Private native terminal-report foundation abuse tests (Task 11C)."""
from __future__ import annotations

import json
import os
import stat

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
    lambda value: value.update(protocol=2),
    lambda value: value.update(terminal_complete=False),
    lambda value: value.update(run_id="56" * 16),
    lambda value: value.update(nonce="78" * 32),
    lambda value: value.update(attempt_id="a002"),
    lambda value: value.update(runner="vitest"),
    lambda value: value.update(native_exit_code=0, bridge_exit_code=1),
])
def test_consume_rejects_forged_or_incomplete_terminal_records(case, mutation):
    _, _, binding = _allocate(case)
    value = _payload()
    mutation(value)
    _write(binding, value)
    with pytest.raises(C.Problem) as error:
        consume_report(binding)
    assert error.value.code == "report-invalid"
    assert "run_id" not in error.value.message
    assert str(binding.path) not in error.value.message


@pytest.mark.parametrize("payload", [
    b"",
    b"{}\n",
    b"{}\ntrailing\n",
    b"[]\n",
    b"not-json\n",
])
def test_consume_rejects_missing_malformed_and_trailing_payloads(case, payload):
    _, _, binding = _allocate(case)
    F.create_exclusive(binding.report_directory, binding.report_name, payload)
    with pytest.raises(C.Problem, match="report-invalid"):
        consume_report(binding)


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
    binding.path.unlink()
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
    assert error.value.code in {"capacity-exceeded", "report-invalid"}
    assert str(binding.path) not in error.value.message
