"""Task11 guarded command execution acceptance fixtures."""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
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


def test_generic_command_full_retains_selection_disabled_plan_reason(case):
    request = C.RunRequest(mode=C.Mode.FULL)

    plan = operations._plan(request)

    assert plan.execution == "full"
    assert plan.reasons == (C.Reason(
        code="selection-disabled",
        message="command profiles execute the configured full gate",
    ),)


def test_shadow_report_and_guard_exit_codes_must_match():
    evidence = C.AttemptEvidence(
        attempt_id="a001",
        result=C.AttemptResult(
            attempt_id="a001", phase="execution", status=C.Status.PASSED,
            raw_exit_code=0, final_exit_code=0, inventory_complete=True),
        inventory=None, terminal_complete=True, parallel_identity=False,
        runtime_identity=None,
    )
    assert operations._shadow_report_matches_guard(evidence, 0)
    assert not operations._shadow_report_matches_guard(evidence, 1)
    mismatched = replace(evidence, result=replace(evidence.result,
                                                    final_exit_code=1))
    assert not operations._shadow_report_matches_guard(mismatched, 0)


def test_shadow_public_outcome_preserves_raw_first_failure_and_redacts_values():
    outcome = operations._shadow_outcome(raw_codes=(0, 23), gate_count=2)
    assert outcome == (C.Status.FAILED, 23, "runner", None, 23)
    assert all(token not in json.dumps(outcome, default=str)
               for token in ("PTEST_", "--cov", "secret"))


def test_shadow_guard_problem_cannot_be_reported_as_a_pass():
    problem = C.Problem(
        code="ownership-uncertain", message="live descendants", phase="guard")
    outcome = operations._shadow_outcome(
        raw_codes=(0, 0), gate_count=2, guard_problem=problem)
    assert outcome[0:3] == (C.Status.INCOMPLETE, 70, "ptest")


def test_pytest_full_plan_is_the_only_early_full_plan_without_generic_reason(case):
    request = C.RunRequest(mode=C.Mode.FULL)

    plan = operations._plan(request, C.RunnerKind.PYTEST)

    assert plan.execution == "full"
    assert plan.reasons == ()


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


def _execute(root, domain, *, mode=C.Mode.FULL, **options):
    config = config_api.resolve_config(root).config
    return operations.execute(domain, config, C.RunRequest(mode=mode, **options))


def _git_command_project(case, domain, *, args=()):
    root = _command_project(case, domain, args=args)
    (root / "runtime-input.txt").write_text("original input")
    (root / ".gitignore").write_text("ignored-input.txt\n")
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")
    for argv in (("init",), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(("git", "-c", "core.hooksPath=" + os.devnull,
                        "-c", "commit.gpgsign=false", "-C", str(root), *argv),
                       env=env, check=True, capture_output=True)
    return root


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


def test_command_unchanged_source_preserves_runner_result_and_records_snapshots(case):
    domain = case.domain()
    root = _git_command_project(case, domain, args=("exit", "0"))

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.PASSED, 0, 0)
    assert result.input_before.digest is not None
    assert result.input_before.digest == result.input_after.digest
    assert result.input_before.clean and result.input_after.clean
    assert result.input_before.files == result.input_after.files
    assert result.input_before.compatibility is result.input_after.compatibility is None
    assert result.source_valid is False
    assert any(reason.code == "unknown-input" for reason in result.limitations)
    assert result.baseline_published is False
    assert result.full_gate_eligible is False


@pytest.mark.parametrize("mode, raw, status, final", [
    (C.Mode.FULL, 0, C.Status.INCOMPLETE, 70),
    (C.Mode.FULL, 23, C.Status.INCOMPLETE, 23),
    (C.Mode.AUTOMATIC, 0, C.Status.INCOMPLETE, 70),
    (C.Mode.SCOPED, 0, C.Status.PASSED, 0),
    (C.Mode.SCOPED, 23, C.Status.FAILED, 23),
])
def test_command_tracked_source_change_preserves_mode_exit_contract(case, mode, raw, status, final):
    domain = case.domain()
    root = _git_command_project(case, domain, args=("modify-exit", "runtime-input.txt", str(raw)))

    result = _execute(root, domain, mode=mode, result_path="result.json")

    assert (result.status, result.exit_code, result.runner_exit_code) == (status, final, raw)
    assert result.exit_origin == ("ptest" if final == 70 else "runner")
    assert result.input_before.digest is not None and result.input_after.digest is not None
    assert result.input_before.digest != result.input_after.digest
    assert (root / "runtime-input.txt").read_text() == "runner modification"
    assert result.source_valid is False
    assert result.full_gate_eligible is False
    assert result.baseline_published is False
    assert result.attempts[0].status is status
    assert result.attempts[0].raw_exit_code == raw
    assert result.attempts[0].final_exit_code == final
    assert result.attempts[0].source_valid is False
    changed_reason = next(reason for reason in result.reasons
                          if reason.code == "changed-during-run")
    assert changed_reason.paths == ()
    assert "tracked" in changed_reason.message
    assert "runtime-input.txt" not in changed_reason.message
    assert "runner modification" not in changed_reason.message
    assert len(changed_reason.message) < 256
    exported = json.loads((root / "result.json").read_text())["data"]
    assert exported["status"] == status.value
    assert exported["runner_exit_code"] == raw
    assert exported["exit_code"] == final
    assert exported["source_valid"] is exported["full_gate_eligible"] is exported["baseline_published"] is False
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


@pytest.mark.parametrize("path_class", ["untracked", "ignored"])
def test_command_changed_path_class_evidence_excludes_paths_and_content(case, path_class):
    domain = case.domain()
    path = f"{path_class}-input.txt"
    root = _git_command_project(case, domain, args=("modify-exit", path, "0"))
    # An unrelated dirty tracked file must not appear in changed-during-run classes.
    (root / "runtime-input.txt").write_text("prior edit")
    (root / path).write_text("before")

    result = _execute(root, domain)

    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70)
    reason = next(reason for reason in result.reasons if reason.code == "changed-during-run")
    assert path_class in reason.message
    assert "classes: tracked" not in reason.message
    assert reason.paths == ()
    assert path not in reason.message and "runner modification" not in reason.message
    assert len(reason.message) < 256


@pytest.mark.parametrize("raw, final", [(0, 70), (23, 23)])
def test_command_known_to_unknown_source_cannot_pass_full_gate(case, raw, final):
    domain = case.domain()
    root = _git_command_project(case, domain, args=("modify-mode-exit", "runtime-input.txt", str(raw)))

    result = _execute(root, domain)

    assert result.input_before.digest is not None
    assert result.input_after.digest is None
    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.INCOMPLETE, final, raw)
    assert result.exit_origin == ("ptest" if raw == 0 else "runner")
    assert result.source_valid is result.full_gate_eligible is result.baseline_published is False
    reason = next(reason for reason in result.reasons if reason.code == "unknown-input")
    assert reason.paths == () and "runtime-input.txt" not in reason.message
    assert any(reason.code == "unknown-input" for reason in result.limitations)
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


def test_command_unknown_snapshot_is_not_source_valid_or_gate_eligible(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.PASSED, 0, 0)
    assert result.source_valid is False
    assert result.full_gate_eligible is False
    assert result.baseline_published is False
    assert result.input_before.digest is None
    assert result.input_after.digest is None
    assert any(reason.code == "unknown-input" for reason in result.limitations)


def _pytest_project(case, domain, *, workers=1):
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_native.py").write_text("def test_body():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-q"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        f"workers = {workers}\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    return root


def _git_pytest_project(case, domain):
    root = _pytest_project(case, domain)
    config_path = root / ".ptest.toml"
    config_path.write_text(config_path.read_text().replace(
        'args = ["-q"]', 'args = ["-q", "-p", "no:xdist"]'))
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")
    for argv in (("init",), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(("git", "-c", "core.hooksPath=" + os.devnull,
                        "-c", "commit.gpgsign=false", "-C", str(root), *argv),
                       env=env, check=True, capture_output=True)
    return root


def test_pytest_full_missing_source_evidence_is_incomplete_while_command_full_passes(case):
    """Required native identity stops at a001; command full keeps its semantics."""
    domain = case.domain()
    command_root = _command_project(case, domain, args=("exit", "0"))
    command_result = _execute(command_root, domain)
    assert (command_result.status, command_result.exit_code,
            command_result.runner_exit_code) == (C.Status.PASSED, 0, 0)

    pytest_root = _pytest_project(case, domain)
    pytest_result = _execute(pytest_root, domain)

    assert pytest_result.input_before.digest is None
    assert (pytest_result.status, pytest_result.exit_code,
            pytest_result.runner_exit_code) == (C.Status.INCOMPLETE, 70, None)
    assert pytest_result.exit_origin == "ptest"
    assert pytest_result.source_valid is False
    assert pytest_result.full_gate_eligible is False
    assert any(reason.code == "unknown-input" for reason in pytest_result.reasons)
    assert pytest_result.attempts[0].status is C.Status.NOT_RUN
    assert pytest_result.attempts[0].raw_exit_code is None
    assert pytest_result.attempts[0].final_exit_code is None
    assert pytest_result.attempts[0].inventory_complete is False


def test_pytest_full_equal_execution_only_digests_pass_despite_missing_compatibility(case):
    """Equal full-policy digests carry the native outcome; compatibility alone never blocks."""
    domain = case.domain()
    root = _git_pytest_project(case, domain)
    assert 'args = ["-q", "-p", "no:xdist"]' in (root / ".ptest.toml").read_text()

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.PASSED, 0, 0)
    assert result.exit_origin == "runner"
    assert result.input_before.digest is not None
    assert result.input_before.digest == result.input_after.digest
    assert result.input_before.compatibility is None
    assert result.source_valid is False
    assert result.full_gate_eligible is False
    assert result.baseline_published is False
    assert result.counts is None
    assert any("execution-only" in reason.message for reason in result.limitations)
    assert any("inventory is not complete" in reason.message for reason in result.limitations)
    assert result.attempts[0].inventory_complete is False
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


def test_source_capture_follows_queue_changes_and_precedes_guard_finalization(case, monkeypatch):
    domain = case.domain()
    blocker = _follower(case, domain)
    assert scheduler.poll(domain, blocker).state is C.LeaseState.GRANTED
    root = _git_command_project(case, domain, args=("exit", "0"))
    events, snapshots = [], []
    poll, ensure = scheduler.poll, operations.source.ensure_fingerprint_key
    capture, run_guard = operations.source.snapshot, operations._run_guard
    begin, finish = scheduler.begin_finalization, scheduler.finish

    def queued_edit(*args):
        state = poll(*args)
        if state.state is C.LeaseState.QUEUED:
            assert not events
            assert not (domain.root / "input-hmac.key").exists()
            (root / "runtime-input.txt").write_text("edited while queued")
            events.append("queued-edit")
            assert scheduler.cancel_pending(domain, blocker, platform.process_identity(os.getpid()))
        return state

    def admitted_key(*args):
        assert events == ["queued-edit"]
        leases = scheduler.reconcile(domain)
        assert sum(lease.state is C.LeaseState.GRANTED for lease in leases) == 1
        events.append("key")
        return ensure(*args)

    def observed_capture(*args, **kwargs):
        expected_events = (
            ["queued-edit", "key"] if not snapshots
            else ["queued-edit", "key", "before"] if len(snapshots) == 1
            else ["queued-edit", "key", "before", "gate", "guard-reaped"]
        )
        assert events == expected_events
        assert (root / "runtime-input.txt").read_text() == "edited while queued"
        leases = scheduler.reconcile(domain)
        expected_state = (
            C.LeaseState.GRANTED if not snapshots
            else C.LeaseState.RUNNING if len(snapshots) == 1
            else C.LeaseState.DRAINING)
        assert any(lease.state is expected_state for lease in leases)
        snapshot = capture(*args, **kwargs)
        snapshots.append(snapshot)
        events.append(("before" if len(snapshots) == 1
                       else "gate" if len(snapshots) == 2 else "after"))
        return snapshot

    def observed_guard(*args):
        assert events[-1] == "before"
        result = run_guard(*args)
        frames = result[1]
        assert frames.registered and frames.phase and frames.facts and frames.draining and frames.eof
        assert frames.invalid is None
        events.append("guard-reaped")
        return result

    def observed_begin(*args):
        assert events[-1] == "after" and len(snapshots) == 3
        events.append("finalization")
        return begin(*args)

    def observed_finish(*args):
        assert events[-1] == "finalization"
        exported = json.loads((root / "result.json").read_text())["data"]
        assert args[-1].source_valid is exported["source_valid"] is False
        assert exported["status"] == "passed"
        events.append("exported-finish")
        return finish(*args)

    monkeypatch.setattr(scheduler, "poll", queued_edit)
    monkeypatch.setattr(operations.source, "ensure_fingerprint_key", admitted_key)
    monkeypatch.setattr(operations.source, "snapshot", observed_capture)
    monkeypatch.setattr(operations, "_run_guard", observed_guard)
    monkeypatch.setattr(scheduler, "begin_finalization", observed_begin)
    monkeypatch.setattr(scheduler, "finish", observed_finish)

    result = _execute(root, domain, result_path="result.json")

    assert result.exit_code == 0
    assert result.input_before is snapshots[0] and result.input_after is snapshots[2]
    assert result.input_before.digest is not None
    assert result.input_before.digest == result.input_after.digest
    assert events == ["queued-edit", "key", "before", "gate",
                      "guard-reaped", "after", "finalization",
                      "exported-finish"]


def test_cancellation_during_source_capture_revokes_grant_without_guard_launch(case, monkeypatch):
    domain = case.domain()
    root = _git_command_project(case, domain, args=("marker", "launch-marker"))
    capture = operations.source.snapshot
    launches = []
    launch = operations._launch_guard

    def capture_and_cancel(*args, **kwargs):
        result = capture(*args, **kwargs)
        signal.raise_signal(signal.SIGTERM)
        return result

    def observed_launch(*args):
        launches.append(True)
        return launch(*args)

    monkeypatch.setattr(operations.source, "snapshot", capture_and_cancel)
    monkeypatch.setattr(operations, "_launch_guard", observed_launch)

    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (C.Status.CANCELLED, 143, None)
    assert not launches
    assert not (root / "launch-marker").exists()
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.CANCELLED


def test_attempt_gate_blocks_launch_when_source_changed_after_admission(
        case, monkeypatch):
    domain = case.domain()
    root = _git_command_project(
        case, domain, args=("marker", "launch-marker"))
    run_guard = operations._run_guard

    def edit_before_ready(*args, **kwargs):
        (root / "runtime-input.txt").write_text("changed before ready")
        return run_guard(*args, **kwargs)

    monkeypatch.setattr(operations, "_run_guard", edit_before_ready)
    result = _execute(root, domain)

    assert (result.status, result.exit_code, result.runner_exit_code) == (
        C.Status.INCOMPLETE, 70, None)
    assert result.source_valid is False
    assert len(result.attempts) == 1
    assert result.attempts[0].status is C.Status.NOT_RUN
    assert result.attempts[0].raw_exit_code is None
    assert result.attempts[0].final_exit_code is None
    assert result.attempts[0].timings is None
    assert not (root / "launch-marker").exists()
    assert any(reason.code == "changed-during-run" for reason in result.reasons)
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


def test_pytest_unknown_identity_stops_at_first_gate_without_launch(case):
    domain = case.domain()
    root = _pytest_project(case, domain)
    marker = root / "native-launched"
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('launched')\n"
        "def test_body():\n"
        "    assert True\n"
    )

    result = _execute(root, domain)

    assert not marker.exists()
    assert (result.status, result.exit_code, result.runner_exit_code) == (
        C.Status.INCOMPLETE, 70, None)
    assert result.source_valid is False
    assert result.attempts[0].status is C.Status.NOT_RUN
    assert result.attempts[0].raw_exit_code is None
    assert result.attempts[0].final_exit_code is None
    assert result.attempts[0].timings is None
    assert any(reason.code == "unknown-input" for reason in result.reasons)
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.RELEASED


@pytest.mark.parametrize("boundary", ["ensure_fingerprint_key", "snapshot"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_unexpected_source_capture_failure_cannot_strand_granted_lease(case, monkeypatch, boundary, failure):
    domain = case.domain()
    root = _command_project(case, domain, args=("marker", "launch-marker"))

    def fail(*args, **kwargs):
        raise failure("injected capture failure")

    monkeypatch.setattr(operations.source, boundary, fail)
    with pytest.raises(failure, match="injected capture failure"):
        _execute(root, domain)

    assert not (root / "launch-marker").exists()
    assert scheduler.reconcile(domain)[0].state is C.LeaseState.CANCELLED


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


def test_guard_problem_reason_names_actual_cause(case, monkeypatch):
    """An ownership/lease guard failure names its cause, never a deadline."""
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "0"))
    _guard_fault(monkeypatch, "problem:ownership-uncertain:0")
    result = _execute(root, domain)
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70)
    assert any(reason.code == "ownership-uncertain"
               and reason.message == "injected guard failure"
               for reason in result.reasons)
    assert not any("exceeded its deadline" in reason.message
                   for reason in result.reasons)


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


def test_guard_launch_forwards_only_doctor_smoke_manifest(case, monkeypatch):
    """The parent-to-guard boundary allowlists the opt-in smoke manifest only.

    The real-repository smoke test reads ``PTEST_DOCTOR_SMOKE_MANIFEST`` from
    its runner environment, and that runner is a grandchild of the invoking
    ``ptest`` process via the guard. A hostile ``PTEST_*`` name set alongside
    it must still be excluded at the same boundary.
    """
    domain = case.domain()
    root = _command_project(case, domain)
    (root / "command.py").write_text(
        "import json, os\n"
        "open('seen-env.json', 'w').write(json.dumps({"
        "'manifest': os.environ.get('PTEST_DOCTOR_SMOKE_MANIFEST'), "
        "'hostile': os.environ.get('PTEST_HOSTILE_PROBE')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PTEST_DOCTOR_SMOKE_MANIFEST", "/tmp/smoke-manifest.json")
    monkeypatch.setenv("PTEST_HOSTILE_PROBE", "must-not-cross-guard")

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    seen = json.loads((root / "seen-env.json").read_text(encoding="utf-8"))
    assert seen["manifest"] == "/tmp/smoke-manifest.json"
    assert seen["hostile"] is None


_FAKE_NODE_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "root = Path(os.getcwd())\n"
    "(root / 'node-record.json').write_text(json.dumps(\n"
    "    {'argv': sys.argv, 'cwd': os.getcwd()}))\n"
    "with open(root / 'order.log', 'a') as log:\n"
    "    log.write('node\\n')\n"
    "exit_file = root / 'node-exit'\n"
    "raise SystemExit(int(exit_file.read_text().strip()) if exit_file.exists() else 0)\n"
)

_SETUP_SCRIPT = (
    "from pathlib import Path\n"
    "Path('node_modules').mkdir(exist_ok=True)\n"
    "with open('order.log', 'a') as log:\n"
    "    log.write('setup\\n')\n"
)


def _fake_node(root: Path) -> str:
    node = root / "node"
    node.write_text(_FAKE_NODE_SCRIPT, encoding="utf-8")
    node.chmod(0o755)
    return str(node)


def _vitest_project(case, domain, *, args=(), full_args=(), setup=False):
    root = case.project(domain, kind="vitest")
    node = _fake_node(root)
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1
    )[1].split('"', 1)[0]
    config = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([node])}\n"
        f"args = {json.dumps(list(args))}\n"
        f"full_args = {json.dumps(list(full_args))}\n"
        'kind = "vitest"\n'
        'test_roots = ["."]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
    )
    if setup:
        (root / "setup.py").write_text(_SETUP_SCRIPT, encoding="utf-8")
        config += (
            "[setup]\n"
            f"argv = {json.dumps([sys.executable, 'setup.py'])}\n"
            'required_paths = ["node_modules"]\n'
            "network = false\n"
            "lifecycle_scripts = false\n"
        )
    (root / ".ptest.toml").write_text(config, encoding="utf-8")
    return root


def _node_record(root: Path) -> dict:
    return json.loads((root / "node-record.json").read_text(encoding="utf-8"))


def test_vitest_scoped_executes_literal_exclusive_command(case):
    domain = case.domain()
    root = _vitest_project(case, domain)

    completed = case.invoke(domain, root, "--", "src/a.test.ts", timeout=20)

    assert completed.code == 0
    record = _node_record(root)
    assert record["argv"][0] == str(root / "node")
    assert record["argv"][1:] == ["node_modules/vitest/vitest.mjs", "run", "src/a.test.ts"]
    assert record["cwd"] == str(root)
    result = _run_data(completed)
    assert result["status"] == "passed"
    assert result["runner_exit_code"] == 0
    assert result["plan"]["execution"] == "scoped"


def test_vitest_failure_preserves_native_exit(case):
    domain = case.domain()
    root = _vitest_project(case, domain)
    (root / "node-exit").write_text("1", encoding="utf-8")

    completed = case.invoke(domain, root, "--", "src/a.test.ts", timeout=20)

    assert completed.code == 1
    result = _run_data(completed)
    assert result["status"] == "failed"
    assert result["runner_exit_code"] == 1


def test_vitest_full_appends_full_args(case):
    domain = case.domain()
    root = _vitest_project(case, domain,
                           args=("--reporter", "verbose"), full_args=("--coverage",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    record = _node_record(root)
    assert record["argv"][1:] == ["node_modules/vitest/vitest.mjs", "run",
                                  "--reporter", "verbose", "--coverage"]
    assert _run_data(completed)["plan"]["execution"] == "full"


def test_vitest_setup_runs_first_when_required_paths_missing(case):
    domain = case.domain()
    root = _vitest_project(case, domain, setup=True)

    completed = case.invoke(domain, root, "--", "src/a.test.ts", timeout=20)

    assert completed.code == 0
    assert (root / "order.log").read_text(encoding="utf-8").splitlines() == ["setup", "node"]
    assert (root / "node_modules").is_dir()


def test_vitest_setup_is_skipped_once_current(case):
    domain = case.domain()
    root = _vitest_project(case, domain, setup=True)

    assert case.invoke(domain, root, "--", "src/a.test.ts", timeout=20).code == 0
    assert case.invoke(domain, root, "--", "src/a.test.ts", timeout=20).code == 0

    assert (root / "order.log").read_text(encoding="utf-8").splitlines() == ["setup", "node", "node"]


def test_setup_summary_kind_matches_vitest_config(case):
    from ptest import operations as ops_module
    from ptest.adapters import vitest as vitest_adapter

    domain = case.domain()
    root = _vitest_project(case, domain, setup=True)
    config = config_api.resolve_config(root).config
    checkout = case.checkout(domain)
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())
    grant = C.Grant(run_id="ab" * 16, nonce="cd" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=0, domain_id="ef" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="pt_prefix", worker_count=1)
    prepared = vitest_adapter.prepare(config, plan, grant, attempt)

    setup_prepared = ops_module._setup_prepared(
        config, replace(checkout, root=root), C.RunRequest(mode=C.Mode.SCOPED),
        prepared, domain, grant, attempt)

    assert setup_prepared is not None
    assert setup_prepared.summary.kind is C.RunnerKind.VITEST


@pytest.mark.parametrize("kind", ["go", "cargo"])
def test_native_go_cargo_execution_remains_deferred(case, kind):
    domain = case.domain()
    root = case.project(domain, kind=kind)
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8")
        + 'test_roots = ["tests"]\n',
        encoding="utf-8",
    )

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 2
    assert b"native profile execution is deferred" in completed.stderr
    assert completed.result is None


def test_monorepo_scope_routes_to_vitest_child(case):
    domain = case.domain()
    root = case.project(domain, kind="command")
    web = root / "web"
    web.mkdir()
    node = _fake_node(web)
    (web / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{"cd" * 16}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([node])}\n"
        "args = []\n"
        "full_args = []\n"
        'kind = "vitest"\n'
        'test_roots = ["."]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    (root / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"web\"]\n", encoding="utf-8")

    completed = case.invoke(domain, root, "--", "web/src/a.test.ts", timeout=20)

    assert completed.code == 0
    record = _node_record(web)
    assert record["argv"][1:] == ["node_modules/vitest/vitest.mjs", "run", "src/a.test.ts"]
    assert record["cwd"] == str(web)


# --- run_setup_only honest skip/failure (shared _finish_setup) ----------------


def _setup_only_config(case, root):
    return case.config(
        setup=C.SetupConfig(
            argv=("true",), required_paths=("made.txt",),
            network=False, lifecycle_scripts=False),
        checkout=C.CheckoutIdentity(
            project_id="ab" * 16, checkout_id="cd" * 16, root=root),
    )


def _stored_setup_fingerprint(domain, config):
    return operations._stored_setup_fingerprint(
        domain, operations._checkout(config))


def test_setup_only_missing_paths_raise_problem(case, tmp_path, monkeypatch):
    """A passing setup that leaves required paths missing is not PASSED."""
    domain = case.domain()
    config = _setup_only_config(case, tmp_path)
    monkeypatch.setattr(
        operations, "execute",
        lambda *args, **kwargs: case.result(status="passed"))
    with pytest.raises(C.Problem, match="required setup path is missing"):
        operations.run_setup_only(domain, config, queue_timeout_s=5)
    assert _stored_setup_fingerprint(domain, config) is None


def test_setup_only_failed_command_records_no_fingerprint(
        case, tmp_path, monkeypatch):
    """A failed setup command returns its result and records no fingerprint."""
    (tmp_path / "made.txt").write_text("x", encoding="utf-8")
    domain = case.domain()
    config = _setup_only_config(case, tmp_path)
    monkeypatch.setattr(
        operations, "execute",
        lambda *args, **kwargs: case.result(status="failed"))
    result = operations.run_setup_only(domain, config, queue_timeout_s=5)
    assert result.status is C.Status.FAILED
    assert _stored_setup_fingerprint(domain, config) is None


def test_setup_only_changed_inputs_raise_problem(case, tmp_path, monkeypatch):
    """Tool/lock inputs changing during setup surface as a Problem."""
    (tmp_path / "made.txt").write_text("x", encoding="utf-8")
    domain = case.domain()
    config = _setup_only_config(case, tmp_path)
    monkeypatch.setattr(
        operations, "execute",
        lambda *args, **kwargs: case.result(status="passed"))
    digests = iter(["a" * 64, "a" * 64, "b" * 64])
    monkeypatch.setattr(
        operations, "_setup_fingerprint",
        lambda *args, **kwargs: next(digests))
    with pytest.raises(C.Problem) as excinfo:
        operations.run_setup_only(domain, config, queue_timeout_s=5)
    assert excinfo.value.code == "changed-during-run"
    assert _stored_setup_fingerprint(domain, config) is None


def test_setup_only_record_failure_raises_problem(case, tmp_path, monkeypatch):
    """An unrecordable fingerprint is a Problem, never a raw OSError."""
    (tmp_path / "made.txt").write_text("x", encoding="utf-8")
    domain = case.domain()
    config = _setup_only_config(case, tmp_path)
    monkeypatch.setattr(
        operations, "execute",
        lambda *args, **kwargs: case.result(status="passed"))

    def _unwritable(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(operations, "_record_setup_fingerprint", _unwritable)
    with pytest.raises(C.Problem) as excinfo:
        operations.run_setup_only(domain, config, queue_timeout_s=5)
    assert excinfo.value.code == "state-unavailable"
    assert _stored_setup_fingerprint(domain, config) is None


# --- Parallel tier (T2): slot requests and parallel-workers reasons ---

def _xdist_project(case, domain, *, addopts):
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1
    )[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "def test_body():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"%s\"\n" % addopts,
        encoding="utf-8")
    config = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
    )
    (root / ".ptest.toml").write_text(config, encoding="utf-8")
    resolved = config_api.resolve_config(root).config
    assert resolved is not None
    return resolved


def _reason_messages(result):
    return [(reason.code, reason.message) for reason in result.reasons]


def test_parallel_partial_grant_reports_requested_and_granted(case):
    """4 requested, 2 granted: result carries the exact partial message.

    Only merge-stable fields are pinned (workers, generated options, the
    reason); the run outcome itself belongs to the bridge (T1) and its
    integration test (T5).
    """
    from ptest import executability as E

    domain = case.domain(slots=2, jobs=2)
    config = _xdist_project(case, domain, addopts="-n 4")
    assert E.parallel_request(config).reason is None

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.granted_workers == 2
    assert result.command.workers == 4
    # Generated -n lives in the adapter argv (covered in test_pytest_adapter);
    # the operations command summary only records the requested worker count.
    assert list(result.command.generated_options) == []
    assert ("parallel-workers", "2 xdist workers (4 requested, 2 granted)") in (
        _reason_messages(result))


def test_parallel_serial_fallback_reports_serial_reason(case):
    domain = case.domain()
    config = _xdist_project(case, domain, addopts="-n 4 --dist=each")

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.status == C.Status.PASSED
    assert result.granted_workers == 1
    assert result.command.workers == 1
    assert ("parallel-workers",
            "serial: --dist each is not supported; ptest runs serially") in (
        _reason_messages(result))


def test_parallel_auto_requests_machine_slots(case):
    """-n auto resolves to the machine's max slots with no partial reason."""
    domain = case.domain(slots=2, jobs=2)
    config = _xdist_project(case, domain, addopts="--numprocesses=auto")

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.granted_workers == 2
    assert result.command.workers == 2
    assert [code for code, _ in _reason_messages(result)
            if code == "parallel-workers"] == []


def test_parallel_request_workers_cap_limits_request(case):
    """ptest --workers caps the requested count before admission."""
    domain = case.domain(slots=4, jobs=4)
    config = _xdist_project(case, domain, addopts="-n 4")

    result = operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.SCOPED, workers=2))

    assert result.granted_workers == 2
    assert result.command.workers == 2
    assert [code for code, _ in _reason_messages(result)
            if code == "parallel-workers"] == []


def test_parallel_oversized_request_caps_at_contract_ceiling(case):
    """-n 100 is capped to 64 before summary/admission (no ValueError).

    The scheduler then grants min(64, max_slots); the partial-grant reason
    reports the capped request.
    """
    from ptest import executability as E

    domain = case.domain(slots=2, jobs=2)
    config = _xdist_project(case, domain, addopts="-n 100")
    assert E.parallel_request(config).reason is None

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))

    assert result.granted_workers == 2
    assert result.command.workers == 64
    assert ("parallel-workers", "2 xdist workers (64 requested, 2 granted)") in (
        _reason_messages(result))


def test_caller_serial_spelling_keeps_qualified_project_serial(case):
    """A caller -n 0 serializes like a configured one (no admission error)."""
    domain = case.domain()
    config = _xdist_project(case, domain, addopts="-n 4")

    result = operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.SCOPED, argv=("-n", "0")))

    assert result.status == C.Status.PASSED
    assert result.granted_workers == 1
    assert result.command.workers == 1
    assert ("parallel-workers", "serial: .ptest.toml sets -n 0") in (
        _reason_messages(result))


def test_parallel_worker_reason_messages():
    from types import SimpleNamespace

    assert operations._parallel_worker_reason(None, 4, 2) is None
    qualified = SimpleNamespace(active=True, reason=None, runs=True)
    assert operations._parallel_worker_reason(qualified, 4, 4) is None
    assert operations._parallel_worker_reason(qualified, 1, 1) is None

    reason = operations._parallel_worker_reason(qualified, 4, 2)
    assert (reason.code, reason.message) == (
        "parallel-workers", "2 xdist workers (4 requested, 2 granted)")

    reason = operations._parallel_worker_reason(qualified, 4, 1)
    assert (reason.code, reason.message) == (
        "parallel-workers", "serial (4 requested, 1 granted)")

    fallback = SimpleNamespace(
        active=True,
        reason="--dist each is not supported; ptest runs serially", runs=True)
    reason = operations._parallel_worker_reason(fallback, 1, 1)
    assert (reason.code, reason.message) == (
        "parallel-workers",
        "serial: --dist each is not supported; ptest runs serially")

    remote = SimpleNamespace(
        active=True,
        reason="remote xdist workers (--tx, --rsyncdir, --px) are not supported",
        runs=False)
    assert operations._parallel_worker_reason(remote, 1, 1) is None

    inactive = SimpleNamespace(active=False, reason=None, runs=True)
    assert operations._parallel_worker_reason(inactive, 1, 1) is None
