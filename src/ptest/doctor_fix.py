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
from dataclasses import dataclass, field
from pathlib import Path

from . import contracts as C
from . import executability as executability_api
from . import files
from . import config as config_api

_PHASE = "doctor-fix"
_CONFIG_LIMIT = 256 * 1024
_CONFIG_NAME = ".ptest.toml"

#: Files probed (in order) for the selection ``full_triggers`` draft.
#: Every ``conftest.py`` under the child and the test helper modules found
#: by the static import scan join these; the bare ``conftest.py`` entry is
#: covered by that walk.
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


def _parent_chain(path: str) -> list[str]:
    parents: list[str] = []
    parent = path.rpartition("/")[0]
    while parent:
        parents.append(parent)
        parent = parent.rpartition("/")[0]
    return parents


def _under_any(rel: str, roots: tuple) -> bool:
    return any(rel == root or _is_under(rel, root) for root in roots)


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


def _existing_module_files(base: Path, parts: tuple) -> list[Path]:
    """Existing module file, package init, and parent inits for ``parts``."""
    found: list[Path] = []
    if (not parts or len(parts) > 32
            or any(not part or part in (".", "..") or "/" in part
                   or "\\" in part for part in parts)):
        return found
    rel = Path(*parts)
    candidate = base / rel.parent / (rel.name + ".py")
    if _real_py(candidate):
        found.append(candidate)
    package_init = base / rel / "__init__.py"
    if _real_py(package_init):
        found.append(package_init)
    for depth in range(1, len(parts)):
        init = base / Path(*parts[:depth]) / "__init__.py"
        if _real_py(init):
            found.append(init)
    return found


def _resolve_helper(project_dir: Path, test_roots: tuple, importer_rel: str,
                    record: tuple) -> list[str]:
    """Test-root helper files one import record pulls in, as rel paths."""
    module, level, names = record
    anchor: tuple = ()
    if level and level > 0:
        try:
            importer_parts = tuple(Path(importer_rel).parent.parts)
        except (ValueError, OSError):
            return []
        if level - 1 > len(importer_parts):
            return []
        anchor = importer_parts[:len(importer_parts) - (level - 1)]
        bases = [project_dir]
    else:
        bases = [project_dir] + [project_dir / root for root in test_roots
                                 if root != "."]
    module_parts = tuple(module) if module else ()
    if level and level > 0 and module_parts:
        full = anchor + module_parts
    elif level and level > 0:
        full = anchor
    else:
        full = module_parts
    probed = [full] if full else []
    for name in names:
        if name in ("*", ""):
            continue
        probed.append(full + (name,))
    found: list[str] = []
    for parts in probed:
        for base in bases:
            for path in _existing_module_files(base, parts):
                try:
                    rel = path.relative_to(project_dir).as_posix()
                except ValueError:
                    continue
                name = path.name
                if (name == "conftest.py" or _is_test_file_name(name)
                        or not _under_any(rel, test_roots)):
                    continue
                found.append(rel)
    return found


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
    """Scan test roots: ``(hits, helpers, tests, helper_files, complete)``.

    ``hits`` maps each test file (any depth under the test roots) to the
    src packages it imports; unreadable files map to every package (fail
    safe). ``helpers`` holds non-test, non-conftest modules under the test
    roots that test or conftest files import. ``tests`` and
    ``helper_files`` are the full universes behind directory compression.
    """
    wanted = set(package_names)
    hits: dict[str, set] = {}
    helpers: set[str] = set()
    universe_tests: set[str] = set()
    universe_helpers: set[str] = set()
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
            name = item.rsplit("/", 1)[-1]
            if name == "conftest.py":
                source = _read_scan_text(base / item)
                if source is not None:
                    _, records, ok = _parse_imports(source)
                    if ok:
                        for record in records:
                            helpers.update(_resolve_helper(
                                project_dir, test_roots, rel, record))
                continue
            if _is_test_file_name(name):
                universe_tests.add(rel)
                source = _read_scan_text(base / item)
                if source is None:
                    hits[rel] = set(wanted)
                    continue
                tops, records, ok = _parse_imports(source)
                hits[rel] = set(wanted) if not ok else set(tops & wanted)
                if ok:
                    for record in records:
                        helpers.update(_resolve_helper(
                            project_dir, test_roots, rel, record))
            else:
                universe_helpers.add(rel)
    return hits, helpers, universe_tests, universe_helpers, complete


def _compress_to_dirs(files: list, universe: set,
                       blocked: set = frozenset()) -> list:
    """Replace fully-covered directory contents with the directory itself.

    A directory compresses only when every universe file beneath it is in
    ``files``; correctness over brevity, so partial coverage stays listed.
    Directories holding a ``blocked`` path never compress: a trigger
    directory covering a test file would shadow that file's group and
    force full on its change.
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
    tainted = {parent for path in blocked
               for parent in _parent_chain(path)}
    chosen = [directory for directory, paths in members.items()
              if paths and paths <= original and directory not in tainted]
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
    hits, _, universe_tests, _, scan_complete = test_scan
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


def _generator_default_off(selection: object) -> bool:
    """True for an untouched 0.2.x generator `[selection]` table.

    The generator wrote all ten keys with `enabled = false`; such a table
    cannot be told apart from a user who deliberately typed `enabled =
    false` into an otherwise untouched generated table, so `ptest doctor`
    reports "config is out of date" and `doctor --fix` flips it for such
    a table. Changing or deleting any other `[selection]` line makes the
    choice durable: anything else counts as an explicit user choice and
    doctor proposes nothing for it.
    """
    if not isinstance(selection, dict):
        return False
    if set(selection) != {"enabled", "closed_inputs", "input_roots",
                           "ignored_inputs", "environment", "full_triggers",
                           "always", "no_tests", "non_input_outputs",
                           "full_ratio"}:
        return False
    if selection.get("enabled") is not False:
        return False
    if selection.get("closed_inputs") is not False:
        return False
    for key in ("input_roots", "ignored_inputs", "environment",
                "full_triggers", "always", "no_tests"):
        if selection.get(key) != []:
            return False
    if selection.get("non_input_outputs") not in ([], [".venv"]):
        return False
    return selection.get("full_ratio") == 0.7


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


def _selection_draft(project_dir: Path, test_roots: tuple) -> tuple[dict, dict, dict]:
    """Draft ``[selection]`` values from the source layout.

    Source roots are the real top-level ``src/`` packages, never the
    bare ``src/`` itself: generated trees beneath it (for example a
    hand-tuned ``*.egg-info`` exclusion) must not overlap the drafted
    inputs, or the policy would fail ptest's own validation. Groups come
    from the static import scan; full triggers add every ``conftest.py``
    and the test helper modules to the lockfile and pytest config files.
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
    _, helpers, universe_tests, helper_universe, _ = test_scan
    triggers = [name for name in _TRIGGER_CANDIDATES
                if _is_real_file(project_dir, name)]
    triggers.extend(_selection_conftest_draft(project_dir))
    triggers.extend(_compress_to_dirs(sorted(helpers), helper_universe,
                                      blocked=universe_tests))
    seen = set(triggers)
    triggers = sorted(seen)
    triggers = [entry for entry in triggers
                if not any(other != entry and _is_under(entry, other)
                           for other in seen)]
    groups = _selection_group_draft(project_dir, tuple(test_roots),
                                    test_scan=test_scan)
    return ({"input_roots": sorted(roots)}, {"full_triggers": triggers},
            {"groups": groups})


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
            fixed_argv = _setup_extras_fix(
                project_dir, kind, current_argv, tuple(current_args))
            if fixed_argv is not None:
                changes.append(FieldChange("setup", "argv", fixed_argv))

    selection = parsed.get("selection", {})
    if kind is C.RunnerKind.VITEST:
        # Vite writes its cache under node_modules (e.g. .vite-temp), so
        # an existing vitest config without that exemption can never
        # record a baseline.  Append only the missing exemption, keeping
        # any hand-tuned entries; dependency changes stay governed by the
        # lockfile/setup fingerprint.
        have_outputs: tuple = ()
        if isinstance(selection, dict) and isinstance(
                selection.get("non_input_outputs"), list):
            have_outputs = tuple(
                item for item in selection["non_input_outputs"]
                if isinstance(item, str))
        if "node_modules" not in have_outputs:
            changes.append(FieldChange(
                "selection", "non_input_outputs",
                have_outputs + ("node_modules",)))
    if (kind is C.RunnerKind.PYTEST
            and _has_cov(current_args)
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
    if (kind is C.RunnerKind.PYTEST
            and not any(change.table == "selection"
                        and change.key == "enabled"
                        for change in changes)
            and _generator_default_off(selection)):
        # Untouched 0.2.x generator table without --cov: flip enablement
        # only, with no draft. Hand-tuned tables are the user's choice.
        changes.append(FieldChange("selection", "enabled", True))

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
