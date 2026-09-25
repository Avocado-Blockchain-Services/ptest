"""Runner-aware review context for doctor assessment (milestone 1).

Holds immutable context metadata plus pure classification/routing helpers.
Secure file walking, admission accounting, excerpt identities, and final
publication stay in their existing owners; this module never executes
configuration, never follows symlinks, and never emits secret/private
names into inventories.

Shared low-level JS token/glob primitives live here as the single source
of truth; :mod:`ptest.executability` imports them so smoke behavior is
preserved without a second parser or a new dependency.

Missing-evidence reasons: ``not-collected``, ``outside-selected-scope``,
``excluded-suite``, ``dynamic-config``, ``unresolved-import``,
``read-limit``, ``item-limit``, ``depth-limit``, ``ambiguous-config``.
"""
from __future__ import annotations

import os
import re
import stat
import configparser
import shlex
import tomllib
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path

from .files import read_regular

CONTEXT_MAX_FILES = 32
"""Bound on files touched by one context-collection traversal."""

_CONTEXT_MAX_DEPTH = 3
"""Deepest import/fixture link followed from a context seed."""

_CONTEXT_MAX_LINKS_PER_FILE = 16
"""Outgoing links parsed from a single file."""

_CONTEXT_READ_LIMIT = 64 * 1024 + 1
"""Bounded read for one context file (mirrors the excerpt cap + sentinel)."""

_CONTEXT_MISSING_CAP = 64
"""Bound on recorded missing-evidence entries per context."""

_KNOWN_EXCLUDED_CAP = 64
"""Bound on the known-suite exclusion inventory per context."""

_CONFIG_STATUSES = frozenset({"resolved", "partial", "unavailable"})

_CONTEXT_ROLES = frozenset(
    {"config", "setup", "fixture", "helper", "test", "source"})

_RELATION_KINDS = frozenset({"local-import", "fixture-use"})
_MISSING_REASONS = frozenset({
    "not-collected", "outside-selected-scope", "excluded-suite",
    "dynamic-config", "unresolved-import", "read-limit", "item-limit",
    "depth-limit", "ambiguous-config", "unsupported-argv",
})

# One exclusion authority shared with packet walking and every static
# context read/metadata path.  Keeping it here avoids a weaker second
# policy in import traversal.
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".pipeline", ".superpowers", "graphify-out",
    ".ptest", ".claude", ".agents", ".codex", ".opencode", ".gemini",
    ".venv", "venv", "node_modules", "__pycache__", "build", "dist",
    "target", "coverage", ".coverage", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".next", ".ssh", ".aws", ".gnupg",
})
_EXCLUDED_FILES = frozenset({
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".npmrc", ".netrc",
    ".pypirc", "recommendations.md",
})
_EXCLUDED_SUFFIXES = (".diff", ".patch", ".pem", ".key", ".p12", ".pfx")
_PRIVATE_BASENAME = re.compile(
    r"(?:^\.env(?:\.|$)|^id_|^(?:secrets?|credentials?)(?:[._-]|$))", re.I)
_GENERATED_BASENAME = re.compile(r"\.(?:min|generated)\.", re.I)


def is_excluded_name(name: str) -> bool:
    """Shared filename exclusion policy for collected source and metadata."""
    if not isinstance(name, str) or not name:
        return True
    return (name in _EXCLUDED_DIRS or name in _EXCLUDED_FILES
            or name.lower().endswith(_EXCLUDED_SUFFIXES)
            or bool(_PRIVATE_BASENAME.search(name))
            or bool(_GENERATED_BASENAME.search(name)))


def is_excluded_path(rel: str) -> bool:
    """True when any normalized path component is excluded or private."""
    if not isinstance(rel, str) or not rel:
        return True
    return any(is_excluded_name(part) for part in rel.split("/"))


# --- shared JS token/glob primitives (moved from executability) --------------
#
# Static only: nothing is executed. Comments are dropped, template literals
# are opaque, and regex literals are skipped so a ``{`` inside one cannot
# corrupt brace-depth tracking.

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
        elif ch in "{}[]():,=.;?*>+-|&":
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


# Idents allowed inside a literal array without marking the value dynamic:
# the recognized ``...configDefaults.exclude`` spread.
_SPREAD_IDENTS = frozenset({"configDefaults", "exclude"})


def _test_block_key_arrays(
        text: str, keys: tuple[str, ...]) -> tuple[dict[str, list[str]], bool]:
    """Literal string arrays for ``keys`` one level inside ``test: {...}``.

    Returns ``(literals, dynamic)``. Every ``test:`` block is scanned;
    only keys one level inside it are read, so nested ``include`` arrays
    (coverage, typecheck) are ignored along with every non-literal value.
    ``dynamic`` is True when a wanted key carries a non-array value or a
    non-literal array element outside the recognized
    ``...configDefaults.exclude`` spread.
    """
    wanted = frozenset(keys)
    literals: dict[str, list[str]] = {key: [] for key in keys}
    dynamic = False
    tokens = _js_tokens(text)
    n = len(tokens)
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
                elif depth == 1 and token[0] in ("ident", "string") \
                        and token[1] in wanted \
                        and index + 2 < n \
                        and tokens[index + 1] == ("punct", ":"):
                    opener = tokens[index + 2]
                    if opener != ("punct", "["):
                        dynamic = True
                        index += 1
                        continue
                    brackets = 1
                    index += 3
                    while index < n and brackets > 0:
                        item = tokens[index]
                        if item == ("punct", "["):
                            brackets += 1
                        elif item == ("punct", "]"):
                            brackets -= 1
                        elif item[0] == "string" and brackets >= 1:
                            literals[token[1]].append(item[1])
                        elif item[0] == "ident" \
                                and item[1] not in _SPREAD_IDENTS:
                            dynamic = True
                        elif item[0] == "punct" and item[1] in ("(", ")"):
                            dynamic = True
                        index += 1
                    continue
                index += 1
            continue
        index += 1
    return literals, dynamic


def _test_block_arrays(text: str) -> tuple[list[str], list[str]]:
    """Literal ``exclude``/``include`` strings from every ``test: {...}`` block.

    Only keys one level inside ``test:`` are read, so nested ``include``
    arrays (coverage, typecheck) are ignored along with every non-literal
    value. Keys may be quoted (``"test"``, ``'exclude'``). Unknown or
    absent blocks yield empty lists.
    """
    literals, _ = _test_block_key_arrays(text, ("exclude", "include"))
    return literals["exclude"], literals["include"]


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


_VITEST_CONFIG_NAMES = (
    "vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
    "vitest.config.mts", "vite.config.ts", "vite.config.js",
    "vite.config.mjs", "vite.config.mts",
)
"""Supported Vitest/Vite config basenames, in discovery order."""


# --- immutable context metadata ----------------------------------------------


def _check_relpath(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{name} must be a nonempty string")
    if value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError(f"{name} must be root-relative")
    if re.match(r"[A-Za-z]:", value):
        raise ValueError(f"{name} must be root-relative")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(f"{name} is not normalized")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} is not valid UTF-8") from None
    return value


@dataclass(frozen=True)
class ReviewContext:
    """Immutable runner-aware context metadata for one child packet."""

    runner_kind: str
    config_paths: tuple[str, ...] = ()
    active_roots: tuple[str, ...] = ()
    roles: tuple[tuple[str, str], ...] = ()
    relations: tuple[tuple[str, str, str], ...] = ()
    known_excluded: tuple[str, ...] = ()
    missing: tuple[tuple[str, str], ...] = ()
    config_status: str = "unavailable"
    suite_profiles: tuple["SuiteProfile", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.runner_kind, str) or not self.runner_kind:
            raise TypeError("context.runner_kind must be a nonempty str")
        for field in ("config_paths", "active_roots", "known_excluded"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)):
                raise TypeError(f"context.{field} must be a tuple")
            for value in values:
                _check_relpath(value, f"context.{field}")
                if is_excluded_path(value):
                    raise ValueError(f"context.{field} contains excluded path")
            object.__setattr__(self, field, tuple(values))
        roles = self.roles
        if not isinstance(roles, (tuple, list)):
            raise TypeError("context.roles must be a tuple")
        for role in roles:
            if (not isinstance(role, (tuple, list)) or len(role) != 2
                    or not isinstance(role[0], str)
                    or role[1] not in _CONTEXT_ROLES):
                raise TypeError(
                    "context.roles entries must be (path, role) pairs")
            _check_relpath(role[0], "context.roles.path")
            if is_excluded_path(role[0]):
                raise ValueError("context.roles contains excluded path")
        object.__setattr__(self, "roles", tuple(
            (path, role) for path, role in roles))
        relations = self.relations
        if not isinstance(relations, (tuple, list)):
            raise TypeError("context.relations must be a tuple")
        for relation in relations:
            if (not isinstance(relation, (tuple, list))
                    or len(relation) != 3
                    or not isinstance(relation[0], str)
                    or not isinstance(relation[1], str)
                    or relation[2] not in _RELATION_KINDS):
                raise TypeError(
                    "context.relations entries must be "
                    "(from, to, kind) triples")
            _check_relpath(relation[0], "context.relations.from")
            _check_relpath(relation[1], "context.relations.to")
            if is_excluded_path(relation[0]) or is_excluded_path(relation[1]):
                raise ValueError("context.relations contains excluded path")
        object.__setattr__(self, "relations", tuple(
            (source, target, kind) for source, target, kind in relations))
        missing = self.missing
        if not isinstance(missing, (tuple, list)):
            raise TypeError("context.missing must be a tuple")
        for entry in missing:
            if (not isinstance(entry, (tuple, list)) or len(entry) != 2
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], str)
                    or not entry[1]):
                raise TypeError(
                    "context.missing entries must be (path, reason) pairs")
            _check_relpath(entry[0], "context.missing.path")
            if is_excluded_path(entry[0]):
                raise ValueError("context.missing contains excluded path")
        object.__setattr__(self, "missing", tuple(
            (path, reason) for path, reason in missing))
        profiles = self.suite_profiles
        if not isinstance(profiles, (tuple, list)):
            raise TypeError("context.suite_profiles must be a tuple")
        if any(not isinstance(profile, SuiteProfile) for profile in profiles):
            raise TypeError("context.suite_profiles entries must be SuiteProfile")
        if len(profiles) > 2 or len({p.name for p in profiles}) != len(profiles):
            raise ValueError("context.suite_profiles must have unique bounded names")
        object.__setattr__(self, "suite_profiles", tuple(profiles))
        if self.config_status not in _CONFIG_STATUSES:
            raise ValueError("context.config_status is unknown")


def empty_context(runner_kind: str) -> ReviewContext:
    """Context for runners with no suite/config semantics (e.g. command)."""
    if not isinstance(runner_kind, str) or not runner_kind:
        raise TypeError("runner_kind must be a nonempty str")
    return ReviewContext(
        runner_kind=runner_kind, missing=(("config", "not-collected"),),
        config_status="unavailable")


def replace(context: ReviewContext, **changes) -> ReviewContext:
    """Return a copy of ``context`` with ``changes`` applied (validated)."""
    if not isinstance(context, ReviewContext):
        raise TypeError("context must be a ReviewContext")
    return _dc_replace(context, **changes)


def context_body(context: ReviewContext) -> dict:
    """Canonical JSON-safe mapping of ``context`` for packet hashing."""
    if not isinstance(context, ReviewContext):
        raise TypeError("context must be a ReviewContext")
    return {
        "runner_kind": context.runner_kind,
        "config_paths": list(context.config_paths),
        "active_roots": list(context.active_roots),
        "roles": [[path, role] for path, role in context.roles],
        "relations": [[source, target, kind]
                      for source, target, kind in context.relations],
        "known_excluded": list(context.known_excluded),
        "missing": [[path, reason] for path, reason in context.missing],
        "config_status": context.config_status,
        "suite_profiles": [{
            "name": profile.name,
            "config_path": profile.config_path,
            "status": profile.status,
            "includes": list(profile.includes),
            "excludes": list(profile.excludes),
        } for profile in context.suite_profiles],
    }


# --- safe path helpers --------------------------------------------------------


def _normalize_ref(value: str) -> str | None:
    """Normalize a config-supplied relative path; None when it escapes."""
    if not isinstance(value, str) or not value:
        return None
    if "\\" in value or "\x00" in value:
        return None
    if re.match(r"[A-Za-z]:", value):
        return None
    candidate = value.strip()
    if candidate.startswith("/") or candidate.startswith("~"):
        return None
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def _is_sensitive(rel: str) -> bool:
    """Compatibility name for the shared path exclusion authority."""
    return is_excluded_path(rel)


def _within_scope(rel: str, scope: str) -> bool:
    """True when child-relative ``rel`` lies inside the selected scope."""
    if scope in ("", "."):
        return True
    return rel == scope or rel.startswith(scope + "/")


def _sanitize_roots(roots: tuple) -> tuple[str, ...]:
    """Configured roots safe for the ``active_roots`` inventory.

    Whole-child ``.`` roots and anything that is not a normalized
    root-relative path are dropped rather than inventoried: the field
    must never carry values the context validator rejects.
    """
    clean: list[str] = []
    for root in roots:
        if not isinstance(root, str) or root in ("", "."):
            continue
        try:
            _check_relpath(root, "active_roots")
        except (TypeError, ValueError):
            continue
        if root not in clean:
            clean.append(root)
    return tuple(clean)


# --- Vitest single-config selector --------------------------------------------


def _config_flag_values(argv: tuple[str, ...]) -> tuple[list[str], bool]:
    """Literal ``--config`` values in ``argv`` plus whether parsing is clean."""
    values: list[str] = []
    ok = True
    index = 0
    items = list(argv)
    while index < len(items):
        token = items[index]
        if not isinstance(token, str):
            ok = False
            index += 1
            continue
        if token == "--config":
            if index + 1 >= len(items) \
                    or not isinstance(items[index + 1], str):
                return values, False
            values.append(items[index + 1])
            index += 2
            continue
        if token.startswith("--config="):
            values.append(token.partition("=")[2])
            index += 1
            continue
        index += 1
    return values, ok


def _regular_file_names(root: Path) -> dict[str, bool] | None:
    """Basename → is-regular-non-symlink for ``root``; None when unreadable."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return None
    found: dict[str, bool] = {}
    for entry in entries:
        try:
            stamp = os.lstat(entry.path)
        except OSError:
            continue
        found[entry.name] = (stat.S_ISREG(stamp.st_mode)
                             and not stat.S_ISLNK(stamp.st_mode))
    return found


def _select_from_names(names: set[str], values: list[str], ok: bool,
                       is_regular) -> tuple[str | None, str]:
    """Single-config selection over discovered config basenames.

    ``is_regular(name)`` reports a regular non-symlink file. A unique
    safely representable literal ``--config`` wins; without it, the sole
    supported Vitest candidate wins (or the sole Vite candidate only when
    no Vitest candidate exists). Multiple candidates or conflicting flags
    stay partial; patterns are never unioned.
    """
    if not ok:
        return None, "partial"
    if len(set(values)) > 1:
        return None, "partial"
    if values:
        normalized = _normalize_ref(values[0])
        if normalized is None or "/" in normalized:
            # Only child-root config files are selectable here; nested or
            # escaping references stay partial, never authoritative.
            return None, "partial"
        if normalized not in names or not is_regular(normalized):
            return None, "partial"
        return normalized, "resolved"
    vitest = [name for name in _VITEST_CONFIG_NAMES
              if name.startswith("vitest.config") and name in names
              and is_regular(name)]
    if len(vitest) > 1:
        return None, "partial"
    if vitest:
        return vitest[0], "resolved"
    vite = [name for name in _VITEST_CONFIG_NAMES
            if name.startswith("vite.config") and name in names
            and is_regular(name)]
    if len(vite) > 1:
        return None, "partial"
    if vite:
        return vite[0], "resolved"
    return None, "unavailable"


def select_vitest_config(root: Path,
                         argv: tuple[str, ...]) -> tuple[str | None, str]:
    """Select the single authoritative Vitest config for one profile.

    Honors a unique safely representable literal ``--config``; without it,
    uses the sole supported Vitest config candidate (or the sole Vite
    candidate only when no Vitest candidate exists). Multiple candidates
    or conflicting/unsafe flags yield ``(None, "partial")``; no candidate
    yields ``(None, "unavailable")``. Never unions conflicting patterns.
    Symlinks are never followed.
    """
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not isinstance(argv, (tuple, list)):
        raise TypeError("argv must be a tuple")
    found = _regular_file_names(root)
    if found is None:
        return None, "unavailable"
    names = {name for name in found if name in _VITEST_CONFIG_NAMES}
    values, ok = _config_flag_values(tuple(argv))
    return _select_from_names(
        names, values, ok, lambda name: found.get(name, False))


@dataclass(frozen=True)
class SuitePatterns:
    """Literal suite patterns parsed from one config text (never executed)."""

    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    setups: tuple[str, ...] = ()
    dynamic: bool = False


@dataclass(frozen=True)
class SuiteProfile:
    """One scoped/full argv profile and its selected literal config facts."""

    name: str
    config_path: str | None
    status: str
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in ("scoped", "full"):
            raise ValueError("suite profile name is unknown")
        if self.config_path is not None:
            _check_relpath(self.config_path, "suite_profile.config_path")
        if self.status not in _CONFIG_STATUSES:
            raise ValueError("suite profile status is unknown")
        for field in ("includes", "excludes"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)):
                raise TypeError(f"suite_profile.{field} must be a tuple")
            if any(not isinstance(value, str) or len(value) > 512
                   for value in values):
                raise ValueError(f"suite_profile.{field} entries are invalid")
            object.__setattr__(self, field, tuple(values))


def parse_config_text(text: str) -> SuitePatterns:
    """Parse an unambiguous exported literal Vitest config without running it.

    Only ``export default { ... }`` and ``export default defineConfig({
    ... })`` are authoritative forms. Test objects in unused declarations,
    conditional exports, functions, and arbitrary spreads stay partial.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    literals, dynamic = _exported_test_arrays(
        text, ("include", "exclude", "setupFiles", "globalSetup"))
    setups = [value for value in literals["setupFiles"]
              + literals["globalSetup"]]
    return SuitePatterns(
        includes=tuple(v for v in literals["include"]
                       if not _is_sensitive(v)),
        excludes=tuple(v for v in literals["exclude"]
                       if not _is_sensitive(v)),
        setups=tuple(v for v in setups if not _is_sensitive(v)),
        dynamic=dynamic)


def _matching_token(tokens: list, start: int, opener: str,
                    closer: str) -> int | None:
    """Return the matching closer index for one balanced token group."""
    if start >= len(tokens) or tokens[start] != ("punct", opener):
        return None
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token == ("punct", opener):
            depth += 1
        elif token == ("punct", closer):
            depth -= 1
            if depth == 0:
                return index
    return None


def _exported_root_object(tokens: list) -> tuple[int, int] | None:
    """Find the sole supported root object in an exported config expression."""
    export_at: int | None = None
    depth = {"{": 0, "[": 0, "(": 0}
    pairs = {"}": "{", "]": "[", ")": "("}
    for index in range(len(tokens) - 1):
        token = tokens[index]
        if (not any(depth.values())
                and token == ("ident", "export")
                and tokens[index + 1] == ("ident", "default")):
            if export_at is not None:
                return None
            export_at = index
        if token[0] == "punct":
            if token[1] in depth:
                depth[token[1]] += 1
            elif token[1] in pairs:
                depth[pairs[token[1]]] = max(
                    0, depth[pairs[token[1]]] - 1)
    if export_at is None:
        return None
    expression = export_at + 2
    wrapped = (expression + 2 < len(tokens)
               and tokens[expression] == ("ident", "defineConfig")
               and tokens[expression + 1] == ("punct", "(")
               and tokens[expression + 2] == ("punct", "{"))
    if wrapped and not _trusted_define_config_binding(tokens):
        return None
    root_open = expression + 2 if wrapped else expression
    if root_open >= len(tokens) or tokens[root_open] != ("punct", "{"):
        return None
    root_close = _matching_token(tokens, root_open, "{", "}")
    if root_close is None:
        return None
    tail = root_close + 1
    if wrapped:
        if tail >= len(tokens) or tokens[tail] != ("punct", ")"):
            return None
        tail += 1
    if any(token != ("punct", ";") for token in tokens[tail:]):
        return None
    return root_open, root_close


def _trusted_define_config_binding(tokens: list) -> bool:
    """Accept only the direct Vitest named import, without a local shadow."""
    imported = False
    for index in range(len(tokens) - 1):
        if tokens[index:index + 2] != [
                ("ident", "import"), ("punct", "{")]:
            continue
        close = _matching_token(tokens, index + 1, "{", "}")
        if close is None or close + 2 >= len(tokens):
            continue
        if tokens[close + 1:close + 3] != [
                ("ident", "from"), ("string", "vitest/config")]:
            continue
        members = tokens[index + 2:close]
        # A direct binding is safe to recognize among additional named
        # imports. Do not accept `defineConfig as other`, since the exported
        # expression below calls the original name.
        if any(token == ("ident", "defineConfig")
               and (position == 0
                    or members[position - 1] == ("punct", ","))
               and (position + 1 == len(members)
                    or members[position + 1] == ("punct", ","))
               for position, token in enumerate(members)):
            imported = True
            break
    if not imported:
        return False

    # A top-level declaration with this name shadows the imported helper.
    # Track only statement-level declarations; this is intentionally not a
    # general JavaScript binding or scope evaluator.
    depth = {"{": 0, "[": 0, "(": 0}
    pairs = {"}": "{", "]": "[", ")": "("}
    for index, token in enumerate(tokens):
        if not any(depth.values()) and token in (
                ("ident", "const"), ("ident", "let"), ("ident", "var"),
                ("ident", "class"), ("ident", "function")):
            if index + 1 < len(tokens) and tokens[index + 1] == (
                    "ident", "defineConfig"):
                return False
        if token[0] == "punct":
            if token[1] in depth:
                depth[token[1]] += 1
            elif token[1] in pairs:
                depth[pairs[token[1]]] = max(
                    0, depth[pairs[token[1]]] - 1)
    return True


def _literal_string_array(tokens: list, opener: int, key: str
                          ) -> tuple[list[str], bool]:
    """Read one flat literal string array, allowing Vitest's known default."""
    end = _matching_token(tokens, opener, "[", "]")
    if end is None:
        return [], True
    values: list[str] = []
    dynamic = False
    index = opener + 1
    while index < end:
        token = tokens[index]
        if token[0] == "string":
            values.append(token[1])
            index += 1
            continue
        if token == ("punct", ","):
            index += 1
            continue
        if (key == "exclude" and token == ("punct", ".")
                and tokens[index:index + 5] == [
                    ("punct", "."), ("punct", "."),
                    ("punct", "."), ("ident", "configDefaults"),
                    ("punct", ".")]
                and index + 5 < end
                and tokens[index + 5] == ("ident", "exclude")):
            index += 6
            continue
        dynamic = True
        # Continue over the balanced unsupported expression so literal
        # strings following it remain visible for diagnostics only.
        if token[0] == "punct" and token[1] in ("[", "{", "("):
            closer = {"[": "]", "{": "}", "(": ")"}[token[1]]
            match = _matching_token(tokens, index, token[1], closer)
            index = end if match is None else match + 1
        else:
            index += 1
    return values, dynamic


def _exported_test_arrays(text: str, keys: tuple[str, ...]
                          ) -> tuple[dict[str, list[str]], bool]:
    """Read only the direct ``test`` object on a supported exported root."""
    literals = {key: [] for key in keys}
    tokens = _js_tokens(text)
    bounds = _exported_root_object(tokens)
    if bounds is None:
        return literals, True
    root_open, root_close = bounds
    test_blocks: list[tuple[int, int]] = []
    dynamic = False
    depth = 0
    index = root_open + 1
    while index < root_close:
        token = tokens[index]
        if depth == 0 and _is_key(token, "test"):
            if index + 1 < root_close \
                    and tokens[index + 1] == ("punct", ":"):
                opener = index + 2
                if opener < root_close \
                        and tokens[opener] == ("punct", "{"):
                    closer = _matching_token(tokens, opener, "{", "}")
                    if closer is None or closer > root_close:
                        dynamic = True
                    else:
                        test_blocks.append((opener, closer))
                        index = closer + 1
                        continue
                else:
                    dynamic = True
            else:
                dynamic = True
        # Any root-level spread can supply or replace a test block.
        if depth == 0 and token == ("punct", ".") \
                and tokens[index:index + 3] == [
                    ("punct", "."), ("punct", "."), ("punct", ".")]:
            dynamic = True
        if token == ("punct", "{"):
            depth += 1
        elif token == ("punct", "}"):
            depth -= 1
        index += 1
    if len(test_blocks) > 1:
        return literals, True
    if not test_blocks:
        return literals, dynamic
    opener, closer = test_blocks[0]
    depth = 0
    index = opener + 1
    seen_keys: set[str] = set()
    while index < closer:
        token = tokens[index]
        if depth == 0 and token == ("punct", ".") \
                and tokens[index:index + 3] == [
                    ("punct", "."), ("punct", "."), ("punct", ".")]:
            dynamic = True
        if depth == 0 and token[0] in ("ident", "string") \
                and token[1] in keys and index + 2 < closer \
                and tokens[index + 1] == ("punct", ":"):
            key = token[1]
            if key in seen_keys:
                dynamic = True
            seen_keys.add(key)
            value = tokens[index + 2]
            if value != ("punct", "["):
                dynamic = True
                index += 2
                continue
            values, is_dynamic = _literal_string_array(tokens, index + 2, key)
            literals[key].extend(values)
            dynamic |= is_dynamic
            end = _matching_token(tokens, index + 2, "[", "]")
            index = (end + 1) if end is not None else index + 3
            continue
        if token == ("punct", "{"):
            depth += 1
        elif token == ("punct", "}"):
            depth -= 1
        index += 1
    return literals, dynamic


def selected_include_patterns(root: Path, selected: str | None,
                              argv: tuple[str, ...] = ()) -> tuple | None:
    """Literal include patterns of the selected config, or None.

    None means unknown: no selection, an unreadable config, or dynamic
    (computed) values. Callers must never union patterns across configs;
    pass the single config chosen by :func:`select_vitest_config`.
    """
    _ = argv
    if selected is None:
        return None
    if not isinstance(selected, str):
        raise TypeError("selected must be str or None")
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    normalized = _normalize_ref(selected)
    if normalized is None:
        return None
    try:
        raw = read_regular(root, normalized, _CONTEXT_READ_LIMIT)
    except Exception:
        return None
    if len(raw) >= _CONTEXT_READ_LIMIT:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    patterns = parse_config_text(text)
    if patterns.dynamic:
        return None
    return patterns.includes


# --- argv profiles ------------------------------------------------------------


def effective_argv(args: tuple[str, ...],
                   full_args: tuple[str, ...]) -> tuple[str, ...]:
    """Compose the effective argv tail from runner ``args`` + ``full_args``.

    Mirrors adapter composition: the scoped profile carries ``args`` and
    the full profile carries ``args`` plus ``full_args``. Record differing
    scoped/full profiles separately; exclusion needs every reviewed
    profile (see :func:`conclusively_excluded`).
    """
    if not isinstance(args, (tuple, list)) or not isinstance(
            full_args, (tuple, list)):
        raise TypeError("args and full_args must be tuples")
    for token in list(args) + list(full_args):
        if not isinstance(token, str):
            raise TypeError("argv tokens must be str")
    return tuple(args) + tuple(full_args)


def argv_membership_status(argv: tuple[str, ...]) -> str:
    """``resolved`` when argv membership effects are understood, else partial.

    ``run`` and literal ``--config`` selection are understood. Positional
    filters and every other flag make membership unresolved because their
    suite effects cannot be established from the configuration alone.
    Unknown argv must never classify a potentially active path as
    excluded.
    """
    if not isinstance(argv, (tuple, list)):
        raise TypeError("argv must be a tuple")
    index = 0
    items = list(argv)
    while index < len(items):
        token = items[index]
        if not isinstance(token, str):
            return "partial"
        if not token.startswith("-"):
            if token == "run":
                index += 1
                continue
            return "partial"
        if token == "--config":
            if index + 1 >= len(items) \
                    or not isinstance(items[index + 1], str):
                return "partial"
            index += 2
            continue
        if token.startswith("--config="):
            index += 1
            continue
        return "partial"
    return "resolved"


def conclusively_excluded(path: str, *profiles) -> bool:
    """True only when every reviewed profile excludes ``path``.

    Each profile is a tuple of per-profile membership labels; a single
    non-``excluded`` label (unknown, partial, included) keeps the path
    out of the known-excluded inventory.
    """
    if not isinstance(path, str) or not path:
        raise TypeError("path must be a nonempty string")
    if not profiles:
        return False
    for profile in profiles:
        if not isinstance(profile, (tuple, list)) or not profile:
            return False
        for label in profile:
            if label != "excluded":
                return False
    return True


# --- bounded static collection -------------------------------------------------

_JS_REL_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]*?\s+from\s+)?|require\s*\(\s*|import\s*\(\s*)"
    r"['\"](\.[^'\"]*)['\"]")

_PY_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([A-Za-z_][\w.]*)")
_PY_FROM_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.*[A-Za-z_][\w.]*)[ \t]+import[ \t]+")
_PY_FIXTURE_DEF_RE = re.compile(r"^[ \t]*def[ \t]+([A-Za-z_]\w*)[ \t]*\(")
_PY_TEST_PARAMS_RE = re.compile(
    r"^[ \t]*def[ \t]+test_\w*[ \t]*\(([^)]*)\)")
_PYTEST_PLUGINS_RE = re.compile(r"^[ \t]*pytest_plugins[ \t]*=")
_PYTEST_CONFIG_PRECEDENCE = (
    "pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini",
    "pyproject.toml", "tox.ini", "setup.cfg")
_PYTEST_BUILTINS = frozenset({
    "tmp_path", "tmp_path_factory", "tmpdir", "capsys", "capfd", "caplog",
    "monkeypatch", "request", "pytestconfig", "record_property",
    "record_testsuite_property", "record_xml_attribute",
})

_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _pytest_addopts(text: str, config_path: str) -> tuple[str, bool]:
    """Return addopts from the active pytest config section, without eval."""
    basename = config_path.rsplit("/", 1)[-1]
    if basename in ("pyproject.toml", "pytest.toml", ".pytest.toml"):
        try:
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, TypeError):
            return "", True
        if basename == "pyproject.toml":
            tool = data.get("tool", {})
            pytest_tool = (tool.get("pytest", {})
                           if isinstance(tool, dict) else None)
            section = (pytest_tool.get("ini_options", {})
                       if isinstance(pytest_tool, dict) else None)
        else:
            section = data.get("pytest", data)
        if not isinstance(section, dict):
            return "", True
        addopts = section.get("addopts", "")
        if isinstance(addopts, list) and all(
                isinstance(token, str) for token in addopts):
            return " ".join(addopts), False
        if isinstance(addopts, str):
            return addopts, False
        return "", addopts not in (None, "")

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return "", True
    section = "tool:pytest" if basename == "setup.cfg" else "pytest"
    if not parser.has_section(section):
        return "", False
    return parser.get(section, "addopts", fallback=""), False


def _parse_pytest_addopts(addopts: str | tuple[str, ...] | list[str]
                          ) -> tuple[list[str], bool]:
    """Read literal ignore options; unsupported membership flags are partial."""
    if isinstance(addopts, str):
        try:
            tokens = shlex.split(addopts, comments=False, posix=True)
        except ValueError:
            return [], True
    elif isinstance(addopts, (tuple, list)) and all(
            isinstance(token, str) for token in addopts):
        tokens = list(addopts)
    else:
        return [], True
    found: list[str] = []
    dynamic = False
    membership_options = {
        "-k", "-m", "--ignore", "--ignore-glob", "--deselect", "-c",
        "--confcutdir", "--rootdir", "--pyargs", "--doctest-modules",
        "--doctest-glob", "--override-ini",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        option, separator, value = token.partition("=")
        if option in ("--ignore", "--ignore-glob"):
            if not separator:
                index += 1
                if index >= len(tokens) or tokens[index].startswith("-"):
                    dynamic = True
                    continue
                value = tokens[index]
            normalized = _normalize_ref(value)
            if normalized is None or _is_sensitive(normalized):
                dynamic = True
            elif normalized not in found:
                found.append(normalized)
        elif option in membership_options:
            dynamic = True
            if not separator and index + 1 < len(tokens) \
                    and not tokens[index + 1].startswith("-"):
                index += 1
        index += 1
    return found, dynamic


def parse_pytest_ignores(text: str, config_path: str = "pytest.ini") -> list[str]:
    """Literal ignores in the effective pytest ``addopts`` section only."""
    if not isinstance(text, str) or not isinstance(config_path, str):
        raise TypeError("text and config_path must be str")
    addopts, invalid = _pytest_addopts(text, config_path)
    if invalid:
        return []
    ignores, _dynamic = _parse_pytest_addopts(addopts)
    return ignores


def _js_spec_targets(from_rel: str, spec: str) -> list[str] | None:
    """Resolve a relative JS/TS import to candidate rel paths (None=escape)."""
    if "/" in from_rel:
        anchor = from_rel.rsplit("/", 1)[0]
    else:
        anchor = ""
    joined = f"{anchor}/{spec}" if anchor else spec
    normalized: list[str] = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            else:
                return None
        else:
            normalized.append(part)
    base = "/".join(normalized)
    if not base:
        return None
    for extension in _JS_EXTENSIONS:
        if base.endswith(extension):
            return [base]
    targets = [f"{base}{extension}" for extension in _JS_EXTENSIONS]
    targets.extend(f"{base}/index{extension}" for extension in _JS_EXTENSIONS)
    return targets


def _python_spec_targets(module: str, level: int,
                         from_rel: str) -> list[str]:
    """Resolve a Python import to candidate rel paths (child-relative)."""
    relative = module.replace(".", "/")
    targets: list[str] = []
    if level:
        anchor = from_rel.rpartition("/")[0]
        for _ in range(level - 1):
            anchor = anchor.rpartition("/")[0]
        stem = f"{anchor}/{relative}" if anchor else relative
        if relative:
            targets.extend([f"{stem}.py", f"{stem}/__init__.py"])
        else:
            targets.extend([f"{anchor}/__init__.py"] if anchor else [])
    else:
        if "/" in from_rel:
            anchor = from_rel.rsplit("/", 1)[0]
            targets.extend([f"{anchor}/{relative}.py",
                            f"{anchor}/{relative}/__init__.py"])
        targets.extend([f"{relative}.py", f"{relative}/__init__.py",
                        f"src/{relative}.py",
                        f"src/{relative}/__init__.py"])
    return [target for target in dict.fromkeys(targets) if target]


_ABSOLUTE_EXTERNAL_TOP = frozenset({
    "pytest", "_pytest", "os", "sys", "json", "re", "pathlib", "stat",
    "typing", "dataclasses", "collections", "functools", "itertools",
    "hashlib", "fnmatch", "time", "io", "warnings", "enum", "abc",
    "datetime", "math", "shutil", "tempfile", "unittest",
})


class _Collector:
    """Bounded traversal state shared by the collect_* entry points."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.roles: dict[str, str] = {}
        self.relations: list[tuple[str, str, str]] = []
        self.missing: list[tuple[str, str]] = []
        self._missing_seen: set[tuple[str, str]] = set()
        self.files_read = 0

    def note_missing(self, path: str, reason: str) -> None:
        """Record one bounded missing-evidence reason (deduped)."""
        if is_excluded_path(path):
            return
        key = (path, reason)
        if key in self._missing_seen:
            return
        self._missing_seen.add(key)
        if len(self.missing) < _CONTEXT_MISSING_CAP:
            self.missing.append(key)

    def note_role(self, path: str, role: str) -> None:
        """Assign a role; explicit assignments win over later generic ones."""
        if is_excluded_path(path):
            return
        if path not in self.roles:
            self.roles[path] = role

    def note_relation(self, source: str, target: str, kind: str) -> None:
        """Record one bounded relation (deduped, capped)."""
        if is_excluded_path(source) or is_excluded_path(target):
            return
        entry = (source, target, kind)
        if entry in self.relations:
            return
        if len(self.relations) < CONTEXT_MAX_FILES:
            self.relations.append(entry)

    def read(self, rel: str, reader) -> bytes | None:
        """Read ``rel`` through ``reader`` with scope/secret/budget bounds.

        Returns the bytes, or None after recording the reason. Budget
        exhaustion surfaces as a ``read-limit`` reason via MemoryError.
        Successful reads are cached on the reader for admission reuse.
        """
        if is_excluded_path(rel):
            return None
        if not _within_scope(rel, self.scope):
            self.note_missing(rel, "outside-selected-scope")
            return None
        if _is_sensitive(rel):
            return None
        if self.files_read >= CONTEXT_MAX_FILES:
            self.note_missing(rel, "read-limit")
            return None
        try:
            raw = reader(rel, _CONTEXT_READ_LIMIT)
        except FileNotFoundError:
            self.note_missing(rel, "not-collected")
            return None
        except (MemoryError, OverflowError):
            self.note_missing(rel, "read-limit")
            return None
        except Exception:
            self.note_missing(rel, "unresolved-import")
            return None
        self.files_read += 1
        if len(raw) >= _CONTEXT_READ_LIMIT:
            self.note_missing(rel, "read-limit")
            return None
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            self.note_missing(rel, "unresolved-import")
            return None
        reader_cache_put(reader, rel, raw)
        return raw


def _collect_finish(runner_kind: str, scope: str, active_roots: tuple,
                    collector: _Collector, config_paths: list,
                    known_excluded: list,
                    status: str,
                    suite_profiles: tuple[SuiteProfile, ...] = ()
                    ) -> ReviewContext:
    """Assemble a validated ReviewContext from collector state."""
    excluded: list[str] = []
    for entry in known_excluded:
        if _is_sensitive(entry):
            continue
        try:
            _check_relpath(entry, "known_excluded")
        except (TypeError, ValueError):
            continue
        if entry not in excluded and len(excluded) < _KNOWN_EXCLUDED_CAP:
            excluded.append(entry)
    roles = tuple(sorted((path, role)
                         for path, role in collector.roles.items()))
    return ReviewContext(
        runner_kind=runner_kind,
        config_paths=tuple(config_paths),
        active_roots=tuple(active_roots),
        roles=roles,
        relations=tuple(collector.relations),
        known_excluded=tuple(excluded),
        missing=tuple(collector.missing),
        config_status=status, suite_profiles=suite_profiles)


def _vitest_import_links(text: str) -> list[str]:
    """Relative JS/TS import specifiers in ``text`` (bounded per file)."""
    return _JS_REL_IMPORT_RE.findall(text)[:_CONTEXT_MAX_LINKS_PER_FILE]


def collect_vitest_context(root: Path, scope: str, argv: tuple,
                           reader, candidates: set[str],
                           seeds: tuple[str, ...] = (),
                           test_roots: tuple[str, ...] = (), *,
                           full_argv: tuple[str, ...] | None = None
                           ) -> ReviewContext:
    """Collect bounded Vitest context: config, setup, linked helpers.

    ``reader(rel, limit)`` returns bytes and raises on missing/symlink/
    oversized inputs; every read counts against the caller's own ledger.
    ``candidates`` holds known child-relative config paths; ``seeds`` are
    extra traversal roots (tests). Traversal is bounded to
    ``CONTEXT_MAX_FILES`` files, depth 3, and 16 links per file; caps and
    dynamic values degrade to explicit missing reasons, never to unioned
    or executed configuration.
    """
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not isinstance(scope, str):
        raise TypeError("scope must be str")
    if not isinstance(argv, (tuple, list)):
        raise TypeError("argv must be a tuple")
    if not isinstance(candidates, (set, frozenset, tuple, list)):
        raise TypeError("candidates must be a set of strings")
    if not isinstance(seeds, (tuple, list)):
        raise TypeError("seeds must be a tuple")
    if not isinstance(test_roots, (tuple, list)):
        raise TypeError("test_roots must be a tuple")
    if full_argv is not None and not isinstance(full_argv, (tuple, list)):
        raise TypeError("full_argv must be a tuple or None")
    collector = _Collector(scope)
    candidate_names = {rel for rel in candidates
                       if isinstance(rel, str) and "/" not in rel
                       and rel in _VITEST_CONFIG_NAMES}
    config_paths: list[str] = []
    known_excluded: list[str] = []
    work: list[tuple[str, str, int]] = []
    profiles: list[SuiteProfile] = []
    scoped_argv = tuple(argv)
    effective_full_argv = (tuple(full_argv) if full_argv is not None
                           else scoped_argv)
    for name, profile_argv in (("scoped", scoped_argv),
                               ("full", effective_full_argv)):
        values, ok = _config_flag_values(profile_argv)
        selected, selection_status = _select_from_names(
            candidate_names, values, ok, lambda _name: True)
        profile_status = selection_status
        includes: tuple[str, ...] = ()
        excludes: tuple[str, ...] = ()
        if selection_status == "partial":
            collector.note_missing("config", "ambiguous-config")
        elif selection_status == "unavailable":
            collector.note_missing("config", "not-collected")
        if selected is not None:
            if not _within_scope(selected, scope) or _is_sensitive(selected):
                collector.note_missing(selected, "outside-selected-scope")
                selected = None
                profile_status = "partial"
            else:
                raw = collector.read(selected, reader)
                if raw is None:
                    profile_status = "partial"
                else:
                    if selected not in config_paths:
                        config_paths.append(selected)
                    collector.note_role(selected, "config")
                    (include_values, exclude_values, setup_targets,
                     dynamic) = _parse_selected_config(
                         collector, selected, raw)
                    includes = tuple(include_values)
                    excludes = tuple(exclude_values)
                    for pattern in excludes:
                        if pattern not in known_excluded:
                            known_excluded.append(pattern)
                    unsupported_patterns = any(
                        _compile_glob(pattern) is None
                        for pattern in (*includes, *excludes))
                    if dynamic or unsupported_patterns:
                        collector.note_missing(selected, "dynamic-config")
                        profile_status = "partial"
                    for target in setup_targets:
                        resolved = _resolve_setup_target_file(
                            collector, reader, target)
                        if resolved is not None:
                            work.append((resolved, "setup", 1))
                        else:
                            collector.note_missing(target, "unresolved-import")
                    work.append((selected, "config", 0))
        if argv_membership_status(profile_argv) != "resolved":
            collector.note_missing("config", "unsupported-argv")
            profile_status = "partial"
        profiles.append(SuiteProfile(
            name=name, config_path=selected, status=profile_status,
            includes=includes, excludes=excludes))
    for seed in seeds:
        if isinstance(seed, str) and seed:
            work.append((seed, "test", 0))

    _follow_js_links(collector, reader, work)

    final = ("partial" if collector.missing
             or any(profile.status != "resolved" for profile in profiles)
             else "resolved")
    return _collect_finish(
        "vitest", scope, _sanitize_roots(test_roots), collector,
        config_paths, known_excluded, final, tuple(profiles))


def _parse_selected_config(collector: _Collector, selected: str,
                           raw: bytes | None
                           ) -> tuple[list[str], list[str], list[str], bool]:
    """Parse include/exclude patterns and setup targets from config bytes."""
    if raw is None:
        return [], [], [], True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        collector.note_missing(selected, "unresolved-import")
        return [], [], [], True
    patterns = parse_config_text(text)
    targets: list[str] = []
    for value in patterns.setups:
        normalized = _normalize_ref(value)
        if normalized is None:
            collector.note_missing(selected, "outside-selected-scope")
            continue
        resolved = _resolve_setup_target(selected, normalized)
        if resolved is None:
            collector.note_missing(selected, "outside-selected-scope")
            continue
        targets.append(resolved)
    return (list(patterns.includes), list(patterns.excludes), targets,
            patterns.dynamic)


def _resolve_setup_target(from_rel: str, spec: str) -> str | None:
    """Resolve a setup-file reference against its config file's directory."""
    anchor = from_rel.rpartition("/")[0]
    joined = f"{anchor}/{spec}" if anchor else spec
    normalized: list[str] = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            else:
                return None
        else:
            normalized.append(part)
    base = "/".join(normalized)
    if not base:
        return None
    return base


def _resolve_setup_target_file(collector: _Collector, reader,
                               target: str) -> str | None:
    """First readable setup target, trying exact then extensionless forms."""
    if not _within_scope(target, collector.scope) or _is_sensitive(target):
        return None
    probing = [target]
    if not any(target.endswith(extension) for extension in _JS_EXTENSIONS):
        probing.extend(f"{target}{extension}"
                       for extension in _JS_EXTENSIONS)
    for candidate in probing:
        if _try_one_target(reader, collector, candidate) is not None:
            return candidate
    return None


def _follow_js_links(collector: _Collector, reader,
                     work: list[tuple[str, str, int]]) -> None:
    """Follow bounded JS local-import links from ``work`` seeds."""
    seen: set[str] = set()
    queue = list(work)
    while queue:
        rel, role, depth = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        collector.note_role(rel, role)
        raw = _cached_read(collector, reader, rel)
        if raw is None:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        links = _vitest_import_links(text)
        if depth >= _CONTEXT_MAX_DEPTH:
            if links:
                collector.note_missing(rel, "depth-limit")
            continue
        for spec in links:
            targets = _js_spec_targets(rel, spec)
            if targets is None:
                collector.note_missing(rel, "outside-selected-scope")
                continue
            in_scope = [target for target in targets
                        if _within_scope(target, collector.scope)]
            if not in_scope:
                collector.note_missing(rel, "outside-selected-scope")
                continue
            if collector.files_read >= CONTEXT_MAX_FILES:
                collector.note_missing(rel, "read-limit")
                continue
            resolved = _read_first_target(collector, reader, in_scope)
            if resolved is None:
                collector.note_missing(rel, "unresolved-import")
                continue
            collector.note_relation(rel, resolved, "local-import")
            collector.note_role(resolved, "helper")
            queue.append((resolved, "helper", depth + 1))


def _read_first_target(collector: _Collector, reader,
                       targets: list[str]) -> str | None:
    """First readable sensitive-checked target, without recording misses."""
    for target in targets:
        if _is_sensitive(target):
            continue
        if collector.files_read >= CONTEXT_MAX_FILES:
            return None
        try:
            raw = reader(target, _CONTEXT_READ_LIMIT)
        except Exception:
            continue
        if len(raw) >= _CONTEXT_READ_LIMIT:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        collector.files_read += 1
        reader_cache_put(reader, target, raw)
        return target
    return None


def _cached_read(collector: _Collector, reader, rel: str) -> bytes | None:
    """Collector read with caller-reader cache reuse when available."""
    cached = reader_cache_get(reader, rel)
    if cached is not None:
        if not _within_scope(rel, collector.scope) or _is_sensitive(rel):
            return None
        return cached
    raw = collector.read(rel, reader)
    if raw is not None:
        reader_cache_put(reader, rel, raw)
    return raw


_READER_CACHE_ATTR = "_ptest_context_cache"


def reader_cache_get(reader, rel: str) -> bytes | None:
    """Bytes previously cached for ``rel`` on this reader, if any."""
    cache = getattr(reader, _READER_CACHE_ATTR, None)
    if not isinstance(cache, dict):
        return None
    cached = cache.get(rel)
    return cached if isinstance(cached, bytes) else None


def reader_cache_put(reader, rel: str, raw: bytes) -> None:
    """Cache ``raw`` bytes for ``rel`` on this reader (best effort)."""
    try:
        cache = getattr(reader, _READER_CACHE_ATTR, None)
    except Exception:
        return
    if cache is None:
        try:
            setattr(reader, _READER_CACHE_ATTR, {rel: raw})
        except Exception:
            pass
    elif isinstance(cache, dict) and rel not in cache:
        cache[rel] = raw


def _python_links(text: str) -> tuple[list, list[str], bool]:
    """Bounded imports and fixture params, plus whether links were omitted."""
    modules: list = []
    truncated = False
    for line in text.splitlines():
        match = _PY_IMPORT_RE.match(line) or _PY_FROM_RE.match(line)
        if match is not None:
            token = match.group(1)
            level = len(token) - len(token.lstrip("."))
            if len(modules) < _CONTEXT_MAX_LINKS_PER_FILE:
                modules.append((token.lstrip("."), level))
            else:
                truncated = True
    params: list[str] = []
    for match in _PY_TEST_PARAMS_RE.finditer(text):
        for chunk in match.group(1).split(","):
            name = chunk.strip().split(":")[0].split("=")[0].strip()
            name = name.lstrip("*")
            if name.isidentifier():
                if len(params) < _CONTEXT_MAX_LINKS_PER_FILE:
                    params.append(name)
                else:
                    truncated = True
    return modules, params, truncated


def _conftest_fixtures(text: str) -> set[str]:
    """Fixture names defined in a conftest/plugin text."""
    if "@pytest.fixture" not in text:
        return set()
    armed = False
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@pytest.fixture"):
            armed = True
            continue
        if armed:
            match = _PY_FIXTURE_DEF_RE.match(line)
            if match is not None:
                found.add(match.group(1))
                armed = False
            elif stripped and not stripped.startswith("@"):
                armed = False
    return found


def _pytest_plugins_literals(text: str) -> tuple[list[str], bool]:
    """Literal ``pytest_plugins`` entries; dynamic flag for computed values."""
    plugins: list[str] = []
    dynamic = False
    lines = text.splitlines()
    for lineno, line in enumerate(lines):
        if _PYTEST_PLUGINS_RE.match(line) is None:
            continue
        chunk = line.split("=", 1)[1]
        depth = 0
        cursor = lineno
        while True:
            for token in re.findall(
                    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[A-Za-z_]\w*",
                    chunk):
                if len(token) > 1 and token[0] in "\"'":
                    plugins.append(token[1:-1])
                elif token not in ("pytest_plugins",):
                    dynamic = True
            depth += chunk.count("[") - chunk.count("]")
            if depth <= 0 or cursor + 1 >= len(lines):
                break
            cursor += 1
            chunk = lines[cursor]
    return plugins, dynamic


def collect_pytest_context(root: Path, scope: str, reader,
                           candidates: set[str],
                           seeds: tuple[str, ...] = (),
                           test_roots: tuple[str, ...] = ()) -> ReviewContext:
    """Collect bounded pytest context: conftests, plugins, fixture links.

    ``candidates`` holds known child-relative conftest/config/plugin
    paths; ``seeds`` are test files whose fixture params and imports are
    traced. Unresolvable absolute imports of well-known stdlib/pytest
    names are external; other misses degrade to explicit reasons.
    """
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not isinstance(scope, str):
        raise TypeError("scope must be str")
    if not isinstance(candidates, (set, frozenset, tuple, list)):
        raise TypeError("candidates must be a set of strings")
    if not isinstance(seeds, (tuple, list)):
        raise TypeError("seeds must be a tuple")
    if not isinstance(test_roots, (tuple, list)):
        raise TypeError("test_roots must be a tuple")
    collector = _Collector(scope)
    config_paths: list[str] = []
    known_excluded: list[str] = []
    fixture_defs: dict[str, list[tuple[str, str, str]]] = {}
    plugin_anchors: dict[str, str] = {}
    status = "resolved"

    ordered = sorted(rel for rel in candidates if isinstance(rel, str))
    seed_paths = tuple(seed for seed in seeds
                       if isinstance(seed, str) and seed)

    def applies_to_seed(conftest: str) -> bool:
        parent = conftest.rpartition("/")[0]
        return any(not parent or seed == parent or seed.startswith(parent + "/")
                   for seed in seed_paths)

    config_names = set(_PYTEST_CONFIG_PRECEDENCE)
    root_configs = {rel for rel in ordered
                    if "/" not in rel and rel in config_names}
    nested_configs = [rel for rel in ordered
                      if "/" in rel and rel.rsplit("/", 1)[-1]
                      in config_names and applies_to_seed(rel)]
    if nested_configs:
        # A nested config can change collection for only part of this
        # sampled scope. Keep its contents out of global exclusion claims.
        collector.note_missing("config", "ambiguous-config")
        status = "partial"

    from . import executability as executability_api
    config_read_failures: list[str] = []

    def read_config_candidate(name: str, _limit: int) -> bytes | None:
        if name not in root_configs:
            return None
        raw = _cached_read(collector, reader, name)
        if raw is None or len(raw) >= _CONTEXT_READ_LIMIT:
            config_read_failures.append(name)
            return None
        return raw

    selected_config, pytest_addopts = (
        executability_api._pytest_config_with_addopts(
            root, read=read_config_candidate))
    if config_read_failures:
        # A present but unreadable higher-priority config may decide the
        # runner's collection behavior; a later config cannot replace it.
        selected_config = None
        collector.note_missing("config", "read-limit")
        status = "partial"

    def register_fixtures(text: str, source: str, anchor: str,
                          kind: str) -> None:
        scope_dir = anchor.rpartition("/")[0]
        for name in _conftest_fixtures(text):
            entry = (source, scope_dir, kind)
            entries = fixture_defs.setdefault(name, [])
            if entry not in entries:
                entries.append(entry)

    closure_roots: list[tuple[str, int, str]] = [
        (rel, 0, "fixture") for rel in ordered
        if rel.rsplit("/", 1)[-1] == "conftest.py"
        and applies_to_seed(rel)]
    for rel in ordered:
        base = rel.rsplit("/", 1)[-1]
        if base in config_names:
            if rel != selected_config:
                continue
            role = "config"
        elif base == "conftest.py":
            if not applies_to_seed(rel):
                continue
            role = "fixture"
        else:
            continue
        raw = _cached_read(collector, reader, rel)
        if raw is None:
            continue
        collector.note_role(rel, role)
        if role == "config":
            if rel not in config_paths:
                config_paths.append(rel)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                collector.note_missing(rel, "unresolved-import")
                status = "partial"
                continue
            _addopts_text, invalid = _pytest_addopts(text, rel)
            ignores, dynamic = _parse_pytest_addopts(pytest_addopts)
            if invalid or dynamic:
                collector.note_missing(rel, "dynamic-config")
                status = "partial"
            for ignore in ignores:
                if ignore not in known_excluded:
                    known_excluded.append(ignore)
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        register_fixtures(text, rel, rel, "conftest")
        plugins, dynamic = _pytest_plugins_literals(text)
        if dynamic:
            collector.note_missing(rel, "dynamic-config")
            status = "partial"
        for plugin in plugins:
            for target in _python_spec_targets(plugin, 0, rel):
                resolved = _try_one_target(reader, collector, target)
                if resolved is not None:
                    collector.note_relation(rel, resolved, "local-import")
                    collector.note_role(resolved, "fixture")
                    plugin_anchors[resolved] = rel
                    closure_roots.append((resolved, 0, "fixture"))
                    break
            else:
                collector.note_missing(rel, "unresolved-import")

    queue: list[tuple[str, int, str]] = closure_roots + [
        (seed, 0, "test") for seed in seed_paths]
    seen: set[str] = set()
    while queue:
        rel, depth, role = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        raw = _cached_read(collector, reader, rel)
        if raw is None:
            continue
        collector.note_role(rel, role)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        modules, params, truncated = _python_links(text)
        if truncated:
            collector.note_missing(rel, "item-limit")
        plugin_anchor = plugin_anchors.get(rel)
        if plugin_anchor is not None:
            register_fixtures(text, rel, plugin_anchor, "plugin")
        for name in params if role == "test" else ():
            if name in _PYTEST_BUILTINS or name in ("self", "cls"):
                continue
            owner = _find_fixture_owner(
                collector, name, rel, fixture_defs)
            if owner is not None:
                collector.note_relation(rel, owner, "fixture-use")
            else:
                collector.note_missing(rel, "unresolved-import")
        if depth >= _CONTEXT_MAX_DEPTH:
            if modules:
                collector.note_missing(rel, "depth-limit")
            continue
        for module, level in modules:
            top = module.split(".")[0] if module else ""
            if not module:
                anchor = rel.rpartition("/")[0]
                targets = _python_spec_targets("", level, rel) \
                    if anchor else []
            else:
                targets = _python_spec_targets(module, level, rel)
            resolved = None
            for target in targets:
                resolved = _try_one_target(reader, collector, target)
                if resolved is not None:
                    break
            if resolved is None:
                if top in _ABSOLUTE_EXTERNAL_TOP or (
                        not level and not module):
                    continue
                collector.note_missing(rel, "unresolved-import")
                continue
            collector.note_relation(rel, resolved, "local-import")
            child_role = "fixture" if role == "fixture" else "helper"
            collector.note_role(resolved, child_role)
            if depth + 1 > _CONTEXT_MAX_DEPTH:
                collector.note_missing(resolved, "depth-limit")
            else:
                queue.append((resolved, depth + 1, child_role))

    if status == "resolved" and collector.missing:
        status = "partial"
    if not config_paths and not collector.roles:
        status = "unavailable"
    return _collect_finish("pytest", scope, _sanitize_roots(test_roots),
                           collector, config_paths, known_excluded, status)


def _try_one_target(reader, collector: _Collector,
                    target: str) -> str | None:
    """Readably probe one link target without recording misses."""
    if not _within_scope(target, collector.scope):
        return None
    if _is_sensitive(target):
        return None
    cached = reader_cache_get(reader, target)
    if cached is not None:
        return target
    if collector.files_read >= CONTEXT_MAX_FILES:
        return None
    try:
        raw = reader(target, _CONTEXT_READ_LIMIT)
    except Exception:
        return None
    if len(raw) >= _CONTEXT_READ_LIMIT:
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    collector.files_read += 1
    reader_cache_put(reader, target, raw)
    return target


def _find_fixture_owner(collector: _Collector, name: str, seed: str,
                        fixture_defs: dict[str, list[tuple[str, str, str]]]
                        ) -> str | None:
    """Resolve only a nearest applicable conftest or activated local plugin."""
    entries = fixture_defs.get(name, ())
    directory = seed.rpartition("/")[0]
    while True:
        scoped = [entry for entry in entries if entry[1] == directory]
        direct = sorted({source for source, _scope, kind in scoped
                         if kind == "conftest"})
        if direct:
            return direct[0] if len(direct) == 1 else None
        plugins = sorted({source for source, _scope, kind in scoped
                          if kind == "plugin"})
        if plugins:
            return plugins[0] if len(plugins) == 1 else None
        if not directory:
            return None
        directory = directory.rpartition("/")[0]


__all__ = [
    "CONTEXT_MAX_FILES",
    "ReviewContext",
    "SuitePatterns",
    "empty_context",
    "replace",
    "context_body",
    "select_vitest_config",
    "selected_include_patterns",
    "parse_config_text",
    "parse_pytest_ignores",
    "effective_argv",
    "argv_membership_status",
    "conclusively_excluded",
    "collect_vitest_context",
    "collect_pytest_context",
    "reader_cache_get",
    "reader_cache_put",
]
