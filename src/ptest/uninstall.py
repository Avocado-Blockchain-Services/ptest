"""Reverse what init/doctor/runs set up for one checkout, plus `--self`.

Repository cleanup is anchored at the Git root (the same anchor `init`
uses) and only ever touches regular files below it through no-follow,
descriptor-relative operations. Machine-level cleanup is scoped to this
checkout's own entries: the root checkout id plus the checkout id of
every declared monorepo child that exists
(``sha256(realpath(<repo>/<child>))``, the same formula runs use), each
with its `checkouts/<id>` directory and its id-keyed scheduler rows.
Shared records (other checkouts, the review-model cache, machine
config, domain marker) are never touched.
"""
from __future__ import annotations

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
    scope: str = ""


@dataclass(frozen=True, slots=True)
class RepoPlan:
    root: Path
    checkout_id: str
    entries: tuple = ()
    checkout_ids: tuple = ()


@dataclass(frozen=True, slots=True)
class SelfPlan:
    requested: bool
    root: Path | None = None
    refused: str | None = None
    refused_path: str | None = None
    path_link: str | None = None
    identity: tuple | None = None
    bundles: tuple = ()


@dataclass(frozen=True, slots=True)
class SelfApplied:
    removed: bool = False
    link_removed: bool = False
    kept: tuple = ()


@dataclass(frozen=True, slots=True)
class Applied:
    removed: tuple = ()
    kept: tuple = ()
    skipped: tuple = ()


def checkout_id_for(root: Path) -> str:
    """Key for "this checkout", the same formula history and the CLI use."""
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:32]


# -- small no-follow primitives (files.py owns open/walk/close) ------------------

def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _problem("state-unavailable", f"cannot inspect {path.name}") from None


def _unlink_at(root: Path, parts: list[str]) -> None:
    """Unlink one leaf confined to `root` without following symlinks."""
    for part in parts:
        files.validate_single_name(part)
    root_fd = files._open_dir(Path(root))
    parent_fd = None
    try:
        parent_fd = files._walk_to_parent(root_fd, parts)
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
    finally:
        if parent_fd is not None and parent_fd is not root_fd:
            files._close_quietly(parent_fd)
        files._close_quietly(root_fd)


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
                files._close_quietly(child_fd)
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
    root_fd = files._open_dir(path)
    try:
        _clear_dir_fd(root_fd)
    finally:
        files._close_quietly(root_fd)
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise _problem("state-unavailable", f"cannot remove {path.name}") from None


def _classify(root: Path, rel: str, *, limit: int, decide) -> PlanEntry | None:
    """One shared lstat/read ladder for repo files.

    Returns None when the path is absent, a SKIPPED entry when it is a
    symlink, a non-regular file, unreadable, or oversized, else the
    verdict of ``decide(raw)`` (a PlanEntry, or None when the file is
    not ptest's and earns no entry).
    """
    stamp = _lstat(root.joinpath(*rel.split("/")))
    if stamp is None:
        return None
    if stat.S_ISLNK(stamp.st_mode):
        return PlanEntry(SKIPPED, rel, "symlink", "skip")
    if not stat.S_ISREG(stamp.st_mode):
        return PlanEntry(SKIPPED, rel, "not a regular file", "skip")
    try:
        raw = files.read_regular(root, rel, limit + 1)
    except C.Problem:
        return PlanEntry(SKIPPED, rel, "unreadable", "skip")
    if len(raw) > limit:
        return PlanEntry(SKIPPED, rel, "oversized", "skip")
    return decide(raw)


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


def _decide_root_config(raw: bytes, state: dict) -> PlanEntry | None:
    data = _parse_toml(raw)
    version = data.get("version") if data else None
    if version == 2:
        table = data.get("monorepo")
        if not isinstance(table, dict) or not isinstance(table.get("children"), list):
            return PlanEntry(SKIPPED, _CONFIG_NAME, "not a ptest config", "skip")
        state["children"] = table["children"]
    if version not in (1, 2):
        return PlanEntry(SKIPPED, _CONFIG_NAME, "not a ptest config", "skip")
    state["version"] = version
    return PlanEntry(REMOVE, _CONFIG_NAME, "ptest config", "unlink",
                     rel=_CONFIG_NAME, expect=raw)


def _decide_child_config(child_rel: str):
    def decide(raw: bytes) -> PlanEntry | None:
        child_data = _parse_toml(raw)
        if not child_data or child_data.get("version") != 1:
            return PlanEntry(SKIPPED, child_rel, "not a ptest config", "skip")
        return PlanEntry(REMOVE, child_rel, "ptest config", "unlink",
                         rel=child_rel, expect=raw)
    return decide


def _config_entries(root: Path) -> tuple[list[PlanEntry], list[str]]:
    """Plan root + declared-child config removals. Returns entries + valid children."""
    entries: list[PlanEntry] = []
    valid_children: list[str] = []
    state: dict = {}
    entry = _classify(root, _CONFIG_NAME, limit=_CONFIG_MAX_BYTES,
                      decide=lambda raw: _decide_root_config(raw, state))
    if entry is not None:
        entries.append(entry)
    if state.get("version") != 2:
        return entries, valid_children
    seen: set[str] = set()
    for item in state["children"]:
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
        child_entry = _classify(root, child_rel, limit=_CONFIG_MAX_BYTES,
                                decide=_decide_child_config(child_rel))
        if child_entry is not None:
            entries.append(child_entry)
    return entries, valid_children


# -- guidance ---------------------------------------------------------------------

def _is_managed_guide(raw: bytes) -> bool:
    try:
        guide = agent_rules._guide()
    except C.Problem:
        return False
    if raw == guide:
        return True
    return hashlib.sha256(raw).hexdigest() in agent_rules._PREVIOUS_GUIDE_SHA256S


def _decide_guide(raw: bytes) -> PlanEntry | None:
    if _is_managed_guide(raw):
        return PlanEntry(REMOVE, _GUIDE_REL, "managed guide", "unlink",
                         rel=_GUIDE_REL, expect=raw)
    return PlanEntry(KEPT, _GUIDE_REL, "edited", "keep")


def _decide_block(name: str):
    def decide(raw: bytes) -> PlanEntry | None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return PlanEntry(SKIPPED, name, "not UTF-8 text", "skip")
        try:
            managed = agent_rules._managed_state(text, name)
        except C.Problem:
            return PlanEntry(SKIPPED, name, "unbalanced markers", "skip")
        if not managed:
            return None
        block = agent_rules._block(name)
        if text == block:
            return PlanEntry(
                REMOVE, name, "init-created file holding only the managed block",
                "unlink", rel=name, expect=raw)
        if text.endswith("\n\n" + block):
            return PlanEntry(
                REMOVE, name, "managed block", "rewrite", rel=name,
                expect=raw,
                new_text=text[:-(len(block) + 2)] + "\n")
        return PlanEntry(KEPT, name, "edited", "keep")
    return decide


def _decide_skill(rel: str, provider: str):
    def decide(raw: bytes) -> PlanEntry | None:
        managed = raw in (agent_rules._provider_text(provider),
                          agent_rules._legacy_provider_text(provider),
                          agent_rules._previous_provider_text(provider),
                          agent_rules._pre_gate_provider_text(provider),
                          agent_rules._pre_changed_provider_text(provider))
        if managed:
            return PlanEntry(
                REMOVE, rel, "managed skill", "unlink", rel=rel, expect=raw)
        return PlanEntry(KEPT, rel, "edited", "keep")
    return decide


def _guidance_entries(root: Path) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    docs = root / "docs"
    docs_stamp = _lstat(docs)
    if docs_stamp is not None and stat.S_ISLNK(docs_stamp.st_mode):
        entries.append(PlanEntry(SKIPPED, _GUIDE_REL, "symlink", "skip"))
    elif docs_stamp is not None and stat.S_ISDIR(docs_stamp.st_mode):
        entry = _classify(root, _GUIDE_REL, limit=_FILE_MAX_BYTES,
                          decide=_decide_guide)
        if entry is not None:
            entries.append(entry)
    for name in _BLOCK_FILES:
        entry = _classify(root, name, limit=_FILE_MAX_BYTES,
                          decide=_decide_block(name))
        if entry is not None:
            entries.append(entry)
    for provider in _SKILL_PROVIDERS:
        rel = agent_rules._PROVIDER_SKILLS[provider]
        entry = _classify(root, rel, limit=_FILE_MAX_BYTES,
                          decide=_decide_skill(rel, provider))
        if entry is not None:
            entries.append(entry)
    return entries


# -- reports ------------------------------------------------------------------------

def _decide_report(rel: str):
    def decide(raw: bytes) -> PlanEntry | None:
        try:
            recorded, body = recommendations._split_marker(raw)
        except C.Problem:
            return None
        if recorded == hashlib.sha256(body).hexdigest():
            return PlanEntry(
                REMOVE, rel, "un-edited report", "unlink", rel=rel, expect=raw)
        return PlanEntry(KEPT, rel, "edited", "keep")
    return decide


def _report_entries(root: Path, children: list[str]) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    for prefix in ("", *children):
        rel = _REPORT_NAME if not prefix else prefix + "/" + _REPORT_NAME
        entry = _classify(
            root, rel, limit=recommendations._MAX_REPORT_BYTES + 256,
            decide=_decide_report(rel))
        if entry is not None:
            entries.append(entry)
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
    """Count rows read-only; missing ledger means no rows, never a write."""
    stamp = _lstat(domain.ledger)
    if stamp is None:
        return 0
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path", "coordinator ledger is unsafe")
    try:
        conn = storage.open_database(
            domain.root, _LEDGER_NAME, max_bytes=_LEDGER_MAX_BYTES,
            read_only=True)
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


def _state_entries(domain: C.DomainPaths,
                   checkout_ids: list[str]) -> list[PlanEntry]:
    """Plan state removal for the root id plus every existing child id."""
    entries: list[PlanEntry] = []
    if any(cid in _active_checkout_ids(domain) for cid in checkout_ids):
        raise _problem("active-run",
                       "a ptest run for this checkout is active; refusing to uninstall")
    for checkout_id in checkout_ids:
        checkout_dir = domain.root / _CHECKOUTS_NAME / checkout_id
        stamp = _lstat(checkout_dir)
        if stamp is not None:
            if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
                entries.append(PlanEntry(
                    SKIPPED, str(checkout_dir), "unsafe checkout state", "skip"))
            else:
                entries.append(PlanEntry(
                    REMOVE, str(checkout_dir), "checkout state", "state-dir",
                    identity=(stamp.st_dev, stamp.st_ino),
                    scope=checkout_id))
        try:
            rows = _ledger_rows(domain, checkout_id)
        except C.Problem as exc:
            entries.append(PlanEntry(
                SKIPPED, str(domain.ledger), exc.code, "skip"))
            rows = 0
        if rows:
            entries.append(PlanEntry(
                REMOVE, str(domain.ledger), "checkout ledger rows", "ledger",
                scope=checkout_id))
    return entries


# -- plan / apply --------------------------------------------------------------------------

def _checkout_ids_for(root: Path, children: list[str]) -> list[str]:
    """Root id plus the id of every declared child that exists.

    Children use the same formula runs use:
    ``sha256(realpath(<repo>/<child>))``.
    """
    ids = [checkout_id_for(root)]
    for child in children:
        child_id = checkout_id_for(root.joinpath(*child.split("/")))
        if child_id not in ids:
            ids.append(child_id)
    return ids


def plan_repo(root: Path, domain: C.DomainPaths) -> RepoPlan:
    """Inspect only; never writes. Raises on an active run."""
    root = Path(root)
    checkout_id = checkout_id_for(root)
    entries: list[PlanEntry] = []
    config_entries, children = _config_entries(root)
    entries.extend(config_entries)
    entries.extend(_guidance_entries(root))
    entries.extend(_report_entries(root, children))
    ids = _checkout_ids_for(root, children)
    entries.extend(_state_entries(domain, ids))
    return RepoPlan(root=root, checkout_id=checkout_id,
                    entries=tuple(entries), checkout_ids=tuple(ids))


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
    ids = list(plan.checkout_ids) or [plan.checkout_id]
    # Scheduler-owned locked check-and-delete for every planned id, before
    # state dirs go. A run admitted after planning still refuses first; with
    # no rows this deletes nothing and returns 0.
    try:
        scheduler.forget_checkouts(domain, ids)
    except C.Problem as exc:
        if exc.code == "state-unavailable" and _lstat(domain.ledger) is None:
            pass
        else:
            raise
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
        current = files.read_regular(root, entry.rel, _FILE_MAX_BYTES + 1)
    except C.Problem:
        stamp = _lstat(root.joinpath(*entry.rel.split("/")))
        if stamp is None:
            return
        raise _problem("unsafe-path", f"path {entry.target} changed during apply")
    if current != entry.expect:
        raise _problem("unsafe-path", f"path {entry.target} changed during apply")
    _unlink_at(root, files._split_relative(entry.rel))


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


def _is_bundle_dir(bundles: Path, name: str) -> bool:
    """One entry is an installer bundle: a real dir with a matching marker."""
    try:
        stamp = os.lstat(bundles / name)
    except OSError:
        return False
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        return False
    marker = bundles / name / "complete.json"
    try:
        stamp = os.lstat(marker)
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode):
            return False
        with open(marker, "rb") as stream:
            data = json.loads(stream.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return (isinstance(data, dict) and data.get("version") == 1
            and data.get("bundle_id") == name)


def _bundles_dir(root: Path) -> Path | None:
    bundles = root / ".ptest-bundles"
    try:
        stamp = os.lstat(bundles)
    except OSError:
        return None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        return None
    return bundles


def _lenient_bundle_ids(root: Path) -> tuple[str, ...]:
    """Valid bundle ids for candidate selection; () when there is no layout."""
    bundles = _bundles_dir(root)
    if bundles is None:
        return ()
    try:
        names = sorted(os.listdir(bundles))
    except OSError:
        return ()
    return tuple(name for name in names if _is_bundle_dir(bundles, name))


def _non_bundle_entries(root: Path) -> tuple[str, ...]:
    """Entries inside `.ptest-bundles` that install.sh never creates."""
    bundles = _bundles_dir(root)
    if bundles is None:
        return ()
    try:
        names = sorted(os.listdir(bundles))
    except OSError:
        return ()
    return tuple(name for name in names
                 if not _is_bundle_dir(bundles, name))


def _self_temp_links(root: Path) -> tuple[str, ...]:
    """Leftover installer temp symlinks (`.ptest-link-*`), links only."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ()
    found = []
    for name in names:
        if not name.startswith(".ptest-link-"):
            continue
        try:
            stamp = os.lstat(root / name)
        except OSError:
            continue
        if stat.S_ISLNK(stamp.st_mode):
            found.append(name)
    return tuple(found)


def _public_link_in_bundles(root: Path) -> bool:
    """`<root>/ptest` qualifies only as a symlink into `.ptest-bundles`."""
    public = root / "ptest"
    try:
        stamp = os.lstat(public)
    except OSError:
        return False
    if not stat.S_ISLNK(stamp.st_mode):
        return False
    try:
        resolved = Path(os.path.realpath(public))
    except OSError:
        return False
    bundles = root / ".ptest-bundles"
    return resolved == bundles or bundles in resolved.parents


def _path_link_for(root: Path) -> str | None:
    """A PATH launcher counts only for the link check, never for roots.

    Consults the live ``PATH`` lookup plus the known launcher
    ``~/.local/bin/ptest`` under the passwd home; returns the first one
    that is a symlink resolving into the root being removed.
    """
    known = (Path(pwd.getpwuid(os.getuid()).pw_dir)
             / ".local" / "bin" / "ptest")
    seen: list[str] = []
    for candidate in (shutil.which("ptest"), str(known)):
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        try:
            if os.path.islink(candidate):
                resolved = Path(os.path.realpath(candidate))
                if resolved == root or root in resolved.parents:
                    return candidate
        except OSError:
            continue
    return None


def plan_self() -> SelfPlan:
    """Locate the install root without touching anything.

    Every existing candidate is checked for an install.sh layout before
    any refusal: a stale candidate never vetoes a valid install root.

    Roots come only from the running binary (``sys.argv[0]`` or this
    file) or the default install root. The ``PATH`` lookup and the known
    launcher ``~/.local/bin/ptest`` feed only the PATH-link check: a
    link is removed only when it resolves into the root being removed.
    The caller prints the planned root before asking for confirmation.
    """
    candidates: list[Path] = []
    running = str(Path(__file__).resolve())
    for source in (sys.argv[0] if sys.argv else None, running):
        root = _root_from_binary(source)
        if root is not None and root not in candidates:
            candidates.append(root)
    # Realpath the default so link checks compare like with like: the
    # passwd home may itself sit behind a symlink.
    default = Path(os.path.realpath(_default_install_root()))
    if default not in candidates:
        candidates.append(default)
    existing: list[Path] = []
    selected: Path | None = None
    selected_ids: tuple[str, ...] = ()
    for root in candidates:
        stamp = _lstat(root)
        if stamp is None:
            continue
        if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
            continue
        ids = _lenient_bundle_ids(root)
        if ids:
            selected, selected_ids = root, ids
            break
        existing.append(root)
    if selected is None:
        if existing:
            return SelfPlan(requested=True, refused="not an install.sh layout",
                            refused_path=str(existing[0]))
        return SelfPlan(requested=True)
    bad = _non_bundle_entries(selected)
    if bad:
        return SelfPlan(
            requested=True, refused="install root holds non-bundle entries: "
            + ", ".join(bad[:5]), refused_path=str(selected))
    link = _path_link_for(selected)
    stamp = _lstat(selected)
    assert stamp is not None
    return SelfPlan(requested=True, root=selected, path_link=link,
                    identity=(stamp.st_dev, stamp.st_ino),
                    bundles=selected_ids)


def _check_self_plan(plan: SelfPlan) -> Path:
    """Re-verify the planned root identity and layout at apply time."""
    assert plan.requested and plan.root is not None and plan.refused is None
    root = plan.root
    try:
        stamp = os.lstat(root)
    except OSError:
        raise _problem("state-unavailable",
                       f"install root {root} is unavailable") from None
    if stat.S_ISLNK(stamp.st_mode) or not stat.S_ISDIR(stamp.st_mode):
        raise _problem("unsafe-path",
                       f"install root {root} is not a directory") from None
    if plan.identity is not None and (stamp.st_dev, stamp.st_ino) != plan.identity:
        raise _problem("unsafe-path",
                       f"install root {root} changed during apply")
    bad = _non_bundle_entries(root)
    if bad:
        raise _problem("not-install-layout",
                       "refusing --self: install root holds non-bundle "
                       "entries: " + ", ".join(bad[:5]))
    ids = _lenient_bundle_ids(root)
    if not ids:
        raise _problem("not-install-layout",
                       f"refusing --self: {root} is not an install.sh layout")
    if plan.bundles and set(ids) != set(plan.bundles):
        raise _problem("unsafe-path",
                       f"install root {root} changed during apply")
    return root


def apply_self(plan: SelfPlan) -> SelfApplied:
    """Remove only installer-created entries; keep user files and the root.

    Deletes every bundle dir, `<root>/ptest` (only when it is a symlink
    into `<root>/.ptest-bundles`), and leftover `.ptest-link-*` temp
    symlinks, then removes the root itself only when it is empty.
    """
    root = _check_self_plan(plan)
    bundles = root / ".ptest-bundles"
    for name in sorted(os.listdir(bundles)):
        # Per-entry recheck: refuse anything planted after the plan check.
        if not _is_bundle_dir(bundles, name):
            raise _problem("not-install-layout",
                           "refusing --self: install root changed during apply")
        _remove_tree(bundles / name)
    if _public_link_in_bundles(root):
        try:
            os.unlink(root / "ptest")
        except OSError:
            raise _problem("state-unavailable",
                           "could not remove the installer symlink") from None
    for name in _self_temp_links(root):
        try:
            stamp = os.lstat(root / name)
            if stat.S_ISLNK(stamp.st_mode):
                os.unlink(root / name)
        except OSError:
            raise _problem("state-unavailable",
                           f"could not remove {name}") from None
    link_removed = False
    if plan.path_link:
        try:
            stamp = os.lstat(plan.path_link)
            if stat.S_ISLNK(stamp.st_mode):
                resolved = Path(os.path.realpath(plan.path_link))
                if resolved == root or root in resolved.parents:
                    os.unlink(plan.path_link)
                    link_removed = True
        except OSError:
            pass
    try:
        os.rmdir(bundles)
    except OSError:
        raise _problem("unsafe-path",
                       "install root changed during apply") from None
    try:
        leftovers = sorted(os.listdir(root))
    except OSError:
        raise _problem("state-unavailable",
                       f"install root {root} is unavailable") from None
    if not leftovers:
        try:
            os.rmdir(root)
        except OSError:
            raise _problem("state-unavailable",
                           f"could not remove install root {root}") from None
        return SelfApplied(removed=True, link_removed=link_removed)
    return SelfApplied(removed=True, link_removed=link_removed,
                       kept=tuple(leftovers))


# -- rendering + JSON payload ----------------------------------------------------------

def _state_facts(domain: C.DomainPaths | None) -> tuple[str | None, bool]:
    """Machine-state location the plan inspected, plus env selection.

    Mirrors the ``where``/``status`` ``domain_root``/``domain_from_env``
    naming: the flag is true only when ``PTEST_STATE_DIR`` selected a
    non-fixture domain.
    """
    if domain is None:
        return None, False
    return str(domain.root), bool(
        not domain.fixture and os.environ.get("PTEST_STATE_DIR"))


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


def _wrap_text(text: str, width: int) -> list[str]:
    """Wrap without truncating: a hidden root is a dangerous root."""
    width = max(1, width)
    return [text[start:start + width] for start in range(0, len(text), width)] or [""]


def render_text(plan: RepoPlan, *, applied: Applied | None = None,
               dry_run: bool = False, width: int | None = None,
               self_plan: SelfPlan | None = None,
               self_kept: tuple = (),
               summary_only: bool = False,
               domain: C.DomainPaths | None = None) -> str:
    """Plain grouped plan/result rendering for human terminals.

    ``summary_only`` (with ``applied``) emits just the trailing summary
    line plus any ``--self`` kept lines, for callers that already
    printed the plan before consent.
    """
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
    state_root, state_from_env = _state_facts(domain)
    state_text = ("<unresolved>" if state_root is None
                  else terminal_text(state_root))
    chunks = _wrap_text(state_text, budget)
    lines.append(f"  state: {chunks[0]}")
    for chunk in chunks[1:]:
        lines.append(f"      {chunk}")
    if state_from_env:
        lines[-1] += " (PTEST_STATE_DIR)"
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
            chunks = _wrap_text(terminal_text(self_plan.root), budget)
            lines.append(f"    {chunks[0]}")
            for chunk in chunks[1:]:
                lines.append(f"      {chunk}")
            lines[-1] += " (install root)"
            if self_plan.path_link:
                chunks = _wrap_text(terminal_text(self_plan.path_link),
                                    budget)
                lines.append(f"    {chunks[0]}")
                for chunk in chunks[1:]:
                    lines.append(f"      {chunk}")
                lines[-1] += " (PATH symlink)"
            for name in self_kept:
                lines.append(f"    {terminal_text(name)} (kept user file)")
        elif self_plan.refused:
            lines.append(f"    {_truncate(terminal_text(self_plan.refused_path or ''), budget)}"
                         f" ({terminal_text(self_plan.refused)})")
        else:
            lines.append("    no install root found (nothing to do)")
    removes = [entry for entry in plan.entries if entry.action == REMOVE]
    self_removes = (self_plan is not None and self_plan.requested
                    and self_plan.root is not None)
    if not removes and not self_removes:
        tail: str | None = "nothing to remove"
    elif applied is not None:
        tail = (f"removed {len(applied.removed)}, kept {len(applied.kept)}, "
                f"skipped {len(applied.skipped)}")
    else:
        tail = None
    if summary_only:
        assert applied is not None and tail is not None
        out = tail + "\n"
        for name in self_kept:
            out += f"    {terminal_text(name)} (kept user file)\n"
        return out
    if tail is not None:
        lines.append(tail)
    return "\n".join(lines) + "\n"


def _entry_payload(entry: PlanEntry) -> dict:
    return {"action": entry.action, "target": entry.target,
            "detail": entry.detail}


def document_data(plan: RepoPlan, *, applied: Applied | None,
                  dry_run: bool, self_plan: SelfPlan | None,
                  self_removed: bool = False,
                  self_link_removed: bool = False,
                  self_kept: tuple = (),
                  domain: C.DomainPaths | None = None) -> dict:
    """Allowlisted uninstall payload for the public JSON document."""
    removes = [entry.target for entry in plan.entries if entry.action == REMOVE]
    self_work = (self_plan is not None and self_plan.requested
                 and self_plan.root is not None)
    if applied is None:
        result = {"removed": [], "kept": [], "skipped": [],
                  "nothing_to_remove": not removes and not self_work,
                  "applied": False}
    else:
        result = {"removed": list(applied.removed), "kept": list(applied.kept),
                  "skipped": list(applied.skipped),
                  "nothing_to_remove": not applied.removed and not self_removed,
                  "applied": True}
    requested = bool(self_plan is not None and self_plan.requested)
    if self_plan is None or not self_plan.requested:
        self_payload: dict = {"requested": requested, "root": None,
                              "removed": False, "path_symlink_removed": False,
                              "kept": []}
    else:
        self_payload = {
            "requested": True,
            "root": None if self_plan.root is None else str(self_plan.root),
            "removed": self_removed,
            "path_symlink_removed": self_link_removed,
            "kept": list(self_kept),
        }
    state_root, state_from_env = _state_facts(domain)
    return {"root": str(plan.root), "dry_run": dry_run,
            "plan": [_entry_payload(entry) for entry in plan.entries],
            "result": result, "self": self_payload,
            "domain_root": state_root, "domain_from_env": state_from_env}
