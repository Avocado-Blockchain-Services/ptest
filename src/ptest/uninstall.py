"""Reverse what init/doctor/runs set up for one checkout, plus `--self`.

Repository cleanup is anchored at the Git root (the same anchor `init`
uses) and only ever touches regular files below it through no-follow,
descriptor-relative operations. Machine-level cleanup is scoped to this
checkout's own entries: its `checkouts/<checkout_id>` directory and its
`checkout_id`-keyed scheduler rows. Shared records (other checkouts, the
review-model cache, machine config, domain marker) are never touched.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import agent_rules
from . import contracts as C
from . import files
from . import monorepo
from . import recommendations
from . import scheduler
from . import storage
from .project_facts import terminal_width
from .render import terminal_text

_PHASE = "uninstall"

REMOVE = "remove"
KEPT = "kept"
SKIPPED = "skipped"

_CONFIG_NAME = ".ptest.toml"
_CONFIG_MAX_BYTES = 256 * 1024
_FILE_MAX_BYTES = 256 * 1024
_GUIDE_REL = "docs/ptest-agent.md"
_BLOCK_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
_REPORT_NAME = "recommendations.md"
_CHECKOUTS_NAME = "checkouts"
_LEDGER_NAME = "coordinator.sqlite3"
_LEDGER_MAX_BYTES = 16 * 1024 * 1024

_ACTIVE_STATES = frozenset({
    C.LeaseState.QUEUED, C.LeaseState.GRANTED, C.LeaseState.RUNNING,
    C.LeaseState.DRAINING, C.LeaseState.FINALIZING,
    C.LeaseState.CANCELLING, C.LeaseState.UNCERTAIN,
})

_SKILL_PROVIDERS = ("claude", "codex", "opencode", "gemini")
_LEGACY_SKILL_REL = ".codex/skills/ptest/SKILL.md"


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE, retryable=False)


@dataclass(frozen=True, slots=True)
class PlanEntry:
    """One planned action; `target` is display text, `rel` is repo-relative."""

    action: str
    target: str
    detail: str
    kind: str
    rel: str = ""
    expect: bytes | None = None
    new_text: str | None = None
    identity: tuple | None = None


@dataclass(frozen=True, slots=True)
class RepoPlan:
    root: Path
    checkout_id: str
    entries: tuple = ()


@dataclass(frozen=True, slots=True)
class SelfPlan:
    requested: bool
    root: Path | None = None
    refused: str | None = None
    refused_path: str | None = None
    path_link: str | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    removed: tuple = ()
    kept: tuple = ()
    skipped: tuple = ()


def checkout_id_for(root: Path) -> str:
    """Key for "this checkout", the same formula history and the CLI use."""
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]


# -- small no-follow primitives ------------------------------------------------

def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _problem("state-unavailable", f"cannot inspect {path.name}") from None


def _open_dir(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise _problem("state-unavailable", f"directory {path} is unavailable") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _problem("unsafe-path", f"directory {path} is a symlink") from None
        raise _problem("state-unavailable", f"directory {path} is unavailable") from None


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _unlink_at(root: Path, parts: list[str]) -> None:
    """Unlink one leaf confined to `root` without following symlinks."""
    for part in parts:
        files.validate_single_name(part)
    root_fd = _open_dir(root)
    try:
        parent_fd = root_fd
        owned: list[int] = []
        cursor = root_fd
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=cursor)
            except FileNotFoundError:
                raise _problem("state-unavailable", f"path {parts[-1]} is unavailable") from None
            except OSError:
                raise _problem("unsafe-path", f"path {part} is unsafe") from None
            stamp = os.fstat(child)
            if not stat.S_ISDIR(stamp.st_mode):
                _close_quietly(child)
                raise _problem("unsafe-path", f"path {part} is not a directory")
            if cursor is not root_fd:
                owned.append(cursor)
            cursor = child
        parent_fd = cursor
        try:
            stamp = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise _problem("state-unavailable", f"path {parts[-1]} is unavailable") from None
        except OSError:
            raise _problem("unsafe-path", f"path {parts[-1]} is unsafe") from None
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            raise _problem("unsafe-path", f"path {parts[-1]} is not a regular file")
        os.unlink(parts[-1], dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        for fd in owned:
            _close_quietly(fd)
    finally:
        if parent_fd is not None and parent_fd is not root_fd:
            _close_quietly(parent_fd)
        _close_quietly(root_fd)


def _clear_dir_fd(fd: int) -> None:
    """Empty one open directory without following symlinks (links unlinked)."""
    with os.scandir(fd) as iterator:
        names = [entry.name for entry in iterator]
    for name in names:
        try:
            child_stamp = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            raise _problem("unsafe-path", f"path {name} is unsafe") from None
        if stat.S_ISDIR(child_stamp.st_mode) and not stat.S_ISLNK(child_stamp.st_mode):
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=fd)
            except OSError:
                raise _problem("unsafe-path", f"path {name} is unsafe") from None
            try:
                _clear_dir_fd(child_fd)
            finally:
                _close_quietly(child_fd)
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                raise _problem("state-unavailable", f"cannot remove {name}") from None
        else:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            except OSError:
                raise _problem("state-unavailable", f"cannot remove {name}") from None
    try:
        os.fsync(fd)
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    """Remove a directory tree without following symlinks (links unlinked)."""
    stamp = _lstat(path)
    if stamp is None:
        return
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        raise _problem("unsafe-path", f"path {path.name} is not a directory")
    root_fd = _open_dir(path)
    try:
        _clear_dir_fd(root_fd)
    finally:
        _close_quietly(root_fd)
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise _problem("state-unavailable", f"cannot remove {path.name}") from None


def _read_repo_file(root: Path, rel: str) -> bytes:
    return files.read_regular(root, rel, _FILE_MAX_BYTES + 1)


# -- config discovery ------------------------------------------------------------

def _parse_toml(raw: bytes) -> dict | None:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safe_child(value: object) -> str | None:
    """Validate one monorepo child declaration with the shared parser."""
    try:
        return "/".join(monorepo._safe_segments(value))
    except C.Problem:
        return None


def _config_entries(root: Path) -> tuple[list[PlanEntry], list[str]]:
    """Plan root + declared-child config removals. Returns entries + valid children."""
    entries: list[PlanEntry] = []
    valid_children: list[str] = []
    target = root / _CONFIG_NAME
    stamp = _lstat(target)
    if stamp is None:
        return entries, valid_children
    if stat.S_ISLNK(stamp.st_mode):
        entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "symlink", "skip"))
        return entries, valid_children
    if not stat.S_ISREG(stamp.st_mode):
        entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "not a regular file", "skip"))
        return entries, valid_children
    try:
        raw = _read_repo_file(root, _CONFIG_NAME)
    except C.Problem:
        entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "unreadable", "skip"))
        return entries, valid_children
    if len(raw) > _CONFIG_MAX_BYTES:
        entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "oversized", "skip"))
        return entries, valid_children
    data = _parse_toml(raw)
    version = data.get("version") if data else None
    children: list = []
    if version == 2:
        table = data.get("monorepo")
        if not isinstance(table, dict) or not isinstance(table.get("children"), list):
            entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "not a ptest config", "skip"))
            return entries, valid_children
        children = table["children"]
    if version not in (1, 2):
        entries.append(PlanEntry(SKIPPED, _CONFIG_NAME, "not a ptest config", "skip"))
        return entries, valid_children
    entries.append(PlanEntry(REMOVE, _CONFIG_NAME, "ptest config", "unlink",
                             rel=_CONFIG_NAME, expect=raw))
    if version != 2:
        return entries, valid_children
    seen: set[str] = set()
    for item in children:
        declaration = _safe_child(item)
        if declaration is None or declaration in seen:
            entries.append(PlanEntry(
                SKIPPED, str(item), "invalid monorepo child", "skip"))
            continue
        seen.add(declaration)
        child_dir = root.joinpath(*declaration.split("/"))
        dir_stamp = _lstat(child_dir)
        if dir_stamp is None:
            continue
        if stat.S_ISLNK(dir_stamp.st_mode) or not stat.S_ISDIR(dir_stamp.st_mode):
            entries.append(PlanEntry(
                SKIPPED, declaration, "unsafe child directory", "skip"))
            continue
        valid_children.append(declaration)
        child_rel = declaration + "/" + _CONFIG_NAME
        config_stamp = _lstat(child_dir / _CONFIG_NAME)
        if config_stamp is None:
            continue
        if stat.S_ISLNK(config_stamp.st_mode):
            entries.append(PlanEntry(SKIPPED, child_rel, "symlink", "skip"))
            continue
        if not stat.S_ISREG(config_stamp.st_mode):
            entries.append(PlanEntry(
                SKIPPED, child_rel, "not a regular file", "skip"))
            continue
        try:
            child_raw = _read_repo_file(root, child_rel)
        except C.Problem:
            entries.append(PlanEntry(SKIPPED, child_rel, "unreadable", "skip"))
            continue
        child_data = _parse_toml(child_raw)
        if not child_data or child_data.get("version") != 1:
            entries.append(PlanEntry(
                SKIPPED, child_rel, "not a ptest config", "skip"))
            continue
        entries.append(PlanEntry(REMOVE, child_rel, "ptest config", "unlink",
                                 rel=child_rel, expect=child_raw))
    return entries, valid_children


# -- guidance ---------------------------------------------------------------------

def _is_managed_guide(raw: bytes) -> bool:
    try:
        guide = agent_rules._guide()
    except C.Problem:
        return False
    if raw == guide:
        return True
    return hashlib.sha256(raw).hexdigest() == agent_rules._BASE_GUIDE_SHA256


def _guidance_entries(root: Path) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    docs = root / "docs"
    docs_stamp = _lstat(docs)
    if docs_stamp is not None and stat.S_ISLNK(docs_stamp.st_mode):
        entries.append(PlanEntry(SKIPPED, _GUIDE_REL, "symlink", "skip"))
    elif docs_stamp is not None and stat.S_ISDIR(docs_stamp.st_mode):
        guide_stamp = _lstat(root / _GUIDE_REL)
        if guide_stamp is not None:
            if stat.S_ISLNK(guide_stamp.st_mode) or not stat.S_ISREG(guide_stamp.st_mode):
                entries.append(PlanEntry(SKIPPED, _GUIDE_REL, "symlink", "skip"))
            else:
                try:
                    raw = _read_repo_file(root, _GUIDE_REL)
                except C.Problem:
                    entries.append(PlanEntry(SKIPPED, _GUIDE_REL, "unreadable", "skip"))
                    raw = None
                if raw is not None:
                    if len(raw) > _FILE_MAX_BYTES:
                        entries.append(PlanEntry(SKIPPED, _GUIDE_REL, "oversized", "skip"))
                    elif _is_managed_guide(raw):
                        entries.append(PlanEntry(
                            REMOVE, _GUIDE_REL, "managed guide", "unlink",
                            rel=_GUIDE_REL, expect=raw))
                    else:
                        entries.append(PlanEntry(KEPT, _GUIDE_REL, "edited", "keep"))
    for name in _BLOCK_FILES:
        target = root / name
        stamp = _lstat(target)
        if stamp is None:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            entries.append(PlanEntry(SKIPPED, name, "symlink", "skip"))
            continue
        try:
            raw = _read_repo_file(root, name)
        except C.Problem:
            entries.append(PlanEntry(SKIPPED, name, "unreadable", "skip"))
            continue
        if len(raw) > _FILE_MAX_BYTES:
            entries.append(PlanEntry(SKIPPED, name, "oversized", "skip"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            entries.append(PlanEntry(SKIPPED, name, "not UTF-8 text", "skip"))
            continue
        try:
            managed = agent_rules._managed_state(text, name)
        except C.Problem:
            entries.append(PlanEntry(SKIPPED, name, "unbalanced markers", "skip"))
            continue
        if not managed:
            continue
        block = agent_rules._block(name)
        if text == block:
            entries.append(PlanEntry(
                REMOVE, name, "init-created file holding only the managed block",
                "unlink", rel=name, expect=raw))
        elif text.endswith("\n\n" + block):
            entries.append(PlanEntry(
                REMOVE, name, "managed block", "rewrite", rel=name,
                expect=raw,
                new_text=text[:-(len(block) + 2)] + "\n"))
        else:
            entries.append(PlanEntry(KEPT, name, "edited", "keep"))
    for provider in _SKILL_PROVIDERS:
        rel = agent_rules._PROVIDER_SKILLS[provider]
        stamp = _lstat(root / rel)
        if stamp is None:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            entries.append(PlanEntry(SKIPPED, rel, "symlink", "skip"))
            continue
        try:
            raw = _read_repo_file(root, rel)
        except C.Problem:
            entries.append(PlanEntry(SKIPPED, rel, "unreadable", "skip"))
            continue
        managed = raw in (agent_rules._provider_text(provider),
                          agent_rules._legacy_provider_text(provider),
                          agent_rules._previous_provider_text(provider))
        if managed:
            entries.append(PlanEntry(
                REMOVE, rel, "managed skill", "unlink", rel=rel, expect=raw))
        else:
            entries.append(PlanEntry(KEPT, rel, "edited", "keep"))
    legacy_stamp = _lstat(root / _LEGACY_SKILL_REL)
    if legacy_stamp is not None:
        if stat.S_ISLNK(legacy_stamp.st_mode) or not stat.S_ISREG(legacy_stamp.st_mode):
            entries.append(PlanEntry(SKIPPED, _LEGACY_SKILL_REL, "symlink", "skip"))
        else:
            try:
                raw = _read_repo_file(root, _LEGACY_SKILL_REL)
            except C.Problem:
                entries.append(PlanEntry(
                    SKIPPED, _LEGACY_SKILL_REL, "unreadable", "skip"))
                raw = None
            if raw is not None:
                if raw == agent_rules._legacy_provider_text("codex"):
                    entries.append(PlanEntry(
                        REMOVE, _LEGACY_SKILL_REL, "managed skill", "unlink",
                        rel=_LEGACY_SKILL_REL, expect=raw))
                else:
                    entries.append(PlanEntry(KEPT, _LEGACY_SKILL_REL, "edited", "keep"))
    return entries


# -- reports ------------------------------------------------------------------------

def _report_entries(root: Path, children: list[str]) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    for prefix in ("", *children):
        rel = _REPORT_NAME if not prefix else prefix + "/" + _REPORT_NAME
        target = root.joinpath(*rel.split("/"))
        stamp = _lstat(target)
        if stamp is None:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            entries.append(PlanEntry(SKIPPED, rel, "symlink", "skip"))
            continue
        try:
            raw = files.read_regular(
                root, rel, recommendations._MAX_REPORT_BYTES + 257)
        except C.Problem:
            entries.append(PlanEntry(SKIPPED, rel, "unreadable", "skip"))
            continue
        try:
            recorded, body = recommendations._split_marker(raw)
        except C.Problem:
            continue
        if recorded == hashlib.sha256(body).hexdigest():
            entries.append(PlanEntry(
                REMOVE, rel, "un-edited report", "unlink", rel=rel, expect=raw))
        else:
            entries.append(PlanEntry(KEPT, rel, "edited", "keep"))
    return entries


# -- machine state ---------------------------------------------------------------------

def _active_checkout_ids(domain: C.DomainPaths) -> set[str]:
    try:
        leases = scheduler.reconcile(domain)
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return set()
        raise
    return {item.checkout_id for item in leases
            if item.state in _ACTIVE_STATES}


def _ledger_rows(domain: C.DomainPaths, checkout_id: str) -> int:
    stamp = _lstat(domain.ledger)
    if stamp is None:
        return 0
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", "coordinator ledger is unsafe")
    try:
        conn = storage.open_database(
            domain.root, _LEDGER_NAME, max_bytes=_LEDGER_MAX_BYTES)
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return 0
        raise
    try:
        row = conn.execute("SELECT COUNT(*) FROM jobs WHERE checkout_id=?",
                           (checkout_id,)).fetchone()
        return int(row[0])
    except sqlite3.Error:
        raise _problem("coordinator-corrupt", "coordinator ledger is unreadable")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _delete_ledger_rows(domain: C.DomainPaths, checkout_id: str) -> None:
    try:
        conn = storage.open_database(
            domain.root, _LEDGER_NAME, max_bytes=_LEDGER_MAX_BYTES)
    except C.Problem as exc:
        if exc.code == "state-unavailable":
            return
        raise
    try:
        conn.execute("DELETE FROM jobs WHERE checkout_id=?", (checkout_id,))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE checkout_id=?",
            (checkout_id,)).fetchone()[0]
        if int(remaining):
            raise _problem("state-unavailable", "checkout ledger rows could not be removed")
    except C.Problem:
        raise
    except sqlite3.Error:
        raise _problem("coordinator-corrupt", "coordinator ledger is unreadable")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _state_entries(domain: C.DomainPaths, checkout_id: str) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    if checkout_id in _active_checkout_ids(domain):
        raise _problem("active-run",
                       "a ptest run for this checkout is active; refusing to uninstall")
    checkout_dir = domain.root / _CHECKOUTS_NAME / checkout_id
    stamp = _lstat(checkout_dir)
    if stamp is not None:
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            entries.append(PlanEntry(
                SKIPPED, str(checkout_dir), "unsafe checkout state", "skip"))
        else:
            entries.append(PlanEntry(
                REMOVE, str(checkout_dir), "checkout state", "state-dir",
                identity=(stamp.st_dev, stamp.st_ino)))
    try:
        rows = _ledger_rows(domain, checkout_id)
    except C.Problem as exc:
        entries.append(PlanEntry(
            SKIPPED, str(domain.ledger), exc.code, "skip"))
        rows = 0
    if rows:
        entries.append(PlanEntry(
            REMOVE, str(domain.ledger), "checkout ledger rows", "ledger"))
    return entries


# -- plan / apply --------------------------------------------------------------------------

def plan_repo(root: Path, domain: C.DomainPaths) -> RepoPlan:
    """Inspect only; never writes. Raises on an active run."""
    root = Path(root)
    checkout_id = checkout_id_for(root)
    entries: list[PlanEntry] = []
    config_entries, children = _config_entries(root)
    entries.extend(config_entries)
    entries.extend(_guidance_entries(root))
    entries.extend(_report_entries(root, children))
    entries.extend(_state_entries(domain, checkout_id))
    return RepoPlan(root=root, checkout_id=checkout_id,
                    entries=tuple(entries))


def _prune_skill_dirs(root: Path, rel: str) -> None:
    parts = rel.split("/")[:-1]
    for depth in range(len(parts), 0, -1):
        candidate = root.joinpath(*parts[:depth])
        try:
            os.rmdir(candidate)
        except OSError:
            continue


def apply_repo(plan: RepoPlan, domain: C.DomainPaths) -> Applied:
    """Execute a plan with per-file re-verification; fail closed per entry."""
    if plan.checkout_id in _active_checkout_ids(domain):
        raise _problem("active-run",
                       "a ptest run for this checkout is active; refusing to uninstall")
    removed: list[str] = []
    kept: list[str] = []
    skipped: list[str] = []
    root = plan.root
    for entry in plan.entries:
        if entry.action == KEPT:
            kept.append(entry.target)
            continue
        if entry.action == SKIPPED:
            skipped.append(entry.target)
            continue
        try:
            if entry.kind == "unlink" and entry.rel:
                _apply_unlink(root, entry)
                removed.append(entry.target)
                if entry.rel.endswith("/SKILL.md"):
                    _prune_skill_dirs(root, entry.rel)
                if entry.rel == _GUIDE_REL:
                    try:
                        os.rmdir(root / "docs")
                    except OSError:
                        pass
            elif entry.kind == "rewrite" and entry.rel and entry.new_text is not None:
                _apply_rewrite(root, entry)
                removed.append(entry.target)
            elif entry.kind == "state-dir":
                _apply_state_dir(entry)
                removed.append(entry.target)
            elif entry.kind == "ledger":
                _delete_ledger_rows(domain, plan.checkout_id)
                removed.append(entry.target)
            else:
                raise _problem("invalid-config", f"unknown plan entry {entry.target}")
        except C.Problem:
            skipped.append(entry.target)
    failures = [entry.target for entry in plan.entries
                if entry.action == REMOVE and entry.target not in removed]
    if failures:
        raise _problem("uninstall-incomplete",
                       "could not remove: " + ", ".join(failures[:5]))
    return Applied(removed=tuple(removed), kept=tuple(kept), skipped=tuple(skipped))


def _apply_unlink(root: Path, entry: PlanEntry) -> None:
    assert entry.rel and entry.expect is not None
    try:
        current = _read_repo_file(root, entry.rel)
    except C.Problem:
        stamp = _lstat(root / entry.rel)
        if stamp is None:
            return
        raise _problem("unsafe-path", f"path {entry.target} changed during apply")
    if current != entry.expect:
        raise _problem("unsafe-path", f"path {entry.target} changed during apply")
    _unlink_at(root, entry.rel.split("/"))


def _apply_rewrite(root: Path, entry: PlanEntry) -> None:
    assert entry.rel and entry.expect is not None and entry.new_text is not None
    target = root / entry.rel
    stamp = _lstat(target)
    if stamp is None:
        raise _problem("state-unavailable", f"path {entry.target} is unavailable")
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", f"path {entry.target} is unsafe")
    agent_rules._replace(root, target, entry.new_text,
                         mode=stat.S_IMODE(stamp.st_mode),
                         expect_bytes=entry.expect)


def _apply_state_dir(entry: PlanEntry) -> None:
    target = Path(entry.target)
    stamp = _lstat(target)
    if stamp is None:
        return
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        raise _problem("unsafe-path", f"path {target.name} is unsafe")
    if entry.identity is not None and (stamp.st_dev, stamp.st_ino) != entry.identity:
        raise _problem("unsafe-path", f"path {target.name} changed during apply")
    _remove_tree(target)


# -- --self --------------------------------------------------------------------------

def _default_install_root() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local" / "ptest"


def _root_from_binary(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        real = Path(os.path.realpath(value))
    except OSError:
        return None
    parts = real.parts
    if ".ptest-bundles" not in parts:
        return None
    root = Path(*parts[:parts.index(".ptest-bundles")])
    try:
        stamp = os.lstat(root / ".ptest-bundles")
    except OSError:
        return None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        return None
    return root


def _has_install_layout(root: Path) -> bool:
    bundles = root / ".ptest-bundles"
    try:
        stamp = os.lstat(bundles)
    except OSError:
        return False
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        return False
    try:
        with os.scandir(bundles) as iterator:
            names = [entry.name for entry in iterator
                     if not entry.is_symlink()]
    except OSError:
        return False
    for name in names:
        marker = bundles / name / "complete.json"
        try:
            stamp = os.lstat(marker)
            if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
                continue
            with open(marker, "rb") as stream:
                data = json.loads(stream.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if (isinstance(data, dict) and data.get("version") == 1
                and data.get("bundle_id") == name):
            return True
    return False


def plan_self() -> SelfPlan:
    """Locate the install root without touching anything.

    Every existing candidate is checked for an install.sh layout before
    any refusal: a stale candidate never vetoes a valid install root.
    """
    candidates: list[Path] = []
    located = shutil.which("ptest")
    for source in (sys.argv[0] if sys.argv else None, located):
        root = _root_from_binary(source)
        if root is not None and root not in candidates:
            candidates.append(root)
    default = _default_install_root()
    if default not in candidates:
        candidates.append(default)
    existing: list[Path] = []
    for root in candidates:
        stamp = _lstat(root)
        if stamp is None:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            continue
        if _has_install_layout(root):
            link: str | None = None
            if located:
                try:
                    if os.path.islink(located):
                        resolved = Path(os.path.realpath(located))
                        if resolved == root or root in resolved.parents:
                            link = located
                except OSError:
                    link = None
            return SelfPlan(requested=True, root=root, path_link=link)
        existing.append(root)
    if existing:
        return SelfPlan(requested=True, refused="not an install.sh layout",
                        refused_path=str(existing[0]))
    return SelfPlan(requested=True)


def apply_self(plan: SelfPlan) -> tuple[bool, bool]:
    """Remove the install root and its PATH symlink. Returns (removed, link)."""
    assert plan.requested and plan.root is not None and plan.refused is None
    _remove_tree(plan.root)
    link_removed = False
    if plan.path_link:
        try:
            stamp = os.lstat(plan.path_link)
            if stat.S_ISLNK(stamp.st_mode):
                resolved = Path(os.path.realpath(plan.path_link))
                if resolved == plan.root or plan.root in resolved.parents:
                    os.unlink(plan.path_link)
                    link_removed = True
        except OSError:
            pass
    return True, link_removed


# -- rendering + JSON payload ----------------------------------------------------------

def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


def render_text(plan: RepoPlan, *, applied: Applied | None = None,
               dry_run: bool = False, width: int | None = None,
               self_plan: SelfPlan | None = None) -> str:
    """Plain grouped plan/result rendering for human terminals."""
    term = terminal_width(width)
    header = ("ptest uninstall preview (dry run, changes nothing)"
              if dry_run else "ptest uninstall plan")
    if applied is not None:
        header = "ptest uninstall result"
    lines = [f"{header} for {terminal_text(plan.root)}"]
    groups: dict[str, list[PlanEntry]] = {REMOVE: [], KEPT: [], SKIPPED: []}
    for entry in plan.entries:
        groups[entry.action].append(entry)
    labels = {REMOVE: "remove:", KEPT: "kept (edited):", SKIPPED: "skipped:"}
    budget = max(16, term - 4)
    for action in (REMOVE, KEPT, SKIPPED):
        items = groups[action]
        if not items:
            continue
        lines.append(f"  {labels[action]}")
        for entry in items:
            target = _truncate(terminal_text(entry.target), budget)
            detail = _truncate(terminal_text(entry.detail), budget)
            lines.append(f"    {target} ({detail})")
    if self_plan is not None and self_plan.requested:
        lines.append("  self:")
        if self_plan.root is not None:
            lines.append(f"    {_truncate(terminal_text(self_plan.root), budget)} (install root)")
            if self_plan.path_link:
                lines.append(f"    {_truncate(terminal_text(self_plan.path_link), budget)} (PATH symlink)")
        elif self_plan.refused:
            lines.append(f"    {_truncate(terminal_text(self_plan.refused_path or ''), budget)}"
                         f" ({terminal_text(self_plan.refused)})")
        else:
            lines.append("    no install root found (nothing to do)")
    removes = [entry for entry in plan.entries if entry.action == REMOVE]
    self_removes = (self_plan is not None and self_plan.requested
                    and self_plan.root is not None)
    if applied is not None:
        if not removes and not self_removes:
            lines.append("nothing to remove")
        else:
            lines.append(
                f"removed {len(applied.removed)}, kept {len(applied.kept)}, "
                f"skipped {len(applied.skipped)}")
    else:
        if not removes and not self_removes:
            lines.append("nothing to remove")
    return "\n".join(lines) + "\n"


def _entry_payload(entry: PlanEntry) -> dict:
    return {"action": entry.action, "target": entry.target,
            "detail": entry.detail}


def document_data(plan: RepoPlan, *, applied: Applied | None,
                  dry_run: bool, self_plan: SelfPlan | None,
                  self_removed: bool = False,
                  self_link_removed: bool = False) -> dict:
    """Allowlisted uninstall payload for the public JSON document."""
    removes = [entry.target for entry in plan.entries if entry.action == REMOVE]
    self_work = (self_plan is not None and self_plan.requested
                 and self_plan.root is not None)
    if applied is None:
        result = {"removed": [], "kept": [], "skipped": [],
                  "nothing_to_remove": not removes and not self_work}
    else:
        result = {"removed": list(applied.removed), "kept": list(applied.kept),
                  "skipped": list(applied.skipped),
                  "nothing_to_remove": not applied.removed and not self_removed}
    if self_plan is None:
        self_payload: dict = {"requested": False, "root": None,
                              "removed": False, "path_symlink_removed": False}
    else:
        self_payload = {
            "requested": True,
            "root": None if self_plan.root is None else str(self_plan.root),
            "removed": self_removed,
            "path_symlink_removed": self_link_removed,
        }
    return {"root": str(plan.root), "dry_run": dry_run,
            "plan": [_entry_payload(entry) for entry in plan.entries],
            "result": result, "self": self_payload}
