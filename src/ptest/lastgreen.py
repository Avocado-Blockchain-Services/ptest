"""Last-green-run reference: per-project verified points in the state domain.

A bare/``--changed`` run of a project consults the recorded point before
falling back to the branch-base diff: when the previous ``bare``,
``--changed``, ``scoped`` or ``full`` run of that project passed, the
verified point (HEAD commit plus a fingerprint of the dirty working tree
at that moment) is stored as small JSON under the state domain.  The next
bare run diffs committed changes since that commit plus working-tree
differences against the fingerprint, plus the test files that failed in
the last run (pytest ``--lf`` style).  Failing runs never move the point.

This is a cache, never a gate: any unreadable record, a recorded commit
that is not an ancestor of HEAD (rebase/branch switch), or ``--base``
falls back to the branch-base rule, and cache I/O never raises.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from . import monorepo

RECORD_VERSION = 1
RECORD_DIRNAME = "last-green"
MAX_GREEN_FILES = 1024
MAX_HASH_BYTES = 2 * 1024 * 1024
MAX_LAST_FAILED = 512
_SHA_RE = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class GreenRecord:
    """One project's verified point plus its last-failed test files."""

    root: str
    project: str
    commit: str | None
    recorded_at: float
    files: dict
    last_failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Consultation:
    """Reference chosen for one bare/--changed project run."""

    sha: str | None
    label: str
    changed: tuple[str, ...] | None
    reference: str
    green: bool


def _norm_root(value: str | Path) -> str:
    return os.path.realpath(os.fspath(value))


def _key(repo_root: str, project: str) -> str:
    digest = hashlib.sha1(
        (repo_root + "\0" + project).encode("utf-8")).hexdigest()
    return f"green-{digest}.json"


def record_path(domain_root: str | Path, repo_root: str, project: str) -> Path:
    root = _norm_root(repo_root)
    return Path(domain_root) / RECORD_DIRNAME / _key(root, project)


def _valid_sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(char in _SHA_RE for char in value))


def load(domain_root: str | Path, repo_root: str,
         project: str) -> GreenRecord | None:
    """Read one verified point; None when missing or unreadable."""
    root = _norm_root(repo_root)
    try:
        raw = record_path(domain_root, root, project).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    try:
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("root") != root
                or not isinstance(payload.get("project"), str)
                or payload["project"] != project):
            return None
        commit = payload.get("commit")
        if commit is not None and not _valid_sha(commit):
            return None
        recorded_at = payload.get("recorded_at")
        if not isinstance(recorded_at, (int, float)):
            return None
        files = payload.get("files")
        if not isinstance(files, dict):
            return None
        for key, value in files.items():
            if not isinstance(key, str) or (
                    value is not None and not isinstance(value, str)):
                return None
        failed = payload.get("last_failed", ())
        if not isinstance(failed, list) or not all(
                isinstance(item, str) for item in failed):
            return None
        return GreenRecord(root=root, project=project, commit=commit,
                           recorded_at=float(recorded_at),
                           files=dict(files),
                           last_failed=tuple(failed))
    except (AttributeError, TypeError, ValueError):
        return None


def _dump(record: GreenRecord) -> bytes:
    return json.dumps({
        "version": RECORD_VERSION,
        "root": record.root,
        "project": record.project,
        "commit": record.commit,
        "recorded_at": record.recorded_at,
        "files": record.files,
        "last_failed": list(record.last_failed),
    }, sort_keys=True).encode("utf-8")


_COUNTER = itertools.count()


def _ensure_dir(path: Path) -> None:
    """mkdir -p where every created component is private 0700.

    The state domain requires the coordination directory to be 0700, so
    the cache must never materialise it (or any parent) with a wider
    mode when the coordinator has not run yet in this domain.
    """
    missing: list[Path] = []
    current = Path(path)
    while True:
        try:
            stamp = os.lstat(current)
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise
            current = parent
            continue
        except OSError:
            raise
        if not stat.S_ISDIR(stamp.st_mode):
            raise NotADirectoryError(str(current))
        break
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
    os.chmod(path, 0o700)


def _store(domain_root: str | Path, record: GreenRecord) -> None:
    directory = Path(domain_root) / RECORD_DIRNAME
    _ensure_dir(directory)
    name = _key(record.root, record.project)
    tmp = directory / f"{name}.tmp.{os.getpid()}.{next(_COUNTER)}"
    tmp.write_bytes(_dump(record))
    os.chmod(tmp, 0o600)
    os.replace(tmp, directory / name)


def _head(top: Path) -> str | None:
    raw = monorepo._git_blob(top, "rev-parse", "HEAD", "--")
    if raw is None:
        return None
    try:
        # `rev-parse` echoes the `--` path separator on its own line.
        sha = raw.decode("utf-8", "strict").split()[0]
    except (UnicodeDecodeError, IndexError):
        return None
    return sha if _valid_sha(sha) else None


def _under(path: str, prefix: str) -> bool:
    return prefix == "" or path == prefix or path.startswith(prefix + "/")


def _content_hash(top: Path, rel: str) -> str | None:
    """sha256 of working-tree bytes; None when missing, unreadable, or large."""
    try:
        with open(top / rel, "rb") as handle:
            digest = hashlib.sha256()
            remaining = MAX_HASH_BYTES + 1
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                return None
            return digest.hexdigest()
    except OSError:
        return None


def record_pass(domain_root: str | Path, repo_root: str, project: str,
                top: Path) -> None:
    """Store HEAD plus the dirty-tree fingerprint; clears last-failed."""
    try:
        top = Path(top)
        head = _head(top)
        if head is None:
            return
        dirty = monorepo.worktree_changed_files(top, None)
        if dirty is None:
            return
        prefix = project
        scoped = [path for path in dirty if _under(path, prefix)]
        if len(scoped) > MAX_GREEN_FILES:
            return
        files = {path: _content_hash(top, path) for path in scoped}
        _store(domain_root, GreenRecord(
            root=_norm_root(repo_root), project=project, commit=head,
            recorded_at=time.time(), files=files, last_failed=()))
    except Exception:
        return


def record_failure(domain_root: str | Path, repo_root: str, project: str,
                   files: tuple[str, ...]) -> None:
    """Merge failed test files into the project record; keeps the point."""
    try:
        if not files:
            return
        root = _norm_root(repo_root)
        previous = load(domain_root, root, project)
        seen = list(previous.last_failed) if previous is not None else []
        for item in files:
            if isinstance(item, str) and item and item not in seen:
                seen.append(item)
        merged = tuple(seen[:MAX_LAST_FAILED])
        if previous is not None:
            record = GreenRecord(root=previous.root, project=previous.project,
                                 commit=previous.commit,
                                 recorded_at=previous.recorded_at,
                                 files=dict(previous.files),
                                 last_failed=merged)
        else:
            record = GreenRecord(root=root, project=project, commit=None,
                                 recorded_at=time.time(), files={},
                                 last_failed=merged)
        _store(domain_root, record)
    except Exception:
        return


def _committed_since(top: Path, commit: str) -> tuple[str, ...] | None:
    raw = monorepo._git_blob(top, "diff", "--name-only", "-z",
                             "--no-ext-diff", "--no-textconv",
                             "--no-renames", commit, "HEAD", "--")
    if raw is None:
        return None
    return monorepo._nul_paths(raw)


def dirty_vs_fingerprint(top: Path, project: str,
                         record: GreenRecord) -> tuple[str, ...]:
    """Project-relative dirty paths whose content differs from the record."""
    dirty = monorepo.worktree_changed_files(Path(top), None)
    if dirty is None:
        return ()
    prefix = project
    changed: list[str] = []
    for path in dirty:
        if not _under(path, prefix):
            continue
        rel = path if prefix == "" else path[len(prefix) + 1:]
        current = _content_hash(Path(top), path)
        if record.files.get(path) != current:
            changed.append(rel)
    return tuple(changed)


def _is_ancestor(top: Path, commit: str) -> bool:
    return monorepo._git_blob(top, "merge-base", "--is-ancestor",
                              commit, "HEAD", "--") is not None


def _age_text(recorded_at: float) -> str:
    delta = max(0.0, time.time() - recorded_at)
    if delta >= 86400:
        return f"{int(delta // 86400)}d"
    if delta >= 3600:
        return f"{int(delta // 3600)}h"
    if delta >= 60:
        return f"{int(delta // 60)}m"
    return f"{int(delta)}s"


def green_reference(record: GreenRecord) -> str:
    """Start-line head naming a usable green point."""
    assert record.commit is not None
    return (f"changed since last green run ({record.commit[:7]}, "
            f"{_age_text(record.recorded_at)} ago)")


def fallback_reference(label: str, *, explicit_base: bool) -> str:
    """Start-line head for the branch-base rule."""
    if explicit_base:
        return f"changed vs {label}"
    return f"changed vs {label} (no green run yet)"


def _prefix(project: str, rel: str) -> str:
    return rel if project == "" else f"{project}/{rel}"


def consult(domain_root: str | Path, top: Path, project: str, *,
            fallback_sha: str | None, fallback_label: str,
            fallback_changed: tuple[str, ...] | None,
            explicit_base: bool = False) -> Consultation:
    """Choose the changed set for one project: green point or fallback.

    ``fallback_changed`` is repo-relative (today's branch-base rule);
    the returned ``changed`` stays repo-relative for ``impact.plan``.
    """
    top = Path(top)
    record = None
    try:
        record = load(domain_root, _norm_root(top), project)
    except Exception:
        record = None
    failed = record.last_failed if record is not None else ()

    def with_failed(base: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if base is None:
            return None
        extra = [_prefix(project, item) for item in failed
                 if isinstance(item, str) and item]
        return tuple(sorted(set(base) | set(extra)))

    if (explicit_base or record is None or record.commit is None
            or not _is_ancestor(top, record.commit)):
        return Consultation(sha=fallback_sha, label=fallback_label,
                            changed=with_failed(fallback_changed),
                            reference=fallback_reference(
                                fallback_label,
                                explicit_base=explicit_base),
                            green=False)
    try:
        committed = _committed_since(top, record.commit)
        if committed is None:
            raise OSError("committed range unavailable")
        dirty = dirty_vs_fingerprint(top, project, record)
        # A file that was already dirty at the green point and was then
        # committed unchanged (ptest init, then commit .ptest.toml) is not
        # a change since that run.
        names = [path for path in committed if _under(path, project)
                 and not (record.files.get(path) is not None
                          and _content_hash(top, path) == record.files[path])]
        names.extend(_prefix(project, rel) for rel in dirty)
        names.extend(_prefix(project, item) for item in failed
                     if isinstance(item, str) and item)
        return Consultation(sha=record.commit, label=fallback_label,
                            changed=tuple(sorted(set(names))),
                            reference=green_reference(record),
                            green=True)
    except Exception:
        return Consultation(sha=fallback_sha, label=fallback_label,
                            changed=with_failed(fallback_changed),
                            reference=fallback_reference(
                                fallback_label,
                                explicit_base=explicit_base),
                            green=False)
