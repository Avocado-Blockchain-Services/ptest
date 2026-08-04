"""Durable, generation-safe GCS records for the opt-in Spot test queue."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


REQUEST_SCHEMA = "ptest-spot-request-v1"
LEASE_SCHEMA = "ptest-spot-lease-v1"
RESULT_SCHEMA = "ptest-spot-result-v1"


class SpotQueueUnavailable(RuntimeError):
    """The durable queue cannot be safely read or updated."""


class SpotRecordError(ValueError):
    """A durable queue record is malformed or fails its schema contract."""


class SpotQueueStore(Protocol):
    """Durable record operations shared by the worker and ptest client."""

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> "Lease | None": ...

    def publish_result(self, result: "SpotResult") -> bool: ...


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


@dataclass(frozen=True)
class SpotRequest:
    request_key: str
    command: str
    source_uri: str
    created_at: datetime

    def __post_init__(self):
        _request_key(self.request_key)
        _text(self.command, "command")
        if not isinstance(self.source_uri, str) or not self.source_uri.startswith("gs://"):
            raise SpotRecordError("source_uri must be a gs:// URI")
        if self.created_at.tzinfo is None:
            raise SpotRecordError("created_at must include a timezone")

    def to_record(self) -> dict:
        return {
            "schema": REQUEST_SCHEMA,
            "request_key": self.request_key,
            "command": self.command,
            "source_uri": self.source_uri,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_record(cls, record: object) -> "SpotRequest":
        if not isinstance(record, dict) or record.get("schema") != REQUEST_SCHEMA:
            raise SpotRecordError("request record has an invalid schema")
        return cls(
            _request_key(record.get("request_key")),
            _text(record.get("command"), "command"),
            _text(record.get("source_uri"), "source_uri"),
            _timestamp(record.get("created_at"), "created_at"),
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
class SpotResult:
    request_key: str
    status: str
    exit_code: int | None
    output: str
    completed_at: datetime

    def __post_init__(self):
        _request_key(self.request_key)
        if self.status not in {"passed", "failed", "infrastructure"}:
            raise SpotRecordError("result status must be terminal")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise SpotRecordError("exit_code must be an integer or null")
        if not isinstance(self.output, str) or self.completed_at.tzinfo is None:
            raise SpotRecordError("result is malformed")

    def to_record(self) -> dict:
        return {
            "schema": RESULT_SCHEMA,
            "request_key": self.request_key,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
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

    def seed(self, namespace: str, request_key: str, value: dict) -> None:
        key = (namespace, _request_key(request_key))
        prior = self.records.get(key)
        self.records[key] = _Stored(dict(value), 1 if prior is None else prior.generation + 1)

    def _read(self, namespace: str, request_key: str) -> _Stored | None:
        return self.records.get((namespace, _request_key(request_key)))

    def _create(self, namespace: str, request_key: str, value: dict) -> bool:
        key = (namespace, _request_key(request_key))
        if key in self.records:
            return False
        self.records[key] = _Stored(dict(value), 1)
        return True

    def _replace(self, namespace: str, request_key: str, value: dict,
                 generation: int) -> bool:
        key = (namespace, _request_key(request_key))
        stored = self.records.get(key)
        if stored is None or stored.generation != generation:
            return False
        self.records[key] = _Stored(dict(value), generation + 1)
        return True

    def create_request(self, request: SpotRequest) -> bool:
        stored = self._read("requests", request.request_key)
        if stored is not None:
            return SpotRequest.from_record(stored.value) == request
        return self._create("requests", request.request_key, request.to_record())

    def read_request(self, request_key: str) -> SpotRequest | None:
        stored = self._read("requests", request_key)
        return None if stored is None else SpotRequest.from_record(stored.value)

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> Lease | None:
        _request_key(request_key)
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if self.read_request(request_key) is None:
            return None
        stored = self._read("leases", request_key)
        if stored is not None:
            lease = Lease.from_record(stored.value, stored.generation)
            if lease.expires_at > now:
                return None
            expected_generation = stored.generation
        else:
            expected_generation = 0
        candidate = Lease(
            request_key, worker_id, now,
            now + timedelta(seconds=lease_seconds), expected_generation + 1,
        )
        wrote = (self._create("leases", request_key, candidate.to_record())
                 if expected_generation == 0 else
                 self._replace("leases", request_key, candidate.to_record(), expected_generation))
        return candidate if wrote else None

    def publish_result(self, result: SpotResult) -> bool:
        stored = self._read("results", result.request_key)
        if stored is None:
            return self._create("results", result.request_key, result.to_record())
        return SpotResult.from_record(stored.value) == result

    def read_result(self, request_key: str) -> SpotResult | None:
        stored = self._read("results", request_key)
        return None if stored is None else SpotResult.from_record(stored.value)

    def publish_message(self, request_key: str) -> bool:
        self.messages.append(_request_key(request_key))
        return True

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

    def create_request(self, request: SpotRequest) -> bool:
        existing = self.read_request(request.request_key)
        if existing is not None:
            return existing == request
        return self._write("requests", request.request_key, request.to_record(), 0)

    def read_request(self, request_key: str) -> SpotRequest | None:
        stored = self._read("requests", request_key)
        return None if stored is None else SpotRequest.from_record(stored.value)

    def claim(self, request_key: str, worker_id: str, now: datetime,
              lease_seconds: int) -> Lease | None:
        _request_key(request_key)
        if lease_seconds <= 0 or now.tzinfo is None:
            raise ValueError("lease_seconds and now must be positive and timezone-aware")
        if self.read_request(request_key) is None:
            return None
        stored = self._read("leases", request_key)
        if stored is not None:
            lease = Lease.from_record(stored.value, stored.generation)
            if lease.expires_at > now:
                return None
            generation = stored.generation
        else:
            generation = 0
        candidate = Lease(request_key, worker_id, now, now + timedelta(seconds=lease_seconds),
                          generation + 1)
        if not self._write("leases", request_key, candidate.to_record(), generation):
            return None
        persisted = self._read("leases", request_key)
        if persisted is None:
            raise SpotQueueUnavailable("lease disappeared after creation")
        return Lease.from_record(persisted.value, persisted.generation)

    def publish_result(self, result: SpotResult) -> bool:
        stored = self._read("results", result.request_key)
        if stored is not None:
            return SpotResult.from_record(stored.value) == result
        if self._write("results", result.request_key, result.to_record(), 0):
            return True
        stored = self._read("results", result.request_key)
        return stored is not None and SpotResult.from_record(stored.value) == result

    def read_result(self, request_key: str) -> SpotResult | None:
        stored = self._read("results", request_key)
        return None if stored is None else SpotResult.from_record(stored.value)

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
