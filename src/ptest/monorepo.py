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
from .selection import _matches


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


def route_scopes(scopes: tuple[str, ...], children: tuple[ChildTarget, ...]) -> RoutedChildRequest:
    if not scopes:
        raise _problem("a monorepo scope is required")
    by_name = {child.declaration: child for child in children}
    # Declarations are validated manifest names, safe to show.
    where = " or ".join(f'"{name}/..."' for name in by_name)
    selected: str | None = None
    rebased: list[str] = []
    for scope in scopes:
        # Shell completion adds a trailing "/" and people type "./"; both
        # name the same path.
        while scope.startswith("./"):
            scope = scope[2:]
        scope = scope.rstrip("/")
        try:
            parts = _safe_segments(scope)
        except C.Problem:
            raise _problem("test paths must be relative to the repository root, "
                           'without ".." (for example "api/tests")') from None
        child_name = parts[0]
        if len(parts) < 2 or child_name not in by_name:
            raise _problem(f"name a test path inside a project: {where}; "
                           'to run every project use "ptest --full"')
        if selected is not None and selected != child_name:
            raise _problem("run one project at a time: all paths must be "
                           f"inside the same project ({where})")
        selected = child_name
        rebased.append("/".join(parts[1:]))
    assert selected is not None
    return RoutedChildRequest(target=by_name[selected], scopes=tuple(rebased))


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
                              "--find-renames", base, "HEAD", "--")
        if committed is None:
            return None
        paths = _nul_paths(committed)
        if paths is None:
            return None
        changed.extend(paths)
    worktree = _git_blob(root, "diff", "--name-only", "-z", "--no-ext-diff",
                         "--no-textconv", "--find-renames", "HEAD", "--")
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


@dataclass(frozen=True, slots=True)
class ChangedChild:
    """One child's --changed verdict: run it, or skip it as unaffected."""

    target: ChildTarget
    run: bool
    vitest_changed: bool


_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _child_baseline_head(domain: C.DomainPaths, child: ChildTarget) -> str | None:
    """Recorded baseline commit for one child, or None (run the child)."""
    from . import history
    from .operations import _checkout

    try:
        if child.config is None:
            return None
        checkout = _checkout(child.config)
        view = history.read_history(domain, checkout)
        baseline = view.baseline
        return baseline.head if baseline is not None else None
    except Exception:
        # Advisory read only: unknown history runs the child (fail closed).
        return None


def child_baseline_heads(domain: C.DomainPaths,
                         children: tuple[ChildTarget, ...]) -> dict[str, str | None]:
    """Best-effort recorded-baseline commits keyed by child declaration."""
    return {child.declaration: _child_baseline_head(domain, child)
            for child in children}


def _committed_since(root: Path, older: str) -> tuple[str, ...] | None:
    """Repo-relative paths committed between one baseline head and HEAD."""
    if not _COMMIT_RE.fullmatch(older):
        return None
    raw = _git_blob(root, "diff", "--name-only", "-z", "--no-ext-diff",
                    "--no-textconv", "--find-renames", older, "HEAD", "--")
    if raw is None:
        return None
    return _nul_paths(raw)


def _run_all(children: tuple[ChildTarget, ...],
             base: str | None) -> tuple[ChangedChild, ...]:
    return tuple(ChangedChild(child, True, _vitest_base(child, base) is not None)
                 for child in children)


def select_changed_children(root: Path, children: tuple[ChildTarget, ...],
                            base: str | None,
                            baseline_heads: dict[str, str | None] | None = None,
                            ) -> tuple[ChangedChild, ...]:
    """Decide per child whether root `ptest --changed` runs it.

    With an explicit base the committed range is ``base..HEAD`` for every
    child.  Without one, each child consults its recorded baseline: the
    child runs unless its directory holds no worktree change AND no change
    committed since its baseline head; a missing baseline (or unreadable
    history) runs the child, since that run can record one.  In both
    modes a child also runs when a changed file outside every child
    directory is one of its full triggers (its selection
    ``full_triggers`` plus the implicit ``.ptest.toml``; a changed root
    manifest therefore runs every child).  Unavailable Git evidence runs
    every child.  The ``vitest_changed`` flag marks the section-A.2
    delegation (see ``child_changed_request``).
    """
    if base is not None:
        changed = worktree_changed_files(root, base)
        if changed is None:
            return _run_all(children, base)
        return tuple(
            _classify(child, changed, changed, children, base)
            for child in children)
    worktree = worktree_changed_files(root, None)
    if worktree is None or baseline_heads is None:
        return _run_all(children, base)
    committed: dict[str, tuple[str, ...] | None] = {}
    for child in children:
        head = baseline_heads.get(child.declaration)
        committed[child.declaration] = (
            None if head is None else _committed_since(root, head))
    selected: list[ChangedChild] = []
    for child in children:
        own_range = committed[child.declaration]
        if own_range is None:
            selected.append(ChangedChild(child, True, False))
            continue
        relevant = worktree + own_range
        selected.append(_classify(child, relevant, relevant, children, base))
    return tuple(selected)


def _classify(child: ChildTarget, own: tuple[str, ...], outside_pool: tuple[str, ...],
              children: tuple[ChildTarget, ...], base: str | None) -> ChangedChild:
    """Run one child when its own paths changed or an outside trigger did."""
    triggers = ((child.config.selection.full_triggers
                 if child.config is not None else ())
                + (".ptest.toml",))
    if any(_matches(path, (child.declaration,)) for path in own):
        return ChangedChild(child, True, _vitest_base(child, base) is not None)
    outside = [path for path in outside_pool
               if not any(_matches(path, (other.declaration,)) for other in children)]
    run = any(_matches(path, triggers) for path in outside)
    return ChangedChild(child, run, run and _vitest_base(child, base) is not None)


def _vitest_base(target: ChildTarget, base: str | None) -> str | None:
    """Return base when this child should delegate to `vitest --changed`."""
    if base is None or target.config is None:
        return None
    if target.config.runner.kind is not C.RunnerKind.VITEST:
        return None
    return base


def child_changed_request(target: ChildTarget, *, base: str | None,
                          workers: int | None = None,
                          queue_timeout_s: float = C.DEFAULT_QUEUE_TIMEOUT_S,
                          no_setup: bool = False,
                          result_path: str | None = None,
                          fixture_domain: Path | None = None,
                          verbose: bool = False,
                          quiet: bool = False) -> C.RunRequest:
    """Build the section-A run request for one child that has changes.

    DESIGN NOTE (section A.2): ptest owns no selection protocol for vitest
    (the adapter runs one literal exclusive command), so a vitest child
    cannot plan a file subset the way a qualified pytest child can.  When
    an explicit base names the comparison ref — the simple case — delegate
    to vitest's own `--changed <base>` via a scoped request carrying exactly
    that argv, so only affected test files run.  Without a base there is no
    ref to name, so the child runs in normal CHANGED (automatic) mode and
    takes its full suite with the planner's reason.  Every other child runs
    in CHANGED mode with --base passed through.
    """
    changed_base = _vitest_base(target, base)
    if changed_base is not None:
        return C.RunRequest(mode=C.Mode.SCOPED, argv=("--changed", changed_base),
                            base=base, workers=workers,
                            queue_timeout_s=queue_timeout_s, no_setup=no_setup,
                            result_path=result_path,
                            fixture_domain=fixture_domain,
                            verbose=verbose, quiet=quiet)
    return C.RunRequest(mode=C.Mode.AUTOMATIC, base=base, workers=workers,
                        queue_timeout_s=queue_timeout_s, no_setup=no_setup,
                        result_path=result_path,
                        fixture_domain=fixture_domain,
                        verbose=verbose, quiet=quiet)
