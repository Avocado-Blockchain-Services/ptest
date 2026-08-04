"""Authenticated capacity controller for the opt-in Spot worker MIG."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from spot_queue import Lease


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
        return max(active_workers, active_leases)
    if max_workers == 0:
        return 0
    if backlog:
        return min(max_workers, max(backlog, 1))
    return 1 if active_workers and idle_age_seconds < idle_timeout_seconds else 0


def reconcile(adapter, max_workers: int, idle_timeout_seconds: int = 3600) -> int:
    """Read authenticated metrics and set only the Spot MIG target.

    A queue request cannot be safely converted into a Cloud Run Job execution
    without its source/command contract. Until a compatible overflow dispatcher
    is configured, overflow remains durably queued and ptest's normal timeout
    fallback is the only safe fallback.
    """
    backlog, workers, leases, idle_age = adapter.authenticated_metrics()
    target = scale_target(backlog, workers, idle_age, max_workers, leases,
                          idle_timeout_seconds)
    adapter.set_target(target)
    if backlog > max_workers and getattr(adapter, "overflow_enabled", False):
        adapter.retain_overflow(backlog - max_workers)
    return target


def serve(adapter, max_workers: int, idle_timeout_seconds: int = 3600,
          host="0.0.0.0", port=8080):
    """Serve health checks and IAM-protected Cloud Scheduler reconciliation."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/reconcile" or not adapter.authorize(
                    self.headers.get("Authorization", "")):
                self.send_error(401)
                return
            try:
                target = reconcile(adapter, max_workers, idle_timeout_seconds)
            except RuntimeError as exc:
                self.send_error(503, str(exc))
                return
            body = json.dumps({"target": target}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


class GcloudControllerAdapter:
    """Minimal real adapter; Cloud Run IAM authenticates the OIDC caller first."""

    def __init__(self, project: str, region: str, mig: str, subscription: str, bucket: str,
                 overflow_enabled: bool = False):
        self.project, self.region, self.mig = project, region, mig
        self.subscription, self.bucket = subscription, bucket
        self.overflow_enabled = overflow_enabled

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
        active = sum(1 for instance in instances if instance.get("instanceStatus") == "RUNNING")
        leases = self._active_leases()
        # With no lease, a worker's last-start time is a conservative observable
        # upper bound for its idle age. Query the actual VM rather than treating
        # the policy timeout as a metric or inferring it from MIG desired size.
        parsed = []
        for instance in instances:
            parts = instance.get("instance", "").rstrip("/").split("/")
            if len(parts) < 5 or parts[-4] != "zones" or parts[-2] != "instances":
                continue
            details = json.loads(self._run("compute", "instances", "describe", parts[-1],
                                           "--zone", parts[-3], "--format=json"))
            if details.get("lastStartTimestamp"):
                parsed.append(datetime.fromisoformat(
                    details["lastStartTimestamp"].replace("Z", "+00:00")))
        idle_age = int(min((datetime.now(timezone.utc) - value).total_seconds() for value in parsed)
                       ) if parsed else 0
        return backlog, active, leases, idle_age

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

    def _active_leases(self) -> int:
        listing = self._run("storage", "ls", f"gs://{self.bucket}/spot/v1/leases/")
        now, active = datetime.now(timezone.utc), 0
        for uri in listing.splitlines():
            raw = self._run("storage", "cat", uri)
            try:
                lease = Lease.from_record(json.loads(raw), 1)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            active += lease.expires_at > now
        return active

    def set_target(self, target: int) -> None:
        self._run("compute", "instance-groups", "managed", "resize", self.mig,
                  "--region", self.region, "--size", str(target), "--quiet")

    def retain_overflow(self, excess: int) -> dict[str, int | str]:
        # Explicit, safe behavior: do not launch a Cloud Run job with an
        # incomplete request contract; leave the durable Pub/Sub message alone.
        self.overflow_state = {"mode": "retain-queue", "excess": excess}
        return self.overflow_state

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
    adapter = GcloudControllerAdapter(
        os.environ["SPOT_PROJECT"], os.environ["SPOT_REGION"], os.environ["SPOT_MIG"],
        os.environ["SPOT_SUBSCRIPTION"], os.environ["SPOT_BUCKET"],
        os.environ.get("SPOT_OVERFLOW_TO_CLOUDRUN", "false").lower() == "true",
    )
    serve(adapter, int(os.environ.get("SPOT_MAX_WORKERS", "5")),
          int(os.environ.get("SPOT_IDLE_SECONDS", "3600")),
          port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
