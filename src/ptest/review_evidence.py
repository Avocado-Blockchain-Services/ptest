"""Pure suite-aware ranking and contiguous source-unit selection.

The collector owns filesystem boundaries and source hashes. This module only
selects complete units from an already admitted immutable packet; it never
opens files, executes project code, or synthesizes lines.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import posixpath
import re
import tokenize
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


def _resource_patterns(entry) -> tuple[re.Pattern, ...]:
    patterns = []
    for value in entry.text_patterns:
        try:
            patterns.append(re.compile(value, re.IGNORECASE))
        except re.error:
            continue
    return tuple(patterns)


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


def _relation_score(path: str, context) -> int:
    roles = dict(context.roles)
    outgoing = {source for source, _target, _kind in context.relations}
    incoming = {target for _source, target, _kind in context.relations}

    def matches(candidate: str) -> bool:
        return candidate == path

    score = 0
    if any(matches(candidate) for candidate in outgoing):
        score += 2
    if any(matches(candidate) for candidate in incoming):
        score += 3
    role = next((value for candidate, value in roles.items()
                 if matches(candidate)), None)
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


def _python_identifier_text(text: str) -> str:
    """Normalize executable Python identifiers for semantic word matching."""
    try:
        names = [token.string for token in tokenize.generate_tokens(
            io.StringIO(text).readline) if token.type == tokenize.NAME]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    words = []
    for name in names:
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
        words.append(re.sub(r"_+", " ", split))
    return " ".join(words)


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
    key = (path, id(entry))
    if cache is not None and key in cache:
        return cache[key]
    source_key = ("source-analysis", path)
    source = cache.get(source_key) if cache is not None else None
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
        call_hit = any(pattern.search(name) or pattern.search(name + "(")
                       for name in call_names for pattern in patterns)
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


_ROOT_MANIFEST_NAMES = frozenset({
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
})


def _is_config_path(path: str, context=None) -> bool:
    """Recognize deciding config and child-root manifests only."""
    if (path in getattr(context, "config_paths", ())
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
               signal_cache: dict | None = None) -> tuple:
    call_hit, body_hit, generic_control, criterion_control = _item_signal(
        path, text, entry, signal_cache)
    path_specific, path_generic = _matches_path(path, entry)
    context_score = _relation_score(path, context)
    role = _source_role(path, context)
    caller = role == "test" and _TEST_PATH_RE.search(path) is not None
    direct = call_hit or body_hit or criterion_control or path_specific
    # Generic cleanup can only order already relevant candidates. A finally
    # block in an unrelated helper never makes it an item candidate.
    return (not _is_config_path(path, context),
            not criterion_control,
            not call_hit, not body_hit, not path_specific, not caller,
            -context_score if direct else 0,
            not generic_control if direct else True,
            not path_generic, path)


def _has_item_anchor(path: str, text: str, entry, context,
                     signal_cache: dict | None = None) -> bool:
    call_hit, body_hit, _control, criterion_control = _item_signal(
        path, text, entry, signal_cache)
    path_specific, _path_generic = _matches_path(path, entry)
    return call_hit or body_hit or criterion_control or path_specific


def _has_related_fixture_anchor(path: str, texts: dict, entry, context) -> bool:
    """An active test is relevant when it uses this item's fixture evidence."""
    for source, target, kind in context.relations:
        if source != path or kind != "fixture-use":
            continue
        text = texts.get(target)
        if text is not None and _has_item_anchor(target, text, entry, context):
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


def _unit_priority(unit: SourceUnit, item_id: str) -> tuple:
    """Put complete mechanisms and callers ahead of import-only units."""
    text = unit.text
    if unit.role == "config":
        return (0, unit.start_line)
    entry = next((item for item in CATALOG if item.id == item_id), None)
    call_hit, body_hit, control, criterion_control = (
        _item_signal(unit.path, text, entry)
        if entry else (False, False, False, False))
    assertion = bool(re.search(r"(?m)^\s*assert\b", text))
    function = bool(re.search(
        r"(?m)^\s*(?:async\s+)?def\s+|^\s*class\s+", text))
    imported = bool(re.match(r"\s*(?:from\s+\S+\s+import|import\s+)", text))
    if criterion_control:
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
    context = _with_declaration_prefix(context, declaration)
    paths = sorted({path for path in candidates if isinstance(path, str)
                    and isinstance(texts.get(path), str)})
    paths = _active_paths(paths, context)
    from .deterministic_items import DETERMINISTIC_ITEM_IDS
    rows: list[list[str]] = []
    for item in catalog:
        if item.id in DETERMINISTIC_ITEM_IDS:
            continue
        ranked = sorted(paths, key=lambda path: _item_rank(
            path, texts[path], item, context, signal_cache))
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
    context = _with_declaration_prefix(context, declaration)
    paths = [path for path in candidates
             if isinstance(path, str) and isinstance(texts.get(path), str)]
    paths = _active_paths(paths, context)
    return tuple(sorted(set(paths), key=lambda path: _item_rank(
        path, texts[path], entry, context, signal_cache)))


def _python_used_imports(source, text, entry, cache, RC, signal_cache):
    """Return imports used by one item-relevant caller body only.

    A module-level import can be used by an unrelated test or helper. Treating
    every load in the module as a dependency makes one caller inherit the
    entire file's import closure and routinely exceeds the packet budget.
    """
    key = ("python-used-imports", source, entry.id)
    if cache is not None and key in cache:
        return cache[key]
    try:
        analysis = (signal_cache or {}).get(("source-analysis", source))
        tree = analysis[3] if analysis is not None else ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        tree = None
    if tree is None:
        result = ()
    else:
        callers = [node for node in tree.body
                   if isinstance(node, (ast.FunctionDef,
                                        ast.AsyncFunctionDef))
                   and node.name.startswith("test_")]
        lines = text.splitlines(keepends=True)
        ranked_callers = []
        for node in callers:
            start = min([node.lineno, *(item.lineno
                                        for item in node.decorator_list)])
            end = node.end_lineno or node.lineno
            body = _line_text(lines, start, end)
            code = _executable_text(source, body)
            searchable = code + "\n" + _python_identifier_text(code)
            patterns = _resource_patterns(entry)
            body_hit = any(pattern.search(searchable)
                           for pattern in patterns)
            call_names = [_call_name(value.func) for value in ast.walk(node)
                          if isinstance(value, ast.Call)]
            call_hit = any(pattern.search(name) or pattern.search(name + "(")
                           for name in call_names for pattern in patterns)
            criterion = (entry.id == "TIME-001"
                         and (_TIME_SPECIFIC_RE.search(code) is not None
                              or any(_TIME_SPECIFIC_RE.search(name + "(")
                                     for name in call_names)))
            assertion = bool(re.search(r"(?m)^\s*assert\b", body))
            ranked_callers.append((not criterion, not call_hit, not body_hit,
                                   not assertion, start, node,
                                   bool(call_hit or body_hit or criterion)))
        ranked_callers.sort(key=lambda item: item[:5])
        selected = next((item[5] for item in ranked_callers if item[6]), None)
        if selected is None:
            result = ()
        else:
            loaded = {node.id for node in ast.walk(selected)
                      if isinstance(node, ast.Name)
                      and isinstance(node.ctx, ast.Load)}
            module_specs, _fixture_params, _truncated = RC._python_links(text)
            supported = set(module_specs)
            result_values = []
            import_nodes = [node for node in tree.body
                            if isinstance(node, (ast.Import,
                                                 ast.ImportFrom))]
            for node in import_nodes[:RC._CONTEXT_MAX_LINKS_PER_FILE]:
                if isinstance(node, ast.ImportFrom):
                    module, level = node.module or "", node.level
                    used = tuple(dict.fromkeys(
                        alias.name for alias in node.names
                        if alias.name != "*"
                        and (alias.asname or alias.name) in loaded))
                    if (module, level) in supported and used:
                        result_values.append((module, level, used))
                else:
                    for alias in node.names:
                        module = alias.name
                        binding = alias.asname or module.split(".", 1)[0]
                        if (module, 0) in supported and binding in loaded:
                            result_values.append((module, 0, ()))
            result = tuple(dict.fromkeys(result_values))
    if cache is not None:
        cache[key] = result
    return result


def selected_item_support(callers, entry, texts, context, *,
                          candidate_paths=None,
                          support_cache: dict | None = None,
                          signal_cache: dict | None = None) -> tuple[str, ...]:
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
        queue = [(caller, 0)]
        visited: set[str] = set()
        link_count = 0
        while queue and link_count < RC._CONTEXT_MAX_LINKS_PER_FILE:
            source, depth = queue.pop(0)
            if source in visited or depth >= RC._CONTEXT_MAX_DEPTH:
                continue
            visited.add(source)
            source_text = texts.get(source)
            if source_text is None:
                continue
            edges: list[str] = []
            for target, kind in relations.get(source, ()):
                if target not in candidates:
                    continue
                target_text = texts.get(target)
                if kind == "fixture-use":
                    edges.append(target)
                elif (kind == "local-import" and target_text is not None
                      and (roles.get(target) in {"fixture", "setup"}
                           or _has_item_anchor(target, target_text, entry,
                                               context))):
                    edges.append(target)
            js_key = ("js-support-targets", source)
            js_targets = (support_cache.get(js_key)
                          if support_cache is not None else None)
            if js_targets is None:
                discovered = []
                for specifier in RC._vitest_import_links(source_text):
                    targets = RC._js_spec_targets(source, specifier)
                    if targets is not None:
                        target = next((value for value in targets
                                       if value in candidates), None)
                        if target is not None:
                            discovered.append(target)
                js_targets = tuple(dict.fromkeys(discovered))
                if support_cache is not None:
                    support_cache[js_key] = js_targets
            edges.extend(js_targets)
            # Resolve only bounded, literal local Python imports whose bound
            # name is used by this caller. Import edges from the collector
            # remain useful for fixtures/setup, but source-role metadata may
            # be absent when the collection cap stopped before the target.
            py_key = ("python-support-targets", source, entry.id)
            py_targets = (support_cache.get(py_key)
                          if support_cache is not None else None)
            # Python imports are rooted only at the selected caller body.
            # Imported support modules can be complete bounded owners, but
            # their unrelated imports are not recursively treated as needs.
            if py_targets is None and depth == 0:
                child_source = (source[len(prefix):]
                                if prefix and source.startswith(prefix)
                                else source)
                found = []
                for module, level, used_symbols in _python_used_imports(
                        source, source_text, entry, support_cache, RC,
                        signal_cache):
                    module_targets = [prefix + value for value in
                                      RC._python_spec_targets(
                                          module, level, child_source)
                                      if prefix + value in candidates]
                    if module_targets:
                        found.append(module_targets[0])
                    if not used_symbols:
                        continue
                    # ``from package import name`` may load a sibling
                    # submodule or a literal top-level re-export in the
                    # package initializer. Resolve only those two forms
                    # from already-frozen candidate text.
                    for init_path in module_targets:
                        if not init_path.endswith("/__init__.py"):
                            continue
                        init_text = texts.get(init_path)
                        if not isinstance(init_text, str):
                            continue
                        try:
                            init_tree = ast.parse(init_text)
                        except (SyntaxError, ValueError, RecursionError,
                                MemoryError):
                            continue
                        child_init = (init_path[len(prefix):]
                                      if prefix and init_path.startswith(prefix)
                                      else init_path)
                        for statement in init_tree.body[:RC._CONTEXT_MAX_LINKS_PER_FILE]:
                            if isinstance(statement, ast.ImportFrom):
                                for alias in statement.names:
                                    exported = alias.asname or alias.name
                                    if exported not in used_symbols:
                                        continue
                                    if statement.module:
                                        specs = RC._python_spec_targets(
                                            statement.module,
                                            statement.level, child_init)
                                    else:
                                        specs = RC._python_spec_targets(
                                            alias.name, statement.level,
                                            child_init)
                                    found.extend(prefix + value for value in specs
                                                 if prefix + value in candidates)
                            elif isinstance(statement, ast.Import):
                                for alias in statement.names:
                                    exported = alias.asname or alias.name.split(
                                        ".", 1)[0]
                                    if exported not in used_symbols:
                                        continue
                                    found.extend(prefix + value for value in
                                                 RC._python_spec_targets(
                                                     alias.name, 0, child_init)
                                                 if prefix + value in candidates)
                        package_dir = posixpath.dirname(init_path)
                        for symbol in used_symbols:
                            sibling = symbol.replace(".", "/")
                            for target in (f"{package_dir}/{sibling}.py",
                                           f"{package_dir}/{sibling}/__init__.py"):
                                if target in candidates:
                                    found.append(target)
                py_targets = tuple(dict.fromkeys(found))
                if support_cache is not None:
                    support_cache[py_key] = py_targets
            if py_targets is not None:
                edges.extend(py_targets)
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
                queue.append((target, depth + 1))
                if link_count >= RC._CONTEXT_MAX_LINKS_PER_FILE:
                    break
    return tuple(dict.fromkeys(result))


def item_source_chains(item_id: str, texts, context, candidate_paths, *,
                       declaration: str = ".",
                       signal_cache: dict | None = None,
                       support_cache: dict | None = None,
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
    candidates = set(candidate_paths)
    context = _with_declaration_prefix(context, declaration)
    role_by_path = dict(context.roles)
    callers = rank_item_candidates(
        context, tuple(candidates), texts, item_id,
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
                            caller, texts, entry, context))):
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
                         linked_fixture_names=frozenset()
                         ) -> tuple[SourceUnit, ...]:
    if not excerpt.complete:
        return ()
    if whole_module:
        if len(excerpt.text.encode("utf-8")) <= MAX_UNIT_BYTES:
            lines = excerpt.text.splitlines(keepends=True)
            if lines and len(lines) <= MAX_UNIT_LINES:
                return (SourceUnit(excerpt.path, excerpt.start_line,
                                   excerpt.start_line + len(lines) - 1,
                                   excerpt.sha256, excerpt.text,
                                   _source_role(excerpt.path, context)),)
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
            if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for child in node.body):
                continue
            decorators = getattr(node, "decorator_list", ())
            start = min([node.lineno, *(item.lineno for item in decorators)])
            end = node.end_lineno or node.lineno
            text = _line_text(lines, start, end)
            code = _executable_text(excerpt.path, text)
            call_hit, body_hit, control, criterion_control = _item_signal(
                excerpt.path, text, entry) if entry is not None else (
                    False, False, False, False)
            direct = (any(pattern.search(code) for pattern in patterns)
                      or call_hit or body_hit)
            if direct:
                candidates.append((node, text, start, end,
                                   criterion_control))
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
            excerpt.path, text, entry) if entry is not None else (
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
        if owner_level and (direct or autouse_fixture
                           or (fixture_used and "fixture" in decorator_names)
                           or linked_caller):
            candidates.append((node, text, start, end,
                               criterion_control))

    selected_nodes: list[tuple[ast.AST, str, int, int]] = []
    referenced: set[str] = set()
    def candidate_priority(value):
        node, text, start, _end, criterion_control = value
        code = _executable_text(excerpt.path, text)
        call_hit, body_hit, control, _specific = _item_signal(
            excerpt.path, text, entry) if entry is not None else (
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
    for owner in tree.body:
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            owner_nodes.setdefault(owner.name, []).append(owner)

    def direct_owner_values(caller):
        direct_calls = {
            call.func.id for call in ast.walk(caller)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        values = []
        for name in sorted(direct_calls):
            matches = owner_nodes.get(name, ())
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
        active_callers = [value for value in candidates
                          if isinstance(value[0], (ast.FunctionDef,
                                                    ast.AsyncFunctionDef))
                          and value[0].name.startswith("test_")]
        if active_callers:
            caller = active_callers[0]
            support = direct_owner_values(caller[0])
            if support is not None and 1 + len(support) <= MAX_UNITS_PER_ITEM:
                selected_candidates = [caller, *support]
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
    imported_names: set[str] = set()
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
                        key=lambda unit: _unit_priority(unit, item_id))
                 [:MAX_UNITS_PER_ITEM])


def source_units(excerpt, item_id: str, context, *,
                 whole_module: bool = False,
                 caller_core: bool = False,
                 linked_fixture_names=frozenset()
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
            whole_module=(whole_module
                          or (path.rsplit("/", 1)[-1] == "conftest.py"
                              and _source_role(path, context)
                              in {"setup", "fixture"})),
            caller_core=caller_core,
            linked_fixture_names=linked_fixture_names)
    if not excerpt.complete:
        return ()
    role = _source_role(path, context)
    basename = path.rsplit("/", 1)[-1].lower()
    is_config = _is_config_path(path, context)
    source_extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                         ".go", ".rs", ".java", ".kt", ".sql", ".sh",
                         ".c", ".h", ".cpp", ".cc", ".cs", ".rb", ".php")
    entry = next(item for item in CATALOG if item.id == item_id)
    rank = _item_rank(path, excerpt.text, entry, context)
    caller = role == "test" and _TEST_PATH_RE.search(path) is not None
    fixture_used = role == "fixture" and any(
        target == path and kind == "fixture-use"
        for _source, target, kind in context.relations)
    relevant = (whole_module or fixture_used or not rank[0]
                or (caller and _has_item_anchor(path, excerpt.text,
                                                entry, context)))
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
    texts = {excerpt.path: excerpt.text for excerpt in packet.excerpts}
    entry = next((item for item in CATALOG if item.id == item_id), None)
    if entry is None:
        raise ValueError("unknown checklist item")
    all_ranked_paths = tuple(sorted(
        _active_paths(texts, context),
        key=lambda path: _item_rank(path, texts[path], entry, context)))
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
    linked_fixture_names: dict[str, set[str]] = {}
    for source, target, kind in context.relations:
        if (kind == "fixture-use" and source in caller_paths
                and target in texts
                and _has_item_anchor(target, texts[target], entry, context)):
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
        whole_module = (path in support_paths and path not in caller_paths
                        or path in config_paths
                        or role_by_path.get(path) == "setup")
        units = source_units(excerpt, item_id, context,
                             whole_module=whole_module,
                             linked_fixture_names=frozenset(
                                 linked_fixture_names.get(path, ())))
        if not units:
            if not excerpt.complete:
                missing.append((path, "partial-source"))
            elif (_item_rank(path, excerpt.text, entry,
                             context)[0] is False
                  or _source_role(path, context) in {
                      "config", "setup", "fixture", "helper", "test"}):
                missing.append((path, "incomplete-or-irrelevant-unit"))
            continue
        units_by_path[path] = units
        if path in caller_paths:
            core_units_by_path[path] = source_units(
                excerpt, item_id, context, caller_core=True,
                linked_fixture_names=frozenset(
                    linked_fixture_names.get(path, ())))
    initial: list[SourceUnit] = []
    reserve: list[SourceUnit] = []
    initial_paths: set[str] = set()
    initial_bytes = 0
    reserve_paths: set[str] = set()
    reserve_bytes = 0
    ranked_units = {
        path: tuple(sorted(units_by_path.get(path, ()),
                           key=lambda unit: _unit_priority(unit, item_id)))
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
    # Preserve additional concrete operations in the chosen test modules
    # when space remains; the first caller plus its referenced declarations
    # and complete support chain already fit atomically above.
    for path in caller_paths:
        core = set(core_units_by_path.get(path, ()))
        for unit in ranked_units.get(path, ()):
            if unit in core:
                continue
            if not add_units((unit,), allow_reserve=True):
                missing.append((path, "source-budget-exhausted"))
    for path in ranked_paths:
        if path not in initial_paths and path not in reserve_paths \
                and path in by_path:
            reason = ("source-budget-exhausted" if initial or reserve
                      else "no-relevant-unit")
            missing.append((path, reason))
    return (tuple(initial), tuple(reserve[:MAX_RESERVE_UNITS]),
            tuple(missing[:64]))


def _source_role(path: str, context) -> str:
    """Resolve only an explicitly normalized source path."""
    if _is_config_path(path, context):
        return "config"
    roles = dict(context.roles)
    return roles.get(path, "source")


__all__ = ["SourceUnit", "source_id", "rank_candidates",
           "rank_item_candidates", "source_units",
           "select_item_sources", "MAX_UNIT_LINES", "MAX_UNITS_PER_ITEM"]
