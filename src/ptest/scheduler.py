"""Atomic, local-only admission and conservative lease recovery.

The scheduler deliberately has a very small public surface.  Domain resolution,
process observation and the public records belong to the neighbouring frozen
modules; this module only coordinates an explicitly supplied ``DomainPaths``.
There is no legacy-state importer, background worker, remote backend or process
launcher here.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import json
import os
import pwd
import secrets
import sqlite3
import stat
import sys
import time
import tomllib
import warnings
from pathlib import Path
from typing import NamedTuple

import psutil

from . import files, platform, storage
from .contracts import (
    AdmissionRequest,
    AdmissionState,
    DomainPaths,
    EffectiveLimits,
    Finalization,
    Grant,
    LeaseState,
    LeaseView,
    ProcessIdentity,
    Problem,
    QuiescenceProof,
    Reason,
    Ticket,
    DEFAULT_QUEUE_TIMEOUT_S,
    MAX_QUEUE_TIMEOUT_S,
    MAX_PENDING_JOBS,
    MAX_TERMINAL_SUMMARIES,
    SCHEMA_VERSION,
    PROTOCOL_VERSION,
)

_PHASE = "scheduler"
# The one text for an expired queue deadline, whoever notices it first.
QUEUE_DEADLINE_MESSAGE = "admission queue deadline expired"
_DB_MAX_BYTES = 16 * 1024 * 1024
_MARKER_MAX_BYTES = 65536
_MACHINE_MAX_BYTES = 65536
_MAX_OBSERVATIONS = 256
# Transient lock contention waits with bounded exponential backoff instead of
# surfacing as coordinator-unavailable. Budgets use the real clock (never the
# faked _now) so frozen-time tests cannot spin. Genuine errors (corruption,
# ownership violations, capacity) are not transient and return immediately.
_BEGIN_RETRY_DEADLINE_S = 30.0
_BEGIN_RETRY_BASE_S = 0.005
_BEGIN_RETRY_MAX_S = 0.2
_TRANSACTION_RETRY_DEADLINE_S = 30.0
_TRANSACTION_RETRY_BASE_S = 0.005
_TRANSACTION_RETRY_MAX_S = 0.5
_ANCESTRY_RETRY_DEADLINE_S = 2.0
_ANCESTRY_RETRY_BASE_S = 0.005
_ANCESTRY_RETRY_MAX_S = 0.1
_ACTIVE = frozenset({
    "GRANTED", "RUNNING", "DRAINING", "FINALIZING", "CANCELLING", "UNCERTAIN",
})
_TERMINAL = frozenset({"RELEASED", "CANCELLED"})
_SCHEMA_TABLES = frozenset({"domain", "jobs", "job_resources", "observations"})


def _fail(code: str, message: str, *, retryable: bool = False) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=retryable)


def is_transient_contention(exc: BaseException) -> bool:
    """True only for cause-verified lock contention (never a verdict).

    A plain ``coordinator-unavailable`` Problem is NOT enough: storage and
    transaction layers reuse that code for genuine failures (disk errors,
    failed commits) that must surface immediately. Only TransientContention
    — raised after is_transient_sqlite passed on the live exception —
    authorizes an automatic retry.
    """
    return isinstance(exc, storage.TransientContention)


def _transient_retry(fn):
    """Re-run one coordinator call while it reports transient contention.

    Each attempt holds no lock when it fails (the connection is closed on
    every error path), so retries cannot deadlock with the lock holder.
    Attempts are NOT re-runs of committed work: a sqlite-level failure
    always rolls back first, and CAS updates only commit once. Genuine
    failures keep their exact code and surface on the first attempt.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        deadline = time.monotonic() + _TRANSACTION_RETRY_DEADLINE_S
        delay = _TRANSACTION_RETRY_BASE_S
        while True:
            try:
                return fn(*args, **kwargs)
            except storage.TransientContention:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(_TRANSACTION_RETRY_MAX_S, delay * 2)
    return wrapper


def _unavailable_from_sqlite(exc: sqlite3.Error, message: str) -> None:
    """Map a transaction failure by cause: contention retries, damage stands."""
    if storage.is_transient_sqlite(exc):
        raise storage.TransientContention(message="coordinator is busy",
                                          phase=_PHASE)
    _fail("coordinator-unavailable", message, retryable=True)


def _now() -> float:
    return float(time.monotonic())


def _state_marker(domain: DomainPaths) -> Path:
    return Path(domain.root) / "domain.json"


def _expected_normal_paths() -> tuple[Path, Path, tuple[tuple[Path, str], ...]]:
    state = platform.configured_state_directory()
    if state is not None:
        return state, state, ()
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError, TypeError, AttributeError):
        _fail("state-unavailable", "account home cannot be resolved")
    if not home.is_absolute():
        _fail("unsafe-path", "account home is not absolute")
    if sys.platform.startswith("linux"):
        machine_parent = home / ".config" / "ptest"
        root_parent = home / ".local" / "state" / "ptest"
        steps = ((home, ".config"), (home / ".config", "ptest"),
                 (home, ".local"), (home / ".local", "state"),
                 (home / ".local" / "state", "ptest"))
    elif sys.platform == "darwin":
        machine_parent = home / "Library" / "Application Support" / "ptest"
        root_parent = machine_parent
        steps = ((home, "Library"), (home / "Library", "Application Support"),
                 (home / "Library" / "Application Support", "ptest"))
    else:
        _fail("unsupported-platform", "platform is outside the Linux/macOS scope")
    return machine_parent, root_parent, steps


def _validate_domain_argument(domain: DomainPaths) -> None:
    if not isinstance(domain, DomainPaths):
        raise TypeError("scheduler domain must be DomainPaths")
    root = Path(domain.root)
    if not root.is_absolute():
        _fail("unsafe-path", "scheduler domain root must be absolute")
    expected_marker = _state_marker(domain)
    if domain.fixture:
        if domain.domain_id is None:
            _fail("unsafe-path", "fixture domain has no fixture identity")
        expected = root / "fixture-domain.toml"
        if Path(domain.marker) != expected:
            _fail("unsafe-path", "fixture marker is outside the fixture domain")
        if Path(domain.ledger) != root / "coordinator.sqlite3":
            _fail("unsafe-path", "fixture ledger is outside the fixture domain")
    else:
        machine_parent, root_parent, _ = _expected_normal_paths()
        expected_root = root_parent / "coordination"
        if root != expected_root:
            _fail("unsafe-path", "normal scheduler domain is not canonical")
        if Path(domain.machine_config) != machine_parent / "machine.toml":
            _fail("unsafe-path", "machine config is not canonical")
        if Path(domain.marker) != expected_marker:
            _fail("unsafe-path", "normal domain marker is not canonical")
        if Path(domain.ledger) != root / "coordinator.sqlite3":
            _fail("unsafe-path", "normal ledger is not canonical")


def _prepare_root(domain: DomainPaths) -> None:
    _validate_domain_argument(domain)
    root = Path(domain.root)
    if domain.fixture:
        files.validate_private_dir(root)
        return

    machine_parent, root_parent, steps = _expected_normal_paths()
    if not steps:
        files.ensure_private_dir(root_parent.parent, root_parent.name)
        files.ensure_private_dir(root_parent, "coordination")
        files.validate_private_dir(root)
        return
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        stamp = os.lstat(home)
    except (KeyError, OSError, TypeError, AttributeError):
        _fail("state-unavailable", "account home cannot be inspected")
    if (not stat.S_ISDIR(stamp.st_mode) or stamp.st_uid != os.getuid()
            or stat.S_ISLNK(stamp.st_mode)):
        _fail("unsafe-path", "account home is not an owned directory")
    for parent, name in steps:
        files.ensure_shared_dir(parent, name)
    files.ensure_private_dir(root_parent, "coordination")
    # The helper above is intentionally the only creator.  Recheck the exact
    # paths after creation so a concurrent replacement cannot be followed.
    files.validate_private_dir(root)
    if machine_parent != Path(domain.machine_config).parent:
        _fail("unsafe-path", "machine config parent changed")


def prepare_state_directory(domain: DomainPaths) -> None:
    """Create validated private directories without admitting a test run."""
    _prepare_root(domain)


def _open_bootstrap(root: Path, *, create: bool = True):
    lock_path = root / "bootstrap.lock"
    try:
        files.validate_private_file(lock_path)
    except Problem as exc:
        if exc.code != "state-unavailable" or not create:
            raise
        try:
            files.create_exclusive(root, "bootstrap.lock", b"", private=True)
        except Problem as create_exc:
            if create_exc.code != "already-exists":
                raise
        files.validate_private_file(lock_path)
    try:
        before = os.lstat(lock_path)
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        _fail("coordinator-unavailable", "bootstrap lock cannot be opened", retryable=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        opened = os.fstat(fd)
        after = os.lstat(lock_path)
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or opened.st_nlink != 1):
            _fail("unsafe-path", "bootstrap lock identity changed")
    except Problem:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        _fail("coordinator-unavailable", "bootstrap lock cannot be acquired", retryable=True)
    return fd


@contextlib.contextmanager
def _bootstrap(root: Path, *, create: bool = True):
    fd = _open_bootstrap(root, create=create)
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _read_private(root: Path, name: str, limit: int) -> bytes:
    raw = files.read_regular(root, name, limit + 1)
    if len(raw) > limit:
        _fail("capacity-exceeded", f"private state file {name!r} exceeds its bound")
    return raw


def _machine_parent(domain: DomainPaths) -> Path:
    return Path(domain.machine_config).parent


def _check_int(value: object, *, lo: int, hi: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _fail("invalid-config", f"{label} is outside its allowed range")
    return value


def _parse_limits(data: object, *, fixture: bool) -> tuple[int, int, int | None]:
    if not isinstance(data, dict):
        _fail("invalid-config", "machine limits are not a table")
    allowed = {"max_slots", "max_jobs", "memory_mb"}
    if set(data) - allowed:
        _fail("invalid-config", "machine limits contain unknown fields")
    if "max_slots" not in data or "max_jobs" not in data:
        _fail("invalid-config", "machine limits require paired slots and jobs")
    slots = _check_int(data["max_slots"], lo=1, hi=4 if fixture else 64, label="max_slots")
    jobs = _check_int(data["max_jobs"], lo=1, hi=4 if fixture else 64, label="max_jobs")
    if jobs > slots:
        _fail("invalid-config", "max_jobs exceeds max_slots")
    memory = data.get("memory_mb")
    if memory is not None:
        memory = _check_int(memory, lo=64, hi=1048576, label="memory_mb")
    if fixture and memory is not None:
        _fail("invalid-config", "fixture limits do not declare memory_mb")
    return slots, jobs, memory


def _read_cpu_file(path: Path) -> str:
    # Kernel metadata only: no environment-selected paths or subprocesses.
    with path.open("r", encoding="ascii") as stream:
        data = stream.read(_MACHINE_MAX_BYTES + 1)
    if len(data) > _MACHINE_MAX_BYTES:
        raise ValueError("CPU metadata exceeds its bound")
    return data.strip()


def _cpuset_size(value: str) -> int:
    intervals = []
    for component in value.split(","):
        limits = component.split("-")
        if len(limits) not in (1, 2):
            raise ValueError("invalid CPU set")
        first, last = int(limits[0]), int(limits[-1])
        if first < 0 or last < first:
            raise ValueError("invalid CPU interval")
        intervals.append((first, last))
    total, end = 0, -1
    for first, last in sorted(intervals):
        total += max(0, last - max(first, end + 1) + 1)
        end = max(end, last)
    return total


def _cgroup_cpu_bounds() -> list[int]:
    memberships = _read_cpu_file(Path("/proc/self/cgroup")).splitlines()
    unified = [line[3:] for line in memberships if line.startswith("0::")]
    if len(unified) != 1:
        raise ValueError("cgroup-v2 membership unavailable")
    member = Path(unified[0])
    if not member.is_absolute() or ".." in member.parts:
        raise ValueError("unsupported cgroup membership")
    mounts = []
    for line in _read_cpu_file(Path("/proc/self/mountinfo")).splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if separator >= 6 and fields[separator + 1] == "cgroup2" and fields[4] == "/sys/fs/cgroup":
            mounts.append(Path(fields[3]))
    if len(mounts) != 1 or ".." in mounts[0].parts:
        raise ValueError("unsupported cgroup-v2 mount layout")
    root = Path("/sys/fs/cgroup")
    current = root / member.relative_to(mounts[0])
    if len(current.relative_to(root).parts) > _MAX_OBSERVATIONS:
        raise ValueError("cgroup ancestry exceeds its bound")
    bounds = []
    while True:
        try:
            cpu_max = _read_cpu_file(current / "cpu.max")
        except FileNotFoundError:
            pass
        else:
            quota, period = cpu_max.split()
            period = int(period)
            if period <= 0:
                raise ValueError("invalid CPU period")
            if quota != "max":
                quota = int(quota)
                if quota <= 0:
                    raise ValueError("invalid CPU quota")
                bounds.append(max(1, quota // period))
        try:
            cpuset = _read_cpu_file(current / "cpuset.cpus.effective")
        except FileNotFoundError:
            pass
        else:
            if cpuset:
                bounds.append(_cpuset_size(cpuset))
        if current == root:
            break
        current = current.parent
    return bounds


def _usable_cpus() -> int:
    try:
        count = os.cpu_count()
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("CPU count unavailable")
        bounds = [count]
        if sys.platform.startswith("linux"):
            bounds.append(len(os.sched_getaffinity(0)))
            bounds.extend(_cgroup_cpu_bounds())
        if min(bounds) < 1:
            raise ValueError("CPU bound unavailable")
        return min(bounds)
    except (AttributeError, OSError, ValueError, UnicodeError, IndexError):
        warnings.warn("CPU limits unavailable; using one usable CPU", RuntimeWarning, stacklevel=2)
        return 1


def _default_machine_config() -> bytes:
    available = _usable_cpus()
    slots = min(4, max(1, available // 2))
    jobs = min(2, slots)
    return f"max_slots = {slots}\nmax_jobs = {jobs}\n".encode("ascii")


def _load_config_limits(domain: DomainPaths, *, create_missing: bool) -> tuple[int, int, int | None]:
    if domain.fixture:
        files.validate_private_file(Path(domain.marker))
        raw = _read_private(Path(domain.root), "fixture-domain.toml", _MACHINE_MAX_BYTES)
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            _fail("invalid-config", "fixture marker is not valid TOML")
        if (set(data) != {
                "version", "fixture", "max_slots", "max_jobs", "uid",
                "directory_device", "directory_inode", "fixture_id", "workload",
        } or type(data.get("version")) is not int or data.get("version") != SCHEMA_VERSION
                or data.get("fixture") is not True
                or data.get("uid") != os.getuid()
                or data.get("workload") != "synthetic-or-miniature"):
            _fail("invalid-config", "fixture marker is incompatible")
        stamp = os.lstat(domain.root)
        if data.get("directory_device") != stamp.st_dev or data.get("directory_inode") != stamp.st_ino:
            _fail("unsafe-path", "fixture marker identity is stale")
        if data.get("fixture_id") != domain.domain_id:
            _fail("unsafe-path", "fixture marker identity does not match DomainPaths")
        return _parse_limits(
            {"max_slots": data["max_slots"], "max_jobs": data["max_jobs"]},
            fixture=True,
        )

    parent = _machine_parent(domain)
    path = Path(domain.machine_config)
    try:
        files.validate_private_file(path)
    except Problem as exc:
        if exc.code != "state-unavailable" or not create_missing:
            raise
        files.create_exclusive(parent, path.name, _default_machine_config(), private=True)
        files.validate_private_file(path)
    raw = _read_private(parent, path.name, _MACHINE_MAX_BYTES)
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        _fail("invalid-config", "machine.toml is not valid TOML")
    return _parse_limits(data, fixture=False)


def _marker_object(info: dict) -> dict:
    return {
        key: info[key] for key in ("domain_id", "ledger_device", "ledger_inode",
                                   "protocol_version", "schema_version")
    }


def _read_state_marker(domain: DomainPaths) -> dict:
    marker = _state_marker(domain)
    files.validate_private_file(marker)
    before = os.lstat(marker)
    try:
        value = json.loads(_read_private(Path(domain.root), marker.name, _MARKER_MAX_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        _fail("coordinator-corrupt", "domain marker is not valid JSON")
    after = os.lstat(marker)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        _fail("unsafe-path", "domain marker was replaced during validation")
    required = {"domain_id", "ledger_device", "ledger_inode", "protocol_version", "schema_version"}
    if not isinstance(value, dict) or set(value) != required:
        _fail("protocol-mismatch", "domain marker has an incompatible schema")
    if (not isinstance(value["domain_id"], str) or len(value["domain_id"]) != 32
            or any(c not in "0123456789abcdef" for c in value["domain_id"])):
        _fail("protocol-mismatch", "domain marker has an invalid identity")
    if (type(value["protocol_version"]) is not int or type(value["schema_version"]) is not int
            or value["protocol_version"] != PROTOCOL_VERSION or value["schema_version"] != SCHEMA_VERSION):
        _fail("protocol-mismatch", "domain marker version is unsupported")
    for key in ("ledger_device", "ledger_inode"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
            _fail("coordinator-corrupt", "domain marker ledger identity is invalid")
    return value


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.execute("""CREATE TABLE domain (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        domain_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        protocol_version INTEGER NOT NULL,
        ledger_device INTEGER NOT NULL,
        ledger_inode INTEGER NOT NULL,
        marker_device INTEGER,
        marker_inode INTEGER,
        boot_id TEXT NOT NULL,
        config_slots INTEGER NOT NULL,
        config_jobs INTEGER NOT NULL,
        config_memory INTEGER,
        config_generation INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE jobs (
        run_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL UNIQUE,
        state TEXT NOT NULL,
        checkout_id TEXT NOT NULL,
        requested_slots INTEGER NOT NULL,
        slots INTEGER NOT NULL,
        exclusive INTEGER NOT NULL,
        memory_estimate INTEGER,
        reserved_memory INTEGER,
        enqueue_time REAL NOT NULL,
        grant_time REAL,
        deadline REAL NOT NULL,
        phase TEXT NOT NULL,
        fixture INTEGER NOT NULL,
        owner_pid INTEGER NOT NULL,
        owner_birth REAL NOT NULL,
        owner_uid INTEGER NOT NULL,
        owner_pgid INTEGER NOT NULL,
        guard_pid INTEGER,
        guard_birth REAL,
        guard_uid INTEGER,
        guard_pgid INTEGER,
        nonce TEXT,
        generation INTEGER,
        reason_code TEXT,
        reason_message TEXT,
        final_status TEXT,
        final_exit_code INTEGER,
        final_committed INTEGER,
        final_source_valid INTEGER
    )""")
    conn.execute("""CREATE TABLE job_resources (
        run_id TEXT NOT NULL REFERENCES jobs(run_id) ON DELETE CASCADE,
        resource TEXT NOT NULL,
        PRIMARY KEY(run_id, resource)
    )""")
    conn.execute("""CREATE TABLE observations (
        run_id TEXT NOT NULL REFERENCES jobs(run_id) ON DELETE CASCADE,
        pid INTEGER NOT NULL,
        birth REAL,
        uid INTEGER,
        pgid INTEGER,
        uncertain INTEGER NOT NULL,
        PRIMARY KEY(run_id, pid)
    )""")


def _unavailable_or_corrupt(exc: sqlite3.Error, corrupt_message: str) -> None:
    """Lock contention is retryable; every other storage error is corruption."""
    if storage.is_transient_sqlite(exc):
        raise storage.TransientContention(message="coordinator is busy",
                                          phase=_PHASE)
    _fail("coordinator-corrupt", corrupt_message)


def _validate_schema(conn: sqlite3.Connection) -> None:
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error as exc:
        _unavailable_or_corrupt(exc, "coordinator schema cannot be read")
    if version != SCHEMA_VERSION or {row[0] for row in rows} != _SCHEMA_TABLES:
        _fail("protocol-mismatch", "coordinator schema is unsupported")
    expected_columns = {
        "domain": {"singleton", "domain_id", "schema_version", "protocol_version",
                    "ledger_device", "ledger_inode", "marker_device", "marker_inode", "boot_id", "config_slots",
                    "config_jobs", "config_memory", "config_generation"},
        "jobs": {"run_id", "sequence", "state", "checkout_id", "requested_slots",
                  "slots", "exclusive", "memory_estimate", "reserved_memory",
                  "enqueue_time", "grant_time", "deadline", "phase", "fixture",
                  "owner_pid", "owner_birth", "owner_uid", "owner_pgid", "guard_pid",
                  "guard_birth", "guard_uid", "guard_pgid", "nonce", "generation",
                  "reason_code", "reason_message", "final_status", "final_exit_code",
                  "final_committed", "final_source_valid"},
        "job_resources": {"run_id", "resource"},
        "observations": {"run_id", "pid", "birth", "uid", "pgid", "uncertain"},
    }
    for table in _SCHEMA_TABLES:
        try:
            columns = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
            if columns != expected_columns[table]:
                _fail("protocol-mismatch", "coordinator table schema is unsupported")
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # nosec B608 - table is closed constant
        except sqlite3.Error as exc:
            _unavailable_or_corrupt(exc, "coordinator schema is incomplete")


def _initial_state(domain: DomainPaths) -> None:
    root = Path(domain.root)
    ledger = Path(domain.ledger)
    limits = _load_config_limits(domain, create_missing=True)
    boot = _boot_identity()
    domain_id = domain.domain_id or secrets.token_hex(16)
    conn = storage.open_database(root, ledger.name, max_bytes=_DB_MAX_BYTES)
    conn.row_factory = sqlite3.Row
    try:
        ledger_stamp = os.lstat(ledger)
        with conn:
            _begin(conn)
            _create_schema(conn)
            conn.execute(
                "INSERT INTO domain VALUES (1,?,?,?,?,?,NULL,NULL,?,?,?,?,?)",
                (domain_id, SCHEMA_VERSION, PROTOCOL_VERSION,
                 ledger_stamp.st_dev, ledger_stamp.st_ino, boot,
                 limits[0], limits[1], limits[2], 0),
            )
        _publish_marker(domain, conn)
    except sqlite3.Error as exc:
        _unavailable_or_corrupt(exc, "coordinator initialization failed")
    finally:
        conn.close()


def _domain_info(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM domain WHERE singleton=1").fetchone()
    if row is None:
        _fail("coordinator-corrupt", "coordinator identity is missing")
    return dict(row)


def _boot_identity() -> str:
    boot = platform.boot_identity()
    if not isinstance(boot, str) or not boot:
        _fail("state-unavailable", "kernel boot identity is unavailable")
    return boot


def _empty_initialization(conn: sqlite3.Connection) -> bool:
    return all(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # nosec B608 - table is closed constant
               for table in ("jobs", "job_resources", "observations"))


def _publish_marker(domain: DomainPaths, conn: sqlite3.Connection) -> None:
    info = _domain_info(conn)
    files.create_exclusive(Path(domain.root), "domain.json",
                           json.dumps(_marker_object(info), sort_keys=True,
                                      separators=(",", ":")).encode("ascii"), private=True)
    _bind_marker(domain, conn)


def _bind_marker(domain: DomainPaths, conn: sqlite3.Connection) -> None:
    stamp = os.lstat(_state_marker(domain))
    with conn:
        conn.execute("UPDATE domain SET marker_device=?,marker_inode=? WHERE singleton=1",
                     (stamp.st_dev, stamp.st_ino))


def _open_state(domain: DomainPaths, *, create: bool,
                read_only: bool = False) -> tuple[sqlite3.Connection, dict]:
    """Validate and open once under bootstrap lock; never reopen a validated DB."""
    _validate_domain_argument(domain)
    if create:
        _prepare_root(domain)
    root, ledger, marker = Path(domain.root), Path(domain.ledger), _state_marker(domain)
    if not os.path.lexists(root):
        _fail("state-unavailable", "coordinator has not been initialized")
    files.validate_private_dir(root)
    if not create and not any(os.path.lexists(p) for p in (ledger, marker, root / "bootstrap.lock")):
        _fail("state-unavailable", "coordinator has not been initialized")
    # Partial-state decisions belong inside the lock: another initializer can
    # currently be between the schema commit and exclusive marker publication.
    with _bootstrap(root, create=create):
        has_db, has_marker = os.path.lexists(ledger), os.path.lexists(marker)
        if not has_db:
            if has_marker:
                _fail("coordinator-corrupt", "coordinator database is missing")
            if not create:
                _fail("state-unavailable", "coordinator has not been initialized")
            _initial_state(domain)
            has_marker = True
        files.validate_private_file(ledger)
        stamp = os.lstat(ledger)
        marker_stamp = os.lstat(marker) if has_marker else None
        marker_value = _read_state_marker(domain) if has_marker else None
        if marker_value and (marker_value["ledger_device"], marker_value["ledger_inode"]) != (stamp.st_dev, stamp.st_ino):
            _fail("unsafe-path", "coordinator database identity changed")
        conn = storage.open_database(root, ledger.name, max_bytes=_DB_MAX_BYTES, read_only=read_only)
        conn.row_factory = sqlite3.Row
        try:
            _validate_schema(conn)
            info = _domain_info(conn)
            after = os.lstat(ledger)
            if ((info["ledger_device"], info["ledger_inode"]) != (stamp.st_dev, stamp.st_ino)
                    or (after.st_dev, after.st_ino) != (stamp.st_dev, stamp.st_ino)):
                _fail("unsafe-path", "coordinator database identity changed")
            if (info["schema_version"] != SCHEMA_VERSION or info["protocol_version"] != PROTOCOL_VERSION
                    or (domain.domain_id is not None and info["domain_id"] != domain.domain_id)):
                _fail("protocol-mismatch", "coordinator domain identity is incompatible")
            _validated_limits(info)
            if domain.fixture and _load_config_limits(domain, create_missing=False) != (
                    info["config_slots"], info["config_jobs"], info["config_memory"]):
                _fail("protocol-mismatch", "fixture limits changed after initialization")
            if not has_marker:
                if (read_only or info["marker_inode"] is not None or info["marker_device"] is not None
                        or not _empty_initialization(conn)):
                    _fail("coordinator-corrupt", "coordinator marker is missing")
                _publish_marker(domain, conn)
            else:
                if marker_value != _marker_object(info):
                    _fail("protocol-mismatch", "coordinator identity does not match its marker")
                current = os.lstat(marker)
                identity = (current.st_dev, current.st_ino)
                if identity != (marker_stamp.st_dev, marker_stamp.st_ino):
                    _fail("unsafe-path", "domain marker changed during open")
                if info["marker_inode"] is None and info["marker_device"] is None:
                    if read_only or not _empty_initialization(conn):
                        _fail("coordinator-corrupt", "coordinator marker publication is incomplete")
                    _bind_marker(domain, conn)
                elif identity != (info["marker_device"], info["marker_inode"]):
                    _fail("unsafe-path", "coordinator marker identity changed")
            return conn, _domain_info(conn)
        except sqlite3.Error as exc:
            conn.close()
            _unavailable_or_corrupt(exc, "coordinator database is corrupt")
        except BaseException:
            conn.close()
            raise


def _boot_transition(row: dict, now: float) -> dict:
    row = dict(row)
    row.update(enqueue_time=now, grant_time=None, deadline=now)
    if row["state"] not in _TERMINAL:
        row.update(state="CANCELLED", phase="complete", nonce=None, generation=None,
                   final_status="incomplete", final_exit_code=70, final_committed=0,
                   final_source_valid=0, reason_code="ownership-uncertain",
                   reason_message="previous kernel boot ended before finalization")
    return row


def _recover_boot_locked(conn: sqlite3.Connection, now: float) -> dict:
    # Read after BEGIN IMMEDIATE, never use limits/boot read before the lock.
    info = _domain_info(conn)
    boot = _boot_identity()
    if info["boot_id"] != boot:
        conn.execute("""UPDATE jobs SET state='CANCELLED',phase='complete',nonce=NULL,generation=NULL,
                     final_status='incomplete',final_exit_code=70,final_committed=0,final_source_valid=0,
                     reason_code='ownership-uncertain',
                     reason_message='previous kernel boot ended before finalization'
                     WHERE state NOT IN ('RELEASED','CANCELLED')""")
        conn.execute("UPDATE jobs SET enqueue_time=?,grant_time=NULL,deadline=?", (now, now))
        conn.execute("DELETE FROM observations")
        conn.execute("UPDATE domain SET boot_id=?,config_generation=config_generation+1 WHERE singleton=1", (boot,))
        info = _domain_info(conn)
    return info


def _begin(conn: sqlite3.Connection) -> None:
    """Take the write lock, waiting out transient holders with backoff.

    A busy lock is never an ownership verdict: the holder always commits or
    rolls back, so waiting is safe. Only the budget is bounded; genuine
    errors fail immediately.
    """
    deadline = time.monotonic() + _BEGIN_RETRY_DEADLINE_S
    delay = _BEGIN_RETRY_BASE_S
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.Error as exc:
            if not storage.is_transient_sqlite(exc):
                _fail("coordinator-unavailable", "coordinator transaction is unavailable", retryable=True)
            if time.monotonic() >= deadline:
                raise storage.TransientContention(
                    message="coordinator transaction is unavailable", phase=_PHASE)
            time.sleep(delay)
            delay = min(_BEGIN_RETRY_MAX_S, delay * 2)


def _finish_transaction(conn: sqlite3.Connection, success: bool) -> None:
    try:
        try:
            conn.commit() if success else conn.rollback()
        except sqlite3.Error as exc:
            if success:
                _unavailable_from_sqlite(exc, "coordinator commit failed")
    finally:
        # Even a failed commit must close/roll back and release the SQLite lock.
        conn.close()


def _active_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT count(*) FROM jobs WHERE state IN (%s)" % ",".join("?" for _ in _ACTIVE),  # nosec B608 - placeholders are generated for closed enum values
        tuple(_ACTIVE),
    ).fetchone()[0])


def _pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM jobs WHERE state='QUEUED'").fetchone()[0])


def _validated_limits(info: dict) -> EffectiveLimits:
    try:
        return EffectiveLimits(max_slots=info["config_slots"], max_jobs=info["config_jobs"],
                               memory_mb=info["config_memory"])
    except (TypeError, ValueError):
        _fail("coordinator-corrupt", "stored admission limits are invalid")


def _selected_limits(conn: sqlite3.Connection, requested: tuple, old: tuple) -> tuple:
    if _active_count(conn) or _pending_count(conn):
        if old[2] is None:
            memory = requested[2]
        elif requested[2] is None:
            memory = old[2]
        else:
            memory = min(requested[2], old[2])
        return min(requested[0], old[0]), min(requested[1], old[1]), memory
    return requested


def _refresh_limits_locked(conn: sqlite3.Connection, domain: DomainPaths, info: dict) -> dict:
    requested = _load_config_limits(domain, create_missing=False)
    old = (info["config_slots"], info["config_jobs"], info["config_memory"])
    requested = _selected_limits(conn, requested, old)
    if requested == old:
        return info
    generation = int(info["config_generation"]) + 1
    conn.execute(
        "UPDATE domain SET config_slots=?,config_jobs=?,config_memory=?,config_generation=? WHERE singleton=1",
        (*requested, generation),
    )
    info = dict(info)
    info.update(config_slots=requested[0], config_jobs=requested[1],
                config_memory=requested[2], config_generation=generation)
    return info


def _reason(code: str, message: str) -> Reason:
    return Reason(code=code, message=message)


def _row_resources(conn: sqlite3.Connection, run_id: str) -> frozenset[str]:
    return frozenset(row[0] for row in conn.execute(
        "SELECT resource FROM job_resources WHERE run_id=?", (run_id,)))


def _request_contained(domain: DomainPaths, request: AdmissionRequest) -> None:
    if request.fixture != domain.fixture:
        _fail("unsafe-path", "admission fixture scope does not match the domain")
    if domain.fixture:
        root = os.path.realpath(domain.root)
        checkout = os.path.realpath(request.checkout.root)
        if checkout != root and not checkout.startswith(root + os.sep):
            _fail("unsafe-path", "fixture checkout escapes its fixture domain")


def _actual_slots(requested: int, max_slots: int, memory: int | None,
                  budget: int | None) -> int:
    slots = min(requested, max_slots)
    if budget is not None and memory is None:
        slots = min(slots, 1)
    return slots


def _memory_values(slots: int, per_worker: int | None,
                   budget: int | None) -> tuple[int | None, int | None]:
    estimate = None if per_worker is None else per_worker * slots
    if budget is None:
        return estimate, estimate
    return estimate, budget if per_worker is None else estimate


def _resources_available(conn: sqlite3.Connection, request: dict, slots: int,
                         estimate: int | None, reserved: int | None, info: dict) -> bool:
    if request["exclusive"] and _active_count(conn):
        return False
    active = conn.execute(
        "SELECT run_id,slots,exclusive,reserved_memory,checkout_id FROM jobs WHERE state IN (%s)"  # nosec B608 - placeholders are generated for closed enum values
        % ",".join("?" for _ in _ACTIVE), tuple(_ACTIVE),
    ).fetchall()
    if len(active) >= int(info["config_jobs"]):
        return False
    if not request["exclusive"] and any(bool(row[2]) for row in active):
        return False
    if any(row[4] == request["checkout_id"] for row in active):
        return False
    wanted = frozenset(request["locks"])
    for row in active:
        if wanted & _row_resources(conn, row[0]):
            return False
    if sum(int(row[1]) for row in active) + slots > int(info["config_slots"]):
        return False
    budget = info["config_memory"]
    if budget is not None:
        # A newly introduced budget cannot turn an existing unknown estimate
        # into zero charge; the old grant continues to hold the entire budget.
        used = sum(int(budget if row[3] is None else row[3]) for row in active)
        if reserved is None or used + reserved > int(budget):
            return False
    return True


def _observe_process(pid: int) -> tuple[ProcessIdentity | None, bool]:
    """None is indeterminate. Only an independent ESRCH proves absence."""
    try:
        observed = platform.process_identity(pid)
        if observed is not None:
            return observed, False
        os.kill(pid, 0)
    except OSError as exc:
        return None, exc.errno == errno.ESRCH
    return None, False


def _same_identity(observed: ProcessIdentity | None, row: sqlite3.Row | dict,
                   prefix: str = "owner") -> bool:
    if observed is None:
        return False
    return (observed.pid == row[f"{prefix}_pid"]
            and observed.birth == row[f"{prefix}_birth"]
            and observed.uid == row[f"{prefix}_uid"]
            and observed.pgid == row[f"{prefix}_pgid"])


def _descendant_observations(row: dict, recorded: list[dict]) -> tuple[list[dict], bool]:
    """Track bounded observed identities, including children before they escape.

    Inaccessible/capped scans persist an uncertainty sentinel. This is a
    cooperative lifecycle check, not proof against unobserved daemonization.

    Returns the observations plus whether THIS pass found fresh uncertainty.
    Recorded sentinels are carried forward (a dead guard can never be
    re-observed, so its doubt must stick), but the flag tells the caller
    whether the current pass added anything new: only a clean pass under a
    live guard may clear stale doubt.
    """
    observations = {}
    fresh_uncertain = False
    for item in recorded:
        item = dict(item)
        if item["uncertain"]:
            observations[item["pid"]] = item
            continue
        identity, absent = _observe_process(item["pid"])
        if absent or (identity is not None and identity.birth != item["birth"]):
            continue
        observations[item["pid"]] = item

    def uncertain():
        nonlocal fresh_uncertain
        fresh_uncertain = True
        observations[0] = dict(run_id=row["run_id"], pid=0, birth=None,
                               uid=None, pgid=None, uncertain=1)

    guard, absent = _observe_process(row["guard_pid"])
    if not absent and not _same_identity(guard, row, "guard"):
        uncertain()
    if _same_identity(guard, row, "guard"):
        pending, visited = [guard.pid], set()
        while pending:
            pid = pending.pop()
            if pid in visited:
                uncertain()
                break
            visited.add(pid)
            if len(visited) > _MAX_OBSERVATIONS:
                uncertain()
                break
            try:
                children = psutil.Process(pid).children()
            except psutil.NoSuchProcess:
                identity, gone = _observe_process(pid)
                expected = observations.get(pid)
                guard_reused = (pid == guard.pid and identity is not None
                                and not _same_identity(identity, row, "guard"))
                descendant_reused = (pid != guard.pid and identity is not None
                                     and expected is not None
                                     and identity.birth != expected["birth"])
                if gone or guard_reused or descendant_reused:
                    observations.pop(pid, None)
                    continue
                if identity is not None and (
                        (expected is not None
                         and identity.birth == expected["birth"]
                         and identity.uid == expected["uid"]
                         and identity.pgid == expected["pgid"])
                        or (pid == guard.pid and expected is None
                            and _same_identity(identity, row, "guard"))):
                    # Alive with an unchanged identity but an unlistable
                    # subtree (an unreaped zombie, a racing exec): it cannot
                    # act or escape, and there is nothing new beneath it this
                    # pass. Keep the record and re-walk it next pass instead
                    # of poisoning the whole observation. Anything changed
                    # (or unreadable) below still fails closed.
                    continue
                uncertain()
                break
            except (psutil.Error, OSError):
                # Inaccessible live trees remain unknown permanently.
                uncertain()
                break
            for child in children:
                if len(observations) >= _MAX_OBSERVATIONS:
                    uncertain()
                    pending.clear()
                    break
                identity, gone = _observe_process(child.pid)
                if gone:
                    continue
                if identity is None:
                    uncertain()
                    continue
                old = observations.get(child.pid)
                if old is not None and old["birth"] == identity.birth and old["uid"] != identity.uid:
                    uncertain()
                observations[child.pid] = dict(run_id=row["run_id"], pid=identity.pid,
                                              birth=identity.birth, uid=identity.uid,
                                              pgid=identity.pgid, uncertain=0)
                pending.append(child.pid)
        current, gone = _observe_process(guard.pid)
        if not gone and not _same_identity(current, row, "guard"):
            uncertain()
    return list(observations.values()), fresh_uncertain


def _escaped_or_unknown(row: dict, observations: list[dict], *, require_absent: bool = False) -> bool:
    for item in observations:
        if item["uncertain"]:
            return True
        identity, absent = _observe_process(item["pid"])
        if absent or (identity is not None and identity.birth != item["birth"]):
            continue
        if identity is None:
            return True
        if identity.uid != item["uid"] or identity.pgid != row["guard_pgid"]:
            return True
        if require_absent and not _unreaped_zombie(item["pid"]):
            return True
    return False


def _observation_cause(row: dict, observations: list[dict]) -> str | None:
    """Why the descendant check fails: 'escape', 'transient', or None (clean).

    Positive evidence (a live identity outside the guard group) is an escape
    verdict and must stick. A momentarily unreadable table or process is
    transient pressure: it reports UNCERTAIN for this pass but may heal when
    the next pass observes cleanly. Escape dominates: a live escapee beside
    a stale ghost still reports escape. Doubt recorded against a dead or
    changed guard can never be re-observed, so it stays a verdict — only a
    clean pass under a live, unchanged guard may clear stale doubt.
    """
    found_transient = False
    for item in observations:
        if item["uncertain"]:
            guard, gone = _observe_process(row["guard_pid"])
            if gone or guard is None or not _same_identity(guard, row, "guard"):
                return "escape"
            found_transient = True
            continue
        identity, absent = _observe_process(item["pid"])
        if absent or (identity is not None and identity.birth != item["birth"]):
            continue
        if identity is None:
            found_transient = True
            continue
        if identity.uid != item["uid"] or identity.pgid != row["guard_pgid"]:
            return "escape"
    return "transient" if found_transient else None


def _unreaped_zombie(pid: int) -> bool:
    """True when a recorded pid is an unreaped zombie, not a live survivor.

    A zombie keeps its identity (birth/uid/pgid still match the record) but
    its subtree is unlistable, holds no resources, and cannot escape — the
    same signal the descendant walk tolerates. Anything listable, changed,
    or unreadable stays unknown so the release still fails closed.
    """
    try:
        psutil.Process(pid).children()
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        # Inaccessible live trees remain unknown, never zombies.
        return False
    return False


def _observations(conn: sqlite3.Connection, row: dict, *, persist: bool) -> list[dict]:
    recorded = [dict(item) for item in conn.execute("SELECT * FROM observations WHERE run_id=?", (row["run_id"],))]
    observed, fresh_uncertain = _descendant_observations(row, recorded)
    if not fresh_uncertain and row["guard_pid"] is not None:
        # A clean pass under a live, unchanged guard proves the stale doubt
        # wrong: a raced walk or indeterminate read from an earlier pass must
        # not poison every later verdict. Clearing needs a positively live
        # guard — after death nothing can be re-observed, so doubt sticks
        # (fail closed). Positive escape evidence is never a sentinel, so a
        # live escapee still re-fires below on this same pass.
        guard, gone = _observe_process(row["guard_pid"])
        if not gone and guard is not None and _same_identity(guard, row, "guard"):
            observed = [item for item in observed if not item["uncertain"]]
    if persist:
        observed_pids = {item["pid"] for item in observed}
        conn.executemany(
            "DELETE FROM observations WHERE run_id=? AND pid=?",
            ((row["run_id"], item["pid"]) for item in recorded if item["pid"] not in observed_pids),
        )
        for item in observed:
            conn.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?)",
                         tuple(item[key] for key in ("run_id", "pid", "birth", "uid", "pgid", "uncertain")))
    return observed


def _uncertain(row: dict, message: str, code: str = "ownership-uncertain") -> dict:
    row.update(state="UNCERTAIN", reason_code=code, reason_message=message)
    return row


# UNCERTAIN verdicts that a later clean pass may heal back to RUNNING: a
# raced walk, a stale group probe, a momentarily indeterminate owner read.
# Everything else — escape verdicts, proof contradictions, queue/deadline
# outcomes — keeps its history and never enters this set.
_HEALABLE_UNCERTAIN_REASONS = frozenset({
    "descendant observation was incomplete",
    "process-group ownership could not be proven",
    "owner identity is indeterminate",
})


def _recovery_view(conn: sqlite3.Connection, row: dict, now: float, *, persist: bool) -> dict:
    row = dict(row)
    if row["state"] in _TERMINAL:
        return row
    if row["state"] == "QUEUED" and now >= row["deadline"]:
        row.update(state="CANCELLED", phase="complete", reason_code="queue-timeout",
                   reason_message=QUEUE_DEADLINE_MESSAGE)
        return row
    owner, absent = _observe_process(row["owner_pid"])
    if row["guard_pid"] is None:
        if absent:
            row.update(state="CANCELLED", phase="complete", reason_code="ownership-uncertain",
                       reason_message="owner absent before guard registration")
        elif row["state"] != "QUEUED" and not _same_identity(owner, row):
            _uncertain(row, "grant owner identity is indeterminate")
        return row
    observations = _observations(conn, row, persist=persist)
    cause = _observation_cause(row, observations)
    if cause == "escape":
        return _uncertain(row, "observed descendants are escaped or indeterminate",
                          "unsupported-detached-descendant")
    if cause == "transient":
        return _uncertain(row, "descendant observation was incomplete",
                          "ownership-uncertain")
    checked_at = _now()
    group = platform.probe_group(row["guard_pgid"])
    if group.exists is None or not group.permission or group.checked_at < checked_at:
        return _uncertain(row, "process-group ownership could not be proven")
    if group.exists:
        if not absent and not _same_identity(owner, row):
            return _uncertain(row, "owner identity is indeterminate")
        if (row["state"] == "UNCERTAIN" and row["phase"] == "setup"
                and not absent and (
                    (row["reason_code"] == "ownership-uncertain"
                     and row["reason_message"] in _HEALABLE_UNCERTAIN_REASONS)
                    or row["reason_code"] == "unsupported-detached-descendant")):
            # Live guard, live group, live owner verified, descendants clean:
            # whatever the recorded doubt was (a raced walk, a stale probe,
            # or an escape verdict whose evidence has since vanished — the
            # subjects reaped, the pids recycled), nothing alive remains to
            # leak, so restore RUNNING and let bounded retries proceed. A
            # proof contradiction ("finalization lacked...") never heals:
            # it names a caller that lied, not an observation that lapsed.
            # Anything still wrong re-fires on every pass before this point
            # is reached: a live escapee, a reused guard, and a still-blind
            # tree all recreate their verdicts here. Handoff phases never
            # heal: their pre-doubt state is not RUNNING and must not be
            # rewritten. An absent owner never heals either: an orphaned run
            # must not spawn.
            row.update(state="RUNNING", reason_code=None, reason_message=None)
        return row
    if _escaped_or_unknown(row, observations, require_absent=True):
        return _uncertain(row, "observed descendants remain after group absence",
                          "unsupported-detached-descendant")
    if not _observe_process(row["guard_pid"])[1]:
        return _uncertain(row, "guard absence cannot be proven")
    owner, absent = _observe_process(row["owner_pid"])
    if absent:
        row.update(state="RELEASED", phase="complete", reason_code="ownership-uncertain",
                   reason_message="owner and group absent; outcome incomplete")
    elif not _same_identity(owner, row):
        _uncertain(row, "finalizer identity is indeterminate")
    # A live owner retains its exact state, including the authenticated DRAINING
    # handoff. Only begin_finalization may consume that handoff; group absence
    # alone must not make a RUNNING guard death look like clean completion.
    return row


def _reconcile_locked(conn: sqlite3.Connection, now: float) -> None:
    rows = conn.execute("SELECT * FROM jobs WHERE state NOT IN ('RELEASED','CANCELLED') ORDER BY sequence").fetchall()
    for original in rows:
        row = _recovery_view(conn, dict(original), now, persist=True)
        conn.execute("""UPDATE jobs SET state=?,phase=?,reason_code=?,reason_message=?
                     WHERE run_id=? AND nonce IS ? AND generation IS ?""",
                     (row["state"], row["phase"], row["reason_code"], row["reason_message"],
                      row["run_id"], row["nonce"], row["generation"]))


class _TransientAncestry(Exception):
    """A momentarily unobservable process table; safe to look again."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _check_nested_once(conn: sqlite3.Connection, owner: ProcessIdentity) -> None:
    guards = conn.execute("SELECT * FROM jobs WHERE guard_pid IS NOT NULL AND state NOT IN ('RELEASED','CANCELLED')").fetchall()
    if not guards:
        return
    identity, _ = _observe_process(owner.pid)
    if identity is None:
        raise _TransientAncestry("admission owner identity cannot be verified")
    if identity != owner:
        _fail("ownership-uncertain", "admission owner identity cannot be verified")
    visited = set()
    pid = owner.pid
    try:
        while pid:
            if pid in visited or len(visited) >= _MAX_OBSERVATIONS:
                _fail("ownership-uncertain", "ancestry observation exceeded its bound")
            visited.add(pid)
            identity = platform.process_identity(pid)
            if identity is None:
                raise _TransientAncestry("ancestry identity is unavailable")
            for row in guards:
                if identity.pid == row["guard_pid"] or identity.pgid == row["guard_pgid"]:
                    current = platform.process_identity(row["guard_pid"])
                    if _same_identity(current, row, "guard"):
                        _fail("nested-invocation", "execution is nested inside an active guard")
                    _fail("ownership-uncertain", "active guard ancestry is indeterminate")
            parent = psutil.Process(pid).ppid()
            if platform.process_identity(pid) != identity:
                _fail("ownership-uncertain", "ancestry changed during observation")
            pid = parent
    except _TransientAncestry:
        raise
    except (psutil.Error, OSError) as exc:
        raise _TransientAncestry("ancestry is inaccessible") from exc


def _check_nested(conn: sqlite3.Connection, owner: ProcessIdentity) -> None:
    """Walk the owner ancestry, looking past momentary observation stalls.

    Only an unobservable table is retried. A present-but-different identity,
    a detected nesting, a bound cycle, or a changed mid-walk identity still
    fails closed on the first pass.
    """
    deadline = time.monotonic() + _ANCESTRY_RETRY_DEADLINE_S
    delay = _ANCESTRY_RETRY_BASE_S
    while True:
        try:
            _check_nested_once(conn, owner)
            return
        except _TransientAncestry as exc:
            if time.monotonic() >= deadline:
                _fail("ownership-uncertain", exc.message)
            time.sleep(delay)
            delay = min(_ANCESTRY_RETRY_MAX_S, delay * 2)


def _deadline_for(request: AdmissionRequest, now: float) -> float:
    if request.deadline == 0:
        return now + DEFAULT_QUEUE_TIMEOUT_S
    if request.deadline < now:
        return request.deadline
    if request.deadline - now > MAX_QUEUE_TIMEOUT_S:
        _fail("invalid-bound", "admission deadline exceeds the queue bound")
    return request.deadline


def _prune_terminal(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT run_id FROM jobs WHERE state IN ('RELEASED','CANCELLED') ORDER BY sequence DESC"
    ).fetchall()
    for row in rows[MAX_TERMINAL_SUMMARIES:]:
        conn.execute("DELETE FROM jobs WHERE run_id=?", (row[0],))


@_transient_retry
def enqueue(domain: DomainPaths, request: AdmissionRequest) -> Ticket:
    """Atomically append one local admission request to the domain FIFO."""
    if not isinstance(request, AdmissionRequest):
        raise TypeError("scheduler request must be AdmissionRequest")
    _request_contained(domain, request)
    if request.owner.uid != os.getuid():
        _fail("ownership-uncertain", "admission owner is not the current account")
    conn, info = _open_state(domain, create=True)
    ok = False
    try:
        _begin(conn)
        now = _now()
        info = _recover_boot_locked(conn, now)
        _reconcile_locked(conn, now)
        info = _refresh_limits_locked(conn, domain, info)
        _check_nested(conn, request.owner)
        if _pending_count(conn) >= MAX_PENDING_JOBS:
            _fail("capacity-exceeded", "pending admission capacity is full")
        if conn.execute("SELECT 1 FROM jobs WHERE run_id=?", (request.run_id,)).fetchone():
            _fail("already-exists", "admission run identity already exists")
        slots = _actual_slots(request.slots, int(info["config_slots"]),
                               request.memory_mb, info["config_memory"])
        estimate, reserved = _memory_values(slots, request.memory_mb, info["config_memory"])
        if info["config_memory"] is not None and reserved is not None and reserved > info["config_memory"]:
            _fail("capacity-exceeded", "admission memory request exceeds the machine budget")
        sequence = int(conn.execute("SELECT coalesce(max(sequence),0)+1 FROM jobs").fetchone()[0])
        conn.execute(
            """INSERT INTO jobs (
                run_id,sequence,state,checkout_id,requested_slots,slots,exclusive,
                memory_estimate,reserved_memory,enqueue_time,grant_time,deadline,
                phase,fixture,owner_pid,owner_birth,owner_uid,owner_pgid
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request.run_id, sequence, "QUEUED", request.checkout.checkout_id,
             request.slots, slots, int(request.exclusive), estimate, reserved,
             now, None, _deadline_for(request, now), "admission", int(request.fixture),
             request.owner.pid, request.owner.birth, request.owner.uid, request.owner.pgid),
        )
        for resource in request.locks:
            conn.execute("INSERT INTO job_resources VALUES (?,?)", (request.run_id, resource))
        _prune_terminal(conn)
        ok = True
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)
    return Ticket(run_id=request.run_id, sequence=sequence)


def _grant_queued_locked(conn: sqlite3.Connection, info: dict, now: float) -> None:
    # Recovery expires every queued deadline before this strict FIFO scan.
    rows = conn.execute("SELECT * FROM jobs WHERE state='QUEUED' ORDER BY sequence").fetchall()
    for row in rows:
        requested = {
            "run_id": row["run_id"], "checkout_id": row["checkout_id"],
            "exclusive": bool(row["exclusive"]), "locks": _row_resources(conn, row["run_id"]),
        }
        slots = _actual_slots(row["requested_slots"], int(info["config_slots"]),
                              None if row["memory_estimate"] is None else
                              (row["memory_estimate"] // max(1, row["slots"])),
                              info["config_memory"])
        per_worker = None if row["memory_estimate"] is None else row["memory_estimate"] // max(1, row["slots"])
        estimate, reserved = _memory_values(slots, per_worker, info["config_memory"])
        if not _resources_available(conn, requested, slots, estimate, reserved, info):
            break
        nonce = secrets.token_hex(32)
        conn.execute(
            "UPDATE jobs SET state='GRANTED',slots=?,memory_estimate=?,reserved_memory=?,grant_time=?,phase='admission',nonce=?,generation=? WHERE run_id=?",
            (slots, estimate, reserved, now, nonce, info["config_generation"], row["run_id"]),
        )


def _grant_from_row(row: sqlite3.Row | dict, domain_id: str) -> Grant | None:
    if row["state"] not in _ACTIVE or row["nonce"] is None or row["generation"] is None or row["slots"] <= 0:
        return None
    return Grant(
        run_id=row["run_id"], nonce=row["nonce"], slots=row["slots"],
        memory_estimate_mb=row["memory_estimate"], reserved_memory_mb=row["reserved_memory"],
        generation=row["generation"], domain_id=domain_id,
    )


def _problem_from_row(row: sqlite3.Row | dict) -> Problem | None:
    if row["reason_code"] is None:
        return None
    try:
        return Problem(code=row["reason_code"], message=row["reason_message"] or "scheduler state changed",
                       phase=_PHASE, retryable=False)
    except (TypeError, ValueError):
        return Problem(code="coordinator-corrupt", message="scheduler reason is invalid",
                       phase=_PHASE, retryable=False)


@_transient_retry
def poll(domain: DomainPaths, ticket: Ticket) -> AdmissionState:
    """Reconcile, then attempt FIFO admission; polling never bypasses the head."""
    if not isinstance(ticket, Ticket):
        raise TypeError("scheduler ticket must be Ticket")
    conn, info = _open_state(domain, create=False)
    ok = False
    try:
        _begin(conn)
        now = _now()
        info = _recover_boot_locked(conn, now)
        _reconcile_locked(conn, now)
        info = _refresh_limits_locked(conn, domain, info)
        _grant_queued_locked(conn, info, now)
        row = conn.execute("SELECT * FROM jobs WHERE run_id=? AND sequence=?",
                           (ticket.run_id, ticket.sequence)).fetchone()
        if row is None:
            _fail("state-unavailable", "admission ticket is no longer retained")
        ok = True
        return AdmissionState(
            state=LeaseState(row["state"]),
            grant=_grant_from_row(row, info["domain_id"]),
            problem=_problem_from_row(row),
            position=(sum(1 for item in conn.execute(
                "SELECT sequence FROM jobs WHERE state='QUEUED' AND sequence<=?", (row["sequence"],))) - 1
                      if row["state"] == "QUEUED" else None),
        )
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)


@_transient_retry
def register_guard(domain: DomainPaths, grant: Grant, guard: ProcessIdentity) -> bool:
    """CAS-register one guard identity before repository work can begin."""
    if not isinstance(grant, Grant):
        raise TypeError("scheduler grant must be Grant")
    if not isinstance(guard, ProcessIdentity):
        raise TypeError("scheduler guard must be ProcessIdentity")
    if guard.uid != os.getuid():
        _fail("ownership-uncertain", "guard is not owned by the current account")
    conn, info = _open_state(domain, create=False)
    ok = False
    try:
        _begin(conn)
        info = _recover_boot_locked(conn, _now())
        _reconcile_locked(conn, _now())
        row = conn.execute("SELECT * FROM jobs WHERE run_id=?", (grant.run_id,)).fetchone()
        valid = (row is not None and row["state"] == "GRANTED"
                 and _matches_grant(row, grant, info)
                 and _same_identity(platform.process_identity(row["owner_pid"]), row)
                 and platform.process_identity(guard.pid) == guard)
        if valid:
            conn.execute(
                "UPDATE jobs SET state='RUNNING',phase='setup',guard_pid=?,guard_birth=?,guard_uid=?,guard_pgid=? WHERE run_id=? AND state='GRANTED' AND guard_pid IS NULL",
                (guard.pid, guard.birth, guard.uid, guard.pgid, grant.run_id),
            )
            result = conn.execute("SELECT changes()").fetchone()[0] == 1
        else:
            result = False
        ok = True
        return bool(result)
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)


@_transient_retry
def mark_draining(domain: DomainPaths, grant: Grant, guard: ProcessIdentity) -> bool:
    """CAS the authenticated live guard from RUNNING/CANCELLING to DRAINING."""
    if not isinstance(grant, Grant):
        raise TypeError("scheduler grant must be Grant")
    if not isinstance(guard, ProcessIdentity):
        raise TypeError("scheduler guard must be ProcessIdentity")
    conn, info = _open_state(domain, create=False)
    ok = False
    try:
        _begin(conn)
        # This handoff is deliberately not a recovery boundary: a changed boot
        # or any stale identity simply cannot authorize a new state transition.
        info = _domain_info(conn)
        if (info["boot_id"] != _boot_identity() or grant.domain_id != info["domain_id"]
                or guard.pid != os.getpid() or guard.uid != os.getuid()
                or guard.pgid != guard.pid):
            result = False
        else:
            live = platform.process_identity(guard.pid)
            if live is None:
                _fail("ownership-uncertain", "live guard identity cannot be verified")
            if live != guard:
                result = False
            else:
                cursor = conn.execute(
                    """UPDATE jobs SET state='DRAINING',phase='draining'
                       WHERE run_id=? AND nonce=? AND generation=?
                         AND slots=? AND memory_estimate IS ? AND reserved_memory IS ?
                         AND state IN ('RUNNING','CANCELLING')
                         AND guard_pid=? AND guard_birth=? AND guard_uid=? AND guard_pgid=?""",
                    (grant.run_id, grant.nonce, grant.generation,
                     grant.slots, grant.memory_estimate_mb, grant.reserved_memory_mb,
                     guard.pid, guard.birth, guard.uid, guard.pgid),
                )
                result = cursor.rowcount == 1
        ok = True
        return result
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)


def _lease_view(row: sqlite3.Row | dict, now: float) -> LeaseView:
    state = LeaseState(row["state"])
    age = max(0.0, now - float(row["enqueue_time"]))
    queue_wait = age if row["grant_time"] is None else max(0.0, row["grant_time"] - row["enqueue_time"])
    if state in _TERMINAL:
        ownership = "gone"
    elif state == LeaseState.UNCERTAIN:
        ownership = "uncertain"
    else:
        ownership = "certain"
    reasons = ()
    if row["reason_code"] is not None:
        reasons = (_reason(row["reason_code"], row["reason_message"] or "scheduler state changed"),)
    return LeaseView(
        run_id=row["run_id"], checkout_id=row["checkout_id"], state=state,
        sequence=row["sequence"], requested_slots=row["requested_slots"], slots=row["slots"],
        memory_estimate_mb=row["memory_estimate"], reserved_memory_mb=row["reserved_memory"],
        phase=row["phase"], age_s=age, queue_wait_s=queue_wait,
        ownership=ownership, fixture=bool(row["fixture"]), reasons=reasons,
    )


@_transient_retry
def reconcile(domain: DomainPaths) -> tuple[LeaseView, ...]:
    """Observe the same recovery decisions as admission without writing state."""
    try:
        conn, info = _open_state(domain, create=False, read_only=True)
    except Problem as exc:
        if exc.code == "state-unavailable":
            return ()
        raise
    try:
        conn.execute("BEGIN")
        info = _domain_info(conn)
        boot_changed = _boot_identity() != info["boot_id"]
        now = _now()
        rows = conn.execute("SELECT * FROM jobs ORDER BY sequence").fetchall()
        return tuple(_lease_view(_boot_transition(dict(row), now) if boot_changed else
                                 _recovery_view(conn, dict(row), now, persist=False), now)
                     for row in rows)
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator read failed")
    finally:
        conn.close()


class QueueHolder(NamedTuple):
    """One live slot holder for waiting-line display (all fields public)."""

    run_id: str
    pid: int
    checkout_id: str


@_transient_retry
def queue_holders(domain: DomainPaths, *, limit: int = 5) -> tuple[QueueHolder, ...]:
    """Best-effort read-only snapshot of live slot holders, oldest first.

    Display-only: never raises for missing state, and never exposes more
    than the owner's pid and checkout id (no argv, no secrets).
    """
    try:
        conn, _ = _open_state(domain, create=False, read_only=True)
    except Problem as exc:
        if exc.code == "state-unavailable":
            return ()
        raise
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT run_id, owner_pid, checkout_id FROM jobs WHERE state IN "
            "('GRANTED','RUNNING','DRAINING','FINALIZING','CANCELLING','UNCERTAIN')"
            " ORDER BY sequence LIMIT ?", (max(1, limit),)).fetchall()
        holders = []
        for row in rows:
            try:
                run_id = str(row["run_id"])
                pid = int(row["owner_pid"])
                checkout_id = str(row["checkout_id"])
            except (TypeError, ValueError):
                continue
            if pid > 0 and checkout_id and run_id:
                holders.append(QueueHolder(
                    run_id=run_id, pid=pid, checkout_id=checkout_id))
        return tuple(holders)
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _row_blocker(conn: sqlite3.Connection, row: sqlite3.Row, info: dict) -> str | None:
    """Why ``row`` is not admitted now, in _resources_available's order.

    None means slots (or memory) are the only shortfall; the caller's slot
    line already says that truthfully.
    """
    active = conn.execute(
        "SELECT run_id,slots,exclusive,checkout_id FROM jobs WHERE state IN (%s)"  # nosec B608 - placeholders are generated for closed enum values
        % ",".join("?" for _ in _ACTIVE), tuple(_ACTIVE),
    ).fetchall()
    if row["exclusive"] and active:
        verb = "is" if len(active) == 1 else "are"
        return f"this run needs the whole machine; {_count(len(active), 'run')} {verb} still active"
    if len(active) >= int(info["config_jobs"]):
        return (f"the job limit is reached ({len(active)} of "
                f"{int(info['config_jobs'])} running)")
    if any(bool(item["exclusive"]) for item in active):
        return "an exclusive run holds the whole machine"
    if any(item["checkout_id"] == row["checkout_id"] for item in active):
        return "this checkout already has a run in progress"
    wanted = _row_resources(conn, row["run_id"])
    for item in active:
        held = sorted(wanted & _row_resources(conn, item["run_id"]))
        if held:
            return f"lock '{held[0]}' is held by another run"
    return None


def admission_blocker(domain: DomainPaths, run_id: str) -> str | None:
    """Best-effort, read-only reason a queued run is still waiting.

    Display-only: None for runs that are not queued, for a slot-only
    shortfall, and on any state problem. Strict FIFO means earlier queued
    runs are admitted first, so they are counted ahead of the run's own
    blocker.
    """
    try:
        conn, _ = _open_state(domain, create=False, read_only=True)
    except Problem:
        return None
    try:
        conn.execute("BEGIN")
        info = _domain_info(conn)
        queued = conn.execute(
            "SELECT * FROM jobs WHERE state='QUEUED' ORDER BY sequence").fetchall()
        index = next((position for position, row in enumerate(queued)
                      if row["run_id"] == run_id), None)
        if index is None:
            return None
        reason = _row_blocker(conn, queued[index], info)
        if index == 0:
            return reason
        verb = "is" if index == 1 else "are"
        ahead = f"{_count(index, 'earlier run')} {verb} queued first"
        return ahead if reason is None else f"{ahead}; {reason}"
    except (sqlite3.Error, Problem, KeyError, TypeError, ValueError):
        return None
    finally:
        conn.close()


@_transient_retry
def forget_checkouts(domain: DomainPaths, ids) -> int:
    """Delete only terminal scheduler rows for ``ids``, under one lock.

    Runs under ``BEGIN IMMEDIATE`` on the scheduler-owned coordinator:
    when any non-terminal row exists for those checkout ids the whole
    forget is refused (``active-run``) and nothing is deleted; otherwise
    every terminal row for those ids is removed and the deleted count
    is returned. Callers remove state directories only after this
    returns, so a run admitted in between still refuses first.
    """
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return 0
    for item in wanted:
        if not isinstance(item, str) or not item:
            raise TypeError("scheduler checkout ids must be nonempty strings")
    conn, _ = _open_state(domain, create=False)
    ok = False
    try:
        _begin(conn)
        now = _now()
        _recover_boot_locked(conn, now)
        _reconcile_locked(conn, now)
        marks = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"SELECT checkout_id, state FROM jobs WHERE checkout_id IN ({marks})",
            wanted).fetchall()
        live = sorted({row["checkout_id"] for row in rows
                       if row["state"] not in _TERMINAL})
        if live:
            _fail("active-run",
                  "a ptest run for this checkout is active; refusing to uninstall")
        cursor = conn.execute(
            f"DELETE FROM jobs WHERE checkout_id IN ({marks})"
            " AND state IN ('RELEASED','CANCELLED')",
            wanted)
        ok = True
        return cursor.rowcount
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)


@_transient_retry
def initialize(domain: DomainPaths) -> None:
    """Create and validate the account coordinator before read-only checks."""
    conn, _ = _open_state(domain, create=True)
    conn.close()


@_transient_retry
def effective_limits(domain: DomainPaths) -> EffectiveLimits:
    """Read-only T11 presentation using the shared frozen limits record.

    No repository configuration is available at this boundary, so repo_workers
    remains null. Missing state/config never causes default persistence.
    """
    try:
        conn, info = _open_state(domain, create=False, read_only=True)
    except Problem as exc:
        if exc.code != "state-unavailable":
            raise
        try:
            limits = _load_config_limits(domain, create_missing=False)
        except Problem as config_error:
            if config_error.code == "state-unavailable":
                return EffectiveLimits()
            raise
    else:
        try:
            conn.execute("BEGIN")
            info = _domain_info(conn)
            _boot_identity()
            requested = _load_config_limits(domain, create_missing=False)
            limits = _selected_limits(conn, requested, (info["config_slots"], info["config_jobs"], info["config_memory"]))
        except sqlite3.Error as exc:
            _unavailable_from_sqlite(exc, "coordinator read failed")
        finally:
            conn.close()
    return EffectiveLimits(max_slots=limits[0], max_jobs=limits[1], memory_mb=limits[2])


def _matches_grant(row: sqlite3.Row | dict, grant: Grant, info: dict) -> bool:
    return (row["nonce"] == grant.nonce and row["generation"] == grant.generation
            and row["slots"] == grant.slots and row["memory_estimate"] == grant.memory_estimate_mb
            and row["reserved_memory"] == grant.reserved_memory_mb and info["domain_id"] == grant.domain_id)


def _matches_owner(row: sqlite3.Row | dict, owner: ProcessIdentity) -> bool:
    """Authenticate a cancellation caller against the durable owner identity."""
    return (owner.pid == os.getpid() and owner.uid == os.getuid()
            and owner.pid == row["owner_pid"]
            and owner.birth == row["owner_birth"]
            and owner.uid == row["owner_uid"]
            and owner.pgid == row["owner_pgid"]
            and platform.process_identity(owner.pid) == owner)


@_transient_retry
def cancel_pending(domain: DomainPaths, ticket: Ticket, owner: ProcessIdentity) -> bool:
    """Cancel only an exact owner's queued or never-registered grant.

    The transaction deliberately does not reconcile or recover any other row.
    Registration and cancellation contend on the same ``BEGIN IMMEDIATE`` lock;
    whichever CAS commits first determines whether the grant is still
    unregistered, so a registered live guard can never be cancelled here.
    """
    if not isinstance(ticket, Ticket):
        raise TypeError("scheduler ticket must be Ticket")
    if not isinstance(owner, ProcessIdentity):
        raise TypeError("scheduler owner must be ProcessIdentity")
    conn, info = _open_state(domain, create=False)
    ok = False
    try:
        _begin(conn)
        if info["boot_id"] != _boot_identity():
            ok = True
            return False
        row = conn.execute(
            "SELECT * FROM jobs WHERE run_id=? AND sequence=?",
            (ticket.run_id, ticket.sequence),
        ).fetchone()
        if row is None or not _matches_owner(row, owner):
            ok = True
            return False
        if row["state"] not in {"QUEUED", "GRANTED"} or row["guard_pid"] is not None:
            ok = True
            return False
        cursor = conn.execute(
            """UPDATE jobs SET state='CANCELLED',phase='complete',nonce=NULL,generation=NULL
               WHERE run_id=? AND sequence=? AND state IN ('QUEUED','GRANTED')
                 AND guard_pid IS NULL AND owner_pid=? AND owner_birth=?
                 AND owner_uid=? AND owner_pgid=?""",
            (ticket.run_id, ticket.sequence, owner.pid, owner.birth, owner.uid, owner.pgid),
        )
        ok = True
        return cursor.rowcount == 1
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok)


def _finalization_failure(conn: sqlite3.Connection, run_id: str, *, escaped: bool) -> None:
    conn.execute(
        """UPDATE jobs SET state='UNCERTAIN',reason_code=?,reason_message=?
           WHERE run_id=? AND state NOT IN ('RELEASED','CANCELLED')""",
        ("unsupported-detached-descendant" if escaped else "ownership-uncertain",
         "observed descendants are escaped or indeterminate" if escaped else
         "finalization lacked current ownership and quiescence proof", run_id),
    )


@_transient_retry
def begin_finalization(domain: DomainPaths, grant: Grant) -> QuiescenceProof:
    """Prove quiescence and enter ``FINALIZING`` while retaining every claim."""
    if not isinstance(grant, Grant):
        raise TypeError("scheduler grant must be Grant")
    conn, info = _open_state(domain, create=False)
    ok = False
    commit_on_error = False
    try:
        _begin(conn)
        info = _domain_info(conn)
        if info["boot_id"] != _boot_identity():
            _fail("ownership-uncertain", "finalization boot identity is stale")
        row = conn.execute("SELECT * FROM jobs WHERE run_id=?", (grant.run_id,)).fetchone()
        if row is None or not _matches_grant(row, grant, info) or row["state"] != "DRAINING":
            _fail("ownership-uncertain", "finalization grant does not match a live lease")
        row = dict(row)
        if row["guard_pid"] is None:
            _fail("ownership-uncertain", "finalization guard was never registered")

        owner, owner_absent = _observe_process(row["owner_pid"])
        if owner_absent or not _same_identity(owner, row):
            _finalization_failure(conn, grant.run_id, escaped=False)
            commit_on_error = True
            _fail("ownership-uncertain", "finalization owner identity is indeterminate")

        observations = _observations(conn, row, persist=True)
        group = platform.probe_group(row["guard_pgid"])
        escaped = _escaped_or_unknown(
            row, observations, require_absent=group.exists is False
        )
        guard, guard_absent = _observe_process(row["guard_pid"])
        valid = (not escaped and guard_absent and group.exists is False
                 and group.permission and isinstance(row["grant_time"], (int, float))
                 and not isinstance(row["grant_time"], bool)
                 and group.checked_at >= row["grant_time"])
        if not valid:
            _finalization_failure(conn, grant.run_id, escaped=escaped)
            commit_on_error = True
            _fail("ownership-uncertain", "finalization lacked current ownership and quiescence proof")

        proof = QuiescenceProof(
            run_id=grant.run_id,
            generation=grant.generation,
            pgid=row["guard_pgid"],
            checked_at=group.checked_at,
            group_absent=True,
            escaped_survivors=False,
        )
        cursor = conn.execute(
            """UPDATE jobs SET state='FINALIZING',phase='finalization'
               WHERE run_id=? AND nonce=? AND generation=? AND state='DRAINING'
                 AND guard_pid=? AND guard_birth=? AND guard_uid=? AND guard_pgid=?""",
            (grant.run_id, grant.nonce, grant.generation, row["guard_pid"],
             row["guard_birth"], row["guard_uid"], row["guard_pgid"]),
        )
        if cursor.rowcount != 1:
            _fail("ownership-uncertain", "finalization lease changed during proof")
        ok = True
        return proof
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok or commit_on_error)


@_transient_retry
def finish(domain: DomainPaths, grant: Grant, proof: QuiescenceProof,
           final: Finalization) -> None:
    """Commit finalization only after a typed, conservative quiescence proof."""
    if not isinstance(grant, Grant):
        raise TypeError("scheduler grant must be Grant")
    if not isinstance(proof, QuiescenceProof):
        raise TypeError("scheduler proof must be QuiescenceProof")
    if not isinstance(final, Finalization):
        raise TypeError("scheduler finalization must be Finalization")
    conn, info = _open_state(domain, create=False)
    ok = False
    commit_on_error = False
    try:
        _begin(conn)
        now = _now()
        info = _recover_boot_locked(conn, now)
        row = conn.execute("SELECT * FROM jobs WHERE run_id=?", (grant.run_id,)).fetchone()
        # Authenticate every grant field before touching this or any other row.
        if row is None or not _matches_grant(row, grant, info) or row["state"] not in _ACTIVE:
            _fail("ownership-uncertain", "finalization grant does not match a live lease")
        row = dict(row)
        observations = [] if row["guard_pid"] is None else _observations(conn, row, persist=True)
        if proof.escaped_survivors:
            conn.execute("INSERT OR REPLACE INTO observations VALUES (?,0,NULL,NULL,NULL,1)", (grant.run_id,))
        checked_at = _now()
        group = None if row["guard_pgid"] is None else platform.probe_group(row["guard_pgid"])
        # Observe recorded descendants after the group probe: a child can have
        # left the group between the initial tree scan and ESRCH.
        escaped = proof.escaped_survivors or _escaped_or_unknown(
            row, observations, require_absent=group is not None and group.exists is False)
        owner, _ = _observe_process(row["owner_pid"])
        valid = (row["guard_pid"] is not None
                 and proof.run_id == grant.run_id and proof.generation == grant.generation
                 and proof.pgid == row["guard_pgid"] and proof.group_absent and not escaped
                 and row["grant_time"] <= proof.checked_at <= now
                 and group.exists is False and group.permission
                 and group.checked_at >= max(proof.checked_at, checked_at)
                 and _observe_process(row["guard_pid"])[1]
                 and _same_identity(owner, row))
        if not valid:
            _finalization_failure(conn, grant.run_id, escaped=escaped)
            commit_on_error = True
            _fail("ownership-uncertain", "finalization proof did not establish safe release")
        conn.execute("UPDATE jobs SET state='FINALIZING',phase='finalization' WHERE run_id=? AND nonce=? AND generation=?",
                     (grant.run_id, grant.nonce, grant.generation))
        conn.execute(
            """UPDATE jobs SET state='RELEASED',phase='complete',final_status=?,
               final_exit_code=?,final_committed=?,final_source_valid=?,reason_code=NULL,reason_message=NULL
               WHERE run_id=?""",
            (final.status.value, final.exit_code, int(final.committed), int(final.source_valid), grant.run_id),
        )
        _prune_terminal(conn)
        ok = True
    except sqlite3.Error as exc:
        _unavailable_from_sqlite(exc, "coordinator write failed")
    finally:
        _finish_transaction(conn, ok or commit_on_error)


__all__ = [
    "enqueue", "poll", "register_guard", "mark_draining", "cancel_pending",
    "begin_finalization", "reconcile", "finish", "effective_limits",
    "admission_blocker",
    "prepare_state_directory",
]
