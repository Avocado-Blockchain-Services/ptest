"""Deterministic per-project executability checks.

Pure static inspection: no model, no subprocess, no imports of project
code. Reads are bounded and never follow symlinks.
"""
from __future__ import annotations

import configparser
import os
import re
import shlex
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C
from .adapters.pytest import reject_unowned_controls, require_python_launcher
from .adapters.vitest import VITEST_ENTRY, VITEST_EXCLUSIVE_NOTE
from .files import read_regular
from .runtime.pytest_bridge import (
    full_ini_refusal_name, full_narrowing_text, full_redirect_name,
)

STATUS_EXECUTABLE = "executable"
STATUS_CAVEAT = "caveat"
STATUS_NOT_EXECUTABLE = "not-executable"

_MAX_BYTES = 256 * 1024
_MAX_CONFTEST_FILES = 64
_MAX_WALK_ENTRIES = 2000
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__"})

_SCOPED_REFUSED_HOOKS = frozenset({
    "pytest_cmdline_main", "pytest_collection", "pytest_runtestloop",
    "pytest_runtest_protocol", "pytest_runtest_call", "pytest_pyfunc_call",
})
_FULL_REFUSED_HOOKS = frozenset({
    "pytest_collection_modifyitems", "pytest_ignore_collect",
    "pytest_runtest_makereport", "pytest_report_teststatus",
    "pytest_sessionfinish",
})
# Section F: conftest.py collection hooks are the project's own suite
# definition, so they are allowed in full mode and recorded in the run
# label; the remaining full-only hooks stay refused.
_FULL_COLLECTION_HOOKS = frozenset({
    "pytest_collection_modifyitems", "pytest_ignore_collect",
})
_HOOK_RE = re.compile(r"^(?:async\s+)?def\s+(pytest_[a-z_]+)\s*\(")
_SHORT_N_RE = re.compile(r"^-[qvxslhVfd]*n")
_VITEST_TEST_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mts|cts|mjs|cjs)$")

@dataclass(frozen=True, slots=True)
class Executability:
    project: str
    runner: str
    status: str
    caveats: tuple[str, ...]
    reason: str | None
    fix: str | None
    full: bool
    example: str | None

    def verdict(self) -> str:
        if self.status == STATUS_NOT_EXECUTABLE:
            return f"not runnable: {self.reason} — fix: {self.fix}"
        if self.status == STATUS_CAVEAT:
            return "ready with caveats: " + "; ".join(self.caveats)
        return "ready"

    def to_public(self) -> dict:
        if self.status == STATUS_NOT_EXECUTABLE:
            detail = self.reason
        else:
            detail = "; ".join(self.caveats) or "ready"
        return {"status": self.status, "detail": detail,
                "fix": self.fix if self.status == STATUS_NOT_EXECUTABLE else None}


def _cfg(project: str) -> str:
    return ".ptest.toml" if project == "." else f"{project}/.ptest.toml"


def _project_root(config: C.Config, project: str) -> Path:
    if config.config_path is not None:
        return config.config_path.parent
    if config.checkout is not None:
        return config.checkout.root
    return Path(project)


def _read(root: Path, name: str) -> bytes | None:
    try:
        raw = read_regular(root, name, _MAX_BYTES + 1)
    except C.Problem:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    return raw


def _ini_addopts(raw: bytes, section: str) -> tuple[str, ...] | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section(section):
        return None
    if not parser.has_option(section, "addopts"):
        return ()
    return _split_addopts(parser.get(section, "addopts"))


def _split_addopts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError:
            return ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _toml_pytest_addopts(raw: bytes) -> tuple[str, ...]:
    """addopts from a ``pytest.toml``/``.pytest.toml`` ``[pytest]`` table.

    A present file always wins (pytest 9 treats even an empty
    ``pytest.toml`` as its configuration source), yielding no addopts when
    it defines none. A malformed file yields no prediction: the native run
    refuses it at runtime.
    """
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    section = parsed.get("pytest", {})
    if not isinstance(section, dict) or "addopts" not in section:
        return ()
    return _split_addopts(section["addopts"])


def _pytest_addopts(root: Path) -> tuple[str, ...]:
    """First pytest section wins: pytest.toml, pytest.ini, pyproject, tox.ini, setup.cfg.

    Mirrors pytest 9's ``findpaths`` order (``pytest.toml``/``.pytest.toml``
    first, then ``pytest.ini``/``.pytest.ini``): the first file carrying
    pytest configuration decides the checked-in addopts.
    """
    for name in ("pytest.toml", ".pytest.toml"):
        raw = _read(root, name)
        if raw is not None:
            return _toml_pytest_addopts(raw) or ()
    for name, section in (("pytest.ini", "pytest"), (".pytest.ini", "pytest")):
        raw = _read(root, name)
        if raw is not None:
            found = _ini_addopts(raw, section)
            if found is not None:
                return found
    raw = _read(root, "pyproject.toml")
    if raw is not None:
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            tool = parsed.get("tool", {})
            if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
                section = tool["pytest"]
                ini_options = section.get("ini_options", {})
                if isinstance(ini_options, dict) and "addopts" in ini_options:
                    return _split_addopts(ini_options["addopts"])
                if "addopts" in section:
                    return _split_addopts(section["addopts"])
                return ()
    raw = _read(root, "tox.ini")
    if raw is not None:
        found = _ini_addopts(raw, "pytest")
        if found is not None:
            return found
    raw = _read(root, "setup.cfg")
    if raw is not None:
        found = _ini_addopts(raw, "tool:pytest")
        if found is not None:
            return found
    return ()


def _has_no_xdist(tokens: tuple[str, ...]) -> bool:
    for index, token in enumerate(tokens):
        if token == "no:xdist":
            return True
        if token == "-p" and index + 1 < len(tokens) \
                and tokens[index + 1] in ("no:xdist",):
            return True
        if token in ("-pno:xdist", "-p=no:xdist"):
            return True
    return False


def _xdist_active(tokens: tuple[str, ...]) -> bool:
    """True when pytest config tokens activate xdist (no:xdist suppresses).

    Zero worker counts (``-n 0``, ``-n0``, ``--numprocesses=0``) and
    ``--dist no`` serialize xdist instead of enabling it, matching the
    serial spellings the adapter accepts.
    """
    if _has_no_xdist(tokens):
        return False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        if token in ("-n", "--numprocesses"):
            if nxt == "0":
                index += 2
                continue
            return True
        if token in ("-n0", "--numprocesses=0"):
            index += 1
            continue
        if token == "--dist":
            if nxt == "no":
                index += 2
                continue
            return True
        if token == "--dist=no":
            index += 1
            continue
        if token.startswith("--dist="):
            return True
        if _SHORT_N_RE.match(token):
            # Attached cluster value (``-n2``, ``-vn4``): only zero
            # serializes; anything else enables xdist.
            rest = token[token.index("n", 1) + 1:]
            if rest == "0":
                index += 1
                continue
            return True
        if token.startswith("--numprocesses="):
            return True
        if token == "--maxprocesses" or token.startswith("--maxprocesses="):
            return True
        if token == "-p" and nxt in ("xdist", "xdist.plugin"):
            return True
        if token in ("-pxdist", "-pxdist.plugin", "-p=xdist", "-p=xdist.plugin"):
            return True
        index += 1
    return False


def pytest_xdist_active(root: Path) -> bool:
    """Shared init/executability predicate for static xdist activation."""
    return _xdist_active(_pytest_addopts(root))


def _has_serial_spelling(argv: tuple[str, ...]) -> bool:
    for index, token in enumerate(argv):
        if token == "-n0" or token == "--numprocesses=0":
            return True
        if token in ("-n", "--numprocesses") \
                and index + 1 < len(argv) and argv[index + 1] == "0":
            return True
    return False


def _unowned_token(argv: tuple[str, ...]) -> str | None:
    """First token reject_unowned_controls would refuse (full=False)."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if (token in ("-n", "--numprocesses")
                and index + 1 < len(argv) and argv[index + 1] == "0"):
            index += 2
            continue
        if token in ("-n0", "--numprocesses=0"):
            index += 1
            continue
        try:
            reject_unowned_controls((token,), full=False)
        except C.Problem:
            return token
        index += 1
    return None


def iter_files(root: Path, start: Path, depth: int, budget: list,
               visited: set[str] | None = None) -> object:
    """Yield project-relative files, sorted, bounded, skipping owned dirs.

    ``visited`` is a shared set of normalized directory paths already
    walked: when overlapping bases (``src/`` plus the repository root)
    share one set, each directory is entered once and the shared budget
    is not charged twice for the same files.
    """
    if visited is not None:
        key = os.path.normpath(os.fspath(start))
        if key in visited:
            return
        visited.add(key)
    try:
        entries = sorted(os.scandir(start), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        name = entry.name
        try:
            if entry.is_symlink():
                continue
        except OSError:
            continue
        if name in _SKIP_DIRS or name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir and visited is not None \
                and os.path.normpath(entry.path) in visited:
            continue
        budget[0] -= 1
        if budget[0] < 0:
            return
        if is_dir:
            if depth > 0:
                yield from iter_files(
                    root, Path(entry.path), depth - 1, budget, visited)
        else:
            try:
                rel = Path(entry.path).relative_to(root)
            except ValueError:
                continue
            yield rel.as_posix()


def _conftest_paths(root: Path, test_roots: tuple[str, ...]) -> list[str]:
    """conftest.py at the root and under literal test roots, depth 3."""
    starts: list[tuple[str, int]] = []
    for test_root in test_roots:
        if test_root in (".", ""):
            continue
        if test_root.startswith(("-", "@", "/")) or "\\" in test_root \
                or "::" in test_root:
            continue
        if any(part in {"", ".", ".."} for part in test_root.split("/")):
            continue
        starts.append((test_root, 3))
    found: list[str] = []
    try:
        stamp = os.lstat(root / "conftest.py")
    except OSError:
        pass
    else:
        if stat.S_ISREG(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode):
            found.append("conftest.py")
    for start, depth in starts:
        base = root / start
        try:
            stamp = os.lstat(base)
        except OSError:
            continue
        if not stat.S_ISDIR(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
            continue
        budget = [_MAX_WALK_ENTRIES]
        for rel in iter_files(root, base, depth, budget):
            if Path(rel).name != "conftest.py" or rel in found:
                continue
            found.append(rel)
            if len(found) >= _MAX_CONFTEST_FILES:
                return sorted(found)
    return sorted(found)


def _defined_hooks(root: Path, rel: str) -> list[str]:
    raw = _read(root, rel)
    if raw is None:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    hooks: list[str] = []
    for line in text.splitlines():
        match = _HOOK_RE.match(line)
        if match:
            hooks.append(match.group(1))
    return hooks


def _scan_conftest_hooks(root: Path, test_roots: tuple[str, ...]) -> list[tuple[str, str]]:
    """(conftest path, hook) pairs in file order for refused hooks."""
    pairs: list[tuple[str, str]] = []
    for rel in _conftest_paths(root, test_roots):
        for hook in _defined_hooks(root, rel):
            if hook in _SCOPED_REFUSED_HOOKS or hook in _FULL_REFUSED_HOOKS:
                pairs.append((rel, hook))
    return pairs


def _redirect_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Full-mode redirect names, derived from the bridge.

    Redirects stay refused even from checked-in configuration; plain
    narrowing filters are project-owned there and join the run label.
    """
    found: list[str] = []
    for index in range(len(tokens)):
        refusal = full_redirect_name(tokens, index)
        if refusal is not None and refusal not in found:
            found.append(refusal)
    return tuple(found)


def _refused_ini_narrowing(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Checked-in narrowing spellings the full gate refuses, in file order."""
    found: list[str] = []
    for index in range(len(tokens)):
        refusal = full_ini_refusal_name(tokens, index)
        if refusal is not None and refusal not in found:
            found.append(refusal)
    return tuple(found)


def full_project_filter_text(root: Path, test_roots: tuple[str, ...]) -> str | None:
    """Bare project-filtered text, or None when the static scan sees none.

    The text records narrowing from the project's checked-in pytest
    configuration (allowlisted addopts marker/keyword filters) and its
    ``conftest.py`` collection hooks, for example
    ``full (project-filtered: -m not slow; conftest collection hook)``.
    """
    parts: list[str] = []
    narrowing = full_narrowing_text(_pytest_addopts(root))
    if narrowing:
        parts.append(narrowing)
    pairs = _scan_conftest_hooks(root, test_roots)
    if any(hook in _FULL_COLLECTION_HOOKS for _, hook in pairs):
        parts.append("conftest collection hook")
    if not parts:
        return None
    return "full (project-filtered: " + "; ".join(parts) + ")"


def full_project_filter_label(root: Path, test_roots: tuple[str, ...]) -> str | None:
    """Init-time prediction of a project-filtered full gate, or None.

    Static only: the bridge-owned attempt report decides the run label at
    runtime, so this stays worded as a prediction (``expected: full
    (project-filtered: ...)``).
    """
    text = full_project_filter_text(root, test_roots)
    if text is None:
        return None
    return "expected: " + text


# Stock vitest ``configDefaults.exclude``: a config that spreads it (the
# common ``exclude: [...configDefaults.exclude, 'e2e/**']`` shape) lists no
# literal for the spread, so the known defaults are always applied.
_VITEST_DEFAULT_EXCLUDE = (
    "**/node_modules/**",
    "**/dist/**",
    "**/cypress/**",
    "**/.{idea,git,cache,output,temp}/**",
    "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build}.config.*",
)
_VITEST_CONFIG_NAMES = (
    "vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
    "vitest.config.mts", "vite.config.ts", "vite.config.js",
    "vite.config.mjs", "vite.config.mts",
)
_PLAYWRIGHT_CONFIG_NAMES = (
    "playwright.config.ts", "playwright.config.js",
    "playwright.config.mjs", "playwright.config.mts",
)
_PLAYWRIGHT_IMPORT = "@playwright/test"
_HEAD_BYTES = 4096


def _js_tokens(text: str) -> list:
    """Lex a JS/TS config into braces, strings, idents and punctuation.

    Static only: nothing is executed. Comments are dropped, template
    literals are opaque, and regex literals are skipped so a ``{`` inside
    one cannot corrupt brace-depth tracking. Non-literal values (spreads,
    identifiers, calls) surface as punctuation/idents and are ignored by
    the array readers below.
    """
    tokens: list = []
    i, n = 0, len(text)
    last: tuple | None = None
    while i < n:
        ch = text[i]
        if ch in " \t\r\n\v\f":
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i + 2)
            i = n if end < 0 else end + 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == "/" and i + 1 < n and text[i + 1] not in ("/", "*") \
                and (last is None or last in (
                    ("punct", "="), ("punct", "("), ("punct", ","),
                    ("punct", ":"), ("punct", "["), ("punct", "!"),
                    ("punct", "&"), ("punct", "|"), ("punct", "?"),
                    ("punct", "{"))):
            # Regex literal, not division: skip it including classes/escapes.
            j = i + 1
            in_class = False
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    j += 1
                    break
                elif c == "\n":
                    break
                j += 1
            i = j
        elif ch in ("'", '"'):
            j = i + 1
            out: list[str] = []
            closed = False
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    out.append(text[j + 1])
                    j += 2
                    continue
                if c == ch:
                    closed = True
                    j += 1
                    break
                if c == "\n":
                    break
                out.append(c)
                j += 1
            if closed:
                last = ("string", "".join(out))
                tokens.append(last)
            i = j
        elif ch == "`":
            j = i + 1
            depth = 0
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "$" and j + 1 < n and text[j + 1] == "{":
                    depth += 1
                    j += 2
                    continue
                if c == "}" and depth:
                    depth -= 1
                    j += 1
                    continue
                if c == "`" and not depth:
                    j += 1
                    break
                j += 1
            i = j
        elif ch in "{}[]():,=":
            last = ("punct", ch)
            tokens.append(last)
            i += 1
        elif ch.isalpha() or ch in "_$":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            last = ("ident", text[i:j])
            tokens.append(last)
            i = j
        else:
            i += 1
    return tokens


def _is_key(token: tuple, *names: str) -> bool:
    """True for an ident or quoted-string object key with one of ``names``."""
    return token[0] in ("ident", "string") and token[1] in names


def _test_block_arrays(text: str) -> tuple[list[str], list[str]]:
    """Literal ``exclude``/``include`` strings from every ``test: {...}`` block.

    Only keys one level inside ``test:`` are read, so nested ``include``
    arrays (coverage, typecheck) are ignored along with every non-literal
    value. Keys may be quoted (``"test"``, ``'exclude'``). Unknown or
    absent blocks yield empty lists.
    """
    tokens = _js_tokens(text)
    n = len(tokens)
    excludes: list[str] = []
    includes: list[str] = []
    index = 0
    while index + 2 < n:
        if _is_key(tokens[index], "test") \
                and tokens[index + 1] == ("punct", ":") \
                and tokens[index + 2] == ("punct", "{"):
            depth = 1
            index += 3
            while index < n and depth > 0:
                token = tokens[index]
                if token == ("punct", "{"):
                    depth += 1
                elif token == ("punct", "}"):
                    depth -= 1
                elif depth == 1 and _is_key(token, "exclude", "include") \
                        and index + 2 < n \
                        and tokens[index + 1] == ("punct", ":") \
                        and tokens[index + 2] == ("punct", "["):
                    target = excludes if token[1] == "exclude" else includes
                    brackets = 1
                    index += 3
                    while index < n and brackets > 0:
                        item = tokens[index]
                        if item == ("punct", "["):
                            brackets += 1
                        elif item == ("punct", "]"):
                            brackets -= 1
                        elif item[0] == "string" and brackets >= 1:
                            target.append(item[1])
                        index += 1
                    continue
                index += 1
            continue
        index += 1
    return excludes, includes


def _playwright_test_dir(root: Path) -> str:
    """Literal Playwright ``testDir`` (default ``e2e``); never executed."""
    for name in _PLAYWRIGHT_CONFIG_NAMES:
        raw = _read(root, name)
        if raw is None:
            continue
        try:
            tokens = _js_tokens(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        for first, second, third in zip(tokens, tokens[1:], tokens[2:]):
            if first == ("ident", "testDir") and second == ("punct", ":") \
                    and third[0] == "string":
                cleaned = third[1].strip().removeprefix("./").rstrip("/")
                if cleaned.startswith("/"):
                    continue  # absolute dirs point outside the project
                if cleaned and cleaned != ".":
                    return cleaned
    return "e2e"


_EXTGLOB_MARKERS = ("?(", "*(", "+(", "@(", "!(")


def _has_extglob(pattern: str) -> bool:
    """True when the glob uses extglob groups vitest supports natively.

    The static matcher cannot evaluate these, so the caller treats the
    pattern as unknown: an unknown include accepts, an unknown exclude
    is ignored.
    """
    return any(marker in pattern for marker in _EXTGLOB_MARKERS)


_MAX_BRACE_EXPANSIONS = 256
"""Cap on brace-expansion products per config glob; beyond it the glob is unknown."""


def _expand_braces(pattern: str) -> list[str] | None:
    """Expand every ``{a,b}`` group, including nested ones.

    Unbalanced braces and singletons (``{a}``) are left literal.
    Returns None when expansion exceeds ``_MAX_BRACE_EXPANSIONS``: the
    glob is then unknown (an unknown include accepts, an unknown
    exclude is ignored).
    """
    expanded = [pattern]
    while True:
        for index, item in enumerate(expanded):
            start = item.find("{")
            if start < 0:
                continue
            depth = 0
            end = -1
            for pos in range(start, len(item)):
                if item[pos] == "{":
                    depth += 1
                elif item[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        end = pos
                        break
            if end < 0:
                continue
            parts: list[str] = []
            current: list[str] = []
            nested = 0
            for char in item[start + 1:end]:
                if char == "{":
                    nested += 1
                elif char == "}":
                    nested -= 1
                if char == "," and nested == 0:
                    parts.append("".join(current))
                    current = []
                else:
                    current.append(char)
            parts.append("".join(current))
            if len(parts) == 1:
                continue
            expanded[index:index + 1] = [
                item[:start] + part + item[end + 1:] for part in parts]
            if len(expanded) > _MAX_BRACE_EXPANSIONS:
                return None
            break
        else:
            return expanded


def _glob_to_regex(pattern: str) -> str:
    """Translate one brace-free glob to an anchored regex.

    ``**/`` is zero or more directories, ``**`` is anything, ``*`` never
    crosses ``/``, ``?`` is one non-separator, and ``[...]`` classes pass
    through (``[!...]`` becomes negation).
    """
    out: list[str] = ["^"]
    index, end = 0, len(pattern)
    while index < end:
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 3] == "**/":
                out.append("(?:[^/]+/)*")
                index += 3
            elif pattern[index:index + 2] == "**":
                out.append(".*")
                index += 2
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                out.append(re.escape(char))
                index += 1
            else:
                body = pattern[index + 1:close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                index = close + 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return "".join(out)


_CompiledGlob = tuple[re.Pattern[str], ...] | None
"""One config glob, expanded and compiled; None means unknown.

Unknown covers extglob groups, brace explosions past
``_MAX_BRACE_EXPANSIONS``, and invalid classes such as ``[z-a]``.
"""


def _compile_glob(pattern: str) -> _CompiledGlob:
    """Expand and compile one config glob; None means unknown.

    Unknown globs never match here; the candidate filter decides (an
    unknown include accepts, an unknown exclude is ignored).
    """
    cleaned = pattern.strip().removeprefix("./")
    if not cleaned:
        return ()
    expanded = _expand_braces(cleaned)
    if expanded is None:
        return None
    compiled: list[re.Pattern[str]] = []
    for item in expanded:
        if _has_extglob(item):
            continue
        try:
            compiled.append(re.compile(_glob_to_regex(item)))
        except re.error:
            continue
    if not compiled:
        return None
    return tuple(compiled)


def _glob_match(pattern: str, rel: str) -> bool:
    """Match one config glob against a project-relative posix path.

    Anchored, so ``e2e/**`` never matches ``src/e2e/x.spec.ts``. Unknown
    patterns (extglob groups, brace explosions, invalid classes) never
    match here; the candidate filter decides (unknown include accepts,
    unknown exclude is ignored).
    """
    compiled = _compile_glob(pattern)
    if compiled is None:
        return False
    return any(rx.match(rel) for rx in compiled)


def _vitest_filters(
    root: Path,
) -> tuple[tuple[_CompiledGlob, ...], tuple[_CompiledGlob, ...], str]:
    """``(excludes, includes, test_dir)`` for one project root, precompiled.

    Every glob is expanded and compiled once here, so per-file candidate
    checks never recompile; unknown entries are None (an unknown include
    accepts, an unknown exclude is ignored). When a ``vitest.config.*``
    file is present, ``vite.config.*`` files are ignored: vitest resolves
    its ``test`` section from the vitest config alone. The Playwright
    ``testDir`` is returned separately and excluded with a plain prefix
    check, never as a glob.
    """
    excludes = list(_VITEST_DEFAULT_EXCLUDE)
    includes: list[str] = []
    raws: list[tuple[str, bytes]] = []
    for name in _VITEST_CONFIG_NAMES:
        raw = _read(root, name)
        if raw is None:
            continue
        raws.append((name, raw))
    if any(name.startswith("vitest.config") for name, _ in raws):
        raws = [(name, raw) for name, raw in raws
                if name.startswith("vitest.config")]
    for _, raw in raws:
        try:
            found, wanted = _test_block_arrays(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        excludes.extend(found)
        includes.extend(wanted)
    return (tuple(_compile_glob(pattern) for pattern in excludes),
            tuple(_compile_glob(pattern) for pattern in includes),
            _playwright_test_dir(root))


def _imports_playwright(root: Path, rel: str) -> bool:
    """True when the file head imports ``@playwright/test`` (or is unreadable)."""
    try:
        raw = read_regular(root, rel, _HEAD_BYTES + 1)
    except C.Problem:
        return True
    try:
        head = raw[:_HEAD_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return _PLAYWRIGHT_IMPORT in head


def _is_vitest_candidate(root: Path, rel: str,
                         excludes: tuple[_CompiledGlob, ...],
                         includes: tuple[_CompiledGlob, ...],
                         test_dir: str) -> bool:
    if not _VITEST_TEST_RE.search(Path(rel).name):
        return False
    if rel == test_dir or rel.startswith(test_dir + "/"):
        return False
    if includes:
        if all(pattern is not None for pattern in includes) \
                and not any(rx.match(rel) for pattern in includes
                            if pattern is not None for rx in pattern):
            return False
    if any(rx.match(rel) for pattern in excludes
           if pattern is not None for rx in pattern):
        return False
    return not _imports_playwright(root, rel)


def _walk_bases(root: Path, test_root: str) -> list[Path]:
    """Walk starts in selection order: ``src``/``__tests__`` first under ``.``.

    A dot root otherwise walks alphabetically, so ``e2e/`` shadows the real
    unit tests and a shared entry budget can expire before ``src/`` is
    reached. Priority starts reach the real tests first; the full root walk
    still follows so nothing is missed.
    """
    if test_root not in (".", ""):
        return [root / test_root]
    bases: list[Path] = []
    for sub in ("src", "__tests__"):
        candidate = root / sub
        try:
            stamp = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISDIR(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode):
            bases.append(candidate)
    bases.append(root)
    return bases


def iter_candidates(root: Path, test_root: str, kind: C.RunnerKind,
                    budget: list) -> object:
    """Yield candidate test paths in selection order, deduped and bounded.

    The single selection shared by the executability example and the smoke
    candidate: name shape, vitest exclude/include globs, the Playwright
    testDir, ``@playwright/test`` imports, and src-first ordering under a
    dot root. ``budget`` is the shared ``[remaining]`` entry counter.
    """
    filters = _vitest_filters(root) if kind is C.RunnerKind.VITEST else None
    seen: set[str] = set()
    visited: set[str] = set()
    for base in _walk_bases(root, test_root):
        try:
            stamp = os.lstat(base)
        except OSError:
            continue
        if not stat.S_ISDIR(stamp.st_mode) or stat.S_ISLNK(stamp.st_mode):
            continue
        for rel in iter_files(root, base, 6, budget, visited):
            if rel in seen:
                continue
            seen.add(rel)
            name = Path(rel).name
            if kind is C.RunnerKind.PYTEST:
                if name.startswith("test_") and name.endswith(".py") \
                        or name.endswith("_test.py"):
                    yield rel
            elif filters is not None and _is_vitest_candidate(root, rel, *filters):
                yield rel


def _example_test(root: Path, test_root: str, kind: C.RunnerKind) -> str | None:
    for rel in iter_candidates(root, test_root, kind, [_MAX_WALK_ENTRIES]):
        return str(rel)
    return None


def _regular_present(root: Path, relative: str) -> bool:
    try:
        stamp = os.lstat(root / relative)
    except OSError:
        return False
    return stat.S_ISREG(stamp.st_mode) and not stat.S_ISLNK(stamp.st_mode)


def check_config(config: C.Config, *, project: str = ".") -> Executability:
    cfg = _cfg(project)
    root = _project_root(config, project)
    kind = config.runner.kind
    example: str | None = None
    roots = config.runner.test_roots
    first_root = roots[0] if roots else "."
    if kind is C.RunnerKind.PYTEST or kind is C.RunnerKind.VITEST:
        example = _example_test(root, first_root, kind)

    if kind is C.RunnerKind.PYTEST:
        try:
            require_python_launcher(config.runner.launcher)
        except C.Problem:
            return Executability(
                project=project, runner=kind.value,
                status=STATUS_NOT_EXECUTABLE, caveats=(),
                reason="pytest launcher is not a supported Python interpreter launcher",
                fix=f'set [runner] launcher = ["uv", "run", "--locked", "--no-sync", "python"]'
                    f' or ["python"] in {cfg}',
                full=False, example=example)
        bad = _unowned_token(
            tuple(config.runner.args) + tuple(config.runner.full_args))
        if bad is not None:
            return Executability(
                project=project, runner=kind.value,
                status=STATUS_NOT_EXECUTABLE, caveats=(),
                reason=f"runner args contain a parallel, remote or argfile control ({bad})",
                fix=f"remove {bad} from [runner] args in {cfg}",
                full=False, example=example)
        addopts = _pytest_addopts(root)
        active = _xdist_active(addopts)
        serial = _has_serial_spelling(
            tuple(config.runner.args) + tuple(config.runner.full_args))
        if active and not serial:
            return Executability(
                project=project, runner=kind.value,
                status=STATUS_NOT_EXECUTABLE, caveats=(),
                reason="pytest addopts enable xdist, which ptest runs serially",
                fix=f'add "-n", "0" to [runner] args in {cfg}',
                full=False, example=example)
        pairs = _scan_conftest_hooks(root, roots)
        for rel, hook in pairs:
            if hook in _SCOPED_REFUSED_HOOKS:
                return Executability(
                    project=project, runner=kind.value,
                    status=STATUS_NOT_EXECUTABLE, caveats=(),
                    reason=f"{rel} defines {hook}, which ptest refuses",
                    fix=f"move {hook} out of conftest.py into an installed plugin,"
                        " or configure a command profile",
                    full=False, example=example)
        caveats: list[str] = []
        full = True
        if active and serial:
            caveats.append("serial: xdist disabled under ptest (-n 0)")
        if "." in roots:
            caveats.append('ptest --full unavailable: test_roots is "."')
            full = False
        redirects = _redirect_tokens(addopts)
        if redirects:
            caveats.append(
                "ptest --full unavailable: pytest addopts redirect native configuration ("
                + " ".join(redirects) + ")")
            full = False
        refused = _refused_ini_narrowing(addopts)
        if refused:
            caveats.append(
                "ptest --full unavailable: pytest addopts narrow or observe the suite ("
                + " ".join(refused) + ")")
            full = False
        # No prediction when the addopts themselves refuse the full run:
        # labelling a run that cannot start would contradict the caveat.
        label = None if (redirects or refused) else full_project_filter_label(root, roots)
        if label is not None:
            caveats.append(label)
        for rel, hook in pairs:
            if hook in _FULL_REFUSED_HOOKS and hook not in _FULL_COLLECTION_HOOKS:
                caveats.append(f"ptest --full unavailable: {rel} defines {hook}")
                full = False
                break
        if config.setup is not None:
            caveats.append(
                "setup runs when required paths or its fingerprint are missing: " + " ".join(config.setup.argv))
        if caveats:
            return Executability(
                project=project, runner=kind.value, status=STATUS_CAVEAT,
                caveats=tuple(caveats), reason=None, fix=None,
                full=full, example=example)
        return Executability(
            project=project, runner=kind.value, status=STATUS_EXECUTABLE,
            caveats=(), reason=None, fix=None, full=True, example=example)

    if kind is C.RunnerKind.VITEST:
        launcher = config.runner.launcher
        if not (launcher == ("node",)
                or (len(launcher) == 1 and Path(launcher[0]).is_absolute()
                    and Path(launcher[0]).name == "node")):
            return Executability(
                project=project, runner=kind.value,
                status=STATUS_NOT_EXECUTABLE, caveats=(),
                reason="vitest launcher must be node",
                fix=f'set [runner] launcher = ["node"] in {cfg}',
                full=False, example=example)
        if not _regular_present(root, VITEST_ENTRY) and config.setup is None:
            return Executability(
                project=project, runner=kind.value,
                status=STATUS_NOT_EXECUTABLE, caveats=(),
                reason="node_modules/vitest/vitest.mjs is missing",
                fix='install dependencies, or declare [setup] argv = ["npm", "ci"]'
                    f' in {cfg}',
                full=False, example=example)
        caveats = ["exclusive: Vitest runs as one command and manages its own workers"]
        if config.setup is not None:
            caveats.append(
                "setup runs when required paths or its fingerprint are missing: " + " ".join(config.setup.argv))
        return Executability(
            project=project, runner=kind.value, status=STATUS_CAVEAT,
            caveats=tuple(caveats), reason=None, fix=None,
            full=True, example=example)

    if kind is C.RunnerKind.COMMAND:
        caveats = ["exclusive: runs as one literal command"]
        if config.setup is not None:
            caveats.append(
                "setup runs when required paths or its fingerprint are missing: " + " ".join(config.setup.argv))
        return Executability(
            project=project, runner=kind.value, status=STATUS_CAVEAT,
            caveats=tuple(caveats), reason=None, fix=None,
            full=True, example=example)

    return Executability(
        project=project, runner=kind.value, status=STATUS_NOT_EXECUTABLE,
        caveats=(),
        reason=f"native {kind.value} execution is not available in this release",
        fix=f'configure kind = "command" with an explicit launcher in {cfg}',
        full=False, example=example)


def check_resolution(resolution: C.ConfigResolution) -> tuple[Executability, ...]:
    manifest = getattr(resolution, "monorepo", None)
    children = tuple(getattr(manifest, "children", ()) or ()) \
        if manifest is not None else ()
    if children:
        from .config import resolve_config
        items: list[Executability] = []
        for declaration in children:
            child = resolve_config(resolution.root / declaration)
            if child.config is None or child.problem is not None:
                items.append(Executability(
                    project=declaration, runner="unknown",
                    status=STATUS_NOT_EXECUTABLE, caveats=(),
                    reason="child configuration is missing or invalid",
                    fix="run ptest init from the repository root",
                    full=False, example=None))
            else:
                items.append(check_config(child.config, project=declaration))
        return tuple(items)
    if resolution.config is not None:
        return (check_config(resolution.config, project="."),)
    return (Executability(
        project=".", runner="unknown", status=STATUS_NOT_EXECUTABLE,
        caveats=(), reason="no ptest configuration",
        fix="run ptest init from the repository root",
        full=False, example=None),)


def commands(items: tuple[Executability, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for item in items:
        if item.status == STATUS_NOT_EXECUTABLE or item.example is None:
            continue
        prefix = "" if item.project == "." else f"{item.project}/"
        command = f"ptest {prefix}{item.example}"
        if command not in ordered:
            ordered.append(command)
    if items and all(item.full for item in items):
        if "ptest --full" not in ordered:
            ordered.append("ptest --full")
    return tuple(ordered[:8])
