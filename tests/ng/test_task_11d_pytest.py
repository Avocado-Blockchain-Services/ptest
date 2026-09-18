"""Task 11D: scoped native pytest execution contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptest import contracts as C, operations
from ptest.adapters.pytest import prepare
from ptest.runtime import pytest_bridge


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(
        run_id="12" * 16,
        nonce="34" * 32,
        slots=slots,
        memory_estimate_mb=None,
        reserved_memory_mb=None,
        generation=0,
        domain_id="56" * 16,
    )


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(
        run_id="12" * 16,
        attempt_id="a001",
        resource_prefix="pt_checkout_run_a001_w000",
        worker_count=workers,
    )


def test_pytest_automatic_is_refused_before_admission(case, monkeypatch):
    domain = case.domain()
    config = case.config(runner_kind="pytest")
    request = C.RunRequest(mode=C.Mode.AUTOMATIC)

    def enqueue(*args, **kwargs):
        raise AssertionError("native pytest must be rejected before admission")

    monkeypatch.setattr(operations.scheduler, "enqueue", enqueue)

    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, request)


@pytest.mark.parametrize("mode", [C.Mode.AUTOMATIC])
def test_pytest_non_scoped_modes_are_refused_before_admission(case, monkeypatch, mode):
    domain = case.domain()
    config = case.config(runner_kind="pytest")

    monkeypatch.setattr(
        operations.scheduler,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("native pytest must be rejected before admission")),
    )

    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, C.RunRequest(mode=mode, workers=8))


def test_pytest_setup_is_refused_before_admission(case, monkeypatch):
    domain = case.domain()
    setup = C.SetupConfig(
        argv=("uv", "sync"), required_paths=(".venv",),
        network=False, lifecycle_scripts=False,
    )
    config = case.config(runner_kind="pytest", setup=setup)
    monkeypatch.setattr(
        operations.scheduler,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("native pytest setup must be rejected before admission")),
    )

    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))


def test_pytest_scoped_preparation_is_basic_serial_and_never_adds_xdist():
    config = C.Config(
        runner=C.RunnerConfig(
            kind=C.RunnerKind.PYTEST,
            launcher=("python",),
            args=("-s", "--reporter", "custom"),
            full_args=("--cov",),
            test_roots=("tests",),
            workers=8,
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
        config_path=Path("/project/.ptest.toml"),
    )
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("tests/test_a.py",))

    prepared = prepare(config, plan, _grant(1), _attempt(1))

    assert "-n" not in prepared.argv
    assert prepared.summary.workers == 1
    assert prepared.capability.execution is C.ExecutionTier.BASIC_SERIAL
    assert prepared.capability.selection is False
    assert prepared.argv[-1] == "tests/test_a.py"


def test_bridge_writes_private_terminal_report_after_native_exit(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    report_path = report_dir / "native-a001-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    descriptor = Path(pytest_bridge.__file__).with_name("protocol-v1.json")
    monkeypatch.setenv("PTEST_BRIDGE_PROTOCOL", str(descriptor))
    monkeypatch.setenv("PTEST_GRANT_WORKERS", "1")
    monkeypatch.setenv("PTEST_RUN_ID", "12" * 16)
    monkeypatch.setenv("PTEST_GRANT_NONCE", "34" * 32)
    monkeypatch.setenv("PTEST_PYTEST_ATTEMPT", "a001")
    monkeypatch.setenv("PTEST_PYTEST_EXECUTION", "scoped")
    monkeypatch.setenv("PTEST_PYTEST_REPORT_PATH", str(report_path))

    class FakePytest:
        __version__ = "9.1.1"
        UsageError = RuntimeError
        hookimpl = staticmethod(lambda **kwargs: (lambda fn: fn))

        @staticmethod
        def main(argv, plugins):
            assert argv == ["tests/test_a.py"]
            assert len(plugins) == 1
            return 23

    monkeypatch.setitem(__import__("sys").modules, "pytest", FakePytest)

    assert pytest_bridge.run(["tests/test_a.py"]) == 23
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload == {
        "protocol": 1,
        "run_id": "12" * 16,
        "nonce": "34" * 32,
        "attempt_id": "a001",
        "runner": "pytest",
        "observed_runtime_version": "9.1.1",
        "execution_mode": "scoped",
        "effective_profile": "basic_serial",
        "terminal_complete": True,
        "native_exit_code": 23,
        "bridge_exit_code": 23,
        "problem": "native-failure",
    }
