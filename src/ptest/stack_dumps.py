"""Bounded, escaped stack-dump collection/printing and per-attempt cleanup.

One attempt's bridge processes each keep a ``<report>.stack-<pid>`` file
(frozen names from the contracts barrier, owned by T2) with a one-line
``ptest stack dump: ...`` header followed by faulthandler output written on
SIGWINCH. After the guard is reaped, operations prints the non-empty dumps
to stderr and deletes the marker plus every dump of the attempt on every
outcome. Files that fail the private-file check (symlink, foreign owner,
non-regular, wrong mode, hard-linked) are refused: never read and never
deleted through links.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import os
import stat
import sys
from pathlib import Path

from . import contracts as C
from . import files, render

# Frozen post-test-stall names. The getattr default covers this worktree
# before the T2 contracts barrier lands; the bytes are identical.
_MARKER_SUFFIX = getattr(C, "STALL_MARKER_SUFFIX", ".done")
_DUMP_INFIX = getattr(C, "STACK_DUMP_INFIX", ".stack-")
_HEADER_PREFIX = getattr(C, "STACK_DUMP_HEADER_PREFIX", "ptest stack dump: ")


def _stack_dump_pid(report_name: str, name: str) -> int | None:
    """The PID in ``name`` when it names a dump file of ``report_name``."""
    local = getattr(C, "stack_dump_pid", None)
    if callable(local):
        try:
            return local(report_name, name)
        except (TypeError, ValueError):
            return None
    prefix = report_name + _DUMP_INFIX
    if not name.startswith(prefix):
        return None
    digits = name[len(prefix):]
    if (not 1 <= len(digits) <= 10 or not digits.isascii()
            or not digits.isdigit() or digits[0] == "0"):
        return None
    return int(digits)


_MAX_FILES = 32
_MAX_LINES_PER_FILE = 400
_MAX_BYTES_PER_FILE = 32 * 1024
_MAX_BYTES_TOTAL = 128 * 1024
_MAX_DIR_ENTRIES = 4096

_REASONS = ("post-test-stall", "execution-timeout", "guard handoff incomplete")


@dataclass(frozen=True)
class Dump:
    """One validated non-empty dump: escaped lines ready to print."""

    pid: int
    controller: bool
    worker_id: str | None
    lines: tuple[str, ...]
    note: str | None
    size: int


def _candidate_names(report_dir: Path) -> Iterator[str]:
    """Yield directory entry names, examining at most a bounded prefix."""
    try:
        iterator = os.scandir(report_dir)
    except OSError:
        return
    try:
        for index, entry in enumerate(iterator):
            if index >= _MAX_DIR_ENTRIES:
                break
            try:
                yield entry.name
            except OSError:
                return
    except OSError:
        return
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass


def _parse_identity(first: str) -> tuple[bool, str | None]:
    controller = "role=controller" in first
    worker_id: str | None = None
    if not controller:
        for token in first.split():
            if token.startswith("id=") and len(token) > 3:
                worker_id = token[3:]
                break
    return controller, worker_id


def _read_dump(report_dir: Path, name: str, pid: int) -> Dump | None:
    try:
        files.validate_private_file(report_dir / name)
        raw = files.read_regular(report_dir, name, _MAX_BYTES_PER_FILE + 1)
    except (C.Problem, OSError):
        return None
    extra_bytes = len(raw) - _MAX_BYTES_PER_FILE if len(raw) > _MAX_BYTES_PER_FILE else 0
    if extra_bytes:
        raw = raw[:_MAX_BYTES_PER_FILE]
    text = raw.decode("utf-8", errors="replace")
    first, sep, rest = text.partition("\n")
    if not sep or not rest or not first.startswith(_HEADER_PREFIX):
        return None
    controller, worker_id = _parse_identity(first)
    content = rest.split("\n")
    if content and content[-1] == "":
        content.pop()
    extra_lines = len(content) - _MAX_LINES_PER_FILE if len(content) > _MAX_LINES_PER_FILE else 0
    if extra_lines:
        content = content[:_MAX_LINES_PER_FILE]
    lines = (render.terminal_text(first),) + tuple(
        render.terminal_text(line) for line in content)
    note = None
    if extra_lines or extra_bytes:
        note = (f"ptest: stack dump truncated (pid={pid}: +{extra_lines} lines, "
                f"+{extra_bytes} bytes beyond per-file cap)")
    size = len("\n".join(lines).encode("utf-8"))
    return Dump(pid=pid, controller=controller, worker_id=worker_id,
                lines=lines, note=note, size=size)


def collect(report_path: Path) -> tuple[Dump, ...]:
    """Validate and parse one attempt's non-empty dumps, ordered for print."""
    report_dir = Path(report_path).parent
    report_name = Path(report_path).name
    found: list[Dump] = []
    for name in _candidate_names(report_dir):
        pid = _stack_dump_pid(report_name, name)
        if pid is None:
            continue
        dump = _read_dump(report_dir, name, pid)
        if dump is None:
            continue
        found.append(dump)
    found.sort(key=lambda item: (not item.controller, item.pid))
    return tuple(found)


def emit(report_path: Path, reason: str) -> int:
    """Print one attempt's dumps to stderr; return the number printed.

    Prints nothing (no header) when no non-empty dump exists. Never raises
    for filesystem trouble: unreadable entries are refused silently.
    """
    if reason not in _REASONS:
        raise ValueError(f"unknown stack dump reason {reason!r}")
    try:
        dumps = collect(report_path)
    except (C.Problem, OSError):
        return 0
    if not dumps:
        return 0
    shown: list[Dump] = []
    total = 0
    capped_total = False
    for dump in dumps:
        if len(shown) >= _MAX_FILES:
            break
        if total + dump.size > _MAX_BYTES_TOTAL:
            capped_total = True
            break
        shown.append(dump)
        total += dump.size
    try:
        print(f"ptest: stack dumps ({len(shown)} processes) — {reason}",
              file=sys.stderr)
        for dump in shown:
            label = f"ptest: stack dump pid={dump.pid}"
            if dump.controller:
                label += " (controller)"
            elif dump.worker_id is not None:
                label += f" ({render.terminal_text(dump.worker_id)})"
            print(label + ":", file=sys.stderr)
            for line in dump.lines:
                print(line, file=sys.stderr)
            if dump.note is not None:
                print(dump.note, file=sys.stderr)
        if len(dumps) > len(shown):
            print(f"ptest: stack dumps truncated (showing {len(shown)} of "
                  f"{len(dumps)}; capped at {_MAX_FILES} files / "
                  f"{_MAX_BYTES_TOTAL // 1024} KiB total)", file=sys.stderr)
    except OSError:
        return 0
    return len(shown)


def _remove_if_owned_regular(report_dir: Path, name: str) -> None:
    try:
        stamp = os.lstat(report_dir / name)
    except OSError:
        return
    if not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != os.getuid():
        return
    try:
        files.unlink_if_same(report_dir, name, stamp.st_dev, stamp.st_ino)
    except (C.Problem, OSError):
        return


def cleanup(report_path: Path) -> None:
    """Delete one attempt's marker and dumps; refuse anything unsafe.

    Only lstat-regular files owned by this user are unlinked (through
    ``files.unlink_if_same`` on dev/ino). Symlinks, foreign-owned and
    non-regular entries are left alone, never followed. Never raises.
    """
    try:
        report_dir = Path(report_path).parent
        report_name = Path(report_path).name
        names = [name for name in _candidate_names(report_dir)
                 if _stack_dump_pid(report_name, name) is not None]
    except (C.Problem, OSError):
        return
    for name in [report_name + _MARKER_SUFFIX, *names]:
        _remove_if_owned_regular(report_dir, name)


__all__ = ["Dump", "collect", "emit", "cleanup"]
