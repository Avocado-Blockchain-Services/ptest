"""Evaluation harness for dependency-recorded test selection (spec 6.7).

Samples seeded deterministic mutants across the eight mutation classes,
plans v1 (static) and v2 (dynamic) for each mutant in a scratch git
worktree, runs the ground truth through the branch's ptest, and reports
misses classified by the spec section 9 residual risks (R1-R9).

Planning needs the merged tree (v1: impact.plan with conftest_edges
from T1; v2: selection_engine.preview from T1-T5); config/domain
loading, the subprocess ground-truth/baseline runs and the store
backup/restore all use base-tree APIs.

Isolation: every scratch worktree gets a campaign-private ``project_id``
(``campaign_project_id``), so planning, recording and the store backup
and restore touch only ``projects/<campaign id>/selection.db``. The
project's real store, shared by every checkout and agent, is never read
or written, while runs still wait in the machine-wide queue. A seed
``--full`` run records the pristine tree before the first mutant. Every pure-logic unit in this
module is stdlib-only and covered by tests/ng/test_selection_eval.py.
The A1-A5 campaign itself is never executed in tests.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

MUTATION_CLASSES = (
    "raise-entry",
    "comparison-flip",
    "constant-change",
    "attr-change",
    "default-change",
    "decorator-change",
    "import-change",
    "data-change",
)

DATA_SUFFIXES = frozenset({
    ".json", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini",
    ".csv", ".md",
})

_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "htmlcov", ".hypothesis", "build", "dist",
})

_COMPARE_FLIP = {
    "Eq": "!=",
    "NotEq": "==",
    "Lt": ">",
    "Gt": "<",
    "LtE": ">=",
    "GtE": "<=",
    "Is": "is not",
    "IsNot": "is",
    "In": "not in",
    "NotIn": "in",
}

_MISS_PRIORITY = (
    ("R1", ("nondeterministic", "order_dependent", "shared_state")),
    ("R2", ("dynamic_name",)),
    ("R3", ("import_side_effect",)),
    ("R4", ("spawned_binary",)),
    ("R5", ("recorder_tamper",)),
    ("R6", ("py_as_data",)),
    ("R7", ("installed_package",)),
    ("R8", ("line_number_dependent",)),
    ("R9", ("background_thread",)),
)

#: Site classes that are module-level (skeleton) changes in spec terms.
#: R2 (dynamic references to changed module-level names) only applies to
#: these; a function-body miss can never be an R2 no matter what tokens
#: appear elsewhere in the module.
_MODULE_LEVEL_SITE_CLASSES = frozenset({
    "constant-change",
    "attr-change",
    "import-change",
})


@dataclass(frozen=True, slots=True)
class Site:
    site_class: str
    path: str
    lineno: int
    name: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Mutant:
    mutant_id: str
    site: Site


@dataclass(frozen=True, slots=True)
class MutantRecord:
    mutant_id: str
    site_class: str
    path: str
    lineno: int
    v1_files: tuple = ()
    v2_files: tuple = ()
    v1_is_full: bool = False
    full_files: int = 0
    failed: tuple = ()
    baseline_failed: tuple = ()
    misses: tuple = ()
    miss_classes: tuple = ()
    # Whole-file keyword hits on the mutated module, demoted to hints:
    # they never classify, they only suggest where a human should look.
    suggestions: tuple = ()
    planning_ms: Mapping = field(default_factory=dict)
    # Selection sizes in tests (v1 selects whole files: their recorded
    # test counts; v2 is node-level), the v2 engine, its static reason
    # when it fell back, and its -v detail lines for diagnosis.
    v1_tests: int = 0
    v2_tests: int = 0
    total_tests: int = 0
    v2_engine: str = ""
    v2_reason: str = ""
    v2_details: tuple = ()
    # Set when the mutant could not be evaluated (its other fields are
    # empty); reported, never counted as a pass.
    error: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    seed: int
    mutants: tuple = ()
    overhead_ms: Mapping = field(default_factory=dict)


def _iter_functions(tree_node):
    for node in ast.walk(tree_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _single_line(node):
    return node.end_lineno is not None and node.end_lineno == node.lineno


def is_test_file(path: str) -> bool:
    """A test module (``test_*.py`` / ``*_test.py``); it always runs when
    changed, under either planner, so mutating it proves nothing."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py"))


def collect_sites(tree: Mapping[str, str]) -> list[Site]:
    """Collect one site per mutation opportunity, sorted deterministically.

    Test modules are skipped; conftest, factories and other test support
    stay, since a change there must reach the tests that use it.
    """
    sites: list[Site] = []
    for path in sorted(tree):
        if is_test_file(path):
            continue
        text = tree[path]
        if path.endswith(".py"):
            try:
                module = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            sites.extend(_code_sites(path, module))
        elif Path(path).suffix in DATA_SUFFIXES:
            sites.append(Site("data-change", path, 1, Path(path).name))
    sites.sort(key=lambda site: (
        site.site_class, site.path, site.lineno, site.name, site.detail))
    return sites


def _code_sites(path, module):
    sites: list[Site] = []
    counters: dict = {}

    def claim(site_class, lineno, name):
        key = (site_class, lineno, name)
        index = counters.get(key, 0)
        counters[key] = index + 1
        sites.append(Site(site_class, path, lineno, name, str(index)))

    for node in _iter_functions(module):
        claim("raise-entry", node.lineno, node.name)
        defaults = _default_entries(node)
        for index in range(len(defaults)):
            claim("default-change", node.lineno, node.name)
        for _ in _decorator_constant_args(node):
            claim("decorator-change", node.lineno, node.name)
    for node in ast.walk(module):
        if isinstance(node, ast.Compare) and _single_line(node):
            claim("comparison-flip", node.lineno, "compare")
        elif (isinstance(node, ast.BoolOp)
                and isinstance(node.op, (ast.And, ast.Or))
                and _single_line(node)):
            claim("comparison-flip", node.lineno, "boolean")
    for node in module.body:
        if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and _single_line(node)
                and _target_name(node) is not None):
            claim("constant-change", node.lineno, _target_name(node))
        elif isinstance(node, ast.ClassDef):
            for entry in node.body:
                if (isinstance(entry, (ast.Assign, ast.AnnAssign))
                        and _single_line(entry)
                        and _target_name(entry) is not None):
                    claim("attr-change", entry.lineno,
                          f"{node.name}.{_target_name(entry)}")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _single_line(node):
            claim("import-change", node.lineno, "import")
    return sites


def _target_name(node):
    if isinstance(node, ast.Assign):
        targets = node.targets
        if len(targets) == 1 and isinstance(targets[0], ast.Name):
            return targets[0].id
        return None
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.value is not None:
            return node.target.id
        return None
    return None


def _default_entries(node):
    """Ordered (argname, default) pairs matching collection order."""
    entries = []
    positional = node.args.args
    for arg, default in zip(
            positional[len(positional) - len(node.args.defaults):],
            node.args.defaults):
        entries.append((arg.arg, default))
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is not None:
            entries.append((arg.arg, default))
    return entries


def _decorator_constant_args(node):
    found = []
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and _single_line(decorator):
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and _single_line(arg):
                    found.append(arg)
    return found


def sample_mutants(sites: list[Site], count: int, seed: int) -> list[Mutant]:
    """Seeded deterministic sampling, stratified over the operator classes.

    Classes take turns, so a class with many sites (comparisons) cannot
    crowd out the rare ones (data, decorators); a class that runs out of
    sites leaves its turns to the others.
    """
    rng = random.Random(seed)
    pools = []
    for name in MUTATION_CLASSES:
        pool = [site for site in sites if site.site_class == name]
        rng.shuffle(pool)
        pools.append(pool)
    picked: list[Site] = []
    while len(picked) < count and any(pools):
        for pool in pools:
            if pool and len(picked) < count:
                picked.append(pool.pop())
    return [Mutant(f"m{index + 1:04d}", site)
            for index, site in enumerate(picked)]


def _offsets(lines):
    offsets = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    offsets.append(total)
    return offsets


def _splice(source, start, end, replacement):
    return source[:start] + replacement + source[end:]


def _char_col(line, byte_col):
    """Translate an AST byte offset (UTF-8) to a character offset.

    AST col_offset/end_col_offset count UTF-8 bytes, while our source is
    a str: adding them to character offsets splices in the wrong place
    on any line with non-ASCII text before or inside the node.
    """
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


def _node_span(lines, node):
    offsets = _offsets(lines)
    start = offsets[node.lineno - 1] + _char_col(
        lines[node.lineno - 1], node.col_offset)
    end = offsets[node.end_lineno - 1] + _char_col(
        lines[node.end_lineno - 1], node.end_col_offset)
    return start, end


_MISSING = object()


def _bump(segment):
    """Bump one literal site to a different, still-valid expression.

    The replacement is always a bare expression, never a trailing
    comment: a ``#`` would swallow the rest of the line (``; Y = 2``,
    ``, b=3``) and produce a SyntaxError. Non-scalar literals become
    ``None``; a ``None`` literal becomes ``True`` so the mutant is
    never a semantic no-op.
    """
    try:
        value = ast.literal_eval(segment)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        value = _MISSING
    if isinstance(value, bool):
        return "False" if value else "True"
    if isinstance(value, int):
        return str(value + 1)
    if isinstance(value, float):
        return repr(value + 1.0)
    if isinstance(value, str):
        return repr(value + "-mutant")
    if value is None:
        return "True"
    return "None"


def diff_edits(before: str, after: str) -> int:
    """Count changed lines between two texts (single-site check).

    Unified-diff hunk headers merge nearby changes and a single
    SequenceMatcher opcode can span a replacement plus an adjacent
    insertion, so both undercount; summing max(deleted, inserted) per
    non-equal opcode counts every touched line instead. Every operator
    in this harness touches exactly one line.
    """
    import difflib
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines())
    return sum(max(i2 - i1, j2 - j1)
               for tag, i1, i2, j1, j2 in matcher.get_opcodes()
               if tag != "equal")


def check_mutant(source: str, mutated: str) -> str:
    """Validate a mutant before use: parses, differs, exactly one site.

    Raises RuntimeError instead of letting a broken mutant silently
    skew the campaign numbers.
    """
    if mutated == source:
        raise RuntimeError("mutant is a semantic no-op (no text changed)")
    try:
        ast.parse(mutated)
    except (SyntaxError, ValueError) as exc:
        raise RuntimeError(f"mutant does not parse: {exc}") from exc
    if diff_edits(source, mutated) != 1:
        raise RuntimeError("mutant changes more than one site")
    return mutated


def _matching(sites, site):
    same = [entry for entry in sites if entry == site]
    if len(same) != 1:
        raise ValueError(f"mutation site is not unique: {site!r}")
    return same[0]


def apply_mutation(source: str, site: Site) -> str:
    """Apply one mutation operator; deterministic and single-sited."""
    if site.site_class == "data-change":
        marker = "ptest-eval-mutant\n"
        if source.endswith("\n"):
            return source + marker
        return source + "\n" + marker
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    if site.site_class == "raise-entry":
        candidates = [node for node in _iter_functions(module)
                      if node.lineno == site.lineno and node.name == site.name]
        node = candidates[int(site.detail)]
        first = node.body[0]
        if first.lineno == node.lineno:
            indent = " " * (node.col_offset + 4)
            at = _offsets(lines)[node.lineno]
        else:
            indent = lines[first.lineno - 1][:len(lines[first.lineno - 1])
                                             - len(lines[first.lineno - 1].lstrip())]
            at = _offsets(lines)[first.lineno - 1]
        return _splice(source, at, at,
                       f'{indent}raise RuntimeError("ptest-eval-mutant")\n')
    if site.site_class == "comparison-flip":
        matches = []
        for node in ast.walk(module):
            if getattr(node, "lineno", None) != site.lineno:
                continue
            if isinstance(node, ast.Compare) and _single_line(node):
                matches.append(node)
            elif (isinstance(node, ast.BoolOp)
                    and isinstance(node.op, (ast.And, ast.Or))
                    and _single_line(node)):
                matches.append(node)
        matches.sort(key=lambda node: node.col_offset)
        node = matches[int(site.detail)]
        start, end = _node_span(lines, node)
        segment = source[start:end]
        if isinstance(node, ast.BoolOp):
            old, new = (" and ", " or ") if isinstance(node.op, ast.And) else (" or ", " and ")
            return _splice(source, start, end, segment.replace(old, new, 1))
        old = _compare_symbol(node.ops[0])
        new = _COMPARE_FLIP[type(node.ops[0]).__name__]
        return _splice(source, start, end, _replace_op(segment, old, new))
    if site.site_class in ("constant-change", "attr-change"):
        targets = []
        if site.site_class == "constant-change":
            scope = module.body
        else:
            scope = next(node.body for node in ast.walk(module)
                         if isinstance(node, ast.ClassDef)
                         and site.name.startswith(node.name + "."))
        for node in scope:
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and node.lineno == site.lineno
                    and _target_name(node) == site.name.split(".")[-1]):
                targets.append(node)
        node = targets[int(site.detail)]
        value = node.value
        start, end = _node_span(lines, value)
        return _splice(source, start, end, _bump(source[start:end]))
    if site.site_class == "default-change":
        for node in _iter_functions(module):
            if node.lineno == site.lineno and node.name == site.name:
                entries = _default_entries(node)
                _matching(entries, entries[int(site.detail)])
                _arg, default = entries[int(site.detail)]
                start, end = _node_span(lines, default)
                return _splice(source, start, end, _bump(source[start:end]))
        raise ValueError(f"no such function: {site!r}")
    if site.site_class == "decorator-change":
        for node in _iter_functions(module):
            if node.lineno == site.lineno and node.name == site.name:
                args = _decorator_constant_args(node)
                target = args[int(site.detail)]
                start, end = _node_span(lines, target)
                return _splice(source, start, end, _bump(source[start:end]))
        raise ValueError(f"no such function: {site!r}")
    if site.site_class == "import-change":
        matches = [node for node in module.body
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   and node.lineno == site.lineno]
        node = matches[int(site.detail)]
        start, end = _node_span(lines, node)
        return _splice(source, start, end, _rewrite_import(node))
    raise ValueError(f"unknown mutation class: {site.site_class}")


def _replace_op(segment, old, new):
    if re.fullmatch(r"[A-Za-z ]+", old):
        return re.sub(r"(?<![A-Za-z0-9_.])" + re.escape(old)
                      + r"(?![A-Za-z0-9_])", new, segment, count=1)
    return segment.replace(old, new, 1)


def _compare_symbol(op):
    return {"Eq": "==", "NotEq": "!=", "Lt": "<", "Gt": ">",
            "LtE": "<=", "GtE": ">=", "Is": "is", "IsNot": "is not",
            "In": "in", "NotIn": "not in"}[type(op).__name__]


def _rewrite_import(node):
    if isinstance(node, ast.ImportFrom):
        prefix = "." * node.level + (node.module or "")
        names = list(node.names)
        if len(names) > 1:
            kept = ", ".join(
                alias.name + (f" as {alias.asname}" if alias.asname else "")
                for alias in names[:-1])
            return f"from {prefix} import {kept}"
        alias = names[0]
        return (f"from {prefix} import {alias.name} "
                f"as {alias.asname or '_ptest_eval_alias'}")
    names = list(node.names)
    if len(names) > 1:
        kept = ", ".join(
            alias.name + (f" as {alias.asname}" if alias.asname else "")
            for alias in names[:-1])
        return f"import {kept}"
    alias = names[0]
    return f"import {alias.name} as {alias.asname or '_ptest_eval_alias'}"


def ground_truth_target(v1_files, v1_is_full, affordable):
    """Union of v1/v2 files, or full when v1 is full and affordable."""
    if v1_is_full and affordable:
        return ("full", ())
    return ("union", tuple(v1_files))


def classify_miss(indicators: Mapping[str, bool], *,
                  site_class: str | None = None) -> str:
    """Classify a miss by spec section 9 (R1-R9) or 'unclassified'.

    ``indicators`` must be signals derived from the missed test and its
    recorded dependencies (see :func:`signals_for_miss`), never keyword
    hits across the whole mutated module. Each R class additionally
    requires its spec precondition: R3 only for an import-line change,
    R2 only for a module-level (skeleton) change. Anything else is
    'unclassified' and blocks the release; whole-file keyword hits are
    reported as suggestions only (see ``MutantRecord.suggestions``).
    """
    for code, keys in _MISS_PRIORITY:
        if code == "R3" and site_class != "import-change":
            continue
        if code == "R2" and site_class not in _MODULE_LEVEL_SITE_CLASSES:
            continue
        if any(indicators.get(key) for key in keys):
            return code
    return "unclassified"


def _changed_line(original: str, mutated: str) -> str:
    """The added line(s) of a validated single-edit mutant.

    This is the dependency edge that actually fired: the test failed
    because of this change, so R signals derived from it describe the
    miss. Incidental tokens elsewhere in the module are ignored.
    """
    import difflib
    added = [line[2:] for line in difflib.ndiff(
        original.splitlines(), mutated.splitlines())
        if line.startswith("+ ") and not line.startswith("+++")]
    return "\n".join(added)


def _test_function_source(scratch_root: Path, nodeid: str) -> str:
    """Source of the missed test function; "" when it cannot be read.

    Unknown test locations yield no signals (fail towards
    'unclassified') rather than whole-file guesses.
    """
    path, _, rest = nodeid.partition("::")
    if not path or not rest:
        return ""
    name = rest.split("::")[-1].split("[", 1)[0]
    try:
        text = (Path(scratch_root) / path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return ""
    for node in ast.walk(module):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
                and node.end_lineno is not None):
            lines = text.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1:node.end_lineno])
    return ""


def signals_for_miss(scratch_root: Path, nodeid: str,
                     original: str, mutated: str) -> dict:
    """R signals for one miss: missed test function + mutant changed line.

    The mutated module is a recorded dependency of the miss (the test
    fails because of this change); only the changed line counts, so an
    incidental ``getattr(`` or ``global`` elsewhere in the file cannot
    launder a real selection bug into an R class.
    """
    merged: dict[str, bool] = {}
    for signals in (indicators_for(_changed_line(original, mutated)),
                    indicators_for(_test_function_source(scratch_root,
                                                         nodeid))):
        for key, value in signals.items():
            merged[key] = bool(merged.get(key) or value)
    return merged


def find_misses(mutant_failed, baseline_failed, selected) -> tuple:
    """Miss: fails under the mutant, passes in the baseline, not selected."""
    baseline = set(baseline_failed)
    chosen = set(selected)
    return tuple(sorted(node for node in mutant_failed
                        if node not in baseline and node not in chosen))


def median(values) -> float:
    return statistics.median(values)


def render_json(report: EvalReport) -> str:
    payload = {
        "seed": report.seed,
        "mutants": [
            {
                "mutant_id": record.mutant_id,
                "site_class": record.site_class,
                "path": record.path,
                "lineno": record.lineno,
                "v1_files": list(record.v1_files),
                "v2_files": list(record.v2_files),
                "v1_is_full": record.v1_is_full,
                "full_files": record.full_files,
                "failed": list(record.failed),
                "baseline_failed": list(record.baseline_failed),
                "misses": list(record.misses),
                "miss_classes": list(record.miss_classes),
                "suggestions": list(record.suggestions),
                "planning_ms": dict(record.planning_ms),
                "v1_tests": record.v1_tests,
                "v2_tests": record.v2_tests,
                "total_tests": record.total_tests,
                "v2_engine": record.v2_engine,
                "v2_reason": record.v2_reason,
                "v2_details": list(record.v2_details),
                "error": record.error,
            }
            for record in report.mutants
        ],
        "overhead_ms": {key: list(value)
                        for key, value in dict(report.overhead_ms).items()},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_markdown(report: EvalReport) -> str:
    lines = [f"# selection evaluation (seed {report.seed})", ""]
    total_misses = sum(len(record.misses) for record in report.mutants)
    errors = sum(1 for record in report.mutants if record.error)
    lines.append(f"{len(report.mutants)} mutants, {total_misses} misses, "
                 f"{errors} not evaluated.")
    lines.append("Miss classes follow spec section 9 residual risks R1-R9; "
                 "anything else is unclassified and blocks release.")
    lines.append("")
    lines.append("| mutant | class | path | v1 files | v2 files | full | "
                 "v1 tests | v2 tests | failed | miss | miss class | "
                 "planning ms (v1/v2) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for record in report.mutants:
        miss = ",".join(record.misses) if record.misses else "none"
        classes = ",".join(record.miss_classes) if record.miss_classes else "-"
        if ("unclassified" in (record.miss_classes or ())
                and record.suggestions):
            classes += f" (suggests: {','.join(record.suggestions)})"
        planning = record.planning_ms or {}
        plan_ms = f"{planning.get('v1', '-')}/{planning.get('v2', '-')}"
        if record.error:
            miss = f"not evaluated: {record.error.splitlines()[0][:80]}"
        lines.append(f"| {record.mutant_id} | {record.site_class} | "
                     f"{record.path}:{record.lineno} | {len(record.v1_files)} | "
                     f"{len(record.v2_files)} | {record.full_files} | "
                     f"{record.v1_tests} | {record.v2_tests} | "
                     f"{len(record.failed)} | {miss} | {classes} | {plan_ms} |")
    lines.append("")
    if report.overhead_ms:
        lines.append("## recording overhead (median of 3, ms)")
        for key in sorted(dict(report.overhead_ms)):
            lines.append(f"- {key}: {median(report.overhead_ms[key])}")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Seeded mutation evaluation for ptest selection (spec 6.7).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--mutants", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--classes", nargs="*", default=list(MUTATION_CLASSES),
                        choices=list(MUTATION_CLASSES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--benchmark-overhead", action="store_true")
    parser.add_argument("--ptest", default="uv run ptest")
    parser.add_argument("--truth", choices=("full", "union"), default="full",
                        help="ground truth: the whole suite (default), or "
                             "the union of the v1 and v2 selections")
    return parser.parse_args(argv)


def read_tree_files(root: Path) -> dict:
    """Read candidate files under root (read-only, sorted, no-follow)."""
    tree = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.startswith(".") or any(
                part in _SKIP_DIRS or part.startswith(".")
                for part in rel.split("/")[:-1]):
            continue
        suffix = path.suffix
        if suffix != ".py" and suffix not in DATA_SUFFIXES:
            continue
        try:
            tree[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return tree


def _write_reports(out: Path, report: EvalReport) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "selection-eval.json").write_text(render_json(report), encoding="utf-8")
    (out / "selection-eval.md").write_text(render_markdown(report), encoding="utf-8")


def _dry_run(args) -> int:
    project = Path(args.project)
    tree = read_tree_files(project)
    sites = [site for site in collect_sites(tree) if site.site_class in args.classes]
    mutants = sample_mutants(sites, args.mutants, args.seed)
    records = tuple(
        MutantRecord(mutant_id=mutant.mutant_id,
                     site_class=mutant.site.site_class,
                     path=mutant.site.path, lineno=mutant.site.lineno)
        for mutant in mutants)
    _write_reports(Path(args.out), EvalReport(seed=args.seed, mutants=records))
    for mutant in mutants:
        print(f"{mutant.mutant_id} {mutant.site.site_class} "
              f"{mutant.site.path}:{mutant.site.lineno}")
    return 0


# --- Full campaign (never executed in unit tests; needs the merged tree) ---

def _run(cmd, cwd, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


RESULT_EXPORT_PREFIX = "selection-eval-run-"
#: One ptest run may wait in the machine-wide queue behind other work.
_RUN_TIMEOUT_S = 4 * 3600


def ground_truth_argv(ptest_cmd: str, files: tuple, export_name: str,
                      full: bool, scope: tuple = ()) -> list:
    """Build ``ptest --result-json NAME <files>`` (or ``--full --again``) argv.

    Options must precede the test paths: ptest's ``_parse_execution``
    treats the first non-option token as the start of the runner tail,
    so a trailing ``--result-json`` would be passed through to pytest
    (which rejects it) instead of producing the export.
    """
    base = shlex.split(ptest_cmd)
    if full:
        # --again: a pristine checkout of an already-passed tree would be
        # skipped as "already verified"; --again forces the fresh run.
        # ``scope`` names a monorepo child so a full run never spreads to
        # its siblings; --again takes no path, and a scoped child never
        # reuses a green here because the campaign project id is fresh.
        if scope:
            return base + ["--full", "--result-json", export_name,
                           *list(scope)]
        return base + ["--full", "--again", "--result-json", export_name]
    return base + ["--result-json", export_name, *list(files)]


def read_result_export(path: Path) -> dict:
    """Read a ``--result-json`` export; fail loud on missing/garbled data."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"ground truth has no readable result export at {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"ground truth result export at {path} is not a JSON object")
    return payload


def pytest_lastfailed(scratch: Path) -> tuple:
    """Failing node ids from pytest lastfailed caches under a worktree.

    The ground-truth and baseline runs go through ptest; pytest itself
    records the failing node ids in ``.pytest_cache/v/cache/lastfailed``
    (one per config root). Merged over every cache found, sorted.
    """
    found: set[str] = set()
    for cache in sorted(scratch.rglob(".pytest_cache/v/cache/lastfailed")):
        if not cache.is_file() or cache.is_symlink():
            continue
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            found.update(
                node for node in payload if isinstance(node, str) and node)
    return tuple(sorted(found))


def failing_nodeids(export_path: Path, scratch: Path,
                    run_failed: bool) -> tuple:
    """Failing node ids for one through-ptest run; fail loud when empty.

    A run that failed without a single identifiable failure would
    silently skew the A-numbers, so it raises instead of returning ().
    """
    read_result_export(export_path)  # validates the export exists and parses
    failed = pytest_lastfailed(scratch)
    if not failed and run_failed:
        raise RuntimeError(
            f"ptest run in {scratch} failed but recorded no failing tests")
    return failed


def file_part(nodeid: str) -> str:
    """The file portion of a ``path::test`` node id."""
    return nodeid.split("::", 1)[0]


def selected_covering(mutant_failed: tuple, selected_files) -> tuple:
    """The failing nodes covered by a file-level selection.

    Plans select files; failures are node ids. A failing test in a
    selected file was executed by the selection, so it counts as
    covered; failures in unselected files are misses.
    """
    chosen = set(selected_files)
    return tuple(node for node in mutant_failed
                 if file_part(node) in chosen)


def v2_covering(mutant_failed: tuple, plan, prefix: str) -> tuple:
    """The failing nodes the v2 plan would have run, node by node.

    v2 deselects recorded tests inside the files it selects, so a file
    match is not enough: a deselected failing test is a miss.
    """
    kind = getattr(plan, "kind", "selected")
    if kind == "full":
        return tuple(mutant_failed)
    if kind == "none":
        return ()
    files = {_scope_path(prefix, path)
             for path in (getattr(plan, "files", ()) or ())}
    skipped = {_scope_node(prefix, nodeid)
               for nodeid in (getattr(plan, "deselect", ()) or ())}
    return tuple(node for node in mutant_failed
                 if file_part(node) in files and node not in skipped)


def clear_lastfailed(scratch: Path) -> None:
    """Forget earlier runs' failures; a persistent scratch is reused."""
    for cache in scratch.rglob(".pytest_cache/v/cache/lastfailed"):
        if cache.is_file() and not cache.is_symlink():
            cache.unlink()


def run_setup(workdir: Path) -> None:
    """Run the project's own ``[setup] argv`` once in a fresh scratch, so
    its first ptest run already has the project's workers (xdist)."""
    config = _planning_config(workdir)
    setup = getattr(config, "setup", None)
    argv = list(getattr(setup, "argv", ()) or ())
    if not argv:
        return
    done = _run(argv, cwd=workdir, timeout=_RUN_TIMEOUT_S)
    if done.returncode != 0:
        tail = "\n".join(str(done.stderr or "").strip().splitlines()[-8:])
        raise RuntimeError(f"setup {argv} failed in {workdir}:\n{tail}")


def create_scratch(repo: Path, out: Path, name: str) -> Path:
    scratch = out / "scratch" / name
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    done = _run(["git", "worktree", "add", "--detach", str(scratch)], cwd=repo)
    if done.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {done.stderr.strip()}")
    return scratch


def remove_scratch(repo: Path, scratch: Path) -> None:
    _run(["git", "worktree", "remove", "--force", str(scratch)], cwd=repo)
    shutil.rmtree(scratch, ignore_errors=True)


def _planning_config(project_root: Path):
    """Load the project config via the base config resolver."""
    from ptest import config as config_mod  # noqa: lazy; stdlib-only at import
    resolution = config_mod.resolve_config(Path(project_root))
    if resolution.config is None:
        problem = resolution.problem
        detail = f": {problem}" if problem is not None else ""
        raise RuntimeError(f"planning needs a readable .ptest.toml{detail}")
    return resolution.config


def _planning_domain_config(project_root: Path):
    """Load (domain, config) via base platform + config resolvers."""
    from ptest import platform as platform_mod  # noqa: lazy; stdlib-only at import
    return platform_mod.domain_paths(None), _planning_config(project_root)


def plan_v1(top: Path, project_root: Path, changed: tuple):
    """Static plan: impact.plan with the 0.4.10 rules.

    ``changed`` holds repo-relative (top-relative) paths; ``project_root``
    is the child under evaluation (``top`` itself for a standalone
    project). ``conftest_edges=False`` is the 0.4.10 rule set — the
    merged T1 tree defaults it to True, which would enlarge the v1
    baseline and tilt the A2 precision comparison in v2's favour.
    """
    from ptest import impact  # noqa: imported lazily; stdlib-only at import
    config = _planning_config(project_root)
    return _plan_v1_call(impact, top, project_root, config, tuple(changed))


def _plan_v1_call(impact_mod, top, project_root, config, changed):
    """Call impact.plan with the 0.4.10 rule set (seam for unit tests)."""
    import inspect
    if "conftest_edges" not in inspect.signature(impact_mod.plan).parameters:
        raise RuntimeError(
            "v1 planning needs the merged T1 tree "
            "(impact.plan without conftest_edges is not 0.4.10 rules)")
    return impact_mod.plan(Path(top), Path(project_root), config,
                           tuple(changed), conftest_edges=False)


def plan_v2(top: Path, project_root: Path, changed: tuple):
    """Dynamic plan: selection_engine.preview (needs the merged T1-T5 tree)."""
    try:
        from ptest import selection_engine  # noqa: needs the merged tree
    except ImportError as exc:
        raise RuntimeError(
            "plan v2 needs the merged T1-T5 tree "
            "(ptest.selection_engine.preview is absent)") from exc
    domain, config = _planning_domain_config(project_root)
    return selection_engine.preview(domain, Path(top), Path(project_root),
                                    config, tuple(changed))


def campaign_project_id(project: Path, seed: int, nonce: str = "") -> str:
    """The campaign-private 32-hex project id.

    ``nonce`` makes each invocation fresh, so no green or verified result
    from an earlier campaign can be reused and skip a run.
    """
    text = f"selection-eval:{Path(project).resolve()}:{int(seed)}:{nonce}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def overlay_project_id(config_path: Path, project_id: str) -> bool:
    """Point a scratch config at the campaign store; False when absent.

    Only a scratch worktree's ``.ptest.toml`` is ever rewritten. A config
    without ``project_id`` cannot be isolated and fails loud.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", project_id):
        raise ValueError("campaign project id must be 32 hex")
    if not config_path.is_file() or config_path.is_symlink():
        return False
    text = config_path.read_text(encoding="utf-8")
    updated, count = re.subn(r'(?m)^project_id\s*=.*$',
                             f'project_id = "{project_id}"', text)
    if count != 1:
        raise RuntimeError(
            f"{config_path} needs exactly one project_id to isolate")
    config_path.write_text(updated, encoding="utf-8")
    return True


def store_db_path(project_id: str) -> Path:
    """``<state>/projects/<id>/selection.db`` without creating it."""
    from ptest import contracts as contracts_mod  # noqa: lazy import
    from ptest import platform as platform_mod  # noqa: lazy import
    domain = platform_mod.domain_paths(None)
    return Path(contracts_mod.selection_store_path(domain.root, project_id))


def tests_per_file(project_id: str) -> dict:
    """Recorded tests per test file in the campaign store (seed state)."""
    from ptest import platform as platform_mod  # noqa: lazy import
    from ptest import selection_store  # noqa: lazy import
    store = selection_store.open_store(platform_mod.domain_paths(None),
                                       project_id, create=False)
    try:
        counts: dict = {}
        for node in store.snapshot().nodes.values():
            counts[node.test_file] = counts.get(node.test_file, 0) + 1
        return counts
    finally:
        store.close()


def selection_tests(plan, counts: Mapping, *, dynamic: bool) -> int:
    """Tests a plan runs: everything when full, node-level for dynamic
    plans, every recorded test of each selected file otherwise."""
    total = sum(counts.values())
    if getattr(plan, "kind", "selected") == "full":
        return total
    if getattr(plan, "kind", "selected") == "none":
        return 0
    if dynamic and getattr(plan, "engine", "") == "dynamic":
        return int(getattr(plan, "tests", 0) or 0)
    return sum(counts.get(path, 0)
               for path in (getattr(plan, "files", ()) or ()))


def _journal(store_db: Path) -> Path:
    return store_db.with_name(store_db.name + "-journal")


def backup_store(store_db: Path, backup_dir: Path) -> Path | None:
    """Copy the campaign store aside; None when there is nothing to keep."""
    if not store_db.is_file() or store_db.is_symlink():
        return None
    if _journal(store_db).exists():
        raise RuntimeError(f"{store_db} has a live journal; not copying it")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (store_db.name + ".bak")
    shutil.copy2(store_db, backup)
    return backup


def restore_store(store_db: Path, backup: Path | None) -> None:
    """Restore the campaign store from backup_store; none removes it.

    A stale journal beside a replaced database would be replayed into it
    as a hot journal, so it goes first. Only ever called on the
    campaign-private store (no other process knows its project id).
    """
    journal = _journal(store_db)
    if journal.is_file() and not journal.is_symlink():
        journal.unlink()
    if backup is None:
        if store_db.is_file() and not store_db.is_symlink():
            store_db.unlink()
        return
    store_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, store_db)


def isolate_scratch(scratch: Path, prefix: str, project_id: str,
                    source: Path | None = None) -> Path:
    """The scratch project dir, its config pointed at the campaign store.

    ``source`` is the real project dir: an untracked ``.env`` beside the
    repo root or the project is symlinked in (shared, never copied).
    """
    workdir = scratch if not prefix else scratch / prefix
    overlay_project_id(workdir / ".ptest.toml", project_id)
    if source is not None:
        source = Path(source)
        top = source
        for _ in prefix.split("/") if prefix else ():
            top = top.parent
        for origin, target in ((top / ".env", scratch / ".env"),
                               (source / ".env", workdir / ".env")):
            if (origin.is_file() and not target.exists()
                    and not target.is_symlink()):
                target.symlink_to(origin)
    return workdir


def overlay_dynamic(config_path: Path, value: bool) -> bytes:
    """Set [selection] dynamic in a scratch worktree; returns prior bytes."""
    previous = config_path.read_bytes()
    text = previous.decode("utf-8")
    wanted = "true" if value else "false"
    updated, count = re.subn(r"(?m)^dynamic\s*=.*$", f"dynamic = {wanted}", text)
    if not count:
        if "[selection]" in text:
            updated = text.replace("[selection]",
                                   f"[selection]\ndynamic = {wanted}", 1)
        else:
            updated = text.rstrip("\n") + f"\n\n[selection]\ndynamic = {wanted}\n"
    config_path.write_text(updated, encoding="utf-8")
    return previous


def benchmark_overhead(project: Path, out: Path, ptest_cmd: str,
                       project_id: str | None = None) -> dict:
    """Full run with dynamic=false vs true, back to back, 3x; medians."""
    repo = project
    # A scratch worktree checks out the whole repo at its root, so for a
    # monorepo child the benchmark targets the child's own config and
    # runs from the child dir. Overlaying the repo-root manifest would
    # append [selection] to the [monorepo] manifest, which
    # parse_monorepo_manifest rejects — every run would fail.
    prefix = _project_prefix(Path(project))
    scratch = create_scratch(repo, out, "overhead")
    try:
        workdir = scratch if not prefix else scratch / prefix
        if project_id is not None:
            isolate_scratch(scratch, prefix, project_id)
        config_path = workdir / ".ptest.toml"
        if not config_path.is_file():
            raise RuntimeError(
                f"benchmark needs a .ptest.toml at {workdir}")
        base = shlex.split(ptest_cmd) + ["--full", "--again"]
        samples = {"dynamic-false": [], "dynamic-true": []}
        for _ in range(3):
            for label, value in (("dynamic-false", False), ("dynamic-true", True)):
                previous = overlay_dynamic(config_path, value)
                try:
                    started = time.perf_counter()
                    done = _run(base, cwd=workdir)
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    if done.returncode != 0:
                        raise RuntimeError(f"benchmark run failed ({label})")
                    samples[label].append(elapsed_ms)
                finally:
                    config_path.write_bytes(previous)
        return samples
    finally:
        remove_scratch(repo, scratch)


def _has_top_level_call(module) -> bool:
    """True when importing the module executes a bare call statement."""
    return any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
               for node in module.body)


def indicators_for(mutated_text: str) -> dict:
    """Cheap static signals mapping a mutant file to R1-R9 indicators.

    Every indicator has a real detector: text patterns for runtime
    behaviours plus an AST check for import-time side effects. All
    eleven must stay implemented — a hard-coded False makes its R
    class unassignable.
    """
    try:
        module = ast.parse(mutated_text)
    except (SyntaxError, ValueError):
        module = None
    return {
        "dynamic_name": any(token in mutated_text
                            for token in ("getattr(", "import_module(",
                                          "eval(", "__import__",
                                          "globals()[", "locals()[")),
        "spawned_binary": any(token in mutated_text
                              for token in ("subprocess", "os.system",
                                            "Popen(", "os.exec")),
        "background_thread": any(token in mutated_text
                                 for token in ("threading",
                                               "concurrent.futures",
                                               "_thread")),
        "import_side_effect": bool(
            module is not None and _has_top_level_call(module)) or any(
            token in mutated_text
            for token in ("sys.modules", "os.environ", "atexit.")),
        "nondeterministic": bool(re.search(r"\brandom\b", mutated_text)) or any(
            token in mutated_text
            for token in ("os.urandom", "uuid.", "time.time", "shuffle(",
                          "secrets.", "SystemRandom")),
        "order_dependent": any(token in mutated_text
                               for token in ("global ", "globals()",
                                             "pytest.mark.order",
                                             "depends_on")),
        "shared_state": any(token in mutated_text
                            for token in ("global ", "shared_memory",
                                          "multiprocessing", "mmap",
                                          "singleton")),
        "recorder_tamper": any(token in mutated_text
                               for token in ("selection.db", "PTEST_STATE_DIR",
                                             ".deps", "selection_store",
                                             "dependency file")),
        # The open() arm requires a `.py` path inside the same call:
        # `open(` anywhere plus `.py` anywhere else fires on almost any
        # module and would mislabel real selection bugs as R6.
        "py_as_data": any(token in mutated_text
                          for token in ("ast.parse", "importlib.resources",
                                        "pkgutil", ".py'",
                                        '.py"')) or bool(re.search(
            r"open\([^()\n]*\.py", mutated_text)),
        "installed_package": any(token in mutated_text
                                 for token in ("site-packages",
                                               "importlib.metadata",
                                               "pkg_resources",
                                               "pip install")),
        "line_number_dependent": any(token in mutated_text
                                     for token in ("lineno", "co_firstlineno",
                                                   "f_lineno",
                                                   "getsourcelines",
                                                   "traceback")),
    }


def _ptest_failures(ptest_cmd: str, scratch: Path, files: tuple,
                    full: bool, label: str, scope: tuple = ()) -> tuple:
    """Run one file set through ptest and return the failing node ids."""
    export_name = f"{RESULT_EXPORT_PREFIX}{label}-{scratch.name}.json"
    done = _run(ground_truth_argv(ptest_cmd, tuple(files), export_name, full,
                                  scope=scope),
                cwd=scratch, timeout=_RUN_TIMEOUT_S)
    # ptest writes the export relative to the project it routed to: the
    # child dir for a monorepo child, the scratch root otherwise.
    export = scratch / export_name
    for candidate in (scratch / part / export_name for part in scope):
        if candidate.is_file():
            export = candidate
    try:
        return failing_nodeids(export, scratch, done.returncode != 0)
    except RuntimeError as exc:
        tail = "\n".join(str(getattr(done, "stderr", "") or "")
                         .strip().splitlines()[-12:])
        raise RuntimeError(f"{exc}; ptest exit {done.returncode}:\n{tail}") \
            from exc


def _project_prefix(project: Path) -> str:
    """Posix path of the project relative to its git toplevel.

    ``""`` for a standalone project (the project is the toplevel, or git
    is unavailable); a monorepo child such as ``services/control-plane``
    yields ``"services/control-plane"``.
    """
    # subprocess directly (not _run): tests stub _run for ptest runs,
    # and prefix detection must keep working under those stubs.
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              cwd=project, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    try:
        rel = Path(project).resolve().relative_to(
            Path(done.stdout.strip()).resolve())
    except (OSError, ValueError):
        return ""
    text = rel.as_posix()
    return "" if text == "." else text


def _scope_path(prefix: str, path: str) -> str:
    """A project-relative plan path as a repo-root ptest scope."""
    return f"{prefix}/{path}" if prefix else path


def _scope_node(prefix: str, nodeid: str) -> str:
    """A pytest node id as a repo-root scope (idempotent).

    Pytest records ``lastfailed`` ids relative to the child's rootdir
    (e.g. ``tests/test_a.py::test_1``), while v2 plan files are compared
    as repo-root scopes (``services/cp/tests/test_a.py``). Without the
    prefix, ``file_part`` never matches and every selected failure
    counts as a miss. Already-prefixed ids pass through unchanged.
    """
    if not prefix:
        return nodeid
    head, sep, tail = nodeid.partition("::")
    if head == prefix or head.startswith(prefix + "/"):
        return nodeid
    return f"{prefix}/{head}{sep}{tail}" if sep else f"{prefix}/{head}"


@dataclass
class Campaign:
    """Shared state of one campaign: two persistent scratch worktrees
    (mutants are applied to ``truth`` and reverted; ``base`` stays
    pristine), the private store and its seed backup."""
    project: Path
    out: Path
    prefix: str
    project_id: str
    truth: Path
    base: Path
    store_db: Path | None = None
    backup: Path | None = None
    seed_failed: tuple = ()
    counts: Mapping = field(default_factory=dict)

    def workdir(self, scratch: Path) -> Path:
        return scratch if not self.prefix else scratch / self.prefix

    @property
    def scope(self) -> tuple:
        return (self.prefix,) if self.prefix else ()


def _log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr,
          flush=True)


def _ptest_in(args, campaign: Campaign, scratch: Path, files: tuple,
              full: bool, label: str) -> tuple:
    """One ptest run in a persistent scratch; failing ids as repo scopes."""
    clear_lastfailed(scratch)
    failed = _ptest_failures(args.ptest, scratch, files, full, label,
                             scope=campaign.scope)
    return tuple(_scope_node(campaign.prefix, node) for node in failed)


def _run_mutant(args, campaign: Campaign, mutant: Mutant) -> MutantRecord:
    """Plan v1/v2, run the ground truth, detect and classify misses."""
    started = time.perf_counter()
    if campaign.store_db is not None:
        restore_store(campaign.store_db, campaign.backup)  # seed state
    prefix = campaign.prefix
    scratch = campaign.truth
    workdir = campaign.workdir(scratch)
    target = workdir / mutant.site.path
    original_bytes = target.read_bytes()
    try:
        original = original_bytes.decode("utf-8")
        mutated = apply_mutation(original, mutant.site)
        if mutant.site.site_class == "data-change":
            if diff_edits(original, mutated) != 1:
                raise RuntimeError("data mutant changes more than one site")
        else:
            mutated = check_mutant(original, mutated)
        target.write_text(mutated, encoding="utf-8")
        changed_top = _scope_path(prefix, mutant.site.path)
        before = time.perf_counter()
        v1 = plan_v1(scratch, workdir, (changed_top,))
        v1_ms = (time.perf_counter() - before) * 1000.0
        before = time.perf_counter()
        v2 = plan_v2(scratch, workdir, (changed_top,))
        v2_ms = (time.perf_counter() - before) * 1000.0
        v1_files = tuple(getattr(v1, "files", ()) or ())
        v2_files = tuple(getattr(v2, "files", ()) or ())
        v1_is_full = getattr(v1, "kind", "selected") == "full"
        full_files = int(getattr(v1, "total", 0) or 0)
        if args.truth == "full":
            kind, files = "full", ()
        else:
            union = sorted(set(v1_files) | set(v2_files))
            kind, files = ground_truth_target(
                tuple(_scope_path(prefix, path) for path in union),
                v1_is_full=v1_is_full, affordable=len(union) <= 200)
        mutant_failed = _ptest_in(args, campaign, scratch, files,
                                  kind == "full", "ground-truth")
        truth_s = time.perf_counter() - before
        covered = v2_covering(mutant_failed, v2, prefix)
        # The seed run is the pristine baseline. A candidate miss is
        # confirmed on the pristine scratch, so a flaky or
        # order-dependent test is not reported as a miss.
        baseline_failed = tuple(campaign.seed_failed)
        misses = find_misses(mutant_failed, baseline_failed, covered)
        if misses:
            confirm = tuple(sorted({file_part(node) for node in misses}))
            rerun = _ptest_in(args, campaign, campaign.base, confirm,
                              False, "baseline")
            baseline_failed = tuple(sorted(set(baseline_failed)
                                           | set(rerun)))
            misses = find_misses(mutant_failed, baseline_failed, covered)
        whole = indicators_for(mutated)
        suggestions = tuple(sorted(key for key, hit in whole.items() if hit))
        miss_classes = tuple(
            classify_miss(
                signals_for_miss(scratch, miss, original, mutated),
                site_class=mutant.site.site_class)
            for miss in misses)
        counts = campaign.counts
        record = MutantRecord(
            mutant_id=mutant.mutant_id,
            site_class=mutant.site.site_class,
            path=mutant.site.path, lineno=mutant.site.lineno,
            v1_files=v1_files, v2_files=v2_files,
            v1_is_full=v1_is_full, full_files=full_files,
            failed=mutant_failed, baseline_failed=baseline_failed,
            misses=misses, miss_classes=miss_classes,
            suggestions=suggestions,
            planning_ms={"v1": v1_ms, "v2": v2_ms},
            v1_tests=selection_tests(v1, counts, dynamic=False),
            v2_tests=selection_tests(v2, counts, dynamic=True),
            total_tests=sum(counts.values()),
            v2_engine=str(getattr(v2, "engine", "") or ""),
            v2_reason=str(getattr(v2, "static_reason", "")
                          or getattr(v2, "reason", "") or ""),
            v2_details=tuple(str(line) for line in
                             (getattr(v2, "details", ()) or ())[:12]))
        _log(f"{mutant.mutant_id} {mutant.site.site_class} "
             f"{mutant.site.path}: v1 {record.v1_tests} v2 {record.v2_tests}"
             f" of {record.total_tests} tests, {len(mutant_failed)} failed,"
             f" {len(misses)} misses · truth {truth_s:.0f}s ·"
             f" total {time.perf_counter() - started:.0f}s")
        return record
    finally:
        target.write_bytes(original_bytes)
        if campaign.store_db is not None:
            restore_store(campaign.store_db, campaign.backup)


def _prepare(args, project: Path, out: Path) -> Campaign:
    prefix = _project_prefix(Path(project))
    project_id = campaign_project_id(project, args.seed,
                                     os.urandom(8).hex())
    truth = create_scratch(project, out, "truth")
    base = create_scratch(project, out, "base")
    campaign = Campaign(project=project, out=out, prefix=prefix,
                        project_id=project_id, truth=truth, base=base)
    for scratch in (truth, base):
        isolate_scratch(scratch, prefix, project_id, project)
        run_setup(campaign.workdir(scratch))
    try:
        campaign.store_db = store_db_path(project_id)
    except Exception:
        campaign.store_db = None
    return campaign


def run_campaign(args) -> int:
    """Seeded A1-A5 campaign: mutants in a scratch worktree, v1 vs v2, truth."""
    project = Path(args.project)
    out = Path(args.out)
    tree = read_tree_files(project)
    sites = [site for site in collect_sites(tree) if site.site_class in args.classes]
    mutants = sample_mutants(sites, args.mutants, args.seed)
    campaign = _prepare(args, project, out)
    records: list[MutantRecord] = []
    try:
        _log("seed: recording the pristine tree")
        campaign.seed_failed = _ptest_in(args, campaign, campaign.base, (),
                                         True, "seed")
        if campaign.seed_failed:
            _log(f"seed: {len(campaign.seed_failed)} failing tests on the"
                 f" pristine tree (baseline)")
        if campaign.store_db is not None:
            campaign.backup = backup_store(
                campaign.store_db, out / "store-backup")
        try:
            campaign.counts = tests_per_file(campaign.project_id)
        except Exception:
            campaign.counts = {}
        for mutant in mutants:
            try:
                records.append(_run_mutant(args, campaign, mutant))
            except (RuntimeError, OSError, ValueError,
                    subprocess.SubprocessError) as exc:
                _log(f"{mutant.mutant_id} error: {exc}")
                records.append(MutantRecord(
                    mutant_id=mutant.mutant_id,
                    site_class=mutant.site.site_class,
                    path=mutant.site.path, lineno=mutant.site.lineno,
                    error=str(exc)[:2000]))
            # Partial reports survive an interrupted multi-hour campaign.
            _write_reports(out, EvalReport(seed=args.seed,
                                           mutants=tuple(records)))
        overhead = {}
        if args.benchmark_overhead:
            overhead = benchmark_overhead(project, out, args.ptest,
                                          campaign.project_id)
    except BaseException:
        _write_reports(out, EvalReport(seed=args.seed,
                                       mutants=tuple(records)))
        raise
    finally:
        for scratch in (campaign.truth, campaign.base):
            remove_scratch(project, scratch)
        if campaign.store_db is not None:
            restore_store(campaign.store_db, None)
        if campaign.backup is not None and campaign.backup.is_file():
            campaign.backup.unlink()
    _write_reports(out, EvalReport(seed=args.seed, mutants=tuple(records),
                                   overhead_ms=overhead))
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return _dry_run(args)
    return run_campaign(args)


if __name__ == "__main__":
    sys.exit(main())
