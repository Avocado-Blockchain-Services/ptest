"""Plan and apply ``ptest doctor --fix`` configuration updates.

Planning is read-only and reuses init's own logic: the parallel tier comes
from ``executability.parallel_request`` (the predicate init consults) and
the setup baseline from ``config._fresh_config``. Applied changes are
limited to what the deterministic doctor items know exactly: a stale
``-n 0`` serial fallback, a ``[setup]`` argv missing its test-dependency
group/extra, and a ``[selection]`` draft when coverage is configured.
Model-review findings about test code are never applied.

File edits are line surgery on the existing bytes: only managed
``key = value`` lines are replaced or appended, so every unmanaged
setting stays byte-identical. Writes are atomic (temp plus rename, mode
kept), never follow symlinks, and fail closed when the file changed
between planning and writing.
"""
from __future__ import annotations

import difflib
import errno
import json
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from . import executability as executability_api
from . import files
from . import config as config_api

_PHASE = "doctor-fix"
_CONFIG_LIMIT = 256 * 1024
_CONFIG_NAME = ".ptest.toml"

_PYTEST_DEP_RE = re.compile(r"^pytest(?![A-Za-z0-9_-])")

#: Files probed (in order) for the selection ``full_triggers`` draft.
_TRIGGER_CANDIDATES = (
    "uv.lock", "conftest.py", "pyproject.toml", "pytest.ini", "tox.ini",
    "setup.cfg", ".ptest.toml",
)

_SELECTION_DRAFT_NOTE = (
    "# DRAFT: the [selection] proposal below is a starting point. "
    "Review input_roots and full_triggers before applying."
)


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase=_PHASE)


def _cfg(declaration: str) -> str:
    return ".ptest.toml" if declaration == "." else f"{declaration}/.ptest.toml"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(json.dumps(str(item), ensure_ascii=False)
                               for item in value) + "]"
    raise TypeError("unsupported fix value")


@dataclass
class FieldChange:
    """One managed ``key = value`` update inside a config file."""

    table: str
    key: str
    value: object
    draft: bool = False

    def rendered(self) -> str:
        return f"{self.key} = {_toml_value(self.value)}"


@dataclass
class FileFix:
    """Planned update for one ``.ptest.toml``."""

    rel: str
    previous: bytes
    updated: bytes
    changes: tuple = ()
    drafts: bool = False
    identity: tuple | None = None

    @property
    def change_count(self) -> int:
        return len(self.changes)


@dataclass
class Refusal:
    """One project the planner could not safely fix."""

    rel: str
    code: str
    message: str


@dataclass
class FixPlan:
    """Planned fixes across the root dispatcher and each child."""

    files: tuple = ()
    refusals: tuple = ()

    @property
    def change_count(self) -> int:
        return sum(item.change_count for item in self.files)


def _read_text(root: Path, rel: str) -> tuple[bytes, tuple]:
    """Read a config file without following symlinks, with its identity."""
    try:
        stamp = os.lstat(root / rel)
    except FileNotFoundError:
        raise _problem("state-unavailable",
                       f"config file {rel} does not exist") from None
    except OSError:
        raise _problem("state-unavailable",
                       f"config file {rel} is unavailable") from None
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path",
                       f"config file {rel} is a symlink") from None
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path",
                       f"config file {rel} is not a regular file") from None
    try:
        raw = files.read_regular(root, rel, _CONFIG_LIMIT + 1)
    except C.Problem as problem:
        raise _problem(problem.code, problem.message) from None
    if len(raw) > _CONFIG_LIMIT:
        raise _problem("invalid-bound",
                       f"config file {rel} exceeds its bound") from None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _problem("invalid-config",
                       f"config file {rel} is not UTF-8 text") from None
    return raw, (stamp.st_dev, stamp.st_ino)


def _parse_table(raw: bytes, rel: str) -> dict:
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, tomllib.TOMLDecodeError):
        raise _problem("invalid-config",
                       f"config file {rel} does not parse") from None
    if not isinstance(parsed, dict):
        raise _problem("invalid-config",
                       f"config file {rel} does not parse") from None
    return parsed


def _drop_stale_serial(args: tuple) -> tuple | None:
    """Drop ``-n 0``-family tokens, or None when none are present."""
    kept: list[str] = []
    index = 0
    changed = False
    items = list(args)
    while index < len(items):
        token = items[index]
        nxt = items[index + 1] if index + 1 < len(items) else None
        if token in ("-n0", "--numprocesses=0"):
            changed = True
            index += 1
            continue
        if token in ("-n", "--numprocesses") and nxt == "0":
            changed = True
            index += 2
            continue
        kept.append(token)
        index += 1
    if not changed:
        return None
    return tuple(kept)


def _stale_args_fix(config: C.Config, current: tuple,
                    declaration: str) -> tuple | None:
    """Fresh args for a stale serial fallback, else None.

    The parallel tier predicate is init's own: only when every other
    tier input qualifies and the single serializer is this file's
    ``-n 0`` is the fallback stale.
    """
    if not executability_api._has_serial_spelling(current):
        return None
    try:
        request = executability_api.parallel_request(
            config, project=declaration)
    except Exception:
        return None
    if request.reason != f"{_cfg(declaration)} sets -n 0":
        return None
    return _drop_stale_serial(current)


def _project_dir(root: Path, config: C.Config, declaration: str) -> Path:
    if config.config_path is not None:
        return Path(config.config_path).parent
    if declaration == ".":
        return Path(root)
    return Path(root) / declaration


def _pyproject_groups(project_dir: Path) -> tuple[dict, dict] | None:
    """Return ``(extras, groups)`` from pyproject, or None when unusable."""
    try:
        raw = files.read_regular(project_dir, "pyproject.toml",
                                 _CONFIG_LIMIT + 1)
    except C.Problem:
        return None
    if len(raw) > _CONFIG_LIMIT:
        return None
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    project = parsed.get("project", {})
    extras = project.get("optional-dependencies", {}) \
        if isinstance(project, dict) else {}
    groups = parsed.get("dependency-groups", {})
    if not isinstance(extras, dict):
        extras = {}
    if not isinstance(groups, dict):
        groups = {}
    return extras, groups


def _dep_names(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("dependencies", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _setup_extras_fix(project_dir: Path, kind: C.RunnerKind,
                      current: tuple) -> tuple | None:
    """Append the test-dependency ``--group``/``--extra``, else None.

    Applies only to an unmodified-shape ``uv sync`` argv that names no
    group/extra, when exactly one optional extra or dependency group
    carries the pytest dependency the main dependencies omit.
    """
    if kind is not C.RunnerKind.PYTEST:
        return None
    if len(current) < 2 or current[0] != "uv" or current[1] != "sync":
        return None
    if any(token in ("--extra", "--group", "--all-extras", "--all-groups",
                     "--no-default-groups")
           or token.startswith(("--extra=", "--group="))
           for token in current):
        return None
    try:
        locked = os.lstat(project_dir / "uv.lock")
    except OSError:
        return None
    if stat.S_ISLNK(locked.st_mode) or not stat.S_ISREG(locked.st_mode):
        return None
    found = _pyproject_groups(project_dir)
    if found is None:
        return None
    extras, groups = found
    try:
        raw = files.read_regular(project_dir, "pyproject.toml",
                                 _CONFIG_LIMIT + 1)
        main_names = _dep_names(
            tomllib.loads(raw.decode("utf-8")).get("project", {}))
    except (C.Problem, ValueError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if any(_PYTEST_DEP_RE.match(name.strip()) for name in main_names):
        return None
    candidates = []
    for name, deps in extras.items():
        if not isinstance(name, str):
            continue
        if any(_PYTEST_DEP_RE.match(item.strip())
               for item in _dep_names(deps)):
            candidates.append(("--extra", name))
    for name, deps in groups.items():
        if not isinstance(name, str):
            continue
        if any(_PYTEST_DEP_RE.match(item.strip())
               for item in _dep_names(deps)):
            candidates.append(("--group", name))
    if len(candidates) != 1:
        return None
    flag, name = candidates[0]
    return tuple(current) + (flag, name)


def _has_cov(args: tuple) -> bool:
    return any(token == "--cov" or token.startswith("--cov=")
               or token == "--cov-report"
               or token.startswith("--cov-report=")
               for token in args)


def _is_real_file(project_dir: Path, name: str) -> bool:
    try:
        stamp = os.lstat(project_dir / name)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode)


def _selection_draft(project_dir: Path, test_roots: tuple) -> tuple[dict, dict]:
    """Draft ``[selection]`` values from the source layout."""
    roots = set(test_roots)
    try:
        if (project_dir / "src").is_dir() and not os.path.islink(
                project_dir / "src"):
            roots.add("src")
    except OSError:
        pass
    triggers = [name for name in _TRIGGER_CANDIDATES
                if _is_real_file(project_dir, name)]
    return {"input_roots": sorted(roots)}, {"full_triggers": triggers}


def plan_project(root: Path, declaration: str,
                 config: C.Config) -> FileFix | None:
    """Compute the fix for one project, or None when it is up to date."""
    rel = _cfg(declaration)
    raw, identity = _read_text(root, rel)
    parsed = _parse_table(raw, rel)
    kind = config.runner.kind
    if kind is C.RunnerKind.COMMAND:
        return None
    project_dir = _project_dir(root, config, declaration)
    changes: list[FieldChange] = []

    runner = parsed.get("runner", {})
    current_args: tuple = ()
    if isinstance(runner, dict) and isinstance(runner.get("args"), list):
        current_args = tuple(
            item for item in runner["args"] if isinstance(item, str))
        fresh_args = _stale_args_fix(config, current_args, declaration)
        if fresh_args is not None:
            changes.append(FieldChange("runner", "args", fresh_args))
            current_args = fresh_args

    setup = parsed.get("setup", {})
    if isinstance(setup, dict) and isinstance(setup.get("argv"), list):
        current_argv = tuple(
            item for item in setup["argv"] if isinstance(item, str))
        try:
            fresh = config_api._fresh_config(
                project_dir, Path(config.config_path)
                if config.config_path is not None
                else project_dir / _CONFIG_NAME, kind)
        except C.Problem:
            fresh = None
        if fresh is not None and fresh.setup is not None:
            fixed_argv = _setup_extras_fix(project_dir, kind, current_argv)
            if fixed_argv is not None:
                changes.append(FieldChange("setup", "argv", fixed_argv))

    selection = parsed.get("selection", {})
    if (kind is C.RunnerKind.PYTEST
            and (not isinstance(selection, dict)
                 or selection.get("enabled") is not True)
            and _has_cov(current_args)):
        # The enablement itself flips false (or missing) to true; every
        # other key is only filled when absent, so hand-tuned values stay
        # untouched and appear as kept context in the diff.
        if not isinstance(selection, dict) or "enabled" not in selection:
            changes.append(FieldChange(
                "selection", "enabled", True, draft=True))
        elif selection.get("enabled") is not True:
            changes.append(FieldChange("selection", "enabled", True))
        roots, triggers = _selection_draft(
            project_dir, tuple(config.runner.test_roots))
        draft = (("closed_inputs", True),
                 ("input_roots", tuple(roots["input_roots"])),
                 ("full_triggers", tuple(triggers["full_triggers"])))
        have = selection if isinstance(selection, dict) else {}
        for key, value in draft:
            if key not in have:
                changes.append(FieldChange(
                    "selection", key, value, draft=True))

    if not changes:
        return None
    updated = _apply_changes(raw, changes)
    try:
        tomllib.loads(updated.decode("utf-8"))
    except (ValueError, tomllib.TOMLDecodeError):
        raise _problem("invalid-config",
                       f"config file {rel} cannot be patched safely") from None
    return FileFix(rel=rel, previous=raw, updated=updated,
                   changes=tuple(changes),
                   drafts=any(change.draft for change in changes),
                   identity=identity)


_TABLE_RE = re.compile(r"^\[([^[\]]+)\]\s*(?:#.*)?$")
_KEY_RE_TEMPLATE = r"^\s*[\"']?%s[\"']?\s*="


def _table_name(header: str) -> str:
    """Normalise a matched table header: ``["selection"]`` is `selection`."""
    name = header.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
        return name[1:-1]
    return name


def _apply_changes(raw: bytes, changes: list[FieldChange]) -> bytes:
    """Splice managed ``key = value`` lines into the existing text."""
    lines = raw.decode("utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    by_table: dict[str, list[FieldChange]] = {}
    for change in changes:
        by_table.setdefault(change.table, []).append(change)

    # Locate table regions: header index -> end index (next header or EOF).
    # Quoted headers (``["selection"]``) name the same table as bare ones.
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _TABLE_RE.match(line.strip())
        if match:
            headers.append((index, _table_name(match.group(1))))

    pending = {table: list(items) for table, items in by_table.items()}
    edits: dict[int, str] = {}
    inserts: dict[int, list[str]] = {}

    for table, items in pending.items():
        start = next((index for index, name in headers if name == table),
                     None)
        if start is None:
            continue
        end = next((index for index, _ in headers if index > start),
                   len(lines))
        remaining = list(items)
        for index in range(start + 1, end):
            for change in list(remaining):
                if re.match(_KEY_RE_TEMPLATE % re.escape(change.key),
                            lines[index]):
                    edits[index] = change.rendered() + "\n"
                    remaining.remove(change)
                    break
        if remaining:
            inserts.setdefault(end, []).extend(
                change.rendered() + "\n" for change in remaining)

    # Missing tables are appended at EOF in first-change order.
    missing = [table for table in by_table
               if not any(name == table for _, name in headers)]
    if missing:
        tail: list[str] = []
        if lines and lines[-1].strip():
            tail.append("\n")
        for table in missing:
            tail.append(f"[{table}]\n")
            tail.extend(change.rendered() + "\n"
                        for change in by_table[table])
        lines.extend(tail)

    # Apply edits and inserts (inserts before the end boundary line).
    result: list[str] = []
    for index, line in enumerate(lines):
        if index in inserts:
            result.extend(inserts[index])
        result.append(edits.get(index, line))
    if len(lines) in inserts:
        result.extend(inserts[len(lines)])
    return "".join(result).encode("utf-8")


def plan_all(root: Path, resolution: C.ConfigResolution) -> FixPlan:
    """Plan fixes for the dispatcher children or the standalone project."""
    if resolution.config is None and resolution.monorepo is None:
        raise resolution.problem or _problem(
            "initialization-required", "project configuration is required")
    targets: list[tuple[str, C.Config | None]] = []
    if resolution.monorepo is not None:
        for child in resolution.monorepo.children:
            targets.append((child, None))
    elif isinstance(resolution.config, C.Config):
        targets.append((".", resolution.config))
    files_out: list[FileFix] = []
    refusals: list[Refusal] = []
    for declaration, config in targets:
        try:
            if config is None:
                from .monorepo import diagnose_child
                diagnosis = diagnose_child(resolution.root, declaration)
                if diagnosis.kind != "ok" or not isinstance(
                        diagnosis.config, C.Config):
                    raise _problem(
                        "invalid-config",
                        f"child {declaration} does not resolve")
                config = diagnosis.config
            planned = plan_project(root, declaration, config)
        except C.Problem as problem:
            refusals.append(Refusal(rel=_cfg(declaration), code=problem.code,
                                    message=problem.message))
            continue
        if planned is not None:
            files_out.append(planned)
    return FixPlan(files=tuple(files_out), refusals=tuple(refusals))


def render_diff(plan: FixPlan) -> str:
    """Unified diffs plus draft notes; display only, never written."""
    parts: list[str] = []
    for item in plan.files:
        old = item.previous.decode("utf-8").splitlines()
        new = item.updated.decode("utf-8").splitlines()
        diff = difflib.unified_diff(old, new, fromfile=item.rel,
                                    tofile=item.rel, lineterm="")
        parts.append("\n".join(diff))
        if item.drafts:
            parts.append(_SELECTION_DRAFT_NOTE)
    return "\n".join(parts) + "\n" if parts else ""


def _atomic_write_text(root: Path, rel: str, data: bytes, *,
                       expect: bytes, identity: tuple | None) -> None:
    """Replace one config file atomically, keeping its mode.

    Fails closed when the file is a symlink, changed identity or bytes
    since planning, or cannot be staged safely.
    """
    parent = root / Path(rel).parent
    leaf = Path(rel).name
    try:
        stamp = os.lstat(root / rel)
    except OSError:
        raise _problem("state-unavailable",
                       f"config file {rel} is unavailable") from None
    if stat.S_ISLNK(stamp.st_mode):
        raise _problem("unsafe-path",
                       f"config file {rel} is a symlink") from None
    if not stat.S_ISREG(stamp.st_mode):
        raise _problem("unsafe-path",
                       f"config file {rel} is not a regular file") from None
    if identity is not None and (stamp.st_dev, stamp.st_ino) != identity:
        raise _problem("state-changed",
                       f"config file {rel} changed during planning") from None
    try:
        current = files.read_regular(root, rel, _CONFIG_LIMIT + 1)
    except C.Problem as problem:
        raise _problem(problem.code, problem.message) from None
    if current != expect:
        raise _problem("state-changed",
                       f"config file {rel} changed during planning") from None
    mode = stat.S_IMODE(stamp.st_mode)
    try:
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise _problem("unsafe-path",
                       f"config directory for {rel} is unsafe") from None
    tmp = f"{leaf}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    tmp_fd = None
    try:
        try:
            tmp_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
            # The mode argument passes through the umask; restore the
            # exact kept mode before the rename.
            os.fchmod(tmp_fd, mode)
        except OSError:
            raise _problem("state-unavailable",
                           f"cannot stage config file {rel}") from None
        try:
            view = memoryview(data)
            while view:
                written = os.write(tmp_fd, view)
                view = view[written:]
            os.fsync(tmp_fd)
        except OSError:
            raise _problem("state-unavailable",
                           f"cannot stage config file {rel}") from None
        os.close(tmp_fd)
        tmp_fd = None
        try:
            os.rename(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            raise _problem("state-unavailable",
                           f"cannot replace config file {rel}") from None
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        os.close(dir_fd)


def apply_plan(root: Path, plan: FixPlan) -> tuple[str, ...]:
    """Write every planned file; fail closed on the first stale file."""
    updated: list[str] = []
    for item in plan.files:
        _atomic_write_text(root, item.rel, item.updated,
                           expect=item.previous, identity=item.identity)
        updated.append(item.rel)
    return tuple(updated)


def selection_enabled_by(plan: FixPlan) -> bool:
    """True when the plan enables a disabled ``[selection]`` table."""
    return any(change.table == "selection" and change.key == "enabled"
               and change.value is True
               for item in plan.files for change in item.changes)
