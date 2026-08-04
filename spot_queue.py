"""Durable, generation-safe GCS records for the opt-in Spot test queue."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


REQUEST_SCHEMA = "ptest-spot-request-v2"
LEASE_SCHEMA = "ptest-spot-lease-v1"
RESULT_SCHEMA = "ptest-spot-result-v1"
CANCEL_SCHEMA = "ptest-spot-cancel-v1"
WORKER_SCHEMA = "ptest-spot-worker-v1"
WORKER_INDEX_SCHEMA = "ptest-spot-worker-index-v1"
WORKER_INDEX_IDLE_LIMIT = 64
WORKER_INDEX_LEGACY_ACTIVE_SECONDS = 300
TERMINAL_OUTPUT_LIMIT = 200_000
PTEST_SPOT_COMPANION_ABI = 1
SOURCE_URI_RE = re.compile(
    r"gs://[A-Za-z0-9][A-Za-z0-9._-]+/sources/[0-9a-f]{64}\.tar\.gz"
)


class SpotQueueUnavailable(RuntimeError):
    """The durable queue cannot be safely read or updated."""


class SpotRecordError(ValueError):
    """A durable queue record is malformed or fails its schema contract."""


class SpotQueueStore(Protocol):
    """Durable record operations shared by the worker and ptest client."""

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> "Lease | None": ...

    def publish_result(self, result: "SpotResult", lease: "Lease") -> bool: ...

    def renew(self, lease: "Lease", now: datetime, lease_seconds: int) -> "Lease | None": ...

    def cancel_request(self, request_key: str, reason: str, now: datetime) -> bool: ...

    def wait_cancellation_quiesced(self, request_key: str,
                                   timeout_seconds: int) -> bool: ...


def _request_key(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SpotRecordError("request_key must be a lowercase SHA-256 digest")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpotRecordError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise SpotRecordError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SpotRecordError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SpotRecordError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _bound(record_key: str, lookup_key: str, record_name: str) -> None:
    if record_key != _request_key(lookup_key):
        raise SpotRecordError(f"{record_name} request_key does not match its object key")


@dataclass(frozen=True)
class SpotRequest:
    request_key: str
    command: str
    source_uri: str
    created_at: datetime
    kind: str = "pytest"

    def __post_init__(self):
        _request_key(self.request_key)
        _text(self.command, "command")
        if self.kind not in {"pytest", "vitest"}:
            raise SpotRecordError("kind must be one of: pytest, vitest")
        if not isinstance(self.source_uri, str) or not SOURCE_URI_RE.fullmatch(self.source_uri):
            raise SpotRecordError("source_uri must be a canonical source archive URI")
        if self.created_at.tzinfo is None:
            raise SpotRecordError("created_at must include a timezone")

    def to_record(self) -> dict:
        return {
            "schema": REQUEST_SCHEMA,
            "request_key": self.request_key,
            "command": self.command,
            "kind": self.kind,
            "source_uri": self.source_uri,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
        }

    @property
    def stable_identity(self) -> tuple[str, str, str, str]:
        """Fields that remain stable when an at-least-once publish is retried."""
        return self.request_key, self.command, self.source_uri, self.kind

    @classmethod
    def from_record(cls, record: object) -> "SpotRequest":
        if not isinstance(record, dict) or record.get("schema") != REQUEST_SCHEMA:
            raise SpotRecordError("request record has an invalid schema")
        return cls(
            _request_key(record.get("request_key")),
            _text(record.get("command"), "command"),
            _text(record.get("source_uri"), "source_uri"),
            _timestamp(record.get("created_at"), "created_at"),
            _text(record.get("kind"), "kind"),
        )


@dataclass(frozen=True)
class Lease:
    request_key: str
    worker_id: str
    acquired_at: datetime
    expires_at: datetime
    generation: int

    def __post_init__(self):
        _request_key(self.request_key)
        _text(self.worker_id, "worker_id")
        if self.acquired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise SpotRecordError("lease timestamps must include a timezone")
        if self.expires_at <= self.acquired_at or type(self.generation) is not int \
                or self.generation <= 0:
            raise SpotRecordError("lease is invalid")

    def to_record(self) -> dict:
        return {
            "schema": LEASE_SCHEMA,
            "request_key": self.request_key,
            "worker_id": self.worker_id,
            "acquired_at": self.acquired_at.astimezone(timezone.utc).isoformat(),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
        }

    @property
    def stable_identity(self) -> tuple[str, str, datetime, datetime]:
        """Fields that identify one acquisition across a post-CAS re-read."""
        return self.request_key, self.worker_id, self.acquired_at, self.expires_at

    @classmethod
    def from_record(cls, record: object, generation: int) -> "Lease":
        if not isinstance(record, dict) or record.get("schema") != LEASE_SCHEMA:
            raise SpotRecordError("lease record has an invalid schema")
        return cls(
            _request_key(record.get("request_key")),
            _text(record.get("worker_id"), "worker_id"),
            _timestamp(record.get("acquired_at"), "acquired_at"),
            _timestamp(record.get("expires_at"), "expires_at"), generation,
        )


@dataclass(frozen=True)
class Cancellation:
    request_key: str
    reason: str
    cancelled_at: datetime
    lease_worker_id: str | None
    lease_expires_at: datetime | None

    def __post_init__(self):
        _request_key(self.request_key)
        _text(self.reason, "reason")
        if self.cancelled_at.tzinfo is None:
            raise SpotRecordError("cancelled_at must include a timezone")
        if (self.lease_worker_id is None) != (self.lease_expires_at is None):
            raise SpotRecordError("cancellation lease barrier is malformed")
        if self.lease_worker_id is not None:
            _text(self.lease_worker_id, "lease_worker_id")
            if self.lease_expires_at.tzinfo is None:
                raise SpotRecordError("lease_expires_at must include a timezone")

    def to_record(self) -> dict:
        return {
            "schema": CANCEL_SCHEMA,
            "request_key": self.request_key,
            "reason": self.reason,
            "cancelled_at": self.cancelled_at.astimezone(timezone.utc).isoformat(),
            "lease_worker_id": self.lease_worker_id,
            "lease_expires_at": (
                self.lease_expires_at.astimezone(timezone.utc).isoformat()
                if self.lease_expires_at else None
            ),
        }

    @classmethod
    def from_record(cls, record: object) -> "Cancellation":
        if not isinstance(record, dict) or record.get("schema") != CANCEL_SCHEMA:
            raise SpotRecordError("cancellation record has an invalid schema")
        expires = record.get("lease_expires_at")
        return cls(
            _request_key(record.get("request_key")),
            _text(record.get("reason"), "reason"),
            _timestamp(record.get("cancelled_at"), "cancelled_at"),
            record.get("lease_worker_id"),
            _timestamp(expires, "lease_expires_at") if expires is not None else None,
        )


@dataclass(frozen=True)
class WorkerState:
    worker_id: str
    idle_since: datetime | None
    heartbeat_at: datetime
    active_request: str | None
    active_lease_expires_at: datetime | None = None

    def __post_init__(self):
        _text(self.worker_id, "worker_id")
        if self.idle_since is not None and self.idle_since.tzinfo is None:
            raise SpotRecordError("idle_since must include a timezone")
        if self.heartbeat_at.tzinfo is None:
            raise SpotRecordError("heartbeat_at must include a timezone")
        if self.active_request is not None:
            _request_key(self.active_request)
        if (self.idle_since is None) != (self.active_request is not None):
            raise SpotRecordError("worker state must be exactly idle or active")
        if self.active_lease_expires_at is not None:
            if self.active_request is None:
                raise SpotRecordError("idle worker cannot retain a lease expiry")
            if self.active_lease_expires_at.tzinfo is None:
                raise SpotRecordError("active lease expiry must include a timezone")
            if self.active_lease_expires_at <= self.heartbeat_at:
                raise SpotRecordError("active lease expiry must follow its heartbeat")

    def to_record(self) -> dict:
        return {"schema": WORKER_SCHEMA, "worker_id": self.worker_id,
                "idle_since": self.idle_since.astimezone(timezone.utc).isoformat() if self.idle_since else None,
                "heartbeat_at": self.heartbeat_at.astimezone(timezone.utc).isoformat(),
                "active_request": self.active_request,
                "active_lease_expires_at": (
                    self.active_lease_expires_at.astimezone(timezone.utc).isoformat()
                    if self.active_lease_expires_at else None
                )}

    @classmethod
    def from_record(cls, record: object) -> "WorkerState":
        if not isinstance(record, dict) or record.get("schema") != WORKER_SCHEMA:
            raise SpotRecordError("worker state record has an invalid schema")
        idle = record.get("idle_since")
        expires = record.get("active_lease_expires_at")
        return cls(_text(record.get("worker_id"), "worker_id"),
                   _timestamp(idle, "idle_since") if idle is not None else None,
                   _timestamp(record.get("heartbeat_at"), "heartbeat_at"),
                   record.get("active_request"),
                   _timestamp(expires, "active_lease_expires_at")
                   if expires is not None else None)


@dataclass(frozen=True)
class SpotResult:
    request_key: str
    status: str
    exit_code: int | None
    output: str
    completed_at: datetime
    output_uri: str | None = None

    def __post_init__(self):
        _request_key(self.request_key)
        if self.status not in {"passed", "failed", "infrastructure"}:
            raise SpotRecordError("result status must be terminal")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise SpotRecordError("exit_code must be an integer or null")
        if not isinstance(self.output, str) or self.completed_at.tzinfo is None:
            raise SpotRecordError("result is malformed")
        if len(self.output) > TERMINAL_OUTPUT_LIMIT:
            raise SpotRecordError("terminal output exceeds its bounded preview limit")
        if self.output_uri is not None and (not isinstance(self.output_uri, str) or not self.output_uri):
            raise SpotRecordError("output_uri must be a non-empty string or null")
        if self.status == "passed" and self.exit_code != 0:
            raise SpotRecordError("passed results require exit_code 0")
        if self.status == "failed" and (self.exit_code is None or self.exit_code == 0):
            raise SpotRecordError("failed results require a nonzero exit_code")
        if self.status == "infrastructure" and self.exit_code is not None:
            raise SpotRecordError("infrastructure results require a null exit_code")

    def to_record(self) -> dict:
        return {
            "schema": RESULT_SCHEMA,
            "request_key": self.request_key,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "output_uri": self.output_uri,
            "completed_at": self.completed_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_record(cls, record: object) -> "SpotResult":
        if not isinstance(record, dict) or record.get("schema") != RESULT_SCHEMA:
            raise SpotRecordError("result record has an invalid schema")
        return cls(
            _request_key(record.get("request_key")),
            _text(record.get("status"), "status"),
            record.get("exit_code"),
            record.get("output"),
            _timestamp(record.get("completed_at"), "completed_at"),
            record.get("output_uri"),
        )


@dataclass(frozen=True)
class _Stored:
    value: dict
    generation: int


class InMemorySpotQueueStore:
    """Test adapter with the same generation semantics as GCS preconditions."""

    def __init__(self):
        self.records: dict[tuple[str, str], _Stored] = {}
        self.messages: list[str] = []
        self.outputs: dict[str, str] = {}
        self.worker_states: dict[str, WorkerState] = {}
        self._lock = threading.RLock()

    def seed(self, namespace: str, request_key: str, value: dict) -> None:
        key = (namespace, _request_key(request_key))
        with self._lock:
            prior = self.records.get(key)
            self.records[key] = _Stored(
                dict(value), 1 if prior is None else prior.generation + 1
            )

    def _read(self, namespace: str, request_key: str) -> _Stored | None:
        with self._lock:
            return self.records.get((namespace, _request_key(request_key)))

    def _create(self, namespace: str, request_key: str, value: dict) -> bool:
        key = (namespace, _request_key(request_key))
        with self._lock:
            if key in self.records:
                return False
            self.records[key] = _Stored(dict(value), 1)
            return True

    def _replace(self, namespace: str, request_key: str, value: dict,
                 generation: int) -> bool:
        key = (namespace, _request_key(request_key))
        with self._lock:
            stored = self.records.get(key)
            if stored is None or stored.generation != generation:
                return False
            self.records[key] = _Stored(dict(value), generation + 1)
            return True

    def create_request(self, request: SpotRequest) -> bool:
        stored = self._read("requests", request.request_key)
        if stored is not None:
            existing = SpotRequest.from_record(stored.value)
            _bound(existing.request_key, request.request_key, "request")
            return existing.stable_identity == request.stable_identity
        return self._create("requests", request.request_key, request.to_record())

    def read_request(self, request_key: str) -> SpotRequest | None:
        stored = self._read("requests", request_key)
        if stored is None:
            return None
        request = SpotRequest.from_record(stored.value)
        _bound(request.request_key, request_key, "request")
        return request

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> Lease | None:
        _request_key(request_key)
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if self.read_request(request_key) is None:
            return None
        stored = self._read("states", request_key)
        if stored is not None:
            if stored.value.get("schema") in {RESULT_SCHEMA, CANCEL_SCHEMA}:
                return None
            lease = Lease.from_record(stored.value, stored.generation)
            _bound(lease.request_key, request_key, "lease")
            if lease.expires_at > now:
                return None
            expected_generation = stored.generation
        else:
            expected_generation = 0
        candidate = Lease(
            request_key, worker_id, now,
            now + timedelta(seconds=lease_seconds), expected_generation + 1,
        )
        wrote = (self._create("states", request_key, candidate.to_record())
                 if expected_generation == 0 else
                 self._replace("states", request_key, candidate.to_record(), expected_generation))
        return candidate if wrote else None

    def renew(self, lease: Lease, now: datetime, lease_seconds: int) -> Lease | None:
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if now >= lease.expires_at:
            return None
        stored = self._read("states", lease.request_key)
        if stored is None or stored.generation != lease.generation or \
                stored.value.get("schema") != LEASE_SCHEMA:
            return None
        current = Lease.from_record(stored.value, stored.generation)
        if now >= current.expires_at:
            return None
        candidate = Lease(lease.request_key, lease.worker_id, now,
                          now + timedelta(seconds=lease_seconds), stored.generation + 1)
        return candidate if self._replace("states", lease.request_key, candidate.to_record(),
                                         stored.generation) else None

    def publish_result(self, result: SpotResult, lease: Lease) -> bool:
        stored = self._read("states", result.request_key)
        if stored is None:
            return False
        schema = stored.value.get("schema")
        if schema == RESULT_SCHEMA:
            existing = SpotResult.from_record(stored.value)
            _bound(existing.request_key, result.request_key, "result")
            return existing == result
        if schema == CANCEL_SCHEMA or stored.generation != lease.generation:
            return False
        current = Lease.from_record(stored.value, stored.generation)
        _bound(current.request_key, result.request_key, "lease")
        if current.worker_id != lease.worker_id or result.completed_at >= current.expires_at:
            return False
        return self._replace("states", result.request_key, result.to_record(), stored.generation)

    def read_result(self, request_key: str) -> SpotResult | None:
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") in {LEASE_SCHEMA, CANCEL_SCHEMA}:
            return None
        result = SpotResult.from_record(stored.value)
        _bound(result.request_key, request_key, "result")
        return result

    def cancel_request(self, request_key: str, reason: str, now: datetime) -> bool:
        _request_key(request_key)
        reason = _text(reason, "reason")
        if now.tzinfo is None:
            raise ValueError("cancellation time must be timezone-aware")
        if self.read_request(request_key) is None:
            return False
        for _attempt in range(8):
            stored = self._read("states", request_key)
            if stored is None:
                cancellation = Cancellation(request_key, reason, now, None, None)
                if self._create("states", request_key, cancellation.to_record()):
                    return True
                continue
            schema = stored.value.get("schema")
            if schema == RESULT_SCHEMA:
                result = SpotResult.from_record(stored.value)
                _bound(result.request_key, request_key, "result")
                return False
            if schema == CANCEL_SCHEMA:
                cancellation = Cancellation.from_record(stored.value)
                _bound(cancellation.request_key, request_key, "cancellation")
                return cancellation.reason == reason
            lease = Lease.from_record(stored.value, stored.generation)
            _bound(lease.request_key, request_key, "lease")
            active = lease.expires_at > now
            cancellation = Cancellation(
                request_key, reason, now,
                lease.worker_id if active else None,
                lease.expires_at if active else None,
            )
            if self._replace("states", request_key, cancellation.to_record(), stored.generation):
                return True
        return False

    def is_cancelled(self, request_key: str) -> bool:
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") != CANCEL_SCHEMA:
            return False
        cancellation = Cancellation.from_record(stored.value)
        _bound(cancellation.request_key, request_key, "cancellation")
        return True

    def cancellation_quiesced(self, request_key: str, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("quiescence time must be timezone-aware")
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") != CANCEL_SCHEMA:
            return False
        cancellation = Cancellation.from_record(stored.value)
        _bound(cancellation.request_key, request_key, "cancellation")
        if cancellation.lease_worker_id is None or now >= cancellation.lease_expires_at:
            return True
        with self._lock:
            state = self.worker_states.get(cancellation.lease_worker_id)
        return bool(
            state and state.heartbeat_at >= cancellation.cancelled_at
            and state.active_request != request_key
        )

    def wait_cancellation_quiesced(self, request_key: str,
                                   timeout_seconds: int) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.cancellation_quiesced(request_key, datetime.now(timezone.utc)):
                return True
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return self.cancellation_quiesced(request_key, datetime.now(timezone.utc))

    def heartbeat_worker(self, worker_id: str, active_request: str | None,
                         now: datetime,
                         active_lease_expires_at: datetime | None = None) -> WorkerState:
        _text(worker_id, "worker_id")
        if now.tzinfo is None:
            raise ValueError("worker heartbeat must be timezone-aware")
        if active_request is not None:
            _request_key(active_request)
        with self._lock:
            prior = self.worker_states.get(worker_id)
            idle_since = None if active_request else (
                prior.idle_since if prior and prior.active_request is None else now)
            expiry = active_lease_expires_at
            if expiry is None and prior and prior.active_request == active_request:
                expiry = prior.active_lease_expires_at
            state = WorkerState(worker_id, idle_since, now, active_request, expiry)
            self.worker_states[worker_id] = state
        return state

    def list_worker_states(self) -> list[WorkerState]:
        with self._lock:
            return list(self.worker_states.values())

    def release(self, lease: Lease, now: datetime) -> bool:
        stored = self._read("states", lease.request_key)
        if stored is None or stored.generation != lease.generation or \
                stored.value.get("schema") != LEASE_SCHEMA:
            return False
        released = Lease(lease.request_key, lease.worker_id, now - timedelta(seconds=1), now,
                         stored.generation + 1)
        return self._replace("states", lease.request_key, released.to_record(), stored.generation)

    def publish_message(self, request_key: str) -> bool:
        self.messages.append(_request_key(request_key))
        return True

    def store_output(self, request_key: str, output: str) -> str:
        self.outputs[_request_key(request_key)] = output
        return f"memory://spot/v1/outputs/{request_key}.log"

    def wait_result(self, request_key: str, timeout_seconds: int) -> SpotResult | None:
        return self.read_result(request_key)


class GcloudSpotQueueStore:
    """Authenticated GCS records plus Pub/Sub request-key notifications."""

    def __init__(self, base: list[str], bucket: str, topic: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", bucket):
            raise ValueError("invalid queue bucket")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,254}", topic):
            raise ValueError("invalid Pub/Sub topic")
        self.base, self.bucket, self.topic = list(base), bucket, topic

    def _object(self, namespace: str, request_key: str) -> str:
        return f"spot/v1/{namespace}/{_request_key(request_key)}.json"

    def _uri(self, namespace: str, request_key: str) -> str:
        return f"gs://{self.bucket}/{self._object(namespace, request_key)}"

    def store_output(self, request_key: str, output: str) -> str:
        uri = f"gs://{self.bucket}/spot/v1/outputs/{_request_key(request_key)}.log"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as payload:
            payload.write(output)
            payload.flush()
            result = self._run(["storage", "cp", payload.name, uri])
        if result.returncode:
            raise SpotQueueUnavailable("raw Spot output could not be stored")
        return uri

    @staticmethod
    def _worker_id(worker_id: str) -> str:
        value = _text(worker_id, "worker_id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise SpotRecordError("worker_id contains unsafe storage characters")
        return value

    def _worker_uri(self, worker_id: str) -> str:
        return f"gs://{self.bucket}/spot/v1/workers/{self._worker_id(worker_id)}.json"

    def _worker_index_uri(self) -> str:
        return f"gs://{self.bucket}/spot/v1/workers/index/current.json"

    @staticmethod
    def _message(result: subprocess.CompletedProcess) -> str:
        return "\n".join(str(value) for value in (result.stdout, result.stderr) if value).lower()

    @classmethod
    def _missing(cls, result: subprocess.CompletedProcess) -> bool:
        for line in cls._message(result).splitlines():
            redacted = re.sub(r"gs://\S+", "", line)
            if "no urls matched" in redacted:
                return True
            if re.search(
                r"\bhttperror\s+404\b|\bstatus\s*=\s*404\b|"
                r"(?:^|[):])\s*404\s+not\s+found\b",
                redacted,
            ):
                return True
        return False

    @classmethod
    def _precondition(cls, result: subprocess.CompletedProcess) -> bool:
        message = cls._message(result)
        return "412" in message or "conditionnotmet" in message or "precondition failed" in message

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.base + args, capture_output=True, text=True, timeout=60)
        except Exception as exc:
            raise SpotQueueUnavailable("queue command could not run") from exc

    def _read(self, namespace: str, request_key: str) -> _Stored | None:
        uri = self._uri(namespace, request_key)
        described = self._run(["storage", "objects", "describe", uri, "--format=json"])
        if described.returncode != 0:
            if self._missing(described):
                return None
            raise SpotQueueUnavailable("queue object could not be described")
        try:
            generation = int(json.loads(described.stdout)["generation"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SpotQueueUnavailable("queue metadata was malformed") from exc
        fetched = self._run(["storage", "cat", f"{uri}#{generation}"])
        if fetched.returncode != 0:
            if self._missing(fetched):
                return None
            raise SpotQueueUnavailable("queue object could not be read")
        try:
            value = json.loads(fetched.stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SpotQueueUnavailable("queue JSON was malformed") from exc
        if not isinstance(value, dict):
            raise SpotRecordError("queue JSON must be an object")
        return _Stored(value, generation)

    def _write(self, namespace: str, request_key: str, value: dict,
               generation: int) -> bool:
        uri = self._uri(namespace, request_key)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as payload:
            json.dump(value, payload, sort_keys=True, separators=(",", ":"))
            payload.flush()
            result = self._run([
                "storage", "cp", payload.name, uri,
                f"--if-generation-match={generation}",
            ])
        if result.returncode == 0:
            return True
        if self._precondition(result):
            return False
        raise SpotQueueUnavailable("queue object could not be written")

    def _read_worker_index(self) -> _Stored | None:
        uri = self._worker_index_uri()
        described = self._run(["storage", "objects", "describe", uri, "--format=json"])
        if described.returncode != 0:
            if self._missing(described):
                return None
            raise SpotQueueUnavailable("worker index could not be described")
        try:
            generation = int(json.loads(described.stdout)["generation"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SpotQueueUnavailable("worker index metadata was malformed") from exc
        fetched = self._run(["storage", "cat", f"{uri}#{generation}"])
        if fetched.returncode != 0:
            if self._missing(fetched):
                return None
            raise SpotQueueUnavailable("worker index could not be read")
        try:
            value = json.loads(fetched.stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SpotQueueUnavailable("worker index JSON was malformed") from exc
        if not isinstance(value, dict) or value.get("schema") != WORKER_INDEX_SCHEMA \
                or not isinstance(value.get("workers"), dict):
            raise SpotRecordError("worker index record has an invalid schema")
        return _Stored(value, generation)

    def _write_worker_index(self, value: dict, generation: int) -> bool:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as payload:
            json.dump(value, payload, sort_keys=True, separators=(",", ":"))
            payload.flush()
            result = self._run([
                "storage", "cp", payload.name, self._worker_index_uri(),
                f"--if-generation-match={generation}",
            ])
        if result.returncode == 0:
            return True
        if self._precondition(result):
            return False
        raise SpotQueueUnavailable("worker index could not be written")

    def _update_worker_index(self, state: WorkerState, now: datetime) -> None:
        """CAS-merge one worker into a bounded, controller-readable snapshot."""
        for _attempt in range(8):
            stored = self._read_worker_index()
            states: dict[str, WorkerState] = {}
            if stored is not None:
                for worker_id, record in stored.value["workers"].items():
                    parsed = WorkerState.from_record(record)
                    if parsed.worker_id != worker_id:
                        raise SpotRecordError("worker index key does not match worker_id")
                    states[worker_id] = parsed
            states[state.worker_id] = state

            active, idle = {}, []
            legacy_cutoff = now - timedelta(seconds=WORKER_INDEX_LEGACY_ACTIVE_SECONDS)
            for worker_id, candidate in states.items():
                if candidate.active_request is not None:
                    if candidate.active_lease_expires_at is not None:
                        if candidate.active_lease_expires_at > now:
                            active[worker_id] = candidate
                    elif candidate.heartbeat_at >= legacy_cutoff:
                        active[worker_id] = candidate
                else:
                    idle.append(candidate)
            idle.sort(key=lambda candidate: candidate.heartbeat_at, reverse=True)
            bounded = {**active, **{
                candidate.worker_id: candidate
                for candidate in idle[:WORKER_INDEX_IDLE_LIMIT]
            }}
            record = {
                "schema": WORKER_INDEX_SCHEMA,
                "workers": {
                    worker_id: candidate.to_record()
                    for worker_id, candidate in sorted(bounded.items())
                },
            }
            generation = stored.generation if stored is not None else 0
            if self._write_worker_index(record, generation):
                return
        raise SpotQueueUnavailable("worker index could not win a stable generation")

    def create_request(self, request: SpotRequest) -> bool:
        existing = self.read_request(request.request_key)
        if existing is not None:
            return existing.stable_identity == request.stable_identity
        if self._write("requests", request.request_key, request.to_record(), 0):
            return True
        # A create-only precondition failure can be a concurrent identical
        # publisher. Re-read rather than turning an at-least-once delivery into
        # an unnecessary local full-suite fallback.
        existing = self.read_request(request.request_key)
        return existing is not None and existing.stable_identity == request.stable_identity

    def read_request(self, request_key: str) -> SpotRequest | None:
        stored = self._read("requests", request_key)
        if stored is None:
            return None
        request = SpotRequest.from_record(stored.value)
        _bound(request.request_key, request_key, "request")
        return request

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> Lease | None:
        _request_key(request_key)
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if self.read_request(request_key) is None:
            return None
        stored = self._read("states", request_key)
        if stored is not None:
            if stored.value.get("schema") in {RESULT_SCHEMA, CANCEL_SCHEMA}:
                return None
            lease = Lease.from_record(stored.value, stored.generation)
            _bound(lease.request_key, request_key, "lease")
            if lease.expires_at > now:
                return None
            generation = stored.generation
        else:
            generation = 0
        candidate = Lease(request_key, worker_id, now, now + timedelta(seconds=lease_seconds),
                          generation + 1)
        if not self._write("states", request_key, candidate.to_record(), generation):
            return None
        persisted = self._read("states", request_key)
        if persisted is None:
            raise SpotQueueUnavailable("lease disappeared after creation")
        if persisted.value.get("schema") != LEASE_SCHEMA:
            return None
        lease = Lease.from_record(persisted.value, persisted.generation)
        _bound(lease.request_key, request_key, "lease")
        return lease if lease.stable_identity == candidate.stable_identity else None

    def renew(self, lease: Lease, now: datetime, lease_seconds: int) -> Lease | None:
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if now >= lease.expires_at:
            return None
        stored = self._read("states", lease.request_key)
        if stored is None or stored.generation != lease.generation or \
                stored.value.get("schema") != LEASE_SCHEMA:
            return None
        current = Lease.from_record(stored.value, stored.generation)
        if now >= current.expires_at:
            return None
        candidate = Lease(lease.request_key, lease.worker_id, now,
                          now + timedelta(seconds=lease_seconds), stored.generation + 1)
        if not self._write("states", lease.request_key, candidate.to_record(), stored.generation):
            return None
        persisted = self._read("states", lease.request_key)
        if persisted is None:
            raise SpotQueueUnavailable("lease disappeared after renewal")
        if persisted.value.get("schema") != LEASE_SCHEMA:
            return None
        renewed = Lease.from_record(persisted.value, persisted.generation)
        _bound(renewed.request_key, lease.request_key, "lease")
        return renewed if renewed.stable_identity == candidate.stable_identity else None

    def publish_result(self, result: SpotResult, lease: Lease) -> bool:
        stored = self._read("states", result.request_key)
        if stored is None:
            return False
        schema = stored.value.get("schema")
        if schema == RESULT_SCHEMA:
            existing = SpotResult.from_record(stored.value)
            _bound(existing.request_key, result.request_key, "result")
            return existing == result
        if schema == CANCEL_SCHEMA or stored.generation != lease.generation:
            return False
        current = Lease.from_record(stored.value, stored.generation)
        _bound(current.request_key, result.request_key, "lease")
        if current.worker_id != lease.worker_id or result.completed_at >= current.expires_at:
            return False
        if self._write("states", result.request_key, result.to_record(), stored.generation):
            return True
        persisted = self._read("states", result.request_key)
        if persisted is None or persisted.value.get("schema") != RESULT_SCHEMA:
            return False
        existing = SpotResult.from_record(persisted.value)
        _bound(existing.request_key, result.request_key, "result")
        return existing == result

    def read_result(self, request_key: str) -> SpotResult | None:
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") in {LEASE_SCHEMA, CANCEL_SCHEMA}:
            return None
        result = SpotResult.from_record(stored.value)
        _bound(result.request_key, request_key, "result")
        return result

    def cancel_request(self, request_key: str, reason: str, now: datetime) -> bool:
        _request_key(request_key)
        reason = _text(reason, "reason")
        if now.tzinfo is None:
            raise ValueError("cancellation time must be timezone-aware")
        if self.read_request(request_key) is None:
            return False
        for _attempt in range(8):
            stored = self._read("states", request_key)
            if stored is None:
                cancellation = Cancellation(request_key, reason, now, None, None)
                if self._write("states", request_key, cancellation.to_record(), 0):
                    return True
                continue
            schema = stored.value.get("schema")
            if schema == RESULT_SCHEMA:
                result = SpotResult.from_record(stored.value)
                _bound(result.request_key, request_key, "result")
                return False
            if schema == CANCEL_SCHEMA:
                cancellation = Cancellation.from_record(stored.value)
                _bound(cancellation.request_key, request_key, "cancellation")
                return cancellation.reason == reason
            lease = Lease.from_record(stored.value, stored.generation)
            _bound(lease.request_key, request_key, "lease")
            active = lease.expires_at > now
            cancellation = Cancellation(
                request_key, reason, now,
                lease.worker_id if active else None,
                lease.expires_at if active else None,
            )
            if self._write("states", request_key, cancellation.to_record(), stored.generation):
                return True
        raise SpotQueueUnavailable("cancellation could not win a stable generation")

    def is_cancelled(self, request_key: str) -> bool:
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") != CANCEL_SCHEMA:
            return False
        cancellation = Cancellation.from_record(stored.value)
        _bound(cancellation.request_key, request_key, "cancellation")
        return True

    def cancellation_quiesced(self, request_key: str, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("quiescence time must be timezone-aware")
        stored = self._read("states", request_key)
        if stored is None or stored.value.get("schema") != CANCEL_SCHEMA:
            return False
        cancellation = Cancellation.from_record(stored.value)
        _bound(cancellation.request_key, request_key, "cancellation")
        if cancellation.lease_worker_id is None or now >= cancellation.lease_expires_at:
            return True
        state = self.read_worker_state(cancellation.lease_worker_id)
        return bool(
            state and state.heartbeat_at >= cancellation.cancelled_at
            and state.active_request != request_key
        )

    def wait_cancellation_quiesced(self, request_key: str,
                                   timeout_seconds: int) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.cancellation_quiesced(request_key, datetime.now(timezone.utc)):
                return True
            time.sleep(min(2, max(0.1, deadline - time.monotonic())))
        return self.cancellation_quiesced(request_key, datetime.now(timezone.utc))

    def heartbeat_worker(self, worker_id: str, active_request: str | None,
                         now: datetime,
                         active_lease_expires_at: datetime | None = None) -> WorkerState:
        if now.tzinfo is None:
            raise ValueError("worker heartbeat must be timezone-aware")
        if active_request is not None:
            _request_key(active_request)
        prior = self.read_worker_state(worker_id)
        idle_since = None if active_request else (
            prior.idle_since if prior and prior.active_request is None else now)
        expiry = active_lease_expires_at
        if expiry is None and prior and prior.active_request == active_request:
            expiry = prior.active_lease_expires_at
        state = WorkerState(worker_id, idle_since, now, active_request, expiry)
        # Publish the shared floor first. Claim/renew callers do this before
        # their lease CAS, so a partial failure can only overcount until expiry.
        self._update_worker_index(state, now)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as payload:
            json.dump(state.to_record(), payload, sort_keys=True, separators=(",", ":"))
            payload.flush()
            result = self._run(["storage", "cp", payload.name, self._worker_uri(worker_id)])
        if result.returncode:
            raise SpotQueueUnavailable("worker state could not be written")
        return state

    def read_worker_state(self, worker_id: str) -> WorkerState | None:
        result = self._run(["storage", "cat", self._worker_uri(worker_id)])
        if result.returncode:
            if self._missing(result):
                return None
            raise SpotQueueUnavailable("worker state could not be read")
        return WorkerState.from_record(json.loads(result.stdout))

    def release(self, lease: Lease, now: datetime) -> bool:
        stored = self._read("states", lease.request_key)
        if stored is None or stored.generation != lease.generation or \
                stored.value.get("schema") != LEASE_SCHEMA:
            return False
        released = Lease(lease.request_key, lease.worker_id, now - timedelta(seconds=1), now,
                         stored.generation + 1)
        return self._write("states", lease.request_key, released.to_record(), stored.generation)

    def publish_message(self, request_key: str) -> bool:
        result = self._run(["pubsub", "topics", "publish", self.topic,
                            "--message", _request_key(request_key)])
        if result.returncode != 0:
            raise SpotQueueUnavailable("queue message could not be published")
        return True

    def wait_result(self, request_key: str, timeout_seconds: int) -> SpotResult | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self.read_result(request_key)
            if result is not None:
                return result
            time.sleep(min(5, max(0.1, deadline - time.monotonic())))
        return self.read_result(request_key)
