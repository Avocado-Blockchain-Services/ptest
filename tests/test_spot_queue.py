from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import (
    InMemorySpotQueueStore,
    Lease,
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


def test_gcloud_result_transition_uses_the_exact_lease_generation(monkeypatch):
    from types import SimpleNamespace
    from spot_queue import GcloudSpotQueueStore

    store = GcloudSpotQueueStore(["gcloud"], "private-bucket", "ptest-spot")
    lease = Lease(KEY, "worker-a", NOW, NOW + timedelta(seconds=60), 7)
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW + timedelta(seconds=1))
    writes = []
    monkeypatch.setattr(
        store, "_read", lambda namespace, key: SimpleNamespace(
            value=lease.to_record(), generation=7
        )
    )
    monkeypatch.setattr(
        store, "_write",
        lambda namespace, key, value, generation: writes.append(
            (namespace, key, value, generation)
        ) or True,
    )

    assert store.publish_result(result, lease) is True
    assert writes == [("states", KEY, result.to_record(), 7)]


def test_gcloud_claim_rejects_a_replacement_lease_seen_after_its_cas(monkeypatch):
    from types import SimpleNamespace
    from spot_queue import GcloudSpotQueueStore

    store = GcloudSpotQueueStore(["gcloud"], "private-bucket", "ptest-spot")
    replacement = Lease(
        KEY, "worker-b", NOW + timedelta(seconds=61),
        NOW + timedelta(seconds=121), 99,
    )
    states = iter([
        None,
        SimpleNamespace(value=replacement.to_record(), generation=99),
    ])
    monkeypatch.setattr(store, "read_request", lambda _key: request())
    monkeypatch.setattr(store, "_read", lambda namespace, _key: next(states))
    monkeypatch.setattr(store, "_write", lambda *_args: True)

    assert store.claim(KEY, "worker-a", NOW, 60) is None


def test_expired_lease_can_be_reacquired_by_another_worker():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    first = store.claim(KEY, "worker-a", NOW, 60)

    replacement = store.claim(KEY, "worker-b", NOW + timedelta(seconds=60), 60)

    assert replacement is not None
    assert replacement.worker_id == "worker-b"
    assert replacement.generation == first.generation + 1


def test_renew_is_generation_safe_and_extends_the_lease():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)

    renewed = store.renew(lease, NOW + timedelta(seconds=30), 60)

    assert renewed.generation == 2
    assert renewed.expires_at == NOW + timedelta(seconds=90)
    assert store.renew(lease, NOW + timedelta(seconds=31), 60) is None


def test_cancellation_prevents_a_later_claim_after_local_timeout_fallback():
    store = InMemorySpotQueueStore()
    store.create_request(request())

    assert store.cancel_request(KEY, "timeout-local-fallback", NOW) is True
    assert store.claim(KEY, "worker-a", NOW, 60) is None
    assert store.is_cancelled(KEY) is True


def test_result_and_cancellation_race_has_exactly_one_generation_checked_winner():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW + timedelta(seconds=1))
    start = threading.Barrier(3)

    def publish():
        start.wait()
        return store.publish_result(result, lease)

    def cancel():
        start.wait()
        return store.cancel_request(
            KEY, "timeout-local-fallback", NOW + timedelta(seconds=1)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publish)
        cancelling = pool.submit(cancel)
        start.wait()
        outcomes = publishing.result(), cancelling.result()

    assert outcomes.count(True) == 1
    assert (store.read_result(KEY) is not None) != store.is_cancelled(KEY)


def test_cancellation_blocks_a_stale_lease_result_and_is_timestamp_idempotent():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)

    assert store.cancel_request(KEY, "timeout-local-fallback", NOW + timedelta(seconds=1))
    assert store.publish_result(
        SpotResult(KEY, "passed", 0, "late result", NOW + timedelta(seconds=2)), lease
    ) is False
    assert store.cancel_request(
        KEY, "timeout-local-fallback", NOW + timedelta(seconds=30)
    ) is True
    assert store.read_result(KEY) is None


def test_result_terminal_transition_blocks_a_later_cancellation():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW + timedelta(seconds=1))

    assert store.publish_result(result, lease) is True
    assert store.cancel_request(
        KEY, "timeout-local-fallback", NOW + timedelta(seconds=2)
    ) is False
    assert store.read_result(KEY) == result


def test_cancelled_lease_requires_worker_quiescence_or_captured_expiry():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    store.claim(KEY, "worker-a", NOW, 60)
    store.heartbeat_worker("worker-a", KEY, NOW)
    store.cancel_request(KEY, "timeout-local-fallback", NOW + timedelta(seconds=1))

    assert store.cancellation_quiesced(KEY, NOW + timedelta(seconds=2)) is False

    store.heartbeat_worker("worker-a", None, NOW + timedelta(seconds=3))
    assert store.cancellation_quiesced(KEY, NOW + timedelta(seconds=3)) is True

    other = InMemorySpotQueueStore()
    other.create_request(request())
    other.claim(KEY, "preempted-worker", NOW, 60)
    other.cancel_request(KEY, "timeout-local-fallback", NOW + timedelta(seconds=1))
    assert other.cancellation_quiesced(KEY, NOW + timedelta(seconds=59)) is False
    assert other.cancellation_quiesced(KEY, NOW + timedelta(seconds=60)) is True


def test_worker_heartbeat_tracks_actual_idle_since_and_completed_lease_releases():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)

    active = store.heartbeat_worker("worker-a", KEY, NOW)
    idle = store.heartbeat_worker("worker-a", None, NOW + timedelta(seconds=5))

    assert active.idle_since is None
    assert idle.idle_since == NOW + timedelta(seconds=5)
    assert store.release(lease, NOW + timedelta(seconds=6)) is True
    assert store.claim(KEY, "worker-b", NOW + timedelta(seconds=6), 60).worker_id == "worker-b"


def test_terminal_result_publication_is_idempotent_but_rejects_conflicts():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    lease = store.claim(KEY, "worker-a", NOW, 60)
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW)

    assert store.publish_result(result, lease) is True
    assert store.publish_result(result, lease) is True
    assert store.publish_result(
        SpotResult(KEY, "failed", 1, "FAILED", NOW), lease
    ) is False
    assert store.read_result(KEY) == result


def test_malformed_records_are_rejected_not_claimed_or_reused():
    store = InMemorySpotQueueStore()
    store.create_request(request())
    store.seed("states", KEY, {"schema": "ptest-spot-lease-v1", "worker_id": 7})

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
    store.seed("states", KEY, {**other_lease.to_record(), "request_key": other_key})
    with pytest.raises(SpotRecordError):
        store.claim(KEY, "worker-two", NOW + timedelta(seconds=61), 60)

    store.seed("states", KEY, SpotResult(other_key, "passed", 0, "", NOW).to_record())
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
        ("infrastructure", 1),
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


def test_ptest_cancels_durable_request_before_timeout_fallback(ptest, monkeypatch, tmp_path):
    class Queue:
        def __init__(self): self.events = []
        def read_result(self, _key): return None
        def create_request(self, _value): return True
        def publish_message(self, _key): return True
        def wait_result(self, _key, _timeout): return None
        def cancel_request(self, key, reason, now):
            self.events.append("cancel")
            self.cancelled = (key, reason, now)
            return True
        def wait_cancellation_quiesced(self, key, timeout_seconds):
            assert key == KEY and timeout_seconds > 0
            self.events.append("quiesced")
            return True

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: queue)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *_args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *_args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *_args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue({"kind": "pytest", "spot_topic": "ptest-spot"}, config,
                                "fake", tmp_path, "uv run pytest tests") is None
    assert queue.cancelled[0:2] == (KEY, "timeout-local-fallback")
    assert queue.events == ["cancel", "quiesced"]


def test_ptest_refuses_local_fallback_until_cancelled_worker_is_quiesced(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def read_result(self, _key): return None
        def create_request(self, _value): return True
        def publish_message(self, _key): return True
        def wait_result(self, _key, _timeout): return None
        def cancel_request(self, _key, _reason, _now): return True
        def wait_cancellation_quiesced(self, _key, _timeout): return False

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *_args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *_args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *_args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 75


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


@pytest.mark.parametrize(
    "terminal",
    [
        SpotResult(KEY, "passed", 0, "stale pass", NOW - timedelta(hours=2)),
        SpotResult(KEY, "failed", 1, "old failure", NOW),
    ],
    ids=["stale-pass", "failed-result"],
)
def test_ptest_requires_fresh_for_a_non_reusable_terminal_key(
    ptest, monkeypatch, tmp_path, terminal
):
    class Queue:
        def read_result(self, _key): return terminal
        def create_request(self, _value):
            raise AssertionError("immutable terminal keys cannot be republished")

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    monkeypatch.setattr(ptest, "utc_now", lambda: NOW)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 75


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

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 75
    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 0
    assert queue.request.created_at == NOW
    assert queue.publish_attempts == 2


def test_ptest_rejoins_an_existing_request_before_considering_local_fallback(
    ptest, monkeypatch, tmp_path
):
    existing = request()

    class Queue:
        def read_result(self, _key): return None
        def is_cancelled(self, _key): return False
        def read_request(self, _key): return existing
        def create_request(self, value): return value.stable_identity == existing.stable_identity
        def publish_message(self, _key): return True
        def wait_result(self, key, _timeout): return SpotResult(key, "passed", 0, "joined", NOW)

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    monkeypatch.setattr(
        ptest, "budget_check",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("an already durable request must be rejoined")
        ),
    )
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 0


def test_ptest_returns_the_result_that_wins_the_timeout_cancellation_race(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def __init__(self): self.reads = 0
        def read_result(self, key):
            self.reads += 1
            return None if self.reads == 1 else SpotResult(key, "passed", 0, "winner", NOW)
        def create_request(self, _value): return True
        def publish_message(self, _key): return True
        def wait_result(self, _key, _timeout): return None
        def cancel_request(self, _key, _reason, _now): return False

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *_args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *_args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *_args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 0


def test_malformed_spot_result_refuses_a_possible_duplicate_local_run(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def read_result(self, request_key):
            raise SpotRecordError("malformed terminal result")

    monkeypatch.setattr(ptest.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *args: Queue())
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}
    project = {"kind": "pytest", "spot_topic": "ptest-spot"}

    assert ptest.run_spot_queue(project, config, "fake", tmp_path, "uv run pytest tests") == 75


def test_ambiguous_wait_failure_after_publish_refuses_local_fallback(
    ptest, monkeypatch, tmp_path
):
    from spot_queue import SpotQueueUnavailable

    class Queue:
        def read_result(self, _key): return None
        def create_request(self, _value): return True
        def publish_message(self, _key): return True
        def wait_result(self, _key, _timeout):
            raise SpotQueueUnavailable("result lookup failed after publish")

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *_args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *_args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *_args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 75


def test_ambiguous_create_failure_after_persistence_refuses_local_fallback(
    ptest, monkeypatch, tmp_path
):
    from spot_queue import SpotQueueUnavailable

    class Queue:
        def __init__(self):
            self.persisted = False

        def read_result(self, _key): return None
        def is_cancelled(self, _key): return False
        def read_request(self, _key): return None
        def create_request(self, _value):
            self.persisted = True
            raise SpotQueueUnavailable("response lost after create-only CAS")
        def publish_message(self, _key):
            raise AssertionError("ambiguous creation must be retried, not published")

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: queue)
    monkeypatch.setattr(ptest, "GcsCoordination", lambda *_args: object())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "prepare_source_archive", lambda *_args: Path("sources/archive"))
    monkeypatch.setattr(ptest, "budget_check", lambda *_args: True)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 75
    assert queue.persisted is True


def test_cancelled_deterministic_key_is_an_explicit_safe_local_retry(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def __init__(self): self.events = []
        def read_result(self, _key): return None
        def is_cancelled(self, _key): return True
        def wait_cancellation_quiesced(self, key, timeout_seconds):
            assert key == KEY and timeout_seconds > 0
            self.events.append("quiesced")
            return True
        def create_request(self, _value):
            raise AssertionError("a cancelled deterministic key must not be republished")

    queue = Queue()
    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: queue)
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) is None
    assert queue.events == ["quiesced"]


def test_cancelled_deterministic_retry_returns_75_while_prior_worker_may_run(
    ptest, monkeypatch, tmp_path
):
    class Queue:
        def read_result(self, _key): return None
        def is_cancelled(self, _key): return True
        def wait_cancellation_quiesced(self, _key, _timeout): return False

    monkeypatch.setattr(ptest.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(ptest, "GcloudSpotQueueStore", lambda *_args: Queue())
    monkeypatch.setattr(ptest, "source_manifest", lambda _root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda _root, entries=None: "b" * 64)
    monkeypatch.setattr(ptest, "remote_request_key", lambda _fields: KEY)
    config = {"defaults": {"bucket": "private-bucket", "gcp_project": "test-project"}}

    assert ptest.run_spot_queue(
        {"kind": "pytest", "spot_topic": "ptest-spot"}, config,
        "fake", tmp_path, "uv run pytest tests"
    ) == 75


def test_ptest_starts_without_spot_queue_when_installed_as_a_single_file(tmp_path):
    runner = tmp_path / "ptest"
    runner.write_text(Path(__file__).resolve().parents[1].joinpath("ptest").read_text())

    completed = subprocess.run(
        [sys.executable, str(runner), "where"], capture_output=True, text=True
    )

    assert completed.returncode == 0
    assert "backend" in completed.stdout
