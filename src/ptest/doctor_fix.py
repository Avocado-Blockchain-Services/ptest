"""Plan and apply ``ptest doctor --fix`` configuration updates.

Planning is read-only and reuses init's own logic: the parallel tier comes
from ``executability.parallel_request`` (the predicate init consults) and
the setup baseline from ``config._fresh_config``. Applied changes are
limited to what the deterministic doctor items know exactly: a stale
``-n 0`` serial fallback, a ``[setup]`` argv missing its test-dependency
group/extra, a ``[selection]`` draft when coverage is configured, and the
missing ``node_modules`` exemption on a vitest ``[selection]`` policy.
Model-review findings about test code are never applied.

File edits are line surgery on the existing bytes: only managed
``key = value`` lines are replaced or appended, so every unmanaged
setting stays byte-identical. Writes are atomic (temp plus rename, mode
kept), never follow symlinks, and fail closed when the file changed
between planning and writing.
"""
from __future__ import annotations

import ast
import difflib
import errno
import json
import os
import re
import secrets
import stat
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import contracts as C
from . import executability as executability_api
from . import files
from . import config as config_api
from .adapters import pytest as pytest_adapter

_PHASE = "doctor-fix"
_CONFIG_LIMIT = 256 * 1024
_CONFIG_NAME = ".ptest.toml"

#: Files probed (in order) for the selection ``full_triggers`` draft.
#: Every ``conftest.py`` under the child joins these. Test helper modules
#: are not drafted: the import graph plans a helper edit, so a helper
#: only forces the full suite when a user lists it by hand.
_TRIGGER_CANDIDATES = (
    "uv.lock", "poetry.lock", "Pipfile.lock", "pyproject.toml", "pytest.ini",
    "tox.ini", "setup.cfg", ".ptest.toml",
)

#: Bounds for the static import scan behind the ``[selection]`` draft. The
#: scan parses project code but never executes it.
_IMPORT_SCAN_MAX_FILES = 8192
_IMPORT_SCAN_MAX_DIRS = 16384
_IMPORT_SCAN_MAX_BYTES = 256 * 1024

#: Directories the import scan never descends into.
_IMPORT_SCAN_SKIP_DIRS = frozenset({
    ".git", ".hg", ".venv", "venv", "__pycache__", "node_modules", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

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
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        inner = ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
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
    notes: tuple = ()

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


def _pyproject_test_deps(project_dir: Path) -> tuple | None:
    """Return ``(main, extras, groups, default_groups)`` or None when unusable."""
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
    if not isinstance(project, dict):
        project = {}
    main = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})
    groups = parsed.get("dependency-groups", {})
    if not isinstance(main, list):
        main = []
    if not isinstance(extras, dict):
        extras = {}
    if not isinstance(groups, dict):
        groups = {}
    tool = parsed.get("tool", {})
    uv = tool.get("uv", {}) if isinstance(tool, dict) else {}
    default_groups = uv.get("default-groups", ["dev"]) \
        if isinstance(uv, dict) else ["dev"]
    if (not isinstance(default_groups, list)
            or not all(isinstance(name, str) for name in default_groups)):
        default_groups = ["dev"]
    return main, extras, groups, tuple(default_groups)


def _dep_names(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("dependencies", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalised_dep_name(item: str) -> str | None:
    """PEP 503-normalised leading distribution name, or None when absent."""
    match = re.match(r"[A-Za-z0-9_.-]+", item.strip())
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def _provides(deps: object, target: str) -> bool:
    return any(_normalised_dep_name(item) == target
               for item in _dep_names(deps))


def _runner_uses_n(args: tuple) -> bool:
    """True when runner args request xdist workers (a non-zero ``-n``)."""
    items = tuple(args)
    index = 0
    while index < len(items):
        token = items[index]
        if token in ("-n", "--numprocesses"):
            nxt = items[index + 1] if index + 1 < len(items) else None
            if nxt != "0":
                return True
            index += 2
            continue
        if token.startswith("-n") and len(token) > 2:
            rest = token[2:]
            if rest == "0":
                index += 1
                continue
            if rest == "auto" or rest.isdigit():
                return True
            index += 1
            continue
        if token.startswith("--numprocesses="):
            if token.split("=", 1)[1] != "0":
                return True
            index += 1
            continue
        index += 1
    return False


def _setup_extras_fix(project_dir: Path, kind: C.RunnerKind,
                      current: tuple, runner_args: tuple = ()) -> tuple | None:
    """Append the test-plugin ``--group``/``--extra`` flags, else None.

    Plain ``uv sync`` already installs main dependencies and the default
    groups, so only plugins living in an existing extra or a non-default
    group need a flag: pytest itself (always), pytest-cov/coverage (when
    ``--cov`` is in the runner args), pytest-xdist (when ``-n`` is used in
    the runner args or pytest addopts). Applies only to an
    unmodified-shape ``uv sync`` argv that names no group/extra. A
    group/extra that does not exist in this pyproject is never named,
    ``--group`` is never emitted for a default group, and ``--all-extras``
    is never used.
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
    found = _pyproject_test_deps(project_dir)
    if found is None:
        return None
    main, extras, groups, default_groups = found
    needs_cov = _has_cov(tuple(runner_args))
    needs_xdist = (_runner_uses_n(tuple(runner_args))
                   or executability_api.pytest_xdist_active(project_dir))
    wanted = (("pytest", True), ("pytest-cov", needs_cov),
              ("coverage", needs_cov), ("pytest-xdist", needs_xdist))
    default_deps = [main] + [groups[name] for name in default_groups
                             if isinstance(groups.get(name), list)]
    flags: list[str] = []
    seen: set[tuple[str, str]] = set()

    def _take(flag: str, name: str) -> None:
        if (flag, name) not in seen:
            seen.add((flag, name))
            flags.extend((flag, name))

    for target, needed in wanted:
        if not needed:
            continue
        if any(_provides(deps, target) for deps in default_deps):
            continue
        for name, deps in extras.items():
            if isinstance(name, str) and _provides(deps, target):
                _take("--extra", name)
                break
        else:
            for name, deps in groups.items():
                if (isinstance(name, str) and name not in default_groups
                        and _provides(deps, target)):
                    _take("--group", name)
                    break
            # A plugin that lives nowhere in this pyproject contributes no
            # flag: a missing group/extra is never referenced.
    if not flags:
        return None
    return tuple(current) + tuple(flags)


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


def _src_package_entries(project_dir: Path) -> tuple:
    """Top-level ``src/`` ``(name, rel)`` entries from the real layout.

    Skips generated trees (``*.egg-info``), hidden entries, symlinks,
    and names that could never validate as policy paths, so the draft
    can never contradict a hand-tuned exclusion beneath ``src/``.
    """
    src = project_dir / "src"
    try:
        if os.path.islink(src) or not src.is_dir():
            return ()
    except OSError:
        return ()
    try:
        entries = sorted(src.iterdir())
    except OSError:
        return ()
    found: list[tuple] = []
    for entry in entries:
        name = entry.name
        if (not name or name.startswith(".") or name.endswith(".egg-info")
                or "\\" in name or len(name) > 64):
            continue
        try:
            stamp = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISLNK(stamp.st_mode):
            continue
        if stat.S_ISDIR(stamp.st_mode):
            found.append((name, f"src/{name}"))
        elif (stat.S_ISREG(stamp.st_mode) and name.endswith(".py")
                and name[:-3].replace("_", "").isalnum()):
            found.append((name[:-3], f"src/{name}"))
    return tuple(found)


def _is_test_file_name(name: str) -> bool:
    """True for pytest's default collection basenames (never conftest)."""
    return name.endswith(".py") and (
        name.startswith("test_") or name[:-3].endswith("_test"))


def _is_under(path: str, directory: str) -> bool:
    return path != directory and path.startswith(directory + "/")


def _real_py(path: Path) -> bool:
    try:
        stamp = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode)


def _walk_py_files(base: Path) -> tuple[tuple[str, ...], bool]:
    """Bounded walk of real ``.py`` files below ``base``.

    Returns ``(relpaths, complete)``: ``complete`` is False when a bound
    or an unreadable entry cut the walk short. Symlinks, hidden entries,
    and generated/dependency trees are skipped.
    """
    found: list[str] = []
    skipped = False
    dirs_seen = 0
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            skipped = True
            continue
        for entry in entries:
            name = entry.name
            if not name or name.startswith("."):
                continue
            try:
                stamp = os.lstat(entry)
            except OSError:
                skipped = True
                continue
            if stat.S_ISLNK(stamp.st_mode):
                continue
            if stat.S_ISDIR(stamp.st_mode):
                if (name in _IMPORT_SCAN_SKIP_DIRS
                        or name.endswith(".egg-info")):
                    continue
                dirs_seen += 1
                if dirs_seen > _IMPORT_SCAN_MAX_DIRS:
                    skipped = True
                else:
                    stack.append(entry)
            elif stat.S_ISREG(stamp.st_mode) and name.endswith(".py"):
                if len(found) >= _IMPORT_SCAN_MAX_FILES:
                    skipped = True
                else:
                    found.append(entry.relative_to(base).as_posix())
    return tuple(sorted(found)), not skipped


def _read_scan_text(path: Path) -> str | None:
    """Read one scan file, or None when it is missing, big, or undecodable."""
    try:
        stamp = os.lstat(path)
    except OSError:
        return None
    if (stat.S_ISLNK(stamp.st_mode) or not stat.S_ISREG(stamp.st_mode)
            or stamp.st_size > _IMPORT_SCAN_MAX_BYTES):
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_imports(source: str) -> tuple[set[str], list, bool]:
    """Split imports into absolute top-level names plus from/import records.

    Each record is ``(module_parts_or_None, level, names)``. Returns
    ``ok=False`` when the source does not parse; callers treat such files
    as importing everything (fail safe, never silently skipped).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set(), [], False
    tops: set[str] = set()
    records: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                if parts and parts[0]:
                    tops.add(parts[0])
                    records.append((parts, 0, ()))
        elif isinstance(node, ast.ImportFrom):
            module = tuple(node.module.split(".")) if node.module else None
            names = tuple(alias.name for alias in node.names if alias.name)
            if node.level == 0 and module and module[0]:
                tops.add(module[0])
            records.append((module, node.level, names))
    return tops, records, True


def _src_dependency_graph(project_dir: Path,
                          packages: tuple) -> tuple[dict, bool]:
    """Map each src package to the sibling src packages it imports.

    Static AST scan, bounded, never executed. A file the scan cannot read
    counts as importing every sibling (fail safe). ``complete`` is False
    when a walk bound cut the scan short.
    """
    names = {name for name, _ in packages}
    graph: dict[str, set] = {}
    complete = True
    for name, rel in packages:
        imported: set[str] = set()
        base = project_dir / rel
        if _real_py(base):
            rels = (base.relative_to(project_dir).as_posix(),)
            walk_ok = True
        else:
            try:
                if os.path.islink(base) or not base.is_dir():
                    graph[name] = set()
                    continue
            except OSError:
                graph[name] = set()
                continue
            sub, walk_ok = _walk_py_files(base)
            rels = tuple(f"{rel}/{item}" for item in sub)
            complete = complete and walk_ok
        for item in rels:
            source = _read_scan_text(project_dir / item)
            if source is None:
                imported.update(names - {name})
                continue
            tops, _, ok = _parse_imports(source)
            if not ok:
                imported.update(names - {name})
                continue
            imported.update(tops & names - {name})
        graph[name] = imported
    return graph, complete


def _transitive_dependents(graph: dict) -> dict:
    """Map each package to itself plus every package importing it transitively."""
    dependents: dict[str, set] = {}
    for target in graph:
        found = {target}
        for candidate in graph:
            if candidate == target:
                continue
            seen: set[str] = set()
            stack = [candidate]
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                edges = graph.get(current, set())
                if target in edges:
                    found.add(candidate)
                    break
                stack.extend(edges - seen)
        dependents[target] = found
    return dependents


def _scan_test_roots(project_dir: Path, test_roots: tuple,
                     package_names: tuple) -> tuple:
    """Scan test roots: ``(hits, tests, complete)``.

    ``hits`` maps each test file (any depth under the test roots) to the
    src packages it imports; unreadable files map to every package (fail
    safe). ``tests`` is the universe of test files behind directory
    compression.
    """
    wanted = set(package_names)
    hits: dict[str, set] = {}
    universe_tests: set[str] = set()
    complete = True
    for root in test_roots:
        base = project_dir if root == "." else project_dir / root
        try:
            if os.path.islink(base) or not base.is_dir():
                continue
        except OSError:
            complete = False
            continue
        rels, walk_ok = _walk_py_files(base)
        complete = complete and walk_ok
        for item in rels:
            rel = item if root == "." else f"{root}/{item}"
            if not _is_test_file_name(item.rsplit("/", 1)[-1]):
                continue
            universe_tests.add(rel)
            source = _read_scan_text(base / item)
            if source is None:
                hits[rel] = set(wanted)
                continue
            tops, _, ok = _parse_imports(source)
            hits[rel] = set(wanted) if not ok else set(tops & wanted)
    return hits, universe_tests, complete


def _compress_to_dirs(files: list, universe: set) -> list:
    """Replace fully-covered directory contents with the directory itself.

    A directory compresses only when every universe file beneath it is in
    ``files``; correctness over brevity, so partial coverage stays listed.
    """
    original = set(files)
    if not original:
        return []
    members: dict[str, set] = {}
    for path in universe:
        parent = path.rpartition("/")[0]
        while parent:
            members.setdefault(parent, set()).add(path)
            parent = parent.rpartition("/")[0]
    chosen = [directory for directory, paths in members.items()
              if paths and paths <= original]
    # A chosen dir nested under another chosen dir is subsumed by it.
    final = [directory for directory in chosen
             if not any(other != directory and _is_under(directory, other)
                        for other in chosen)]
    covered = {path for directory in final for path in members[directory]}
    return sorted((original - covered) | set(final))


def _selection_group_draft(project_dir: Path, test_roots: tuple,
                           test_scan: tuple | None = None) -> tuple:
    """Draft ``groups`` mapping src packages to their importing test files.

    For each src package P the group tests are every test file (any depth
    under the test roots) importing P or any package that transitively
    imports P, as file paths compressed to fully-covered directories. A
    package no test file imports falls back to the declared test roots;
    an incomplete scan falls every group back to the roots (fail safe).
    Without any ``src/`` layout each test root maps to itself.
    """
    groups: list[dict] = []
    packages = _src_package_entries(project_dir)
    if not packages:
        for root in test_roots:
            name = root if root != "." else "root"
            groups.append({"name": name, "sources": [root],
                           "tests": [root]})
        return tuple(groups[:256])
    fallback = tuple(test_roots) or ("tests",)
    names = tuple(name for name, _ in packages)
    graph, src_complete = _src_dependency_graph(project_dir, packages)
    dependents = _transitive_dependents(graph)
    if test_scan is None:
        test_scan = _scan_test_roots(project_dir, tuple(test_roots), names)
    hits, universe_tests, scan_complete = test_scan
    complete = src_complete and scan_complete
    for package, rel in packages:
        if not complete:
            tests = list(fallback)
        else:
            want = dependents[package]
            files = sorted(path for path, imported in hits.items()
                           if imported & want)
            tests = _compress_to_dirs(files, universe_tests) or list(fallback)
        groups.append({"name": package, "sources": [rel],
                       "tests": tests})
    return tuple(groups[:256])


def _selection_key_is_default(have: dict, key: str) -> bool:
    """True when a draftable selection key is absent or still at default.

    ``closed_inputs`` defaults to false and the list keys default to
    empty; only non-default values count as hand-tuned and are kept.
    """
    if key == "closed_inputs":
        return have.get("closed_inputs") is not True
    value = have.get(key)
    return not isinstance(value, list) or len(value) == 0


def _selection_needs_close(selection: object) -> bool:
    """True when enablement or any draftable key still needs its draft."""
    if not isinstance(selection, dict):
        return True
    if selection.get("enabled") is not True:
        return True
    return any(_selection_key_is_default(selection, key)
               for key in ("closed_inputs", "input_roots",
                           "full_triggers", "groups"))


def _selection_conftest_draft(project_dir: Path) -> list[str]:
    """Every ``conftest.py`` under the child, at any depth, as rel paths."""
    try:
        if os.path.islink(project_dir) or not project_dir.is_dir():
            return []
    except OSError:
        return []
    rels, _ = _walk_py_files(project_dir)
    return sorted(item for item in rels if item.rsplit("/", 1)[-1] == "conftest.py")


def _normalised_triggers(entries: Iterable[str]) -> list[str]:
    """Sorted, unique trigger entries without those a directory entry covers."""
    seen = set(entries)
    return [entry for entry in sorted(seen)
            if not any(other != entry and _is_under(entry, other)
                       for other in seen)]


def _selection_draft(project_dir: Path, test_roots: tuple) -> tuple[dict, dict, dict]:
    """Draft ``[selection]`` values from the source layout.

    Source roots are the real top-level ``src/`` packages, never the
    bare ``src/`` itself: generated trees beneath it (for example a
    hand-tuned ``*.egg-info`` exclusion) must not overlap the drafted
    inputs, or the policy would fail ptest's own validation. Groups come
    from the static import scan; full triggers are the lockfile and
    pytest config files plus every ``conftest.py``. Test helper modules
    and package ``__init__.py`` files are not drafted: a helper edit is
    planned from the import graph, not forced to the full suite.
    """
    packages = _src_package_entries(project_dir)
    roots = set(test_roots) | {rel for _, rel in packages}
    if not packages:
        try:
            if (project_dir / "src").is_dir() and not os.path.islink(
                    project_dir / "src"):
                roots.add("src")
        except OSError:
            pass
    names = tuple(name for name, _ in packages)
    test_scan = _scan_test_roots(project_dir, tuple(test_roots), names)
    triggers = [name for name in _TRIGGER_CANDIDATES
                if _is_real_file(project_dir, name)]
    triggers.extend(_selection_conftest_draft(project_dir))
    triggers = _normalised_triggers(triggers)
    groups = _selection_group_draft(project_dir, tuple(test_roots),
                                    test_scan=test_scan)
    return ({"input_roots": sorted(roots)}, {"full_triggers": triggers},
            {"groups": groups})


def _in_test_roots(rel: str, test_roots: tuple) -> bool:
    """True when ``rel`` is strictly below a named test root.

    A ``.`` root attributes nothing: the old generator never put a helper
    under it, so an entry there is hand-written and stays.
    """
    return any(_is_under(rel, root) for root in test_roots)


def _drafted_helper_entry(project_dir: Path, test_roots: tuple,
                          entry: str) -> bool:
    """True when ``entry`` has the shape an older draft gave a helper.

    A ``.py`` path under a test root that is neither a conftest nor a test
    file (it may be gone from disk: a stale draft), or a real directory
    strictly under a test root holding a non-conftest, non-test ``.py`` and
    no test file, read by a complete walk. Anything else was never drafted.
    """
    if not _in_test_roots(entry, test_roots):
        return False
    name = entry.rsplit("/", 1)[-1]
    if entry.endswith(".py"):
        return name != "conftest.py" and not _is_test_file_name(name)
    path = project_dir / entry
    try:
        if os.path.islink(path) or not path.is_dir():
            return False
    except OSError:
        return False
    rels, complete = _walk_py_files(path)
    if not complete:
        return False
    names = [item.rsplit("/", 1)[-1] for item in rels]
    if any(_is_test_file_name(item) for item in names):
        return False
    return any(item != "conftest.py" for item in names)


def _migrate_helper_triggers(project_dir: Path, test_roots: tuple,
                             entries: list[str]) -> tuple | None:
    """``(new full_triggers, removed, deliberate kept)``, or None.

    None means no entry is attributable to the old helper draft. Conftests
    under a removed directory entry are restored, since the directory used
    to cover them; the result is normalised as a fresh draft is.
    """
    removed = tuple(entry for entry in entries
                    if _drafted_helper_entry(project_dir, test_roots, entry))
    if not removed:
        return None
    gone = set(removed)
    kept = [entry for entry in entries if entry not in gone]
    for entry in removed:
        if entry.endswith(".py"):
            continue
        rels, _ = _walk_py_files(project_dir / entry)
        kept.extend(f"{entry}/{item}" for item in rels
                    if item.rsplit("/", 1)[-1] == "conftest.py")
    result = tuple(_normalised_triggers(kept))
    deliberate = tuple(
        entry for entry in result if entry not in _TRIGGER_CANDIDATES
        and entry.rsplit("/", 1)[-1] != "conftest.py")
    return result, removed, deliberate


_TOML_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|\'[^\'\n]*\'')


def _assignment_text(raw: bytes, table: str, key: str) -> str | None:
    """Source text of ``key = value`` inside ``[table]``, or None.

    Uses the same table, key and value-end logic as ``_apply_changes``.
    """
    lines = raw.decode("utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    headers = [(index, _table_name(match.group(1)))
               for index, line in enumerate(lines)
               if (match := _TABLE_RE.match(line.strip()))]
    start = next((index for index, name in headers if name == table), None)
    if start is None:
        return None
    end = next((index for index, _ in headers if index > start), len(lines))
    for index in range(start + 1, end):
        if re.match(_KEY_RE_TEMPLATE % re.escape(key), lines[index]):
            return "".join(lines[index:_value_end(lines, index, end)])
    return None


def _plan_trigger_migration(raw: bytes, selection: dict, project_dir: Path,
                            test_roots: tuple, changes: list[FieldChange],
                            notes: list[str]) -> None:
    """Drop helper entries an older draft put in ``full_triggers``.

    The file carries no draft marker, so provenance is read from the list:
    it must be non-empty, hold ``.ptest.toml`` (only the old generator
    wrote it), be sorted and unique, and be a comment-free assignment in
    ``[selection]``. A list failing a gate is left as written; a helper
    listed out of sorted order is how a user keeps it as a trigger.
    """
    value = selection.get("full_triggers")
    if (not isinstance(value, list) or not value
            or not all(isinstance(item, str) and item for item in value)
            or _CONFIG_NAME not in value):
        return
    text = _assignment_text(raw, "selection", "full_triggers")
    if text is None:
        return
    shaped = [entry for entry in value
              if _drafted_helper_entry(project_dir, test_roots, entry)]
    if not shaped:
        return
    why = None
    if value != sorted(set(value)):
        why = "not in sorted order"
    elif "#" in _TOML_STRING_RE.sub("", text):
        why = "its value has comments"
    if why is not None:
        notes.append(f"selection.full_triggers: left as written ({why}): "
                     f"{', '.join(shaped)}; each still forces the full suite "
                     "when edited")
        return
    migrated = _migrate_helper_triggers(project_dir, test_roots, value)
    if migrated is None:
        return
    result, removed, deliberate = migrated
    changes.append(FieldChange("selection", "full_triggers", result))
    notes.append(
        "selection.full_triggers: removed test-helper entries "
        f"{len(removed)} ({', '.join(removed)}); a helper edit now runs only "
        "the tests that reach it, once a run has recorded under this config")
    notes.append(
        "selection.full_triggers: until then a helper edit still runs the "
        "full suite. A checkout whose last green point is older than this "
        "edit pays one full run on its next bare or --changed run, including "
        "in-flight worktrees after they merge it; later bare runs are "
        "selective again")
    notes.append(
        "selection.full_triggers: .ptest.toml is itself a full trigger. "
        "Worktrees that have not merged this edit are unaffected. A run with "
        "an explicit --base is full on every run while .ptest.toml differs "
        "from that base")
    keep = "to keep a helper as a deliberate trigger, list it out of sorted order"
    if deliberate:
        notes.append(f"selection.full_triggers: kept {', '.join(deliberate)}. "
                     f"{keep[0].upper()}{keep[1:]}")
    else:
        notes.append(f"selection.full_triggers: {keep}")


#: The Cache Directory Tagging signature (https://bford.info/cachedir/).
_CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
#: At most this many gitignored entries are drafted per project.
_GITIGNORED_DRAFT_LIMIT = 64
_SELECTION_PATH_KEYS = ("input_roots", "ignored_inputs", "full_triggers",
                        "always", "no_tests", "non_input_outputs")


def _tagged_cache(project_dir: Path, rel: str) -> bool:
    """A real directory holding a regular ``CACHEDIR.TAG`` that starts with
    the standard signature; a link anywhere on the way means no."""
    try:
        if not stat.S_ISDIR(os.lstat(project_dir / rel).st_mode):
            return False
        data = files.read_regular(project_dir, f"{rel}/CACHEDIR.TAG",
                                  len(_CACHEDIR_SIGNATURE))
    except (OSError, C.Problem):
        return False
    return data == _CACHEDIR_SIGNATURE


#: A directory is declared whole only when a walk of at most this many
#: entries finds nothing below it but the files being declared.
_COLLAPSE_WALK_LIMIT = 10_000


def _holds_only(project_dir: Path, entry: str, declared: set[str]) -> bool:
    """True when every entry below directory ``entry`` is one of the
    ``declared`` files. ptest's own byproducts and outputs are missing from
    its snapshot, so the snapshot alone cannot tell whether declaring the
    directory would also turn them into inputs or outputs."""
    base = project_dir / entry
    try:
        if not stat.S_ISDIR(os.lstat(base).st_mode):
            return True
    except OSError:
        return False
    seen = 0
    for directory, subdirs, names in os.walk(base):
        # os.walk lists a symlinked directory but never descends into it.
        links = [name for name in subdirs
                 if os.path.islink(os.path.join(directory, name))]
        for name in names + links:
            seen += 1
            if seen > _COLLAPSE_WALK_LIMIT:
                return False
            rel = Path(directory, name).relative_to(project_dir).as_posix()
            if rel not in declared:
                return False
    return True


def _draft_gitignored(config: C.Config, project_dir: Path,
                      changes: list[FieldChange], notes: list[str]) -> None:
    """Declare the gitignored files ptest would count as undeclared inputs.

    ptest's own snapshot names them, so built-in outputs and runner
    byproducts never appear. Tagged caches become outputs; anything else
    becomes an input, which is fingerprinted, so a wrong guess costs a
    rerun and never keeps stale proof. ``.env`` files and symlinks are only
    named: tests may read a ``.env``, yet a worktree's ``.env`` is a
    symlink, which an input entry refuses, so that choice is the user's.
    """
    from . import selection as selection_api
    from . import source

    snapshot = source.listing_snapshot(config)
    if snapshot.digest is None:
        if any(item.code == "scan-limit" for item in snapshot.limitations):
            notes.append("gitignored files not checked: "
                         f"{snapshot.limitations[0].message}")
        return
    ignored = [change.new for change in snapshot.changes
               if change.kind == "ignored" and change.new
               and not change.new.startswith("../")]
    if not ignored:
        return
    policy = config.selection
    for change in changes:
        if change.table == "selection" and change.key in _SELECTION_PATH_KEYS:
            policy = replace(policy, **{change.key: tuple(change.value)})
    test_roots = tuple(config.runner.test_roots)
    outputs, inputs = list(policy.non_input_outputs), list(policy.ignored_inputs)
    added_input = False
    env_files: list[str] = []
    declared = set(ignored)
    entries: list[str] = []
    for entry in selection_api.declarable(ignored, snapshot):
        if _holds_only(project_dir, entry, declared):
            entries.append(entry)
        else:
            entries.extend(path for path in ignored
                           if path.startswith(entry + "/"))
    entries = sorted(dict.fromkeys(entries))
    for entry in entries[:_GITIGNORED_DRAFT_LIMIT]:
        try:
            link = stat.S_ISLNK(os.lstat(project_dir / entry).st_mode)
        except OSError:
            continue
        name = entry.rsplit("/", 1)[-1]
        if name == ".env" or name.startswith(".env."):
            env_files.append(entry)
            continue
        if _tagged_cache(project_dir, entry):
            target = outputs
        elif link:
            notes.append(f"not declared: {entry} is a symlink; declare it "
                         "in [selection] yourself")
            continue
        else:
            target = inputs
        target.append(entry)
        try:
            candidate = replace(policy, non_input_outputs=tuple(outputs),
                                ignored_inputs=tuple(inputs))
            invalid = selection_api._invalid_policy(candidate, test_roots)
        except (TypeError, ValueError):
            invalid = True
        if invalid:
            target.pop()
            notes.append(f"not declared: {entry} overlaps a declared "
                         "[selection] entry")
            continue
        added_input = added_input or target is inputs
    if len(entries) > _GITIGNORED_DRAFT_LIMIT:
        notes.append(f"not declared: {len(entries) - _GITIGNORED_DRAFT_LIMIT} "
                     "more gitignored paths")
    for key, before, after in (
            ("non_input_outputs", policy.non_input_outputs, tuple(outputs)),
            ("ignored_inputs", policy.ignored_inputs, tuple(inputs))):
        if after != before:
            changes[:] = [change for change in changes
                          if (change.table, change.key) != ("selection", key)]
            changes.append(FieldChange("selection", key, after))
    if env_files:
        it = "it" if len(env_files) == 1 else "them"
        notes.append(
            f"not declared: {', '.join(env_files)} — tests may read {it}. To "
            f"let the full gate reuse a green run, add {it} to [selection] "
            f"non_input_outputs and run ptest --full --again after an edit "
            "(ignored_inputs refuses a worktree's symlinked .env)")
    if added_input:
        notes.append("ignored_inputs are gitignored files tests may read; "
                     "move one to non_input_outputs if no test reads it")


def plan_project(root: Path, declaration: str, config: C.Config, *,
                 tree_drafts: bool = True,
                 notes: list | None = None) -> FileFix | None:
    """Compute the fix for one project, or None when it is up to date.

    ``tree_drafts`` adds the work that depends on the working tree
    (gitignored files, and removing helper entries an older draft put in
    ``full_triggers``); notes about them are appended to ``notes``.
    """
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

    # Coverage on request (0.6): --cov in args/full_args measures every
    # run, iteration included. Move it to coverage_args, which a run adds
    # only with --cov. A config that already declares coverage_args is the
    # user's own split and stays as written.
    coverage_tokens: tuple = ()
    if (kind is C.RunnerKind.PYTEST and isinstance(runner, dict)
            and "coverage_args" not in runner):
        current_full = (tuple(item for item in runner["full_args"]
                              if isinstance(item, str))
                        if isinstance(runner.get("full_args"), list) else ())
        moved_args, kept_args = pytest_adapter.split_coverage_tokens(current_args)
        moved_full, kept_full = pytest_adapter.split_coverage_tokens(current_full)
        moved = moved_args + moved_full
        if moved and pytest_adapter.coverage_tokens_valid(moved):
            if moved_args:
                fresh = [change for change in changes
                         if (change.table, change.key) != ("runner", "args")]
                changes[:] = fresh
                changes.append(FieldChange("runner", "args", kept_args))
                current_args = kept_args
            if moved_full:
                changes.append(FieldChange("runner", "full_args", kept_full))
            changes.append(FieldChange("runner", "coverage_args", moved))
            coverage_tokens = moved
    elif kind is C.RunnerKind.PYTEST and isinstance(runner, dict):
        coverage_tokens = tuple(config.runner.coverage_args)

    if kind is C.RunnerKind.PYTEST and isinstance(runner, dict):
        # Older generators wrote ``tests`` whenever that directory existed,
        # so a full run silently skipped test files pytest itself collects
        # elsewhere (fullon2 integrations: 2 of 96 files). Widen only when
        # the configured roots miss real test files.
        configured = tuple(config.runner.test_roots)
        detected = config_api.pytest_test_roots(project_dir)
        missing = [item for item in detected
                   if not any(item == have or item.startswith(have + "/")
                              for have in configured)]
        if ("." not in configured and "." not in detected and missing
                and any(config_api._dir_has_test_files(project_dir / item)
                        for item in missing)):
            changes.append(FieldChange(
                "runner", "test_roots",
                tuple(dict.fromkeys(configured + tuple(missing)))))

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
            fixed_argv = _setup_extras_fix(
                project_dir, kind, current_argv,
                tuple(current_args) + coverage_tokens)
            if fixed_argv is not None:
                changes.append(FieldChange("setup", "argv", fixed_argv))

    selection = parsed.get("selection", {})
    have_outputs: tuple = ()
    if isinstance(selection, dict) and isinstance(
            selection.get("non_input_outputs"), list):
        have_outputs = tuple(
            item for item in selection["non_input_outputs"]
            if isinstance(item, str))
    if kind is C.RunnerKind.VITEST and "node_modules" not in have_outputs:
        # Vite writes its cache under node_modules (e.g. .vite-temp), so
        # an existing vitest config without that exemption can never
        # record a baseline.  Append only the missing exemption, keeping
        # any hand-tuned entries; dependency changes stay governed by the
        # lockfile/setup fingerprint.
        changes.append(FieldChange(
            "selection", "non_input_outputs",
            have_outputs + ("node_modules",)))
    if (kind is C.RunnerKind.PYTEST
            and not _has_cov(tuple(current_args) + coverage_tokens)
            and isinstance(selection, dict)
            and selection.get("enabled") is False
            and all(_selection_key_is_default(selection, key)
                    for key in ("closed_inputs", "input_roots",
                                "full_triggers", "groups", "always",
                                "no_tests", "ignored_inputs",
                                "environment"))
            and selection.get("full_ratio", 0.7) == 0.7):
        # Pre-0.3 generator default: graph selection needs no coverage
        # draft, only the switch. Hand-tuned selection keys mean the
        # user chose `false` deliberately, so it stays.
        changes.append(FieldChange("selection", "enabled", True))
    if (kind is C.RunnerKind.PYTEST
            and _has_cov(tuple(current_args) + coverage_tokens)
            and (not isinstance(selection, dict)
                 or _selection_needs_close(selection))):
        # The enablement itself flips false (or missing) to true; every
        # other key is drafted when absent OR still at its default, so
        # init-fresh defaults (false/empty) are completed while hand-tuned
        # non-default values stay untouched and appear as kept context.
        have = selection if isinstance(selection, dict) else {}
        if "enabled" not in have:
            changes.append(FieldChange(
                "selection", "enabled", True, draft=True))
        elif have.get("enabled") is not True:
            changes.append(FieldChange("selection", "enabled", True))
        roots, triggers, groups = _selection_draft(
            project_dir, tuple(config.runner.test_roots))
        draft = (("closed_inputs", True),
                 ("input_roots", tuple(roots["input_roots"])),
                 ("full_triggers", tuple(triggers["full_triggers"])),
                 ("groups", tuple(groups["groups"])))
        for key, value in draft:
            if _selection_key_is_default(have, key):
                changes.append(FieldChange(
                    "selection", key, value, draft=True))

    enabled_now = isinstance(selection, dict) and (
        selection.get("enabled") is True
        or any(change.table == "selection" and change.key == "enabled"
               and change.value is True for change in changes))
    if (tree_drafts and kind is C.RunnerKind.PYTEST
            and isinstance(selection, dict) and enabled_now
            and selection.get("dynamic") is not False):
        _plan_trigger_migration(
            raw, selection, project_dir, tuple(config.runner.test_roots),
            changes, notes if notes is not None else [])
    if tree_drafts and kind in (C.RunnerKind.PYTEST, C.RunnerKind.VITEST):
        _draft_gitignored(config, project_dir, changes,
                          notes if notes is not None else [])
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


def _value_end(lines: list[str], start: int, end: int) -> int:
    """Return the index after the last line of the value keyed at ``start``.

    The value ends at the first line that completes a parseable TOML
    assignment; an unparseable value keeps its single line, and the final
    whole-file parse then refuses the patch.
    """
    for stop in range(start + 1, end + 1):
        try:
            tomllib.loads("".join(lines[start:stop]))
        except tomllib.TOMLDecodeError:
            continue
        return stop
    return start + 1


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
    spanned: set[int] = set()

    for table, items in pending.items():
        start = next((index for index, name in headers if name == table),
                     None)
        if start is None:
            continue
        end = next((index for index, _ in headers if index > start),
                   len(lines))
        remaining = list(items)
        for index in range(start + 1, end):
            if index in spanned:
                continue
            for change in list(remaining):
                if re.match(_KEY_RE_TEMPLATE % re.escape(change.key),
                            lines[index]):
                    edits[index] = change.rendered() + "\n"
                    # A value written over several lines is replaced whole.
                    spanned.update(range(index + 1,
                                         _value_end(lines, index, end)))
                    remaining.remove(change)
                    break
        if remaining:
            # New keys join the table's own settings: after its last
            # setting line, ahead of the trailing comments and blank lines
            # that lead into the next table.
            anchor = start + 1
            for index in range(start + 1, end):
                text = lines[index].strip()
                if text and not text.startswith("#"):
                    anchor = index + 1
            inserts.setdefault(anchor, []).extend(
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
        if index not in spanned:
            result.append(edits.get(index, line))
    if len(lines) in inserts:
        result.extend(inserts[len(lines)])
    return "".join(result).encode("utf-8")


def plan_all(root: Path, resolution: C.ConfigResolution, *,
             tree_drafts: bool = True) -> FixPlan:
    """Plan fixes for the dispatcher children or the standalone project.

    The run-start upgrade notice passes ``tree_drafts=False``: it speaks
    once per ptest version, and drafts for gitignored files depend on the
    working tree, not on the version. It also leaves out the helper-trigger
    migration: it costs a full run in the checkout that applies it, so only
    an explicit ``doctor --fix`` (or doctor's mention) proposes it.
    """
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
    notes: list[str] = []
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
            project_notes: list[str] = []
            planned = plan_project(root, declaration, config,
                                   tree_drafts=tree_drafts,
                                   notes=project_notes)
            notes.extend(f"# {_cfg(declaration)}: {note}"
                         for note in project_notes)
        except C.Problem as problem:
            refusals.append(Refusal(rel=_cfg(declaration), code=problem.code,
                                    message=problem.message))
            continue
        if planned is not None:
            files_out.append(planned)
    return FixPlan(files=tuple(files_out), refusals=tuple(refusals),
                   notes=tuple(notes))


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
    parts.extend(plan.notes)
    return "\n".join(parts) + "\n" if parts else ""


#: Summary preview shows this many groups/triggers before the ellipsis.
_SUMMARY_PREVIEW_LIMIT = 4


def _summarise_groups(value: object) -> str:
    """``N (name M test files, …)`` for a drafted ``groups`` value."""
    groups = list(value) if isinstance(value, (list, tuple)) else []
    parts: list[str] = []
    for group in groups[:_SUMMARY_PREVIEW_LIMIT]:
        if isinstance(group, dict):
            tests = group.get("tests", ())
            count = len(tests) if isinstance(tests, (list, tuple)) else 0
            noun = "test file" if count == 1 else "test files"
            parts.append(f"{group.get('name', '?')} {count} {noun}")
        else:
            parts.append(str(group))
    if len(groups) > _SUMMARY_PREVIEW_LIMIT:
        parts.append("…")
    shown = ", ".join(parts)
    return f"{len(groups)} ({shown})" if shown else f"{len(groups)} ()"


def _summarise_triggers(value: object) -> str:
    """``N (first, …)`` for a drafted ``full_triggers`` value."""
    items = [str(item) for item in value] if isinstance(
        value, (list, tuple)) else []
    shown = items[:_SUMMARY_PREVIEW_LIMIT]
    if len(items) > _SUMMARY_PREVIEW_LIMIT:
        shown = [*shown, "…"]
    joined = ", ".join(shown)
    return f"{len(items)} ({joined})" if joined else f"{len(items)} ()"


def _summarise_change(change: FieldChange) -> str:
    """One short ``table.key`` line for a planned change."""
    if change.table == "selection" and change.key == "groups":
        return f"selection.groups: {_summarise_groups(change.value)}"
    if change.table == "selection" and change.key == "full_triggers":
        return f"selection.full_triggers: {_summarise_triggers(change.value)}"
    return f"{change.table}.{change.key} = {_toml_value(change.value)}"


def render_summary(plan: FixPlan) -> str:
    """Short per-key preview; display only, never written.

    One line per changed key (``<rel>: <table>.<key> = <value>``), with
    the ``[selection]`` mapping drafts compressed to a group count with
    per-group test-file counts and a trigger count with the first few
    entries. The file on disk still receives the full content; draft
    notes are kept.
    """
    lines: list[str] = []
    for item in plan.files:
        for change in item.changes:
            lines.append(f"{item.rel}: {_summarise_change(change)}")
        if item.drafts:
            lines.append(_SELECTION_DRAFT_NOTE)
    lines.extend(plan.notes)
    return "\n".join(lines) + "\n" if lines else ""


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
