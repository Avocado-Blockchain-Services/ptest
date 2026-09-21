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
_PROVIDER_SKILLS = {
    "claude": ".claude/skills/ptest/SKILL.md",
    "codex": ".codex/skills/ptest/SKILL.md",
    "opencode": ".opencode/skills/ptest/SKILL.md",
    "gemini": ".gemini/skills/ptest/SKILL.md",
}
SUPPORTED_AGENTS = tuple(_PROVIDER_SKILLS)


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="agent-rules")


@dataclass(frozen=True, slots=True)
class RulesPlan:
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RulesResult:
    changed: bool
    actions: tuple[str, ...]


def _provider_text(provider: str) -> bytes:
    return (
        f"# ptest skill for {provider}\n\n"
        "Read `docs/ptest-agent.md` before running or changing tests.\n"
        "Run ptest from the monorepo root; prefix focused scopes with the "
        "declared child and use `ptest --full` for the integrated gate.\n"
    ).encode("utf-8")


def _provider_target(root: Path, provider: str) -> tuple[str, Path, bytes | None]:
    if provider not in _PROVIDER_SKILLS:
        raise _problem("unsupported-capability", "agent provider is not supported")
    relative = _PROVIDER_SKILLS[provider]
    parts = relative.split("/")
    cursor = root
    for part in parts[:-1]:
        cursor = cursor / part
        try:
            stamp = os.lstat(cursor)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            raise _problem("unsafe-path", f"agent provider path {relative} is unsafe")
        if stamp.st_uid != os.getuid() or stat.S_IMODE(stamp.st_mode) & 0o022:
            raise _problem("unsafe-path", f"agent provider path {relative} is unsafe")
    target = root / relative
    try:
        stamp = os.lstat(target)
    except FileNotFoundError:
        return relative, target, None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", f"agent provider target {relative} is unsafe")
    if stamp.st_size > _MAX_FILE_BYTES:
        raise _problem("invalid-bound", f"agent provider target {relative} exceeds the size limit")
    try:
        current = target.read_bytes()
    except OSError:
        raise _problem("state-unavailable", f"agent provider target {relative} is unavailable") from None
    expected = _provider_text(provider)
    if current != expected:
        raise _problem("already-exists", f"agent provider target {relative} already exists")
    return relative, target, expected


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


def preview(root: Path, *, agents: tuple[str, ...] = ()) -> RulesPlan:
    root, texts, guide = _validated(root)
    providers = tuple(dict.fromkeys(agents))
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
    for provider in providers:
        relative, _, existing = _provider_target(root, provider)
        if existing is None:
            actions.append(f"create {relative}")
    return RulesPlan(actions=tuple(actions))


def apply(root: Path, *, agents: tuple[str, ...] = ()) -> RulesResult:
    root, texts, guide = _validated(root)
    providers = tuple(dict.fromkeys(agents))
    plan = preview(root, agents=providers)
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
    for provider in providers:
        relative, target, existing = _provider_target(root, provider)
        if existing is None:
            cursor = root
            parts = relative.split("/")
            for name in parts[:-1]:
                files.ensure_shared_dir(cursor, name)
                cursor = cursor / name
            files.create_exclusive(cursor, parts[-1], _provider_text(provider), private=False)
    return RulesResult(changed=True, actions=plan.actions)
