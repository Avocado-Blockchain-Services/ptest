"""Cloud Run forwarding for the explicit database-cleanup profile."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def cloudrun_submission(ptest, tmp_path, monkeypatch):
    archive = ptest.pack_tree(tmp_path)
    monkeypatch.setattr(ptest, "pack_tree", lambda root, entries=None: archive)
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "at_concurrency_limit", lambda *args: False)
    monkeypatch.setattr(ptest, "budget_check", lambda *args: True)
    monkeypatch.setattr(ptest, "coalesce_remote_run", lambda key, submit: submit())

    calls = []

    def fake_gcloud(args, **kwargs):
        calls.append(args)
        if "objects" in args and "describe" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="404 not found")
        if "storage" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "executions" in args and "describe" in args:
            return SimpleNamespace(
                returncode=0,
                stdout='{"status":{"conditions":[{"type":"Completed","status":"True"}],'
                       '"succeededCount":1,"completionTime":"2026-08-03T12:00:00Z"}}',
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="ptest-api-abcde\n",
            stderr="",
        )

    monkeypatch.setattr(ptest.subprocess, "run", fake_gcloud)
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: "1 passed\n")
    return tmp_path, calls


def _remote_env_vars(calls):
    execute = next(call for call in calls if "execute" in call)
    return execute[execute.index("--update-env-vars") + 1]


def test_remote_profile_opt_in_forwards_only_the_explicit_diagnostic_flag(
    ptest, cloudrun_submission, monkeypatch
):
    """Removing the explicit opt-in must stop profile output reaching Cloud Run."""
    root, calls = cloudrun_submission
    monkeypatch.setenv("PTEST_PROFILE_DB_CLEANUP", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leave-the-client")

    assert ptest.run_cloudrun(
        {"job": "ptest-api", "bucket": "ptest-sources", "gcp_project": "test-project"},
        {}, "api", root, "pytest tests",
    ) == 0

    assert _remote_env_vars(calls).endswith(
        ",PTEST_PROFILE_DB_CLEANUP=1"
    )
    assert "UNRELATED_SECRET" not in _remote_env_vars(calls)


def test_remote_profile_opt_in_is_absent_from_the_default_command(
    ptest, cloudrun_submission, monkeypatch
):
    """The normal remote command must retain its two existing runner variables."""
    root, calls = cloudrun_submission
    monkeypatch.delenv("PTEST_PROFILE_DB_CLEANUP", raising=False)

    assert ptest.run_cloudrun(
        {"job": "ptest-api", "bucket": "ptest-sources", "gcp_project": "test-project"},
        {}, "api", root, "pytest tests",
    ) == 0

    assert _remote_env_vars(calls).split(",")[-1] == "PTEST_CMD=pytest tests"
    assert "PTEST_PROFILE_DB_CLEANUP" not in _remote_env_vars(calls)
