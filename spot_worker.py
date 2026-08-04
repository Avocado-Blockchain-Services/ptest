"""One-suite-at-a-time Spot worker and its authenticated GCP adapter."""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from spot_queue import GcloudSpotQueueStore, SpotResult


class Worker:
    """Supervisor that owns queue credentials; children receive none."""

    def __init__(self, adapter, workspace_root: Path, worker_id: str, lease_seconds: int = 120):
        self.adapter, self.workspace_root, self.worker_id = adapter, workspace_root, worker_id
        self.lease_seconds, self.stopping = lease_seconds, False

    def stop(self, *_args):
        self.stopping = True
        stop_active = getattr(self.adapter, "stop_active", None)
        if stop_active:
            stop_active()

    def run_once(self) -> bool:
        message = self.adapter.pull()
        if message is None or self.stopping:
            return False
        request = self.adapter.read_request(message)
        if request is None or self.adapter.read_result(request.request_key) is not None:
            self.adapter.acknowledge(message)
            return False
        lease = self.adapter.claim(request.request_key, self.worker_id, self.lease_seconds)
        if lease is None:
            return False
        self.adapter.heartbeat(lease)
        bound = self.adapter.for_message(message, self)
        heartbeat_done = threading.Event()
        current_lease = [lease]

        def heartbeat_loop():
            while not heartbeat_done.wait(max(1, self.lease_seconds // 3)) and not self.stopping:
                renewed = bound.heartbeat(current_lease[0])
                if renewed is None:
                    self.stop()
                    return
                current_lease[0] = renewed

        heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat.start()
        try:
            run_claimed_request(request, self.workspace_root, bound)
        finally:
            heartbeat_done.set()
            heartbeat.join(timeout=1)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self.stopping:
            if not self.run_once() and not self.stopping:
                time.sleep(2)


def run_claimed_request(request, workspace_root: Path, adapter) -> SpotResult | None:
    """Run one leased request; durably publish before acknowledging its message."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="ptest-spot-", dir=workspace_root))
    archive, source = workspace / "source.tar.gz", workspace / "source"
    source.mkdir()
    try:
        adapter.download(request.source_uri, archive)
        adapter.unpack(archive, source)
        prepare = getattr(adapter, "prepare", None)
        if prepare:
            prepare(source)
        if getattr(adapter, "preempted", lambda: False)():
            return None
        code, output = adapter.run(request.command, source)
        # Preemption is not a terminal test outcome. The durable lease expires
        # and Pub/Sub redelivers the original request to a future worker.
        if getattr(adapter, "preempted", lambda: False)():
            return None
        result = SpotResult(request.request_key, "passed" if code == 0 else "failed", code,
                            output, datetime.now(timezone.utc))
        if not adapter.publish_result(result):
            return SpotResult(request.request_key, "infrastructure", None,
                              "result publication lost", datetime.now(timezone.utc))
        if not getattr(adapter, "preempted", lambda: False)():
            adapter.acknowledge()
        return result
    except Exception as exc:
        if getattr(adapter, "preempted", lambda: False)():
            return None
        result = SpotResult(request.request_key, "infrastructure", None, str(exc),
                            datetime.now(timezone.utc))
        try:
            adapter.publish_result(result)
        except Exception:
            pass
        return result
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


class LocalProcessAdapter:
    """Credentialless, bounded child process runner used below the supervisor."""

    def __init__(self, timeout_seconds: int = 1800):
        self.timeout_seconds, self._process = timeout_seconds, None

    def run(self, command, cwd):
        # Deliberately do not forward GCP credential/configuration variables.
        # Bubblewrap gives each untrusted suite an unprivileged user and a
        # network namespace with no interfaces; the supervisor retains GCP I/O.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd),
               "PYTHONUNBUFFERED": "1", "NO_PROXY": "*"}
        self._process = subprocess.Popen(
            ["bwrap", "--die-with-parent", "--unshare-user", "--uid", "65534", "--gid", "65534",
             "--unshare-net", "--ro-bind", "/", "/", "--bind", str(cwd), "/work",
             "--chdir", "/work", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "/bin/sh", "-lc", command],
            cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = self._process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            self.stop_active()
            output, _ = self._process.communicate()
            return 124, (output + "\nptest spot task timed out")[-200000:]
        finally:
            code = self._process.returncode if self._process else None
            self._process = None
        return code, output[-200000:]

    def prepare(self, cwd: Path) -> None:
        """Provision lockfile-pinned Node dependencies before network isolation."""
        package = cwd / "package.json"
        if not package.exists():
            return
        lock = cwd / "package-lock.json"
        if not lock.exists():
            raise RuntimeError("Spot Vitest requests require package-lock.json")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd),
               "NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_AUDIT": "false",
               "NPM_CONFIG_FUND": "false"}
        completed = subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                                   cwd=cwd, env=env, text=True, capture_output=True, timeout=600)
        if completed.returncode:
            raise RuntimeError((completed.stdout + completed.stderr)[-200000:])
        for path in cwd.rglob("*"):
            if path.is_symlink():
                continue
            path.chmod(0o777 if path.is_dir() else (0o755 if path.stat().st_mode & 0o111 else 0o666))

    def stop_active(self):
        if self._process and self._process.poll() is None:
            os.killpg(self._process.pid, signal.SIGTERM)


def _safe_archive_name(name: str) -> Path:
    candidate = Path(name)
    if not name or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("unsafe archive member")
    return candidate


def unpack_archive(archive: Path, destination: Path) -> None:
    """Extract only regular files/dirs and contained symlinks with safe modes."""
    with tarfile.open(archive, "r:gz") as payload:
        members = payload.getmembers()
        for member in members:
            path = _safe_archive_name(member.name)
            if member.isdir() or member.isfile():
                continue
            if member.issym():
                resolved = path.parent / member.linkname
                if Path(member.linkname).is_absolute() or ".." in resolved.parts:
                    raise ValueError("unsafe archive member")
                continue
            raise ValueError("unsafe archive member")
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o777)
        for member in members:
            target = destination / _safe_archive_name(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o777)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.parent.chmod(0o777)
                source = payload.extractfile(member)
                if source is None:
                    raise ValueError("unsafe archive member")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o666)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.parent.chmod(0o777)
                target.symlink_to(member.linkname)


class GcloudWorkerAdapter:
    """Gcloud-backed supervisor: it alone reads queue records and Pub/Sub."""

    def __init__(self, project: str, bucket: str, topic: str, subscription: str):
        self.project, self.subscription = project, subscription
        self.store = GcloudSpotQueueStore(["gcloud", "--project", project], bucket, topic)
        self.runner = LocalProcessAdapter(int(os.environ.get("SPOT_TASK_TIMEOUT_SECONDS", "1800")))

    def _run(self, *args: str) -> str:
        result = subprocess.run(["gcloud", "--project", self.project, *args], text=True,
                                capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "gcloud worker command failed")
        return result.stdout

    def pull(self):
        messages = json.loads(self._run("pubsub", "subscriptions", "pull", self.subscription,
                                        "--limit=1", "--format=json"))
        if not messages:
            return None
        message = messages[0]
        try:
            key = base64.b64decode(message["message"]["data"], validate=True).decode("ascii")
            return {"ack_id": message["ackId"], "request_key": key}
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("malformed Pub/Sub Spot request") from exc

    def read_request(self, message):
        return self.store.read_request(message["request_key"])

    def read_result(self, request_key):
        return self.store.read_result(request_key)

    def claim(self, request_key, worker_id, lease_seconds):
        return self.store.claim(request_key, worker_id, datetime.now(timezone.utc), lease_seconds)

    def heartbeat(self, _lease):
        # First heartbeat establishes the loop. Bound heartbeats renew both the
        # durable generation-checked lease and the transient delivery deadline.
        return None

    def acknowledge(self, message):
        self._run("pubsub", "subscriptions", "ack", self.subscription,
                  "--ack-ids", message["ack_id"])

    def for_message(self, message, worker):
        return _MessageAdapter(self, message, worker)

    def stop_active(self):
        self.runner.stop_active()


class _MessageAdapter:
    def __init__(self, supervisor: GcloudWorkerAdapter, message, worker: Worker):
        self.supervisor, self.message, self.worker = supervisor, message, worker

    def download(self, source_uri, destination):
        self.supervisor._run("storage", "cp", source_uri, str(destination))

    @staticmethod
    def unpack(archive, destination):
        unpack_archive(archive, destination)

    def run(self, command, cwd):
        return self.supervisor.runner.run(command, cwd)

    def prepare(self, cwd):
        self.supervisor.runner.prepare(cwd)

    def publish_result(self, result):
        return self.supervisor.store.publish_result(result)

    def acknowledge(self):
        self.supervisor.acknowledge(self.message)

    def heartbeat(self, lease):
        renewed = self.supervisor.store.renew(
            lease, datetime.now(timezone.utc),
            int((lease.expires_at - lease.acquired_at).total_seconds()),
        )
        if renewed is None:
            return None
        self.supervisor._run("pubsub", "subscriptions", "modify-message-ack-deadline",
                             self.supervisor.subscription, "--ack-ids", self.message["ack_id"],
                             "--ack-deadline", str(max(10, int((renewed.expires_at -
                                                                  renewed.acquired_at).total_seconds()))))
        return renewed

    def preempted(self):
        return self.worker.stopping


def _serve_healthz() -> ThreadingHTTPServer:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    """Execute the production pull → claim → run → publish → ack loop."""
    required = ("SPOT_PROJECT", "SPOT_BUCKET", "SPOT_TOPIC", "SPOT_SUBSCRIPTION")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("missing worker configuration: " + ", ".join(missing))
    _serve_healthz()
    worker = Worker(GcloudWorkerAdapter(os.environ["SPOT_PROJECT"], os.environ["SPOT_BUCKET"],
                                        os.environ["SPOT_TOPIC"], os.environ["SPOT_SUBSCRIPTION"]),
                    Path(os.environ.get("SPOT_WORKSPACE_ROOT", "/tmp/ptest-spot")),
                    os.environ.get("HOSTNAME", "spot-worker"),
                    int(os.environ.get("SPOT_LEASE_SECONDS", "120")))
    worker.run_forever()


if __name__ == "__main__":
    main()
