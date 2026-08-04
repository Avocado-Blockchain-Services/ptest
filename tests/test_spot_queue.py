from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import (
    InMemorySpotQueueStore,
    SpotRecordError,
    SpotRequest,
    SpotResult,
)


NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
KEY = "a" * 64


def request():
    return SpotRequest(
        request_key=KEY,
        command="uv run pytest tests",
        source_uri="gs://private-bucket/sources/" + "b" * 64 + ".tar.gz",
        created_at=NOW,
    )


def test_claim_is_exclusive_and_generation_safe():
    store = InMemorySpotQueueStore()
    assert store.create_request(request()) is True

    first = store.claim(KEY, "worker-a", NOW, 60)
    assert first is not None
    assert first.worker_id == "worker-a"
    assert first.generation == 1
    assert store.claim(KEY, "worker-b", NOW + timedelta(seconds=1), 60) is None


def test_request_creation_is_idempotent_for_at_least_once_delivery():
    store = InMemorySpotQueueStore()

    assert store.create_request(request()) is True
    assert store.create_request(request()) is True


def test_request_retries_keep_the_winning_timestamp_when_identity_is_stable():
    store = InMemorySpotQueueStore()
    original = request()
    retry = SpotRequest(
        original.request_key, original.command, original.source_uri,
        NOW + timedelta(seconds=30),
    )

    assert store.create_request(original) is True
    assert store.create_request(retry) is True
    assert store.read_request(KEY).created_at == NOW


def test_gcloud_request_create_race_reuses_an_identical_winner(monkeypatch):
    from spot_queue import GcloudSpotQueueStore

    store = GcloudSpotQueueStore(["gcloud"], "private-bucket", "ptest-spot")
    calls = []
    record = request().to_record()

    def run(args):
        calls.append(args)
        if args[:3] == ["storage", "objects", "describe"]:
            describes = sum(call[:3] == args[:3] for call in calls)
            if describes == 1:
                return subprocess.CompletedProcess(args, 1, "", "404 Not Found")
            return subprocess.CompletedProcess(args, 0, '{"generation":"2"}', "")
        if args[:2] == ["storage", "cat"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(record), "")
        if args[:3] == ["storage", "cp", args[2]]:
            return subprocess.CompletedProcess(args, 1, "", "412 Precondition Failed")
        raise AssertionError(args)

    monkeypatch.setattr(store, "_run", run)

    assert store.create_request(request()) is True
    assert any("--if-generation-match=0" in call for call in calls)


def test_gcloud_store_publishes_request_keys_to_pubsub(monkeypatch):
    from spot_queue import GcloudSpotQueueStore

    store = GcloudSpotQueueStore(["gcloud", "--project=test"], "private-bucket", "ptest-spot")
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "message-id", "")

    monkeypatch.setattr(store, "_run", run)

    assert store.publish_message(KEY) is True
    assert calls == [["pubsub", "topics", "publish", "ptest-spot", "--message", KEY]]


def test_expired_lease_can_be_reacquired_by_another_worker():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    first = store.claim(KEY, "worker-a", NOW, 60)

    replacement = store.claim(KEY, "worker-b", NOW + timedelta(seconds=60), 60)

    assert replacement is not None
    assert replacement.worker_id == "worker-b"
    assert replacement.generation == first.generation + 1


def test_terminal_result_publication_is_idempotent_but_rejects_conflicts():
    store = InMemorySpotQueueStore()
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW)

    assert store.publish_result(result) is True
    assert store.publish_result(result) is True
    assert store.publish_result(SpotResult(KEY, "failed", 1, "FAILED", NOW)) is False
    assert store.read_result(KEY) == result


def test_malformed_records_are_rejected_not_claimed_or_reused():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    store.seed("leases", KEY, {"schema": "ptest-spot-lease-v1", "worker_id": 7})

    with pytest.raises(SpotRecordError):
        store.claim(KEY, "worker-a", NOW, 60)

    with pytest.raises(SpotRecordError):
        SpotRequest.from_record({"schema": "ptest-spot-request-v1"})


def test_request_lease_and_result_records_must_match_their_lookup_key():
    store = InMemorySpotQueueStore()
    other_key = "c" * 64
    other_request = SpotRequest(other_key, request().command, request().source_uri, NOW)
    store.seed("requests", KEY, other_request.to_record())

    with pytest.raises(SpotRecordError):
        store.read_request(KEY)

    store = InMemorySpotQueueStore()
    store.create_request(request())
    other_lease = store.claim(KEY, "worker", NOW, 60)
    store.seed("leases", KEY, {**other_lease.to_record(), "request_key": other_key})
    with pytest.raises(SpotRecordError):
        store.claim(KEY, "worker-two", NOW + timedelta(seconds=61), 60)

    store.seed("results", KEY, SpotResult(other_key, "passed", 0, "", NOW).to_record())
    with pytest.raises(SpotRecordError):
        store.read_result(KEY)


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("passed", 1),
        ("passed", None),
        ("failed", 0),
        ("failed", None),
        ("infrastructure", 0),
    ],
)
def test_terminal_results_reject_status_exit_code_combinations(status, exit_code):
    with pytest.raises(SpotRecordError):
        SpotResult(KEY, status, exit_code, "", NOW)


def test_requests_keep_the_exact_command_source_key_schema_and_timestamp():
    stored = request().to_record()

    assert stored == {
        "schema": "ptest-spot-request-v1",
        "request_key": KEY,
        "command": "uv run pytest tests",
        "source_uri": "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz",
        "created_at": NOW.isoformat(),
    }
    assert SpotRequest.from_record(stored) == request()


def test_ptest_publishes_a_durable_spot_request_and_waits_for_its_result(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def __init__(self):
            self.requests = []
            self.messages = []

        def read_result(self, request_key):
            return None

        def create_request(self, value):
            self.requests.append(value)
            return True

        def publish_message(self, request_key):
            self.messages.append(request_key)
            return True

        def wait_result(self, request_key, timeout_seconds):
            return SpotResult(request_key, "passed", 0, "12 passed", NOW)

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: queue)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *args: True)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 0

    assert queue.requests[0].command == "uv run pytest tests"
    assert queue.requests[0].source_uri == "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz"
    assert queue.messages == [queue.requests[0].request_key]


def test_ptest_fresh_ignores_a_passing_spot_result(ptest, monkeypatch, tmp_path):
    class Queue:
        def __init__(self):
            self.reads = 0
            self.created = 0

        def read_result(self, request_key):
            self.reads += 1
            return SpotResult(KEY, "passed", 0, "cached", NOW)

        def create_request(self, value):
            self.created += 1
            return True

        def publish_message(self, request_key):
            return True

        def wait_result(self, request_key, timeout_seconds):
            return SpotResult(request_key, "passed", 0, "fresh", NOW)

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: queue)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *args: True)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests", fresh=True) == 0

    assert queue.reads == 0
    assert queue.created == 1


def test_ptest_reuses_a_fresh_passing_spot_result(ptest, monkeypatch, tmp_path, capsys):
    class Queue:
        def __init__(self):
            self.created = 0
            self.messages = 0

        def read_result(self, request_key):
            return SpotResult(KEY, "passed", 0, "cached result", NOW)

        def create_request(self, value):
            self.created += 1
            return True

        def publish_message(self, request_key):
            self.messages += 1
            return True

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: queue)
    monkeypatch.setattr(ptest, "remote_request_key", lambda fields: KEY)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *args: (_ for _ in ()).throw(
        AssertionError("cached result must not package source")
    ))
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}
    monkeypatch.setattr(ptest, "utc_now", lambda: NOW)

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 0

    assert queue.created == queue.messages == 0
    assert "cached result" in capsys.readouterr().out


def test_ptest_retry_after_message_failure_reuses_the_durable_request(
    ptest, monkeypatch, tmp_path
):
    from spot_queue import SpotQueueUnavailable

    class Queue:
        def __init__(self):
            self.request = None
            self.publish_attempts = 0

        def read_result(self, request_key):
            return None

        def create_request(self, value):
            if self.request is None:
                self.request = value
                return True
            return self.request.stable_identity == value.stable_identity

        def publish_message(self, request_key):
            self.publish_attempts += 1
            if self.publish_attempts == 1:
                raise SpotQueueUnavailable("publish failed")
            return True

        def wait_result(self, request_key, timeout_seconds):
            return SpotResult(request_key, "passed", 0, "recovered", NOW)

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: queue)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda fields: KEY)
    now = iter([NOW, NOW + timedelta(seconds=30)])
    monkeypatch.setattr(ptest, "utc_now", lambda: next(now))
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") is None
    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 0
    assert queue.request.created_at == NOW
    assert queue.publish_attempts == 2


def test_malformed_spot_result_falls_back_to_local_infrastructure_path(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def read_result(self, request_key):
            raise SpotRecordError("malformed terminal result")

    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: Queue())
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") is None


def test_ptest_starts_without_spot_queue_when_installed_as_a_single_file(tmp_path):
    runner = tmp_path / "ptest"
    runner.write_text(Path(__file__).resolve().parents[1].joinpath("ptest").read_text())

    completed = subprocess.run(
        [sys.executable, str(runner), "where"], capture_output=True, text=True
    )

    assert completed.returncode == 0
    assert "backend" in completed.stdout
