"""Changed-by-default impact: base resolution, changed set, test selection.

Pure decision layer for bare ``ptest`` / ``ptest --changed``. No coverage,
history, or baseline reads anywhere in this module: the changed set comes
from git worktree evidence (via monorepo.worktree_changed_files) and test
selection from a static AST reverse-import graph.
"""
from __future__ import annotations

import ast
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from .monorepo import _git_blob, worktree_changed_files
from .selection import _matches

MAX_SCAN_FILES = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SELECTED = 200


@dataclass(frozen=True, slots=True)
class Base:
    sha: str | None
    label: str


@dataclass(frozen=True, slots=True)
class Impact:
    kind: str
    changed: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    direct: int = 0
    via: int = 0
    total: int = 0
    reason: str = ""
    ignored: int = 0


def _problem(message: str) -> C.Problem:
    return C.Problem(code="invalid-config", message=message,
                     phase="config", retryable=False)


def git_top(start: Path) -> Path | None:
    raw = _git_blob(Path(start), "rev-parse", "--show-toplevel")
    if raw is None:
        return None
    try:
        return Path(os.fsdecode(raw.strip())).resolve()
    except (OSError, RuntimeError):
        return None


def _rev_parse_ok(top: Path, ref: str) -> bool:
    return _git_blob(top, "rev-parse", "--verify", "--quiet",
                     ref + "^{commit}", "--") is not None


def _merge_base(top: Path, ref: str) -> str | None:
    raw = _git_blob(top, "merge-base", "HEAD", ref, "--")
    if raw is None:
        return None
    try:
        return raw.decode("utf-8", "strict").strip() or None
    except UnicodeDecodeError:
        return None


def _default_ref(top: Path) -> str | None:
    """origin/HEAD target, else first existing local main/master/dev."""
    raw = _git_blob(top, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if raw is not None:
        try:
            target = raw.decode("utf-8", "strict").strip()
        except UnicodeDecodeError:
            target = ""
        if target.startswith("refs/remotes/origin/") \
                and _rev_parse_ok(top, target):
            return target[len("refs/remotes/"):]
    for name in ("main", "master", "dev"):
        if _rev_parse_ok(top, "refs/heads/" + name):
            return name
    return None


def _current_branch(top: Path) -> str | None:
    raw = _git_blob(top, "rev-parse", "--abbrev-ref", "HEAD")
    if raw is None:
        return None
    try:
        name = raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return None
    return name or None


def resolve_base(top: Path | None, explicit: str | None) -> Base:
    if explicit is not None:
        if top is None:
            raise _problem("--base needs a git repository")
        if (not explicit or explicit.startswith("-") or "\0" in explicit
                or not _rev_parse_ok(top, explicit)):
            raise _problem("--base is not a commit in this repository")
        sha = _merge_base(top, explicit)
        if not sha:
            raise _problem("--base is not a commit in this repository")
        return Base(sha=sha, label=explicit)
    if top is None:
        return Base(sha=None, label="HEAD")
    ref = _default_ref(top)
    if ref is None:
        return Base(sha=None, label="HEAD")
    if _current_branch(top) == ref.split("/")[-1]:
        return Base(sha=None, label="HEAD")
    sha = _merge_base(top, ref)
    if not sha:
        return Base(sha=None, label="HEAD")
    return Base(sha=sha, label=ref)


def changed_files(top: Path | None, base: Base) -> tuple[str, ...] | None:
    if top is None:
        return None
    return worktree_changed_files(Path(top), base.sha)


_TRIGGER_BASENAMES = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "pyproject.toml", "setup.py",
    "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", ".ptest.toml",
})

_CONFIG_PREFIXES = ("vitest.config.", "vite.config.", "jest.config.")

_IGNORED_SUFFIXES = (".md", ".rst", ".txt")

_IGNORED_BASENAMES = frozenset({"LICENSE", "CODEOWNERS"})

_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "site-packages",
                          "build", "dist", "venv", ".tox", "htmlcov"})

_OUTPUT_DIRS = frozenset({
    "build", "dist", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
})

_VITEST_CODE_EXTS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
})


def _is_output(path: str) -> bool:
    """Tool/build output is never a changed input, even when untracked."""
    parts = path.split("/")
    if any(part in _OUTPUT_DIRS or part.endswith(".egg-info")
           for part in parts):
        return True
    basename = parts[-1]
    return basename == ".coverage" or basename.startswith(".coverage.")


def _is_code_file(path: str, kind_value: str) -> bool:
    lowered = path.lower()
    if kind_value == C.RunnerKind.VITEST.value:
        return any(lowered.endswith(ext) for ext in _VITEST_CODE_EXTS)
    return lowered.endswith(".py")


def _dir_has_py_source(directory: Path) -> bool:
    """Whether a directory on disk looks like importable source (fail safe)."""
    try:
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.name.endswith(".py"):
                    return True
            except OSError:
                return True
    except OSError:
        return True
    return False


def _inside_code_area(project_root: Path, path: str,
                      test_roots: tuple) -> bool:
    """Whether a non-code file lies where a test could reach it.

    Inside a test root or a directory holding ``.py`` source (a package
    or a plain source dir) the file keeps today's fail-safe behaviour.
    A missing ancestor directory cannot be proven inside, so it counts
    as outside; code files never consult this helper.
    """
    if test_roots and any(_under_root(path, root) for root in test_roots):
        return True
    parts = path.split("/")[:-1]
    for index in range(1, len(parts) + 1):
        ancestor = project_root.joinpath(*parts[:index])
        try:
            if not ancestor.is_dir():
                return False
        except OSError:
            return True
        if _dir_has_py_source(ancestor):
            return True
    return False


def _is_trigger_basename(basename: str) -> bool:
    if basename in _TRIGGER_BASENAMES:
        return True
    if basename.startswith(_CONFIG_PREFIXES):
        return True
    return fnmatch.fnmatchcase(basename, "requirements*.txt")


def _is_test_basename(basename: str) -> bool:
    return (basename.startswith("test_") and basename.endswith(".py")
            or basename.endswith("_test.py"))


def _under_root(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def _is_test_file(path: str, test_roots: tuple) -> bool:
    basename = path.rsplit("/", 1)[-1]
    if not _is_test_basename(basename):
        return False
    return any(_under_root(path, root) for root in test_roots)


def _is_test_support(path: str, test_roots: tuple) -> bool:
    for root in test_roots:
        if root == ".":
            parts = path.split("/")[:-1]
            if "tests" in parts or "test" in parts:
                return True
        elif _under_root(path, root):
            return True
    return False


def _is_ignored(path: str, selection) -> bool:
    parts = path.split("/")
    if any(part.startswith(".") for part in parts):
        return True
    if path.lower().endswith(_IGNORED_SUFFIXES):
        return True
    if parts[-1] in _IGNORED_BASENAMES:
        return True
    if _matches(path, selection.no_tests):
        return True
    if _matches(path, selection.ignored_inputs):
        return True
    if _matches(path, selection.non_input_outputs):
        return True
    return False


def _iter_py_files(project_root: Path):
    """Yield project-relative posix paths of .py files, no symlink follow."""
    stack = [project_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name.startswith(".") or entry.name in _SKIP_DIRS \
                        or entry.name.endswith(".egg-info"):
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.name.endswith(".py"):
                yield entry.relative_to(project_root).as_posix()


def _module_names(project_root: Path, rel: str) -> tuple[str, ...]:
    """Dotted module names for one project-relative .py path.

    The dotted path from the project root, the same without a leading
    ``src.``, and the dotted path from the package root (the first
    ancestor directory without ``__init__.py``). A package
    ``__init__.py`` counts as the package module itself.
    """
    parts = rel.split("/")
    filename = parts[-1]
    dirs = parts[:-1]
    if filename == "__init__.py":
        mod_parts = dirs
    elif filename.endswith(".py"):
        mod_parts = dirs + [filename[:-len(".py")]]
    else:
        return ()
    names: set[str] = set()
    if mod_parts:
        names.add(".".join(mod_parts))
        if len(mod_parts) > 1 and mod_parts[0] == "src":
            names.add(".".join(mod_parts[1:]))
    node = project_root.joinpath(*dirs) if dirs else project_root
    while node != project_root:
        try:
            has_init = (node / "__init__.py").is_file()
        except OSError:
            break
        if not has_init:
            break
        node = node.parent
    try:
        pkg_rel = (project_root.joinpath(*parts)).relative_to(node).as_posix()
    except ValueError:
        pkg_rel = ""
    pkg_parts = pkg_rel.split("/") if pkg_rel else []
    if pkg_parts and pkg_parts[-1] == "__init__.py":
        pkg_parts = pkg_parts[:-1]
    elif pkg_parts and pkg_parts[-1].endswith(".py"):
        pkg_parts = pkg_parts[:-1] + [pkg_parts[-1][:-len(".py")]]
    if pkg_parts and pkg_parts != mod_parts:
        names.add(".".join(pkg_parts))
    return tuple(sorted(names))


def _package_name(rel: str) -> str:
    """From-root dotted package for resolving relative imports.

    An ``__init__.py`` is its own package; any other module resolves
    against its parent package.
    """
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _import_edges(source: str, package: str) -> set[str]:
    """Module names one file depends on (absolute + resolved relative)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    edges: set[str] = set()

    def _add(dotted: str) -> None:
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            edges.add(".".join(parts[:i]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level:
                base_parts = package.split(".") if package else []
                if level - 1 > len(base_parts):
                    continue
                base = ".".join(base_parts[:len(base_parts) - level + 1])
                head = (base + "." + node.module) if node.module else base
                head = head.strip(".")
            else:
                if not node.module:
                    continue
                head = node.module
            if not head:
                continue
            _add(head)
            for alias in node.names:
                if alias.name != "*":
                    edges.add(head + "." + alias.name)
    return edges


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def plan(top: Path | None, project_root: Path, config: C.Config,
         repo_changed: tuple[str, ...] | None) -> Impact:
    project_root = Path(project_root)
    if top is None or repo_changed is None:
        return Impact(kind="full", reason="git changes are unavailable")
    try:
        prefix = project_root.resolve().relative_to(
            Path(top).resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return Impact(kind="full", reason="git changes are unavailable")
    if prefix == ".":
        prefix = ""

    under: list[str] = []
    for path in repo_changed:
        if prefix == "":
            under.append(path)
        elif path == prefix or path.startswith(prefix + "/"):
            under.append(path[len(prefix) + 1:])

    if prefix != "":
        for path in sorted(repo_changed):
            if path == prefix or path.startswith(prefix + "/"):
                continue
            directory, _, basename = path.rpartition("/")
            if _is_trigger_basename(basename) and (
                    directory == "" or _matches(prefix, (directory,))):
                return Impact(kind="full",
                              reason=f"{path} is a full trigger")

    if not under:
        return Impact(kind="none")

    selection = config.selection
    kind_value = config.runner.kind.value
    test_roots = tuple(config.runner.test_roots)
    dropped = 0
    scoped: list[str] = []
    for path in sorted(under):
        if _is_output(path):
            dropped += 1
        else:
            scoped.append(path)
    for path in scoped:
        if _is_trigger_basename(path.rsplit("/", 1)[-1]) \
                or _matches(path, selection.full_triggers):
            return Impact(kind="full",
                          reason=f"{path} is a full trigger")
    relevant: list[str] = []
    noted: list[str] = []
    graph_kinds = (C.RunnerKind.PYTEST.value, C.RunnerKind.VITEST.value)
    for path in scoped:
        if _is_code_file(path, kind_value):
            (relevant if not _is_ignored(path, selection)
             else noted).append(path)
        elif kind_value in graph_kinds and not _inside_code_area(
                project_root, path, test_roots):
            dropped += 1
        elif _is_ignored(path, selection):
            noted.append(path)
        else:
            relevant.append(path)
    changed = tuple(sorted(relevant) + sorted(noted))
    if not relevant:
        return Impact(kind="none", changed=changed, ignored=dropped)

    if kind_value == C.RunnerKind.VITEST.value:
        return Impact(kind="vitest", changed=changed, ignored=dropped)
    if kind_value != C.RunnerKind.PYTEST.value:
        return Impact(kind="full", changed=changed, ignored=dropped,
                      reason=f"{kind_value} has no import graph")
    if not selection.enabled:
        return Impact(
            kind="full", changed=changed, ignored=dropped,
            reason="selection is off in .ptest.toml — "
                   "ptest doctor --fix turns it on")

    direct: set[str] = set()
    seeds: list[str] = []
    for path in sorted(relevant):
        if not path.endswith(".py"):
            return Impact(kind="full", changed=changed, ignored=dropped,
                          reason=f"{path} is outside the import graph")
        if _is_test_file(path, test_roots):
            if (project_root / path).is_file():
                direct.add(path)
            continue
        if _is_test_support(path, test_roots):
            return Impact(kind="full", changed=changed, ignored=dropped,
                          reason=f"{path} is test support")
        candidate = project_root / path
        if candidate.is_file():
            text = _read_text(candidate)
            if text is None:
                return Impact(kind="full", changed=changed, ignored=dropped,
                              reason=f"{path} could not be parsed")
            try:
                ast.parse(text)
            except (SyntaxError, ValueError):
                return Impact(kind="full", changed=changed, ignored=dropped,
                              reason=f"{path} could not be parsed")
        seeds.append(path)

    total = 0
    for rel in _iter_py_files(project_root):
        if _is_test_file(rel, test_roots):
            total += 1

    via: set[str] = set()
    if seeds:
        py_files: list[str] = []
        for rel in _iter_py_files(project_root):
            py_files.append(rel)
            if len(py_files) > MAX_SCAN_FILES:
                return Impact(kind="full", changed=changed, ignored=dropped,
                              reason="import graph too large")
        names_of: dict[str, tuple[str, ...]] = {}
        for rel in py_files:
            names_of[rel] = _module_names(project_root, rel)
        reverse: dict[str, set[str]] = {}
        for rel in py_files:
            text = _read_text(project_root / rel)
            if text is None:
                continue
            for name in _import_edges(text, _package_name(rel)):
                reverse.setdefault(name, set()).add(rel)
        queue: list[str] = []
        for seed in seeds:
            queue.extend(_module_names(project_root, seed))
        seen: set[str] = set(queue)
        reached: set[str] = set()
        while queue:
            name = queue.pop()
            for rel in reverse.get(name, ()):
                if rel in reached:
                    continue
                reached.add(rel)
                if _is_test_file(rel, test_roots):
                    if rel not in direct:
                        via.add(rel)
                for alias in names_of.get(rel, ()):
                    if alias not in seen:
                        seen.add(alias)
                        queue.append(alias)

    files = tuple(sorted(direct | via))
    if not files:
        return Impact(kind="none", changed=changed, ignored=dropped)
    if total > 0 and len(files) >= selection.full_ratio * total:
        return Impact(
            kind="full", changed=changed, ignored=dropped,
            reason=(f"{len(files)} of {total} test files reaches "
                    f"full_ratio {selection.full_ratio:g}"))
    if len(files) > MAX_SELECTED:
        return Impact(
            kind="full", changed=changed, ignored=dropped,
            reason=f"{len(files)} test files exceed the 200-file "
                   "scoped limit")
    return Impact(kind="selected", changed=changed, files=files,
                  direct=len(direct), via=len(via), total=total,
                  ignored=dropped)
