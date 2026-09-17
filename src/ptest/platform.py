"""Account-local domain and conservative process observations.

This module only resolves and observes.  It never creates the normal domain,
changes a mode, reads process command lines/environments, or sends a signal.
The scheduler owns state creation and revalidates its private state files.
"""
from __future__ import annotations

import ctypes
import errno
import math
import os
import pwd
import re
import stat
import sys
import time
import tomllib
from pathlib import Path

import psutil

from .contracts import DomainPaths, GroupObservation, Problem, ProcessIdentity
from .files import read_regular, validate_private_file

_PHASE = "platform"
_FIXTURE_MARKER_MAX_BYTES = 65536
_DOMAIN_MARKER_NAME = "domain.json"
_MACHINE_CONFIG_NAME = "machine.toml"
_FIXTURE_MARKER_NAME = "fixture-domain.toml"
_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")

_LINUX_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs", "tmpfs"})
_DARWIN_FILESYSTEMS = frozenset({"apfs", "hfs"})
_LINUX_FILESYSTEM_MAGIC = {
    0x0000EF53: "ext4",
    0x58465342: "xfs",
    0x9123683E: "btrfs",
    0x01021994: "tmpfs",
}


class _LinuxStatFs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


class _DarwinStatFs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def _fail(code: str, message: str) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=False)


def _os_kind() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    _fail("unsupported-platform", "platform is outside the Linux/macOS scope")
    raise AssertionError("unreachable")


def _uid_pair() -> tuple[int, int]:
    try:
        uid = os.getuid()
        effective_uid = os.geteuid()
    except (AttributeError, OSError):
        _fail("unsupported-platform", "operating system does not expose account identity")
        raise AssertionError("unreachable")
    if (isinstance(uid, bool) or not isinstance(uid, int)
            or isinstance(effective_uid, bool)
            or not isinstance(effective_uid, int)):
        _fail("unsupported-platform", "account identity is not numeric")
    if uid < 0 or effective_uid < 0 or uid != effective_uid:
        _fail("unsupported-platform", "real and effective account identities differ")
    return uid, effective_uid


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        _fail("state-unavailable", "cannot inspect the account domain path")
        raise AssertionError("unreachable")


def _validate_dir_stamp(path: Path, uid: int, *, mode: int | None = None,
                        parent: bool = False) -> bool:
    stamp = _lstat(path)
    if stamp is None:
        return False
    if stat.S_ISLNK(stamp.st_mode):
        _fail("unsafe-path", "domain path contains a symlink")
    if not stat.S_ISDIR(stamp.st_mode):
        _fail("unsafe-path", "domain path component is not a directory")
    if stamp.st_uid != uid:
        _fail("unsafe-path", "domain path component has a foreign owner")
    if mode is not None:
        if stat.S_IMODE(stamp.st_mode) != mode:
            _fail("unsafe-path", "private coordination directory has an unsafe mode")
    elif parent and stat.S_IMODE(stamp.st_mode) & 0o022:
        _fail("unsafe-path", "canonical domain parent is group/other-writable")
    return True


def _validate_dir_chain(home: Path, components: tuple[str, ...], uid: int,
                        *, final_mode: int | None = None) -> bool:
    """Validate existing components below ``home`` without making any.

    A missing component makes the remainder absent by construction.  Existing
    canonical parents must be owned and non-writable by group/other; the final
    coordination directory, when present, must already be private 0700.
    """
    if not _validate_dir_stamp(home, uid, parent=True):
        _fail("state-unavailable", "account home does not exist")
    current = home
    for index, component in enumerate(components):
        current = current / component
        last = index == len(components) - 1
        if not _validate_dir_stamp(
                current, uid, mode=final_mode if last else None,
                parent=not last or final_mode is None):
            return False
    return True


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while True:
        if _lstat(current) is not None:
            return current
        if current.parent == current:
            return None
        current = current.parent


def _linux_statfs(path: Path) -> str | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statfs = libc.statfs
        statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_LinuxStatFs))
        statfs.restype = ctypes.c_int
        result = _LinuxStatFs()
        if statfs(os.fsencode(path), ctypes.byref(result)) != 0:
            return None
    except (AttributeError, OSError, TypeError):
        return None
    return _LINUX_FILESYSTEM_MAGIC.get(int(result.f_type) & 0xFFFFFFFF)


def _darwin_statfs(path: Path) -> str | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        statfs = libc.statfs
        statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_DarwinStatFs))
        statfs.restype = ctypes.c_int
        result = _DarwinStatFs()
        if statfs(os.fsencode(path), ctypes.byref(result)) != 0:
            return None
        return bytes(result.f_fstypename).split(b"\x00", 1)[0].decode(
            "ascii", "ignore").lower() or None
    except (AttributeError, OSError, TypeError, UnicodeError):
        return None


def _filesystem_type(path: Path) -> str | None:
    """Return the native filesystem name for the nearest existing path."""
    existing = _nearest_existing(Path(path))
    if existing is None:
        return None
    if sys.platform.startswith("linux"):
        return _linux_statfs(existing)
    if sys.platform == "darwin":
        return _darwin_statfs(existing)
    return None


def _validate_local_filesystem(path: Path, system: str) -> None:
    filesystem = _filesystem_type(path)
    allowed = _LINUX_FILESYSTEMS if system == "linux" else _DARWIN_FILESYSTEMS
    if filesystem not in allowed:
        _fail("unsupported-platform", "domain filesystem is not a supported local filesystem")


def _account_home(uid: int) -> Path:
    try:
        record = pwd.getpwuid(uid)
        home = Path(record.pw_dir)
    except (KeyError, OSError, TypeError, AttributeError):
        _fail("state-unavailable", "account home cannot be resolved")
        raise AssertionError("unreachable")
    if not home.is_absolute() or not str(home):
        _fail("unsafe-path", "account home is not an absolute path")
    if not _validate_dir_stamp(home, uid, parent=True):
        _fail("state-unavailable", "account home does not exist")
    return home


def _validate_existing_private_file(path: Path) -> None:
    if _lstat(path) is not None:
        try:
            validate_private_file(path)
        except Problem as exc:
            if exc.code == "state-unavailable":
                _fail("unsafe-path", "domain metadata disappeared during validation")
            raise


def _normal_domain() -> DomainPaths:
    system = _os_kind()
    uid, _ = _uid_pair()
    home = _account_home(uid)
    if system == "linux":
        machine_parent = home / ".config" / "ptest"
        coordination = home / ".local" / "state" / "ptest" / "coordination"
    else:
        machine_parent = home / "Library" / "Application Support" / "ptest"
        coordination = machine_parent / "coordination"

    relative_machine_parent = tuple(machine_parent.relative_to(home).parts)
    _validate_dir_chain(home, relative_machine_parent, uid)
    _validate_local_filesystem(machine_parent, system)
    relative_coordination = tuple(
        coordination.relative_to(home).parts)
    _validate_dir_chain(home, relative_coordination, uid, final_mode=0o700)
    _validate_local_filesystem(coordination, system)

    machine_config = machine_parent / _MACHINE_CONFIG_NAME
    _validate_existing_private_file(machine_config)
    marker = coordination / _DOMAIN_MARKER_NAME
    _validate_existing_private_file(marker)
    return DomainPaths(
        root=coordination,
        machine_config=machine_config,
        ledger=coordination / "coordinator.sqlite3",
        marker=marker,
        fixture=False,
        domain_id=None,
    )


def _check_marker_int(data: dict, name: str, *, lo: int | None = None,
                      hi: int | None = None) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("invalid-config", "fixture marker has an invalid integer")
    if lo is not None and value < lo or hi is not None and value > hi:
        _fail("invalid-config", "fixture marker integer is outside its bound")
    return value


def _fixture_domain(fixture: Path) -> DomainPaths:
    if not fixture.is_absolute():
        _fail("unsafe-path", "fixture domain must be an absolute path")
    system = _os_kind()
    uid, _ = _uid_pair()
    if not _validate_dir_stamp(fixture, uid, mode=0o700):
        _fail("unsafe-path", "fixture domain must already exist")
    _validate_local_filesystem(fixture, system)

    marker = fixture / _FIXTURE_MARKER_NAME
    if _lstat(marker) is None:
        _fail("unsafe-path", "fixture domain marker is missing")
    validate_private_file(marker)
    try:
        raw = read_regular(fixture, _FIXTURE_MARKER_NAME,
                           _FIXTURE_MARKER_MAX_BYTES + 1)
        if len(raw) > _FIXTURE_MARKER_MAX_BYTES:
            _fail("invalid-config", "fixture domain marker exceeds its bound")
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        _fail("invalid-config", "fixture domain marker is not valid TOML")
        raise AssertionError("unreachable")
    if not isinstance(data, dict):
        _fail("invalid-config", "fixture domain marker is not a table")
    expected = {
        "version", "fixture", "max_slots", "max_jobs", "uid",
        "directory_device", "directory_inode", "fixture_id", "workload",
    }
    if set(data) != expected:
        _fail("invalid-config", "fixture domain marker has unknown or missing fields")
    if _check_marker_int(data, "version") != 1:
        _fail("invalid-config", "fixture domain marker version is unsupported")
    if data.get("fixture") is not True:
        _fail("invalid-config", "fixture domain marker is not a fixture")
    _check_marker_int(data, "max_slots", lo=1, hi=4)
    _check_marker_int(data, "max_jobs", lo=1, hi=4)
    if _check_marker_int(data, "uid", lo=0) != uid:
        _fail("unsafe-path", "fixture domain marker has a foreign account")
    stamp = os.lstat(fixture)
    if _check_marker_int(data, "directory_device", lo=0) != stamp.st_dev:
        _fail("unsafe-path", "fixture domain marker has a stale device")
    if _check_marker_int(data, "directory_inode", lo=0) != stamp.st_ino:
        _fail("unsafe-path", "fixture domain marker has a stale inode")
    fixture_id = data.get("fixture_id")
    if not isinstance(fixture_id, str) or not _HEX32_RE.fullmatch(fixture_id):
        _fail("invalid-config", "fixture domain marker has an invalid identity")
    if data.get("workload") != "synthetic-or-miniature":
        _fail("invalid-config", "fixture domain workload is unsupported")
    return DomainPaths(
        root=fixture,
        machine_config=fixture / _MACHINE_CONFIG_NAME,
        ledger=fixture / "coordinator.sqlite3",
        marker=marker,
        fixture=True,
        domain_id=fixture_id,
    )


def domain_paths(fixture: Path | None) -> DomainPaths:
    """Resolve a normal account domain or validate an explicit fixture.

    The normal path is derived solely from the passwd account record and OS;
    HOME, XDG and all ptest environment variables are intentionally ignored.
    """
    if fixture is None:
        return _normal_domain()
    if not isinstance(fixture, Path):
        _fail("unsafe-path", "fixture domain must be a path")
    return _fixture_domain(fixture)


def process_identity(pid: int) -> ProcessIdentity | None:
    """Observe a process birth, real/effective uid and process group.

    A process that disappears, is inaccessible, has split real/effective
    ownership, or changes identity during the observation is indeterminate.
    No command line or environment fields are queried.
    """
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not (sys.platform.startswith("linux") or sys.platform == "darwin")):
        return None
    try:
        process = psutil.Process(pid)
        birth = float(process.create_time())
        uids = process.uids()
        real_uid = uids.real
        effective_uid = uids.effective
        if (isinstance(real_uid, bool) or not isinstance(real_uid, int)
                or isinstance(effective_uid, bool)
                or not isinstance(effective_uid, int)
                or real_uid < 0 or real_uid != effective_uid):
            return None
        pgid = os.getpgid(pid)
        if float(process.create_time()) != birth:
            return None
        return ProcessIdentity(pid=pid, birth=birth, uid=real_uid, pgid=pgid)
    except (psutil.Error, ProcessLookupError, PermissionError, OSError,
            ValueError, TypeError):
        return None


def probe_group(pgid: int) -> GroupObservation:
    """Probe a process group with ``killpg(..., 0)`` and never a real signal."""
    checked_at = time.monotonic()
    if (isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 0
            or not hasattr(os, "killpg")):
        return GroupObservation(exists=None, permission=False,
                                checked_at=checked_at)
    try:
        os.killpg(pgid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return GroupObservation(exists=False, permission=True,
                                    checked_at=checked_at)
        return GroupObservation(exists=None, permission=False,
                                checked_at=checked_at)
    return GroupObservation(exists=True, permission=True, checked_at=checked_at)


def _read_linux_boot_id() -> str | None:
    try:
        with open("/proc/sys/kernel/random/boot_id", "rb") as stream:
            value = stream.read(128).decode("ascii").strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value):
        return None
    return value


def boot_identity() -> str:
    """Return a boot-stable identity, failing closed if it is unavailable."""
    system = _os_kind()
    if system == "linux":
        value = _read_linux_boot_id()
        if value is None:
            _fail("state-unavailable", "kernel boot identity is unavailable")
        return value
    try:
        value = float(psutil.boot_time())
    except (OSError, psutil.Error, TypeError, ValueError):
        _fail("state-unavailable", "kernel boot-time observation is unavailable")
        raise AssertionError("unreachable")
    if not math.isfinite(value) or value < 0:
        _fail("state-unavailable", "kernel boot-time observation is invalid")
    return f"macos:{value:.6f}"
