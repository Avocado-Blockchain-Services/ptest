"""Task11 guarded command execution acceptance fixtures."""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C, config as config_api, operations, platform, scheduler


_COMMAND_FIXTURE = Path(__file__).parent / "fixtures" / "command" / "command.py"
_GUARD_FAULTS = _COMMAND_FIXTURE.with_name("guard_faults.py")


def _command_project(case, domain, *, args=(), full_args=()):
    root = case.project(domain, kind="command")
    shutil.copy2(_COMMAND_FIXTURE, root / "command.py")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1
    )[1].split('"', 1)[0]
    config = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([sys.executable, 'command.py'])}\n"
        f"args = {json.dumps(list(args))}\n"
        f"full_args = {json.dumps(list(full_args))}\n"
        'kind = "command"\n'
        'lifecycle = "cooperative-process-group"\n'
    )
    (root / ".ptest.toml").write_text(config, encoding="utf-8")
    return root


def _run_data(completed):
    assert completed.result is not None
    assert completed.result["kind"] == "run"
    assert completed.result["error"] is None
    return completed.result["data"]


def test_command_success_preserves_literal_argv_and_streams(case):
    domain = case.domain()
    tokens = ("space value", "quoted 'value'", "--looks-like-a-flag", "$(not shell)", "界")
    root = _command_project(case, domain, args=("literal", *tokens))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == list(tokens)
    assert completed.stderr == b""
    result = _run_data(completed)
    assert result["status"] == "passed"
    assert result["runner_exit_code"] == 0
    assert result["counts"] is None
    assert result["full_gate_eligible"] is False
    assert result["baseline_published"] is False
    assert all(token not in json.dumps(result) for token in tokens)


def test_command_preserves_stdout_and_stderr_bytes(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("streams",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    assert completed.stdout == b"literal stdout\n"
    assert completed.stderr == b"literal stderr\n"


@pytest.mark.parametrize("raw, expected", [(23, 23), ("signal", 143)])
def test_command_failure_preserves_native_exit_precedence(case, raw, expected):
    domain = case.domain()
    root = _command_project(case, domain, args=("signal" if raw == "signal" else "exit",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == expected
    result = _run_data(completed)
    assert result["status"] == "failed"
    assert result["runner_exit_code"] == (-15 if raw == "signal" else 23)
    assert result["exit_code"] == expected


def test_native_profiles_are_rejected_before_admission(case):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8")
        + 'test_roots = ["tests"]\n',
        encoding="utf-8",
    )

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 2
    assert b"unsupported-capability" in completed.stderr
    assert completed.result is None


def test_command_scope_appends_literal_tail_and_excludes_full_args(case):
    domain = case.domain()
    root = _command_project(case, domain, full_args=("exit",))

    completed = case.invoke(domain, root, "--", "literal", "--full", "界", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == ["--full", "界"]
    result = _run_data(completed)
    assert result["plan"]["execution"] == "scoped"


def test_command_completion_releases_exclusive_lease(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal", "done"))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    leases = scheduler.reconcile(domain)
    assert len(leases) == 1
    assert leases[0].state.value == "RELEASED"


def _execute(root, domain, **options):
    config = config_api.resolve_config(root).config
    return operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL, **options))


def _source_snapshots(monkeypatch, *digests):
    snapshots = [C.InputSnapshot(
        digest=digest, compatibility="command-test-compatibility",
        head="a" * 40, clean=True,
    ) for digest in digests]
    monkeypatch.setattr(operations.source, "ensure_fingerprint_key", lambda _domain: None)
    monkeypatch.setattr(operations.source, "snapshot", lambda *args, **kwargs: snapshots.pop(0))


def _guard_fault(monkeypatch, mode):
    monkeypatch.setenv("TEST_GUARD_FAULT", mode)
    monkeypatch.setattr(operations, "_GUARD_SCRIPT",
                        f"exec(compile(open({_GUARD_FAULTS.as_posix()!r}).read(), 'guard_faults', 'exec'))")


def _follower(case, domain):
    root = case.project(domain)
    config = config_api.resolve_config(root).config
    return scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=os.urandom(16).hex(), checkout=operations._checkout(config),
        owner=platform.process_identity(os.getpid()), slots=1, exclusive=True,
        fixture=True, deadline=time.monotonic() + 30))


@pytest.mark.parametrize("code", [1, 2])
def test_command_native_failure_codes(case, code):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", str(code)))
    completed = case.invoke(domain, root, timeout=5)
    assert completed.code == code
    assert _run_data(completed)["runner_exit_code"] == code


def test_command_missing_executable_is_failed_127(case):
    domain = case.domain()
    root = _command_project(case, domain)
    config = config_api.resolve_config(root).config
    config = replace(config, runner=replace(config.runner, launcher=("/missing/ptest-runner",)))
    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.FAILED, 127, None)
    assert any(reason.code == "missing-executable" for reason in result.reasons)


def test_command_unchanged_source_preserves_runner_result_and_records_snapshots(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    _source_snapshots(monkeypatch, "a" * 64, "a" * 64)

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.PASSED, 0, 0)
    assert result.input_before.digest == result.input_after.digest == "a" * 64
    assert result.baseline_published is False
    assert result.full_gate_eligible is False


def test_command_source_change_with_zero_exit_is_incomplete_70(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain)
    changed = root / "runtime-input.txt"
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8").replace(
            'args = []', f'args = ["modify-exit", "{changed}", "0"]'),
        encoding="utf-8",
    )
    _source_snapshots(monkeypatch, "a" * 64, "b" * 64)

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.INCOMPLETE, 70, 0)
    assert any(reason.code == "changed-during-run" for reason in result.reasons)
    assert result.source_valid is False
    assert result.full_gate_eligible is False
    assert result.baseline_published is False
    changed_reason = next(reason for reason in result.reasons
                          if reason.code == "changed-during-run")
    assert changed_reason.paths == ()
    assert "runtime-input.txt" not in changed_reason.message


def test_command_source_change_with_runner_failure_preserves_runner_exit(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain)
    changed = root / "runtime-input.txt"
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8").replace(
            'args = []', f'args = ["modify-exit", "{changed}", "23"]'),
        encoding="utf-8",
    )
    _source_snapshots(monkeypatch, "a" * 64, "b" * 64)

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.INCOMPLETE, 23, 23)
    assert result.exit_origin == "runner"
    assert any(reason.code == "changed-during-run" for reason in result.reasons)


def test_command_unknown_snapshot_is_not_source_valid_or_gate_eligible(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    unknown = C.InputSnapshot(
        digest=None, compatibility=None, head=None, clean=False,
        limitations=(C.Reason(code="unknown-input", message="snapshot unavailable"),),
    )
    monkeypatch.setattr(operations.source, "ensure_fingerprint_key", lambda _domain: None)
    monkeypatch.setattr(operations.source, "snapshot", lambda *args, **kwargs: unknown)

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.PASSED, 0, 0)
    assert result.source_valid is False
    assert result.full_gate_eligible is False
    assert result.input_before is unknown
    assert result.input_after is unknown
    assert any(reason.code == "unknown-input" for reason in result.limitations)


def test_command_from_subdirectory_uses_project_root(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("cwd",))
    child = root / "nested"
    child.mkdir()
    completed = case.invoke(domain, child, timeout=5)
    assert completed.code == 0
    assert completed.stdout.decode().strip() == str(root)
    exports = list(root.glob("ptest-result-*.json"))
    assert len(exports) == 1
    assert json.loads(exports[0].read_text())["data"]["status"] == "passed"


@pytest.mark.parametrize("code", [0, 23])
def test_export_collision_preserves_native_exit_and_existing_file(case, code):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", str(code)))
    target = root / "result.json"
    target.write_bytes(b"user-owned sentinel")
    completed = case.invoke(domain, root, "--result-json", "result.json", timeout=5)
    assert completed.code == (70 if code == 0 else code)
    assert target.read_bytes() == b"user-owned sentinel"


def test_normal_command_has_no_probe_attempt_timeout(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    _guard_fault(monkeypatch, "bounded-attempt")
    result = _execute(root, domain)
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)


@pytest.mark.parametrize("problem, raw, expected", [
    ("execution-timeout", "none", 70), ("execution-timeout", "0", 70),
    ("execution-timeout", "-15", 70), ("execution-timeout", "23", 23),
    ("control-unavailable", "0", 70),
    ("missing-executable", "0", 70),
])
def test_guard_problem_cannot_be_pass_signal_or_missing_executable(case, monkeypatch, problem, raw, expected):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    _guard_fault(monkeypatch, f"problem:{problem}:{raw}")
    result = _execute(root, domain)
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, expected)
    assert result.runner_exit_code == (None if raw == "none" else int(raw))
    assert result.exit_origin == ("runner" if expected == 23 else "ptest")
    assert result.signal is None


@pytest.mark.parametrize("raw, expected", [(0, 130), (1, 1), (2, 2), (-15, 143)])
def test_runner_nonzero_precedes_cooperative_cancel(case, monkeypatch, raw, expected):
    domain = case.domain()
    args = ("signal",) if raw < 0 else ("exit", str(raw))
    root = _command_project(case, domain, args=args)
    run_guard = operations._run_guard

    def cancel_after_facts(*args):
        result = run_guard(*args)
        signal.raise_signal(signal.SIGINT)
        return result

    monkeypatch.setattr(operations, "_run_guard", cancel_after_facts)
    result = _execute(root, domain)
    assert result.exit_code == expected
    assert result.runner_exit_code == raw
    assert result.attempts[0].final_exit_code == expected


@pytest.mark.parametrize("boundary", ["begin_finalization", "finish"])
@pytest.mark.parametrize("raw", [0, 23])
def test_finalization_failure_is_incomplete_and_preserves_runner(case, monkeypatch, boundary, raw):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", str(raw)))

    def fail(*args):
        raise C.Problem(code="ownership-uncertain", message="injected finalization failure", phase="finalization")

    monkeypatch.setattr(scheduler, boundary, fail)
    result = _execute(root, domain, result_path="result.json")
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == (70 if raw == 0 else raw)
    assert result.runner_exit_code == raw
    exported = json.loads((root / "result.json").read_text())["data"]
    assert exported["status"] == "incomplete"
    assert exported["exit_code"] == result.exit_code
    assert scheduler.reconcile(domain)[0].state is not C.LeaseState.RELEASED


def test_export_holds_exclusivity_until_proof_and_publication(case, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    root = _command_project(case, domain, args=("exit", "0"))
    begin, finish = scheduler.begin_finalization, scheduler.finish
    follower = None

    def observe_begin(*args):
        nonlocal follower
        follower = _follower(case, domain)
        assert scheduler.poll(domain, follower).state is C.LeaseState.QUEUED
        return begin(*args)

    def observe_finish(*args):
        assert scheduler.poll(domain, follower).state is C.LeaseState.QUEUED
        data = json.loads((root / "result.json").read_text())["data"]
        assert data["runner_exit_code"] == 0
        assert data["memory_estimate_mb"] is None
        return finish(*args)

    monkeypatch.setattr(scheduler, "begin_finalization", observe_begin)
    monkeypatch.setattr(scheduler, "finish", observe_finish)
    result = _execute(root, domain, result_path="result.json")
    assert result.exit_code == 0
    assert scheduler.poll(domain, follower).state is C.LeaseState.GRANTED


@pytest.mark.parametrize("fault", ["bad-draining", "early-draining", "missing-draining",
                                   "truncated-draining", "killed-before-draining",
                                   "wrong-attempt", "duplicate-facts", "wrong-nonce", "wrong-run"])
def test_invalid_handoff_retains_lease_and_blocks_follower(case, monkeypatch, fault):
    domain = case.domain(slots=2, jobs=2)
    root = _command_project(case, domain, args=("exit", "0"))
    _guard_fault(monkeypatch, fault)
    result = _execute(root, domain)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code != 0
    assert scheduler.reconcile(domain)[0].state is not C.LeaseState.RELEASED
    follower = _follower(case, domain)
    assert scheduler.poll(domain, follower).state is C.LeaseState.QUEUED


def test_incomplete_handoff_normalizes_native_signal(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("signal",))
    _guard_fault(monkeypatch, "missing-draining")
    result = _execute(root, domain)
    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.INCOMPLETE, 143, -15)
    assert result.attempts[0].final_exit_code == 143


def test_startup_cancellation_cannot_strand_unregistered_grant(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("marker", "launch-marker"))
    _guard_fault(monkeypatch, "startup-cancel")
    launch = operations._launch_guard

    def cancel_during_launch(*args):
        signal.raise_signal(signal.SIGINT)
        return launch(*args)

    monkeypatch.setattr(operations, "_launch_guard", cancel_during_launch)
    result = _execute(root, domain)
    assert result.exit_code == 130
    assert result.runner_exit_code is None
    assert not (root / "launch-marker").exists()
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.CANCELLED


@pytest.mark.parametrize("checkpoint", ["queued", "prepared"])
def test_cancel_before_guard_launch_never_starts_work(case, monkeypatch, checkpoint):
    domain = case.domain()
    if checkpoint == "queued":
        blocker = _follower(case, domain)
        assert scheduler.poll(domain, blocker).state is C.LeaseState.GRANTED
    root = _command_project(case, domain, args=("marker", "launch-marker"))
    if checkpoint == "queued":
        poll = scheduler.poll

        def cancel_queued(*args):
            state = poll(*args)
            signal.raise_signal(signal.SIGTERM)
            return state

        monkeypatch.setattr(scheduler, "poll", cancel_queued)
    else:
        adapter = operations.adapter_for(C.RunnerKind.COMMAND)

        def cancel_prepared(*args):
            prepared = adapter.prepare(*args)
            signal.raise_signal(signal.SIGTERM)
            return prepared

        monkeypatch.setattr(operations, "adapter_for", lambda _: replace(adapter, _prepare=cancel_prepared))
    launches = []
    launch = operations._launch_guard

    def observe_launch(*args):
        launches.append(True)
        return launch(*args)

    monkeypatch.setattr(operations, "_launch_guard", observe_launch)
    result = _execute(root, domain)
    assert result.exit_code == 143
    assert not launches
    assert not (root / "launch-marker").exists()


@pytest.mark.parametrize("raw, expected", [(0, 130), (1, 1), (2, 2)])
def test_running_cancellation_preserves_cooperative_child_exit(case, raw, expected):
    domain = case.domain()
    ready_path = domain.root / "ready"
    os.mkfifo(ready_path)
    ready = os.open(ready_path, os.O_RDWR | os.O_NONBLOCK)
    root = _command_project(case, domain, args=("cancel", str(ready_path), str(raw)))

    def cancel_when_ready():
        assert select.select([ready], [], [], 5)[0], "runner readiness watchdog expired"
        assert os.read(ready, 5) == b"ready"
        os.kill(os.getpid(), signal.SIGINT)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            cancelled = pool.submit(cancel_when_ready)
            result = _execute(root, domain)
            cancelled.result(timeout=5)
    finally:
        os.close(ready)
    assert result.exit_code == expected
    assert result.runner_exit_code == raw
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


def test_missing_raw_facts_without_launch_problem_cannot_invent_127(case, monkeypatch):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    _guard_fault(monkeypatch, "missing-raw")
    result = _execute(root, domain)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert result.runner_exit_code is None


@pytest.mark.parametrize("error", [C.Problem(code="control-unavailable", message="launch failed", phase="guard"), OSError("launch failed")])
def test_guard_launch_failure_returns_incomplete_and_revokes_pending_grant(case, monkeypatch, error):
    domain = case.domain()
    root = _command_project(case, domain, args=("marker", "launch-marker"))

    def fail(*args):
        raise error

    monkeypatch.setattr(operations, "_launch_guard", fail)
    result = _execute(root, domain, result_path="result.json")
    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.INCOMPLETE, 70, None)
    assert not (root / "launch-marker").exists()
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.CANCELLED
    assert json.loads((root / "result.json").read_text())["data"]["status"] == "incomplete"
