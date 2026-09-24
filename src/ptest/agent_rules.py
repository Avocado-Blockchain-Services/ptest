"""Safe, opt-in repository-local testing rules for coding agents."""
from __future__ import annotations

import importlib.resources
import errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from .contracts import Problem
from . import files

_GUIDE_PATH = "docs/ptest-agent.md"
_MARKER_START = "<!-- ptest-agent-rules:start -->"
_MARKER_END = "<!-- ptest-agent-rules:end -->"
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
_MAX_FILE_BYTES = 256 * 1024
_PROVIDER_SKILLS = {
    "claude": ".claude/skills/ptest/SKILL.md",
    "codex": ".agents/skills/ptest/SKILL.md",
    "opencode": ".opencode/skills/ptest/SKILL.md",
    "gemini": ".gemini/skills/ptest/SKILL.md",
}
SUPPORTED_AGENTS = tuple(_PROVIDER_SKILLS)
_LEGACY_CODEX_SKILL = ".codex/skills/ptest/SKILL.md"
_PROVIDER_DESCRIPTIONS = {
    "claude": "Coordinate repository testing through ptest from the repository root.",
    "codex": "Run repository tests through ptest; Codex discovers this skill automatically.",
    "opencode": "Run repository tests through ptest from the repository root.",
    "gemini": "Run repository tests through ptest from the repository root.",
}


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="agent-rules")


@dataclass(frozen=True, slots=True)
class RulesPlan:
    actions: tuple[str, ...]
    details: tuple = field(default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions",
                            C._as_str_tuple("rules.actions", self.actions))
        object.__setattr__(self, "details",
                            C._check_action_records("rules.details", self.details))


@dataclass(frozen=True, slots=True)
class RulesResult:
    changed: bool
    actions: tuple[str, ...]
    details: tuple = field(default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed", bool(self.changed))
        object.__setattr__(self, "actions",
                            C._as_str_tuple("rules.actions", self.actions))
        object.__setattr__(self, "details",
                            C._check_action_records("rules.details", self.details))


def _detail(target: str, action: str) -> C.ActionRecord:
    return C.ActionRecord(target=target, action=action, source="guidance")


def _legacy_provider_text(provider: str) -> bytes:
    """Exact released ptest template bytes, used only for upgrade recognition."""
    return (
        f"# ptest skill for {provider}\n\n"
        "Read `docs/ptest-agent.md` before running or changing tests.\n"
        "Run ptest from the monorepo root; prefix focused scopes with the "
        "declared child and use `ptest --full` for the integrated gate.\n"
    ).encode("utf-8")


def _previous_provider_text(provider: str) -> bytes:
    """Exact base-commit skill bytes, recognized as previous managed content.

    The longer pre-shortening template upgrades in place; anything else
    that is not current still raises ``already-exists`` so user edits are
    never clobbered.
    """
    description = _PROVIDER_DESCRIPTIONS[provider]
    return (
        "---\n"
        "name: ptest\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        "# ptest skill\n"
        "\n"
        "Before running or changing tests, read the repository-root guide\n"
        "`docs/ptest-agent.md`. That path is relative to the repository root,\n"
        "not to this skill directory.\n"
        "\n"
        "Run every test command through `ptest` from the repository root (the\n"
        "directory containing the root `.ptest.toml`). Never invoke pytest,\n"
        "Vitest, or another runner directly.\n"
        "\n"
        "During iteration run the smallest relevant scope, such as\n"
        "`ptest tests/<chosen-test>.py`. In a monorepo, prefix the scope with\n"
        "its declared child, such as `ptest api/tests/<chosen-test>.py`; child\n"
        "`.ptest.toml` files remain authoritative. Run the root full gate\n"
        "`ptest --full` once after the integrated change.\n"
        "\n"
        "If a merge is fast-forward and the exact tip commit already passed the required ptest gate, do not rerun ptest solely because of the merge. A merge commit, new changes, or an untested tip still requires the applicable ptest gate.\n"
        "After source merges, run `graphify update .`; skipping duplicate ptest does not skip the graph refresh.\n"
    ).encode("utf-8")


def _provider_text(provider: str) -> bytes:
    """Current generated skill: front matter plus a short guide pointer.

    Shared guidance (merge gate, graph refresh, scopes) lives only in
    `docs/ptest-agent.md`; the skill just points at it so the two can
    never duplicate or drift.
    """
    description = _PROVIDER_DESCRIPTIONS[provider]
    return (
        "---\n"
        "name: ptest\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        "# ptest skill\n"
        "\n"
        "Before running or changing tests, read `docs/ptest-agent.md` (relative to\n"
        "the repository root). Run tests only through `ptest` from the repository root.\n"
    ).encode("utf-8")


def _provider_target(root: Path, provider: str) -> tuple[str, Path, bytes | None, str]:
    """Validate one canonical skill target without writing.

    Returns ``(relative, target, existing, kind)`` where ``kind`` is
    ``"missing"``, ``"legacy"`` (exact released template, safe to upgrade),
    ``"previous"`` (exact base-commit template, safe to upgrade) or
    ``"current"`` (already in the generated format). Anything else raises
    before any write.
    """
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
        return relative, target, None, "missing"
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", f"agent provider target {relative} is unsafe")
    if stamp.st_size > _MAX_FILE_BYTES:
        raise _problem("invalid-bound", f"agent provider target {relative} exceeds the size limit")
    try:
        current = files.read_regular(root, relative, _MAX_FILE_BYTES + 1)
    except OSError:
        raise _problem("state-unavailable", f"agent provider target {relative} is unavailable") from None
    if current == _provider_text(provider):
        return relative, target, current, "current"
    if current == _legacy_provider_text(provider):
        return relative, target, current, "legacy"
    if current == _previous_provider_text(provider):
        return relative, target, current, "previous"
    raise _problem("already-exists", f"agent provider target {relative} already exists")


def _legacy_codex_note(root: Path) -> str | None:
    """Inspect the obsolete Codex skill path without following or mutating it.

    Returns a migration note when the file holds the exact released template,
    ``None`` when absent. Any user-owned, non-regular, symlinked, oversized
    or otherwise unsafe legacy artifact raises before any init write.
    """
    try:
        os.lstat(root / _LEGACY_CODEX_SKILL)
    except FileNotFoundError:
        return None
    except OSError:
        raise _problem(
            "state-unavailable",
            f"legacy agent provider target {_LEGACY_CODEX_SKILL} is unavailable") from None
    try:
        raw = files.read_regular(root, _LEGACY_CODEX_SKILL, _MAX_FILE_BYTES + 1)
    except Problem as problem:
        if problem.code == "state-unavailable":
            # Only a file that vanished between the probe and the read
            # counts as absent; an unreadable artifact must fail loudly
            # rather than be silently reported as missing.
            try:
                os.lstat(root / _LEGACY_CODEX_SKILL)
            except FileNotFoundError:
                return None
        raise
    if len(raw) > _MAX_FILE_BYTES:
        raise _problem(
            "invalid-bound",
            f"legacy agent provider target {_LEGACY_CODEX_SKILL} exceeds the size limit")
    if raw != _legacy_provider_text("codex"):
        raise _problem(
            "already-exists",
            f"legacy agent provider target {_LEGACY_CODEX_SKILL} already exists "
            "and is not ptest-managed")
    return (f"legacy Codex skill preserved at {_LEGACY_CODEX_SKILL}; "
            "Codex now discovers .agents/skills/ptest/SKILL.md")


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


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _open_dir_fd(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise _problem("state-unavailable", f"agent rules directory {path} is unavailable") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _problem("unsafe-path", f"agent rules directory {path} is a symlink") from None
        raise _problem("state-unavailable", f"agent rules directory {path} is unavailable") from None


def _open_component(parent_fd: int, name: str) -> int:
    """Open one child directory without following a trailing symlink."""
    files.validate_single_name(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent_fd)
    except FileNotFoundError:
        raise _problem("state-unavailable", f"agent rules directory {name} is unavailable") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _problem("unsafe-path", f"agent rules path {name} is a symlink") from None
        raise _problem("unsafe-path", f"agent rules path {name} is not a directory") from None
    stamp = os.fstat(fd)
    if not stat.S_ISDIR(stamp.st_mode):
        _close_quietly(fd)
        raise _problem("unsafe-path", f"agent rules path {name} is not a directory")
    return fd


def _walk_existing(root_fd: int, parts: list[str]) -> int:
    """Open every path component no-follow; returns the deepest fd.

    The caller owns the returned fd (which is ``root_fd`` when ``parts``
    is empty). Intermediates are closed before return.
    """
    fd = root_fd
    try:
        for part in parts:
            child = _open_component(fd, part)
            if fd is not root_fd:
                _close_quietly(fd)
            fd = child
        return fd
    except Exception:
        if fd is not root_fd:
            _close_quietly(fd)
        raise


def _write_fully(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)


def _ensure_child_fd(parent_fd: int, name: str) -> tuple[int, bool, tuple[int, int]]:
    """Ensure one chain component below an open parent directory.

    Returns ``(child_fd, created, identity)`` where ``created`` is true
    only when this call created the directory via ``mkdir``; a directory
    won by a concurrent creator reports ``created=False`` so rollback
    never removes it. The caller owns ``child_fd``.
    """
    files.validate_single_name(name)
    try:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd)
    except FileNotFoundError:
        child = None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _problem("unsafe-path", f"agent rules path {name} is a symlink") from None
        raise _problem("unsafe-path", f"agent rules path {name} is not a directory") from None
    if child is not None:
        stamp = os.fstat(child)
        if (not stat.S_ISDIR(stamp.st_mode) or stamp.st_uid != os.getuid()
                or stat.S_IMODE(stamp.st_mode) & 0o022):
            _close_quietly(child)
            raise _problem("unsafe-path", f"agent rules directory {name} is unsafe")
        return child, False, (stamp.st_dev, stamp.st_ino)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        return _ensure_child_fd(parent_fd, name)
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd)
    try:
        stamp = os.fstat(child)
        if not stat.S_ISDIR(stamp.st_mode) or stamp.st_uid != os.getuid():
            raise _problem("unsafe-path", f"agent rules directory {name} failed creation checks")
        os.fchmod(child, 0o700)
        return child, True, (stamp.st_dev, stamp.st_ino)
    except Exception:
        _close_quietly(child)
        raise


def _ensure_tree(root_fd: int, root: Path, parts: list[str],
                 created_dirs: list[tuple[Path, tuple[int, int]]]) -> int:
    """Ensure every component below an open root directory, no-follow.

    Returns the deepest fd (``root_fd`` when ``parts`` is empty); the
    caller owns it. Only directories this invocation created are
    recorded with their device/inode identity.
    """
    fd = root_fd
    cursor = root
    try:
        for part in parts:
            child, created, identity = _ensure_child_fd(fd, part)
            cursor = cursor / part
            if created:
                created_dirs.append((cursor, identity))
            if fd is not root_fd:
                _close_quietly(fd)
            fd = child
        return fd
    except Exception:
        if fd is not root_fd:
            _close_quietly(fd)
        raise


def _create_leaf(parent_fd: int, name: str, data: bytes) -> tuple[int, int]:
    """Exclusively create one leaf file below an open parent directory.

    Returns the created device/inode identity while the file is still
    referenced descriptor-relatively, so rollback can verify ownership.
    """
    files.validate_single_name(name)
    payload = bytes(data)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=parent_fd)
    except FileExistsError:
        raise _problem("already-exists", f"agent rules target {name} already exists") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _problem("unsafe-path", f"agent rules target {name} is a symlink") from None
        if exc.errno == errno.ENOENT:
            raise _problem("state-unavailable", f"parent for agent rules target {name} is unavailable") from None
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise _problem("state-unavailable", f"agent rules target {name} is unavailable") from None
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise _problem("capacity-exceeded", f"agent rules target {name} exceeded capacity") from None
        raise _problem("unsafe-path", f"agent rules target {name} cannot be created") from None
    try:
        try:
            _write_fully(fd, payload)
            stamp = os.fstat(fd)
            if (not stat.S_ISREG(stamp.st_mode) or stamp.st_nlink != 1
                    or stamp.st_uid != os.getuid()
                    or stat.S_IMODE(stamp.st_mode) != 0o644):
                raise _problem("unsafe-path", f"agent rules target {name} failed creation checks")
            identity = (stamp.st_dev, stamp.st_ino)
        except Exception:
            try:
                os.ftruncate(fd, 0)
                os.fsync(fd)
            except OSError:
                pass
            try:
                owned = os.fstat(fd)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(name, dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            except OSError:
                pass
            raise
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        return identity
    finally:
        _close_quietly(fd)


def _read_leaf_fd(fd: int, name: str) -> bytes:
    """Read bounded bytes from an open regular-file descriptor."""
    chunks: list[bytes] = []
    remaining = _MAX_FILE_BYTES + 1
    while remaining > 0:
        piece = os.read(fd, min(8192, remaining))
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def _require_expect_bytes(parent_fd: int, name: str,
                          original_identity: tuple[int, int],
                          expect_bytes: bytes) -> None:
    """Require current leaf bytes to still match a prior verified read."""
    try:
        probe = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent_fd)
    except OSError:
        raise _problem("unsafe-path", f"agent rules target {name} changed during apply") from None
    try:
        fresh = os.fstat(probe)
        if ((fresh.st_dev, fresh.st_ino) != original_identity
                or _read_leaf_fd(probe, name) != expect_bytes):
            raise _problem("unsafe-path", f"agent rules target {name} changed during apply")
    finally:
        _close_quietly(probe)


def _replace(root: Path, path: Path, text: str, *, mode: int,
             expect_identity: tuple[int, int] | None = None,
             expect_bytes: bytes | None = None) -> tuple[int, int]:
    """Atomically replace a validated regular file without following links.

    Every ancestor is retained as a no-follow descriptor from ``root``.
    The mode is set with ``fchmod`` and synced on the temporary
    descriptor *before* publication, so nothing published can remain
    unjournaled: every failure point that can raise sits before
    ``os.replace``, and everything after it is non-raising cleanup.

    ``expect_identity``/``expect_bytes`` carry a previously verified
    ownership precondition (used by rollback) into the initial and
    pre-publication checks, so a file swapped between verification and
    replacement is rejected instead of overwritten. Normal updates pass
    the original bytes to preserve the content precondition. Returns
    the published device/inode identity.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _problem("unsafe-path", f"agent rules target {path.name} escapes the repository") from None
    parts = list(relative.parts)
    if not parts:
        raise _problem("unsafe-path", "agent rules target is the repository root")
    name = parts[-1]
    files.validate_single_name(name)
    payload = text.encode("utf-8")
    root_fd = _open_dir_fd(root)
    temporary: str | None = None
    try:
        parent_fd = _walk_existing(root_fd, parts[:-1])
        try:
            try:
                original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _problem("state-unavailable", f"agent rules target {name} is unavailable") from None
            except OSError:
                raise _problem("unsafe-path", f"agent rules target {name} is unsafe") from None
            if stat.S_ISLNK(original.st_mode) or not stat.S_ISREG(original.st_mode):
                raise _problem("unsafe-path", f"agent rules target {name} is unsafe")
            original_identity = (original.st_dev, original.st_ino)
            if expect_identity is not None and original_identity != expect_identity:
                raise _problem("unsafe-path", f"agent rules target {name} changed during apply")
            if expect_bytes is not None:
                _require_expect_bytes(parent_fd, name, original_identity, expect_bytes)
            temporary = f".{name}.ptest-{secrets.token_hex(8)}"
            fd = None
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=parent_fd)
                _write_fully(fd, payload)
                temp_stamp = os.fstat(fd)
                temp_identity = (temp_stamp.st_dev, temp_stamp.st_ino)
                os.fchmod(fd, stat.S_IMODE(mode))
                os.fsync(fd)
            finally:
                _close_quietly(fd)
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise _problem("unsafe-path", f"agent rules target {name} changed during apply") from None
            if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != original_identity:
                raise _problem("unsafe-path", f"agent rules target {name} changed during apply")
            if expect_bytes is not None:
                _require_expect_bytes(parent_fd, name, original_identity, expect_bytes)
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            return temp_identity
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass
            if parent_fd is not root_fd:
                _close_quietly(parent_fd)
    finally:
        _close_quietly(root_fd)


# sha256 digests of known previous ``docs/ptest-agent.md`` bytes.
# Repositories holding exactly these bytes get an in-place guide upgrade;
# any other differing guide is a user edit and still raises
# ``already-exists``.
_PREVIOUS_GUIDE_SHA256S = frozenset({
    # Base-commit guide (first fast-forward-gate version).
    "72f2a5bbfcafc9b74cc2d1a7e621fe6784f67f701315d0503eef06e866989e68",
    # Guide as shipped on main before the G1 cleanup (byte-identical to
    # what init writes: ``git show main:src/ptest/resources/
    # repository-agent-guide.md``).
    "0b2ea261830578734a9f724e134a1207c651baa160d60b05fe0f025438dd96c6",
})


def _guide_kind(existing: str | None, guide: bytes) -> str:
    """Classify an installed guide: missing, current, or previous managed."""
    if existing is None:
        return "missing"
    raw = existing.encode("utf-8")
    if raw == guide:
        return "current"
    if hashlib.sha256(raw).hexdigest() in _PREVIOUS_GUIDE_SHA256S:
        return "previous"
    raise _problem("already-exists", "docs/ptest-agent.md already exists and is not ptest-managed")


def _validated(root: Path) -> tuple[Path, dict[str, str | None], bytes, str]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise _problem("unsafe-path", "agent rules root is not a directory")
    guide = _guide()
    docs = root / "docs"
    if docs.exists() or docs.is_symlink():
        if docs.is_symlink() or not docs.is_dir():
            raise _problem("unsafe-path", "agent rules docs directory is unsafe")
    existing_guide = _read_regular(root / _GUIDE_PATH) if docs.exists() else None
    kind = _guide_kind(existing_guide, guide)
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
    return root, texts, guide, kind


def preview(root: Path, *, agents: tuple[str, ...] = ()) -> RulesPlan:
    root, texts, guide, guide_kind = _validated(root)
    providers = tuple(dict.fromkeys(agents))
    for provider in providers:
        if provider not in _PROVIDER_SKILLS:
            raise _problem("unsupported-capability", "agent provider is not supported")
    note = _legacy_codex_note(root) if "codex" in providers else None
    actions: list[str] = []
    details: list[C.ActionRecord] = []
    if guide_kind == "missing":
        actions.append("create docs/ptest-agent.md")
        details.append(_detail(_GUIDE_PATH, "would create"))
    elif guide_kind == "previous":
        actions.append("update docs/ptest-agent.md")
        details.append(_detail(_GUIDE_PATH, "would update"))
    else:
        actions.append(f"already present {_GUIDE_PATH}")
        details.append(_detail(_GUIDE_PATH, "already present"))
    existing = [name for name, text in texts.items() if text is not None]
    targets = existing or ["AGENTS.md"]
    for name in targets:
        text = texts[name]
        if text is None:
            actions.append(f"create {name}")
            details.append(_detail(name, "would create"))
        elif not _managed_state(text, name):
            actions.append(f"append managed reference to {name}")
            details.append(_detail(name, "would update"))
        else:
            actions.append(f"already present {name}")
            details.append(_detail(name, "already present"))
    for provider in providers:
        relative, _, _, kind = _provider_target(root, provider)
        if kind == "missing":
            actions.append(f"create {relative}")
            details.append(_detail(relative, "would create"))
        elif kind in ("legacy", "previous"):
            actions.append(f"update {relative}")
            details.append(_detail(relative, "would update"))
        else:
            actions.append(f"already present {relative}")
            details.append(_detail(relative, "already present"))
    if note is not None:
        actions.append(f"note: {note}")
        details.append(_detail(note, "note"))
    return RulesPlan(actions=tuple(actions), details=tuple(details))


def _current_file(root: Path, parts: list[str]) -> tuple[bytes | None, tuple[int, int] | None]:
    """Read one repository-relative file without following symlinks.

    Returns ``(None, None)`` when the leaf is absent; raises on any
    uncertain identity (substituted ancestors, symlinks, non-regular
    files, unreadable inputs) so rollback leaves the entry in place.
    """
    root_fd = _open_dir_fd(root)
    try:
        parent_fd = _walk_existing(root_fd, parts[:-1])
        try:
            name = parts[-1]
            try:
                stamp = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None, None
            except OSError:
                raise _problem("unsafe-path", f"agent rules target {name} is unsafe") from None
            if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
                raise _problem("unsafe-path", f"agent rules target {name} is unsafe")
            identity = (stamp.st_dev, stamp.st_ino)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
            except OSError:
                raise _problem("unsafe-path", f"agent rules target {name} is unsafe") from None
            try:
                fresh = os.fstat(fd)
                if (not stat.S_ISREG(fresh.st_mode)
                        or (fresh.st_dev, fresh.st_ino) != identity):
                    raise _problem("unsafe-path", f"agent rules target {name} changed during apply")
                chunks: list[bytes] = []
                remaining = _MAX_FILE_BYTES + 1
                while remaining > 0:
                    piece = os.read(fd, min(8192, remaining))
                    if not piece:
                        break
                    chunks.append(piece)
                    remaining -= len(piece)
                return b"".join(chunks), identity
            finally:
                _close_quietly(fd)
        finally:
            if parent_fd is not root_fd:
                _close_quietly(parent_fd)
    finally:
        _close_quietly(root_fd)


def _relative_parts(root: Path, path: Path) -> list[str] | None:
    try:
        return list(path.relative_to(root).parts)
    except ValueError:
        return None


def _rollback(root: Path,
              created_files: list[tuple[Path, bytes, tuple[int, int]]],
              created_dirs: list[tuple[Path, tuple[int, int]]],
              updated_files: list[tuple[Path, bytes, int, bytes, tuple[int, int]]]) -> list[str]:
    """Restore owned guidance mutations; never touch concurrent edits.

    Ownership is verified by device/inode identity recorded at write
    time, so a concurrent same-byte replacement under a new inode is
    recognized as foreign. Every traversal is descriptor-relative and
    modes are restored with ``fchmod``; anything whose identity is
    uncertain is left in place and reported. Returns the list of owned
    changes that could not be safely reverted.
    """
    incomplete: list[str] = []
    for path, original, mode, written, identity in reversed(updated_files):
        parts = _relative_parts(root, path)
        if not parts:
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        try:
            current, current_identity = _current_file(root, parts)
        except (OSError, Problem):
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        if current is None or current_identity != identity or current != written:
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        try:
            _replace(root, path, original.decode("utf-8"), mode=stat.S_IMODE(mode),
                     expect_identity=identity, expect_bytes=written)
        except (OSError, Problem, UnicodeError):
            incomplete.append(f"{path.name} could not be restored")
    for path, written, identity in reversed(created_files):
        parts = _relative_parts(root, path)
        if not parts:
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        try:
            current, current_identity = _current_file(root, parts)
        except (OSError, Problem):
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        if current is None:
            continue
        if current_identity != identity or current != written:
            incomplete.append(f"{path.name} changed during apply and was left in place")
            continue
        root_fd = _open_dir_fd(root)
        try:
            parent_fd = _walk_existing(root_fd, parts[:-1])
            try:
                check = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if (check.st_dev, check.st_ino) != identity:
                    incomplete.append(f"{path.name} changed during apply and was left in place")
                    continue
                try:
                    os.unlink(parts[-1], dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
                except OSError:
                    incomplete.append(f"{path.name} could not be removed")
            finally:
                if parent_fd is not root_fd:
                    _close_quietly(parent_fd)
        except (OSError, Problem):
            incomplete.append(f"{path.name} could not be removed")
        finally:
            _close_quietly(root_fd)
    for directory, identity in reversed(created_dirs):
        parts = _relative_parts(root, directory)
        if not parts:
            incomplete.append(f"{directory.name} could not be removed")
            continue
        root_fd = _open_dir_fd(root)
        try:
            parent_fd = _walk_existing(root_fd, parts[:-1])
            try:
                try:
                    stamp = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    incomplete.append(f"{directory.name} could not be removed")
                    continue
                if (not stat.S_ISDIR(stamp.st_mode)
                        or (stamp.st_dev, stamp.st_ino) != identity):
                    incomplete.append(
                        f"{directory.name} changed during apply and was left in place")
                    continue
                try:
                    os.rmdir(parts[-1], dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
                except OSError:
                    incomplete.append(f"{directory.name} could not be removed")
            finally:
                if parent_fd is not root_fd:
                    _close_quietly(parent_fd)
        except (OSError, Problem):
            incomplete.append(f"{directory.name} could not be removed")
        finally:
            _close_quietly(root_fd)
    return incomplete


def apply(root: Path, *, agents: tuple[str, ...] = ()) -> RulesResult:
    root, texts, guide, guide_kind = _validated(root)
    providers = tuple(dict.fromkeys(agents))
    plan = preview(root, agents=providers)
    pending = [item for item in plan.details if item.action in ("would create", "would update")]
    if not pending:
        return RulesResult(changed=False, actions=plan.actions, details=plan.details)
    created_files: list[tuple[Path, bytes, tuple[int, int]]] = []
    created_dirs: list[tuple[Path, tuple[int, int]]] = []
    updated_files: list[tuple[Path, bytes, int, bytes, tuple[int, int]]] = []

    def create_file(relative: str, data: bytes) -> None:
        parts = relative.split("/")
        root_fd = _open_dir_fd(root)
        try:
            parent_fd = _ensure_tree(root_fd, root, parts[:-1], created_dirs)
            try:
                identity = _create_leaf(parent_fd, parts[-1], data)
            finally:
                if parent_fd is not root_fd:
                    _close_quietly(parent_fd)
            created_files.append((root.joinpath(*parts), bytes(data), identity))
        finally:
            _close_quietly(root_fd)

    def update_file(target: Path, original_text: str, original_mode: int, new_text: str) -> None:
        identity = _replace(root, target, new_text, mode=stat.S_IMODE(original_mode),
                            expect_bytes=original_text.encode("utf-8"))
        updated_files.append(
            (target, original_text.encode("utf-8"), original_mode,
             new_text.encode("utf-8"), identity))

    observed: list[C.ActionRecord] = []
    try:
        guide_path = root / _GUIDE_PATH
        if guide_kind == "missing":
            create_file(_GUIDE_PATH, guide)
            observed.append(_detail(_GUIDE_PATH, "created"))
        elif guide_kind == "previous":
            original_guide = _read_regular(guide_path)
            if original_guide is None:
                raise _problem("state-unavailable",
                               f"agent rules target {_GUIDE_PATH} is unavailable")
            stamp = os.lstat(guide_path)
            update_file(guide_path, original_guide,
                        stat.S_IMODE(stamp.st_mode),
                        guide.decode("utf-8"))
            observed.append(_detail(_GUIDE_PATH, "updated"))
        else:
            observed.append(_detail(_GUIDE_PATH, "already present"))
        existing = [name for name, text in texts.items() if text is not None]
        for name in existing or ["AGENTS.md"]:
            target, text = root / name, texts[name]
            block = _block(name)
            if text is None:
                create_file(name, block.encode("utf-8"))
                observed.append(_detail(name, "created"))
            elif not _managed_state(text, name):
                stamp = os.lstat(target)
                update_file(target, text, stat.S_IMODE(stamp.st_mode),
                            text.rstrip("\n") + "\n\n" + block)
                observed.append(_detail(name, "updated"))
            else:
                observed.append(_detail(name, "already present"))
        for provider in providers:
            relative, target, existing, kind = _provider_target(root, provider)
            if kind == "current":
                observed.append(_detail(relative, "already present"))
                continue
            if kind in ("legacy", "previous"):
                if existing is None:
                    raise _problem("state-unavailable",
                                   f"agent provider target {relative} is unavailable")
                stamp = os.lstat(target)
                update_file(target, existing.decode("utf-8"),
                            stat.S_IMODE(stamp.st_mode),
                            _provider_text(provider).decode("utf-8"))
                observed.append(_detail(relative, "updated"))
                continue
            payload = _provider_text(provider)
            create_file(relative, payload)
            observed.append(_detail(relative, "created"))
        for item in plan.details:
            if item.action == "note":
                observed.append(item)
    except Exception as error:
        incomplete = _rollback(root, created_files, created_dirs, updated_files)
        if incomplete:
            raise _problem(
                "state-unavailable",
                f"guidance apply failed ({error}); owned changes could not be "
                "fully restored: " + "; ".join(incomplete)) from error
        raise
    return RulesResult(changed=True, actions=plan.actions, details=tuple(observed))
