import json
import subprocess

import pytest


BASE = ["gcloud", "--project=test-project"]


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def test_submit_execution_is_async_and_returns_the_exact_execution_name(ptest, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return completed(args, stdout="projects/p/locations/r/executions/fake-job-abc12\n")

    monkeypatch.setattr(ptest.subprocess, "run", run)

    execution = ptest.submit_execution(
        BASE, "fake-job", "us-central1", "gs://bucket/source.tar.gz", "run tests"
    )

    assert execution == "fake-job-abc12"
    args = calls[0][0]
    assert "--async" in args
    assert "--wait" not in args
    assert "--format=value(metadata.name)" in args
    assert "PTEST_SRC=gs://bucket/source.tar.gz,PTEST_CMD=run tests" in args


def test_remote_execution_guidance_explains_wait_and_recovery_commands(ptest, capsys):
    ptest.remote_execution_guidance(
        "ptest-api", "ptest-api-abc12", "us-central1", "test-project"
    )

    output = capsys.readouterr().err
    assert "running remotely on Cloud Run" in output
    assert "ptest-api-abc12" in output
    assert "gcloud run jobs executions describe ptest-api-abc12" in output
    assert "gcloud logging read" in output


def test_submit_failure_is_an_infrastructure_error(ptest, monkeypatch):
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: completed(args, 1, stderr="permission denied"),
    )
    with pytest.raises(ptest.CloudRunUnavailable):
        ptest.submit_execution(BASE, "fake-job", "us-central1", "gs://b/src", "tests")


@pytest.mark.parametrize(
    "status, terminal, succeeded, failed",
    [
        ({"runningCount": 1}, False, False, False),
        ({"completionTime": "2026-08-03T12:00:00Z", "succeededCount": 1}, True, True, False),
        ({"completionTime": "2026-08-03T12:00:00Z", "failedCount": 1}, True, False, True),
        ({"completionTime": "2026-08-03T12:00:00Z", "cancelledCount": 1}, True, False, False),
    ],
)
def test_describe_execution_normalizes_cloud_run_status(
    ptest, monkeypatch, status, terminal, succeeded, failed
):
    payload = {"metadata": {"name": "fake-job-abc12"}, "status": status}
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: completed(args, stdout=json.dumps(payload)),
    )

    state = ptest.describe_execution(BASE, "fake-job-abc12", "us-central1")

    assert state.terminal is terminal
    assert state.succeeded is succeeded
    assert state.failed is failed


def test_describe_failure_and_malformed_status_are_retryable(ptest, monkeypatch):
    responses = iter(
        [
            completed([], 1, stderr="temporarily unavailable"),
            completed([], stdout="not json"),
        ]
    )
    monkeypatch.setattr(ptest.subprocess, "run", lambda args, **kwargs: next(responses))

    with pytest.raises(ptest.CloudRunUnavailable):
        ptest.describe_execution(BASE, "fake-job-abc12", "us-central1")
    with pytest.raises(ptest.CloudRunUnavailable):
        ptest.describe_execution(BASE, "fake-job-abc12", "us-central1")


def test_wait_polls_to_success_and_returns_the_existing_summary(ptest, monkeypatch):
    states = iter(
        [
            ptest.ExecutionState("fake-job-abc12", False, False, False, ""),
            ptest.ExecutionState("fake-job-abc12", True, True, False, ""),
        ]
    )
    sleeps = []
    monkeypatch.setattr(ptest, "describe_execution", lambda *args: next(states))
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: "==== 10 passed in 2.0s ====\n")
    monkeypatch.setattr(ptest.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = ptest.wait_for_execution(BASE, "fake-job-abc12", "us-central1")

    assert result.exit_code == 0
    assert result.terminal_category == "success"
    assert "10 passed" in result.summary
    assert sleeps == [2]


def test_wait_classifies_test_failure_and_preserves_failure_digest(ptest, monkeypatch):
    state = ptest.ExecutionState("fake-job-abc12", True, False, True, "nonzero exit")
    logs = "===== FAILURES =====\nboom\n===== short test summary =====\nFAILED test_x.py::test_x\n"
    monkeypatch.setattr(ptest, "describe_execution", lambda *args: state)
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: logs)

    result = ptest.wait_for_execution(BASE, "fake-job-abc12", "us-central1")

    assert result.exit_code == 1
    assert result.terminal_category == "test_failure"
    assert "FAILED test_x.py::test_x" in result.summary


def test_wait_classifies_runner_fatal_and_cancellation_as_infrastructure(
    ptest, monkeypatch
):
    states = iter(
        [
            ptest.ExecutionState("fatal", True, False, True, "nonzero exit"),
            ptest.ExecutionState("cancelled", True, False, False, "cancelled"),
        ]
    )
    logs = iter(["[ptest-runner] FATAL source download failed\n", ""])
    monkeypatch.setattr(ptest, "describe_execution", lambda *args: next(states))
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: next(logs))

    fatal = ptest.wait_for_execution(BASE, "fatal", "us-central1")
    cancelled = ptest.wait_for_execution(BASE, "cancelled", "us-central1")

    assert fatal.exit_code is None and fatal.terminal_category == "infrastructure"
    assert cancelled.exit_code is None and cancelled.terminal_category == "infrastructure"


def test_wait_retries_transient_describe_errors_without_submitting_again(ptest, monkeypatch):
    attempts = iter(
        [
            ptest.CloudRunUnavailable("one"),
            ptest.CloudRunUnavailable("two"),
            ptest.ExecutionState("fake-job-abc12", True, True, False, ""),
        ]
    )

    def describe(*args):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ptest, "describe_execution", describe)
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: "")
    monkeypatch.setattr(ptest.time, "sleep", lambda *args: None)

    result = ptest.wait_for_execution(BASE, "fake-job-abc12", "us-central1")

    assert result.exit_code == 0
