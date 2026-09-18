"""Static, local-only source identity for conservative selection.

This module deliberately has no project/config discovery.  Callers supply both
the domain and the parsed configuration; missing or unsafe evidence becomes an
``InputSnapshot`` limitation and therefore cannot narrow a run.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import subprocess
import time
from pathlib import Path

from . import contracts as C
from .files import create_exclusive, read_regular, validate_private_file

_KEY = "input-hmac.key"
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_TIMEOUT_S = 10.0


def _reason(code: str, message: str, paths: tuple = ()) -> C.Reason:
    return C.Reason(code=code, message=message, paths=paths)


def ensure_fingerprint_key(domain: C.DomainPaths) -> None:
    """Create the private per-domain HMAC key during execution only."""
    key = domain.root / _KEY
    if key.exists():
        validate_private_file(key)
        if len(read_regular(domain.root, _KEY, 65)) != 64:
            raise C.Problem(code="state-unavailable", message="invalid fingerprint key",
                            phase="source", retryable=False)
        return
    try:
        create_exclusive(domain.root, _KEY, secrets.token_bytes(64))
    except C.Problem as exc:
        if exc.code != "already-exists":
            raise
        validate_private_file(key)
        if len(read_regular(domain.root, _KEY, 65)) != 64:
            raise C.Problem(code="state-unavailable", message="invalid fingerprint key",
                            phase="source", retryable=False)


def _git(root: Path, *argv: str) -> bytes | None:
    try:
        result = subprocess.run(("git", "-C", os.fspath(root), *argv),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=_TIMEOUT_S,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _path(value: bytes) -> str | None:
    try:
        text = value.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    if not text or text.startswith("/") or "\x00" in text or any(part in ("", ".", "..") for part in text.split("/")):
        return None
    return text


def _changes(root: Path) -> tuple[tuple, tuple[C.Reason, ...]]:
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
    if raw is None:
        return (), (_reason("unknown-input", "local Git status is unavailable"),)
    pieces = raw.split(b"\0")
    changes: list[C.Change] = []
    limitations: list[C.Reason] = []
    index = 0
    while index < len(pieces) - 1:
        entry = pieces[index]
        index += 1
        if len(entry) < 4:
            limitations.append(_reason("unknown-input", "malformed Git status record")); break
        state, raw_path = entry[:2], entry[3:]
        path = _path(raw_path)
        if path is None:
            limitations.append(_reason("unknown-input", "unsafe Git path")); break
        if state == b"!!":
            changes.append(C.Change(old=None, new=path, kind="ignored")); continue
        if state == b"??":
            changes.append(C.Change(old=None, new=path, kind="untracked")); continue
        old = None
        if b"R" in state or b"C" in state:
            if index >= len(pieces):
                limitations.append(_reason("unknown-input", "truncated rename record")); break
            old = _path(pieces[index]); index += 1
            if old is None:
                limitations.append(_reason("unknown-input", "unsafe renamed Git path")); break
        if b"U" in state or state == b"AA":
            limitations.append(_reason("unknown-input", "Git conflict prevents selection", (path,))); continue
        kind = "renamed" if old else ("deleted" if b"D" in state else "modified")
        changes.append(C.Change(old=old, new=path, kind=kind))
    return tuple(changes), tuple(limitations)


def _committed_changes(root: Path, older: str, newer: str) -> tuple[tuple, tuple[C.Reason, ...]]:
    raw = _git(root, "diff", "--name-status", "-z", "--find-renames", f"{older}..{newer}")
    if raw is None:
        return (), (_reason("unknown-input", "Git range is unavailable"),)
    result: list[C.Change] = []
    fields = raw.split(b"\0"); index = 0
    while index < len(fields) - 1:
        status = fields[index]; index += 1
        if not status:
            continue
        if index >= len(fields):
            return (), (_reason("unknown-input", "truncated Git range record"),)
        if status.startswith((b"R", b"C")):
            old = _path(fields[index]); new = _path(fields[index + 1]) if index + 1 < len(fields) else None
            index += 2
            if old is None or new is None:
                return (), (_reason("unknown-input", "unsafe rename range path"),)
            result.append(C.Change(old=old, new=new, kind="renamed")); continue
        path = _path(fields[index]); index += 1
        if path is None or status[:1] not in (b"A", b"M", b"D", b"T"):
            return (), (_reason("unknown-input", "unsupported Git range record"),)
        result.append(C.Change(old=path if status[:1] != b"A" else None,
                               new=None if status[:1] == b"D" else path,
                               kind="deleted" if status[:1] == b"D" else "modified"))
    return tuple(result), ()


def _ancestor(root: Path, older: str, newer: str) -> bool:
    try:
        result = subprocess.run(("git", "-C", os.fspath(root), "merge-base", "--is-ancestor", older, newer),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _key(domain: C.DomainPaths) -> bytes | None:
    try:
        validate_private_file(domain.root / _KEY)
        key = read_regular(domain.root, _KEY, 65)
    except C.Problem:
        return None
    return key if len(key) == 64 else None


def _digest(key: bytes, root: Path, paths: tuple[str, ...]) -> tuple[str | None, tuple[C.FileFingerprint, ...], tuple[C.Reason, ...]]:
    started = time.monotonic(); total = 0; files: list[C.FileFingerprint] = []; mac = hmac.new(key, digestmod=hashlib.sha256)
    for path in paths:
        if len(files) >= _MAX_FILES or time.monotonic() - started > _TIMEOUT_S:
            return None, (), (_reason("unknown-input", "input fingerprint limit exceeded"),)
        try:
            data = read_regular(root, path, _MAX_FILE_BYTES + 1)
        except C.Problem:
            return None, (), (_reason("unknown-input", "input cannot be read safely", (path,)),)
        if len(data) > _MAX_FILE_BYTES or total + len(data) > _MAX_TOTAL_BYTES:
            return None, (), (_reason("unknown-input", "input fingerprint byte limit exceeded", (path,)),)
        total += len(data); stamp = (root / path).stat()
        digest = hashlib.sha256(data).hexdigest()
        files.append(C.FileFingerprint(path=path, digest=digest, mode=stamp.st_mode & 0o7777, size=len(data)))
        mac.update(path.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\0")
    return mac.hexdigest(), tuple(files), ()


def snapshot(domain: C.DomainPaths, config: C.Config, baseline: C.Baseline | None,
             base: str | None) -> C.InputSnapshot:
    """Read only local Git and the supplied domain key; never create state."""
    key = _key(domain)
    if key is None:
        return C.InputSnapshot(digest=None, compatibility=None, head=None, clean=False,
                               limitations=(_reason("state-unavailable", "fingerprint key is absent"),))
    root = config.checkout.root if config.checkout is not None else (config.config_path.parent if config.config_path else None)
    if root is None or _git(root, "rev-parse", "--is-inside-work-tree") != b"true\n":
        return C.InputSnapshot(digest=None, compatibility=None, head=None, clean=False,
                               limitations=(_reason("unknown-input", "not a local Git worktree"),))
    head_raw = _git(root, "rev-parse", "HEAD")
    if head_raw is None:
        return C.InputSnapshot(digest=None, compatibility=None, head=None, clean=False,
                               limitations=(_reason("unknown-input", "Git HEAD is unavailable"),))
    if baseline is not None and (baseline.compatibility == "" or not _ancestor(root, baseline.head, head_raw.decode().strip())):
        return C.InputSnapshot(digest=None, compatibility=None, head=head_raw.decode().strip(), clean=False,
                               limitations=(_reason("incompatible-baseline", "baseline is unavailable or not an ancestor"),))
    committed: tuple = ()
    limits: tuple[C.Reason, ...] = ()
    if baseline is not None:
        committed, limits = _committed_changes(root, baseline.head, head_raw.decode().strip())
    if not limits and base is not None:
        if not _ancestor(root, base, head_raw.decode().strip()):
            limits = (_reason("unknown-input", "explicit base is unavailable or not an ancestor"),)
        else:
            extra, limits = _committed_changes(root, base, head_raw.decode().strip())
            committed = tuple({(item.old, item.new, item.kind): item for item in (*committed, *extra)}.values())
    working, working_limits = _changes(root)
    limits = limits or working_limits
    changes = tuple({(item.old, item.new, item.kind): item for item in (*committed, *working)}.values())
    if limits:
        return C.InputSnapshot(digest=None, compatibility=None, head=head_raw.decode().strip(), clean=False,
                               changes=changes, limitations=limits)
    listed = _git(root, "ls-files", "-z")
    if listed is None:
        return C.InputSnapshot(digest=None, compatibility=None, head=head_raw.decode().strip(), clean=False,
                               changes=changes, limitations=(_reason("unknown-input", "Git index is unavailable"),))
    deleted = {change.old for change in changes if change.kind == "deleted" and change.old is not None}
    tracked = tuple(path for item in listed.split(b"\0") if item for path in (_path(item),)
                    if path is not None and path not in deleted)
    changed_existing = tuple(path for change in changes for path in (change.old, change.new)
                             if path is not None and (root / path).is_file())
    paths = tuple(dict.fromkeys((*tracked, *changed_existing)))
    digest, files, limits = _digest(key, root, paths)
    if digest is not None:
        environment = hmac.new(key, digestmod=hashlib.sha256)
        for name in sorted(config.selection.environment):
            environment.update(name.encode("utf-8") + b"\0")
            environment.update(os.environ.get(name, "").encode("utf-8") + b"\0")
        environment.update(digest.encode("ascii"))
        digest = environment.hexdigest()
    policy = hashlib.sha256(repr(config.selection).encode()).hexdigest()
    compat = hashlib.sha256((repr(config.runner) + policy).encode()).hexdigest()
    return C.InputSnapshot(digest=digest, compatibility=compat if digest else None,
                           head=head_raw.decode().strip(), clean=not changes,
                           changes=changes, limitations=limits, files=files)
