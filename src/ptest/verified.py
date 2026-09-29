"""Project-wide ledger of green full gates, shared by a project's checkouts.

A checkout's own history proves its baseline; this ledger lets another
checkout of the same project (a worktree, the main checkout after a
fast-forward merge) recognise an identical tree that already passed the
full gate elsewhere. A record is appended only when a full gate passed and
published its baseline, and every record for a tree is dropped as soon as a
full run on the same tree fails, so a flaky pass is never reused. Every
entry point is best-effort: any state problem means "no evidence", never an
error, and nothing here decides a run on its own.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import contracts as C
from . import files

MAX_RECORDS = 64
_MAX_BYTES = 256 * 1024
_DIRECTORY = "projects"
_LEDGER = "verified.json"
_LOCK = "verified.lock"
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Record:
    run_id: str
    head: str
    input_digest: str
    compatibility: str
    policy_digest: str
    runtime_identity: str | None
    scope: str
    checkout_id: str
    root: str
    created_at: str


def _valid(value: object) -> Record | None:
    if not isinstance(value, dict):
        return None
    try:
        record = Record(**{field: value[field] for field in Record.__slots__})
    except (KeyError, TypeError):
        return None
    strings = (record.head, record.compatibility, record.scope, record.root,
               record.created_at)
    if (not all(isinstance(item, str) for item in strings)
            or not isinstance(record.run_id, str) or not _HEX32.fullmatch(record.run_id)
            or not isinstance(record.checkout_id, str)
            or not _HEX32.fullmatch(record.checkout_id)
            or not isinstance(record.input_digest, str)
            or not _HEX64.fullmatch(record.input_digest)
            or not isinstance(record.policy_digest, str)
            or not _HEX64.fullmatch(record.policy_digest)
            or not record.head or len(record.root) > 4096):
        return None
    if record.runtime_identity is not None and (
            not isinstance(record.runtime_identity, str)
            or not _HEX64.fullmatch(record.runtime_identity)):
        return None
    return record


def _project_dir(domain: C.DomainPaths, project_id: str, *, create: bool) -> Path | None:
    if not isinstance(project_id, str) or not _HEX32.fullmatch(project_id):
        return None
    root = Path(domain.root)
    base = root / _DIRECTORY
    project = base / project_id
    if not create:
        return project if project.is_dir() and not project.is_symlink() else None
    if not base.exists():
        files.ensure_private_dir(root, _DIRECTORY)
    if not project.exists():
        files.ensure_private_dir(base, project_id)
    files.validate_private_dir(project)
    return project


def _read(directory: Path, *, strict: bool = False) -> list[Record] | None:
    """Records, [] for no ledger; a read error is [] (or None when strict,
    so a writer never replaces a ledger it could not read)."""
    if not (directory / _LEDGER).exists() and not (directory / _LEDGER).is_symlink():
        return []
    try:
        raw = files.read_regular(directory, _LEDGER, _MAX_BYTES + 1)
    except C.Problem:
        return None if strict else []
    if len(raw) > _MAX_BYTES:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return []
    if not isinstance(data, dict) or data.get("version") != 1:
        return []
    items = data.get("records")
    if not isinstance(items, list):
        return []
    return [record for record in (_valid(item) for item in items) if record is not None]


def _write(directory: Path, records: list[Record]) -> None:
    payload = json.dumps({"version": 1, "records": [asdict(item) for item in records]},
                         sort_keys=True).encode("utf-8")
    files.publish_atomic(directory, _LEDGER, payload)


@contextlib.contextmanager
def _locked(directory: Path):
    fd = os.open(directory / _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def find(domain: C.DomainPaths, project_id: str) -> tuple[Record, ...]:
    """Green full gates of this project, newest first; () on any problem."""
    try:
        directory = _project_dir(domain, project_id, create=False)
        return () if directory is None else tuple(_read(directory))
    except Exception:
        return ()


def record_green(domain: C.DomainPaths, project_id: str, record: Record) -> None:
    """Remember a green full gate; replaces an older record for the same tree."""
    try:
        if _valid(asdict(record)) is None:
            return
        directory = _project_dir(domain, project_id, create=True)
        if directory is None:
            return
        with _locked(directory):
            current = _read(directory, strict=True)
            if current is None:
                return
            kept = [item for item in current
                    if (item.input_digest, item.scope) != (record.input_digest, record.scope)]
            _write(directory, [record, *kept][:MAX_RECORDS])
    except Exception:
        return


def forget(domain: C.DomainPaths, project_id: str, input_digest: str) -> None:
    """Drop every record for a tree whose full run just failed."""
    try:
        directory = _project_dir(domain, project_id, create=False)
        if directory is None:
            return
        with _locked(directory):
            records = _read(directory, strict=True)
            if records is None:
                return
            kept = [item for item in records if item.input_digest != input_digest]
            if len(kept) != len(records):
                _write(directory, kept)
    except Exception:
        return


__all__ = ["MAX_RECORDS", "Record", "find", "forget", "record_green"]
