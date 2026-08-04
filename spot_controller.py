"""Authenticated capacity controller for the opt-in Spot worker MIG."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spot_queue import WORKER_INDEX_SCHEMA, WorkerState


def admission_allowed(backlog: int, active_leases: int, max_workers: int,
                      overflow_reject_enabled: bool) -> bool:
    """Apply the opt-in brake to the controller's current capacity observation."""
    if min(backlog, active_leases, max_workers) < 0:
        raise ValueError("admission inputs must be non-negative")
    return not overflow_reject_enabled or max(backlog, active_leases) < max_workers


def scale_target(backlog: int, active_workers: int, idle_age_seconds: int,
                 max_workers: int, active_leases: int = 0,
                 idle_timeout_seconds: int = 3600) -> int:
    """Return a safe target from observations, without executing any tests.

    ``idle_age_seconds`` is an observed metric; ``idle_timeout_seconds`` is a
    policy setting.  A live lease is an unconditional floor so a delayed MIG
    observation can never terminate a worker that still owns a request.
    """
    if min(backlog, active_workers, idle_age_seconds, max_workers,
           active_leases, idle_timeout_seconds) < 0:
        raise ValueError("capacity inputs must be non-negative")
    if active_leases:
        return min(max_workers, max(backlog, active_workers, active_leases))
    if max_workers == 0:
        return 0
    if backlog:
        return min(max_workers, max(backlog, 1))
    return 1 if active_workers and idle_age_seconds < idle_timeout_seconds else 0


def reconcile(adapter, max_workers: int, idle_timeout_seconds: int = 3600) -> int:
    """Read authenticated metrics and set only the Spot MIG target.

    Backlog above the cap remains in Pub/Sub. Admission rejection is a separate
    pre-persistence decision; reconciliation never dispatches malformed work.
    """
    backlog, workers, leases, idle_age = adapter.authenticated_metrics()
    target = scale_target(backlog, workers, idle_age, max_workers, leases,
                          idle_timeout_seconds)
    if leases > max_workers:
        # Do not issue a downsize while the observed lease floor is already
        # inconsistent with policy; retain current capacity and surface state.
        adapter.record_capacity_fault(leases, max_workers)
        return target
    adapter.set_target(target)
    return target


def build_handler(adapter, max_workers: int, idle_timeout_seconds: int = 3600,
                  overflow_reject_enabled: bool = False):
    """Build the IAM-gated controller HTTP contract."""
    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            else:
                self.send_error(404)

        def do_POST(self):
            if not adapter.authorize(self.headers.get("Authorization", "")):
                self.send_error(401)
                return
            try:
                if self.path == "/admit":
                    backlog, _workers, leases, _idle_age = adapter.authenticated_metrics()
                    self._json({
                        "admitted": admission_allowed(
                            backlog, leases, max_workers, overflow_reject_enabled
                        ),
                        "active_leases": leases,
                        "backlog": backlog,
                        "max_workers": max_workers,
                        "overflow_reject_enabled": overflow_reject_enabled,
                    })
                    return
                if self.path != "/reconcile":
                    self.send_error(404)
                    return
                target = reconcile(adapter, max_workers, idle_timeout_seconds)
            except RuntimeError as exc:
                self.send_error(503, str(exc))
                return
            self._json({"target": target})

        def log_message(self, *_args):
            pass

    return Handler


def serve(adapter, max_workers: int, idle_timeout_seconds: int = 3600,
          overflow_reject_enabled: bool = False, host="0.0.0.0", port=8080):
    """Serve health, admission, and Cloud Scheduler reconciliation."""
    handler = build_handler(
        adapter, max_workers, idle_timeout_seconds, overflow_reject_enabled
    )
    ThreadingHTTPServer((host, port), handler).serve_forever()


class GcloudControllerAdapter:
    """Minimal real adapter; Cloud Run IAM authenticates the OIDC caller first."""

    def __init__(self, project: str, region: str, mig: str, subscription: str, bucket: str,
                 worker_heartbeat_ttl_seconds: int = 120):
        if worker_heartbeat_ttl_seconds <= 0:
            raise ValueError("worker heartbeat TTL must be positive")
        self.project, self.region, self.mig = project, region, mig
        self.subscription, self.bucket = subscription, bucket
        self.worker_heartbeat_ttl_seconds = worker_heartbeat_ttl_seconds

    def _run(self, *args: str) -> str:
        result = subprocess.run(["gcloud", "--project", self.project, *args], text=True,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "gcloud controller command failed")
        return result.stdout

    def authenticated_metrics(self) -> tuple[int, int, int, int]:
        backlog = self._monitoring_backlog()
        instances = json.loads(self._run("compute", "instance-groups", "managed",
                                         "list-instances", self.mig, "--region", self.region,
                                         "--format=json"))
        running = [
            instance for instance in instances
            if instance.get("instanceStatus") == "RUNNING"
        ]
        running_worker_ids = {
            str(instance.get("instance") or instance.get("name")).rsplit("/", 1)[-1]
            for instance in running
            if instance.get("instance") or instance.get("name")
        }
        active = len(running)
        leases, idle_age = self._worker_metrics(active, running_worker_ids)
        return backlog, active, leases, idle_age

    def _worker_index(self) -> list[WorkerState]:
        uri = f"gs://{self.bucket}/spot/v1/workers/index/current.json"
        try:
            raw = self._run("storage", "cat", uri)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "no urls matched" in message or "matched no objects" in message:
                return []
            raise
        try:
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get("schema") != WORKER_INDEX_SCHEMA \
                    or not isinstance(record.get("workers"), dict):
                raise ValueError("invalid schema")
            return [WorkerState.from_record(value) for value in record["workers"].values()]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("worker index is malformed") from exc

    def _worker_metrics(self, active_workers: int = 0,
                        running_worker_ids: set[str] | None = None) -> tuple[int, int]:
        states = self._worker_index()
        now = datetime.now(timezone.utc)
        fresh = [
            state for state in states
            if 0 <= (now - state.heartbeat_at).total_seconds()
            <= self.worker_heartbeat_ttl_seconds
        ]
        indexed_leases = sum(
            1 for state in states
            if state.active_request is not None and (
                state.active_lease_expires_at > now
                if state.active_lease_expires_at is not None
                else state in fresh
            )
        )
        indexed_worker_ids = {
            state.worker_id for state in states
            if state in fresh or (
                state.active_lease_expires_at is not None
                and state.active_lease_expires_at > now
            )
        }
        # A controller may roll out while older workers still have leases but
        # have not joined the new index. Count every such running instance as
        # leased until its first indexed heartbeat; this can only overcount.
        running_worker_ids = running_worker_ids or set()
        unidentified_workers = max(0, active_workers - len(running_worker_ids))
        unindexed_workers = len(running_worker_ids - indexed_worker_ids) + \
            unidentified_workers
        leases = indexed_leases + unindexed_workers
        if unindexed_workers:
            return leases, 0
        if not fresh:
            # No current heartbeat can justify keeping a RUNNING instance. A
            # large observed idle age lets the pure scale policy reach zero.
            return leases, 2 ** 31 - 1
        if any(state.active_request is not None for state in fresh):
            return leases, 0
        idle_age = int(min(max(0, (now - state.idle_since).total_seconds())
                           for state in fresh if state.idle_since is not None))
        return leases, idle_age

    def _worker_idle_age(self) -> int:
        return self._worker_metrics()[1]

    def _monitoring_backlog(self) -> int:
        """Read the supported Cloud Monitoring REST metric (not a gcloud alias)."""
        token = self._run("auth", "print-access-token").strip()
        now = datetime.now(timezone.utc)
        query = urlencode({
            "filter": "metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" "
                      f"AND resource.labels.subscription_id=\"{self.subscription}\"",
            "interval.endTime": now.isoformat(),
            "interval.startTime": (now - timedelta(minutes=5)).isoformat(),
            "view": "FULL",
        })
        request = Request(f"https://monitoring.googleapis.com/v3/projects/{self.project}/timeSeries?{query}",
                          headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        values = [point.get("value", {}).get("int64Value", 0)
                  for series in payload.get("timeSeries", []) for point in series.get("points", [])]
        return max((int(value) for value in values), default=0)

    def set_target(self, target: int) -> None:
        self._run("compute", "instance-groups", "managed", "resize", self.mig,
                  "--region", self.region, "--size", str(target), "--quiet")

    def record_capacity_fault(self, leases: int, maximum: int) -> None:
        self.capacity_fault = {"leases": leases, "maximum": maximum}

    @staticmethod
    def authorize(authorization: str) -> bool:
        # Cloud Run verifies this bearer token against run.invoker before the
        # request reaches the container.  Reject accidental direct traffic too.
        return authorization.startswith("Bearer ") and len(authorization) > 7


def main() -> None:
    """Run the controller under the Cloud Run service contract."""
    required = ("SPOT_PROJECT", "SPOT_REGION", "SPOT_MIG", "SPOT_SUBSCRIPTION", "SPOT_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("missing controller configuration: " + ", ".join(missing))
    overflow_value = os.environ.get("SPOT_OVERFLOW_REJECT_ENABLED", "false").lower()
    if overflow_value not in {"true", "false"}:
        raise SystemExit("SPOT_OVERFLOW_REJECT_ENABLED must be true or false")
    adapter = GcloudControllerAdapter(
        os.environ["SPOT_PROJECT"], os.environ["SPOT_REGION"], os.environ["SPOT_MIG"],
        os.environ["SPOT_SUBSCRIPTION"], os.environ["SPOT_BUCKET"],
        int(os.environ.get("SPOT_WORKER_HEARTBEAT_TTL_SECONDS", "120")),
    )
    serve(adapter, int(os.environ.get("SPOT_MAX_WORKERS", "5")),
          int(os.environ.get("SPOT_IDLE_SECONDS", "3600")),
          overflow_value == "true",
          port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
