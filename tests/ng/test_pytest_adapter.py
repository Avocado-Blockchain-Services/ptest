"""Behavioral contract for the local pytest preparation profile."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from ptest import contracts as C
from ptest.adapters.pytest import prepare
from ptest.runtime import pytest_bridge


def _config(*, workers: int = 1, args: tuple[str, ...] = (),
            runner: C.RunnerConfig | None = None) -> C.Config:
    return C.Config(
        runner=runner or C.RunnerConfig(
            kind=C.RunnerKind.PYTEST,
            launcher=("python",), args=args, full_args=("-q",),
            test_roots=("tests",), workers=workers,
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
        config_path=Path("/project/.ptest.toml"),
    )


def _plan(*, execution: str = "full", files: tuple[str, ...] = ()) -> C.Plan:
    return C.Plan(mode=C.Mode.FULL, execution=execution, files=files)


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(
        run_id="cd" * 16, nonce="ef" * 32, slots=slots,
        memory_estimate_mb=None, reserved_memory_mb=None, generation=1,
        domain_id="01" * 16,
    )


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(
        run_id="cd" * 16, attempt_id="a001", resource_prefix="ptest_a001",
        worker_count=workers,
    )


def test_serial_without_xdist_adds_no_xdist_flag():
    prepared = prepare(_config(workers=1), _plan(), _grant(1), _attempt(1))

    assert "-n" not in prepared.argv
    assert "no:xdist" not in prepared.argv
    assert prepared.summary.workers == 1
    assert prepared.argv[0] == "python"
    assert prepared.argv[-2:] == ("-q", "tests")
    assert prepared.argv[1].endswith("runtime/pytest_bridge.py")


def test_selected_plan_preserves_literal_selected_files_without_full_args():
    prepared = prepare(
        _config(workers=1, args=("-k", "quoted name [x]")),
        _plan(execution="selected", files=("tests/test_unit.py",)),
        _grant(1), _attempt(1),
    )

    assert prepared.argv[2:] == ("-k", "quoted name [x]", "tests/test_unit.py")


@pytest.mark.parametrize("unsafe", [
    ("-n", "1"), ("--numprocesses=2",), ("--tx", "popen//python"),
    ("--px",), ("--rsyncdir", "src"),
])
def test_runner_parallel_or_remote_controls_are_rejected_before_bridge(unsafe):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(args=unsafe), _plan(), _grant(1), _attempt(1))


def test_grant_and_attempt_worker_mismatch_is_rejected():
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(workers=2), _plan(), _grant(2), _attempt(1))


def test_non_interpreter_launcher_is_rejected_before_bridge_execution():
    runner = C.RunnerConfig(
        kind=C.RunnerKind.PYTEST, launcher=("pytest",), test_roots=("tests",),
    )
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(runner=runner), _plan(), _grant(1), _attempt(1))


def test_parallel_grant_adds_exact_xdist_worker_count():
    prepared = prepare(_config(workers=2), _plan(), _grant(2), _attempt(2))

    assert prepared.argv[-4:] == ("-q", "-n", "2", "tests")
    assert prepared.summary.generated_options == ("xdist-workers=2",)


def test_prepared_bridge_receives_the_generated_protocol_descriptor():
    prepared = prepare(_config(), _plan(), _grant(1), _attempt(1))
    env = dict(prepared.env_updates)

    descriptor = json.loads(Path(env["PTEST_BRIDGE_PROTOCOL"]).read_text())
    assert descriptor["protocol"] == 1
    assert prepared.argv[1].endswith("runtime/pytest_bridge.py")


def test_bridge_rejects_an_unsupported_interpreter_before_import(monkeypatch):
    monkeypatch.setattr(pytest_bridge.sys, "version_info", (3, 10, 0))

    with pytest.raises(RuntimeError, match="unsupported CPython version"):
        pytest_bridge._python_version()


@pytest.mark.parametrize("workers", ["", "0", "65", "one"])
def test_bridge_rejects_missing_or_out_of_range_grant_workers(monkeypatch, workers):
    monkeypatch.setenv("PTEST_GRANT_WORKERS", workers)

    with pytest.raises(RuntimeError, match="invalid granted worker count"):
        pytest_bridge._workers()
