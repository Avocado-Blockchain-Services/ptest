"""Same-host deduplication for identical Cloud Run test submissions."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace

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


def _crashing_worker(script: str, state_dir: str, key: str, entered, crash_now=None):
    ptest = _load_ptest(script)

    def crash():
        entered.set()
        if crash_now is not None:
            assert crash_now.wait(5), "test did not release the crashing owner"
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
    ("old_exit_code", "old_completed_at"),
    [(0, 0.0), (1, None)],
    ids=["stale-success", "failure"],
)
def test_waiter_becomes_owner_after_replacement_owner_crashes(
    ptest, tmp_path, old_exit_code, old_completed_at
):
    """An old-boot monotonic value must not masquerade as the result we awaited."""
    key = f"old-record-{old_exit_code}"
    old_record = {
        "exit_code": old_exit_code,
        "output": "old result must not replay\n",
        "completed_at": (
            ptest.time.time() if old_completed_at is None else old_completed_at
        ),
        # Simulate a record from a long-uptime boot followed by a reboot. The
        # persisted monotonic value is then larger than this boot's wait time.
        "completed_ns": 10**30,
    }
    (tmp_path / f"{key}.json").write_text(json.dumps(old_record))

    ctx = multiprocessing.get_context("spawn")
    owner_entered = ctx.Event()
    crash_now = ctx.Event()
    owner = ctx.Process(
        target=_crashing_worker,
        args=(str(ptest.__file__), str(tmp_path), key, owner_entered, crash_now),
    )
    owner.start()
    assert owner_entered.wait(5), "replacement owner did not start"

    waiter_entered = ctx.Event()
    contended = ctx.Event()
    waiter_release = ctx.Event()
    submissions = ctx.Value("i", 0)
    results = ctx.Queue()
    waiter = ctx.Process(
        target=_coalesce_worker,
        args=(
            str(ptest.__file__), str(tmp_path), key, waiter_entered, contended,
            waiter_release, submissions, results, 0, "replacement result\n",
        ),
    )
    waiter.start()
    assert contended.wait(5), "waiter did not block on the replacement owner"

    waiter_release.set()
    crash_now.set()
    owner.join(5)
    waiter.join(5)

    assert owner.exitcode == 23
    assert waiter.exitcode == 0
    assert submissions.value == 1, "waiter must replace the crashed owner"
    assert results.get(timeout=1) == (0, "replacement result\n")


def test_distinct_snapshots_in_same_second_use_distinct_gcs_objects(
    ptest, tmp_path, monkeypatch, capsys
):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "source.py").write_text("value = 1\n")
    (second_root / "source.py").write_text("value = 2\n")
    archives = iter([ptest.pack_tree(first_root), ptest.pack_tree(second_root)])

    monkeypatch.setattr(ptest, "pack_tree", lambda root, entries=None: next(archives))
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "at_concurrency_limit", lambda *args: False)
    monkeypatch.setattr(ptest, "budget_check", lambda *args: True)
    monkeypatch.setattr(ptest, "coalesce_remote_run", lambda key, submit: submit())
    monkeypatch.setattr(ptest, "fetch_execution_logs", lambda *args: "1 passed\n")
    monkeypatch.setattr(ptest.time, "time", lambda: 12_345.0)

    objects = []

    def fake_gcloud(args, **kwargs):
        if "objects" in args and "describe" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="404 not found")
        if "storage" in args:
            uploaded = next((part for part in args if part.startswith("gs://")), "")
            if "cp" in args and uploaded.endswith(".tar.gz"):
                objects.append(uploaded)
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
    pcfg = {
        "job": "ptest-api",
        "bucket": "ptest-sources",
        "region": "us-central1",
        "gcp_project": "test-project",
    }

    assert ptest.run_cloudrun(pcfg, {}, "api", first_root, "pytest tests") == 0
    assert ptest.run_cloudrun(pcfg, {}, "api", second_root, "pytest tests") == 0
    capsys.readouterr()

    assert len(objects) == 2
    assert objects[0] != objects[1]


@pytest.mark.parametrize(
    ("exit_code", "output"),
    [(0, "12 passed\n"), (1, "FAILED test_example.py::test_it\n")],
)
def test_replayed_run_cloudrun_result_bypasses_accounting_and_keeps_contract(
    ptest, tmp_path, monkeypatch, capsys, exit_code, output
):
    archive = ptest.pack_tree(tmp_path)
    monkeypatch.setattr(ptest, "pack_tree", lambda root, entries=None: archive)
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
