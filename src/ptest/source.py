"""Bounded local source evidence. Static reads never execute repository code.

The optional runtime identity is supplied only by a caller with verified native
runner/interpreter/plugin/config/coverage/dependency evidence. Without it we can
fingerprint source, but cannot claim compatibility or enable selection.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import contracts as C
from . import files as safe_files
from .files import create_exclusive, read_regular, validate_private_file
from .selection import _invalid_policy, _matches

_KEY = "input-hmac.key"
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_GIT_BYTES = 32 * 1024 * 1024
_TIMEOUT_S = 10.0
_IDENTITY_PROTOCOL = "ptest-source-v2"
_OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _reason(code: str, message: str, paths: tuple = ()) -> C.Reason:
    return C.Reason(code=code, message=message, paths=paths)


class _Unavailable(Exception):
    def __init__(self, message: str, code: str = "unknown-input"):
        self.reason = _reason(code, message)


@dataclass
class _Scan:
    deadline: float
    captured: int = 0
    read_bytes: int = 0

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("input scan deadline exceeded", "scan-limit")
        return remaining


def ensure_fingerprint_key(domain: C.DomainPaths) -> None:
    """Create a private per-domain HMAC key during execution only."""
    try:
        create_exclusive(domain.root, _KEY, secrets.token_bytes(64))
    except C.Problem as exc:
        if exc.code != "already-exists":
            raise
    if _key(domain) is None:
        raise C.Problem(code="state-unavailable", message="invalid fingerprint key",
                        phase="source", retryable=False)


def _key(domain: C.DomainPaths) -> bytes | None:
    try:
        validate_private_file(domain.root / _KEY)
        key = read_regular(domain.root, _KEY, 65)
    except (C.Problem, OSError):
        return None
    return key if len(key) == 64 else None


def _git(root: Path, scan: _Scan, *argv: str, absent_ok: bool = False) -> bytes | None:
    """Read Git with one cumulative capture/deadline budget and no hooks/fetch."""
    scan.remaining()
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
    command = ("git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", "core.trustctime=true", "-c", "core.checkstat=default", "-c", "core.ignoreStat=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.excludesFile=" + os.devnull,
               "-c", "core.attributesFile=" + os.devnull, "-c", "submodule.recurse=false",
               "-C", os.fspath(root), *argv)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env) as proc:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    if not selector.select(scan.remaining()):
                        raise _Unavailable("input scan deadline exceeded", "scan-limit")
                    piece = os.read(proc.stdout.fileno(), min(65536, _MAX_GIT_BYTES - scan.captured + 1))
                    scan.remaining()
                    scan.captured += len(piece)
                    if scan.captured > _MAX_GIT_BYTES:
                        raise _Unavailable("Git capture byte limit exceeded", "scan-limit")
                    if not piece:
                        break
                    output.extend(piece)
            code = proc.wait(timeout=scan.remaining())
            scan.remaining()
            if absent_ok and code == 1:
                return None
            if code:
                raise _Unavailable("local Git evidence is unavailable")
            return bytes(output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _path(value: bytes) -> str:
    text = value.decode("utf-8", "strict")
    if (not text or text.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in text)
            or any(part in ("", ".", "..") for part in text.split("/"))):
        raise _Unavailable("unsafe Git path")
    return text


def _records(raw: bytes) -> list[bytes]:
    if raw and not raw.endswith(b"\0"):
        raise _Unavailable("truncated Git NUL records")
    records = raw.split(b"\0")[:-1]
    if len(records) > _MAX_FILES * 3:
        raise _Unavailable("Git record count limit exceeded", "scan-limit")
    return records


def _changes(raw: bytes) -> tuple[C.Change, ...]:
    pieces = iter(_records(raw))
    changes = []
    for entry in pieces:
        if len(entry) < 4 or entry[2:3] != b" ":
            raise _Unavailable("malformed Git status record")
        state, path = entry[:2], _path(entry[3:])
        if b"U" in state or state in {b"AA", b"DD"}:
            raise _Unavailable("Git conflict prevents selection")
        if state == b"??":
            changes.append(C.Change(old=None, new=path, kind="untracked"))
        elif b"R" in state or b"C" in state:
            changes.append(C.Change(old=_path(next(pieces, b"")), new=path, kind="renamed"))
        elif b"T" in state or any(value not in b" MAD" for value in state):
            raise _Unavailable("unsupported Git status type")
        elif b"D" in state:
            changes.append(C.Change(old=path, new=None, kind="deleted"))
        else:
            changes.append(C.Change(old=None if b"A" in state else path, new=path, kind="modified"))
    return tuple(changes)


def _committed_changes(root: Path, scan: _Scan, older: str, newer: str | None) -> tuple[C.Change, ...]:
    revisions = (older, newer) if newer is not None else (older,)
    raw = _git(root, scan, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff",
               "--no-textconv", *revisions, "--")
    fields = _records(raw)
    if len(fields) % 2:
        raise _Unavailable("malformed Git range records")
    result = []
    for index in range(0, len(fields), 2):
        header, path = fields[index].split(), _path(fields[index + 1])
        if len(header) != 5 or not header[0].startswith(b":"):
            raise _Unavailable("malformed Git raw diff record")
        old_mode, new_mode = header[0][1:], header[1]
        if old_mode != new_mode and b"000000" not in {old_mode, new_mode}:
            raise _Unavailable("Git file mode changed")
        kind = header[4]
        if kind not in {b"A", b"M", b"D"}:
            raise _Unavailable("unsupported Git range type")
        result.append(C.Change(old=None if kind == b"A" else path, new=None if kind == b"D" else path,
                               kind={b"A": "added", b"M": "modified", b"D": "deleted"}[kind]))
    return tuple(result)


def _revision(root: Path, scan: _Scan, value: str) -> str:
    if not value or value.startswith("-") or "\0" in value:
        raise _Unavailable("invalid local revision")
    oid = _git(root, scan, "rev-parse", "--verify", "--end-of-options", value + "^{commit}").strip()
    if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise _Unavailable("invalid local commit identity")
    return oid.decode("ascii")


def _index(raw: bytes) -> dict[str, int]:
    result = {}
    for record in _records(raw):
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 4:
            raise _Unavailable("malformed Git index record")
        tag, mode, oid, stage = fields
        if stage != b"0":
            raise _Unavailable("Git index conflict prevents selection")
        if tag != b"H":
            raise _Unavailable("Git index flags can hide input changes")
        if mode not in {b"100644", b"100755"}:
            raise _Unavailable("symlink or submodule input is unsupported")
        result[_path(path)] = int(mode, 8)
    if len(result) > _MAX_FILES:
        raise _Unavailable("input file count limit exceeded", "scan-limit")
    return result


@contextmanager
def _opened(root: Path, path: str):
    """Reuse shared no-follow traversal; keep metadata and bytes on one fd."""
    parts = safe_files._split_relative(path)
    root_fd = safe_files._open_dir(root)
    parent_fd = fd = None
    try:
        parent_fd = safe_files._walk_to_parent(root_fd, parts)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        stamp = os.fstat(fd)
        if not stat.S_ISREG(stamp.st_mode):
            raise _Unavailable("input is not a regular file")
        yield fd, stamp
        if _stamp(os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)) != _stamp(os.fstat(fd)):
            raise _Unavailable("input was replaced while fingerprinting")
    finally:
        safe_files._close_quietly(fd)
        if parent_fd is not None and parent_fd != root_fd:
            safe_files._close_quietly(parent_fd)
        safe_files._close_quietly(root_fd)


def _stamp(stamp: os.stat_result) -> tuple:
    return stamp.st_dev, stamp.st_ino, stamp.st_mode, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns


def _fingerprints(key: bytes, root: Path, paths: set[str], scan: _Scan) -> tuple[C.FileFingerprint, ...]:
    if len(paths) > _MAX_FILES:
        raise _Unavailable("input file count limit exceeded", "scan-limit")
    result, stamps = [], {}
    for path in sorted(paths):
        scan.remaining()
        with _opened(root, path) as (fd, before):
            if before.st_size > _MAX_FILE_BYTES or scan.read_bytes + before.st_size > _MAX_TOTAL_BYTES:
                raise _Unavailable("input fingerprint byte limit exceeded", "scan-limit")
            mac = hmac.new(key, digestmod=hashlib.sha256)
            count = 0
            while True:
                scan.remaining()
                piece = os.read(fd, min(65536, _MAX_FILE_BYTES - count + 1,
                                        _MAX_TOTAL_BYTES - scan.read_bytes + 1))
                scan.remaining()
                if not piece:
                    break
                count += len(piece)
                scan.read_bytes += len(piece)
                if count > _MAX_FILE_BYTES or scan.read_bytes > _MAX_TOTAL_BYTES:
                    raise _Unavailable("input fingerprint byte limit exceeded", "scan-limit")
                mac.update(piece)
            after = os.fstat(fd)
            if _stamp(before) != _stamp(after) or count != before.st_size:
                raise _Unavailable("input changed while fingerprinting")
            stamps[path] = _stamp(after)
            result.append(C.FileFingerprint(path=path, digest=mac.hexdigest(),
                                            mode=stat.S_IMODE(after.st_mode), size=count))
    for path, expected in stamps.items():
        scan.remaining()
        with _opened(root, path) as (_, current):
            if _stamp(current) != expected:
                raise _Unavailable("input changed during snapshot")
    return tuple(result)


def _mac(key: bytes, value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def snapshot(domain: C.DomainPaths, config: C.Config, baseline: C.Baseline | None,
             base: str | None, *, runtime_identity: str | None = None) -> C.InputSnapshot:
    """Read supplied state only; unknown runtime evidence prevents compatibility.

    ``runtime_identity`` is a 64-hex digest of *verified* native facts, not a
    caller's guessed runner version. T11 must refresh those facts after setup
    and queue waits. No native probing/import is performed by this function.
    """
    scan = _Scan(time.monotonic() + _TIMEOUT_S)
    head, changes = None, ()
    baseline_head = baseline.head if baseline is not None else None
    try:
        key = _key(domain)
        if key is None:
            raise _Unavailable("fingerprint key is absent or unsafe", "state-unavailable")
        scan.remaining()
        root = config.checkout.root if config.checkout else (config.config_path.parent if config.config_path else None)
        if root is None:
            raise _Unavailable("not a local Git worktree")
        if _invalid_policy(config.selection, config.runner.test_roots):
            raise _Unavailable("invalid selection exclusions", "policy-invalid")
        top = _git(root, scan, "rev-parse", "--show-toplevel").rstrip(b"\n")
        if os.fsdecode(top) != os.fspath(root):
            raise _Unavailable("Git root does not match checkout")
        # Avoid any operation that could fetch missing promisor objects.
        if _git(root, scan, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", absent_ok=True):
            raise _Unavailable("partial clone requires unavailable local evidence")
        git_dir = Path(os.fsdecode(_git(root, scan, "rev-parse", "--absolute-git-dir").rstrip(b"\n")))
        if any(os.path.lexists(git_dir / marker) for marker in _OPERATIONS):
            raise _Unavailable("in-progress Git operation prevents selection")
        head = _revision(root, scan, "HEAD")
        for revision in dict.fromkeys(value for value in (baseline_head, base) if value is not None):
            older = _revision(root, scan, revision)
            if revision == baseline_head and revision != older:
                raise _Unavailable("baseline is not an immutable commit identity", "incompatible-baseline")
            if _git(root, scan, "merge-base", "--is-ancestor", older, head, absent_ok=True) is None:
                raise _Unavailable("baseline or base is not an available ancestor", "incompatible-baseline")
            changes += _committed_changes(root, scan, older, head)
        index_args = ("ls-files", "--stage", "-v", "-z")
        indexed = _git(root, scan, *index_args)
        tracked = _index(indexed)
        status_args = ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
        status = _git(root, scan, *status_args)
        working = tuple(change for change in _changes(status)
                        if not (change.kind == "untracked" and _matches(change.new, config.selection.non_input_outputs)))
        changes = tuple(dict.fromkeys((*changes, *working)))
        if working:
            # Raw diff proves mode safety even for chmod staged in the index.
            _committed_changes(root, scan, head, None)
        deleted = {change.old for change in working if change.kind == "deleted"}
        paths = set(tracked) - deleted
        paths.update(change.new for change in working if change.new is not None)
        ignored = set()
        if config.selection.ignored_inputs:
            raw = _git(root, scan, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
                       *config.selection.ignored_inputs)
            ignored = {_path(path) for path in _records(raw)}
            paths.update(ignored)
        files = _fingerprints(key, root, paths, scan)
        if any(bool(f.mode & 0o111) != bool(tracked[f.path] & 0o111) for f in files if f.path in tracked):
            raise _Unavailable("file mode differs from Git index")
        if (_revision(root, scan, "HEAD") != head or _git(root, scan, *index_args) != indexed
                or _git(root, scan, *status_args) != status):
            raise _Unavailable("Git evidence changed during snapshot")
        environment = [(name, name in os.environ, os.environ.get(name)) for name in sorted(config.selection.environment)]
        external = _mac(key, [environment, [(f.path, f.digest, f.mode, f.size) for f in files if f.path in ignored]])
        identity = [(f.path, f.digest, f.mode, f.size) for f in files]
        digest = _mac(key, [_IDENTITY_PROTOCOL, identity, external])
        compatibility = None
        limitations = ()
        if not isinstance(runtime_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", runtime_identity):
            limitations = (_reason("unknown-input", "verified native runtime identity is unavailable"),)
        else:
            compatibility = _mac(key, [_IDENTITY_PROTOCOL, runtime_identity, external,
                repr(config.runner), repr(config.setup), repr(config.selection), sys.version, sys.executable,
                sys.implementation.name, sys.implementation.cache_tag, platform.system(), platform.machine()])
        scan.remaining()
        return C.InputSnapshot(digest=digest, compatibility=compatibility, head=head,
                               clean=not working, changes=changes, limitations=limitations,
                               files=files, baseline_head=baseline_head)
    except _Unavailable as exc:
        limitation = exc.reason
    except (OSError, C.Problem, UnicodeError, ValueError, subprocess.TimeoutExpired):
        limitation = _reason("unknown-input", "local input cannot be read safely")
    return C.InputSnapshot(digest=None, compatibility=None, head=head, clean=False,
                           changes=changes, limitations=(limitation,), baseline_head=baseline_head)
