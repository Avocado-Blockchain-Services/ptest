"""Per-content AST source index for dependency-recorded test selection.

Pure static analysis over file bytes: scopes (function/method bodies and
skeletons), classes, top-level statements and imports, plus the
content-addressed project index builder with an injectable parse cache.

N11: this module never imports or executes project code — only ``ast``
parses of byte buffers. Fingerprints are
``sha256(ast.dump(node, include_attributes=False))`` hex digests, so
comments, whitespace and line shifts are no change.

On-disk cache format: a fixed magic header, a big-endian uint32
``SOURCE_INDEX_VERSION``, then a canonical JSON object. ``decode_index``
returns None for anything else.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import struct
from pathlib import Path

from . import contracts as C
from . import impact as _impact

_MAGIC = b"ptest-srcidx\n"
_HEADER = _MAGIC + struct.pack(">I", C.SOURCE_INDEX_VERSION)
_PAYLOAD_KEYS = ("parsed", "imports", "scopes", "classes", "statements")


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _fingerprint(dumps: list[str]) -> str:
    return hashlib.sha256("\n".join(dumps).encode("utf-8")).hexdigest()


def _unparsed() -> C.FileIndex:
    return C.FileIndex(parsed=False, imports=(), scopes=(),
                       classes=(), statements=())


# -- load chains ---------------------------------------------------------

class _ChainCollector(ast.NodeVisitor):
    """Collect each dotted ``Name./Attribute`` load chain exactly once."""

    def __init__(self) -> None:
        self.chains: set[tuple[str, ...]] = set()

    @staticmethod
    def _spine(node: ast.AST) -> tuple[str, ...] | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) \
                and isinstance(node.ctx, ast.Load):
            parts.append(node.id)
            return tuple(reversed(parts))
        return None

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            chain = self._spine(node)
            if chain is not None:
                self.chains.add(chain)
            # Skip the spine: its Name would re-emit a sub-chain.
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.chains.add((node.id,))


def _chains_of(nodes: list[ast.AST]) -> tuple[tuple[str, ...], ...]:
    collector = _ChainCollector()
    for node in nodes:
        collector.visit(node)
    return tuple(sorted(collector.chains))


# -- statements ----------------------------------------------------------

def _target_names(target: ast.AST) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return tuple(names)
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return ()


def _bound_names(stmt: ast.stmt) -> tuple[str, ...]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (stmt.name,)
    if isinstance(stmt, ast.Import):
        return tuple(alias.asname or alias.name.split(".")[0]
                     for alias in stmt.names)
    if isinstance(stmt, ast.ImportFrom):
        if any(alias.name == "*" for alias in stmt.names):
            return ("*",)
        return tuple(alias.asname or alias.name for alias in stmt.names)
    if isinstance(stmt, ast.Assign):
        names: list[str] = []
        for target in stmt.targets:
            names.extend(_target_names(target))
        return tuple(names)
    if isinstance(stmt, ast.AnnAssign):
        return _target_names(stmt.target)
    if isinstance(stmt, ast.AugAssign):
        return _target_names(stmt.target)
    return ()


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) \
        and isinstance(stmt.value, ast.Constant) \
        and isinstance(stmt.value.value, str)


def _is_simple_target(target: ast.AST) -> bool:
    """Plain binding targets only: Name, Tuple/List/Starred thereof.

    Anything else (Attribute, Subscript, ...) mutates another object and
    binds no module-level name, so the statement is effectful (spec 5.3).
    """
    if isinstance(target, ast.Name):
        return True
    if isinstance(target, (ast.Tuple, ast.List)):
        return all(_is_simple_target(elt) for elt in target.elts)
    if isinstance(target, ast.Starred):
        return _is_simple_target(target.value)
    return False


def _classify(stmt: ast.stmt) -> str:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "def"
    if isinstance(stmt, ast.ClassDef):
        return "class"
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return "import"
    if isinstance(stmt, ast.Assign):
        if all(_is_simple_target(target) for target in stmt.targets):
            return "assign"
        return "effect"
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        if _is_simple_target(stmt.target):
            return "assign"
        return "effect"
    if _is_docstring(stmt):
        return "doc"
    return "effect"


def _skeleton_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Nodes carrying skeleton refs: decorators, signature/defaults,
    returns and type params (spec 5.2/5.4 name-change sources)."""
    return [*func.decorator_list, func.args] \
        + ([func.returns] if func.returns is not None else []) \
        + list(getattr(func, "type_params", []))


def _skeleton_dumps(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    parts = [_dump(decorator) for decorator in func.decorator_list]
    parts.append(_dump(func.args))
    if func.returns is not None:
        parts.append(_dump(func.returns))
    parts.extend(_dump(param) for param in getattr(func, "type_params", []))
    if isinstance(func, ast.AsyncFunctionDef):
        parts.append("async")
    return parts


def _class_skeleton_dumps(node: ast.ClassDef) -> list[str]:
    parts = [_dump(decorator) for decorator in node.decorator_list]
    parts.extend(_dump(base) for base in node.bases)
    parts.extend(_dump(keyword) for keyword in node.keywords)
    parts.extend(_dump(param) for param in getattr(node, "type_params", []))
    return parts


def _body_dumps(body: list[ast.stmt]) -> list[str]:
    """Body statement dumps, leading docstring included.

    Function/method docstrings are runtime-observable (``__doc__``,
    ``--help``, doctest), so only top-level ``doc`` statements are ignored
    by the planner — never the docstring inside a scope body.
    """
    return [_dump(stmt) for stmt in body]


def _child_statement_lists(stmt: ast.stmt) -> list[list[ast.stmt]]:
    out: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(stmt, field, None)
        if isinstance(value, list) and value \
                and all(isinstance(item, ast.stmt) for item in value):
            out.append(value)
    for handler in getattr(stmt, "handlers", []) or []:
        if isinstance(handler, ast.ExceptHandler):
            out.append(handler.body)
    match_cases = getattr(stmt, "cases", None)
    if isinstance(match_cases, list):
        for case in match_cases:
            body = getattr(case, "body", None)
            if isinstance(body, list):
                out.append(body)
    return out


def index_source(raw: bytes) -> C.FileIndex:
    """Index one file content. Never imports or executes it (N11)."""
    try:
        if not isinstance(raw, bytes) or len(raw) > _impact.MAX_FILE_BYTES:
            return _unparsed()
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return _unparsed()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _unparsed()

    imports: list[C.ImportIndex] = []
    scope_acc: dict[str, dict] = {}
    class_acc: dict[str, dict] = {}
    statements: list[C.StatementIndex] = []
    top_import_ids: set[int] = set()

    def add_scope(qualname: str,
                  node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        entry = scope_acc.setdefault(
            qualname, {"body": [], "skeleton": [], "refs": set()})
        entry["body"].extend(_body_dumps(node.body))
        entry["skeleton"].extend(_skeleton_dumps(node))
        # Body chains plus skeleton chains: a default, decorator or
        # annotation referencing a changed name affects the scope (N1).
        entry["refs"].update(_chains_of(node.body))
        entry["refs"].update(_chains_of(_skeleton_nodes(node)))

    def add_class(node: ast.ClassDef, parts: tuple[str, ...]) -> None:
        qualname = ".".join(parts)
        entry = class_acc.setdefault(
            qualname, {"skeleton": [], "body": [], "refs": set()})
        entry["skeleton"].extend(_class_skeleton_dumps(node))
        members = [stmt for stmt in node.body
                   if not isinstance(stmt, (ast.FunctionDef,
                                             ast.AsyncFunctionDef))]
        # Class docstrings are runtime-observable (__doc__); keep them in
        # the body fingerprint. Only top-level "doc" statements are ignored.
        entry["body"].extend(_dump(stmt) for stmt in members)
        # Member-function skeletons: a method default, decorator or
        # annotation referencing a changed name changes the class (N1).
        # Method bodies stay out: they are covered per-scope (AF).
        method_skeleton: list[ast.AST] = []
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_skeleton.extend(_skeleton_nodes(stmt))
        entry["refs"].update(
            _chains_of([*node.decorator_list, *node.bases,
                        *[keyword.value for keyword in node.keywords],
                        *getattr(node, "type_params", []), *members,
                        *method_skeleton]))

    def walk(stmts: list[ast.stmt], parts: tuple[str, ...],
             in_function: bool) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*parts, stmt.name))
                if not in_function:
                    add_scope(qualname, stmt)
                walk(stmt.body, (*parts, stmt.name), True)
                for extra in _child_statement_lists(stmt):
                    if extra is not stmt.body:
                        walk(extra, (*parts, stmt.name), True)
            elif isinstance(stmt, ast.ClassDef):
                if not in_function:
                    add_class(stmt, (*parts, stmt.name))
                walk(stmt.body, (*parts, stmt.name), in_function)
            else:
                for extra in _child_statement_lists(stmt):
                    walk(extra, parts, in_function)

    walk(tree.body, (), False)

    for position, stmt in enumerate(tree.body):
        kind = _classify(stmt)
        bound = tuple(sorted(set(_bound_names(stmt))))
        if kind == "def":
            refs = _chains_of(_skeleton_nodes(stmt))
            fingerprint = _fingerprint(_skeleton_dumps(stmt))
        elif kind == "class":
            refs = _chains_of([*stmt.decorator_list, *stmt.bases,
                               *[keyword.value
                                 for keyword in stmt.keywords]]
                              + list(getattr(stmt, "type_params", [])))
            fingerprint = _fingerprint(_class_skeleton_dumps(stmt))
        elif kind == "import":
            refs = ()
            fingerprint = _fingerprint([_dump(stmt)])
            top_import_ids.add(id(stmt))
        elif kind == "doc":
            refs = ()
            fingerprint = _fingerprint([_dump(stmt)])
        else:
            refs = _chains_of([stmt])
            fingerprint = _fingerprint([_dump(stmt)])
        statements.append(C.StatementIndex(kind=kind, bound=bound,
                                           refs=refs,
                                           fingerprint=fingerprint))
        if kind == "import":
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    imports.append(C.ImportIndex(
                        level=0, module=alias.name, name=None,
                        local=alias.asname or alias.name.split(".")[0],
                        aliased=alias.asname is not None,
                        statement=position))
            else:
                for alias in stmt.names:  # type: ignore[union-attr]
                    imports.append(C.ImportIndex(
                        level=stmt.level, module=stmt.module or "",
                        name=alias.name,
                        local=None if alias.name == "*"
                        else (alias.asname or alias.name),
                        aliased=alias.asname is not None,
                        statement=position))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) \
                and id(node) not in top_import_ids:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(C.ImportIndex(
                        level=0, module=alias.name, name=None, local=None,
                        aliased=alias.asname is not None, statement=-1))
            else:
                for alias in node.names:
                    imports.append(C.ImportIndex(
                        level=node.level, module=node.module or "",
                        name=alias.name, local=None,
                        aliased=alias.asname is not None, statement=-1))

    scopes = tuple(C.ScopeIndex(qualname=qualname,
                                body=_fingerprint(entry["body"]),
                                skeleton=_fingerprint(entry["skeleton"]),
                                refs=tuple(sorted(entry["refs"])))
                   for qualname, entry in sorted(scope_acc.items()))
    classes = tuple(C.ClassIndex(qualname=qualname,
                                 skeleton=_fingerprint(entry["skeleton"]),
                                 body=_fingerprint(entry["body"]),
                                 refs=tuple(sorted(entry["refs"])))
                    for qualname, entry in sorted(class_acc.items()))
    return C.FileIndex(parsed=True, imports=tuple(imports), scopes=scopes,
                       classes=classes, statements=tuple(statements))


# -- cache encoding ------------------------------------------------------

def encode_index(index: C.FileIndex) -> bytes:
    """Deterministic, versioned bytes for one FileIndex (the cache value)."""
    payload = {
        "classes": [[entry.qualname, entry.skeleton, entry.body,
                     [list(chain) for chain in entry.refs]]
                    for entry in index.classes],
        "imports": [[imp.level, imp.module, imp.name, imp.local,
                     imp.aliased, imp.statement]
                    for imp in index.imports],
        "parsed": index.parsed,
        "scopes": [[entry.qualname, entry.body, entry.skeleton,
                    [list(chain) for chain in entry.refs]]
                   for entry in index.scopes],
        "statements": [[stmt.kind, list(stmt.bound),
                        [list(chain) for chain in stmt.refs],
                        stmt.fingerprint]
                       for stmt in index.statements],
    }
    return _HEADER + json.dumps(payload, sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True).encode("ascii")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_chain_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(chain, list) and all(isinstance(part, str)
                                        for part in chain)
        for chain in value)


def decode_index(blob: bytes) -> C.FileIndex | None:
    """Decode cache bytes; None on any malformed or foreign blob."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < len(_HEADER):
        return None
    if bytes(blob[:len(_MAGIC)]) != _MAGIC:
        return None
    (version,) = struct.unpack(">I", bytes(blob[len(_MAGIC):len(_HEADER)]))
    if version != C.SOURCE_INDEX_VERSION:
        return None
    try:
        payload = json.loads(bytes(blob[len(_HEADER):]).decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) \
            or set(payload) != set(_PAYLOAD_KEYS):
        return None
    try:
        parsed = payload["parsed"]
        if type(parsed) is not bool:
            return None
        for field in ("imports", "scopes", "classes", "statements"):
            if not isinstance(payload[field], list):
                return None
        imports = []
        for raw in payload["imports"]:
            if not isinstance(raw, list) or len(raw) != 6:
                return None
            level, module, name, local, aliased, statement = raw
            if not _is_int(level) or level < 0 \
                    or not isinstance(module, str) \
                    or (name is not None and not isinstance(name, str)) \
                    or (local is not None and not isinstance(local, str)) \
                    or type(aliased) is not bool \
                    or not _is_int(statement):
                return None
            imports.append(C.ImportIndex(level=level, module=module,
                                         name=name, local=local,
                                         aliased=aliased,
                                         statement=statement))
        scopes = []
        for raw in payload["scopes"]:
            if not isinstance(raw, list) or len(raw) != 4:
                return None
            qualname, body, skeleton, refs = raw
            if not isinstance(qualname, str) \
                    or not isinstance(body, str) \
                    or not isinstance(skeleton, str) \
                    or not _is_chain_list(refs):
                return None
            scopes.append(C.ScopeIndex(
                qualname=qualname, body=body, skeleton=skeleton,
                refs=tuple(tuple(chain) for chain in refs)))
        classes = []
        for raw in payload["classes"]:
            if not isinstance(raw, list) or len(raw) != 4:
                return None
            qualname, skeleton, body, refs = raw
            if not isinstance(qualname, str) \
                    or not isinstance(skeleton, str) \
                    or not isinstance(body, str) \
                    or not _is_chain_list(refs):
                return None
            classes.append(C.ClassIndex(
                qualname=qualname, skeleton=skeleton, body=body,
                refs=tuple(tuple(chain) for chain in refs)))
        statements = []
        for raw in payload["statements"]:
            if not isinstance(raw, list) or len(raw) != 4:
                return None
            kind, bound, refs, fingerprint = raw
            if kind not in C.SELECTION_STATEMENT_KINDS \
                    or not isinstance(bound, list) \
                    or not all(isinstance(name, str) for name in bound) \
                    or not _is_chain_list(refs) \
                    or not isinstance(fingerprint, str):
                return None
            statements.append(C.StatementIndex(
                kind=kind, bound=tuple(bound),
                refs=tuple(tuple(chain) for chain in refs),
                fingerprint=fingerprint))
    except (TypeError, struct.error):
        return None
    return C.FileIndex(parsed=parsed, imports=tuple(imports),
                       scopes=tuple(scopes), classes=tuple(classes),
                       statements=tuple(statements))


# -- project walk ----------------------------------------------------------

def iter_python_files(project_root: Path):
    """Yield project-relative posix paths of .py files (impact's walk)."""
    stack = [Path(project_root)]
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
                if entry.name.startswith(".") \
                        or entry.name in _impact._SKIP_DIRS \
                        or entry.name.endswith(".egg-info"):
                    continue
                stack.append(entry)
            elif entry.is_file() and entry.name.endswith(".py"):
                yield entry.relative_to(project_root).as_posix()


def _rel_parts(project_root: Path, rel: str) -> list[str] | None:
    if not isinstance(rel, str) or not rel or "\\" in rel \
            or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def read_source(project_root: Path, rel: str) -> bytes | None:
    """Read one project file: no-follow regular file within the size cap."""
    parts = _rel_parts(project_root, rel)
    if parts is None:
        return None
    path = Path(project_root).joinpath(*parts)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        chunks: list[bytes] = []
        remaining = _impact.MAX_FILE_BYTES + 1
        while True:
            block = os.read(fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
            if remaining <= 0:
                break
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(raw) > _impact.MAX_FILE_BYTES:
        return None
    return raw


# -- name resolution (design 2.3 table) ------------------------------------

def _absolute_module(level: int, module: str, package: str) -> str | None:
    """Resolve a from-import head against the file's package (None drops)."""
    if level == 0:
        return module or None
    base_parts = package.split(".") if package else []
    if level - 1 > len(base_parts):
        return None
    base = ".".join(base_parts[:len(base_parts) - level + 1])
    head = (base + "." + module) if module else base
    return head.strip(".") or None


def _edge_names(imports: tuple[C.ImportIndex, ...], package: str) -> set[str]:
    """0.4 ``_import_edges`` module names, replayed from index records."""
    edges: set[str] = set()

    def _add(dotted: str) -> None:
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            edges.add(".".join(parts[:i]))

    for imp in imports:
        if imp.name is None:
            _add(imp.module)
            continue
        head = _absolute_module(imp.level, imp.module, package)
        if not head:
            continue
        _add(head)
        if imp.name != "*":
            edges.add(head + "." + imp.name)
    return edges


class _Resolver:
    """Module-level bindings of every file plus the module map."""

    def __init__(self, rels: list[str],
                 indexes: dict[str, C.FileIndex],
                 modules_of: dict[str, tuple[str, ...]]) -> None:
        self._mod_paths: dict[str, set[str]] = {}
        for rel in rels:
            for name in modules_of[rel]:
                self._mod_paths.setdefault(name, set()).add(rel)
        self._packages = {rel: _impact._package_name(rel) for rel in rels}
        # name -> ("local",) | ("import", ImportIndex); stars in order.
        self._bindings: dict[str, dict[str, tuple]] = {}
        self._stars: dict[str, list[C.ImportIndex]] = {}
        for rel in rels:
            bound: dict[str, tuple] = {}
            stars: list[C.ImportIndex] = []
            by_statement: dict[int, list[C.ImportIndex]] = {}
            for imp in indexes[rel].imports:
                if imp.statement >= 0:
                    by_statement.setdefault(imp.statement, []).append(imp)
            # Source order: the last binding of a name wins, whatever kind.
            for position, stmt in enumerate(indexes[rel].statements):
                if stmt.kind == "import":
                    for imp in by_statement.get(position, []):
                        if imp.name == "*":
                            # Star imports carry local=None by construction
                            # (index_source), so this branch must come before
                            # the local-is-None skip or _stars stays empty
                            # and neither bound_names() expansion nor the
                            # resolve() star fallback can ever fire.
                            stars.append(imp)
                        elif imp.local is None:
                            continue
                        else:
                            bound[imp.local] = ("import", imp)
                elif stmt.kind in ("def", "class", "assign", "effect"):
                    # "effect" included: a mixed-target assign such as
                    # x = d['k'] = v still binds its plain Name targets
                    # (stmt.bound holds only those; pure mutating targets
                    # yield () so nothing is added for them).
                    for name in stmt.bound:
                        bound[name] = ("local",)
            self._bindings[rel] = bound
            self._stars[rel] = stars
        self._memo: dict[str, frozenset[str]] = {}

    def module_paths(self, module: str) -> set[str]:
        return self._mod_paths.get(module, set())

    def from_target(self, rel: str, imp: C.ImportIndex) -> frozenset[str]:
        """Resolved name keys of one from-import (design 2.3 row)."""
        if imp.name is None or imp.name == "*":
            return frozenset()
        head = _absolute_module(imp.level, imp.module,
                                self._packages[rel])
        if not head:
            return frozenset()
        if f"{head}.{imp.name}" in self._mod_paths:
            return self._walk_module(f"{head}.{imp.name}", ())
        return frozenset(C.selection_name_key(path, imp.name)
                        for path in self._mod_paths.get(head, ()))

    def _walk_module(self, module: str,
                     rest: tuple[str, ...]) -> frozenset[str]:
        if not rest:
            return frozenset(
                C.selection_name_key(path, module.split(".")[-1])
                for path in self._mod_paths.get(module, ()))
        part = rest[0]
        deeper = f"{module}.{part}"
        if deeper in self._mod_paths:
            return self._walk_module(deeper, rest[1:])
        return frozenset(C.selection_name_key(path, part)
                        for path in self._mod_paths.get(module, ()))

    def resolve(self, rel: str, chain: tuple[str, ...]) -> frozenset[str]:
        """Resolve one raw dotted chain to name keys (empty drops)."""
        head, rest = chain[0], chain[1:]
        binding = self._bindings[rel].get(head)
        if binding is None:
            out: set[str] = set()
            for star in self._stars[rel]:
                target = _absolute_module(star.level, star.module,
                                          self._packages[rel])
                if not target:
                    continue
                for path in self._mod_paths.get(target, ()):
                    if head in self.bound_names(path):
                        out.add(C.selection_name_key(path, head))
            return frozenset(out)
        if binding[0] == "local":
            return frozenset((C.selection_name_key(rel, head),))
        imp = binding[1]
        if imp.name is None:
            if imp.aliased:
                return self._walk_module(imp.module, rest)
            first = imp.module.split(".")[0]
            return self._walk_module(first, rest)
        return self._from_rest(rel, imp, rest)

    def _from_rest(self, rel: str, imp: C.ImportIndex,
                   rest: tuple[str, ...]) -> frozenset[str]:
        head = _absolute_module(imp.level, imp.module,
                                self._packages[rel])
        if not head or imp.name == "*":
            return frozenset()
        full = f"{head}.{imp.name}"
        if full in self._mod_paths:
            return self._walk_module(full, rest)
        # Attribute of an imported name: the key is the binding itself.
        return frozenset(C.selection_name_key(path, imp.name)
                        for path in self._mod_paths.get(head, ()))

    def bound_names(self, rel: str) -> frozenset[str]:
        """Every module-level name a file binds (star expansion, guarded)."""
        if rel in self._memo:
            return self._memo[rel]
        seen: set[str] = set()
        out = self._bound_inner(rel, seen)
        self._memo[rel] = out
        return out

    def _bound_inner(self, rel: str, seen: set[str]) -> frozenset[str]:
        if rel in seen:
            return frozenset()
        seen.add(rel)
        out = set(self._bindings.get(rel, ()))
        for star in self._stars.get(rel, []):
            target = _absolute_module(star.level, star.module,
                                      self._packages.get(rel, ""))
            if not target:
                continue
            for path in self._mod_paths.get(target, ()):
                out |= set(self._bound_inner(path, seen))
        return frozenset(out)


def build_project_index(project_root: Path, config: C.Config, *,
                        key: bytes | None, cache: C.ParseCache | None,
                        conftest_edges: bool = True) -> C.ProjectIndex:
    """Index the project once: one cache get_many, at most one put_many."""
    root = Path(project_root)
    test_roots = tuple(config.runner.test_roots)
    rels = list(iter_python_files(root))
    if len(rels) > _impact.MAX_SCAN_FILES:
        # Past the cap no digest is computed, but the single get_many call
        # still happens so one build is one cache round trip.
        if key is not None and cache is not None:
            cache.get_many([])
        return C.ProjectIndex(root=root, files={}, test_files=frozenset(),
                              reverse={}, unparsed=frozenset(),
                              complete=False)

    raws = {rel: read_source(root, rel) for rel in rels}
    digests = {rel: (C.selection_file_digest(key, raw)
                     if key is not None and raw is not None else "")
               for rel, raw in raws.items()}

    indexes: dict[str, C.FileIndex] = {}
    if key is not None and cache is not None:
        wanted = sorted({digest for digest in digests.values() if digest})
        found = dict(cache.get_many(wanted) or {})
        decoded: dict[str, C.FileIndex | None] = {}
        for digest in wanted:
            blob = found.get(digest)
            decoded[digest] = decode_index(blob) \
                if blob is not None else None
        fresh: dict[str, bytes] = {}
        per_digest: dict[str, C.FileIndex] = {}
        one_rel = {}
        for rel in rels:
            one_rel.setdefault(digests[rel], rel)
        for digest in wanted:
            index = decoded[digest]
            if index is None:
                raw = raws[one_rel[digest]]
                index = index_source(raw) if raw is not None else _unparsed()
                fresh[digest] = encode_index(index)
            per_digest[digest] = index
        if fresh:
            cache.put_many(fresh)
        for rel in rels:
            digest = digests[rel]
            if digest:
                indexes[rel] = per_digest[digest]
            else:
                raw = raws[rel]
                indexes[rel] = index_source(raw) \
                    if raw is not None else _unparsed()
    else:
        for rel in rels:
            raw = raws[rel]
            indexes[rel] = index_source(raw) \
                if raw is not None else _unparsed()

    modules_of = {rel: _impact._module_names(root, rel) for rel in rels}
    resolver = _Resolver(rels, indexes, modules_of)

    files: dict[str, C.ProjectFile] = {}
    path_imports: dict[str, frozenset[str]] = {}
    for rel in rels:
        hit: set[str] = set()
        for name in _edge_names(indexes[rel].imports,
                               resolver._packages[rel]):
            hit.update(resolver.module_paths(name))
        path_imports[rel] = frozenset(hit)

    for rel in rels:
        index = indexes[rel]
        test = _impact._is_test_file(rel, test_roots)
        scope_refs = {
            scope.qualname: frozenset(
                key for chain in scope.refs
                for key in resolver.resolve(rel, chain))
            for scope in index.scopes}
        class_refs = {
            entry.qualname: frozenset(
                key for chain in entry.refs
                for key in resolver.resolve(rel, chain))
            for entry in index.classes}
        by_statement: dict[int, list[C.ImportIndex]] = {}
        for imp in index.imports:
            if imp.statement >= 0:
                by_statement.setdefault(imp.statement, []).append(imp)
        statement_refs: list[frozenset[str]] = []
        statement_bound: list[frozenset[str]] = []
        for position, stmt in enumerate(index.statements):
            if stmt.kind == "import":
                targets: set[str] = set()
                bound: set[str] = set()
                for imp in by_statement.get(position, []):
                    if imp.name == "*":
                        target = _absolute_module(
                            imp.level, imp.module,
                            resolver._packages[rel])
                        if target:
                            for path in resolver.module_paths(target):
                                for name in resolver.bound_names(path):
                                    if not name.startswith("_"):
                                        bound.add(
                                            C.selection_name_key(rel, name))
                                        # Design 2.3: statement_refs of an
                                        # import are the resolved targets of
                                        # its from imports; a star resolves
                                        # to path(M):name for every name M
                                        # binds, so propagation can flow
                                        # through the re-export.
                                        targets.add(
                                            C.selection_name_key(
                                                path, name))
                    elif imp.name is not None:
                        targets.update(resolver.from_target(rel, imp))
                        if imp.local is not None:
                            bound.add(C.selection_name_key(rel, imp.local))
                    elif imp.local is not None:
                        bound.add(C.selection_name_key(rel, imp.local))
                statement_refs.append(frozenset(targets))
                statement_bound.append(frozenset(bound))
            elif stmt.kind in ("def", "class", "assign", "effect"):
                # Design 4.4: effect statements (mutating and mixed-target
                # assigns) resolve their refs like any other statement, so
                # an upstream change can match them and propagate through
                # AM; stmt.bound holds only the plain Name targets (possibly
                # none), which stay bound conservatively as before.
                statement_refs.append(frozenset(
                    key for chain in stmt.refs
                    for key in resolver.resolve(rel, chain)))
                statement_bound.append(frozenset(
                    C.selection_name_key(rel, name) for name in stmt.bound))
            else:
                # "doc": no refs, no bindings.
                statement_refs.append(frozenset())
                statement_bound.append(frozenset())
        files[rel] = C.ProjectFile(
            path=rel, digest=digests[rel], index=index,
            modules=modules_of[rel], test=test,
            support=not test and _impact._is_test_support(rel, test_roots),
            imports=path_imports[rel], scope_refs=scope_refs,
            class_refs=class_refs,
            statement_refs=tuple(statement_refs),
            statement_bound=tuple(statement_bound))

    test_files = frozenset(rel for rel, item in files.items() if item.test)
    reverse: dict[str, set[str]] = {rel: set() for rel in rels}
    for rel in rels:
        for imported in path_imports[rel]:
            reverse[imported].add(rel)
    if conftest_edges:
        for rel in rels:
            if rel.rsplit("/", 1)[-1] != "conftest.py":
                continue
            home = rel.rpartition("/")[0]
            for candidate in test_files:
                if candidate != rel and (
                        home == "" or candidate.startswith(home + "/")):
                    reverse[rel].add(candidate)
    return C.ProjectIndex(
        root=root, files=files, test_files=test_files,
        reverse={path: frozenset(importers)
                for path, importers in reverse.items()},
        unparsed=frozenset(rel for rel in rels if not indexes[rel].parsed),
        complete=True)


def missing_seed_importers(index: C.ProjectIndex, project_root: Path,
                            seeds: tuple[str, ...] | list[str]) -> dict[str, set[str]]:
    """Reverse edges for seeds absent from the index (deleted files).

    A deleted seed keeps its 0.4 reachability through its module names:
    every indexed file whose import edges mention one maps back to it.
    """
    root = Path(project_root)
    out: dict[str, set[str]] = {}
    for seed in seeds:
        if seed in index.files:
            continue
        names = set(_impact._module_names(root, seed))
        if not names:
            continue
        importers: set[str] = set()
        for rel, item in index.files.items():
            if _edge_names(item.index.imports,
                           _impact._package_name(rel)) & names:
                importers.add(rel)
        if importers:
            out[seed] = importers
    return out
