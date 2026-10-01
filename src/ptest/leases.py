"""Per-run lease locks and namespace sidecars (T1 owned).

Every admitted run owns one lease lock file held by its owner and its
guard only, plus a private sidecar recording the recorder's PID-namespace
identity and the lease file's (st_dev, st_ino). Observers in another
namespace judge a row by the lock alone; same-namespace and legacy rows
keep the scheduler's pid evidence.

No sqlite access, no schema change, no subprocess/socket/psutil/platform
access and no environment reads: all file I/O goes through ``files.py``.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import files
from .contracts import Problem

LEASES_DIRNAME = "leases"
NAMESPACE_UNKNOWN = "unknown"
NAMESPACE_HOST = "host"
SIDECAR_VERSION = 1
SIDECAR_MAX_BYTES = 4096
LIST_LIMIT = 1024

ACQUIRABLE = "acquirable"
HELD = "held"
INDETERMINATE = "indeterminate"

_LOCK_SUFFIX = ".lock"
_SIDECAR_SUFFIX = ".json"
_NAMESPACE_RE = re.compile(r"(unknown|host|pid:\[[0-9]{1,20}\]@[0-9]{1,20})")
_PID_LINK_RE = re.compile(r"pid:\[[0-9]{1,20}\]")
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_PROC_STAT_LIMIT = 4096

_PHASE = "leases"

# In-process registry of lease fds this process holds: (str(root), run_id).
_registry: dict[tuple[str, str], int] = {}
_registry_lock = threading.Lock()


def _fail(code: str, message: str) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=False)


@dataclass(frozen=True)
class Sidecar:
    status: str  # "absent" | "valid" | "invalid"
    pid_namespace: str | None = None  # set only when status == "valid"
    lease_device: int | None = None
    lease_inode: int | None = None


def _read_proc_stat_starttime() -> int:
    with open("/proc/1/stat", "rb") as stream:
        raw = stream.read(_PROC_STAT_LIMIT)
    tail = raw[raw.rindex(b")") + 2:]
    return int(tail.split()[19])


def namespace_identity() -> str:
    """Identify this process's PID namespace for lease comparison.

    Linux: ``{readlink /proc/self/ns/pid}@{init start time}``, gated by a
    repeated readlink agreeing and ``readlink(/proc/self) == str(getpid())``.
    Anything unreadable or inconsistent returns ``"unknown"``; non-Linux
    returns ``"host"``. Reads /proc directly, never platform/psutil/os.kill.
    A hidepid /proc or a host /proc inside a sandbox yields ``"unknown"``,
    which fails closed (lock-only proof) for that run, even to its own
    namespace.
    """
    if not sys.platform.startswith("linux"):
        return NAMESPACE_HOST
    try:
        first = os.readlink("/proc/self/ns/pid")
        second = os.readlink("/proc/self/ns/pid")
        if first != second:
            return NAMESPACE_UNKNOWN
        if os.readlink("/proc/self") != str(os.getpid()):
            return NAMESPACE_UNKNOWN
        if _PID_LINK_RE.fullmatch(first) is None:
            return NAMESPACE_UNKNOWN
        starttime = _read_proc_stat_starttime()
        if not isinstance(starttime, bool) and isinstance(starttime, int) and starttime >= 0:
            return f"{first}@{starttime}"
        return NAMESPACE_UNKNOWN
    except (OSError, ValueError, IndexError):
        return NAMESPACE_UNKNOWN


def is_foreign(recorded: str, observer: str) -> bool:
    """True unless the identities are equal and neither is ``"unknown"``.

    ``"unknown"`` compares foreign to every observer, including another
    ``"unknown"``: equality is never treated as sole proof.
    """
    return not (recorded == observer and recorded != NAMESPACE_UNKNOWN
                and observer != NAMESPACE_UNKNOWN)


def _leases_dir(root: Path) -> Path:
    return Path(root) / LEASES_DIRNAME


def _check_names(run_id: str) -> tuple[str, str]:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        _fail("unsafe-path", "run id is not a 32-char hex string")
        raise AssertionError("unreachable")
    return f"{run_id}{_LOCK_SUFFIX}", f"{run_id}{_SIDECAR_SUFFIX}"


def _sidecar_bytes(run_id: str, namespace: str, device: int, inode: int) -> bytes:
    return json.dumps(
        {"version": SIDECAR_VERSION, "run_id": run_id,
         "pid_namespace": namespace, "lease_device": device,
         "lease_inode": inode},
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii")


def create(root: Path, run_id: str) -> int:
    """Create and lock one run lease; register the held fd. Fail closed.

    Creates ``<root>/leases/`` (0700), then the ``<run_id>.lock`` file via
    ``files.create_locked`` (LOCK_EX held), fstats it, writes the canonical
    sidecar bytes via ``files.create_exclusive``, and registers the fd.
    An EEXIST on the lock propagates unchanged (already-exists). Any later
    failure unlinks the owned lock (identity-checked), closes the fd and
    raises state-unavailable ``"run lease cannot be created"``.
    """
    lock_name, sidecar_name = _check_names(run_id)
    anchor = Path(root)
    try:
        leases_dir = files.ensure_private_dir(anchor, LEASES_DIRNAME)
    except Problem:
        raise
    except OSError as exc:
        _fail("state-unavailable", "run lease cannot be created")
        raise AssertionError("unreachable") from exc
    fd = files.create_locked(leases_dir, lock_name)
    try:
        stamp = os.fstat(fd)
        payload = _sidecar_bytes(run_id, namespace_identity(),
                                 stamp.st_dev, stamp.st_ino)
        files.create_exclusive(leases_dir, sidecar_name, payload, private=True)
    except Problem:
        try:
            owned = os.fstat(fd)
            files.unlink_if_same(leases_dir, lock_name, owned.st_dev, owned.st_ino)
        except (Problem, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _fail("state-unavailable", "run lease cannot be created")
        raise AssertionError("unreachable")
    except (OSError, ValueError) as exc:
        try:
            owned = os.fstat(fd)
            files.unlink_if_same(leases_dir, lock_name, owned.st_dev, owned.st_ino)
        except (Problem, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _fail("state-unavailable", "run lease cannot be created")
        raise AssertionError("unreachable") from exc
    with _registry_lock:
        old = _registry.get((str(anchor), run_id))
        _registry[(str(anchor), run_id)] = fd
    if old is not None:
        try:
            os.close(old)
        except OSError:
            pass
    return fd


def read_sidecar(root: Path, run_id: str) -> Sidecar:
    """Read one run sidecar. Never creates anything.

    Missing leases dir or missing file returns ``"absent"`` (legacy row).
    A symlinked, wrongly-owned/moded/linked, oversized, non-JSON, wrong-key,
    version-mismatched, run-mismatched or badly-typed record returns
    ``"invalid"``.
    """
    lock_name, sidecar_name = _check_names(run_id)
    leases_dir = _leases_dir(root)
    try:
        entry = os.lstat(leases_dir / sidecar_name)
    except FileNotFoundError:
        return Sidecar(status="absent")
    except OSError:
        return Sidecar(status="invalid")
    if stat.S_ISLNK(entry.st_mode):
        return Sidecar(status="invalid")
    try:
        fd = files.open_regular_fd(leases_dir, sidecar_name)
    except (Problem, OSError):
        # Missing file raced away after lstat: absent. Anything else is an
        # unusable record.
        try:
            os.lstat(leases_dir / sidecar_name)
        except OSError:
            return Sidecar(status="absent")
        return Sidecar(status="invalid")
    try:
        stamp = os.fstat(fd)
        if (not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.getuid()
                or stat.S_IMODE(stamp.st_mode) != 0o600 or stamp.st_nlink != 1):
            return Sidecar(status="invalid")
        chunks = []
        remaining = SIDECAR_MAX_BYTES + 1
        while remaining > 0:
            piece = os.read(fd, min(8192, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        if len(raw) > SIDECAR_MAX_BYTES:
            return Sidecar(status="invalid")
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return Sidecar(status="invalid")
        if not isinstance(value, dict) or set(value) != {
                "version", "run_id", "pid_namespace",
                "lease_device", "lease_inode"}:
            return Sidecar(status="invalid")
        if (type(value["version"]) is not int
                or value["version"] != SIDECAR_VERSION):
            return Sidecar(status="invalid")
        if value["run_id"] != run_id:
            return Sidecar(status="invalid")
        namespace = value["pid_namespace"]
        if (not isinstance(namespace, str)
                or _NAMESPACE_RE.fullmatch(namespace) is None):
            return Sidecar(status="invalid")
        device, inode = value["lease_device"], value["lease_inode"]
        for number in (device, inode):
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                return Sidecar(status="invalid")
        return Sidecar(status="valid", pid_namespace=namespace,
                       lease_device=device, lease_inode=inode)
    except OSError:
        return Sidecar(status="invalid")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def probe(root: Path, run_id: str, sidecar: Sidecar) -> str:
    """Non-blocking lock verdict for one recorded sidecar. Never pins.

    ``ACQUIRABLE`` only when the file at the lock path is regular, owned,
    0600, single-link, names the recorded (st_dev, st_ino), and
    LOCK_EX|LOCK_NB succeeds. ``HELD`` when the lock is held; anything
    missing, replaced or unreadable is ``INDETERMINATE``. The probe fd is
    always closed in finally (closing drops the probe lock immediately;
    never LOCK_UN, never held across any other call).
    """
    if not isinstance(sidecar, Sidecar) or sidecar.status != "valid":
        return INDETERMINATE
    if (not isinstance(sidecar.lease_device, int)
            or not isinstance(sidecar.lease_inode, int)):
        return INDETERMINATE
    lock_name, _ = _check_names(run_id)
    leases_dir = _leases_dir(root)
    fd = None
    try:
        try:
            fd = files.open_regular_fd(leases_dir, lock_name)
        except (Problem, OSError):
            return INDETERMINATE
        try:
            stamp = os.fstat(fd)
        except OSError:
            return INDETERMINATE
        if (not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.getuid()
                or stat.S_IMODE(stamp.st_mode) != 0o600 or stamp.st_nlink != 1):
            return INDETERMINATE
        if ((stamp.st_dev, stamp.st_ino)
                != (sidecar.lease_device, sidecar.lease_inode)):
            return INDETERMINATE
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return HELD
        except OSError:
            return INDETERMINATE
        return ACQUIRABLE
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def remove_if_released(root: Path, run_id: str) -> bool:
    """Delete sidecar then lease only while holding the recorded lock.

    The caller guarantees the committed row is terminal or absent. With a
    valid sidecar the lock inode must equal the record (a replaced file is
    never deleted) and LOCK_EX|LOCK_NB must succeed. True only if the lock
    file was removed. A sidecar with no lock file is left in place.
    """
    lock_name, sidecar_name = _check_names(run_id)
    leases_dir = _leases_dir(root)
    sidecar = read_sidecar(root, run_id)
    try:
        fd = files.open_regular_fd(leases_dir, lock_name)
    except (Problem, OSError):
        return False
    try:
        try:
            stamp = os.fstat(fd)
        except OSError:
            return False
        if sidecar.status == "valid" and (
                (stamp.st_dev, stamp.st_ino)
                != (sidecar.lease_device, sidecar.lease_inode)):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            os.unlink(leases_dir / sidecar_name)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        try:
            removed = files.unlink_if_same(leases_dir, lock_name,
                                           stamp.st_dev, stamp.st_ino)
        except Problem:
            return False
        return removed
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def held_fd(root: Path, run_id: str) -> int | None:
    """Return this process's held lease fd for one run, if any."""
    with _registry_lock:
        return _registry.get((str(Path(root)), run_id))


def release_held(root: Path, run_id: str) -> None:
    """Close this process's held lease fd. Close only, never LOCK_UN."""
    with _registry_lock:
        fd = _registry.pop((str(Path(root)), run_id), None)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def release_all_held() -> None:
    """Close every lease fd this process holds (test teardown)."""
    with _registry_lock:
        fds = list(_registry.values())
        _registry.clear()
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def discard_created(root: Path, run_id: str) -> None:
    """Owner rollback: unlink sidecar and lock naming the held inode."""
    anchor = str(Path(root))
    with _registry_lock:
        fd = _registry.get((anchor, run_id))
    if fd is not None:
        try:
            owned = os.fstat(fd)
        except OSError:
            owned = None
        leases_dir = _leases_dir(root)
        if owned is not None:
            sidecar = read_sidecar(root, run_id)
            if sidecar.status == "valid" and (
                    (sidecar.lease_device, sidecar.lease_inode)
                    == (owned.st_dev, owned.st_ino)):
                try:
                    os.unlink(leases_dir / f"{run_id}{_SIDECAR_SUFFIX}")
                except OSError:
                    pass
            try:
                files.unlink_if_same(leases_dir, f"{run_id}{_LOCK_SUFFIX}",
                                     owned.st_dev, owned.st_ino)
            except (Problem, OSError):
                pass
    release_held(root, run_id)


def adopt_inherited(root: Path, run_id: str, fd: int) -> None:
    """Adopt a guard-inherited lease fd after verifying its record.

    Requires the fd to be a regular owned file whose (st_dev, st_ino)
    equals the valid sidecar record, re-asserts LOCK_EX|LOCK_NB on the
    shared OFD, marks the fd non-inheritable and registers it. Any
    failure raises ownership-uncertain
    ``"inherited run lease does not match its record"``.
    """
    try:
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
            raise ValueError("bad lease fd")
        stamp = os.fstat(fd)
        if not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.getuid():
            raise ValueError("lease fd is not an owned regular file")
        sidecar = read_sidecar(root, run_id)
        if sidecar.status != "valid" or (
                (stamp.st_dev, stamp.st_ino)
                != (sidecar.lease_device, sidecar.lease_inode)):
            raise ValueError("lease fd does not match its record")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)
    except (ValueError, OSError, Problem) as exc:
        raise Problem(code="ownership-uncertain",
                      message="inherited run lease does not match its record",
                      phase=_PHASE, retryable=False) from exc
    with _registry_lock:
        old = _registry.get((str(Path(root)), run_id))
        _registry[(str(Path(root)), run_id)] = fd
    if old is not None and old != fd:
        try:
            os.close(old)
        except OSError:
            pass


def run_ids(root: Path, *, limit: int = LIST_LIMIT) -> tuple[str, ...]:
    """Sorted unique 32-hex run ids with a .lock or .json entry.

    Empty when the leases dir is absent; never creates anything; at most
    ``limit`` entries.
    """
    leases_dir = _leases_dir(root)
    try:
        entries = os.listdir(leases_dir)
    except OSError:
        return ()
    found: set[str] = set()
    for entry in entries:
        stem = None
        if entry.endswith(_LOCK_SUFFIX):
            stem = entry[: -len(_LOCK_SUFFIX)]
        elif entry.endswith(_SIDECAR_SUFFIX):
            stem = entry[: -len(_SIDECAR_SUFFIX)]
        if stem is not None and _RUN_ID_RE.fullmatch(stem) is not None:
            found.add(stem)
    ordered = sorted(found)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        return tuple(ordered)
    return tuple(ordered[:limit])


__all__ = [
    "LEASES_DIRNAME", "NAMESPACE_UNKNOWN", "NAMESPACE_HOST",
    "SIDECAR_VERSION", "SIDECAR_MAX_BYTES", "LIST_LIMIT",
    "ACQUIRABLE", "HELD", "INDETERMINATE", "Sidecar",
    "namespace_identity", "is_foreign", "create", "read_sidecar", "probe",
    "remove_if_released", "held_fd", "release_held", "release_all_held",
    "discard_created", "adopt_inherited", "run_ids",
]
