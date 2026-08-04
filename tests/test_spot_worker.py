from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_queue import SpotRequest
from spot_worker import run_claimed_request


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
