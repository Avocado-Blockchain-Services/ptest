"""Static linked-worktree detection and committed-state checks for ptest files."""
from __future__ import annotations

import os
import stat
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from .files import _check_no_symlink_prefixes, read_regular

CONFIG_NAME = ".ptest.toml"
# Agent-rule files ptest init may write; the three instruction files count
# only when they contain the managed marker below.
AGENT_RULE_FILES: tuple[str, ...] = (
    "docs/ptest-agent.md",
    "AGENTS.md", "CLAUDE.md", "GEMINI.md",
    ".agents/skills/ptest/SKILL.md", ".claude/skills/ptest/SKILL.md",
    ".gemini/skills/ptest/SKILL.md", ".opencode/skills/ptest/SKILL.md",
)
MANAGED_MARKER = "<!-- ptest-agent-rules:start -->"
_MARKED_RULE_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "GEMINI.md"})

_GITDIR_LIMIT = 4097
_CONFIG_LIMIT = 256 * 1024 + 1


@dataclass(frozen=True, slots=True)
class LinkedWorktree:
    root: Path       # linked worktree top: the directory holding the `.git` file
    main_root: Path  # main worktree top: parent of the common `.git` directory


@dataclass(frozen=True, slots=True)
class MainConfig:
    worktree: LinkedWorktree
    relative: str               # "." or posix "a/b": config directory relative to BOTH roots
    path: Path                  # main_root / relative / ".ptest.toml" (absolute, regular file)
    children: tuple[str, ...]   # v2 manifest declarations whose config exists in main; () if standalone


def _norm(path: Path | str) -> Path:
    return Path(os.path.normpath(os.fspath(path)))


def _single_line_content(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        return None
    return text


def _clean_value(text: str) -> str | None:
    if any(ord(char) < 0x20 for char in text):
        return None
    return text


def linked_worktree(root: Path) -> LinkedWorktree | None:
    """Detect a linked git worktree from its static ``.git`` file, or None."""
    try:
        return _linked_worktree_inner(root)
    except Exception:
        return None


def _linked_worktree_inner(root: Path) -> LinkedWorktree | None:
    root = _norm(root if isinstance(root, Path) else Path(root))
    try:
        stamp = os.lstat(root / ".git")
    except OSError:
        return None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        return None
    try:
        raw = read_regular(root, ".git", _GITDIR_LIMIT)
    except C.Problem:
        return None
    if len(raw) > 4096:
        return None
    text = _single_line_content(raw)
    if text is None or not text.startswith("gitdir:"):
        return None
    rest = text[len("gitdir:"):]
    if not rest.startswith((" ", "\t")):
        return None
    value = rest.strip()
    if _clean_value(value) is None or not value:
        return None
    gitdir = _norm(value if os.path.isabs(value) else (root / value))
    try:
        common_raw = read_regular(gitdir, "commondir", _GITDIR_LIMIT)
    except C.Problem:
        return None
    if len(common_raw) > 4096:
        return None
    common_text = _single_line_content(common_raw)
    if common_text is None:
        return None
    common_value = common_text.strip()
    if _clean_value(common_value) is None or not common_value:
        return None
    common = _norm(common_value if os.path.isabs(common_value) else (gitdir / common_value))
    try:
        back_raw = read_regular(gitdir, "gitdir", _GITDIR_LIMIT)
    except C.Problem:
        return None
    if len(back_raw) > 4096:
        return None
    back_text = _single_line_content(back_raw)
    if back_text is None:
        return None
    back_value = back_text.strip()
    if _clean_value(back_value) is None or not back_value:
        return None
    back = _norm(back_value if os.path.isabs(back_value) else (gitdir / back_value))
    if back != _norm(root / ".git"):
        return None
    if common.name != ".git":
        return None
    try:
        common_stamp = os.lstat(common)
    except OSError:
        return None
    if stat.S_ISLNK(common_stamp.st_mode) or not stat.S_ISDIR(common_stamp.st_mode):
        return None
    main_root = _norm(common.parent)
    if main_root == root:
        return None
    try:
        _check_no_symlink_prefixes(gitdir)
        _check_no_symlink_prefixes(common)
        _check_no_symlink_prefixes(main_root)
    except C.Problem:
        return None
    return LinkedWorktree(root=root, main_root=main_root)


def _read_main_file(main_root: Path, name: str) -> bytes | None:
    try:
        raw = read_regular(main_root, name, _CONFIG_LIMIT)
    except C.Problem:
        return None
    if len(raw) > 256 * 1024:
        return None
    return raw


def main_config(cwd: Path, root: Path) -> MainConfig | None:
    """Nearest-first main-checkout config lookup for a linked worktree."""
    try:
        return _main_config_inner(cwd, root)
    except Exception:
        return None


def _main_config_inner(cwd: Path, root: Path) -> MainConfig | None:
    cwd = _norm(cwd if isinstance(cwd, Path) else Path(cwd))
    root = _norm(root if isinstance(root, Path) else Path(root))
    if cwd != root and root not in cwd.parents:
        return None
    link = linked_worktree(root)
    if link is None:
        return None
    cursor = cwd
    while True:
        rel = cursor.relative_to(root).as_posix()
        name = CONFIG_NAME if rel == "." else f"{rel}/{CONFIG_NAME}"
        raw = _read_main_file(link.main_root, name)
        if raw is not None:
            return MainConfig(
                worktree=link,
                relative=rel,
                path=link.main_root / name,
                children=_main_children(link.main_root, rel, raw),
            )
        if cursor == root:
            return None
        cursor = cursor.parent


def _main_children(main_root: Path, rel: str, raw: bytes) -> tuple[str, ...]:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return ()
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        return ()
    if data.get("version") != 2:
        return ()
    try:
        from .monorepo import parse_monorepo_manifest
        manifest = parse_monorepo_manifest(raw, main_root / CONFIG_NAME)
    except Exception:
        return ()
    kept: list[str] = []
    for child in manifest.children:
        name = f"{rel}/{child}/{CONFIG_NAME}" if rel != "." else f"{child}/{CONFIG_NAME}"
        if _read_main_file(main_root, name) is not None:
            kept.append(child)
    return tuple(kept)


def _is_regular_file(root: Path, name: str) -> bool:
    try:
        stamp = os.lstat(root / name)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode)


def _local_v2_children(root: Path) -> tuple[str, ...]:
    try:
        raw = read_regular(root, CONFIG_NAME, _CONFIG_LIMIT)
    except C.Problem:
        return ()
    if len(raw) > 256 * 1024:
        return ()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return ()
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        return ()
    if data.get("version") != 2:
        return ()
    try:
        from .monorepo import parse_monorepo_manifest
        manifest = parse_monorepo_manifest(raw, root / CONFIG_NAME)
    except Exception:
        return ()
    return manifest.children


def uncommitted_config_files(
    root: Path, *, include_agent_rules: bool = False,
) -> tuple[str, ...]:
    """Repo-relative ptest files under ``root`` missing from HEAD's tree."""
    try:
        return _uncommitted_inner(root, include_agent_rules=include_agent_rules)
    except Exception:
        return ()


def _uncommitted_inner(root: Path, *, include_agent_rules: bool) -> tuple[str, ...]:
    root = _norm(root if isinstance(root, Path) else Path(root))
    # Deferred import: config imports this module at top level, so an
    # eager import would cycle. Outside git there is no HEAD tree to
    # compare against; return without spawning any subprocess. The
    # membership check follows symlinks (callers may pass a linked
    # path); git itself resolves the same directory.
    from .config import git_root
    if git_root(os.path.realpath(root)) is None:
        return ()
    candidates: list[str] = []
    if _is_regular_file(root, CONFIG_NAME):
        candidates.append(CONFIG_NAME)
        for child in _local_v2_children(root):
            name = f"{child}/{CONFIG_NAME}"
            if _is_regular_file(root, name):
                candidates.append(name)
    if include_agent_rules:
        marker = MANAGED_MARKER.encode("utf-8")
        for entry in AGENT_RULE_FILES:
            try:
                raw = read_regular(root, entry, _CONFIG_LIMIT)
            except C.Problem:
                continue
            if len(raw) > 256 * 1024:
                continue
            if entry in _MARKED_RULE_FILES and marker not in raw:
                continue
            candidates.append(entry)
    if not candidates:
        return ()
    try:
        from . import source as _source
        scan = _source._Scan(deadline=time.monotonic() + 2.0)
        output = _source._git(
            root, scan, "ls-tree", "-r", "-z", "--name-only", "HEAD",
            "--", *candidates,
        )
    except Exception:
        return ()
    try:
        tracked = set(output.decode("utf-8").split("\0") if output else [])
    except UnicodeDecodeError:
        return ()
    tracked.discard("")
    return tuple(name for name in candidates if name not in tracked)


__all__ = ["CONFIG_NAME", "AGENT_RULE_FILES", "MANAGED_MARKER", "LinkedWorktree",
           "MainConfig", "linked_worktree", "main_config", "uncommitted_config_files"]
