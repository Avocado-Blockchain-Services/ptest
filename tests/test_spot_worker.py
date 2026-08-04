from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import SpotRequest
from spot_worker import Worker, run_claimed_request


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


def test_preempted_worker_publishes_before_leaving_message_unacknowledged(tmp_path):
    events = []

    class Adapter:
        def download(self, source_uri, destination):
            events.append("download")

        def unpack(self, archive, destination):
            events.append("unpack")

        def run(self, command, cwd):
            events.append("run")
            return 0, "12 passed"

        def publish_result(self, result):
            events.append("publish")
            return True

        def preempted(self):
            return True

        def acknowledge(self):
            events.append("acknowledge")

    request = SpotRequest(
        KEY, "uv run pytest tests",
        "gs://private-bucket/sources/" + "b" * 64 + ".tar.gz", NOW,
    )

    run_claimed_request(request, tmp_path, Adapter())

    assert events == ["download", "unpack", "run", "publish"]


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
