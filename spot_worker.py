"""One-suite-at-a-time Spot worker and its authenticated GCP adapter."""

from __future__ import annotations

import base64
import json
import os
import random
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from spot_queue import GcloudSpotQueueStore, SpotResult, TERMINAL_OUTPUT_LIMIT


def poll_backoff(empty_polls: int, random_fn=random.random) -> float:
    """Bound idle polling while desynchronising an otherwise identical fleet."""
    base = min(30.0, float(2 ** max(0, empty_polls)))
    return min(30.0, base * (0.75 + random_fn() * 0.5))


class Worker:
    """Supervisor that owns queue credentials; children receive none."""

    def __init__(self, adapter, workspace_root: Path, worker_id: str, lease_seconds: int = 120,
                 idle_heartbeat_seconds: int = 45):
        self.adapter, self.workspace_root, self.worker_id = adapter, workspace_root, worker_id
        self.lease_seconds, self.stopping = lease_seconds, False
        self.idle_heartbeat_seconds = idle_heartbeat_seconds
        self._last_idle_heartbeat: float | None = None

    def stop(self, *_args):
        self.stopping = True
        stop_active = getattr(self.adapter, "stop_active", None)
        if stop_active:
            stop_active()

    def _dead_letter(self, message) -> None:
        """Acknowledge permanent poison only after its DLQ transfer succeeds."""
        if getattr(self.adapter, "dead_letter", lambda _message: False)(message):
            self.adapter.acknowledge(message)

    def run_once(self) -> bool:
        message = self.adapter.pull()
        if message is None or self.stopping:
            if not self.stopping:
                mark_idle = getattr(self.adapter, "heartbeat_worker", None)
                now = time.monotonic()
                if mark_idle and (self._last_idle_heartbeat is None or
                                 now - self._last_idle_heartbeat >= self.idle_heartbeat_seconds):
                    mark_idle(self.worker_id, None)
                    self._last_idle_heartbeat = now
            return False
        if isinstance(message, dict) and message.get("poison"):
            # A permanent delivery validation failure must be transferred before
            # acknowledgement.  If transfer is unavailable, leave it for retry.
            self._dead_letter(message)
            return False
        try:
            request = self.adapter.read_request(message)
        except (ValueError, KeyError, TypeError):
            self._dead_letter(message)
            return False
        if request is None or self.adapter.read_result(request.request_key) is not None:
            self.adapter.acknowledge(message)
            return False
        if getattr(self.adapter, "is_cancelled", lambda _key: False)(request.request_key):
            self.adapter.acknowledge(message)
            return False
        lease = self.adapter.claim(request.request_key, self.worker_id, self.lease_seconds)
        if lease is None:
            return False
        mark_active = getattr(self.adapter, "heartbeat_worker", None)
        if mark_active:
            mark_active(self.worker_id, request.request_key)
            self._last_idle_heartbeat = None
        self.adapter.heartbeat(lease)
        bound = self.adapter.for_message(message, self)
        bind_lease = getattr(bound, "bind_lease", None)
        if bind_lease:
            bind_lease(lease)
        heartbeat_done = threading.Event()
        current_lease = [lease]

        def heartbeat_loop():
            while not heartbeat_done.wait(max(1, self.lease_seconds // 3)) and not self.stopping:
                try:
                    renewed = bound.heartbeat(current_lease[0])
                except Exception:
                    self.stop()
                    return
                if renewed is None:
                    self.stop()
                    return
                current_lease[0] = renewed

        heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat.start()
        result = None
        try:
            result = run_claimed_request(request, self.workspace_root, bound)
        finally:
            heartbeat_done.set()
            heartbeat.join(timeout=1)
        if result is not None and not self.stopping:
            if mark_active:
                mark_active(self.worker_id, None)
        elif getattr(self.adapter, "is_cancelled", lambda _key: False)(request.request_key):
            # An idle heartbeat written only after run_claimed_request returns
            # is the positive proof that the cancelled child is quiesced. The
            # timeout caller may start locally only after this or lease expiry.
            if mark_active:
                mark_active(self.worker_id, None)
            self.adapter.acknowledge(message)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        empty_polls = 0
        while not self.stopping:
            worked = self.run_once()
            if worked:
                empty_polls = 0
            elif not self.stopping:
                time.sleep(poll_backoff(empty_polls))
                empty_polls += 1


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
            prepare(source, request.kind)
        if getattr(adapter, "preempted", lambda: False)() or \
                getattr(adapter, "cancelled", lambda: False)():
            return None
        code, output = adapter.run(request.command, source)
        # Preemption is not a terminal test outcome. The durable lease expires
        # and Pub/Sub redelivers the original request to a future worker.
        if getattr(adapter, "preempted", lambda: False)() or \
                getattr(adapter, "cancelled", lambda: False)():
            return None
        output_uri = None
        if len(output) > TERMINAL_OUTPUT_LIMIT:
            store_output = getattr(adapter, "store_output", None)
            if store_output is None:
                raise RuntimeError("large Spot output cannot be durably stored")
            output_uri = store_output(request.request_key, output)
            output = output[:TERMINAL_OUTPUT_LIMIT]
        result = SpotResult(request.request_key, "passed" if code == 0 else "failed", code,
                            output, datetime.now(timezone.utc), output_uri)
        if not adapter.publish_result(result):
            return None
        if not getattr(adapter, "preempted", lambda: False)():
            adapter.acknowledge()
        return result
    except Exception as exc:
        if getattr(adapter, "preempted", lambda: False)() or \
                getattr(adapter, "cancelled", lambda: False)():
            return None
        result = SpotResult(request.request_key, "infrastructure", None, str(exc),
                            datetime.now(timezone.utc))
        try:
            adapter.publish_result(result)
        except Exception:
            pass
        return result
    finally:
        stop_postgres = getattr(adapter, "stop_postgres", None)
        if stop_postgres:
            stop_postgres()
        shutil.rmtree(workspace, ignore_errors=True)


def terminate_process_group(process: subprocess.Popen, grace_seconds: float = 5.0) -> bool:
    """Boundedly stop and reap an isolated test process group.

    A worker cannot become idle while descendants still hold the lease's
    workspace or database.  The same primitive is used for timeout,
    cancellation, lease loss, and supervisor shutdown.
    """
    if process.poll() is not None:
        process.wait()
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return True
    try:
        process.wait(timeout=max(0.0, grace_seconds))
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    return process.poll() is not None


class LocalProcessAdapter:
    """Credentialless, bounded child process runner used below the supervisor."""

    def __init__(self, timeout_seconds: int = 1800):
        self.timeout_seconds, self._process = timeout_seconds, None
        self._pgdata: Path | None = None
        self._pgsocket: Path | None = None

    @staticmethod
    def _path() -> str:
        return "/usr/lib/postgresql/15/bin:" + os.environ.get("PATH", "/usr/bin:/bin")

    def run(self, command, cwd):
        # Deliberately do not forward GCP credential/configuration variables.
        # Bubblewrap gives each untrusted suite an unprivileged user and a
        # network namespace with no interfaces; the supervisor retains GCP I/O.
        env = {"PATH": self._path(), "HOME": str(cwd),
               "PYTHONUNBUFFERED": "1", "NO_PROXY": "*",
               "UV_CACHE_DIR": str(cwd / ".uv-cache"), "UV_OFFLINE": "1"}
        if self._pgsocket:
            env["DATABASE_URL"] = (
                "postgresql+asyncpg://ptest@/persea_content_maker_test?host=" + str(self._pgsocket)
            )
        self._process = subprocess.Popen(
            ["bwrap", "--die-with-parent", "--unshare-user", "--uid", "65534", "--gid", "65534",
             "--unshare-net", "--ro-bind", "/", "/", "--bind", str(cwd.parents[2]),
             str(cwd.parents[2]), "--chdir", str(cwd), "--proc", "/proc", "--dev", "/dev", "--tmpfs",
             str(cwd.parents[2] / "bwrap-scratch"),
             "/bin/sh", "-lc", command],
            cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = self._process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_group(self._process)
            output, _ = self._process.communicate()
            return 124, (output + "\nptest spot task timed out")[-200000:]
        finally:
            code = self._process.returncode if self._process else None
            self._process = None
            self.stop_postgres()
        return code, output[-200000:]

    def prepare(self, cwd: Path, kind: str) -> None:
        """Provision the request's pinned dependencies before network isolation."""
        if kind == "pytest":
            if not (cwd / "pyproject.toml").exists() or not (cwd / "uv.lock").exists():
                raise RuntimeError("Spot pytest requests require pyproject.toml and uv.lock")
            completed = subprocess.run(
                ["uv", "sync", "--frozen"], cwd=cwd,
                env={**os.environ, "UV_CACHE_DIR": str(cwd / ".uv-cache")},
                text=True, capture_output=True, timeout=600,
            )
            if completed.returncode:
                raise RuntimeError(completed.stdout + completed.stderr)
            self._start_postgres(cwd)
            return
        if kind != "vitest":
            raise RuntimeError(f"unsupported Spot request kind: {kind}")
        if not (cwd / "package.json").exists():
            raise RuntimeError("Spot Vitest requests require package.json")
        lock = cwd / "package-lock.json"
        if not lock.exists():
            raise RuntimeError("Spot Vitest requests require package-lock.json")
        env = {"PATH": self._path(), "HOME": str(cwd),
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
            terminate_process_group(self._process)

    def _start_postgres(self, cwd: Path) -> None:
        """Create a private, per-request PostgreSQL cluster and Unix socket."""
        self.stop_postgres()
        self._pgdata = cwd / ".spot-pgdata"
        self._pgsocket = cwd / ".spot-pg"
        self._pgdata.mkdir(mode=0o700)
        self._pgsocket.mkdir(mode=0o700)
        pg_log = self._pgdata / "postgres.log"
        commands = [
            ["initdb", "-D", str(self._pgdata), "-U", "ptest", "--auth=trust", "--encoding=UTF8"],
            ["pg_ctl", "-D", str(self._pgdata), "-l", str(pg_log),
             "-o", f"-k {self._pgsocket}", "-w", "-t", "60", "start"],
        ]
        for command in commands:
            # postgres inherits pg_ctl's streams. Capturing them makes
            # subprocess.run wait for the daemon's open pipe after pg_ctl has
            # successfully returned, creating a false startup timeout.
            completed = subprocess.run(
                command, text=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=90,
            )
            if completed.returncode:
                self.stop_postgres()
                detail = pg_log.read_text(errors="replace") if pg_log.exists() else ""
                raise RuntimeError(detail)

    def stop_postgres(self) -> None:
        if self._pgdata is None:
            return
        subprocess.run(["pg_ctl", "-D", str(self._pgdata), "-m", "immediate", "stop"],
                       text=True, capture_output=True, timeout=60)
        self._pgdata = self._pgsocket = None


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

    def __init__(self, project: str, bucket: str, topic: str, subscription: str,
                 dead_letter_topic: str | None = None):
        self.project, self.subscription = project, subscription
        self.dead_letter_topic = dead_letter_topic or f"{topic}-dead"
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
            return {"ack_id": message.get("ackId"), "poison": True,
                    "data": message.get("message", {}).get("data", ""), "error": str(exc)}

    def read_request(self, message):
        return self.store.read_request(message["request_key"])

    def read_result(self, request_key):
        return self.store.read_result(request_key)

    def is_cancelled(self, request_key):
        return self.store.is_cancelled(request_key)

    def claim(self, request_key, worker_id, lease_seconds):
        now = datetime.now(timezone.utc)
        self.heartbeat_worker(
            worker_id, request_key, now + timedelta(seconds=lease_seconds), now
        )
        lease = self.store.claim(request_key, worker_id, now, lease_seconds)
        if lease is None:
            self.heartbeat_worker(worker_id, None)
        return lease

    def heartbeat(self, _lease):
        # First heartbeat establishes the loop. Bound heartbeats renew both the
        # durable generation-checked lease and the transient delivery deadline.
        return None

    def heartbeat_worker(self, worker_id, active_request, lease_expires_at=None, now=None):
        return self.store.heartbeat_worker(
            worker_id, active_request, now or datetime.now(timezone.utc), lease_expires_at
        )

    def release(self, lease):
        return self.store.release(lease, datetime.now(timezone.utc))

    def acknowledge(self, message):
        self._run("pubsub", "subscriptions", "ack", self.subscription,
                  "--ack-ids", message["ack_id"])

    def dead_letter(self, message) -> bool:
        try:
            self._run("pubsub", "topics", "publish", self.dead_letter_topic,
                      "--message", message.get("data", ""))
        except Exception:
            return False
        return True

    def for_message(self, message, worker):
        return _MessageAdapter(self, message, worker)

    def stop_active(self):
        self.runner.stop_active()


class _MessageAdapter:
    def __init__(self, supervisor: GcloudWorkerAdapter, message, worker: Worker):
        self.supervisor, self.message, self.worker = supervisor, message, worker
        self._lease = None
        self._terminal = False
        self._transition_lock = threading.Lock()

    def bind_lease(self, lease):
        self._lease = lease

    def download(self, source_uri, destination):
        prefix = f"gs://{self.supervisor.store.bucket}/"
        if not source_uri.startswith(prefix):
            raise RuntimeError("source archive is outside the configured bucket")
        object_name = source_uri.removeprefix(prefix)
        token = self.supervisor._run("auth", "print-access-token").strip()
        request = Request(
            "https://storage.googleapis.com/storage/v1/b/"
            f"{quote(self.supervisor.store.bucket, safe='')}/o/"
            f"{quote(object_name, safe='')}?alt=media",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=120) as response:
            Path(destination).write_bytes(response.read())

    @staticmethod
    def unpack(archive, destination):
        unpack_archive(archive, destination)

    def run(self, command, cwd):
        return self.supervisor.runner.run(command, cwd)

    def prepare(self, cwd, kind):
        self.supervisor.runner.prepare(cwd, kind)

    def store_output(self, request_key, output):
        return self.supervisor.store.store_output(request_key, output)

    def stop_postgres(self):
        self.supervisor.runner.stop_postgres()

    def publish_result(self, result):
        with self._transition_lock:
            if self._lease is None:
                raise RuntimeError("message adapter has no active lease")
            published = self.supervisor.store.publish_result(result, self._lease)
            if published:
                self._terminal = True
            return published

    def acknowledge(self):
        self.supervisor.acknowledge(self.message)

    def heartbeat(self, lease):
        with self._transition_lock:
            if self._terminal:
                return self._lease
            if self.cancelled():
                self.worker.stop()
                return None
            renewed_at = datetime.now(timezone.utc)
            lease_seconds = int((lease.expires_at - lease.acquired_at).total_seconds())
            self.supervisor.heartbeat_worker(
                self.worker.worker_id, lease.request_key,
                renewed_at + timedelta(seconds=lease_seconds), renewed_at,
            )
            renewed = self.supervisor.store.renew(lease, renewed_at, lease_seconds)
            if renewed is None:
                return None
            self._lease = renewed
            self.supervisor._run(
                "pubsub", "subscriptions", "modify-message-ack-deadline",
                self.supervisor.subscription, "--ack-ids", self.message["ack_id"],
                "--ack-deadline", str(max(10, int((renewed.expires_at -
                                                   renewed.acquired_at).total_seconds()))),
            )
            return renewed

    def preempted(self):
        return self.worker.stopping

    def cancelled(self):
        return self.supervisor.store.is_cancelled(self.message["request_key"])


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
                                        os.environ["SPOT_TOPIC"], os.environ["SPOT_SUBSCRIPTION"],
                                        os.environ.get("SPOT_DEAD_LETTER_TOPIC")),
                    Path(os.environ.get("SPOT_WORKSPACE_ROOT", "/tmp/ptest-spot")),
                    os.environ.get("HOSTNAME", "spot-worker"),
                    int(os.environ.get("SPOT_LEASE_SECONDS", "120")))
    worker.run_forever()


if __name__ == "__main__":
    main()
