"""Safe, opt-in repository-local testing rules for coding agents."""
from __future__ import annotations

import importlib.resources
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .contracts import Problem
from . import files

_GUIDE_PATH = "docs/ptest-agent.md"
_MARKER_START = "<!-- ptest-agent-rules:start -->"
_MARKER_END = "<!-- ptest-agent-rules:end -->"
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
_MAX_FILE_BYTES = 256 * 1024


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="agent-rules")


@dataclass(frozen=True, slots=True)
class RulesPlan:
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RulesResult:
    changed: bool
    actions: tuple[str, ...]


def _guide() -> bytes:
    try:
        return importlib.resources.files("ptest").joinpath(
            "resources", "repository-agent-guide.md").read_bytes()
    except OSError:
        raise _problem("state-unavailable", "bundled repository agent guide is unavailable") from None


def _block(name: str) -> str:
    reference = ("Before running or changing tests, read `docs/ptest-agent.md`."
                 if name == "AGENTS.md" else "@docs/ptest-agent.md")
    return f"{_MARKER_START}\n{reference}\n{_MARKER_END}\n"


def _read_regular(path: Path) -> str | None:
    try:
        stamp = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path", f"agent rules target {path.name} is a symlink")
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", f"agent rules target {path.name} is not a regular file")
    if stamp.st_size > _MAX_FILE_BYTES:
        raise _problem("invalid-bound", f"agent rules target {path.name} exceeds the size limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _problem("invalid-config", f"agent rules target {path.name} is not UTF-8 text") from None


def _managed_state(text: str, name: str) -> bool:
    starts, ends = text.count(_MARKER_START), text.count(_MARKER_END)
    if starts != ends or starts > 1:
        raise _problem("invalid-config", f"agent rules block in {name} is malformed")
    if not starts:
        return False
    expected = _block(name).rstrip("\n")
    begin = text.index(_MARKER_START)
    end = text.index(_MARKER_END, begin) + len(_MARKER_END)
    if text[begin:end] != expected:
        raise _problem("invalid-config", f"agent rules block in {name} is malformed")
    return True


def _replace(path: Path, text: str) -> None:
    """Atomically replace an already validated regular file without following links."""
    parent, name = path.parent, path.name
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    original = os.stat(name, dir_fd=directory, follow_symlinks=False)
    temporary = f".{name}.ptest-{secrets.token_hex(8)}"
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=directory)
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise _problem("unsafe-path", f"agent rules target {path.name} changed during apply")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except OSError:
            pass
        os.close(directory)


def _validated(root: Path) -> tuple[Path, dict[str, str | None], bytes]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise _problem("unsafe-path", "agent rules root is not a directory")
    guide = _guide()
    docs = root / "docs"
    if docs.exists() or docs.is_symlink():
        if docs.is_symlink() or not docs.is_dir():
            raise _problem("unsafe-path", "agent rules docs directory is unsafe")
    existing_guide = _read_regular(root / _GUIDE_PATH) if docs.exists() else None
    if existing_guide is not None and existing_guide.encode("utf-8") != guide:
        raise _problem("already-exists", "docs/ptest-agent.md already exists and is not ptest-managed")
    agents = root / "AGENTS.md"
    try:
        agent_stamp = os.lstat(agents)
    except FileNotFoundError:
        agent_stamp = None
    if agent_stamp is not None and stat.S_ISLNK(agent_stamp.st_mode):
        # A checked-in AGENTS.md -> CLAUDE.md alias is a common local convention.
        # It is the only symlink shape we permit; all other links remain a refused
        # write boundary. The canonical CLAUDE file receives the single block.
        if os.readlink(agents) != "CLAUDE.md":
            raise _problem("unsafe-path", "agent rules target AGENTS.md is a symlink")
        agent_text = None
    else:
        agent_text = _read_regular(agents)
    texts = {"AGENTS.md": agent_text,
             "CLAUDE.md": _read_regular(root / "CLAUDE.md"),
             "GEMINI.md": _read_regular(root / "GEMINI.md")}
    if agent_stamp is not None and stat.S_ISLNK(agent_stamp.st_mode) and texts["CLAUDE.md"] is None:
        raise _problem("unsafe-path", "AGENTS.md alias target CLAUDE.md is unavailable")
    for name, text in texts.items():
        if text is not None:
            _managed_state(text, name)
    return root, texts, guide


def preview(root: Path) -> RulesPlan:
    root, texts, guide = _validated(root)
    actions = []
    if _read_regular(root / _GUIDE_PATH) is None:
        actions.append("create docs/ptest-agent.md")
    existing = [name for name, text in texts.items() if text is not None]
    targets = existing or ["AGENTS.md"]
    for name in targets:
        text = texts[name]
        if text is None:
            actions.append(f"create {name}")
        elif not _managed_state(text, name):
            actions.append(f"append managed reference to {name}")
    return RulesPlan(actions=tuple(actions))


def apply(root: Path) -> RulesResult:
    root, texts, guide = _validated(root)
    plan = preview(root)
    if not plan.actions:
        return RulesResult(changed=False, actions=())
    docs = root / "docs"
    if not docs.exists():
        files.ensure_shared_dir(root, "docs")
    guide_path = root / _GUIDE_PATH
    if not guide_path.exists():
        files.create_exclusive(root, _GUIDE_PATH, guide, private=False)
    existing = [name for name, text in texts.items() if text is not None]
    for name in existing or ["AGENTS.md"]:
        target, text = root / name, texts[name]
        block = _block(name)
        if text is None:
            files.create_exclusive(root, name, block.encode("utf-8"), private=False)
        elif not _managed_state(text, name):
            _replace(target, text.rstrip("\n") + "\n\n" + block)
    return RulesResult(changed=True, actions=plan.actions)
