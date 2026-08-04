from datetime import datetime, timedelta, timezone
from pathlib import Path


DIGEST = "a" * 64
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self, ptest):
        self.ptest = ptest
        self.records = {}
        self.generation = 0

    def seed(self, path, value):
        self.generation += 1
        self.records[path] = self.ptest.StoredJson(value, self.generation)

    def read_json(self, path):
        return self.records.get(path)

    def create_json(self, path, value):
        if path in self.records:
            return False
        self.seed(path, value)
        return True

    def replace_json(self, path, value, generation):
        current = self.records.get(path)
        if current is None or current.generation != generation:
            return False
        self.seed(path, value)
        return True

    def delete_if_generation(self, path, generation):
        current = self.records.get(path)
        if current is None or current.generation != generation:
            return False
        del self.records[path]
        return True

    def object_exists(self, path):
        return path in self.records


def configured():
    pcfg = {
        "job": "fake-job",
        "bucket": "private-bucket",
        "region": "us-central1",
        "gcp_project": "test-project",
        "kind": "vitest",
    }
    cfg = {"defaults": {"max_concurrent_remote_runs": 6}}
    return pcfg, cfg


def request_paths(ptest, command="run tests"):
    key = ptest.remote_request_key(
        {
            "schema": "ptest-remote-key-v1",
            "project": "front",
            "kind": "vitest",
            "command": command,
            "job": "fake-job",
            "region": "us-central1",
            "gcp_project": "test-project",
            "runner_namespace": "v1",
            "tree_digest": DIGEST,
        }
    )
    return ptest.coordination_paths(key, DIGEST)


def harness(ptest, monkeypatch, tmp_path, store, result=None):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"archive")
    calls = {"concurrency": 0, "budget": 0, "upload": 0, "submit": 0, "wait": 0}
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda root, entries=None: DIGEST)
    monkeypatch.setattr(ptest, "pack_tree", lambda root, entries=None: archive)
    monkeypatch.setattr(ptest, "utc_now", lambda: NOW)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *args: store)

    def concurrency(*args):
        calls["concurrency"] += 1
        return False

    def budget(*args):
        calls["budget"] += 1
        return True

    def upload(*args):
        calls["upload"] += 1
        return True

    def submit(*args):
        calls["submit"] += 1
        return "fake-job-abc12"

    def wait(*args):
        calls["wait"] += 1
        return result or ptest.ExecutionResult(
            "fake-job-abc12", "success", "", "==== 10 passed in 2s ====", 0, NOW
        )

    monkeypatch.setattr(ptest, "at_concurrency_limit", concurrency)
    monkeypatch.setattr(ptest, "budget_check", budget)
    monkeypatch.setattr(ptest, "upload_source_once", upload, raising=False)
    monkeypatch.setattr(ptest, "submit_execution", submit)
    monkeypatch.setattr(ptest, "wait_for_execution", wait)
    return calls


def test_fresh_passing_hit_bypasses_budget_upload_concurrency_and_submission(
    ptest, monkeypatch, tmp_path, capsys
):
    store = MemoryStore(ptest)
    paths = request_paths(ptest)
    store.seed(
        paths.passing,
        {"schema": "ptest-pass-v1", "status": "passed",
         "completed_at": (NOW - timedelta(minutes=5)).isoformat(),
         "execution": "fake-job-old", "summary": "==== 10 passed in 2s ===="},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls == {"concurrency": 0, "budget": 0, "upload": 0, "submit": 0, "wait": 0}
    output = capsys.readouterr()
    request_key = paths.claim.split("/")[-2]
    assert f"[ptest] request: {request_key}" in output.err
    assert "reused passing result" in output.err
    assert "10 passed" in output.out


def test_existing_execution_is_joined_without_owner_checks_and_success_is_cached(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    paths = request_paths(ptest)
    store.seed(
        paths.execution,
        {"schema": "ptest-execution-v1", "execution": "fake-job-shared",
         "submitted_at": (NOW - timedelta(minutes=1)).isoformat()},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        ptest, "describe_execution",
        lambda *args: ptest.ExecutionState("fake-job-shared", False, False, False, ""),
    )
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls["wait"] == 1
    assert calls["submit"] == calls["budget"] == calls["concurrency"] == 0
    assert store.read_json(paths.passing).value["status"] == "passed"


def test_owner_alone_checks_budget_and_concurrency_then_publishes_execution(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    calls = harness(ptest, monkeypatch, tmp_path, store)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls == {"concurrency": 1, "budget": 1, "upload": 1, "submit": 1, "wait": 1}
    assert store.read_json(request_paths(ptest).passing).value["status"] == "passed"


def test_live_claim_loser_waits_for_execution_instead_of_submitting(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    paths = request_paths(ptest)
    store.seed(
        paths.claim,
        {"schema": "ptest-claim-v1", "owner": "other",
         "created_at": NOW.isoformat(),
         "lease_expires_at": (NOW + timedelta(minutes=5)).isoformat()},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)

    def publish_on_sleep(_seconds):
        if paths.execution not in store.records:
            store.seed(
                paths.execution,
                {"schema": "ptest-execution-v1", "execution": "fake-job-shared",
                 "submitted_at": NOW.isoformat()},
            )

    monkeypatch.setattr(ptest.time, "sleep", publish_on_sleep)
    monkeypatch.setattr(
        ptest, "describe_execution",
        lambda *args: ptest.ExecutionState("fake-job-shared", False, False, False, ""),
    )
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0
    assert calls["submit"] == calls["budget"] == calls["concurrency"] == 0
    assert calls["wait"] == 1


def test_failed_shared_execution_is_never_cached(ptest, monkeypatch, tmp_path):
    store = MemoryStore(ptest)
    result = ptest.ExecutionResult(
        "fake-job-abc12", "test_failure", "", "FAILED test_x", 1, NOW
    )
    calls = harness(ptest, monkeypatch, tmp_path, store, result)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 1

    assert calls["submit"] == 1
    assert store.read_json(request_paths(ptest).passing) is None


def test_fresh_benchmark_bypasses_passing_lookup_and_does_not_write_a_pass_record(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    normal_paths = request_paths(ptest)
    store.seed(
        normal_paths.passing,
        {"schema": "ptest-pass-v1", "status": "passed",
         "completed_at": NOW.isoformat(), "summary": "old"},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(
        pcfg, cfg, "front", tmp_path, "run tests", fresh=True
    ) == 0

    assert calls["submit"] == calls["budget"] == calls["concurrency"] == 1
    pass_records = [path for path in store.records if path.endswith("/pass.json")]
    assert pass_records == [normal_paths.passing]


def test_coordination_outage_uses_uncached_remote_path_with_safety_checks(
    ptest, monkeypatch, tmp_path
):
    class BrokenStore(MemoryStore):
        def read_json(self, path):
            raise ptest.CoordinationUnavailable("offline")

    store = BrokenStore(ptest)
    calls = harness(ptest, monkeypatch, tmp_path, store)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls == {"concurrency": 1, "budget": 1, "upload": 1, "submit": 1, "wait": 1}


def test_stale_terminal_execution_manifest_is_removed_before_a_new_submission(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    paths = request_paths(ptest)
    store.seed(
        paths.execution,
        {"schema": "ptest-execution-v1", "execution": "fake-job-stale",
         "submitted_at": (NOW - timedelta(hours=3)).isoformat()},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        ptest, "describe_execution",
        lambda *args: ptest.ExecutionState(
            "fake-job-stale", True, True, False, "", NOW - timedelta(hours=2)
        ),
    )
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls["submit"] == 1
    assert calls["wait"] == 1


def test_execution_published_while_stealing_expired_claim_is_joined_not_duplicated(
    ptest, monkeypatch, tmp_path
):
    paths = request_paths(ptest)

    class PublishAfterSteal(MemoryStore):
        def replace_json(self, path, value, generation):
            replaced = super().replace_json(path, value, generation)
            if replaced and path.endswith("/claim.json"):
                self.seed(
                    paths.execution,
                    {"schema": "ptest-execution-v1", "execution": "fake-job-late",
                     "submitted_at": NOW.isoformat()},
                )
            return replaced

    store = PublishAfterSteal(ptest)
    store.seed(
        paths.claim,
        {"schema": "ptest-claim-v1", "owner": "old",
         "created_at": (NOW - timedelta(hours=1)).isoformat(),
         "lease_expires_at": (NOW - timedelta(minutes=1)).isoformat()},
    )
    calls = harness(ptest, monkeypatch, tmp_path, store)
    monkeypatch.setattr(
        ptest, "describe_execution",
        lambda *args: ptest.ExecutionState("fake-job-late", False, False, False, ""),
    )
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0
    assert calls["submit"] == calls["budget"] == calls["concurrency"] == 0
    assert calls["wait"] == 1


def test_coordination_failure_after_submission_waits_for_that_execution_without_resubmit(
    ptest, monkeypatch, tmp_path
):
    class FailExecutionPublish(MemoryStore):
        def create_json(self, path, value):
            if path.endswith("/execution.json"):
                raise ptest.CoordinationUnavailable("write failed")
            return super().create_json(path, value)

    store = FailExecutionPublish(ptest)
    calls = harness(ptest, monkeypatch, tmp_path, store)
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 0

    assert calls["submit"] == 1
    assert calls["wait"] == 1


def test_source_change_during_packaging_refuses_to_cache_or_submit(
    ptest, monkeypatch, tmp_path
):
    store = MemoryStore(ptest)
    calls = harness(ptest, monkeypatch, tmp_path, store)
    digests = iter([DIGEST, "b" * 64])
    monkeypatch.setattr(ptest, "tree_digest", lambda root, entries=None: next(digests))
    pcfg, cfg = configured()

    assert ptest.run_cloudrun(pcfg, cfg, "front", tmp_path, "run tests") == 75

    assert calls["upload"] == calls["submit"] == calls["wait"] == 0
