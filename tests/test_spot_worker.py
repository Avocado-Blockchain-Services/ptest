from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import Lease, SpotRequest
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


def test_worker_uses_supported_pubsub_ack_and_deadline_commands(monkeypatch):
    adapter = GcloudWorkerAdapter("project-a", "private-bucket", "spot-topic", "spot-sub")
    commands = []
    monkeypatch.setattr(adapter, "_run", lambda *args: commands.append(args) or "")

    adapter.acknowledge({"ack_id": "ack-1"})
    lease = Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 1)
    adapter.store.renew = lambda *_args: Lease(KEY, "worker-a", NOW, NOW.replace(minute=2), 2)
    _MessageAdapter(adapter, {"ack_id": "ack-1"}, type("WorkerState", (), {"stopping": False})()).heartbeat(lease)

    assert commands == [
        ("pubsub", "subscriptions", "ack", "spot-sub", "--ack-ids", "ack-1"),
        ("pubsub", "subscriptions", "modify-message-ack-deadline", "spot-sub",
         "--ack-ids", "ack-1", "--ack-deadline", "120"),
    ]


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
