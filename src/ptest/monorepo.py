"""Strict, explicit dispatch for a v2 monorepo root."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import contracts as C


@dataclass(frozen=True, slots=True)
class MonorepoManifest:
    children: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True, slots=True)
class ChildTarget:
    declaration: str
    directory: Path
    config: C.Config


@dataclass(frozen=True, slots=True)
class RoutedChildRequest:
    target: ChildTarget
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileScope:
    """An explicitly named test file (or ``::`` node id): always runs."""

    typed: str
    local: str


@dataclass(frozen=True, slots=True)
class FolderScope:
    """A directory scope: changed tests under it run (``local`` empty
    means the whole child)."""

    typed: str
    local: str


@dataclass(frozen=True, slots=True)
class SplitScopes:
    target: ChildTarget
    files: tuple[FileScope, ...]
    folders: tuple[FolderScope, ...]


def _problem(message: str, code: str = "invalid-config") -> C.Problem:
    return C.Problem(code=code, message=message, phase="config", retryable=False)


def _safe_segments(value: object) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise _problem("monorepo path is invalid")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise _problem("monorepo path is invalid")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise _problem("monorepo path is invalid")
    if re.match(r"^[A-Za-z]:", value):
        raise _problem("monorepo path is invalid")
    return parts


def parse_monorepo_manifest(raw: bytes, source_path: Path) -> MonorepoManifest:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        raise _problem("project configuration is invalid") from None
    if not isinstance(data, dict) or set(data) != {"version", "monorepo"}:
        raise _problem("project configuration is invalid")
    if type(data.get("version")) is not int or data["version"] != 2:
        raise _problem("project configuration is invalid")
    table = data.get("monorepo")
    if not isinstance(table, dict) or set(table) != {"children"}:
        raise _problem("project configuration is invalid")
    children = table["children"]
    if not isinstance(children, list) or not 1 <= len(children) <= 256:
        raise _problem("project configuration is invalid")
    declarations = tuple("/".join(_safe_segments(item)) for item in children)
    if len(set(declarations)) != len(declarations):
        raise _problem("monorepo children must be unique")
    paths = [tuple(item.split("/")) for item in declarations]
    if any(a == b[:len(a)] or b == a[:len(b)] for i, a in enumerate(paths)
           for b in paths[i + 1:]):
        raise _problem("monorepo children must not overlap")
    return MonorepoManifest(children=declarations, source_path=Path(source_path))


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise _problem("monorepo child is unavailable", "state-unavailable") from None
    except OSError:
        raise _problem("monorepo child cannot be inspected", "state-unavailable") from None


@dataclass(frozen=True, slots=True)
class ChildDiagnosis:
    """Non-throwing doctor-only assessment of one declared child."""

    declaration: str
    kind: str  # "ok" | "missing" | "unsafe" | "invalid-config"
    directory: Path | None
    config: C.Config | None
    problem: C.Problem | None
    config_bytes: int = 0


def diagnose_child(root_dir: Path, declaration: str, deadline: float | None = None,
                   config_allowance: int | None = None) -> ChildDiagnosis:
    """Inspect one declared child without throwing and without side effects.

    This reuses manifest parsing and configuration decoding but never changes
    execution's fail-fast ``preflight_children`` behavior. Reads are
    no-follow, bounded, and confined to the root.
    """
    from .config import _CONFIG_MAX_BYTES, _parse_config
    from .files import read_regular

    root_dir = Path(root_dir)
    def expired():
        return deadline is not None and time.monotonic() >= deadline
    def timed_out(config_bytes: int = 0):
        return ChildDiagnosis(declaration, "deadline", None, None, None, config_bytes)
    if expired():
        return timed_out()
    try:
        root_real = root_dir.resolve(strict=True)
    except OSError:
        return ChildDiagnosis(declaration, "missing", None, None,
                              _problem("monorepo child is unavailable", "state-unavailable"))
    current = root_dir
    for component in declaration.split("/"):
        if expired():
            return timed_out()
        current = current / component
        try:
            stamp = os.lstat(current)
        except FileNotFoundError:
            return ChildDiagnosis(declaration, "missing", None, None,
                                  _problem("monorepo child is unavailable", "state-unavailable"))
        except OSError:
            return ChildDiagnosis(declaration, "missing", None, None,
                                  _problem("monorepo child cannot be inspected", "state-unavailable"))
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            return ChildDiagnosis(declaration, "unsafe", None, None,
                                  _problem(f"monorepo child is unsafe: {declaration}", "unsafe-path"))
    try:
        directory = current.resolve(strict=True)
    except OSError:
        return ChildDiagnosis(declaration, "missing", None, None,
                              _problem("monorepo child is unavailable", "state-unavailable"))
    if directory != root_real and root_real not in directory.parents:
        return ChildDiagnosis(declaration, "unsafe", None, None,
                              _problem(f"monorepo child escapes root: {declaration}", "unsafe-path"))
    config_path = current / ".ptest.toml"
    if expired():
        return timed_out()
    try:
        stamp = os.lstat(config_path)
    except FileNotFoundError:
        return ChildDiagnosis(declaration, "missing", directory, None,
                              _problem("monorepo child config is unavailable", "state-unavailable"))
    except OSError:
        return ChildDiagnosis(declaration, "missing", directory, None,
                              _problem("monorepo child cannot be inspected", "state-unavailable"))
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        # The child directory itself was already no-follow verified above.
        # Retain it so static doctor can inspect safe source without treating
        # an unsafe config entry as a directory escape.
        return ChildDiagnosis(declaration, "unsafe", directory, None,
                              _problem(f"monorepo child config is unsafe: {declaration}", "unsafe-path"))
    if config_allowance is not None:
        # The size observation is sufficient to reject an over-limit config
        # without reading an unaccounted byte or parsing a valid prefix.
        if stamp.st_size > _CONFIG_MAX_BYTES:
            return ChildDiagnosis(declaration, "invalid-config", directory, None,
                                  _problem(f"monorepo child config is invalid: {declaration}"))
        if config_allowance <= 0 or stamp.st_size > config_allowance:
            # Do not read a prefix that cannot be parsed as a complete
            # configuration.  This is an ordinary exhausted scan allowance,
            # never a claim that the child configuration is malformed.
            return ChildDiagnosis(declaration, "budget", directory, None, None)
        read_limit = min(_CONFIG_MAX_BYTES + 1, config_allowance)
    else:
        # Direct callers preserve the existing config-size diagnostic: one
        # extra byte distinguishes an oversized file from an exact-limit one.
        read_limit = _CONFIG_MAX_BYTES + 1
    try:
        raw = read_regular(current, ".ptest.toml", read_limit)
    except (C.Problem, OSError, ValueError):
        return ChildDiagnosis(declaration, "invalid-config", directory, None,
                              _problem(f"monorepo child config is invalid: {declaration}"))
    if expired():
        return timed_out(len(raw))
    if len(raw) > _CONFIG_MAX_BYTES:
        return ChildDiagnosis(declaration, "invalid-config", directory, None,
                              _problem(f"monorepo child config is invalid: {declaration}"),
                              len(raw))
    if config_allowance is not None and len(raw) >= config_allowance:
        # A capped descriptor read cannot distinguish EOF from an append that
        # raced the preceding metadata check.  Preserve the byte debit but do
        # not parse a potentially incomplete configuration prefix.
        return ChildDiagnosis(declaration, "budget", directory, None, None, len(raw))
    try:
        config, _warnings = _parse_config(raw, current, config_path)
    except C.Problem as problem:
        return ChildDiagnosis(declaration, "invalid-config", directory, None, problem, len(raw))
    except (OSError, ValueError):
        return ChildDiagnosis(declaration, "invalid-config", directory, None,
                              _problem(f"monorepo child config is invalid: {declaration}"),
                              len(raw))
    if expired():
        return timed_out(len(raw))
    return ChildDiagnosis(declaration, "ok", directory, config, None, len(raw))


def validate_child_scope(directory: Path, local_scope: str) -> None:
    """Reject a selected child subpath unless every component is a real directory."""
    current = Path(directory)
    for component in local_scope.split("/"):
        current = current / component
        try:
            stamp = os.lstat(current)
        except OSError:
            raise _problem("selected monorepo scope is unavailable", "unsafe-path") from None
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            raise _problem("selected monorepo scope is unsafe", "unsafe-path")


def preflight_children(root_dir: Path, manifest: MonorepoManifest) -> tuple[ChildTarget, ...]:
    from .config import _parse_config

    root_dir = Path(root_dir)
    targets: list[ChildTarget] = []
    root_real = root_dir.resolve(strict=True)
    for declaration in manifest.children:
        current = root_dir
        for component in declaration.split("/"):
            current = current / component
            stamp = _lstat(current)
            if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
                raise _problem(f"monorepo child is unsafe: {declaration}", "unsafe-path")
        directory = current.resolve(strict=True)
        if directory != root_real and root_real not in directory.parents:
            raise _problem(f"monorepo child escapes root: {declaration}", "unsafe-path")
        config_path = current / ".ptest.toml"
        stamp = _lstat(config_path)
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            raise _problem(f"monorepo child config is unsafe: {declaration}", "unsafe-path")
        try:
            raw = config_path.read_bytes()
            config, _warnings = _parse_config(raw, current, config_path)
        except C.Problem:
            raise
        except (OSError, ValueError):
            raise _problem(f"monorepo child config is invalid: {declaration}") from None
        targets.append(ChildTarget(declaration=declaration, directory=current, config=config))
    return tuple(targets)


def _child_test_roots(child: ChildTarget) -> tuple[str, ...]:
    """Configured test roots of one child: the scope of a bare child run."""
    if child.config is None:
        return ()
    return tuple(child.config.runner.test_roots)


def _normalize_scope(scope: str) -> str:
    """Strip ``./`` prefixes and a trailing ``/`` (shell completion)."""
    while scope.startswith("./"):
        scope = scope[2:]
    return scope.rstrip("/")


def route_scopes(scopes: tuple[str, ...], children: tuple[ChildTarget, ...]) -> RoutedChildRequest:
    if not scopes:
        raise _problem("a monorepo scope is required")
    by_name = {child.declaration: child for child in children}
    # Declarations are validated manifest names, safe to show.
    where = " or ".join(f'"{name}/..."' for name in by_name)
    whole = " or ".join(f'"{name}"' for name in by_name)
    selected: str | None = None
    rebased: list[str] = []
    for scope in scopes:
        scope = _normalize_scope(scope)
        try:
            parts = _safe_segments(scope)
        except C.Problem:
            first = next(iter(by_name))
            raise _problem("test paths must be relative to the repository root, "
                           f'without ".." (for example "{first}/tests")') from None
        child_name = parts[0]
        if child_name not in by_name:
            raise _problem(f"name a test path inside a project: {where}, "
                           f'or a whole project: {whole}; '
                           'to run every project use "ptest --full"')
        if selected is not None and selected != child_name:
            raise _problem("run one project at a time: all paths must be "
                           f"inside the same project ({where})")
        selected = child_name
        if len(parts) == 1:
            # A bare child name runs that child's own tests: its
            # configured test roots as a scoped run, never the full
            # gate (which stays `ptest --full` only).
            rebased.extend(_child_test_roots(by_name[child_name]))
        else:
            rebased.append("/".join(parts[1:]))
    assert selected is not None
    return RoutedChildRequest(target=by_name[selected], scopes=tuple(rebased))


def full_folder_example(children: tuple[ChildTarget, ...]) -> str:
    """Example folder scope using the first declared child (never hard-coded)."""
    first = children[0].declaration if children else "project"
    return f"{first}/tests"


def full_scope_message(children: tuple[ChildTarget, ...],
                       *, typed: str | None = None) -> str:
    """Plain-words rejection for a ``--full`` scope that is not a folder.

    A named file (or node id) echoes the user's own scope so the line
    names the real project; anything else shows the first declared child.
    """
    if typed is not None:
        return ("--full takes a folder, not a file; "
                f"`ptest {typed}` runs it")
    return ("--full runs all tests under one folder "
            f"(e.g. `ptest --full {full_folder_example(children)}`); "
            "bare `ptest --full` runs the integrated gate")


def split_scopes(scopes: tuple[str, ...],
                 children: tuple[ChildTarget, ...]) -> SplitScopes:
    """Route scopes to one child, then split files from folders.

    Routing (unknown children, cross-child scopes, unsafe paths) raises
    the same problems as :func:`route_scopes`. A bare child name is a
    folder covering the whole child; a ``::`` node id is always a file;
    otherwise an existing directory under the child is a folder and
    anything else is a file the user named explicitly.
    """
    if not scopes:
        raise _problem("a monorepo scope is required")
    routed = route_scopes(scopes, children)
    target = routed.target
    files: list[FileScope] = []
    folders: list[FolderScope] = []
    for scope in scopes:
        typed = _normalize_scope(scope)
        parts = _safe_segments(typed)
        if len(parts) == 1:
            folders.append(FolderScope(typed=typed, local=""))
            continue
        local = "/".join(parts[1:])
        if "::" in local or not (target.directory / local).is_dir():
            files.append(FileScope(typed=typed, local=local))
        else:
            folders.append(FolderScope(typed=typed, local=local))
    return SplitScopes(target=target, files=tuple(files),
                       folders=tuple(folders))


def execute_full(children: tuple[ChildTarget, ...], execute_child: Callable[[ChildTarget], int]) -> int:
    first_failure = 0
    for child in children:
        code = execute_child(child)
        if code and not first_failure:
            first_failure = code
    return first_failure


_GIT_TIMEOUT_S = 10.0


def _git_blob(root: Path, *argv: str) -> bytes | None:
    """Read one Git command with source.py's no-hook posture, or None."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_LITERAL_PATHSPECS="1")
    command = ("git", "-c", "core.hooksPath=" + os.devnull,
               "-c", "submodule.recurse=false",
               "-C", os.fspath(root), *argv)
    try:
        proc = subprocess.run(command, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=env, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    return proc.stdout


def _repo_path(value: bytes) -> str | None:
    """Validate one NUL-separated Git path (source._path rules), or None."""
    try:
        text = value.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None
    if (not text or text.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in text)
            or any(part in ("", ".", "..") for part in text.split("/"))):
        return None
    return text


def _nul_paths(raw: bytes) -> tuple[str, ...] | None:
    if raw and not raw.endswith(b"\0"):
        return None
    paths = []
    for record in raw.split(b"\0")[:-1]:
        path = _repo_path(record)
        if path is None:
            return None
        paths.append(path)
    return tuple(paths)


def worktree_changed_files(root: Path, base: str | None) -> tuple[str, ...] | None:
    """Repo-relative changed paths vs base, plus uncommitted and untracked.

    The committed range covers ``base..HEAD`` (empty when ``base`` is None,
    i.e. the reference is HEAD itself); staged, unstaged, and untracked
    (non-ignored) files are always included.  Returns None when local Git
    evidence is unavailable or malformed: callers treat that as "every
    child may be affected", the same fail-open full gate the selection
    planner uses for ambiguity.
    """
    root = Path(root)
    if base is not None and (not base or base.startswith("-") or "\0" in base):
        return None
    top = _git_blob(root, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        if Path(os.fsdecode(top.strip())).resolve() != root.resolve():
            return None
    except (OSError, RuntimeError):
        return None
    changed: list[str] = []
    if base is not None:
        committed = _git_blob(root, "diff", "--name-only", "-z",
                              "--no-ext-diff", "--no-textconv",
                              "--no-renames", base, "HEAD", "--")
        if committed is None:
            return None
        paths = _nul_paths(committed)
        if paths is None:
            return None
        changed.extend(paths)
    worktree = _git_blob(root, "diff", "--name-only", "-z", "--no-ext-diff",
                         "--no-textconv", "--no-renames", "HEAD", "--")
    if worktree is None:
        return None
    paths = _nul_paths(worktree)
    if paths is None:
        return None
    changed.extend(paths)
    untracked = _git_blob(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked is None:
        return None
    paths = _nul_paths(untracked)
    if paths is None:
        return None
    changed.extend(paths)
    return tuple(sorted(set(changed)))
