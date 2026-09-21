"""Bounded local source evidence. Static reads never execute repository code.

The optional runtime identity is supplied only by a caller with verified native
runner/interpreter/plugin/config/coverage/dependency evidence. Without it we can
fingerprint source, but cannot claim compatibility or enable selection.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
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
_CONVERSION_ATTRIBUTES = {"crlf", "eol", "filter", "ident", "text", "working-tree-encoding"}


def _compatibility_runner(config: C.Config) -> str:
    """Bind advanced identity to declared controls, not ptest's worker grant.

    The native advanced bridge currently admits a truthful serial cap even when
    a repository declares more workers. The grant is ptest-owned execution
    state, so it must not make a clean baseline incompatible with the next
    automatic planning snapshot.
    """
    runner = config.runner
    if runner.kind is C.RunnerKind.PYTEST:
        runner = replace(runner, workers=1)
        config = replace(config, runner=runner)
    return repr(config.runner)


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


def _raw_changes(raw: bytes) -> tuple[C.Change, ...]:
    records = _records(raw)
    result = []
    index = 0
    while index < len(records):
        fields = records[index].split()
        index += 1
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise _Unavailable("malformed Git raw diff record")
        old_mode, new_mode, status = fields[0][1:], fields[1], fields[4]
        if old_mode != new_mode and b"000000" not in {old_mode, new_mode}:
            raise _Unavailable("Git file mode changed")
        code = status[:1]
        if code not in {b"A", b"M", b"D", b"R"} or (code == b"R" and not status[1:].isdigit()):
            raise _Unavailable("unsupported Git range type")
        if index >= len(records):
            raise _Unavailable("malformed Git range records")
        old = new = _path(records[index])
        index += 1
        if code == b"R":
            if index >= len(records):
                raise _Unavailable("malformed Git rename record")
            new = _path(records[index])
            index += 1
        result.append(C.Change(old=None if code == b"A" else old,
                               new=None if code == b"D" else new,
                               kind={b"A": "added", b"M": "modified", b"D": "deleted",
                                     b"R": "renamed"}[code]))
    return tuple(result)


def _committed_changes(root: Path, scan: _Scan, older: str, newer: str) -> tuple[C.Change, ...]:
    raw = _git(root, scan, "diff", "--raw", "-z", "--find-renames", "--no-ext-diff",
               "--no-textconv", older, newer, "--")
    return _raw_changes(raw)


def _staged_changes(root: Path, scan: _Scan, head: str) -> tuple[C.Change, ...]:
    raw = _git(root, scan, "diff", "--cached", "--raw", "-z", "--find-renames",
               "--no-ext-diff", "--no-textconv", head, "--")
    return _raw_changes(raw)


def _revision(root: Path, scan: _Scan, value: str) -> str:
    if not value or value.startswith("-") or "\0" in value:
        raise _Unavailable("invalid local revision")
    oid = _git(root, scan, "rev-parse", "--verify", "--end-of-options", value + "^{commit}").strip()
    if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise _Unavailable("invalid local commit identity")
    return oid.decode("ascii")


def _index(raw: bytes) -> dict[str, tuple[int, str]]:
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
        if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise _Unavailable("invalid Git index object identity")
        result[_path(path)] = (int(mode, 8), oid.decode("ascii"))
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


def _present_tracked(root: Path, paths: set[str], scan: _Scan) -> tuple[set[str], set[str]]:
    present, missing = set(), set()
    for path in sorted(paths):
        scan.remaining()
        try:
            with _opened(root, path):
                present.add(path)
        except FileNotFoundError:
            missing.add(path)
    return present, missing


def _fingerprints(key: bytes, root: Path, paths: set[str], scan: _Scan,
                  object_format: str) -> tuple[tuple[C.FileFingerprint, ...], dict[str, str]]:
    if len(paths) > _MAX_FILES:
        raise _Unavailable("input file count limit exceeded", "scan-limit")
    constructors = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}
    if object_format not in constructors:
        raise _Unavailable("unsupported Git object format")
    result, stamps = [], {}
    object_ids = {}
    for path in sorted(paths):
        scan.remaining()
        with _opened(root, path) as (fd, before):
            if before.st_size > _MAX_FILE_BYTES or scan.read_bytes + before.st_size > _MAX_TOTAL_BYTES:
                raise _Unavailable("input fingerprint byte limit exceeded", "scan-limit")
            mac = hmac.new(key, digestmod=hashlib.sha256)
            object_hash = constructors[object_format]()
            object_hash.update(f"blob {before.st_size}\0".encode("ascii"))
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
                object_hash.update(piece)
            after = os.fstat(fd)
            if _stamp(before) != _stamp(after) or count != before.st_size:
                raise _Unavailable("input changed while fingerprinting")
            stamps[path] = _stamp(after)
            object_ids[path] = object_hash.hexdigest()
            result.append(C.FileFingerprint(path=path, digest=mac.hexdigest(),
                                            mode=stat.S_IMODE(after.st_mode), size=count))
    for path, expected in stamps.items():
        scan.remaining()
        with _opened(root, path) as (_, current):
            if _stamp(current) != expected:
                raise _Unavailable("input changed during snapshot")
    return tuple(result), object_ids


def _conversion_paths(root: Path, scan: _Scan, paths: set[str]) -> set[str]:
    """Resolve attributes without asking Git to convert or read file contents."""
    converted = set()
    pending: list[str] = []
    size = 0

    def inspect(chunk: list[str]) -> None:
        raw = _git(root, scan, "check-attr", "-z", "--all", "--", *chunk)
        records = _records(raw)
        if len(records) % 3:
            raise _Unavailable("malformed Git attribute evidence")
        allowed = set(chunk)
        for index in range(0, len(records), 3):
            path = _path(records[index])
            try:
                attribute = records[index + 1].decode("ascii", "strict")
                value = records[index + 2].decode("utf-8", "strict")
            except UnicodeError as exc:
                raise _Unavailable("unsafe Git attribute evidence") from exc
            if path not in allowed:
                raise _Unavailable("unexpected Git attribute path")
            if attribute in _CONVERSION_ATTRIBUTES and value != "unset":
                converted.add(path)

    for path in sorted(paths):
        encoded_size = len(path.encode("utf-8")) + 1
        if pending and (len(pending) >= 256 or size + encoded_size > 32 * 1024):
            inspect(pending)
            pending, size = [], 0
        pending.append(path)
        size += encoded_size
    if pending:
        inspect(pending)
    return converted


def _mac(key: bytes, value: object) -> str:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _pytest_full_generated(path: str, included: set[str]) -> str | None:
    """Return the source relation for one exact Pytest full-run byproduct."""
    if path in {
        ".pytest_cache/.gitignore", ".pytest_cache/CACHEDIR.TAG", ".pytest_cache/README.md",
        ".pytest_cache/v/cache/nodeids", ".pytest_cache/v/cache/lastfailed",
        ".pytest_cache/v/cache/stepwise",
    }:
        return ""
    match = re.fullmatch(
        r"(?P<parent>(?:[^/]+/)*)__pycache__/(?P<module>[^/]+)\.cpython-(?P<version>[0-9]+)(?:\.opt-[0-9]+|-pytest-(?:8\.4\.2|9\.0\.3|9\.1\.0|9\.1\.1))?\.pyc",
        path,
    )
    if match is None:
        return None
    source = match.group("parent") + match.group("module") + ".py"
    if source not in included:
        return None
    return source


def snapshot(domain: C.DomainPaths, config: C.Config, baseline: C.Baseline | None,
             base: str | None, *, runtime_identity: str | None = None,
             pytest_full_outputs: bool = False) -> C.InputSnapshot:
    """Read supplied state only; unknown runtime evidence prevents compatibility.

    ``runtime_identity`` is a 64-hex digest of *verified* native facts, not a
    caller's guessed runner version. T11 must refresh those facts after setup
    and queue waits. No native probing/import is performed by this function.
    """
    if pytest_full_outputs and (
            config.runner.kind is not C.RunnerKind.PYTEST
            or baseline is not None or base is not None or runtime_identity is not None):
        raise C.Problem(
            code="invalid-config",
            message="pytest full output policy requires a Pytest full content snapshot",
            phase="source", retryable=False,
        )
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
        filter_config = _git(root, scan, "config", "--includes", "--get-regexp",
                             r"^filter\.", absent_ok=True)
        if filter_config:
            raise _Unavailable("Git filter configuration prevents static source inspection")
        # Avoid any operation that could fetch missing promisor objects.
        if _git(root, scan, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", absent_ok=True):
            raise _Unavailable("partial clone requires unavailable local evidence")
        autocrlf_raw = _git(root, scan, "config", "--get", "core.autocrlf", absent_ok=True)
        autocrlf = autocrlf_raw.strip().lower() if autocrlf_raw is not None else b"false"
        if autocrlf not in {b"false", b"true", b"input"}:
            raise _Unavailable("unsupported Git line-ending configuration")
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
        object_format = _git(root, scan, "rev-parse", "--show-object-format").strip().decode("ascii")
        staged = _staged_changes(root, scan, head)
        untracked_raw = _git(root, scan, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = {_path(path) for path in _records(untracked_raw)}
        untracked = {path for path in untracked
                     if not _matches(path, config.selection.non_input_outputs)}
        ignored_raw = _git(root, scan, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
        ignored_all = set()
        for raw_path in _records(ignored_raw):
            path = _path(raw_path)
            if not _matches(path, config.selection.non_input_outputs):
                ignored_all.add(path)
        declared_ignored = {path for path in ignored_all
                            if _matches(path, config.selection.ignored_inputs)}
        undeclared_ignored = {path for path in ignored_all
                            if path not in declared_ignored
                              and not _matches(path, config.selection.non_input_outputs)}
        present, deleted = _present_tracked(root, set(tracked), scan)
        candidate_paths = present | untracked | declared_ignored | undeclared_ignored
        generated = set()
        if pytest_full_outputs:
            for path in untracked | undeclared_ignored:
                relation = _pytest_full_generated(path, candidate_paths)
                if relation is None:
                    continue
                # Filtering is allowed only after the same no-follow regular
                # file checks used for ordinary inputs.  This also makes a
                # symlink/FIFO/raced replacement fail closed instead of
                # disappearing merely because it resembles tool output.
                with _opened(root, path):
                    if relation:
                        with _opened(root, relation):
                            pass
                generated.add(path)
        untracked -= generated
        undeclared_ignored -= generated
        paths = present | untracked | declared_ignored | undeclared_ignored
        files, object_ids = _fingerprints(key, root, paths, scan, object_format)
        raw_changed = {path for path in present if object_ids[path] != tracked[path][1]}
        attribute_converted = _conversion_paths(root, scan, raw_changed)
        converted = set(attribute_converted)
        if autocrlf != b"false":
            converted.update(raw_changed)
        raw_changes = tuple(C.Change(old=path, new=path,
                                     kind="raw" if path in converted else "modified")
                            for path in sorted(raw_changed))
        mode_changed = {f.path for f in files if f.path in tracked
                        and bool(f.mode & 0o111) != bool(tracked[f.path][0] & 0o111)}
        working = tuple(dict.fromkeys((*staged,
            *(C.Change(old=path, new=None, kind="deleted") for path in sorted(deleted)),
            *raw_changes,
            *(C.Change(old=path, new=path, kind="mode") for path in sorted(mode_changed)),
            *(C.Change(old=None, new=path, kind="untracked") for path in sorted(untracked)),
            *(C.Change(old=None, new=path, kind="ignored") for path in sorted(undeclared_ignored)))))
        changes = tuple(dict.fromkeys((*changes, *working)))
        if mode_changed:
            raise _Unavailable("file mode differs from Git index")
        if (_revision(root, scan, "HEAD") != head or _git(root, scan, *index_args) != indexed
                or _git(root, scan, "ls-files", "--others", "--exclude-standard", "-z") != untracked_raw
                or _git(root, scan, "ls-files", "--others", "--ignored", "--exclude-standard", "-z") != ignored_raw
                or _git(root, scan, "config", "--includes", "--get-regexp",
                        r"^filter\.", absent_ok=True) != filter_config
                or _git(root, scan, "config", "--get", "core.autocrlf", absent_ok=True) != autocrlf_raw
                or _conversion_paths(root, scan, raw_changed) != attribute_converted):
            raise _Unavailable("Git evidence changed during snapshot")
        environment = [(name, name in os.environ, os.environ.get(name)) for name in sorted(config.selection.environment)]
        external = _mac(key, [environment, [(f.path, f.digest, f.mode, f.size)
                                            for f in files if f.path in declared_ignored]])
        identity = [(f.path, f.digest, f.mode, f.size) for f in files]
        if pytest_full_outputs:
            digest = _mac(key, [_IDENTITY_PROTOCOL,
                                "ptest-pytest-full-content-v1", identity, external])
        else:
            # Preserve the Task 11D digest payload for every default caller;
            # the execution-only domain is deliberately opt-in and separate.
            digest = _mac(key, [_IDENTITY_PROTOCOL, identity, external])
        compatibility = None
        limitations = ()
        if pytest_full_outputs:
            compatibility = None
            limitations = (_reason("unknown-input", "pytest full content identity is execution-only"),)
        elif not isinstance(runtime_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", runtime_identity):
            limitations = (_reason("unknown-input", "verified native runtime identity is unavailable"),)
        else:
            compatibility = _mac(key, [_IDENTITY_PROTOCOL, runtime_identity, external,
                _compatibility_runner(config), repr(config.setup), repr(config.selection), sys.version, sys.executable,
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
