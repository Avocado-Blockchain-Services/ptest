"""Bounded local source evidence. Static reads never execute repository code.

The optional runtime identity is supplied only by a caller with verified native
runner/interpreter/plugin/config/coverage/dependency evidence. Without it we can
fingerprint source, but cannot claim compatibility or enable selection.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import configparser
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
import tomllib
from pathlib import Path

from . import contracts as C
from . import files as safe_files
from .files import create_exclusive, read_regular, validate_private_file
from .selection import (_invalid_policy, _matches, in_builtin_cache,
                        non_input_outputs)

_KEY = "input-hmac.key"
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_GIT_BYTES = 32 * 1024 * 1024
_TIMEOUT_S = 10.0
_IDENTITY_PROTOCOL = "ptest-source-v2"
# Compatibility alone moved to v3 in 0.6 (coverage on request): the pytest
# runtime identity stopped counting pytest-cov facts, so a stored 0.5
# baseline must fail compatibility at planning time (one full run) rather
# than fail the post-run identity check (exit 70). Content digests are
# unchanged, so verified-tree reuse of plain full runs survives the upgrade.
_COMPATIBILITY_PROTOCOL = "ptest-compat-v3"
# A rebase is in progress while rebase-merge/ or rebase-apply/ exists, as
# `git status` decides; REBASE_HEAD alone can outlive a finished rebase.
_OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
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


def is_bytecode_path(path: str) -> bool:
    """A ``.pyc``/``.pyo`` file below a ``__pycache__`` directory.

    Python 3 never imports such a file without its source (PEP 3147), so it
    is a build byproduct, never an input. A sourceless ``.pyc`` beside its
    siblings (the legacy layout, which is importable) does not match.
    """
    parts = path.split("/")
    return (len(parts) > 1 and "__pycache__" in parts[:-1]
            and parts[-1].endswith((".pyc", ".pyo")))


def is_bytecode_entry(entry: str) -> bool:
    """A declared path of bytecode shape: a bytecode file or a ``__pycache__``
    directory. ``doctor --fix`` removes such entries from ``ignored_inputs``."""
    parts = entry.rstrip("/").split("/")
    return parts[-1] == "__pycache__" or is_bytecode_path(entry)


def is_env_style(path: str) -> bool:
    """A ``.env`` or ``.env.*`` file: the one name shape whose declaration
    is always ``non_input_outputs`` (a worktree's ``.env`` is a symlink that
    ``ignored_inputs`` refuses). Every message that picks a table uses this."""
    name = path.rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.")


#: Which ``[selection]`` table a declaration belongs in (one source of truth
#: for the end line and doctor). Only ``.env`` files are known to be no test
#: input; declaring a file a test reads as a non-input leads to a false
#: "already verified" later, so everything else names both tables.
_TABLES_ENV = "non_input_outputs"
_TABLES_ANY = ("non_input_outputs for files no test reads (and .env files), "
               "ignored_inputs for files a test reads")


def declare_tables(paths: list[str], *, complete: bool = True) -> str:
    """The table advice for these undeclared gitignored paths. ``complete``
    says ``paths`` is every one of them (an unnamed rest may hold anything)."""
    if complete and paths and all(is_env_style(path) for path in paths):
        return _TABLES_ENV
    return _TABLES_ANY


def _reason(code: str, message: str, paths: tuple = ()) -> C.Reason:
    return C.Reason(code=code, message=message, paths=paths)


def _is_non_input_output(config: C.Config, path: str) -> bool:
    """Non-input outputs (declared and built-in), plus nested node_modules
    for vitest.

    ``non_input_outputs`` entries are literal path prefixes, so the
    generated ``node_modules`` exemption only covers the checkout-root
    environment.  Vite also writes caches under nested ``node_modules``
    segments (for example fixture-local installs), so for vitest
    projects any untracked/ignored path with a ``node_modules``
    directory segment is the same non-input tool output.  Tracked
    files are never filtered here (they stay inputs), pytest projects
    are unaffected, and child-scope ``../`` aliases for declared root
    inputs are matched only against the declared prefixes.
    """
    if _matches(path, non_input_outputs(config.selection,
                                        tuple(config.runner.test_roots))):
        return True
    if in_builtin_cache(config.selection, path):
        return True
    return (config.runner.kind is C.RunnerKind.VITEST
            and not path.startswith("../")
            and "node_modules" in path.split("/"))


class _Unavailable(Exception):
    def __init__(self, message: str, code: str = "unknown-input"):
        self.reason = _reason(code, message)


@dataclass
class _Scan:
    #: ``None`` means no wall clock at all (the names-only listing, whose
    #: answer must not depend on speed).
    deadline: float | None
    captured: int = 0
    read_bytes: int = 0

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
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


def _committed_changes(root: Path, scan: _Scan, older: str, newer: str,
                       pathspec: tuple = ()) -> tuple[C.Change, ...]:
    raw = _git(root, scan, "diff", "--raw", "-z", "--find-renames", "--no-ext-diff",
               "--no-textconv", older, newer, "--", *pathspec)
    return _raw_changes(raw)


def _staged_changes(root: Path, scan: _Scan, head: str,
                    pathspec: tuple = ()) -> tuple[C.Change, ...]:
    raw = _git(root, scan, "diff", "--cached", "--raw", "-z", "--find-renames",
               "--no-ext-diff", "--no-textconv", head, "--", *pathspec)
    return _raw_changes(raw)


def _child_scope(root: Path, top: bytes,
                 selection: C.SelectionPolicy) -> tuple[Path, str, tuple, tuple] | None:
    """Resolve a checkout below its Git root to a scoped evidence plan.

    Returns ``(git_root, prefix, pathspec, triggers)`` where ``git_root`` is
    the enclosing repository, ``prefix`` the child directory relative to it,
    ``pathspec`` the literal Git pathspecs limiting every listing to the
    child plus declared root-level inputs, and ``triggers`` the root-relative
    prefixes those extra pathspecs may yield. Returns None when the checkout
    is unrelated to the Git root.
    """
    git_root = Path(os.fsdecode(top))
    try:
        rel = Path(os.path.realpath(root)).relative_to(os.path.realpath(git_root))
    except (OSError, RuntimeError, ValueError):
        return None
    if not rel.parts:
        return None
    prefix = rel.as_posix()
    triggers = tuple(dict.fromkeys((*selection.full_triggers, ".ptest.toml")))
    # Plain pathspecs: _git locks GIT_LITERAL_PATHSPECS=1, so wildcards in
    # child or trigger names never expand (and the :(literal) magic would
    # itself be read as a literal path). Directory-prefix matching still
    # applies, and _scope_path fails closed on anything unexpected.
    pathspec = (prefix, *triggers)
    return git_root, prefix, pathspec, triggers


def _scope_path(prefix: str | None, triggers: tuple, git_path: str) -> str | None:
    """Express one Git-root-relative path in checkout scope, or None.

    Single-repo scope (``prefix`` None) is the identity. A child maps its
    own directory to child-relative paths and declared root-level inputs to
    ``../`` aliases that can never collide with child-relative paths;
    anything else is unclassifiable.
    """
    if prefix is None:
        return git_path
    if git_path != prefix and git_path.startswith(prefix + "/"):
        return git_path[len(prefix) + 1:]
    if _matches(git_path, triggers):
        return "../" + git_path
    return None


def _scope_git_path(prefix: str | None, path: str) -> str:
    """Invert _scope_path for fingerprint reads (aliases back to Git paths)."""
    if prefix is None or not path.startswith("../"):
        return path if prefix is None else f"{prefix}/{path}"
    return path[3:]


def _scoped_conversion(git_root: Path, scan: _Scan, prefix: str | None,
                       triggers: tuple, raw_changed: set[str]) -> set[str]:
    """Attribute-conversion evidence expressed in checkout scope.

    Single-repo scope delegates directly; a child maps its changed aliases
    back to Git paths, resolves attributes from the Git root, and maps back,
    failing closed on anything outside the scope.
    """
    if prefix is None:
        return _conversion_paths(git_root, scan, raw_changed)
    converted = set()
    git_changed = {_scope_git_path(prefix, path) for path in raw_changed}
    for git_path in _conversion_paths(git_root, scan, git_changed):
        mapped = _scope_path(prefix, triggers, git_path)
        if mapped is None or mapped not in raw_changed:
            raise _Unavailable("Git evidence outside the checkout scope cannot be classified")
        converted.add(mapped)
    return converted


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
        # Regular files (100644/100755) are fingerprinted by content below.
        # A tracked symlink (120000) contributes its mode plus the blob id
        # of its link-target text and is never followed; a gitlink
        # (160000) contributes its mode plus the pinned submodule commit.
        # Any other index mode stays fail-closed.
        if mode not in {b"100644", b"100755", b"120000", b"160000"}:
            raise _Unavailable("unsupported Git index mode")
        if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise _Unavailable("invalid Git index object identity")
        result[_path(path)] = (int(mode, 8), oid.decode("ascii"))
    if len(result) > _MAX_FILES:
        raise _Unavailable("input file count limit exceeded", "scan-limit")
    return result


def _classify(base: Path, path: str, scan: _Scan) -> str:
    """Lstat one checkout-relative path without following any link.

    Returns ``"regular"``, ``"symlink"``, ``"dir"``, or ``"other"``
    (FIFO, socket, device, ...). A missing final component raises
    FileNotFoundError; an intermediate symlink or any other traversal
    hazard raises through the shared no-follow helpers and fails closed
    in the caller. The returned kind is advisory: fingerprinting
    re-checks it, so a mid-run swap can only fail closed, never escape.
    """
    scan.remaining()
    parts = safe_files._split_relative(path)
    root_fd = safe_files._open_dir(base)
    parent_fd = None
    try:
        parent_fd = safe_files._walk_to_parent(root_fd, parts)
        stamp = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    finally:
        if parent_fd is not None and parent_fd != root_fd:
            safe_files._close_quietly(parent_fd)
        safe_files._close_quietly(root_fd)
    mode = stamp.st_mode
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "other"


def _readlink_target(base: Path, path: str) -> bytes:
    """Read one link target without following it (atomic, never escapes)."""
    parts = safe_files._split_relative(path)
    root_fd = safe_files._open_dir(base)
    parent_fd = None
    try:
        parent_fd = safe_files._walk_to_parent(root_fd, parts)
        return os.fsencode(os.readlink(parts[-1], dir_fd=parent_fd))
    finally:
        if parent_fd is not None and parent_fd != root_fd:
            safe_files._close_quietly(parent_fd)
        safe_files._close_quietly(root_fd)


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


def _present_tracked(root: Path, tracked: dict[str, tuple[int, str]],
                     scan: _Scan) -> tuple[set[str], set[str]]:
    """Split index paths into worktree-present and worktree-missing.

    A present symlink is an input fingerprinted by its target, never
    followed; a present directory is an input only where the index pins
    a gitlink, whose commit id is the fingerprint. Any other
    non-regular worktree type (a tracked file replaced by a FIFO, a
    directory where the index holds a regular file or symlink) fails
    closed. A tracked path swapped between regular and symlink stays a
    present input: fingerprinting reports it as a change, not an error.
    """
    present, missing = set(), set()
    for path in sorted(tracked):
        try:
            kind = _classify(root, path, scan)
        except FileNotFoundError:
            missing.add(path)
            continue
        if kind in ("regular", "symlink"):
            present.add(path)
        elif kind == "dir" and tracked[path][0] == 0o160000:
            present.add(path)
        else:
            raise _Unavailable("input is not a regular file")
    return present, missing


def _link_fingerprint(key: bytes, target: bytes, scan: _Scan,
                      constructors: dict, object_format: str) -> tuple[str, str, int]:
    """Fingerprint one symlink by its target text, never following it."""
    if len(target) > _MAX_FILE_BYTES or scan.read_bytes + len(target) > _MAX_TOTAL_BYTES:
        raise _Unavailable("input fingerprint byte limit exceeded", "scan-limit")
    scan.read_bytes += len(target)
    object_hash = constructors[object_format]()
    object_hash.update(f"blob {len(target)}\0".encode("ascii"))
    object_hash.update(target)
    return hmac.new(key, target, hashlib.sha256).hexdigest(), object_hash.hexdigest(), len(target)


def _gitlink_fingerprint(key: bytes, oid: str) -> tuple[str, int]:
    """Fingerprint one gitlink by its pinned commit id (never descended into)."""
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(b"gitlink\0")
    mac.update(oid.encode("ascii"))
    return mac.hexdigest(), len(oid)


def _fingerprints(key: bytes, root: Path, paths: set[str], scan: _Scan,
                  object_format: str,
                  tracked: dict[str, tuple[int, str]] | None = None
                  ) -> tuple[tuple[C.FileFingerprint, ...], dict[str, str], dict[str, str]]:
    """Fingerprint worktree inputs without following any symlink.

    Regular files hash their bytes exactly as before (byte-identical
    digests when no link is present). A symlink — tracked or untracked —
    contributes its readlink target text (a changed target changes the
    digest); a directory where the index pins a gitlink contributes the
    pinned commit id. Anything else where an input is expected fails
    closed. Returns ``(files, object_ids, kinds)``; ``object_ids`` holds
    the Git blob identity for regular files and symlink targets and the
    pinned commit for gitlinks, so the worktree-vs-index comparison
    below reports swaps as changes, never errors.
    """
    if len(paths) > _MAX_FILES:
        raise _Unavailable("input file count limit exceeded", "scan-limit")
    constructors = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}
    if object_format not in constructors:
        raise _Unavailable("unsupported Git object format")
    if tracked is None:
        tracked = {}
    result, stamps = [], {}
    object_ids, kinds, link_targets = {}, {}, {}
    for path in sorted(paths):
        scan.remaining()
        kind = _classify(root, path, scan)
        if kind == "symlink":
            # The target text is the identity; the link is never opened,
            # so an absolute, dangling, or checkout-escaping target can
            # neither redirect a read nor fail the snapshot.
            target = _readlink_target(root, path)
            digest, object_id, size = _link_fingerprint(key, target, scan,
                                                       constructors, object_format)
            kinds[path] = kind
            link_targets[path] = target
            object_ids[path] = object_id
            result.append(C.FileFingerprint(path=path, digest=digest,
                                            mode=0o120000, size=size))
            continue
        if kind == "dir":
            entry = tracked.get(path)
            if entry is None or entry[0] != 0o160000:
                raise _Unavailable("input is not a regular file")
            digest, size = _gitlink_fingerprint(key, entry[1])
            kinds[path] = "gitlink"
            object_ids[path] = entry[1]
            result.append(C.FileFingerprint(path=path, digest=digest,
                                            mode=0o160000, size=size))
            continue
        if kind != "regular":
            raise _Unavailable("input is not a regular file")
        kinds[path] = kind
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
    for path, target in link_targets.items():
        scan.remaining()
        if _classify(root, path, scan) != "symlink" or _readlink_target(root, path) != target:
            raise _Unavailable("input changed during snapshot")
    for path in sorted(kinds):
        if kinds[path] == "gitlink":
            scan.remaining()
            if _classify(root, path, scan) != "dir":
                raise _Unavailable("input changed during snapshot")
    return tuple(result), object_ids, kinds


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


def _pytest_coverage_enabled(config: C.Config) -> bool:
    """True when the ptest config itself enables pytest-cov (``--cov``).

    Only a config-declared coverage run makes coverage data files
    expected tool byproducts; any other run keeps them as real inputs,
    fail-closed.
    """
    if config.runner.kind is not C.RunnerKind.PYTEST:
        return False
    # Declared coverage_args count whether or not this run asked for them:
    # a --cov run writes the data file, and a plain run never reads it.
    controls = (tuple(config.runner.args) + tuple(config.runner.full_args)
                + tuple(config.runner.coverage_args))
    return any(token == "--cov" or token.startswith("--cov=")
               for token in controls)


_COVERAGE_SUFFIX_RE = re.compile(r"(?=[A-Za-z0-9_.-]*[A-Za-z0-9])[A-Za-z0-9_.-]+")
_COVERAGE_CONFIG_FILES = (".coveragerc", "setup.cfg", "tox.ini", "pyproject.toml")
_COVERAGE_CONFIG_MAX_BYTES = 65536


def _coverage_configured_data_file(root: str, name: str) -> str | None:
    """Read one coverage ``data_file`` setting from the checkout, or None.

    Only a safe checkout-relative path is ever returned (no absolute
    paths, no ``..`` segments, no empty segments); anything missing,
    oversized, undecodable, unparsable, or unsafe fails closed to None
    so only the default data file stays tolerated.
    """
    candidate = os.path.join(root, name)
    try:
        with open(candidate, "rb") as handle:
            raw = handle.read(_COVERAGE_CONFIG_MAX_BYTES + 1)
        if len(raw) > _COVERAGE_CONFIG_MAX_BYTES:
            return None
        text = raw.decode("utf-8")
    except (OSError, ValueError):
        return None
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            if not isinstance(data, dict):
                return None
            tool = data.get("tool")
            if not isinstance(tool, dict):
                return None
            coverage = tool.get("coverage")
            if not isinstance(coverage, dict):
                return None
            run = coverage.get("run")
            if not isinstance(run, dict):
                return None
            value = run.get("data_file")
        else:
            parser = configparser.RawConfigParser()
            parser.read_string(text)
            section = "run" if name == ".coveragerc" else "coverage:run"
            if not parser.has_section(section):
                return None
            value = parser.get(section, "data_file", fallback=None)
    except Exception:
        return None
    if (not isinstance(value, str) or not value or "\x00" in value
            or os.path.isabs(value)):
        return None
    segments = value.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in segments):
        return None
    return value.replace("\\", "/")


def _coverage_data_bases(root: str) -> tuple[str, ...]:
    """Coverage data-file basenames tolerated as tool byproducts.

    Always the default ``.coverage`` plus the configured ``data_file``
    when one is safely declared.  Parallel-mode ``<base>.<suffix>``
    files match in :func:`_pytest_tool_generated`.
    """
    bases = [".coverage"]
    for name in _COVERAGE_CONFIG_FILES:
        configured = _coverage_configured_data_file(root, name)
        if configured is not None and configured not in bases:
            bases.append(configured)
    return tuple(bases)


_PYTEST_CACHE_FILES = frozenset({
    ".pytest_cache/.gitignore", ".pytest_cache/CACHEDIR.TAG", ".pytest_cache/README.md",
    ".pytest_cache/v/cache/nodeids", ".pytest_cache/v/cache/lastfailed",
    ".pytest_cache/v/cache/stepwise",
})
_PYTEST_INI_MARKERS = {
    "pytest.ini": None, ".pytest.ini": None, "tox.ini": "[pytest]",
    "setup.cfg": "[tool:pytest]", "pyproject.toml": "[tool.pytest.ini_options]",
}


def _pytest_cache_parents(root: str, included: set[str]) -> tuple[str, ...]:
    """Directory prefixes where pytest keeps ``.pytest_cache``: its rootdir.

    The checkout root always; plus the directory of each included pytest
    config file (rich keeps ``tests/pytest.ini``, so its cache is
    ``tests/.pytest_cache``). A nested cache without such a config stays a
    fingerprinted input.
    """
    parents = {""}
    for relative in included:
        directory, _, name = relative.rpartition("/")
        if not directory or name not in _PYTEST_INI_MARKERS:
            continue
        marker = _PYTEST_INI_MARKERS[name]
        if marker is not None:
            try:
                with open(os.path.join(root, relative), "rb") as handle:
                    if marker.encode() not in handle.read(1024 * 1024):
                        continue
            except OSError:
                continue
        parents.add(directory + "/")
    return tuple(sorted(parents))


def _pytest_tool_generated(path: str, included: set[str],
                           *, coverage_bases: tuple[str, ...] = (),
                           cache_parents: tuple[str, ...] = ("",)) -> str | None:
    """Return the source relation for one exact Pytest tool byproduct.

    The native runner (cacheprovider) and the interpreter (bytecode,
    assertion-rewrite cache) write these while a run executes, so they can
    appear or change between the pre-launch and post-run snapshots of any
    Pytest run, scoped or full; so does the Hypothesis plugin's example
    database under ``.hypothesis/``.  When the ptest config itself enables
    coverage, pytest-cov data files (``.coverage``, parallel-mode
    ``.coverage.<suffix>`` at the checkout root, or the configured
    coverage ``data_file``) are expected byproducts too.  Anything outside
    this exact allowlist — unknown cache names, bytecode without its
    source in the snapshot — returns None and stays a fingerprinted
    input (fail-closed).
    """
    for parent in cache_parents:
        if path.startswith(parent) and path[len(parent):] in _PYTEST_CACHE_FILES:
            return ""
    # Hypothesis keeps its example database under the checkout-root
    # ``.hypothesis/`` and writes it during every run (fullon2 server).
    if path.startswith(".hypothesis/"):
        return ""
    for base in coverage_bases:
        if path == base:
            return ""
        if (path.startswith(base + ".")
                and _COVERAGE_SUFFIX_RE.fullmatch(path[len(base) + 1:])):
            return ""
    match = re.fullmatch(
        r"(?P<parent>(?:[^/]+/)*)__pycache__/(?P<module>[^/]+)\.cpython-(?P<version>[0-9]+)(?:\.opt-[0-9]+|-pytest-[0-9]+(?:\.[0-9A-Za-z]+)*)?\.pyc",
        path,
    )
    if match is None:
        return None
    source = match.group("parent") + match.group("module") + ".py"
    if source not in included:
        return None
    return source


def listing_snapshot(config: C.Config) -> C.InputSnapshot:
    """Classify the checkout's files without ptest state (doctor --fix).

    A fresh random key stands in for the domain key, so no state is read
    or written and the digests match nothing: the result can name paths,
    never prove a tree.
    """
    return snapshot(None, config, None, None)


#: ``undeclared_gitignored`` stops after examining this many directory
#: entries. A count, never a clock: one tree always gives one answer.
LISTING_ENTRY_LIMIT = 50_000

#: Dependency installs the names-only listing never enters: they hold
#: millions of files no project declares one by one. An undeclared one is
#: reported as a single path (the directory), since the snapshot counts it.
LISTING_HEAVY = frozenset({"node_modules", ".venv"})


@dataclass(frozen=True)
class IgnoredListing:
    """Names of undeclared gitignored files, without reading any content."""

    paths: tuple[str, ...]
    #: Tracked and untracked (not ignored) names, which ``declarable`` needs
    #: to avoid proposing a directory that also holds them.
    other: tuple[str, ...]
    examined: int
    truncated: bool
    #: Git is there but could not list (for instance its output went over
    #: ``_MAX_GIT_BYTES``): nothing was checked, which is not "nothing found".
    failed: bool = False

    def as_snapshot(self) -> C.InputSnapshot:
        """The listing in the shape ``selection.declarable`` reads."""
        changes = tuple(C.Change(old=None, new=path, kind="untracked")
                        for path in self.other)
        return C.InputSnapshot(digest=None, compatibility=None, head=None,
                               clean=False, changes=changes, limitations=(),
                               files=())


def undeclared_gitignored(config: C.Config) -> IgnoredListing | None:
    """Gitignored files ptest would count as undeclared inputs, by name.

    The snapshot names the same files but hashes every one of them under a
    wall clock; this lists names only, so the answer cannot depend on speed.
    It prunes ``node_modules``, ``.venv`` and every declared
    ``non_input_outputs``/``ignored_inputs`` entry before walking, drops
    bytecode (only ``*.pyc``/``*.pyo`` below ``__pycache__``) and pytest
    byproducts like the snapshot does, and stops
    at ``LISTING_ENTRY_LIMIT`` examined entries (``truncated``). ``None``
    means this is not a Git worktree; a listing with ``failed`` set means Git
    could not answer.
    """
    root = (config.checkout.root if config.checkout
            else (config.config_path.parent if config.config_path else None))
    if root is None:
        return None
    scan = _Scan(None)
    try:
        if _git(root, scan, "rev-parse", "--git-dir", absent_ok=True) is None:
            return None
    except (_Unavailable, OSError, subprocess.TimeoutExpired):
        return None

    def names(*argv: str) -> list[str]:
        # A name Git prints but ptest refuses (control bytes, bad UTF-8) is
        # skipped; a directory entry keeps its trailing slash.
        result = []
        for item in _records(_git(root, scan, *argv) or b""):
            try:
                result.append(_path(item.rstrip(b"/")) + ("/" if item.endswith(b"/") else ""))
            except (_Unavailable, UnicodeError):
                continue
        return result

    try:
        tracked = set(names("ls-files", "-z"))
        others = names("ls-files", "--others", "--exclude-standard", "-z")
        ignored = names("ls-files", "--others", "--ignored",
                        "--exclude-standard", "--directory", "-z")
    except (_Unavailable, OSError, UnicodeError, ValueError,
            subprocess.TimeoutExpired):
        return IgnoredListing(paths=(), other=(), examined=0, truncated=False,
                              failed=True)
    declared = tuple(config.selection.ignored_inputs)
    pytest_tools = None
    if config.runner.kind is C.RunnerKind.PYTEST:
        pytest_tools = (
            _coverage_data_bases(os.fspath(root))
            if _pytest_coverage_enabled(config) else (),
            _pytest_cache_parents(os.fspath(root), tracked))

    def declared_or_output(path: str) -> bool:
        return _is_non_input_output(config, path) or _matches(path, declared)

    def pruned(path: str) -> bool:
        """Skip ``path`` and everything below it; a dependency install that
        nothing declares is recorded once, by its directory."""
        parts = path.split("/")
        for index, part in enumerate(parts):
            if part in LISTING_HEAVY:
                heavy = "/".join(parts[:index + 1])
                if not declared_or_output(heavy):
                    heavy_found.add(heavy)
                return True
        return declared_or_output(path)

    def counts(path: str) -> bool:
        if pruned(path) or is_bytecode_path(path):
            return False
        if pytest_tools is not None and _pytest_tool_generated(
                path, set(), coverage_bases=pytest_tools[0],
                cache_parents=pytest_tools[1]) is not None:
            return False
        return True

    found: list[str] = []
    heavy_found: set[str] = set()
    examined = 0
    truncated = False
    for entry in ignored:
        if truncated:
            break
        if not entry.endswith("/"):
            examined += 1
            if counts(entry):
                found.append(entry)
            continue
        top = entry.rstrip("/")
        if pruned(top):
            continue
        for directory, subdirs, names in os.walk(root / top):
            relative = Path(directory).relative_to(root).as_posix()
            subdirs.sort()
            keep = []
            for name in sorted(names) + subdirs:
                if name not in subdirs and is_bytecode_path(
                        f"{relative}/{name}"):
                    continue   # never an input, so not worth the budget
                examined += 1
                if examined > LISTING_ENTRY_LIMIT:
                    examined, truncated = LISTING_ENTRY_LIMIT, True
                    break
                path = f"{relative}/{name}"
                if name in subdirs:
                    if pruned(path):
                        continue
                    if os.path.islink(root / path):
                        if counts(path):
                            found.append(path)
                        continue
                    keep.append(name)
                elif counts(path):
                    found.append(path)
            if truncated:
                break
            subdirs[:] = keep
    return IgnoredListing(paths=tuple(sorted(set(found) | heavy_found)),
                          other=tuple(sorted(tracked.union(others))),
                          examined=examined, truncated=truncated)


def snapshot(domain: C.DomainPaths | None, config: C.Config, baseline: C.Baseline | None,
             base: str | None, *, runtime_identity: str | None = None,
             pytest_full_outputs: bool = False) -> C.InputSnapshot:
    """Read supplied state only; unknown runtime evidence prevents compatibility.

    ``runtime_identity`` is a 64-hex digest of *verified* native facts, not a
    caller's guessed runner version. T11 must refresh those facts after setup
    and queue waits. No native probing/import is performed by this function.

    Child scope (monorepo): when the checkout is a subdirectory of its Git
    root, Git evidence runs from the Git root with pathspecs limited to the
    child directory plus root-level inputs the child declares (its
    ``full_triggers`` and the root ``.ptest.toml`` manifest, which declares
    the child's membership; a trigger that also matches another child
    couples that sibling's files into this digest, so triggers should name
    files outside every child). Paths are expressed relative to the child;
    declared root inputs use ``../`` aliases that can never collide with
    child-relative paths. Sibling directories never enter the digest,
    changes, or clean flag, so a dirty sibling does not make the child's
    tree dirty. Commit identity stays the repo HEAD. Anything outside that
    scope that cannot be classified fails closed as unknown-input.

    Links are fingerprinted, never followed. A tracked symlink (mode
    120000) contributes its mode plus the blob identity of its link
    target text; a gitlink (mode 160000) contributes its mode plus the
    pinned submodule commit, and its worktree directory is never
    descended into; a worktree symlink without an index entry
    contributes its readlink target. A changed target changes the
    digest, and a path swapped between regular file and symlink is a
    content change, not an error. No link target — absolute, dangling,
    or checkout-escaping — is ever opened, so a link can neither
    redirect a read outside the checkout nor fail the snapshot.
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
        key = _key(domain) if domain is not None else secrets.token_bytes(64)
        if key is None:
            raise _Unavailable("fingerprint key is absent or unsafe", "state-unavailable")
        scan.remaining()
        root = config.checkout.root if config.checkout else (config.config_path.parent if config.config_path else None)
        if root is None:
            raise _Unavailable("not a local Git worktree")
        if _invalid_policy(config.selection, config.runner.test_roots):
            raise _Unavailable("invalid selection exclusions", "policy-invalid")
        top = _git(root, scan, "rev-parse", "--show-toplevel").rstrip(b"\n")
        git_root, prefix, pathspec, triggers = root, None, (), ()
        if os.fsdecode(top) != os.fspath(root):
            scoped = _child_scope(root, top, config.selection)
            if scoped is None:
                raise _Unavailable("Git root does not match checkout")
            git_root, prefix, pathspec, triggers = scoped

        def _scoped_path(git_path: str) -> str:
            mapped = _scope_path(prefix, triggers, git_path)
            if mapped is None:
                raise _Unavailable("Git evidence outside the checkout scope cannot be classified")
            return mapped

        def _scoped_changes(records: tuple[C.Change, ...]) -> tuple[C.Change, ...]:
            if prefix is None:
                return records
            result = []
            for change in records:
                old = change.old if change.old is None else _scope_path(prefix, triggers, change.old)
                new = change.new if change.new is None else _scope_path(prefix, triggers, change.new)
                if ((change.old is not None and old is None)
                        or (change.new is not None and new is None)):
                    raise _Unavailable("Git evidence outside the checkout scope cannot be classified")
                result.append(C.Change(old=old, new=new, kind=change.kind))
            return tuple(result)

        filter_config = _git(git_root, scan, "config", "--includes", "--get-regexp",
                             r"^filter\.", absent_ok=True)
        if filter_config:
            raise _Unavailable("Git filter configuration prevents static source inspection")
        # Avoid any operation that could fetch missing promisor objects.
        if _git(git_root, scan, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", absent_ok=True):
            raise _Unavailable("partial clone requires unavailable local evidence")
        autocrlf_raw = _git(git_root, scan, "config", "--get", "core.autocrlf", absent_ok=True)
        autocrlf = autocrlf_raw.strip().lower() if autocrlf_raw is not None else b"false"
        if autocrlf not in {b"false", b"true", b"input"}:
            raise _Unavailable("unsupported Git line-ending configuration")
        git_dir = Path(os.fsdecode(_git(git_root, scan, "rev-parse", "--absolute-git-dir").rstrip(b"\n")))
        if any(os.path.lexists(git_dir / marker) for marker in _OPERATIONS):
            raise _Unavailable("in-progress Git operation prevents selection")
        head = _revision(git_root, scan, "HEAD")
        for revision in dict.fromkeys(value for value in (baseline_head, base) if value is not None):
            older = _revision(git_root, scan, revision)
            if revision == baseline_head and revision != older:
                raise _Unavailable("baseline is not an immutable commit identity", "incompatible-baseline")
            if _git(git_root, scan, "merge-base", "--is-ancestor", older, head, absent_ok=True) is None:
                raise _Unavailable("baseline or base is not an available ancestor", "incompatible-baseline")
            changes += _scoped_changes(_committed_changes(git_root, scan, older, head, pathspec))
        index_args = ("ls-files", "--stage", "-v", "-z")
        if pathspec:
            index_args += ("--", *pathspec)
        indexed = _git(git_root, scan, *index_args)
        tracked = {_scoped_path(path): value for path, value in _index(indexed).items()}
        object_format = _git(git_root, scan, "rev-parse", "--show-object-format").strip().decode("ascii")
        staged = _scoped_changes(_staged_changes(git_root, scan, head, pathspec))
        untracked_args = ("ls-files", "--others", "--exclude-standard", "-z")
        if pathspec:
            untracked_args += ("--", *pathspec)
        untracked_raw = _git(git_root, scan, *untracked_args)
        untracked = {_scoped_path(_path(path)) for path in _records(untracked_raw)}
        untracked = {path for path in untracked
                     if not _is_non_input_output(config, path)
                     and not is_bytecode_path(path)}
        ignored_args = ("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
        if pathspec:
            ignored_args += ("--", *pathspec)
        ignored_raw = _git(git_root, scan, *ignored_args)
        ignored_all = set()
        for raw_path in _records(ignored_raw):
            path = _scoped_path(_path(raw_path))
            if (not _is_non_input_output(config, path)
                    and not is_bytecode_path(path)):
                ignored_all.add(path)
        declared_ignored = {path for path in ignored_all
                            if _matches(path, config.selection.ignored_inputs)}
        undeclared_ignored = {path for path in ignored_all
                            if path not in declared_ignored
                              and not _is_non_input_output(config, path)}
        if prefix is None:
            present, deleted = _present_tracked(root, tracked, scan)
        else:
            # Declared root-level inputs live at the Git root, not under the
            # child directory; presence is checked from their own base.
            own_tracked = {path: value for path, value in tracked.items()
                           if not path.startswith("../")}
            outer_tracked = {path[3:]: value for path, value in tracked.items()
                             if path.startswith("../")}
            own_present, own_deleted = _present_tracked(root, own_tracked, scan)
            outer_present, outer_deleted = _present_tracked(git_root, outer_tracked, scan)
            present = own_present | {"../" + path for path in outer_present}
            deleted = own_deleted | {"../" + path for path in outer_deleted}
        candidate_paths = present | untracked | declared_ignored | undeclared_ignored
        generated = set()
        # Tool byproducts are never source inputs, for scoped runs as well
        # as full runs: the coordinator itself (editable install bytecode)
        # and the native xdist controller/workers (cacheprovider entries,
        # bytecode, assertion-rewrite cache) write them mid-run.  The digest
        # protocol below is unchanged; only this classification is shared.
        if config.runner.kind is C.RunnerKind.PYTEST:
            # Coverage data files are expected byproducts only when the
            # ptest config itself enables coverage; any other run keeps
            # them as fingerprinted inputs (fail-closed).
            coverage_bases = (
                _coverage_data_bases(os.fspath(root))
                if _pytest_coverage_enabled(config) else ()
            )
            cache_parents = _pytest_cache_parents(os.fspath(root), present)
            for path in untracked | undeclared_ignored:
                if prefix is not None and path.startswith("../"):
                    # Root-level outputs are outside the child content scope;
                    # only declared root inputs are fingerprinted, never
                    # filtered as tool byproducts.
                    continue
                relation = _pytest_tool_generated(
                    path, candidate_paths, coverage_bases=coverage_bases,
                    cache_parents=cache_parents)
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
        # A gitignored file over the per-file limit is never fingerprinted
        # and never makes the snapshot unavailable (a tool cache such as
        # .mypy_cache/3.12/cache.db is routinely larger). It counts as an
        # undeclared input: the tree is unclean, so no baseline is recorded,
        # and the change carries the size for the refusal text. Tracked and
        # untracked files keep the strict limit in _fingerprints.
        oversized: dict[str, int] = {}
        for path in sorted(declared_ignored | undeclared_ignored):
            location = (git_root / path[3:] if path.startswith("../")
                        else root / path)
            try:
                info = os.lstat(location)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_size > _MAX_FILE_BYTES:
                oversized[path] = info.st_size
        declared_ignored -= oversized.keys()
        undeclared_ignored -= oversized.keys()
        paths = present | untracked | declared_ignored | undeclared_ignored
        kinds: dict[str, str] = {}
        if prefix is None:
            files, object_ids, kinds = _fingerprints(key, root, paths, scan, object_format,
                                                     tracked)
        else:
            own_paths = {path for path in paths if not path.startswith("../")}
            outer_paths = {path[3:] for path in paths if path.startswith("../")}
            fingerprints: list[C.FileFingerprint] = []
            object_ids = {}
            for base, names in ((root, own_paths), (git_root, outer_paths)):
                if not names:
                    continue
                scoped_tracked = own_tracked if base is root else outer_tracked
                scoped_files, scoped_ids, scoped_kinds = _fingerprints(
                    key, base, names, scan, object_format, scoped_tracked)
                for item in scoped_files:
                    alias = item.path if base is root else "../" + item.path
                    fingerprints.append(C.FileFingerprint(path=alias, digest=item.digest,
                                                          mode=item.mode, size=item.size))
                    object_ids[alias] = scoped_ids[item.path]
                    kinds[alias] = scoped_kinds[item.path]
            fingerprints.sort(key=lambda item: item.path)
            files = tuple(fingerprints)
        raw_changed = {path for path in present if object_ids[path] != tracked[path][1]}
        attribute_converted = _scoped_conversion(git_root, scan, prefix, triggers, raw_changed)
        converted = set(attribute_converted)
        if autocrlf != b"false":
            converted.update(raw_changed)
        raw_changes = tuple(C.Change(old=path, new=path,
                                     kind="raw" if path in converted else "modified")
                            for path in sorted(raw_changed))
        # Exec-bit drift is an error only for a regular worktree file
        # whose index entry is also regular. A tracked path swapped
        # between regular and symlink (either direction) is already a
        # content change via the object identity above, not a mode error.
        mode_changed = {f.path for f in files if f.path in tracked
                        and kinds.get(f.path) == "regular"
                        and tracked[f.path][0] in (0o100644, 0o100755)
                        and bool(f.mode & 0o111) != bool(tracked[f.path][0] & 0o111)}
        working = tuple(dict.fromkeys((*staged,
            *(C.Change(old=path, new=None, kind="deleted") for path in sorted(deleted)),
            *raw_changes,
            *(C.Change(old=path, new=path, kind="mode") for path in sorted(mode_changed)),
            *(C.Change(old=None, new=path, kind="untracked") for path in sorted(untracked)),
            *(C.Change(old=None, new=path, kind="ignored", size=oversized.get(path))
              for path in sorted(undeclared_ignored | oversized.keys())))))
        changes = tuple(dict.fromkeys((*changes, *working)))
        if mode_changed:
            raise _Unavailable("file mode differs from Git index")
        reverified_converted = _scoped_conversion(git_root, scan, prefix, triggers, raw_changed)
        if (_revision(git_root, scan, "HEAD") != head or _git(git_root, scan, *index_args) != indexed
                or _git(git_root, scan, *untracked_args) != untracked_raw
                or _git(git_root, scan, *ignored_args) != ignored_raw
                or _git(git_root, scan, "config", "--includes", "--get-regexp",
                        r"^filter\.", absent_ok=True) != filter_config
                or _git(git_root, scan, "config", "--get", "core.autocrlf", absent_ok=True) != autocrlf_raw
                or reverified_converted != attribute_converted):
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
            compatibility = _mac(key, [_IDENTITY_PROTOCOL, _COMPATIBILITY_PROTOCOL,
                runtime_identity, external,
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
