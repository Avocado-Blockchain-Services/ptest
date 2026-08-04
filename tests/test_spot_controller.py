import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spot_controller import GcloudControllerAdapter, reconcile, scale_target


def test_idle_worker_is_kept_until_the_exact_idle_timeout():
    assert scale_target(0, 1, 3599, 5) == 1
    assert scale_target(0, 1, 3600, 5) == 0


def test_backlog_is_capped_at_max_workers():
    assert scale_target(99, 0, 0, 5) == 5


def test_active_lease_never_scales_in():
    assert scale_target(0, 3, 3600, 5, active_leases=1) == 3


def test_active_leases_are_a_floor_even_when_worker_observation_lags():
    assert scale_target(0, 1, 9999, 5, active_leases=3) == 3


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


def test_overflow_explicitly_retains_messages_when_no_compatible_cloudrun_job_exists():
    class Adapter:
        overflow_enabled = True

        def authenticated_metrics(self):
            return 8, 0, 0, 0

        def set_target(self, target):
            self.target = target

        def retain_overflow(self, excess):
            self.overflow_state = {"mode": "retain-queue", "excess": excess}
            return self.overflow_state

    adapter = Adapter()

    assert reconcile(adapter, 5) == 5
    assert adapter.overflow_state == {"mode": "retain-queue", "excess": 3}
