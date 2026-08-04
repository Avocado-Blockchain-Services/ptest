"""Same-host deduplication for identical Cloud Run test submissions."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import multiprocessing
import os
from pathlib import Path

import pytest


def _load_ptest(script: str):
    """Load a private module copy in a child process."""
    loader = importlib.machinery.SourceFileLoader(
        f"ptest_dedup_child_{os.getpid()}", script
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _coalesce_worker(
    script: str,
    state_dir: str,
    key: str,
    entered,
    contended,
    release,
    submissions,
    results,
    exit_code: int,
    output: str,
):
    ptest = _load_ptest(script)
    if contended is not None:
        original_flock = ptest.fcntl.flock

        def track_contention(fd, operation):
            try:
                return original_flock(fd, operation)
            except BlockingIOError:
                contended.set()
                raise

        ptest.fcntl.flock = track_contention

    def submit():
        with submissions.get_lock():
            submissions.value += 1
        entered.set()
        assert release.wait(5), "test did not release the fake remote submission"
        return exit_code, output

    results.put(
        ptest.coalesce_remote_run(
            key, submit, state_dir=Path(state_dir)
        )
    )


def _crashing_worker(script: str, state_dir: str, key: str, entered):
    ptest = _load_ptest(script)

    def crash():
        entered.set()
        os._exit(23)

    ptest.coalesce_remote_run(key, crash, state_dir=Path(state_dir))


def _run_two_callers(
    ptest,
    tmp_path: Path,
    *,
    exit_code: int,
    output: str,
):
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    contended = ctx.Event()
    release = ctx.Event()
    submissions = ctx.Value("i", 0)
    results = ctx.Queue()
    common = (
        str(ptest.__file__),
        str(tmp_path),
        "same-run",
        entered,
    )
    tail = (
        release,
        submissions,
        results,
        exit_code,
        output,
    )
    first = ctx.Process(target=_coalesce_worker, args=common + (None,) + tail)
    first.start()
    assert entered.wait(5), "neither caller became the submission owner"

    second = ctx.Process(target=_coalesce_worker, args=common + (contended,) + tail)
    second.start()
    assert contended.wait(5), "the second caller did not contend on the OS lock"
    release.set()
    first.join(5)
    second.join(5)
    assert first.exitcode == 0
    assert second.exitcode == 0
    return submissions, [results.get(timeout=1), results.get(timeout=1)]


def test_snapshot_digest_is_stable_but_changes_with_packaged_source(ptest, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "example.py"
    source.write_text("answer = 41\n")

    first = ptest.snapshot_digest(ptest.pack_tree(root))
    os.utime(source, (1_800_000_000, 1_800_000_000))
    second = ptest.snapshot_digest(ptest.pack_tree(root))
    source.write_text("answer = 42\n")
    changed = ptest.snapshot_digest(ptest.pack_tree(root))

    assert first == second, "filesystem timestamps are not source changes"
    assert changed != first, "changed packaged bytes must produce a new snapshot"


def test_remote_run_key_is_sensitive_to_every_identity_field(ptest):
    fields = {
        "snapshot": "source-a",
        "command": "pytest tests -n auto",
        "job": "ptest-api",
        "region": "us-central1",
        "project": "test-project",
    }
    base = ptest.remote_run_key(**fields)

    for field in fields:
        changed = dict(fields)
        changed[field] += "-different"
        assert ptest.remote_run_key(**changed) != base, field

    assert ptest.remote_run_key(**fields) == base


def test_concurrent_success_submits_once_and_is_reused(ptest, tmp_path):
    submissions, results = _run_two_callers(
        ptest, tmp_path, exit_code=0, output="12 passed\n"
    )

    assert submissions.value == 1
    assert results == [(0, "12 passed\n"), (0, "12 passed\n")]

    def must_not_submit():
        raise AssertionError("a fresh successful result must be reused")

    assert ptest.coalesce_remote_run(
        "same-run", must_not_submit, state_dir=tmp_path
    ) == (0, "12 passed\n")


def test_waiter_replays_failure_but_later_caller_retries(ptest, tmp_path):
    submissions, results = _run_two_callers(
        ptest, tmp_path, exit_code=1, output="FAILED test_example.py::test_it\n"
    )

    assert submissions.value == 1
    assert results == [
        (1, "FAILED test_example.py::test_it\n"),
        (1, "FAILED test_example.py::test_it\n"),
    ]

    retried = []

    def retry():
        retried.append(True)
        return 0, "1 passed\n"

    assert ptest.coalesce_remote_run(
        "same-run", retry, state_dir=tmp_path
    ) == (0, "1 passed\n")
    assert retried == [True]


def test_success_older_than_ten_minutes_is_not_reused(ptest, tmp_path, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(ptest.time, "time", lambda: now)
    assert ptest.coalesce_remote_run(
        "stale-run", lambda: (0, "old\n"), state_dir=tmp_path
    ) == (0, "old\n")

    now += 601
    assert ptest.coalesce_remote_run(
        "stale-run", lambda: (0, "new\n"), state_dir=tmp_path
    ) == (0, "new\n")


def test_crashed_owner_releases_lock_and_leaves_no_reusable_result(ptest, tmp_path):
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    owner = ctx.Process(
        target=_crashing_worker,
        args=(str(ptest.__file__), str(tmp_path), "crashed-run", entered),
    )
    owner.start()
    assert entered.wait(5)
    owner.join(5)
    assert owner.exitcode == 23

    assert ptest.coalesce_remote_run(
        "crashed-run", lambda: (0, "replacement\n"), state_dir=tmp_path
    ) == (0, "replacement\n")


@pytest.mark.parametrize(
    ("exit_code", "output"),
    [(0, "12 passed\n"), (1, "FAILED test_example.py::test_it\n")],
)
def test_replayed_run_cloudrun_result_bypasses_accounting_and_keeps_contract(
    ptest, tmp_path, monkeypatch, capsys, exit_code, output
):
    archive = ptest.pack_tree(tmp_path)
    monkeypatch.setattr(ptest, "pack_tree", lambda root: archive)
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(
        ptest,
        "coalesce_remote_run",
        lambda key, submit: (exit_code, output),
    )

    def accounting_must_not_run(*args, **kwargs):
        raise AssertionError("replayed results must not consume a remote-run slot")

    monkeypatch.setattr(ptest, "at_concurrency_limit", accounting_must_not_run)
    monkeypatch.setattr(ptest, "budget_check", accounting_must_not_run)
    pcfg = {
        "job": "ptest-api",
        "bucket": "ptest-sources",
        "region": "us-central1",
        "gcp_project": "test-project",
    }

    assert ptest.run_cloudrun(
        pcfg, {}, "api", tmp_path, "pytest tests -n auto"
    ) == exit_code
    assert capsys.readouterr().out == output
