"""Safe bounded file primitives (Task0 owned).

All shared descriptor-relative reads, exclusive writes, atomic publication
and private-path validation live here. Later tasks consume these helpers and
never duplicate no-follow/exclusive/publication logic.

Every failure raises :class:`Problem` with a stable code: ``unsafe-path``
for traversal, symlink, type or mode violations, ``state-unavailable`` for
absent inputs, ``already-exists`` for refused overwrites and
``invalid-bound`` for bad caller limits.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

from .contracts import Problem

_PHASE = "files"
_READ_CHUNK = 8192
_CONTROL_CHARS = frozenset(chr(code) for code in list(range(0x00, 0x20)) + [0x7F])


def _fail(code: str, message: str) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=False)


def validate_single_name(name: object) -> str:
    """Validate one structural single-component directory or file name.

    Only structure is checked: the name must be a nonempty string without
    path separators, NUL or other C0 controls, and must not be ``.``/``..``.
    Spaces, non-ASCII and brackets are accepted; descriptor-relative
    no-follow traversal remains the real safety boundary.
    """
    if not isinstance(name, str) or not name:
        _fail("unsafe-path", "name must be a nonempty string")
        raise AssertionError("unreachable")
    if name in (".", "..") or "/" in name or "\x00" in name:
        _fail("unsafe-path", f"name {name!r} is not a single component")
        raise AssertionError("unreachable")
    if any(char in _CONTROL_CHARS for char in name):
        _fail("unsafe-path", f"name {name!r} uses control characters")
        raise AssertionError("unreachable")
    return name


def _split_relative(relative: object) -> list:
    if not isinstance(relative, str) or not relative:
        _fail("unsafe-path", "relative path must be a nonempty string")
        raise AssertionError("unreachable")
    if relative.startswith("/"):
        _fail("unsafe-path", "relative path must not be absolute")
        raise AssertionError("unreachable")
    parts = relative.split("/")
    for part in parts:
        validate_single_name(part)
    return parts


def _open_dir(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        _fail("state-unavailable", f"directory {path} does not exist")
        raise AssertionError("unreachable")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail("unsafe-path", f"directory {path} is a symlink")
        _fail("state-unavailable", f"cannot open directory {path}")
        raise AssertionError("unreachable")
    return fd


def _walk_to_parent(root_fd: int, parts: list) -> int:
    """Open every intermediate component; returns the parent fd.

    Intermediate descriptors are closed before return; only the returned
    parent fd (or ``root_fd`` for a single-component path) stays open for
    the caller to close.
    """
    opened = []
    fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=fd)
            except FileNotFoundError:
                _fail("state-unavailable", f"path component {part!r} does not exist")
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    _fail("unsafe-path", f"path component {part!r} is a symlink")
                _fail("unsafe-path", f"path component {part!r} is not a directory")
            stamp = os.fstat(child)
            if not stat.S_ISDIR(stamp.st_mode):
                os.close(child)
                _fail("unsafe-path", f"path component {part!r} is not a directory")
            if fd is not root_fd:
                opened.append(fd)
            else:
                opened.append(None)
            fd = child
        for item in opened:
            if item is not None and item is not fd:
                _close_quietly(item)
        return fd
    except Exception:
        for item in opened:
            if item is not None:
                try:
                    os.close(item)
                except OSError:
                    pass
        if fd is not root_fd and fd not in opened:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def read_regular(root: Path, relative: str, limit: int) -> bytes:
    """Read at most ``limit`` bytes from a regular file below ``root``.

    The final open uses O_NONBLOCK so a FIFO can never wedge the caller;
    the fstat type check then rejects it as non-regular.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        _fail("invalid-bound", "limit must be an int")
    if limit <= 0 or limit > (1 << 31):
        _fail("invalid-bound", "limit is out of range")
    parts = _split_relative(relative)
    root_fd = _open_dir(Path(root))
    parent_fd = None
    fd = None
    try:
        parent_fd = _walk_to_parent(root_fd, parts)
        try:
            fd = os.open(parts[-1],
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except FileNotFoundError:
            _fail("state-unavailable", f"file {relative!r} does not exist")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _fail("unsafe-path", f"file {relative!r} is a symlink")
            _fail("unsafe-path", f"cannot open file {relative!r}")
        stamp = os.fstat(fd)
        if not stat.S_ISREG(stamp.st_mode):
            _fail("unsafe-path", f"file {relative!r} is not a regular file")
        chunks = []
        remaining = limit
        while remaining > 0:
            piece = os.read(fd, min(_READ_CHUNK, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)
    finally:
        _close_quietly(fd)
        if parent_fd is not None and parent_fd is not root_fd:
            _close_quietly(parent_fd)
        _close_quietly(root_fd)


def _check_no_symlink_prefixes(path: Path) -> Path:
    if not isinstance(path, (str, Path)) or not str(path):
        _fail("unsafe-path", "path must be nonempty")
        raise AssertionError("unreachable")
    current = Path(path)
    if not current.is_absolute():
        current = Path(os.getcwd()) / current
    cursor = Path(current.anchor)
    for part in current.parts[1:]:
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            _fail("state-unavailable", f"path {path} does not exist")
            raise AssertionError("unreachable")
        if stat.S_ISLNK(stamp.st_mode):
            _fail("unsafe-path", f"path {path} crosses a symlink")
            raise AssertionError("unreachable")
    return current


def validate_private_dir(path: Path) -> None:
    """Require an owned 0700 directory with no symlink components."""
    current = _check_no_symlink_prefixes(Path(path))
    stamp = os.lstat(current)
    if not stat.S_ISDIR(stamp.st_mode):
        _fail("unsafe-path", f"path {path} is not a directory")
    if stamp.st_uid != os.getuid():
        _fail("unsafe-path", f"path {path} has a foreign owner")
    if stat.S_IMODE(stamp.st_mode) != 0o700:
        _fail("unsafe-path", f"path {path} must be mode 0700")


def validate_private_file(path: Path) -> None:
    """Require an owned 0600 single-link regular file, never a symlink."""
    current = _check_no_symlink_prefixes(Path(path))
    stamp = os.lstat(current)
    if stat.S_ISLNK(stamp.st_mode):
        _fail("unsafe-path", f"path {path} is a symlink")
    if not stat.S_ISREG(stamp.st_mode):
        _fail("unsafe-path", f"path {path} is not a regular file")
    if stamp.st_uid != os.getuid():
        _fail("unsafe-path", f"path {path} has a foreign owner")
    if stat.S_IMODE(stamp.st_mode) != 0o600:
        _fail("unsafe-path", f"path {path} must be mode 0600")
    if stamp.st_nlink != 1:
        _fail("unsafe-path", f"path {path} must have exactly one hard link")


def ensure_shared_dir(parent: Path, name: str) -> Path:
    """Create one absent canonical-parent component at 0700, or accept an
    existing owned non-group/other-writable directory unchanged (no chmod)."""
    validate_single_name(name)
    anchor = Path(parent)
    parent_fd = _open_dir(anchor)
    try:
        stamp = os.fstat(parent_fd)
        if stamp.st_uid != os.getuid():
            _fail("unsafe-path", f"parent {parent} has a foreign owner")
        target = anchor / name
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is None:
            try:
                os.mkdir(target, 0o700)
            except FileExistsError:
                existing = os.lstat(target)
            else:
                os.chmod(target, 0o700)
                created = os.lstat(target)
                if (not stat.S_ISDIR(created.st_mode)
                        or created.st_uid != os.getuid()
                        or stat.S_IMODE(created.st_mode) != 0o700):
                    _fail("unsafe-path", f"directory {target} failed creation checks")
                return target
        if stat.S_ISLNK(existing.st_mode):
            _fail("unsafe-path", f"directory {target} is a symlink")
        if not stat.S_ISDIR(existing.st_mode):
            _fail("unsafe-path", f"path {target} is not a directory")
        if existing.st_uid != os.getuid():
            _fail("unsafe-path", f"directory {target} has a foreign owner")
        if stat.S_IMODE(existing.st_mode) & 0o022:
            _fail("unsafe-path", f"directory {target} is group/other-writable")
        return target
    finally:
        _close_quietly(parent_fd)


def ensure_private_dir(parent: Path, name: str) -> Path:
    """Create one absent NG-exclusive child at 0700; an existing child with
    any other mode fails visibly and is never relaxed."""
    validate_single_name(name)
    anchor = Path(parent)
    parent_fd = _open_dir(anchor)
    try:
        stamp = os.fstat(parent_fd)
        if stamp.st_uid != os.getuid():
            _fail("unsafe-path", f"parent {parent} has a foreign owner")
        target = anchor / name
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is None:
            try:
                os.mkdir(target, 0o700)
            except FileExistsError:
                existing = os.lstat(target)
            else:
                os.chmod(target, 0o700)
                created = os.lstat(target)
                if (not stat.S_ISDIR(created.st_mode)
                        or created.st_uid != os.getuid()
                        or stat.S_IMODE(created.st_mode) != 0o700):
                    _fail("unsafe-path", f"directory {target} failed creation checks")
                return target
        if stat.S_ISLNK(existing.st_mode):
            _fail("unsafe-path", f"directory {target} is a symlink")
        if not stat.S_ISDIR(existing.st_mode):
            _fail("unsafe-path", f"path {target} is not a directory")
        if existing.st_uid != os.getuid():
            _fail("unsafe-path", f"directory {target} has a foreign owner")
        if stat.S_IMODE(existing.st_mode) != 0o700:
            _fail("unsafe-path", f"directory {target} must be mode 0700")
        return target
    finally:
        _close_quietly(parent_fd)


def create_exclusive(root: Path, relative: str, data: bytes, *,
                     private: bool = True) -> Path:
    """Create one new file with O_EXCL|O_NOFOLLOW; parents must already exist."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    if not isinstance(private, bool):
        raise TypeError("private must be bool")
    payload = bytes(data)
    parts = _split_relative(relative)
    root_fd = _open_dir(Path(root))
    parent_fd = None
    fd = None
    created_path = Path(root).joinpath(*parts)
    try:
        parent_fd = _walk_to_parent(root_fd, parts)
        mode = 0o600 if private else 0o644
        try:
            fd = os.open(parts[-1],
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=parent_fd)
        except FileExistsError:
            _fail("already-exists", f"file {relative!r} already exists")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _fail("unsafe-path", f"file {relative!r} is a symlink")
            if exc.errno == errno.ENOENT:
                _fail("state-unavailable",
                      f"parent for file {relative!r} does not exist")
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                _fail("state-unavailable",
                      f"cannot create file {relative!r}: permission denied")
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                _fail("capacity-exceeded",
                      f"cannot create file {relative!r}: no space left")
            _fail("unsafe-path", f"cannot create file {relative!r}")
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            stamp = os.fstat(fd)
            if (not stat.S_ISREG(stamp.st_mode) or stamp.st_nlink != 1
                    or stamp.st_uid != os.getuid()
                    or stat.S_IMODE(stamp.st_mode) != mode):
                _fail("unsafe-path", f"file {relative!r} failed creation checks")
            os.fsync(fd)
        except Exception:
            try:
                os.unlink(parts[-1], dir_fd=parent_fd)
            except OSError:
                pass
            raise
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        return created_path
    finally:
        _close_quietly(fd)
        if parent_fd is not None and parent_fd is not root_fd:
            _close_quietly(parent_fd)
        _close_quietly(root_fd)


def publish_atomic(root: Path, name: str, data: bytes) -> Path:
    """Atomically replace one owned private result file via a unique temp."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    payload = bytes(data)
    anchor = Path(root)
    validate_private_dir(anchor)
    validate_single_name(name)
    root_fd = _open_dir(anchor)
    temp_name = f"{name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    temp_fd = None
    try:
        try:
            temp_fd = os.open(temp_name,
                              os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=root_fd)
        except OSError:
            _fail("state-unavailable", "cannot stage publish temp")
        try:
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                view = view[written:]
            os.fsync(temp_fd)
        except Exception:
            raise
        try:
            existing = os.lstat(anchor / name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                _fail("unsafe-path", f"destination {name!r} is a symlink")
            if (not stat.S_ISREG(existing.st_mode)
                    or existing.st_uid != os.getuid()
                    or existing.st_nlink != 1):
                _fail("unsafe-path", f"destination {name!r} is not a private file")
        os.rename(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        try:
            os.fsync(root_fd)
        except OSError:
            pass
        return anchor / name
    finally:
        _close_quietly(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=root_fd)
        except OSError:
            pass
        _close_quietly(root_fd)
