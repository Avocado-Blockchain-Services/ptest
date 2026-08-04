from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import Lease, SpotRequest, SpotResult
from spot_worker import GcloudWorkerAdapter, Worker, _MessageAdapter, run_claimed_request, unpack_archive


KEY = "a" * 64
NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


def test_claimed_request_downloads_unpacks_runs_publishes_then_acknowledges(tmp_path):
    events = []

    class Adapter:
        def download(self, source_uri, destination):
            events.append("download")
            Path(destination).write_bytes(b"archive")

        def unpack(self, archive, destination):
            events.append("unpack")

        def run(self, command, cwd):
            events.append("run")
            return 0, "12 passed"

        def publish_result(self, result):
            events.append("publish")
            return True

        def acknowledge(self):
            events.append("acknowledge")

    request = SpotRequest(
        KEY, "uv run pytest tests",
        "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW,
    )

    result = run_claimed_request(request, tmp_path, Adapter())

    assert result.status == "passed"
    assert events == ["download", "unpack", "run", "publish", "acknowledge"]


def test_preempted_worker_leaves_no_terminal_result_or_acknowledgement(tmp_path):
    events = []
    preempted = False

    class Adapter:
        def download(self, source_uri, destination):
            events.append("download")

        def unpack(self, archive, destination):
            events.append("unpack")

        def run(self, command, cwd):
            nonlocal preempted
            events.append("run")
            preempted = True
            return 0, "12 passed"

        def publish_result(self, result):
            events.append("publish")
            return True

        def preempted(self):
            return preempted

        def acknowledge(self):
            events.append("acknowledge")

    request = SpotRequest(
        KEY, "uv run pytest tests",
        "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW,
    )

    assert run_claimed_request(request, tmp_path, Adapter()) is None

    assert events == ["download", "unpack", "run"]


def test_cancelled_request_does_not_start_a_child_or_publish_result(tmp_path):
    events = []

    class Adapter:
        def download(self, *_args): events.append("download")
        def unpack(self, *_args): events.append("unpack")
        def prepare(self, *_args): events.append("prepare")
        def cancelled(self): return True
        def run(self, *_args): events.append("run")
        def publish_result(self, *_args): events.append("publish")
        def acknowledge(self): events.append("ack")

    request = SpotRequest(KEY, "test", "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW)
    assert run_claimed_request(request, tmp_path, Adapter()) is None
    assert events == ["download", "unpack", "prepare"]


def test_worker_uses_supported_pubsub_ack_and_deadline_commands(monkeypatch):
    adapter = GcloudWorkerAdapter("project-a", "private-bucket", "spot-topic", "spot-sub")
    commands = []
    monkeypatch.setattr(adapter, "_run", lambda *args: commands.append(args) or "")

    adapter.acknowledge({"ack_id": "ack-1"})
    lease = Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 1)
    adapter.store.renew = lambda *_args: Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 2)
    adapter.store.is_cancelled = lambda _key: False
    adapter.heartbeat_worker = lambda *_args: None
    _MessageAdapter(adapter, {"ack_id": "ack-1", "request_key": KEY},
                    type("WorkerState", (), {"stopping": False, "worker_id": "worker-a"})()).heartbeat(lease)

    assert commands == [
        ("pubsub", "subscriptions", "ack", "spot-sub", "--ack-ids", "ack-1"),
        ("pubsub", "subscriptions", "modify-message-ack-deadline", "spot-sub",
         "--ack-ids", "ack-1", "--ack-deadline", "120"),
    ]


def test_claim_announces_the_lease_floor_before_creating_the_durable_lease(monkeypatch):
    adapter = GcloudWorkerAdapter("project-a", "private-bucket", "spot-topic", "spot-sub")
    events = []
    lease = Lease(KEY, "worker-a", NOW, NOW + timedelta(seconds=120), 1)
    monkeypatch.setattr("spot_worker.datetime", type("Clock", (), {
        "now": staticmethod(lambda _tz: NOW),
    }))
    adapter.heartbeat_worker = lambda worker, request, expiry=None, now=None: events.append(
        ("index", worker, request, expiry, now)
    )
    adapter.store.claim = lambda *args: events.append(("claim", *args)) or lease

    assert adapter.claim(KEY, "worker-a", 120) == lease
    assert events == [
        ("index", "worker-a", KEY, NOW + timedelta(seconds=120), NOW),
        ("claim", KEY, "worker-a", NOW, 120),
    ]


def test_renew_announces_the_next_lease_floor_before_lease_cas(monkeypatch):
    adapter = GcloudWorkerAdapter("project-a", "private-bucket", "spot-topic", "spot-sub")
    initial = Lease(KEY, "worker-a", NOW, NOW + timedelta(seconds=120), 1)
    renewed_at = NOW + timedelta(seconds=40)
    renewed = Lease(KEY, "worker-a", renewed_at, renewed_at + timedelta(seconds=120), 2)
    events = []
    adapter.store.is_cancelled = lambda _key: False
    adapter.heartbeat_worker = lambda worker, request, expiry=None, now=None: events.append(
        ("index", expiry, now)
    )
    adapter.store.renew = lambda *args: events.append(("renew", *args)) or renewed
    monkeypatch.setattr("spot_worker.datetime", type("Clock", (), {
        "now": staticmethod(lambda _tz: renewed_at),
    }))
    monkeypatch.setattr(adapter, "_run", lambda *_args: "")
    bound = _MessageAdapter(
        adapter, {"ack_id": "ack-1", "request_key": KEY},
        type("WorkerState", (), {"stopping": False, "worker_id": "worker-a"})(),
    )

    assert bound.heartbeat(initial) == renewed
    assert events == [
        ("index", renewed.expires_at, renewed_at),
        ("renew", initial, renewed_at, 120),
    ]


def test_message_adapter_publishes_with_latest_renewed_lease_generation(monkeypatch):
    adapter = GcloudWorkerAdapter("project-a", "private-bucket", "spot-topic", "spot-sub")
    initial = Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 1)
    renewed = Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 2)
    published = []
    adapter.store.is_cancelled = lambda _key: False
    adapter.store.renew = lambda *_args: renewed
    adapter.store.publish_result = lambda result, lease: published.append((result, lease)) or True
    adapter.heartbeat_worker = lambda *_args: None
    monkeypatch.setattr(adapter, "_run", lambda *_args: "")
    bound = _MessageAdapter(
        adapter, {"ack_id": "ack-1", "request_key": KEY},
        type("WorkerState", (), {"stopping": False, "worker_id": "worker-a"})(),
    )
    bound.bind_lease(initial)

    assert bound.heartbeat(initial) == renewed
    result = SpotResult(KEY, "passed", 0, "12 passed", NOW)
    assert bound.publish_result(result) is True
    assert published == [(result, renewed)]


def test_unpack_rejects_path_traversal_and_outside_symlinks(tmp_path):
    import io
    import tarfile

    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as payload:
        bad = tarfile.TarInfo("../outside")
        bad.size = 1
        payload.addfile(bad, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe archive member"):
        unpack_archive(archive, tmp_path / "source")


def test_worker_claims_before_running_and_uses_message_bound_ack(tmp_path, monkeypatch):
    events = []

    class Adapter:
        def pull(self):
            events.append("pull")
            return "message"

        def read_request(self, message):
            assert message == "message"
            return SpotRequest(KEY, "test", "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW)

        def read_result(self, key):
            return None

        def claim(self, key, worker_id, seconds):
            events.append("claim")
            return "lease"

        def heartbeat(self, lease):
            events.append("heartbeat")

        def for_message(self, message, worker):
            events.append("bound")
            return object()

    monkeypatch.setattr("spot_worker.run_claimed_request", lambda request, root, adapter: events.append("run"))

    assert Worker(Adapter(), tmp_path, "worker-a").run_once() is True
    assert events == ["pull", "claim", "heartbeat", "bound", "run"]


def test_heartbeat_failure_stops_child_and_suppresses_result_and_ack(tmp_path):
    events, stopped = [], __import__("threading").Event()
    lease = Lease(KEY, "worker-a", NOW, NOW.replace(minute=1), 1)

    class Bound:
        def download(self, *_args): events.append("download")
        def unpack(self, *_args): events.append("unpack")
        def run(self, *_args):
            events.append("run")
            stopped.wait(2)
            return 143, "terminated"
        def heartbeat(self, _lease): raise RuntimeError("lease renewal lost")
        def preempted(self): return worker.stopping
        def publish_result(self, _result): events.append("publish")
        def acknowledge(self): events.append("ack")

    class Adapter:
        def pull(self): return "message"
        def read_request(self, _message):
            return SpotRequest(KEY, "test", "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW)
        def read_result(self, _key): return None
        def claim(self, *_args): return lease
        def heartbeat(self, _lease): pass
        def for_message(self, *_args): return Bound()
        def stop_active(self): events.append("stop-child"); stopped.set()

    worker = Worker(Adapter(), tmp_path, "worker-a", lease_seconds=1)

    assert worker.run_once() is True
    assert worker.stopping is True
    assert events == ["download", "unpack", "run", "stop-child"]


def test_active_cancellation_records_quiescence_before_acknowledging(tmp_path):
    import threading

    events, child_stopped, cancelled = [], threading.Event(), threading.Event()
    lease = Lease(KEY, "worker-a", NOW, NOW.replace(minute=1), 1)

    class Bound:
        def download(self, *_args): pass
        def unpack(self, *_args): pass
        def run(self, *_args):
            events.append("run")
            child_stopped.wait(2)
            return 143, "terminated"
        def heartbeat(self, _lease):
            cancelled.set()
            return None
        def preempted(self): return worker.stopping
        def cancelled(self): return cancelled.is_set()
        def publish_result(self, _result): events.append("publish")
        def acknowledge(self): events.append("bound-ack")

    class Adapter:
        def pull(self): return "message"
        def read_request(self, _message):
            return SpotRequest(
                KEY, "test", "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW
            )
        def read_result(self, _key): return None
        def is_cancelled(self, _key): return cancelled.is_set()
        def claim(self, *_args): return lease
        def heartbeat(self, _lease): pass
        def for_message(self, *_args): return Bound()
        def heartbeat_worker(self, _worker, active_request):
            events.append("active" if active_request else "idle")
        def acknowledge(self, _message): events.append("ack")
        def stop_active(self):
            events.append("stop-child")
            child_stopped.set()

    worker = Worker(Adapter(), tmp_path, "worker-a", lease_seconds=1)

    assert worker.run_once() is True
    assert events == ["active", "run", "stop-child", "idle", "ack"]
