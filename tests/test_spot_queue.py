from datetime import datetime, timedelta, timezone
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
        source_uri="gs://private-bucket/sources/source.tar.gz",
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


def test_requests_keep_the_exact_command_source_key_schema_and_timestamp():
    stored = request().to_record()

    assert stored == {
        "schema": "ptest-spot-request-v1",
        "request_key": KEY,
        "command": "uv run pytest tests",
        "source_uri": "gs://private-bucket/sources/source.tar.gz",
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
