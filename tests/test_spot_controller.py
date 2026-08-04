import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_controller import GcloudControllerAdapter, reconcile, scale_target
from spot_queue import WorkerState


def test_idle_worker_is_kept_until_the_exact_idle_timeout():
    assert scale_target(0, 1, 3599, 5) == 1
    assert scale_target(0, 1, 3600, 5) == 0


def test_backlog_is_capped_at_max_workers():
    assert scale_target(99, 0, 0, 5) == 5


def test_active_lease_never_scales_in():
    assert scale_target(0, 3, 3600, 5, active_leases=1) == 3


def test_active_leases_are_a_floor_even_when_worker_observation_lags():
    assert scale_target(0, 1, 9999, 5, active_leases=3) == 3


def test_target_never_exceeds_cap_when_lease_count_is_inconsistent():
    assert scale_target(0, 7, 9999, 5, active_leases=7) == 5


def test_lease_count_over_cap_does_not_issue_a_downsize():
    class Adapter:
        overflow_enabled = False
        def authenticated_metrics(self): return 0, 7, 7, 9999
        def set_target(self, _target): raise AssertionError("must retain live lease capacity")
        def record_capacity_fault(self, leases, maximum): self.fault = (leases, maximum)

    adapter = Adapter()
    assert reconcile(adapter, 5) == 5
    assert adapter.fault == (7, 5)


def test_idle_age_and_configured_timeout_are_distinct_inputs():
    assert scale_target(0, 1, 60, 5, idle_timeout_seconds=60) == 0


def test_controller_queries_monitoring_rest_api_with_supported_token_command(monkeypatch):
    adapter = GcloudControllerAdapter("project-a", "us-central1", "mig", "spot-sub", "bucket")
    commands, requests = [], []

    monkeypatch.setattr(adapter, "_run", lambda *args: commands.append(args) or "token")
    monkeypatch.setattr("spot_controller.urlopen", lambda request, timeout: requests.append(request) or type(
        "Response", (), {"read": lambda self: b'{"timeSeries": []}', "__enter__": lambda self: self,
                           "__exit__": lambda self, *_args: None})())

    assert adapter._monitoring_backlog() == 0
    assert commands == [("auth", "print-access-token")]
    assert requests[0].full_url.startswith("https://monitoring.googleapis.com/v3/projects/project-a/timeSeries?")
    assert requests[0].get_header("Authorization") == "Bearer token"


def test_monitoring_query_uses_a_full_five_minute_window_at_hour_boundary(monkeypatch):
    adapter = GcloudControllerAdapter("project-a", "us-central1", "mig", "spot-sub", "bucket")
    requests = []

    class Clock:
        @staticmethod
        def now(_tz):
            return datetime(2026, 8, 4, 12, 3, tzinfo=timezone.utc)

    monkeypatch.setattr("spot_controller.datetime", Clock)
    monkeypatch.setattr(adapter, "_run", lambda *_args: "token")
    monkeypatch.setattr("spot_controller.urlopen", lambda request, timeout: requests.append(request) or type(
        "Response", (), {"read": lambda self: b'{"timeSeries": []}', "__enter__": lambda self: self,
                           "__exit__": lambda self, *_args: None})())

    adapter._monitoring_backlog()

    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query["interval.startTime"] == ["2026-08-04T11:58:00+00:00"]


def test_over_cap_backlog_stays_in_pubsub_without_writing_an_overflow_marker(monkeypatch):
    adapter = GcloudControllerAdapter("project-a", "us-central1", "mig", "spot-sub", "bucket")
    commands = []
    adapter.overflow_enabled = True
    monkeypatch.setattr(adapter, "authenticated_metrics", lambda: (8, 0, 0, 0))
    monkeypatch.setattr(adapter, "set_target", lambda target: commands.append(("resize", target)))
    monkeypatch.setattr(adapter, "_run", lambda *args: commands.append(args) or "")

    assert reconcile(adapter, 5) == 5

    assert commands == [("resize", 5)]
    assert not hasattr(adapter, "overflow_state")


def test_stale_worker_heartbeats_expire_and_cannot_mask_fresh_idle_age(monkeypatch):
    class Clock:
        @staticmethod
        def now(_tz): return datetime(2026, 8, 4, 12, 10, tzinfo=timezone.utc)

    now = Clock.now(timezone.utc)
    stale = WorkerState("gone-worker", None, now.replace(minute=0), "a" * 64)
    fresh = WorkerState(
        "idle-worker", now.replace(hour=11, minute=0), now.replace(minute=9), None
    )
    records = {
        "gs://bucket/spot/v1/workers/gone-worker.json": stale.to_record(),
        "gs://bucket/spot/v1/workers/idle-worker.json": fresh.to_record(),
    }
    adapter = GcloudControllerAdapter(
        "project-a", "us-central1", "mig", "spot-sub", "bucket",
        worker_heartbeat_ttl_seconds=120,
    )
    monkeypatch.setattr("spot_controller.datetime", Clock)

    def run(*args):
        if args[:2] == ("storage", "ls"):
            return "\n".join(records)
        return __import__("json").dumps(records[args[-1]])

    monkeypatch.setattr(adapter, "_run", run)

    assert adapter._worker_idle_age() == 4200


def test_only_stale_worker_heartbeats_allow_scale_down(monkeypatch):
    class Clock:
        @staticmethod
        def now(_tz): return datetime(2026, 8, 4, 12, 10, tzinfo=timezone.utc)

    stale = WorkerState("gone-worker", None, Clock.now(timezone.utc).replace(minute=0), "a" * 64)
    adapter = GcloudControllerAdapter(
        "project-a", "us-central1", "mig", "spot-sub", "bucket",
        worker_heartbeat_ttl_seconds=120,
    )
    monkeypatch.setattr("spot_controller.datetime", Clock)
    monkeypatch.setattr(
        adapter, "_run",
        lambda *args: "gs://bucket/spot/v1/workers/gone-worker.json"
        if args[:2] == ("storage", "ls") else __import__("json").dumps(stale.to_record()),
    )

    assert scale_target(0, 1, adapter._worker_idle_age(), 5) == 0
