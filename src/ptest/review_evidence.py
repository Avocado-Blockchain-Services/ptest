"""Pure suite-aware ranking and contiguous source-unit selection.

The collector owns filesystem boundaries and source hashes. This module only
selects complete units from an already admitted immutable packet; it never
opens files, executes project code, or synthesizes lines.
"""
from __future__ import annotations

import ast
import fnmatch
import functools
import hashlib
import io
import posixpath
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass

from .checklist import CATALOG

MAX_UNIT_BYTES = 64 * 1024
MAX_UNIT_LINES = MAX_UNIT_BYTES
MAX_FUNCTION_LINES = 1024
MAX_UNITS_PER_ITEM = 64
MAX_INITIAL_UNITS = 48
MAX_INITIAL_FILES = 20
MAX_INITIAL_BYTES = 192 * 1024
MAX_RESERVE_FILES = 4
MAX_RESERVE_BYTES = 64 * 1024
MAX_RESERVE_UNITS = 64

_CONFIG_BASENAMES = frozenset({
    ".ptest.toml", "pyproject.toml", "pytest.ini", ".pytest.ini",
    "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg",
    "package.json", "vitest.config.ts", "vitest.config.js",
    "vite.config.ts", "vite.config.js", "jest.config.ts", "jest.config.js",
})
_TEST_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:tests?|__tests__)(?:/|$)|"
    r"(?:^|/)test_[^/]*\.py$|[^/]*_test\.py$|\.test\.|\.spec\.")
_GENERIC_CONTROL_RE = re.compile(
    r"(?i)(?:finally\s*:|yield\b|teardown|cleanup|\.close\s*\(|"
    r"\.dispose\s*\(|\.kill\s*\(|\.wait\s*\(|\.join\s*\(|"
    r"\.terminate\s*\(|shutdown|cancel|disconnect|rollback|release)")
_TIME_CONTROL_RE = re.compile(
    r"(?i)(?:threading\s*\.\s*Timer|\bTimer\s*\(|"
    r"threading\s*\.\s*(?:Event|Barrier|Condition)|"
    r"\b(?:Event|Barrier|Condition)\s*\(|asyncio\s*\.\s*(?:Event|wait_for)|"
    r"call_later\s*\(|\b(?:monotonic|time|sleep)\s*\()")
_TIME_SPECIFIC_RE = re.compile(
    r"(?i)(?:threading\s*\.\s*(?:Timer|Event|Barrier|Condition)"
    r"|\b(?:Timer|Event|Barrier|Condition)\s*\(|"
    r"asyncio\s*\.\s*(?:Event|wait_for)\s*\(|call_later\s*\(|"
    r"\b(?:monotonic|freeze_time|freezegun)\b)")
_PROCESS_BOUNDARY_RE = re.compile(
    r"(?i)(?:\btimeout\s*=|\bexcept\b|finally\s*:|\.cancel\s*\(|"
    r"\.terminate\s*\(|\.kill\s*\(|\.wait\s*\(|\.communicate\s*\(|"
    r"process_group|start_new_session|setsid)")
_DB_QUALIFIED_CALL_RE = re.compile(
    r"(?i)(?:sqlite3?|aiosqlite|psycopg\w*|asyncpg|sqlalchemy)\.connect$|"
    r"(?:connection|conn|session|engine|database|db)\.execute$|"
    r"(?:create_engine|create_all|sessionmaker)$")
_WRITE_METHODS = frozenset({
    "mkdir", "write_text", "write_bytes", "touch", "open", "unlink",
    "rmdir", "rename", "replace",
})


@dataclass(frozen=True, slots=True)
class SourceUnit:
    """A contiguous original line range from one admitted source excerpt."""

    path: str
    start_line: int
    end_line: int
    source_sha256: str
    text: str
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise TypeError("source unit path must be nonempty str")
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (self.start_line, self.end_line)):
            raise TypeError("source unit lines must be integers")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("source unit line span is invalid")
        if not isinstance(self.source_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("source unit source hash is invalid")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("source unit text must be nonempty")
        if self.role not in {"config", "setup", "fixture", "helper",
                             "test", "source"}:
            raise ValueError("source unit role is invalid")
        if len(self.text.encode("utf-8")) > MAX_UNIT_BYTES:
            raise ValueError("source unit exceeds its byte bound")
        if self.end_line - self.start_line + 1 > MAX_UNIT_LINES:
            raise ValueError("source unit exceeds its line bound")


def source_id(packet_sha256: str, item_id: str, unit: SourceUnit) -> str:
    """Create an opaque ID bound to packet, item, source identity and span."""
    if not isinstance(packet_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", packet_sha256):
        raise ValueError("packet hash is invalid")
    if not isinstance(item_id, str) or not item_id:
        raise TypeError("item id must be nonempty str")
    if not isinstance(unit, SourceUnit):
        raise TypeError("unit must be SourceUnit")
    parts = (packet_sha256, item_id, unit.path, unit.source_sha256,
             str(unit.start_line), str(unit.end_line))
    return "src-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


@functools.lru_cache(maxsize=256)
def _compiled_patterns(text_patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    patterns = []
    for value in text_patterns:
        try:
            patterns.append(re.compile(value, re.IGNORECASE))
        except re.error:
            continue
    return tuple(patterns)


def _resource_patterns(entry) -> tuple[re.Pattern, ...]:
    return _compiled_patterns(tuple(entry.text_patterns))


def _matches_path(path: str, entry) -> tuple[bool, bool]:
    specific = generic = False
    for pattern in entry.path_patterns:
        try:
            matched = re.search(pattern, path) is not None
        except re.error:
            matched = False
        if matched:
            if pattern in {r"(?i)(?:^|/)(?:tests?|__tests__)(?:/|$)",
                           r"(?i)(?:^|/)test_[^/]*\.py$|[^/]*_test\.py$|\.test\.|\.spec\.",
                           r"(?:^|/)src(?:/|$)"}:
                generic = True
            else:
                specific = True
    return specific, generic


@dataclass(frozen=True, slots=True)
class _ContextIndex:
    """Per-context lookups precomputed once per ranking call."""

    roles: Mapping[str, str]
    outgoing: frozenset[str]
    incoming: frozenset[str]
    config_paths: frozenset[str]


def _build_context_index(context) -> _ContextIndex:
    return _ContextIndex(
        roles=dict(getattr(context, "roles", ())),
        outgoing=frozenset(
            source for source, _target, _kind in getattr(
                context, "relations", ())),
        incoming=frozenset(
            target for _source, target, _kind in getattr(
                context, "relations", ())),
        config_paths=frozenset(getattr(context, "config_paths", ())),
    )


def _context_index(context, signal_cache: dict | None) -> _ContextIndex:
    """Memoize one index per context object under the signal cache."""
    if signal_cache is None:
        return _build_context_index(context)
    key = ("context-index", id(context))
    stored = signal_cache.get(key)
    if stored is not None:
        previous, index = stored
        if previous is context:
            return index
    index = _build_context_index(context)
    signal_cache[key] = (context, index)
    return index


def _relation_score(path: str, context, *,
                    index: _ContextIndex | None = None) -> int:
    resolved = index if index is not None else _build_context_index(context)
    score = 0
    if path in resolved.outgoing:
        score += 2
    if path in resolved.incoming:
        score += 3
    role = resolved.roles.get(path)
    if role in {"config", "setup", "fixture"}:
        score += 5
    elif role == "helper":
        score += 4
    elif role == "test":
        score += 3
    return score


def _executable_text(path: str, text: str) -> str:
    """Blank Python strings/comments in place so source syntax stays intact."""
    if not path.endswith(".py"):
        return text
    try:
        offsets = [0]
        for line in text.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        masked = list(text)
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type not in {tokenize.STRING, tokenize.COMMENT}:
                continue
            start = offsets[token.start[0] - 1] + token.start[1]
            end = offsets[token.end[0] - 1] + token.end[1]
            for index in range(start, min(end, len(masked))):
                if masked[index] not in "\r\n":
                    masked[index] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    return "".join(masked)


_IDENTIFIER_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_+")


def _python_identifier_text(text: str) -> str:
    """Normalize executable Python identifiers for semantic word matching."""
    try:
        names = [token.string for token in tokenize.generate_tokens(
            io.StringIO(text).readline) if token.type == tokenize.NAME]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    return _IDENTIFIER_SPLIT_RE.sub(" ", " ".join(names))


_EXACT_LOOP_MARKERS = ("\\A", "\\Z", "(?<", "(?=", "(?!")

_CALL_HIT_PREFILTERS: dict = {}


def _needs_exact_loop(pattern) -> bool:
    source = pattern.pattern
    if not isinstance(source, str):
        return True
    return any(marker in source for marker in _EXACT_LOOP_MARKERS)


def _prefilter_for(pattern) -> re.Pattern:
    """One MULTILINE prefilter search per pattern, cached per pattern."""
    variant = _CALL_HIT_PREFILTERS.get(pattern)
    if variant is None:
        variant = re.compile(pattern.pattern, pattern.flags | re.MULTILINE)
        _CALL_HIT_PREFILTERS[pattern] = variant
    return variant


def _call_names_hit(patterns, call_names: tuple[str, ...]) -> bool:
    """Match the old per-name double loop with one search per pattern."""
    patterns = tuple(patterns)
    call_names = tuple(call_names)
    if not patterns or not call_names:
        return False
    joined = "\n".join(f"{name}\n{name}(" for name in call_names)
    for pattern in patterns:
        if not _needs_exact_loop(pattern):
            if _prefilter_for(pattern).search(joined) is None:
                continue
        if any(pattern.search(name) or pattern.search(name + "(")
               for name in call_names):
            return True
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


_PROCESS_LAUNCH_CALLS = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "system",
    "execv", "execve", "execl", "execlp", "spawn", "spawnv",
})


def _safe_candidate_path(value: str, candidates: set[str]) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return None
    normalized = posixpath.normpath(value)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return normalized if normalized in candidates else None


def static_local_invocation_targets(path: str, text: str,
                                    candidates) -> tuple[str, ...]:
    """Resolve literal local script operands without evaluating project code.

    Python recognition is limited to common subprocess/exec calls and
    ``Path(__file__)`` parent/division expressions. Shell recognition only
    follows a literal ``dirname "$0"`` directory binding into a literal
    interpreter operand. Every edge must resolve to the frozen candidate set.
    """
    if not isinstance(path, str) or not isinstance(text, str):
        raise TypeError("path and text must be strings")
    candidate_set = {item for item in candidates if isinstance(item, str)}
    found: set[str] = set()
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if tree is not None:
            parents = {child: parent for parent in ast.walk(tree)
                       for child in ast.iter_child_nodes(parent)}

            def scope_of(node):
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef,
                                           ast.AsyncFunctionDef)):
                        return parent
                    parent = parents.get(parent)
                return None

            def evaluate(node, aliases):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value, False
                if isinstance(node, ast.Name):
                    if node.id == "__file__":
                        return path, True
                    return aliases.get(node.id)
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                    left = evaluate(node.left, aliases)
                    right = evaluate(node.right, aliases)
                    if left is None or right is None or not left[1]:
                        return None
                    suffix = right[0]
                    if suffix.startswith("/"):
                        return None
                    return posixpath.normpath(
                        posixpath.join(left[0], suffix)), True
                if isinstance(node, ast.Attribute):
                    base = evaluate(node.value, aliases)
                    if base is None or not base[1]:
                        return None
                    if node.attr == "parent":
                        return posixpath.dirname(base[0]), True
                    return None
                if isinstance(node, ast.Subscript):
                    parent = node.value
                    if (not isinstance(parent, ast.Attribute)
                            or parent.attr != "parents"):
                        return None
                    base = evaluate(parent.value, aliases)
                    try:
                        index = ast.literal_eval(node.slice)
                    except (ValueError, TypeError):
                        return None
                    if (base is None or not base[1]
                            or isinstance(index, bool)
                            or not isinstance(index, int)
                            or not 0 <= index < 8):
                        return None
                    result = base[0]
                    for _ in range(index + 1):
                        result = posixpath.dirname(result)
                    return result, True
                if isinstance(node, ast.Call):
                    name = _call_name(node.func)
                    if name in {"Path", "PurePath", "PurePosixPath", "str",
                                "os.fspath"} \
                            and len(node.args) == 1:
                        return evaluate(node.args[0], aliases)
                    if (isinstance(node.func, ast.Attribute)
                            and node.func.attr in {
                                "resolve", "absolute", "as_posix"}
                            and not node.args and not node.keywords):
                        value = evaluate(node.func.value, aliases)
                        return value if value is not None and value[1] else None
                    if name in {"os.path.dirname", "path.dirname"} \
                            and len(node.args) == 1:
                        value = evaluate(node.args[0], aliases)
                        if value is not None and value[1]:
                            return posixpath.dirname(value[0]), True
                return None

            def assignments_for(scope):
                nodes = (tree.body if scope is None else
                         [node for node in ast.walk(scope)
                          if node is not scope and scope_of(node) is scope])
                return sorted([node for node in nodes
                               if isinstance(node, (ast.Assign,
                                                    ast.AnnAssign))],
                              key=lambda node: node.lineno)

            def aliases_for(scope, parent_aliases=None):
                aliases = dict(parent_aliases or {})
                ambiguous: set[str] = set()
                assignments = assignments_for(scope)
                for _ in range(3):
                    for node in assignments:
                        value = evaluate(node.value, aliases)
                        if value is None:
                            continue
                        targets = (node.targets if isinstance(node, ast.Assign)
                                   else (node.target,))
                        for target in targets:
                            if not isinstance(target, ast.Name):
                                continue
                            existing = aliases.get(target.id)
                            if existing is not None and existing != value:
                                ambiguous.add(target.id)
                                aliases.pop(target.id, None)
                            elif target.id not in ambiguous:
                                aliases[target.id] = value
                return aliases

            module_aliases = aliases_for(None)
            function_aliases = {
                node: aliases_for(node, module_aliases)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func).rsplit(".", 1)[-1]
                if name not in _PROCESS_LAUNCH_CALLS or not node.args:
                    continue
                command = node.args[0]
                operands = (command.elts if isinstance(command, (ast.List,
                                                                  ast.Tuple))
                            else (command,))
                scope = scope_of(node)
                aliases = function_aliases.get(scope, module_aliases)
                for operand in operands:
                    value = evaluate(operand, aliases)
                    if value is None or not value[1]:
                        continue
                    raw, _is_path = value
                    match = _safe_candidate_path(raw, candidate_set)
                    if match is not None:
                        found.add(match)
    elif path.endswith(".sh"):
        source_dir = posixpath.dirname(path)
        directory_vars = set()
        for line in text.splitlines():
            match = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", line)
            if match is None:
                continue
            name, value = match.groups()
            simple_self_dir = re.fullmatch(
                r'''\$\(\s*dirname\s+(?:--\s+)?["']?\$0["']?\s*\)''',
                value)
            canonical_self_dir = (
                value.strip("\"'").startswith("$(")
                and "$0" in value and "dirname" in value
                and re.search(r"\bcd\s+(?:--\s+)?", value) is not None
                and re.search(r"&&\s*pwd", value) is not None
                and re.search(r"dirname(?:\s+--)?\s+[\"']?\$0", value)
                is not None)
            if simple_self_dir or canonical_self_dir:
                directory_vars.add(name)
        for name in directory_vars:
            variable = rf"(?:\$\{{{re.escape(name)}\}}|\${re.escape(name)})/([^\s\"';]+)"
            for line in text.splitlines():
                command = line.strip()
                if not command or command.startswith("#"):
                    continue
                executable = re.match(
                    r"(?:exec\s+)?(?:python(?:\d+(?:\.\d+)?)?|bash|sh|node|ruby|perl)\s+",
                    command)
                direct_exec = re.match(r"(?:exec\s+)?[\"']?\$", command)
                if executable is None and direct_exec is None:
                    continue
                suffixes = re.findall(variable, command)
                for suffix in suffixes:
                    match = _safe_candidate_path(
                        posixpath.join(source_dir, suffix), candidate_set)
                    if match is not None:
                        found.add(match)
    return tuple(sorted(found))


def _item_signal(path: str, text: str, entry,
                 cache: dict | None = None) -> tuple[bool, bool, bool, bool]:
    """Return concrete-call, body-hit, cleanup and criterion-specific signals."""
    key = ("item-signal", path, id(entry), text)
    if cache is not None and key in cache:
        return cache[key]
    source_key = ("source-analysis", path, text)
    source = cache.get(source_key) if cache is not None else None
    if source is None and cache is not None:
        owner = cache.get(("python-owner-summary-text", path, text))
        if owner is not None:
            source = (owner["code"], owner["searchable"],
                      owner["call_names"], None)
    if source is None:
        code = _executable_text(path, text)
        searchable = code
        call_names = []
        tree = None
        if path.endswith(".py"):
            searchable += "\n" + _python_identifier_text(code)
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None
            if tree is not None:
                call_names = [_call_name(node.func)
                              for node in ast.walk(tree)
                              if isinstance(node, ast.Call)]
        source = (code, searchable, tuple(call_names), tree)
        if cache is not None:
            cache[source_key] = source
    code, searchable, call_names, _tree = source
    patterns = _resource_patterns(entry)
    body_hit = any(pattern.search(searchable) for pattern in patterns)
    call_hit = False
    criterion_control = (entry.id == "TIME-001"
                         and _TIME_SPECIFIC_RE.search(code) is not None)
    if path.endswith(".py"):
        call_hit = _call_names_hit(patterns, tuple(call_names))
        if entry.id == "TIME-001":
            criterion_control = criterion_control or any(
                _TIME_SPECIFIC_RE.search(name + "(")
                for name in call_names)
    if entry.id == "TIME-001":
        control = _TIME_CONTROL_RE.search(code) is not None
    else:
        control = _GENERIC_CONTROL_RE.search(code) is not None
    result = (call_hit, body_hit, control, criterion_control)
    if cache is not None:
        cache[key] = result
    return result


def _strong_item_operation(path: str, text: str, entry,
                           signal_cache: dict | None = None) -> bool:
    """Recognize concrete operations that disambiguate catalog word hits."""
    if entry is None or entry.id not in {
            "DB-001", "DB-002", "CACHE-001", "PROCESS-001",
            "FIX-001", "RESOURCE-001"}:
        return False
    key = ("strong-item-operation", path, id(entry), text)
    if signal_cache is not None and key in signal_cache:
        return signal_cache[key]
    result = False
    if path.endswith(".py"):
        owner = (signal_cache or {}).get(
            ("python-owner-summary-text", path, text))
        if owner is not None:
            aliases = (signal_cache or {}).get(
                ("python-import-aliases", path, owner["source_text"]))
            if aliases is None:
                aliases = _python_import_aliases(
                    path, owner["source_text"], owner["tree"], signal_cache)
            result = _strong_owner_operation(
                path, entry, owner, aliases)
            if signal_cache is not None:
                signal_cache[key] = result
            return result
        analysis = (signal_cache or {}).get(("source-analysis", path, text))
        if analysis is None:
            code = _executable_text(path, text)
            searchable = code + "\n" + _python_identifier_text(code)
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                tree = None
            call_names = (() if tree is None else tuple(
                _call_name(node.func) for node in ast.walk(tree)
                if isinstance(node, ast.Call)))
            analysis = (code, searchable, call_names, tree)
            if signal_cache is not None:
                signal_cache[("source-analysis", path, text)] = analysis
        code, _searchable, call_names, tree = analysis
        imported_aliases = (_python_import_aliases(
            path, text, tree, signal_cache) if tree is not None else {})
        if entry.id in {"DB-001", "DB-002"}:
            for name in call_names:
                normalized = _python_identifier_text(name).lower()
                if (_DB_QUALIFIED_CALL_RE.search(name)
                        or ("." not in name and re.search(
                            r"\b(?:databases?|db)\b", normalized))):
                    result = True
                    break
        elif entry.id == "CACHE-001":
            result = any(
                "." not in name and re.search(
                    r"\bcache\b", _python_identifier_text(name).lower())
                for name in call_names)
        elif entry.id == "PROCESS-001":
            launches = False
            for name in call_names:
                prefix, dot, suffix = name.partition(".")
                resolved = (imported_aliases.get(name, "") if not dot else
                            imported_aliases.get(prefix, "") + "." + suffix
                            if prefix in imported_aliases else name)
                if re.fullmatch(
                        r"(?:subprocess\.(?:run|Popen|call|check_call|"
                        r"check_output)|os\.(?:system|exec\w+|spawn\w*)|"
                        r"multiprocessing\.(?:Process|Pool)|"
                        r"asyncio\.create_subprocess_(?:exec|shell))", resolved):
                    launches = True
                    break
            result = launches and _PROCESS_BOUNDARY_RE.search(code) is not None
        elif entry.id == "FIX-001":
            for name in call_names:
                prefix, dot, suffix = name.rpartition(".")
                base = prefix if dot and suffix in {
                    "create", "build", "create_batch", "build_batch"} else name
                if "factory" in _python_identifier_text(base).lower():
                    result = True
                    break
        elif entry.id == "RESOURCE-001" and tree is not None:
            resource_key = ("python-parent-relative-write", path, text)
            parent_write = (signal_cache.get(resource_key)
                            if signal_cache is not None else None)
            if parent_write is None:
                parent_write = _resource_parent_write(tree)
                if signal_cache is not None:
                    signal_cache[resource_key] = parent_write
            result = parent_write
    if signal_cache is not None:
        signal_cache[key] = result
    return result


def _python_import_aliases(source, text, tree, signal_cache):
    key = ("python-import-aliases", source, text)
    if signal_cache is not None and key in signal_cache:
        return signal_cache[key]
    aliases = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
    if signal_cache is not None:
        signal_cache[key] = aliases
    return aliases


def _resource_parent_write(node):
    parent_relative: set[str] = set()
    for value in ast.walk(node):
        if not isinstance(value, (ast.Assign, ast.AnnAssign)):
            continue
        expression = value.value
        if not isinstance(expression, ast.BinOp) or not isinstance(
                expression.op, ast.Div):
            continue
        has_parent = any(
            isinstance(part, ast.Attribute) and part.attr == "parent"
            for part in ast.walk(expression.left))
        literal_sibling = isinstance(expression.right, ast.Constant) \
            and isinstance(expression.right.value, str)
        if not (has_parent and literal_sibling):
            continue
        targets = value.targets if isinstance(value, ast.Assign) \
            else (value.target,)
        parent_relative.update(target.id for target in targets
                               if isinstance(target, ast.Name))
    if not parent_relative:
        return False
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in parent_relative
        and call.func.attr in _WRITE_METHODS
        for call in ast.walk(node))


def _strong_owner_operation(source, entry, owner, imported_aliases):
    """Evaluate one cached AST owner summary without reparsing its body."""
    if entry is None:
        return False
    if entry.id in {"DB-001", "DB-002"}:
        for name, direct_name in owner["calls"]:
            normalized = _python_identifier_text(name).lower()
            if (_DB_QUALIFIED_CALL_RE.search(name)
                    or (direct_name is not None and re.search(
                        r"\b(?:databases?|db)\b", normalized))):
                return True
    elif entry.id == "CACHE-001":
        return any(direct_name is not None and re.search(
            r"\bcache\b", _python_identifier_text(direct_name).lower())
                   for _name, direct_name in owner["calls"])
    elif entry.id == "PROCESS-001":
        for name, direct_name in owner["calls"]:
            resolved = name
            if direct_name is not None:
                resolved = imported_aliases.get(direct_name, "")
            else:
                prefix, dot, suffix = name.partition(".")
                if dot and prefix in imported_aliases:
                    resolved = imported_aliases[prefix] + "." + suffix
            if re.fullmatch(
                    r"(?:subprocess\.(?:run|Popen|call|check_call|"
                    r"check_output)|os\.(?:system|exec\w+|spawn\w*)|"
                    r"multiprocessing\.(?:Process|Pool)|"
                    r"asyncio\.create_subprocess_(?:exec|shell))", resolved):
                return _PROCESS_BOUNDARY_RE.search(owner["code"]) is not None
    elif entry.id == "FIX-001":
        for name, direct_name in owner["calls"]:
            if direct_name is not None and "factory" in (
                    _python_identifier_text(direct_name).lower()):
                return True
            prefix, dot, _suffix = name.partition(".")
            if dot and "factory" in _python_identifier_text(
                    prefix).lower():
                return True
    elif entry.id == "RESOURCE-001":
        return owner["parent_write"]
    return False


def _python_owner_summary(source, node, parent, tree, lines, signal_cache):
    start = min([node.lineno, *(item.lineno
                                for item in node.decorator_list)])
    end = node.end_lineno or node.lineno
    source_text = "".join(lines)
    key = ("python-owner-summary", source, source_text, start, end)
    cached = signal_cache.get(key) if signal_cache is not None else None
    if cached is not None:
        return cached
    body = _line_text(lines, start, end)
    code = _executable_text(source, body)
    searchable = code + "\n" + _python_identifier_text(code)
    call_nodes = tuple(value for value in ast.walk(node)
                       if isinstance(value, ast.Call))
    calls = []
    local_calls = set()
    method_calls = set()
    for call in call_nodes:
        name = _call_name(call.func)
        direct_name = call.func.id if isinstance(call.func, ast.Name) else None
        calls.append((name, direct_name))
        if direct_name is not None:
            local_calls.add(direct_name)
        elif (isinstance(parent, ast.ClassDef)
              and isinstance(call.func, ast.Attribute)
              and isinstance(call.func.value, ast.Name)
              and call.func.value.id in {"self", "cls"}):
            method_calls.add(call.func.attr)
    summary = {
        "start": start,
        "end": end,
        "body": body,
        "code": code,
        "searchable": searchable,
        "call_names": tuple(name for name, _direct in calls),
        "calls": tuple(calls),
        "local_calls": frozenset(local_calls),
        "method_calls": frozenset(method_calls),
        "assertion": bool(re.search(r"(?m)^\s*assert\b", body)),
        "parent_write": _resource_parent_write(node),
        "tree": tree,
        "source_text": source_text,
    }
    if signal_cache is not None:
        signal_cache[key] = summary
        signal_cache[("python-owner-summary-text", source, body)] = summary
    return summary


def _python_owner_index(source, text, tree, signal_cache):
    """Cache item-independent top-level owner and parent indexes per source."""
    key = ("python-owner-index", source, text)
    cached = signal_cache.get(key) if signal_cache is not None else None
    if cached is not None:
        return cached
    parents = {}
    module_owners: dict[str, list[ast.AST]] = {}
    class_owners: dict[ast.ClassDef, dict[str, list[ast.AST]]] = {}
    tests = []
    owners = []
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for owner in tree.body:
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_owners.setdefault(owner.name, []).append(owner)
            owners.append((owner, tree))
        elif isinstance(owner, ast.ClassDef):
            members: dict[str, list[ast.AST]] = {}
            class_owners[owner] = members
            for member in owner.body:
                if isinstance(member, (ast.FunctionDef,
                                       ast.AsyncFunctionDef)):
                    members.setdefault(member.name, []).append(member)
                    owners.append((member, owner))
    for node, parent in owners:
        if node.name.startswith("test_"):
            tests.append((node, parent))
    index = {
        "parents": parents,
        "module_owners": module_owners,
        "class_owners": class_owners,
        "owners": tuple(owners),
        "tests": tuple(tests),
    }
    if signal_cache is not None:
        signal_cache[key] = index
    return index


_ROOT_MANIFEST_NAMES = frozenset({
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
})


def _is_config_path(path: str, context=None, *,
                    index: _ContextIndex | None = None) -> bool:
    """Recognize deciding config and child-root manifests only."""
    if index is not None:
        if (path in index.config_paths
                or index.roles.get(path) == "config"):
            return True
    elif (path in getattr(context, "config_paths", ())
            or dict(getattr(context, "roles", ())).get(path) == "config"):
        return True
    declaration = getattr(context, "declaration", ".")
    prefix = "" if declaration == "." else declaration.rstrip("/") + "/"
    return (path in ({prefix + ".ptest.toml"}
                     | {prefix + name for name in _ROOT_MANIFEST_NAMES})
            or (path.startswith(prefix) and "/" not in path[len(prefix):]
                and fnmatch.fnmatchcase(
                    path[len(prefix):], "requirements*.txt")))


def _item_rank(path: str, text: str, entry, context,
               signal_cache: dict | None = None, *,
               index: _ContextIndex | None = None) -> tuple:
    call_hit, body_hit, generic_control, criterion_control = _item_signal(
        path, text, entry, signal_cache)
    path_specific, path_generic = _matches_path(path, entry)
    resolved = index if index is not None else _context_index(
        context, signal_cache)
    context_score = _relation_score(path, context, index=resolved)
    role = _source_role(path, context, index=resolved)
    caller = role == "test" and _TEST_PATH_RE.search(path) is not None
    injected_network = (entry is not None
                        and entry.id == "NETWORK-001"
                        and role == "test"
                        and path.endswith((".ts", ".tsx", ".js", ".jsx",
                                           ".mjs"))
                        and _js_network_injected_request_owner(
                            path, text, entry, role, signal_cache))
    direct = call_hit or body_hit or criterion_control or path_specific
    strong = _strong_item_operation(path, text, entry, signal_cache)
    # Generic cleanup can only order already relevant candidates. A finally
    # block in an unrelated helper never makes it an item candidate.
    return (not _is_config_path(path, context, index=resolved),
            not strong,
            not injected_network,
            not criterion_control,
            not call_hit, not body_hit, not path_specific, not caller,
            -context_score if direct else 0,
            not generic_control if direct else True,
            not path_generic, path)


def _has_item_anchor(path: str, text: str, entry, context,
                     signal_cache: dict | None = None, *,
                     index: _ContextIndex | None = None) -> bool:
    call_hit, body_hit, _control, criterion_control = _item_signal(
        path, text, entry, signal_cache)
    path_specific, _path_generic = _matches_path(path, entry)
    resolved = index if index is not None else _context_index(
        context, signal_cache)
    return (call_hit or body_hit or criterion_control or path_specific
            or (entry is not None and entry.id == "NETWORK-001"
                and path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs"))
                and _js_network_injected_request_owner(
                    path, text, entry,
                    _source_role(path, context, index=resolved),
                    signal_cache))
            or _strong_item_operation(path, text, entry, signal_cache))


def _has_related_fixture_anchor(path: str, texts: dict, entry, context,
                                signal_cache=None) -> bool:
    """An active test is relevant when it uses this item's fixture evidence."""
    for source, target, kind in context.relations:
        if source != path or kind != "fixture-use":
            continue
        text = texts.get(target)
        if text is not None and _has_item_anchor(
                target, text, entry, context, signal_cache):
            return True
    return False


def _with_declaration_prefix(context, declaration: str):
    """Align child-relative context paths with packet excerpt paths."""
    if declaration in ("", "."):
        return context
    prefix = declaration.rstrip("/") + "/"
    roles = tuple((path if path.startswith(prefix) else prefix + path, role)
                  for path, role in context.roles)
    relations = tuple(
        (source if source.startswith(prefix) else prefix + source,
         target if target.startswith(prefix) else prefix + target, kind)
        for source, target, kind in context.relations)
    # Ranking and source-role resolution only consume these two fields.
    # Keeping the original object untouched preserves its child-relative
    # contract and avoids changing public citation paths.
    missing = tuple(
        (path if path in {"config", "config-status"}
         or path.startswith(prefix) else prefix + path, reason)
        for path, reason in context.missing)
    return _PathContext(roles=roles, relations=relations,
                        missing=missing,
                        config_paths=tuple(
                            path if path.startswith(prefix) else prefix + path
                            for path in context.config_paths),
                        known_excluded=context.known_excluded,
                        declaration=declaration)


@dataclass(frozen=True, slots=True)
class _PathContext:
    roles: tuple
    relations: tuple
    missing: tuple = ()
    config_paths: tuple = ()
    known_excluded: tuple = ()
    declaration: str = "."


def _active_paths(paths, context) -> list[str]:
    """Drop only test paths the suite collector conclusively excluded."""
    roles = dict(getattr(context, "roles", ()))
    excluded = {path for path, reason in getattr(context, "missing", ())
                if reason == "excluded-suite" and roles.get(path) == "test"}
    return [path for path in paths if path not in excluded]


def _unit_priority(unit: SourceUnit, item_id: str,
                   signal_cache: dict | None = None) -> tuple:
    """Put complete mechanisms and callers ahead of import-only units."""
    text = unit.text
    if unit.role == "config":
        return (0, unit.start_line)
    entry = next((item for item in CATALOG if item.id == item_id), None)
    call_hit, body_hit, control, criterion_control = (
        _item_signal(unit.path, text, entry, signal_cache)
        if entry else (False, False, False, False))
    strong = _strong_item_operation(unit.path, text, entry, signal_cache)
    assertion = bool(re.search(r"(?m)^\s*assert\b", text))
    function = bool(re.search(
        r"(?m)^\s*(?:async\s+)?def\s+|^\s*class\s+", text))
    imported = bool(re.match(r"\s*(?:from\s+\S+\s+import|import\s+)", text))
    if strong or criterion_control:
        return (0, unit.start_line)
    if call_hit:
        return (1, unit.start_line)
    if body_hit:
        return (2, unit.start_line)
    if assertion:
        return (3, unit.start_line)
    if control:
        return (4, unit.start_line)
    if function:
        return (5, unit.start_line)
    if imported:
        return (7, unit.start_line)
    return (6, unit.start_line)


def rank_candidates(context, candidates, texts, catalog, *,
                    declaration: str = ".",
                    signal_cache: dict | None = None) -> tuple[str, ...]:
    """Rank candidate paths with cross-item round-robin representation.

    Every non-deterministic checklist row gets one path opportunity before
    any row receives its second. Context relations and concrete resource
    operations outrank generic directory matches; paths break remaining ties.
    """
    if not isinstance(texts, dict):
        raise TypeError("texts must be a dict of path to text")
    if not isinstance(declaration, str) or not declaration:
        raise TypeError("declaration must be a nonempty string")
    if signal_cache is None:
        signal_cache = {}
    context = _with_declaration_prefix(context, declaration)
    index = _context_index(context, signal_cache)
    paths = sorted({path for path in candidates if isinstance(path, str)
                    and isinstance(texts.get(path), str)})
    paths = _active_paths(paths, context)
    from .deterministic_items import DETERMINISTIC_ITEM_IDS
    rows: list[list[str]] = []
    for item in catalog:
        if item.id in DETERMINISTIC_ITEM_IDS:
            continue
        ranked = sorted(paths, key=lambda path: _item_rank(
            path, texts[path], item, context, signal_cache, index=index))
        rows.append(ranked)
    result: list[str] = []
    seen: set[str] = set()
    cursors = [0] * len(rows)
    while True:
        advanced = False
        for index, ranked in enumerate(rows):
            while cursors[index] < len(ranked) \
                    and ranked[cursors[index]] in seen:
                cursors[index] += 1
            if cursors[index] < len(ranked):
                path = ranked[cursors[index]]
                cursors[index] += 1
                seen.add(path)
                result.append(path)
                advanced = True
        if not advanced:
            break
    # Include paths that have no checklist-specific match as stable tail.
    result.extend(path for path in paths if path not in seen)
    return tuple(result)


def rank_item_candidates(context, candidates, texts, item_id: str, *,
                         declaration: str = ".",
                         signal_cache: dict | None = None) -> tuple[str, ...]:
    """Rank one item's candidates by executable item-specific evidence."""
    if not isinstance(texts, dict):
        raise TypeError("texts must be a dict of path to text")
    if not isinstance(declaration, str) or not declaration:
        raise TypeError("declaration must be a nonempty string")
    entry = next((item for item in CATALOG if item.id == item_id), None)
    if entry is None:
        raise ValueError("unknown checklist item")
    if signal_cache is None:
        signal_cache = {}
    context = _with_declaration_prefix(context, declaration)
    index = _context_index(context, signal_cache)
    paths = [path for path in candidates
             if isinstance(path, str) and isinstance(texts.get(path), str)]
    paths = _active_paths(paths, context)
    return tuple(sorted(set(paths), key=lambda path: _item_rank(
        path, texts[path], entry, context, signal_cache, index=index)))


def _python_test_owners(source, text, entry, tree, signal_cache=None):
    """Rank active test owners once for both units and import dependencies."""
    cache_key = ("python-test-owners", source, id(entry), text)
    if signal_cache is not None and cache_key in signal_cache:
        return signal_cache[cache_key]
    source_key = ("source-analysis", source, text)
    if signal_cache is not None and source_key not in signal_cache:
        code = _executable_text(source, text)
        searchable = code + "\n" + _python_identifier_text(code)
        calls = tuple(_call_name(node.func) for node in ast.walk(tree)
                      if isinstance(node, ast.Call))
        signal_cache[source_key] = (code, searchable, calls, tree)
    lines = text.splitlines(keepends=True)
    index = _python_owner_index(source, text, tree, signal_cache)
    parents = index["parents"]
    module_owners = index["module_owners"]
    class_owners = index["class_owners"]
    imported_aliases = _python_import_aliases(
        source, text, tree, signal_cache)
    patterns = _resource_patterns(entry)
    values = []
    for node, parent in index["tests"]:
        if parent is not tree and not isinstance(parent, ast.ClassDef):
            continue
        summary = _python_owner_summary(
            source, node, parent, tree, lines, signal_cache)
        body = summary["body"]
        executable = summary["code"]
        searchable = summary["searchable"]
        call_names = summary["call_names"]
        body_hit = any(pattern.search(searchable) for pattern in patterns)
        call_hit = any(pattern.search(name) or pattern.search(name + "(")
                       for name in call_names for pattern in patterns)
        criterion = (entry.id == "TIME-001"
                     and (_TIME_SPECIFIC_RE.search(executable) is not None
                          or any(_TIME_SPECIFIC_RE.search(name + "(")
                                 for name in call_names)))
        process_boundary = (entry.id == "PROCESS-001"
                            and _PROCESS_BOUNDARY_RE.search(executable)
                            is not None)
        strong_operation = _strong_owner_operation(
            source, entry, summary, imported_aliases)
        local_calls = summary["local_calls"]
        owner_class = parents.get(node)
        helper_names = set(local_calls)
        helper_names.update(summary["method_calls"])
        for name in helper_names:
            owners = (class_owners.get(owner_class, {}).get(name, ())
                      if isinstance(owner_class, ast.ClassDef)
                      else module_owners.get(name, ()))
            if len(owners) != 1 or owners[0] is node:
                continue
            owner = owners[0]
            owner_summary = _python_owner_summary(
                source, owner, owner_class if isinstance(owner_class, ast.ClassDef)
                else tree, tree, lines, signal_cache)
            owner_code = owner_summary["code"]
            owner_searchable = owner_summary["searchable"]
            owner_calls = owner_summary["call_names"]
            body_hit = body_hit or any(
                pattern.search(owner_searchable) for pattern in patterns)
            call_hit = call_hit or any(
                pattern.search(call_name)
                or pattern.search(call_name + "(")
                for call_name in owner_calls for pattern in patterns)
            criterion = criterion or (entry.id == "TIME-001"
                and (_TIME_SPECIFIC_RE.search(owner_code) is not None
                     or any(_TIME_SPECIFIC_RE.search(call_name + "(")
                            for call_name in owner_calls)))
            process_boundary = process_boundary or (
                entry.id == "PROCESS-001"
                and _PROCESS_BOUNDARY_RE.search(owner_code) is not None)
            strong_operation = strong_operation or _strong_owner_operation(
                source, entry, owner_summary, imported_aliases)
        assertion = bool(re.search(r"(?m)^\s*assert\b", body))
        values.append((not strong_operation,
                       not criterion,
                       not (entry.id == "PROCESS-001" and call_hit
                            and process_boundary),
                       not call_hit, not body_hit,
                       not assertion, summary["start"], node,
                       bool(call_hit or body_hit or criterion
                            or (entry.id == "PROCESS-001"
                                and process_boundary)
                            or strong_operation)))
    values.sort(key=lambda value: value[:6])
    result = tuple(value[7] for value in values if value[8])
    if signal_cache is not None:
        signal_cache[cache_key] = result
    return result


def _python_used_imports(source, text, entry, cache, RC, signal_cache, *,
                         owner_names=()):
    """Return imports used by one item-relevant caller body only.

    A module-level import can be used by an unrelated test or helper. Treating
    every load in the module as a dependency makes one caller inherit the
    entire file's import closure and routinely exceeds the packet budget.
    """
    owner_key = tuple(sorted(set(owner_names)))
    key = ("python-used-imports", source, entry.id, owner_key)
    if cache is not None and key in cache:
        return cache[key]
    try:
        analysis = (signal_cache or {}).get(
            ("source-analysis", source, text))
        tree = analysis[3] if analysis is not None else ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        tree = None
    if tree is None:
        result = ()
    else:
        if owner_key:
            selected = tuple(
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in owner_key)
        else:
            callers = _python_test_owners(source, text, entry, tree,
                                          signal_cache)
            selected = callers[:1]
        if not selected:
            result = ()
        else:
            owner_facts = []
            for owner in selected:
                loaded: set[str] = set()
                bound: set[str] = set()
                assigned: set[str] = set()
                import_bindings: dict[str, int] = {}
                direct_imports: list[ast.AST] = []

                class ScopeNames(ast.NodeVisitor):
                    """Collect bindings in this function, excluding nested scopes."""
                    def visit_Name(self, node):
                        if isinstance(node.ctx, ast.Load):
                            loaded.add(node.id)
                        elif isinstance(node.ctx, (ast.Store, ast.Del)):
                            bound.add(node.id)
                            assigned.add(node.id)

                    def visit_arg(self, node):
                        bound.add(node.arg)
                        assigned.add(node.arg)

                    def visit_Import(self, node):
                        for alias in node.names:
                            binding = alias.asname or alias.name.split(".", 1)[0]
                            bound.add(binding)
                            import_bindings[binding] = (
                                import_bindings.get(binding, 0) + 1)

                    def visit_ImportFrom(self, node):
                        for alias in node.names:
                            if alias.name != "*":
                                binding = alias.asname or alias.name
                                bound.add(binding)
                                import_bindings[binding] = (
                                    import_bindings.get(binding, 0) + 1)

                    def visit_FunctionDef(self, node):
                        if node is not owner:
                            bound.add(node.name)
                            assigned.add(node.name)
                            return
                        self.generic_visit(node)

                    visit_AsyncFunctionDef = visit_FunctionDef

                    def visit_Lambda(self, node):
                        return

                    def visit_ClassDef(self, node):
                        bound.add(node.name)
                        assigned.add(node.name)

                ScopeNames().visit(owner)
                # A direct local import is a valid source edge. Imports nested
                # under control flow remain unresolved, though their binding
                # still shadows a module-level import in this function.
                direct_imports.extend(
                    node for node in owner.body
                    if isinstance(node, (ast.Import, ast.ImportFrom)))
                # Function-local imports shadow an outer binding, while a
                # separate selected owner may still use the module binding.
                owner_facts.append((loaded, bound, assigned, import_bindings,
                                    direct_imports))
            result_values = []
            max_links = RC._CONTEXT_MAX_LINKS_PER_FILE

            def append_import(row):
                if row not in result_values and len(result_values) < max_links:
                    result_values.append(row)

            module_import_counts: dict[str, int] = {}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    bindings = (alias.asname or alias.name
                                for alias in node.names
                                if alias.name != "*")
                elif isinstance(node, ast.Import):
                    bindings = (alias.asname or alias.name.split(".", 1)[0]
                                for alias in node.names)
                else:
                    continue
                for binding in bindings:
                    module_import_counts[binding] = (
                        module_import_counts.get(binding, 0) + 1)

            for node in tree.body:
                if len(result_values) >= max_links:
                    break
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if isinstance(node, ast.ImportFrom):
                    module, level = node.module or "", node.level
                    used_rows = []
                    for alias in node.names:
                        binding = alias.asname or alias.name
                        if alias.name == "*":
                            continue
                        eligible = (module_import_counts.get(binding) == 1
                                    and any(
                                        binding in owner_loaded
                                        and binding not in owner_bound
                                        for (owner_loaded, owner_bound,
                                             _assigned, _import_bindings,
                                             _local_imports) in owner_facts))
                        if not eligible:
                            continue
                        used_rows.append(alias.name)
                    used = tuple(dict.fromkeys(used_rows))
                    if used:
                        append_import((module, level, used))
                else:
                    for alias in node.names:
                        module = alias.name
                        binding = alias.asname or module.split(".", 1)[0]
                        if (module_import_counts.get(binding) == 1
                                and any(binding in owner_loaded
                               and binding not in owner_bound
                               for (owner_loaded, owner_bound, _assigned,
                                    _import_bindings, _local_imports)
                               in owner_facts)):
                            append_import((module, 0, ()))
            # A literal import directly in the selected function is an exact
            # local dependency too. It can replace a shadowed module binding.
            for (loaded, _bound, assigned, import_bindings, direct_imports) \
                    in owner_facts:
                if len(result_values) >= max_links:
                    break
                for node in direct_imports:
                    if len(result_values) >= max_links:
                        break
                    if isinstance(node, ast.ImportFrom):
                        module, level = node.module or "", node.level
                        used = tuple(dict.fromkeys(
                            alias.name for alias in node.names
                            if alias.name != "*"
                            and (alias.asname or alias.name) in loaded
                            and import_bindings.get(
                                alias.asname or alias.name) == 1
                            and (alias.asname or alias.name) not in assigned))
                        if used:
                            append_import((module, level, used))
                    else:
                        for alias in node.names:
                            if len(result_values) >= max_links:
                                break
                            module = alias.name
                            binding = alias.asname or module.split(".", 1)[0]
                            if (binding in loaded and binding not in assigned
                                    and import_bindings.get(binding) == 1):
                                append_import((module, 0, ()))
            result = tuple(dict.fromkeys(result_values))
    if cache is not None:
        cache[key] = result
    return result


def _python_requested_fixture_members(source, text, entry, signal_cache):
    """Return fixture arguments and their directly called members."""
    analysis = (signal_cache or {}).get(("source-analysis", source, text))
    try:
        tree = analysis[3] if analysis is not None else ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    callers = _python_test_owners(source, text, entry, tree, signal_cache)
    if not callers:
        return {}
    owner = callers[0]
    arguments = (*owner.args.posonlyargs, *owner.args.args,
                 *owner.args.kwonlyargs)
    fixture_names = {argument.arg for argument in arguments}
    members = {name: set() for name in fixture_names}
    for node in ast.walk(owner):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in fixture_names):
            members[node.func.value.id].add(node.func.attr)
    return members


def _python_export_targets(init_path, symbol, texts, candidates, prefix,
                           RC, support_cache):
    """Resolve one named package export, scanning for matches not prefixes."""
    key = ("python-export-targets", init_path, symbol)
    if support_cache is not None and key in support_cache:
        return support_cache[key]
    init_text = texts.get(init_path)
    if not isinstance(init_text, str):
        return ()
    tree_key = ("python-export-tree", init_path)
    tree = (support_cache.get(tree_key)
            if support_cache is not None else None)
    if tree is None:
        try:
            tree = ast.parse(init_text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if support_cache is not None and tree is not None:
            support_cache[tree_key] = tree
    if tree is None:
        return ()
    child_init = (init_path[len(prefix):]
                  if prefix and init_path.startswith(prefix) else init_path)
    found = []
    matched = 0
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                exported = alias.asname or alias.name
                if exported != symbol:
                    continue
                if statement.module:
                    specs = RC._python_spec_targets(
                        statement.module, statement.level, child_init)
                else:
                    specs = RC._python_spec_targets(
                        alias.name, statement.level, child_init)
                imported = alias.name
                for spec in specs:
                    target = prefix + spec
                    if target in candidates:
                        found.append((target, imported))
                        matched += 1
                        if matched >= RC._CONTEXT_MAX_LINKS_PER_FILE:
                            break
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                exported = alias.asname or alias.name.split(".", 1)[0]
                if exported != symbol:
                    continue
                for spec in RC._python_spec_targets(
                        alias.name, 0, child_init):
                    target = prefix + spec
                    if target in candidates:
                        found.append((target, ""))
                        matched += 1
                        if matched >= RC._CONTEXT_MAX_LINKS_PER_FILE:
                            break
        if matched >= RC._CONTEXT_MAX_LINKS_PER_FILE:
            break
    package_dir = posixpath.dirname(init_path)
    sibling = symbol.replace(".", "/")
    for target in (f"{package_dir}/{sibling}.py",
                   f"{package_dir}/{sibling}/__init__.py"):
        if target in candidates:
            found.append((target, symbol))
    result = tuple(dict.fromkeys(found))[:RC._CONTEXT_MAX_LINKS_PER_FILE]
    if support_cache is not None:
        support_cache[key] = result
    return result


def _python_selected_base_targets(source, text, symbols, texts, candidates,
                                  prefix, RC, support_cache):
    """Follow only imported bases of the selected top-level class owners."""
    symbol_key = tuple(sorted(symbols))
    key = ("python-selected-base-targets", source, symbol_key)
    if support_cache is not None and key in support_cache:
        return support_cache[key]
    if not symbol_key:
        return ()
    tree_key = ("python-source-tree", source)
    tree = (support_cache.get(tree_key)
            if support_cache is not None else None)
    if tree is None:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        if support_cache is not None and tree is not None:
            support_cache[tree_key] = tree
    if tree is None:
        return ()
    imports = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                imports[alias.asname or alias.name] = (
                    statement.module or "", statement.level, alias.name)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name, 0, "")
    child_source = (source[len(prefix):]
                    if prefix and source.startswith(prefix) else source)
    found = []
    for owner in tree.body:
        if not isinstance(owner, ast.ClassDef) or owner.name not in symbol_key:
            continue
        for base in owner.bases:
            root = base
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not isinstance(root, ast.Name):
                continue
            imported = imports.get(root.id)
            if imported is None:
                continue
            module, level, original = imported
            for spec in RC._python_spec_targets(module, level, child_source):
                target = prefix + spec
                if target not in candidates:
                    continue
                if target.endswith("/__init__.py") and original:
                    found.extend(_python_export_targets(
                        target, original, texts,
                        candidates=candidates, prefix=prefix, RC=RC,
                        support_cache=support_cache))
                else:
                    found.append((target, original))
    result = tuple(dict.fromkeys(found))[:RC._CONTEXT_MAX_LINKS_PER_FILE]
    if support_cache is not None:
        support_cache[key] = result
    return result


def _js_object_properties(tokens, start, end):
    """Read direct literal object properties, preserving ambiguous repeats."""
    values = {}
    index = start + 1
    while index < end:
        if tokens[index] == ("punct", ","):
            index += 1
            continue
        key = tokens[index]
        if (key[0] not in {"ident", "string"} or index + 1 >= end
                or tokens[index + 1] != ("punct", ":")):
            return None
        value_start = index + 2
        cursor = value_start
        stack = []
        pairs = {"{": "}", "[": "]", "(": ")"}
        while cursor < end:
            token = tokens[cursor]
            if token[0] == "punct":
                if token[1] in pairs:
                    stack.append(pairs[token[1]])
                elif stack and token[1] == stack[-1]:
                    stack.pop()
                elif token[1] == "," and not stack:
                    break
            cursor += 1
        if stack or value_start == cursor:
            return None
        values.setdefault(key[1], []).append((value_start, cursor))
        index = cursor
    return values


def _js_literal_alias_value(tokens, start, end):
    value = tokens[start:end]
    if len(value) == 1 and value[0][0] == "string":
        return value[0][1]
    expected = [
        ("ident", "fileURLToPath"), ("punct", "("),
        ("ident", "new"), ("ident", "URL"), ("punct", "("),
    ]
    if (len(value) == 14 and value[:5] == expected
            and value[5][0] == "string"
            and value[6:13] == [
                ("punct", ","), ("ident", "import"), ("punct", "."),
                ("ident", "meta"), ("punct", "."), ("ident", "url"),
                ("punct", ")")]
            and value[13] == ("punct", ")")):
        return value[5][1]
    return None


def _js_shadowed_binding(tokens, binding):
    """Conservatively reject a runtime import name redeclared in the file."""
    declarations = {"const", "let", "var", "class", "function",
                    "interface", "type", "enum"}
    for index, token in enumerate(tokens[:-1]):
        if token is None or token[0] != "ident":
            continue
        if token[1] in declarations:
            following = tokens[index + 1]
            if following == ("ident", binding):
                return True
        if token == ("ident", binding) and tokens[index + 1] == (
                "punct", "="):
            return True
    return False


def _configured_js_aliases(texts, context, RC, support_cache):
    key = ("configured-js-aliases", tuple(context.config_paths),
           getattr(context, "declaration", "."))
    if support_cache is not None and key in support_cache:
        return support_cache[key]
    rows: dict[str, set[str]] = {}
    for path in context.config_paths:
        text = texts.get(path)
        if not isinstance(text, str):
            continue
        tokens = RC._js_tokens(text)
        root = RC._exported_root_object(tokens)
        if root is None:
            continue
        root_start, root_end = root
        root_props = _js_object_properties(tokens, root_start, root_end)
        if root_props is None:
            continue
        resolve_values = root_props.get("resolve", ())
        if len(resolve_values) != 1:
            continue
        resolve_start, resolve_end = resolve_values[0]
        if (resolve_start >= len(tokens)
                or tokens[resolve_start] != ("punct", "{")):
            continue
        resolve_close = RC._matching_token(tokens, resolve_start, "{", "}")
        if resolve_close is None or resolve_end != resolve_close + 1:
            continue
        resolve_props = _js_object_properties(
            tokens, resolve_start, resolve_close)
        if resolve_props is None:
            continue
        alias_values = resolve_props.get("alias", ())
        if len(alias_values) != 1:
            continue
        alias_start, alias_end = alias_values[0]
        if alias_start >= len(tokens) or tokens[alias_start] != (
                "punct", "{"):
            continue
        alias_close = RC._matching_token(tokens, alias_start, "{", "}")
        if alias_close is None or alias_end != alias_close + 1:
            continue
        alias_props = _js_object_properties(tokens, alias_start, alias_close)
        if alias_props is None:
            continue
        imports = _js_import_bindings(text, RC, support_cache, path)
        trusted_constructors = {
            binding for specifier, bindings in imports
            if specifier in {"url", "node:url"}
            for binding in bindings
        }
        if not {"fileURLToPath", "URL"}.issubset(trusted_constructors):
            continue
        runtime_tokens = list(tokens)
        for index, token in enumerate(tokens[:-1]):
            if token == ("ident", "import"):
                end = index + 1
                while end < len(tokens):
                    if tokens[end] == ("ident", "from"):
                        end = min(end + 2, len(tokens))
                        if end < len(tokens) and tokens[end] == ("punct", ";"):
                            end += 1
                        break
                    end += 1
                runtime_tokens[index:end] = [None] * (end - index)
        if any(_js_shadowed_binding(runtime_tokens, name)
               for name in ("fileURLToPath", "URL")):
            continue
        for name, expressions in alias_props.items():
            if len(expressions) != 1:
                continue
            start, end = expressions[0]
            value = _js_literal_alias_value(tokens, start, end)
            if (isinstance(value, str) and value.startswith(".")
                    and not value.startswith("../")
                    and "\\" not in value):
                rows.setdefault(name, set()).add((path, value))
    aliases = {}
    for name, values in rows.items():
        if len(values) == 1:
            aliases[name] = next(iter(values))
    if support_cache is not None:
        support_cache[key] = aliases
    return aliases


def _js_import_bindings(text, RC, support_cache=None, source=None):
    """Return bounded static runtime import bindings and their uses."""
    token_key = ("js-source-tokens", source) if source is not None else None
    tokens = (support_cache.get(token_key)
              if support_cache is not None and token_key is not None else None)
    if tokens is None:
        tokens = RC._js_tokens(text)
        if support_cache is not None and token_key is not None:
            support_cache[token_key] = tokens
    import_key = ("js-import-bindings", source) if source is not None else None
    cached = (support_cache.get(import_key)
              if support_cache is not None and import_key is not None else None)
    if cached is not None:
        return cached
    pending = []
    for index, token in enumerate(tokens[:-1]):
        if token != ("ident", "import"):
            continue
        if tokens[index + 1] in (("punct", "("), ("punct", "."),
                                  ("ident", "type")):
            continue
        end = index + 1
        depth = {"{": 0, "[": 0, "(": 0}
        pairs = {"}": "{", "]": "[", ")": "("}
        from_at = None
        spec_at = None
        stop_at = min(len(tokens), index + 512)
        while end < stop_at:
            current = tokens[end]
            if current == ("ident", "from") and not any(depth.values()):
                if end + 1 < len(tokens) and tokens[end + 1][0] == "string":
                    from_at, spec_at = end, end + 1
                    end = spec_at + 1
                break
            if current[0] == "punct":
                if current[1] == ";" and not any(depth.values()):
                    break
                if current[1] in depth:
                    depth[current[1]] += 1
                elif current[1] in pairs:
                    depth[pairs[current[1]]] = max(
                        0, depth[pairs[current[1]]] - 1)
            end += 1
        if from_at is None or spec_at is None:
            continue
        bindings = []
        segment = tokens[index + 1:from_at]
        brace = next((position for position, value in enumerate(segment)
                      if value == ("punct", "{")), None)
        star = next((position for position, value in enumerate(segment)
                     if value == ("punct", "*")), None)
        if star is not None and star + 2 < len(segment) and segment[star + 1] == (
                "ident", "as"):
            bindings.append(segment[star + 2][1])
        if brace is not None:
            close = next((position for position in range(brace + 1,
                                                         len(segment))
                          if segment[position] == ("punct", "}")), None)
            if close is not None:
                members = segment[brace + 1:close]
                for group in _js_split_members(members):
                    if not group or group[0] == ("ident", "type"):
                        continue
                    alias_at = next((i for i, value in enumerate(group)
                                     if value == ("ident", "as")), None)
                    if alias_at is not None and alias_at + 1 < len(group):
                        bindings.append(group[alias_at + 1][1])
                    elif group[0][0] == "ident":
                        bindings.append(group[0][1])
        prefix_end = brace if brace is not None else (
            star if star is not None else len(segment))
        for value in segment[:prefix_end]:
            if value[0] == "ident" and value[1] not in {"type", "as"}:
                bindings.append(value[1])
                break
        declaration_end = spec_at + 1
        if declaration_end < len(tokens) and tokens[declaration_end] == (
                "punct", ";"):
            declaration_end += 1
        if bindings:
            pending.append((tokens[spec_at][1], tuple(dict.fromkeys(bindings)),
                            index, declaration_end))
    runtime = list(tokens)
    for _specifier, _bindings, start, end in pending:
        runtime[start:end] = [None] * (end - start)
    binding_set = {binding for _specifier, bindings, _start, _end in pending
                   for binding in bindings}
    used_bindings = _js_runtime_bindings_used(runtime, binding_set)
    shadowed_bindings = _js_shadowed_bindings(runtime, binding_set)
    imports = [(specifier, tuple(binding for binding in bindings
                                 if binding in used_bindings
                                 and binding not in shadowed_bindings))
               for specifier, bindings, _start, _end in pending]
    imports = [(specifier, used) for specifier, used in imports if used]
    result = tuple(imports)
    if support_cache is not None and import_key is not None:
        support_cache[import_key] = result
    return result


def _js_split_members(tokens):
    groups = []
    current = []
    depth = 0
    for token in tokens:
        if token == ("punct", ",") and depth == 0:
            groups.append(current)
            current = []
            continue
        if token[0] == "punct" and token[1] in "{[(":
            depth += 1
        elif token[0] == "punct" and token[1] in "}])":
            depth = max(0, depth - 1)
        current.append(token)
    if current:
        groups.append(current)
    return groups


def _js_runtime_bindings_used(tokens, bindings):
    """Scan runtime use sites once, even for modules with many imports."""
    used = set()
    for index, token in enumerate(tokens):
        if token is None or token[0] != "ident" or token[1] not in bindings:
            continue
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if previous == ("ident", "new") or following == ("punct", "("):
            used.add(token[1])
            continue
        if following == ("punct", "."):
            member = index + 3
            if (member < len(tokens) and tokens[index + 2] is not None
                    and tokens[index + 2][0] == "ident"
                    and tokens[member] == ("punct", "(")):
                used.add(token[1])
                continue
        if previous == ("ident", "extends") or previous == ("punct", "<"):
            used.add(token[1])
            continue
        if (previous == ("punct", "/") and index > 1
                and tokens[index - 2] == ("punct", "<")):
            used.add(token[1])
    return used


def _js_shadowed_bindings(tokens, bindings):
    declarations = {"const", "let", "var", "class", "function",
                    "interface", "type", "enum"}
    shadowed = set()
    for index, token in enumerate(tokens[:-1]):
        if token is None or token[0] != "ident":
            continue
        following = tokens[index + 1]
        if (token[1] in declarations and following is not None
                and following[0] == "ident"
                and following[1] in bindings):
            shadowed.add(following[1])
        if (token[1] in bindings and following == ("punct", "=")):
            shadowed.add(token[1])
    return shadowed


def _js_matching_delimiter(tokens, start, opening="{", closing="}"):
    if start >= len(tokens) or tokens[start] != ("punct", opening):
        return None
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token == ("punct", opening):
            depth += 1
        elif token == ("punct", closing):
            depth -= 1
            if depth == 0:
                return index
    return None


def _js_test_callback_ranges(tokens):
    """Find bounded direct and literal-table JS test callbacks."""
    ranges = []
    for index, token in enumerate(tokens[:-1]):
        if (token != ("ident", "it") and token != ("ident", "test")
                and token != ("ident", "specify")):
            continue
        if tokens[index + 1] == ("punct", "("):
            call_open = index + 1
        elif (index + 3 < len(tokens)
              and tokens[index + 1] == ("punct", ".")
              and tokens[index + 2] == ("ident", "each")
              and tokens[index + 3] == ("punct", "(")):
            table_close = _js_matching_delimiter(tokens, index + 3, "(", ")")
            if table_close is None or table_close + 1 >= len(tokens) \
                    or tokens[table_close + 1] != ("punct", "("):
                continue
            table = tokens[index + 4:table_close]
            if not table or table[0] != ("punct", "["):
                continue
            array_close = _js_matching_delimiter(
                tokens, index + 4, "[", "]")
            if array_close is None or array_close >= table_close:
                continue
            literal_table = tokens[index + 4:array_close + 1]
            suffix = tokens[array_close + 1:table_close]
            if any(value[0] not in {"string"}
                   and value not in {("punct", ","), ("ident", "as"),
                                     ("ident", "const")}
                   for value in literal_table[1:-1] + suffix):
                continue
            call_open = table_close + 1
        else:
            continue
        stop = min(len(tokens), call_open + 768)
        arrow = next((position for position in range(call_open + 1, stop - 1)
                      if tokens[position:position + 2]
                      == [("punct", "="), ("punct", ">")]), None)
        if arrow is None:
            continue
        body = next((position for position in range(arrow + 2, stop)
                     if tokens[position] == ("punct", "{")), None)
        if body is None:
            continue
        end = _js_matching_delimiter(tokens, body)
        if end is not None:
            params = set()
            if arrow > call_open + 1 and tokens[arrow - 1] == ("punct", ")"):
                depth = 0
                opening = None
                for position in range(arrow - 1, call_open, -1):
                    if tokens[position] == ("punct", ")"):
                        depth += 1
                    elif tokens[position] == ("punct", "("):
                        depth -= 1
                        if depth == 0:
                            opening = position
                            break
                if opening is not None:
                    params.update(value[1] for value in tokens[opening + 1:arrow - 1]
                                  if value is not None and value[0] == "ident"
                                  and value[1] not in {"async", "const", "let"})
            elif arrow > call_open + 1 and tokens[arrow - 1][0] == "ident":
                params.add(tokens[arrow - 1][1])
            ranges.append((body + 1, end, frozenset(params)))
    return tuple(ranges)


def _js_property_object(tokens, start, end, name):
    for index in range(start, end - 2):
        if (tokens[index] in (("ident", name), ("string", name))
                and tokens[index + 1] == ("punct", ":")
                and tokens[index + 2] == ("punct", "{")):
            close = _js_matching_delimiter(tokens, index + 2)
            if close is not None and close < end:
                return index + 2, close
    return None


def _js_request_property_binding(tokens, start, end):
    for index in range(start, end):
        if tokens[index] not in (("ident", "request"),
                                  ("string", "request")):
            continue
        if index + 1 < end and tokens[index + 1] == ("punct", ":"):
            value = index + 2
            if value < end and tokens[value][0] == "ident":
                return tokens[value][1]
        elif index + 1 >= end or tokens[index + 1] in (
                ("punct", ","), ("punct", "}")):
            return "request"
    return None


def _js_network_injected_request_owner(path, text, entry, role,
                                      signal_cache=None):
    """Recognize one test that exercises an injected runtime request path."""
    if (entry is None or entry.id != "NETWORK-001" or role != "test"
            or _TEST_PATH_RE.search(path) is None
            or not path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs"))):
        return False
    key = ("js-network-injected-request", path, text)
    if signal_cache is not None and key in signal_cache:
        return signal_cache[key]
    from . import review_context as RC

    imports = _js_import_bindings(text, RC, signal_cache, path)
    imported = {name for _specifier, names in imports for name in names}
    tokens = (signal_cache.get(("js-source-tokens", path))
              if signal_cache is not None else None)
    if tokens is None:
        tokens = RC._js_tokens(text)
        if signal_cache is not None:
            signal_cache[("js-source-tokens", path)] = tokens
    def block_depth(start, position):
        depth = 0
        for token in tokens[start:position]:
            if token == ("punct", "{"):
                depth += 1
            elif token == ("punct", "}"):
                depth = max(0, depth - 1)
        return depth

    injected = False
    for start, end, parameters in _js_test_callback_ranges(tokens):
        for index in range(start, end - 4):
            if (tokens[index] != ("ident", "new")
                    or tokens[index + 1][0] != "ident"
                    or tokens[index + 1][1] not in imported
                    or tokens[index + 2] != ("punct", "(")
                    or tokens[index + 3] != ("punct", "{")):
                continue
            client_name = tokens[index + 1][1]
            if client_name in parameters:
                continue
            client_close = _js_matching_delimiter(tokens, index + 3)
            if client_close is None or client_close >= end:
                continue
            transport = _js_property_object(
                tokens, index + 3, client_close, "transport")
            if transport is None:
                continue
            request_name = _js_request_property_binding(
                tokens, *transport)
            if request_name is None or request_name in parameters:
                continue
            request_factory = False
            request_assignment = None
            for position in range(start, index):
                if (tokens[position] in {
                        ("ident", "const"), ("ident", "let"),
                        ("ident", "var")}
                        and position + 5 < end
                        and tokens[position + 1] == ("ident", request_name)
                        and tokens[position + 2] == ("punct", "=")
                        and tokens[position + 3:position + 6] == [
                            ("ident", "vi"), ("punct", "."),
                            ("ident", "fn")]
                        and block_depth(start, position) == 0):
                    request_factory = True
                    request_assignment = position + 2
                    break
            if not request_factory:
                continue
            client_var = None
            if (index >= start + 3 and tokens[index - 1] == ("punct", "=")
                    and tokens[index - 2][0] == "ident"
                    and tokens[index - 3] in {
                        ("ident", "const"), ("ident", "let"),
                        ("ident", "var")}):
                client_var = tokens[index - 2][1]
            if (client_var is None or block_depth(start, index - 3) != 0
                    or client_name in _js_shadowed_bindings(
                        tokens[start:end], {client_name})):
                continue
            if any(tokens[position] == ("punct", "=")
                   and position > start
                   and tokens[position - 1] == ("ident", request_name)
                   and position != request_assignment
                   and block_depth(start, position) == 0
                   for position in range(start, end)):
                continue
            bound_expressions = []
            for position in range(start, end - 6):
                if (tokens[position] == ("ident", client_var)
                        and tokens[position + 1] == ("punct", ".")
                        and tokens[position + 2][0] == "ident"
                        and tokens[position + 3] == ("punct", ".")
                        and tokens[position + 4] == ("ident", "bind")
                        and tokens[position + 5] == ("punct", "(")
                        and position + 6 < end
                        and tokens[position + 6] == ("ident", client_var)):
                    close_bind = _js_matching_delimiter(
                        tokens, position + 5, "(", ")")
                    if close_bind is not None and close_bind < end:
                        bound_expressions.append((position, close_bind))
            if not bound_expressions:
                continue
            supplied = False
            for position in range(start, end - 2):
                if (tokens[position][0] != "ident"
                        or tokens[position + 1] != ("punct", "(")
                        or position > start and tokens[position - 1] == (
                            "punct", ".")):
                    continue
                close_args = _js_matching_delimiter(
                    tokens, position + 1, "(", ")")
                if close_args is None or close_args >= end:
                    continue
                if any(expression_start > position + 1
                       and expression_end < close_args
                       for expression_start, expression_end
                       in bound_expressions):
                    supplied = True
                    break
            if not supplied:
                continue
            for position in range(start, end - 4):
                if (tokens[position] != ("ident", "expect")
                        or tokens[position + 1] != ("punct", "(")
                        or tokens[position + 2] != ("ident", request_name)):
                    continue
                close_expect = _js_matching_delimiter(
                    tokens, position + 1, "(", ")")
                if close_expect is None or close_expect + 2 >= end:
                    continue
                matcher = close_expect + 1
                if tokens[matcher] != ("punct", "."):
                    continue
                if tokens[matcher + 1] == ("ident", "not"):
                    continue
                if matcher + 2 >= end or tokens[matcher + 1][0] != "ident":
                    continue
                method = tokens[matcher + 1][1]
                if method not in {"toHaveBeenCalledWith",
                                  "toHaveBeenCalledExactlyOnceWith"} \
                        or tokens[matcher + 2] != ("punct", "("):
                    continue
                close_call = _js_matching_delimiter(
                    tokens, matcher + 2, "(", ")")
                if close_call is None or close_call >= end:
                    continue
                matcher_tokens = tokens[matcher + 3:close_call]
                has_method = any(
                    matcher_tokens[cursor] in (("ident", "method"),
                                               ("string", "method"))
                    and matcher_tokens[cursor + 1] == ("punct", ":")
                    and cursor + 2 < len(matcher_tokens)
                    and matcher_tokens[cursor + 2][0] == "string"
                    and matcher_tokens[cursor + 2][1].upper() in {
                        "GET", "POST", "PUT", "PATCH", "DELETE"}
                    for cursor in range(max(0, len(matcher_tokens) - 2)))
                has_path = any(
                    matcher_tokens[cursor] in (("ident", "path"),
                                               ("string", "path"))
                    and cursor + 1 < len(matcher_tokens)
                    and matcher_tokens[cursor + 1] == ("punct", ":")
                    for cursor in range(len(matcher_tokens)))
                if has_method and has_path:
                    injected = True
                    break
            if injected:
                break
        if injected:
            break
    if signal_cache is not None:
        signal_cache[key] = injected
    return injected


def _js_runtime_targets(source, source_text, entry, texts, context,
                        candidates, RC, support_cache, depth,
                        signal_cache=None):
    if not source.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        return ()
    key = ("js-runtime-targets", source, entry.id, depth)
    if support_cache is not None and key in support_cache:
        return support_cache[key]
    aliases = _configured_js_aliases(texts, context, RC, support_cache)
    prefix = ("" if getattr(context, "declaration", ".") == "."
              else context.declaration.rstrip("/") + "/")
    result = []
    source_tokens = (support_cache.get(("js-source-tokens", source))
                     if support_cache is not None else None)
    if source_tokens is None:
        source_tokens = RC._js_tokens(source_text)
        if support_cache is not None:
            support_cache[("js-source-tokens", source)] = source_tokens
    for specifier, used in _js_import_bindings(
            source_text, RC, support_cache, source):
        if not used:
            continue
        specs = []
        if specifier.startswith("."):
            specs = RC._js_spec_targets(source, specifier) or []
        else:
            matches = [(alias, value) for alias, value in aliases.items()
                       if specifier == alias or specifier.startswith(alias + "/")]
            if matches:
                alias, (config_path, replacement) = max(
                    matches, key=lambda row: len(row[0]))
                suffix = specifier[len(alias):].lstrip("/")
                mapped = (replacement.rstrip("/")
                          + ("/" + suffix if suffix else ""))
                specs = RC._js_spec_targets(config_path, mapped) or []
        target = next((path for path in specs
                       if path in candidates and path.startswith(prefix)), None)
        if target is None:
            continue
        component_use = any(_js_component_binding_used(source_tokens, name)
                            for name in used)
        target_text = texts.get(target)
        has_text = isinstance(target_text, str)
        item_operation = (has_text and _has_item_anchor(
            target, target_text, entry, context, signal_cache))
        if depth == 0:
            if component_use and not item_operation:
                # Keep a rendered component only when it participates in an
                # executable imported service chain, not for presentation.
                if (not has_text or not _js_has_runtime_call_import(
                        target_text, RC, support_cache, target)):
                    continue
            result.append((target, not item_operation))
        elif item_operation:
            result.append((target, False))
    result.sort(key=lambda row: (row[1], row[0]))
    targets = tuple(dict.fromkeys(path for path, _weak in result))[
        :RC._CONTEXT_MAX_LINKS_PER_FILE]
    if support_cache is not None:
        support_cache[key] = targets
    return targets


def _js_component_binding_used(tokens, binding):
    return any(token == ("punct", "<")
               and index + 1 < len(tokens)
               and tokens[index + 1] == ("ident", binding)
               for index, token in enumerate(tokens))


def _js_has_runtime_call_import(text, RC, support_cache=None, source=None):
    return any(used for _specifier, used in _js_import_bindings(
        text, RC, support_cache, source))


def selected_item_support(callers, entry, texts, context, *,
                          candidate_paths=None,
                          support_cache: dict | None = None,
                          signal_cache: dict | None = None,
                          selected_owners: dict | None = None) -> tuple[str, ...]:
    """Resolve a small source chain from selected callers without execution."""
    from . import review_context as RC

    candidates = set(texts if candidate_paths is None else candidate_paths)
    roles = dict(context.roles)
    declaration = getattr(context, "declaration", ".")
    prefix = "" if declaration == "." else declaration.rstrip("/") + "/"
    relations: dict[str, list[tuple[str, str]]] = {}
    for source, target, kind in context.relations:
        relations.setdefault(source, []).append((target, kind))
    result: list[str] = []
    for caller in callers:
        queue = [(caller, 0, ())]
        visited: set[str] = set()
        link_count = 0
        while queue and link_count < RC._CONTEXT_MAX_LINKS_PER_FILE:
            source, depth, owner_symbols = queue.pop(0)
            if source in visited or depth >= RC._CONTEXT_MAX_DEPTH:
                continue
            visited.add(source)
            source_text = texts.get(source)
            if source_text is None:
                continue
            edges: list[str] = []
            edge_symbols: dict[str, set[str]] = {}
            if (entry.id == "FIX-001" and depth == 0
                    and source.endswith(".py")):
                # Relations are deliberately capped and may omit a later
                # active caller. Resolve only explicitly requested ordinary
                # fixtures in its applicable ancestor conftests.
                requested = _python_requested_fixture_members(
                    source, source_text, entry, signal_cache)
                unresolved = set(requested)
                parent = source.rpartition("/")[0]
                child_prefix = ("" if declaration == "."
                                else declaration.rstrip("/"))
                while unresolved:
                    conftest = (f"{parent}/conftest.py"
                                if parent else "conftest.py")
                    fixture_text = texts.get(conftest)
                    if (conftest in candidates and fixture_text is not None
                            and roles.get(conftest) in {"fixture", "setup"}):
                        matched = unresolved & RC._conftest_fixtures(
                            fixture_text)
                        if matched:
                            edges.append(conftest)
                            edge_symbols.setdefault(conftest, set()).update(
                                matched)
                            unresolved.difference_update(matched)
                    if parent == child_prefix or not parent:
                        break
                    parent = parent.rpartition("/")[0]
            for target, kind in relations.get(source, ()):
                if target not in candidates:
                    continue
                target_text = texts.get(target)
                if kind == "fixture-use":
                    edges.append(target)
                    if (entry.id == "FIX-001" and depth == 0
                            and target_text is not None
                            and target.rsplit("/", 1)[-1] == "conftest.py"
                            and roles.get(target) in {"fixture", "setup"}):
                        requested = _python_requested_fixture_members(
                            source, source_text, entry, signal_cache)
                        fixture_names = (set(requested)
                                         & RC._conftest_fixtures(target_text))
                        edge_symbols.setdefault(target, set()).update(
                            fixture_names)
                elif (kind == "local-import" and target_text is not None
                      and (roles.get(target) in {"fixture", "setup"}
                           or _has_item_anchor(target, target_text, entry,
                                               context, signal_cache))):
                    edges.append(target)
            edges.extend(_js_runtime_targets(
                source, source_text, entry, texts, context, candidates,
                RC, support_cache, depth, signal_cache))
            if source.endswith(".py") and owner_symbols:
                for target, original in _python_selected_base_targets(
                        source, source_text, owner_symbols, texts, candidates,
                        prefix, RC, support_cache):
                    edges.append(target)
                    if original:
                        edge_symbols.setdefault(target, set()).add(original)
            # Resolve only literal local imports used by the chosen Python
            # owner. Package exports are searched by matching symbol, not by
            # taking the first sixteen declarations in the initializer.
            owner_key = tuple(sorted(set(owner_symbols)))
            py_key = ("python-support-targets", source, entry.id,
                      owner_key)
            py_targets = (support_cache.get(py_key)
                          if support_cache is not None else None)
            python_symbol_map = {}
            fixture_owner = (entry.id == "FIX-001" and depth == 1
                             and source.rsplit("/", 1)[-1] == "conftest.py"
                             and bool(owner_key))
            if py_targets is None and (depth == 0 or fixture_owner):
                child_source = (source[len(prefix):]
                                if prefix and source.startswith(prefix)
                                else source)
                found: list[tuple[str, str]] = []
                for module, level, used_symbols in _python_used_imports(
                        source, source_text, entry, support_cache, RC,
                        signal_cache,
                        owner_names=(owner_key if fixture_owner else ())):
                    module_targets = [prefix + value for value in
                                      RC._python_spec_targets(
                                          module, level, child_source)
                                      if prefix + value in candidates]
                    if module_targets:
                        if used_symbols:
                            found.extend((module_targets[0], symbol)
                                         for symbol in used_symbols)
                        else:
                            found.append((module_targets[0], ""))
                        if used_symbols:
                            for target in module_targets:
                                if not target.endswith("/__init__.py"):
                                    continue
                                for symbol in used_symbols:
                                    found.extend(_python_export_targets(
                                        target, symbol, texts, candidates,
                                        prefix, RC, support_cache))
                            package_dirs = {posixpath.dirname(path)
                                            for path in module_targets}
                            for symbol in used_symbols:
                                sibling = symbol.replace(".", "/")
                                for package_dir in package_dirs:
                                    for target in (
                                            f"{package_dir}/{sibling}.py",
                                            f"{package_dir}/{sibling}/__init__.py"):
                                        if target in candidates:
                                            found.append((target, symbol))
                unique_found = tuple(dict.fromkeys(found))[
                    :RC._CONTEXT_MAX_LINKS_PER_FILE]
                py_targets = tuple(dict.fromkeys(
                    target for target, _symbol in unique_found))
                python_symbol_map = {}
                for target, symbol in unique_found:
                    if symbol:
                        python_symbol_map.setdefault(target, set()).add(symbol)
                if support_cache is not None:
                    support_cache[py_key] = py_targets
                    support_cache[("python-symbol-targets", source,
                                   entry.id, owner_key)] = {
                                       target: tuple(sorted(values))
                                       for target, values in
                                       python_symbol_map.items()}
            if py_targets is not None:
                edges.extend(py_targets)
                if support_cache is not None:
                    python_symbol_map = support_cache.get(
                        ("python-symbol-targets", source, entry.id,
                         owner_key), {})
                for target, symbols in python_symbol_map.items():
                    edge_symbols.setdefault(target, set()).update(symbols)
                    if selected_owners is not None:
                        selected_owners.setdefault(target, set()).update(
                            symbols)
            invocation_key = ("invocation-support-targets", source)
            invocation_targets = (support_cache.get(invocation_key)
                                  if support_cache is not None else None)
            if invocation_targets is None:
                invocation_targets = static_local_invocation_targets(
                    source, source_text, candidates)
                if support_cache is not None:
                    support_cache[invocation_key] = invocation_targets
            edges.extend(invocation_targets)
            for target in dict.fromkeys(edges):
                if target == source or target in visited:
                    continue
                result.append(target)
                link_count += 1
                symbols = tuple(sorted(edge_symbols.get(target, ())))
                if selected_owners is not None and symbols:
                    selected_owners.setdefault(target, set()).update(symbols)
                queue.append((target, depth + 1, symbols))
                if link_count >= RC._CONTEXT_MAX_LINKS_PER_FILE:
                    break
    return tuple(dict.fromkeys(result))


def item_source_chains(item_id: str, texts, context, candidate_paths, *,
                       declaration: str = ".",
                       signal_cache: dict | None = None,
                       support_cache: dict | None = None,
                       caller_paths=None,
                       max_chains: int = 24) -> tuple[tuple[str, ...], ...]:
    """Return ranked active caller chains with exact bounded local support.

    Each chain starts with one executable test caller, followed by only its
    resolved local imports/invocations and applicable ancestor conftests.
    Admission and item selection share this function so a caller is never
    intentionally planned separately from its available support.
    """
    if not isinstance(texts, dict):
        raise TypeError("texts must be a dict of path to text")
    if not isinstance(candidate_paths, (set, frozenset, tuple, list)):
        raise TypeError("candidate_paths must be a finite collection")
    if signal_cache is None:
        signal_cache = {}
    candidates = set(candidate_paths)
    context = _with_declaration_prefix(context, declaration)
    role_by_path = dict(context.roles)
    callers = rank_item_candidates(
        context, tuple(candidates if caller_paths is None
                       else set(caller_paths) & candidates), texts, item_id,
        declaration=declaration, signal_cache=signal_cache)
    chains: list[tuple[str, ...]] = []
    child_prefix = "" if declaration == "." else declaration.rstrip("/")
    entry = next((item for item in CATALOG if item.id == item_id), None)
    if entry is None:
        raise ValueError("unknown checklist item")
    considered = 0
    for caller in callers:
        if (role_by_path.get(caller) != "test"
                or _TEST_PATH_RE.search(caller) is None
                or not (_has_item_anchor(caller, texts[caller], entry,
                                         context, signal_cache)
                        or _has_related_fixture_anchor(
                            caller, texts, entry, context, signal_cache))):
            continue
        considered += 1
        if considered > 64:
            break
        support = list(selected_item_support(
            (caller,), entry, texts, context,
            candidate_paths=candidates, support_cache=support_cache,
            signal_cache=signal_cache))
        ancestors: list[str] = []
        parent = caller.rpartition("/")[0]
        while True:
            conftest = f"{parent}/conftest.py" if parent else "conftest.py"
            if conftest in candidates:
                ancestors.append(conftest)
            if parent == child_prefix or not parent:
                break
            parent = parent.rpartition("/")[0]
        chain = tuple(dict.fromkeys(
            (caller, *support, *ancestors)))
        if len(chain) > 1 + 2 * 16:
            chain = chain[:1 + 2 * 16]
        chains.append(chain)
        if len(chains) >= max_chains:
            break
    return tuple(chains)


def selected_invocation_paths(callers, texts, candidate_paths) -> tuple[str, ...]:
    """Follow only literal script-to-script invocations, to depth three."""
    candidates = set(candidate_paths)
    found: list[str] = []
    for caller in callers:
        queue = [(caller, 0)]
        visited: set[str] = set()
        links = 0
        while queue and links < 16:
            source, depth = queue.pop(0)
            if source in visited or depth >= 3:
                continue
            visited.add(source)
            text = texts.get(source)
            if text is None:
                continue
            for target in static_local_invocation_targets(
                    source, text, candidates):
                if target == source or target in visited:
                    continue
                found.append(target)
                links += 1
                queue.append((target, depth + 1))
                if links >= 16:
                    break
    return tuple(dict.fromkeys(found))


def _line_text(lines: list[str], start: int, end: int) -> str:
    return "".join(lines[start - 1:end])


def _node_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _python_source_units(excerpt, item_id: str, context, *,
                         whole_module: bool = False,
                         caller_core: bool = False,
                         linked_fixture_names=frozenset(),
                         selected_symbols=frozenset(),
                         signal_cache: dict | None = None
                         ) -> tuple[SourceUnit, ...]:
    if not excerpt.complete:
        return ()
    selected_symbols = frozenset(selected_symbols)
    if whole_module and not selected_symbols:
        if len(excerpt.text.encode("utf-8")) <= MAX_UNIT_BYTES:
            lines = excerpt.text.splitlines(keepends=True)
            if lines and len(lines) <= MAX_UNIT_LINES:
                return (SourceUnit(excerpt.path, excerpt.start_line,
                                   excerpt.start_line + len(lines) - 1,
                                   excerpt.sha256, excerpt.text,
                                   _source_role(excerpt.path, context)),)
    analysis = (signal_cache or {}).get((
        "source-analysis", excerpt.path, excerpt.text))
    tree = analysis[3] if analysis is not None else None
    if tree is None:
        try:
            tree = ast.parse(excerpt.text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return ()
    lines = excerpt.text.splitlines(keepends=True)
    if not lines:
        return ()
    entry = next((item for item in CATALOG if item.id == item_id), None)
    patterns = _resource_patterns(entry) if entry is not None else ()
    role = _source_role(excerpt.path, context)
    is_test = role == "test" and _TEST_PATH_RE.search(excerpt.path) is not None
    is_fixture_owner = (
        role in {"setup", "fixture"}
        or excerpt.path.rsplit("/", 1)[-1] == "conftest.py")
    fixture_used = role == "fixture" and any(
        target == excerpt.path and kind == "fixture-use"
        for _source, target, kind in context.relations)
    candidates = []
    imports = []
    declarations = []
    functions = []
    selected_test_owners = {id(node) for node in _python_test_owners(
        excerpt.path, excerpt.text, entry, tree, signal_cache
    )} if entry is not None else set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node in tree.body:
                imports.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node)
        elif isinstance(node, ast.ClassDef):
            # Methods are emitted as separate non-overlapping owner units.
            # A class with no methods can still be useful when it directly
            # declares the item-specific mechanism.
            has_methods = any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                for child in node.body)
            if has_methods and node.name not in selected_symbols:
                continue
            decorators = getattr(node, "decorator_list", ())
            start = min([node.lineno, *(item.lineno for item in decorators)])
            end = node.end_lineno or node.lineno
            text = _line_text(lines, start, end)
            code = _executable_text(excerpt.path, text)
            call_hit, body_hit, control, criterion_control = _item_signal(
                excerpt.path, text, entry, signal_cache) if entry is not None else (
                    False, False, False, False)
            direct = (any(pattern.search(code) for pattern in patterns)
                      or call_hit or body_hit)
            if direct or node.name in selected_symbols:
                candidates.append((node, text, start, end,
                                   criterion_control
                                   or node.name in selected_symbols))
        elif node in tree.body:
            targets: set[str] = set()
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                values = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in values:
                    targets.update(child.id for child in ast.walk(target)
                                   if isinstance(child, ast.Name))
            if targets:
                declarations.append((node, targets))

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in functions:
        decorators = getattr(node, "decorator_list", ())
        start = min([node.lineno, *(item.lineno for item in decorators)])
        end = node.end_lineno or node.lineno
        text = _line_text(lines, start, end)
        code = _executable_text(excerpt.path, text)
        call_hit, body_hit, _control, criterion_control = _item_signal(
            excerpt.path, text, entry, signal_cache) if entry is not None else (
                False, False, False, False)
        direct = (any(pattern.search(code) for pattern in patterns)
                  or call_hit or body_hit)
        decorator_names = set().union(*(_node_names(item) for item in decorators)) \
            if decorators else set()
        fixture = "fixture" in decorator_names
        autouse_fixture = (
            is_fixture_owner and "fixture" in decorator_names
            and any(
                isinstance(child, ast.Call)
                and _call_name(child.func).endswith("fixture")
                and any(keyword.arg == "autouse"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                        for keyword in child.keywords)
                for decorator in decorators
                for child in ast.walk(decorator)))
        parent = parents.get(node)
        owner_level = parent is tree or isinstance(parent, ast.ClassDef)
        linked_caller = (is_test and node.name.startswith("test_")
                         and any(argument.arg in linked_fixture_names
                                 for argument in ast.walk(node.args)
                                 if isinstance(argument, ast.arg)))
        propagated_test = (is_test and node.name.startswith("test_")
                           and id(node) in selected_test_owners)
        if owner_level and (direct or propagated_test or autouse_fixture
                           or (fixture_used and "fixture" in decorator_names)
                           or linked_caller
                           or node.name in selected_symbols):
            candidates.append((node, text, start, end,
                               criterion_control
                               or node.name in selected_symbols))

    selected_nodes: list[tuple[ast.AST, str, int, int]] = []
    referenced: set[str] = set()
    def candidate_priority(value):
        node, text, start, _end, criterion_control = value
        code = _executable_text(excerpt.path, text)
        call_hit, body_hit, control, _specific = _item_signal(
            excerpt.path, text, entry, signal_cache) if entry is not None else (
                False, False, False, False)
        direct = (any(pattern.search(code) for pattern in patterns)
                  or call_hit or body_hit)
        active_test = (is_test and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))
        assertion = bool(re.search(r"(?m)^\s*assert\b", text))
        return (not criterion_control, not direct, not control,
                not assertion, not active_test, start)

    candidates.sort(key=candidate_priority)
    bounded_candidates = candidates[:MAX_UNITS_PER_ITEM]
    owner_nodes: dict[str, list[ast.AST]] = {}
    class_members: dict[ast.ClassDef, dict[str, list[ast.AST]]] = {}
    for owner in tree.body:
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            owner_nodes.setdefault(owner.name, []).append(owner)
        if isinstance(owner, ast.ClassDef):
            members = {}
            for member in owner.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.setdefault(member.name, []).append(member)
            class_members[owner] = members

    def direct_owner_values(caller):
        direct_calls = set()
        method_calls = set()
        parent = parents.get(caller)
        if isinstance(parent, ast.ClassDef):
            for call in ast.walk(caller):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Name):
                    direct_calls.add(call.func.id)
                elif (isinstance(call.func, ast.Attribute)
                      and isinstance(call.func.value, ast.Name)
                      and call.func.value.id in {"self", "cls"}):
                    method_calls.add(call.func.attr)
        else:
            direct_calls = {
                call.func.id for call in ast.walk(caller)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
        values = []
        names = sorted(method_calls) + sorted(direct_calls - method_calls)
        for name in names:
            matches = (class_members.get(parent, {}).get(name, ())
                       if name in method_calls
                       else owner_nodes.get(name, ()))
            if len(matches) != 1 or matches[0] is caller:
                continue
            owner = matches[0]
            decorators = getattr(owner, "decorator_list", ())
            start = min([owner.lineno, *(item.lineno
                                         for item in decorators)])
            end = owner.end_lineno or owner.lineno
            owner_text = _line_text(lines, start, end)
            if (end - start + 1 > MAX_UNIT_LINES
                    or len(owner_text.encode("utf-8")) > MAX_UNIT_BYTES):
                return None
            values.append((owner, owner_text, start, end, False))
        return values

    selected_candidates = []
    if caller_core and is_test:
        preferred_owners = _python_test_owners(
            excerpt.path, excerpt.text, entry, tree, signal_cache)
        values_by_owner = {id(value[0]): value for value in candidates}
        active_callers = [values_by_owner[id(node)]
                          for node in preferred_owners
                          if id(node) in values_by_owner]
        direct_ids = {id(value[0]) for value in active_callers}
        linked_callers = [value for value in candidates
                          if id(value[0]) not in direct_ids
                          and isinstance(value[0], (ast.FunctionDef,
                                                    ast.AsyncFunctionDef))
                          and value[0].name.startswith("test_")
                          and any(argument.arg in linked_fixture_names
                                  for argument in ast.walk(value[0].args)
                                  if isinstance(argument, ast.arg))]
        active_callers.extend(linked_callers)
        for caller in active_callers:
            support = direct_owner_values(caller[0])
            if support is not None and 1 + len(support) <= MAX_UNITS_PER_ITEM:
                selected_candidates = [caller, *support]
                break
    else:
        # Optional test owners are selected as caller-plus-direct-helper
        # groups. A caller whose local owner cannot fit the existing cap is
        # omitted as a group, so the packet never presents it alone.
        test_values = [value for value in bounded_candidates
                       if is_test and isinstance(
                           value[0], (ast.FunctionDef, ast.AsyncFunctionDef))
                       and value[0].name.startswith("test_")]
        other_values = [value for value in bounded_candidates
                        if value not in test_values]
        selected_ids: set[int] = set()
        for caller in test_values:
            support = direct_owner_values(caller[0])
            if support is None:
                continue
            group = [caller, *(value for value in support
                               if id(value[0]) not in selected_ids)]
            if len(selected_candidates) + len(group) > MAX_UNITS_PER_ITEM:
                continue
            selected_candidates.extend(group)
            selected_ids.update(id(value[0]) for value in group)
        for value in other_values:
            if len(selected_candidates) >= MAX_UNITS_PER_ITEM:
                break
            if id(value[0]) not in selected_ids:
                selected_candidates.append(value)
                selected_ids.add(id(value[0]))
    for node, text, start, end, _specific in selected_candidates:
        if (end - start + 1 > MAX_FUNCTION_LINES
                and isinstance(node, (ast.FunctionDef,
                                      ast.AsyncFunctionDef))):
            continue
        if end - start + 1 > MAX_UNIT_LINES:
            continue
        if len(text.encode("utf-8")) > MAX_UNIT_BYTES:
            continue
        selected_nodes.append((node, text, start, end))
        referenced.update(_node_names(node))
    units: list[SourceUnit] = []
    # Include only imports used by a selected evidence owner.
    imported_names: set[str] = set(selected_symbols)
    for node in selected_nodes:
        imported_names.update(_node_names(node[0]))
    for node in imports:
        bound = set()
        if isinstance(node, ast.Import):
            bound.update(alias.asname or alias.name.split(".", 1)[0]
                         for alias in node.names)
        else:
            bound.update(alias.asname or alias.name for alias in node.names
                         if alias.name != "*")
        if not bound.intersection(imported_names):
            continue
        text = _line_text(lines, node.lineno, node.end_lineno or node.lineno)
        if text.strip() and len(text.encode("utf-8")) <= MAX_UNIT_BYTES:
            units.append(SourceUnit(excerpt.path, node.lineno, node.end_lineno or node.lineno,
                                    excerpt.sha256, text, role))
    for node, names in declarations:
        # pytest consumes these module declarations itself; function AST
        # references cannot reveal their effect on collection or fixtures.
        runner_declaration = is_test and bool(
            names & {"pytestmark", "pytest_plugins"})
        if not (names & referenced) and not runner_declaration:
            continue
        text = _line_text(lines, node.lineno, node.end_lineno or node.lineno)
        end = node.end_lineno or node.lineno
        if end - node.lineno + 1 <= MAX_UNIT_LINES and len(
                text.encode("utf-8")) <= MAX_UNIT_BYTES:
            units.append(SourceUnit(excerpt.path, node.lineno, end,
                                    excerpt.sha256, text, role))
    for node, text, start, end in selected_nodes:
        if any(start >= owner_start and end <= owner_end
               for _owner, _owner_text, owner_start, owner_end in selected_nodes
               if (owner_start, owner_end) != (start, end)):
            continue
        units.append(SourceUnit(excerpt.path, start, end,
                                excerpt.sha256, text, role))
    unique: dict[tuple, SourceUnit] = {}
    for unit in sorted(units, key=lambda value: (value.start_line,
                                                 value.end_line)):
        unique.setdefault((unit.start_line, unit.end_line, unit.text), unit)
    return tuple(sorted(unique.values(),
                        key=lambda unit: _unit_priority(
                            unit, item_id, signal_cache))
                 [:MAX_UNITS_PER_ITEM])


def source_units(excerpt, item_id: str, context, *,
                 whole_module: bool = False,
                 caller_core: bool = False,
                 linked_fixture_names=frozenset(),
                 selected_symbols=frozenset(),
                 signal_cache: dict | None = None
                 ) -> tuple[SourceUnit, ...]:
    """Select complete contiguous units from one immutable admitted excerpt."""
    if not hasattr(excerpt, "path") or not hasattr(excerpt, "text"):
        raise TypeError("excerpt must be a source excerpt")
    if not isinstance(item_id, str) or not item_id:
        raise TypeError("item id must be nonempty str")
    if not isinstance(context, object):
        raise TypeError("context is required")
    path = excerpt.path
    if path.endswith(".py"):
        return _python_source_units(
            excerpt, item_id, context,
            whole_module=(not selected_symbols and (whole_module
                          or (path.rsplit("/", 1)[-1] == "conftest.py"
                              and _source_role(path, context)
                              in {"setup", "fixture"}))),
            caller_core=caller_core,
            linked_fixture_names=linked_fixture_names,
            selected_symbols=selected_symbols,
            signal_cache=signal_cache)
    if not excerpt.complete:
        return ()
    role = _source_role(path, context)
    basename = path.rsplit("/", 1)[-1].lower()
    is_config = _is_config_path(path, context)
    source_extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                         ".go", ".rs", ".java", ".kt", ".sql", ".sh",
                         ".c", ".h", ".cpp", ".cc", ".cs", ".rb", ".php")
    entry = next(item for item in CATALOG if item.id == item_id)
    rank = _item_rank(path, excerpt.text, entry, context, signal_cache)
    caller = role == "test" and _TEST_PATH_RE.search(path) is not None
    fixture_used = role == "fixture" and any(
        target == path and kind == "fixture-use"
        for _source, target, kind in context.relations)
    relevant = (whole_module or fixture_used or not rank[0]
                or (caller and _has_item_anchor(path, excerpt.text,
                                                entry, context,
                                                signal_cache)))
    if not is_config and not (path.endswith(source_extensions) and relevant):
        return ()
    text = excerpt.text
    lines = text.splitlines(keepends=True)
    if not lines or len(lines) > MAX_UNIT_LINES \
            or len(text.encode("utf-8")) > MAX_UNIT_BYTES:
        return ()
    return (SourceUnit(path, excerpt.start_line,
                       excerpt.start_line + len(lines) - 1,
                       excerpt.sha256, text, role),)


def select_item_sources(packet, item_id: str):
    """Return bounded initial units, reserve units, and explicit omissions."""
    if not isinstance(item_id, str) or not item_id:
        raise TypeError("item id must be nonempty str")
    context = _with_declaration_prefix(packet.context, packet.declaration)
    from . import review_context as RC
    signal_cache: dict = {}
    texts = {excerpt.path: excerpt.text for excerpt in packet.excerpts}
    entry = next((item for item in CATALOG if item.id == item_id), None)
    if entry is None:
        raise ValueError("unknown checklist item")
    all_ranked_paths = tuple(sorted(
        _active_paths(texts, context),
        key=lambda path: _item_rank(
            path, texts[path], entry, context, signal_cache)))
    by_path = {excerpt.path: excerpt for excerpt in packet.excerpts}
    role_by_path = dict(context.roles)
    config_paths = [path for path in all_ranked_paths
                    if role_by_path.get(path) == "config"
                    or _is_config_path(path, context)]
    bound_chains = next((chains for digest, bound_item, chains
                         in packet._item_chains
                         if digest == packet.packet_sha256
                         and bound_item == item_id), None)
    if bound_chains is None:
        chains = item_source_chains(
            item_id, texts, context, tuple(by_path),
            declaration=packet.declaration)
    else:
        chains = tuple(tuple(path for path in chain if path in by_path)
                       for chain in bound_chains
                       if chain and all(path in by_path for path in chain))
    caller_paths = [chain[0] for chain in chains]
    support_paths = list(dict.fromkeys(
        path for chain in chains for path in chain[1:]))
    selected_support_symbols: dict[str, set[str]] = {}
    support_cache: dict = {}
    for chain in chains:
        selected_item_support(
            (chain[0],), entry, texts, context,
            candidate_paths=set(chain), support_cache=support_cache,
            signal_cache=signal_cache,
            selected_owners=(selected_support_symbols
                             if item_id == "FIX-001" else None))
    linked_fixture_names: dict[str, set[str]] = {}
    for source, target, kind in context.relations:
        if (kind == "fixture-use" and source in caller_paths
                and target in texts
                and _has_item_anchor(target, texts[target], entry, context,
                                     signal_cache)):
            linked_fixture_names.setdefault(source, set()).update(
                RC._conftest_fixtures(texts[target]))
    global_setups = [path for path in all_ranked_paths
                     if path not in config_paths + caller_paths
                     and role_by_path.get(path) == "setup"]
    ordered: list[str] = []
    seen_paths: set[str] = set()
    def append_paths(paths):
        for path in paths:
            if path in by_path and path not in seen_paths:
                seen_paths.add(path)
                ordered.append(path)
    append_paths(config_paths)
    append_paths(global_setups)
    for chain in chains:
        append_paths(chain)
    ranked_paths = tuple(ordered)
    units_by_path: dict[str, tuple[SourceUnit, ...]] = {}
    core_units_by_path: dict[str, tuple[SourceUnit, ...]] = {}
    missing: list[tuple[str, str]] = []
    for path in ranked_paths:
        excerpt = by_path[path]
        selected_symbols = frozenset(selected_support_symbols.get(path, ()))
        whole_module = (not selected_symbols and (
            (path in support_paths and path not in caller_paths)
            or path in config_paths
            or role_by_path.get(path) == "setup"))
        units = source_units(excerpt, item_id, context,
                             whole_module=whole_module,
                             linked_fixture_names=frozenset(
                                 linked_fixture_names.get(path, ())),
                             selected_symbols=selected_symbols,
                             caller_core=path in caller_paths,
                             signal_cache=signal_cache)
        if not units:
            if not excerpt.complete:
                missing.append((path, "partial-source"))
            elif (_item_rank(path, excerpt.text, entry,
                             context, signal_cache)[0] is False
                  or _source_role(path, context) in {
                      "config", "setup", "fixture", "helper", "test"}):
                missing.append((path, "incomplete-or-irrelevant-unit"))
            continue
        units_by_path[path] = units
        if path in caller_paths:
            core_units_by_path[path] = units
    initial: list[SourceUnit] = []
    reserve: list[SourceUnit] = []
    initial_paths: set[str] = set()
    initial_bytes = 0
    reserve_paths: set[str] = set()
    reserve_bytes = 0
    ranked_units = {
        path: tuple(sorted(units_by_path.get(path, ()),
                           key=lambda unit: _unit_priority(
                               unit, item_id, signal_cache)))
        for path in ranked_paths
    }
    selected_units: set[SourceUnit] = set()

    def add_units(group_units, *, allow_reserve=False):
        nonlocal initial_bytes, reserve_bytes
        group_units = tuple(unit for unit in group_units
                            if unit not in selected_units)
        if not group_units:
            return True
        new_paths = {unit.path for unit in group_units}
        byte_count = sum(len(unit.text.encode("utf-8"))
                         for unit in group_units)
        if (len(initial) + len(group_units) <= MAX_INITIAL_UNITS
                and len(initial_paths | new_paths) <= MAX_INITIAL_FILES
                and initial_bytes + byte_count <= MAX_INITIAL_BYTES):
            initial.extend(group_units)
            selected_units.update(group_units)
            initial_paths.update(new_paths)
            initial_bytes += byte_count
            return True
        if not allow_reserve:
            return False
        extra_paths = new_paths - initial_paths - reserve_paths
        if (len(initial) + len(reserve) + len(group_units)
                > MAX_UNITS_PER_ITEM
                or len(initial_paths | reserve_paths | extra_paths)
                > MAX_INITIAL_FILES + MAX_RESERVE_FILES
                or len(extra_paths) > MAX_RESERVE_FILES
                or reserve_bytes + byte_count > MAX_RESERVE_BYTES):
            return False
        reserve.extend(group_units)
        selected_units.update(group_units)
        reserve_paths.update(new_paths - initial_paths)
        reserve_bytes += byte_count
        return True

    def add_group(paths, *, allow_reserve=False):
        group_paths = tuple(dict.fromkeys(paths))
        if any(path not in units_by_path for path in group_paths):
            return False
        group_units = tuple(
            unit
            for path in group_paths
            for unit in (core_units_by_path.get(path, ranked_units[path])
                         if path in caller_paths else ranked_units[path]))
        return add_units(group_units, allow_reserve=allow_reserve)

    for path in config_paths:
        if path in units_by_path and not add_group((path,)):
            missing.append((path, "source-budget-exhausted"))
    for path in global_setups:
        if path in units_by_path and not add_group((path,)):
            missing.append((path, "source-budget-exhausted"))
    for chain in chains:
        if not add_group(chain, allow_reserve=True):
            missing.extend((path, "source-budget-exhausted")
                           for path in chain)
    for path in ranked_paths:
        if path not in initial_paths and path not in reserve_paths \
                and path in by_path:
            reason = ("source-budget-exhausted" if initial or reserve
                      else "no-relevant-unit")
            missing.append((path, reason))
    return (tuple(initial), tuple(reserve[:MAX_RESERVE_UNITS]),
            tuple(missing[:64]))


def _source_role(path: str, context, *,
                 index: _ContextIndex | None = None) -> str:
    """Resolve only an explicitly normalized source path."""
    if _is_config_path(path, context, index=index):
        return "config"
    if index is not None:
        return index.roles.get(path, "source")
    roles = dict(context.roles)
    return roles.get(path, "source")


__all__ = ["SourceUnit", "source_id", "rank_candidates",
           "rank_item_candidates", "source_units",
           "select_item_sources", "MAX_UNIT_LINES", "MAX_UNITS_PER_ITEM"]
