"""Evaluation harness for dependency-recorded test selection (spec 6.7).

Samples seeded deterministic mutants across the eight mutation classes,
plans v1 (static) and v2 (dynamic) for each mutant in a scratch git
worktree, runs the ground truth through the branch's ptest, and reports
misses classified by the spec section 9 residual risks (R1-R9).

Only v2 planning needs the merged T1-T5 tree
(selection_engine.preview); v1 planning, config/domain loading, the
subprocess ground-truth/baseline runs and the store backup/restore all
use base-tree APIs. Every pure-logic unit in this module is stdlib-only
and covered by tests/ng/test_selection_eval.py. The A1-A5 campaign
itself is never executed in tests.
"""
from __future__ import annotations

import argparse
import ast
import json
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
    planning_ms: Mapping = field(default_factory=dict)


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


def collect_sites(tree: Mapping[str, str]) -> list[Site]:
    """Collect one site per mutation opportunity, sorted deterministically."""
    sites: list[Site] = []
    for path in sorted(tree):
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
    """Seeded deterministic sampling covering every class that fits."""
    rng = random.Random(seed)
    by_class = {name: [site for site in sites if site.site_class == name]
                for name in MUTATION_CLASSES}
    picked: list[Site] = []
    for name in MUTATION_CLASSES:
        if by_class[name]:
            picked.append(rng.choice(by_class[name]))
    chosen = set(picked)
    rest = [site for site in sites if site not in chosen]
    rng.shuffle(rest)
    picked.extend(rest)
    selected = picked[:max(0, count)]
    return [Mutant(f"m{index + 1:04d}", site)
            for index, site in enumerate(selected)]


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


def classify_miss(indicators: Mapping[str, bool]) -> str:
    """Classify a miss by spec section 9 (R1-R9) or 'unclassified'."""
    for code, keys in _MISS_PRIORITY:
        if any(indicators.get(key) for key in keys):
            return code
    return "unclassified"


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
                "planning_ms": dict(record.planning_ms),
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
    lines.append(f"{len(report.mutants)} mutants, {total_misses} misses.")
    lines.append("Miss classes follow spec section 9 residual risks R1-R9; "
                 "anything else is unclassified and blocks release.")
    lines.append("")
    lines.append("| mutant | class | path | v1 | v2 | full | failed | miss | "
                 "miss class | planning ms (v1/v2) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for record in report.mutants:
        miss = ",".join(record.misses) if record.misses else "none"
        classes = ",".join(record.miss_classes) if record.miss_classes else "-"
        planning = record.planning_ms or {}
        plan_ms = f"{planning.get('v1', '-')}/{planning.get('v2', '-')}"
        lines.append(f"| {record.mutant_id} | {record.site_class} | "
                     f"{record.path}:{record.lineno} | {len(record.v1_files)} | "
                     f"{len(record.v2_files)} | {record.full_files} | "
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


def ground_truth_argv(ptest_cmd: str, files: tuple, export_name: str,
                      full: bool) -> list:
    """Build ``ptest <files> --result-json`` (or ``ptest --full``) argv."""
    base = shlex.split(ptest_cmd)
    if full:
        return base + ["--full", "--result-json", export_name]
    return base + list(files) + ["--result-json", export_name]


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


def plan_v1(project_root: Path, changed: tuple):
    """Static plan: impact.plan with the 0.4.10 rules."""
    from ptest import impact  # noqa: imported lazily; stdlib-only at import
    config = _planning_config(project_root)
    root = Path(project_root)
    return impact.plan(root, root, config, tuple(changed))


def plan_v2(project_root: Path, changed: tuple):
    """Dynamic plan: selection_engine.preview (needs the merged T1-T5 tree)."""
    try:
        from ptest import selection_engine  # noqa: needs the merged tree
    except ImportError as exc:
        raise RuntimeError(
            "plan v2 needs the merged T1-T5 tree "
            "(ptest.selection_engine.preview is absent)") from exc
    domain, config = _planning_domain_config(project_root)
    root = Path(project_root)
    return selection_engine.preview(domain, root, root, config, tuple(changed))


def store_db_path(project_root: Path) -> Path:
    """Locate ``<state>/projects/<id>/selection.db`` without creating it."""
    domain, config = _planning_domain_config(project_root)
    project_id = config.project_id
    try:
        from ptest import contracts as contracts_mod  # noqa: lazy import
        locate = getattr(contracts_mod, "selection_store_path", None)
        if callable(locate):
            return Path(locate(domain.root, project_id))
    except ImportError:
        pass
    return Path(domain.root) / "projects" / project_id / "selection.db"


def backup_store(store_db: Path, backup_dir: Path) -> Path | None:
    """Copy the store aside; None when there is nothing to isolate."""
    if not store_db.is_file() or store_db.is_symlink():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (store_db.name + ".bak")
    shutil.copy2(store_db, backup)
    return backup


def restore_store(store_db: Path, backup: Path | None) -> None:
    """Restore a backup taken by backup_store; no-op when backup is None."""
    if backup is None:
        return
    store_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, store_db)


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


def benchmark_overhead(project: Path, out: Path, ptest_cmd: str) -> dict:
    """Full run with dynamic=false vs true, back to back, 3x; medians."""
    repo = project
    scratch = create_scratch(repo, out, "overhead")
    try:
        config_path = scratch / ".ptest.toml"
        if not config_path.is_file():
            raise RuntimeError("benchmark needs a .ptest.toml at the project root")
        base = shlex.split(ptest_cmd) + ["--full", "--again"]
        samples = {"dynamic-false": [], "dynamic-true": []}
        for _ in range(3):
            for label, value in (("dynamic-false", False), ("dynamic-true", True)):
                previous = overlay_dynamic(config_path, value)
                try:
                    started = time.perf_counter()
                    done = _run(base, cwd=scratch)
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
        "py_as_data": any(token in mutated_text
                          for token in ("ast.parse", "importlib.resources",
                                        "pkgutil", ".py'",
                                        '.py"')) or (
            "open(" in mutated_text and ".py" in mutated_text),
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
                    full: bool, label: str) -> tuple:
    """Run one file set through ptest and return the failing node ids."""
    export_name = f"{RESULT_EXPORT_PREFIX}{label}-{scratch.name}.json"
    done = _run(ground_truth_argv(ptest_cmd, tuple(files), export_name, full),
                cwd=scratch)
    return failing_nodeids(scratch / export_name, scratch,
                           done.returncode != 0)


def _run_mutant(args, project: Path, out: Path, store_db, backup,
                mutant: Mutant) -> MutantRecord:
    """Plan v1/v2, run ground truth + baseline, detect/classify misses."""
    if store_db is not None:
        restore_store(store_db, backup)  # pristine store for planning
    scratch = create_scratch(project, out, mutant.mutant_id)
    baseline_scratch = None
    try:
        target = scratch / mutant.site.path
        original = target.read_text(encoding="utf-8")
        mutated = apply_mutation(original, mutant.site)
        if mutant.site.site_class == "data-change":
            if diff_edits(original, mutated) != 1:
                raise RuntimeError("data mutant changes more than one site")
        else:
            mutated = check_mutant(original, mutated)
        target.write_text(mutated, encoding="utf-8")
        changed = (mutant.site.path,)
        before = time.perf_counter()
        v1 = plan_v1(scratch, changed)
        v1_ms = (time.perf_counter() - before) * 1000.0
        before = time.perf_counter()
        v2 = plan_v2(scratch, changed)
        v2_ms = (time.perf_counter() - before) * 1000.0
        v1_files = tuple(getattr(v1, "files", ()) or ())
        v2_files = tuple(getattr(v2, "files", ()) or ())
        v1_is_full = getattr(v1, "kind", "selected") == "full"
        v2_is_full = getattr(v2, "kind", "selected") == "full"
        full_files = int(getattr(v1, "total", 0) or 0)
        union = sorted(set(v1_files) | set(v2_files))
        kind, files = ground_truth_target(
            union, v1_is_full=v1_is_full, affordable=len(union) <= 200)
        # Baseline without the mutant (pristine scratch worktree), then
        # ground truth with the mutant, both through the branch's ptest.
        baseline_scratch = create_scratch(
            project, out, mutant.mutant_id + "-baseline")
        baseline_failed = _ptest_failures(
            args.ptest, baseline_scratch, files, kind == "full", "baseline")
        mutant_failed = _ptest_failures(
            args.ptest, scratch, files, kind == "full", "ground-truth")
        if v2_is_full:
            covered = mutant_failed
        else:
            covered = selected_covering(mutant_failed, v2_files)
        misses = find_misses(mutant_failed, baseline_failed, covered)
        signals = indicators_for(mutated)
        miss_classes = tuple(classify_miss(signals) for _ in misses)
        return MutantRecord(
            mutant_id=mutant.mutant_id,
            site_class=mutant.site.site_class,
            path=mutant.site.path, lineno=mutant.site.lineno,
            v1_files=v1_files, v2_files=v2_files,
            v1_is_full=v1_is_full, full_files=full_files,
            failed=mutant_failed, baseline_failed=baseline_failed,
            misses=misses, miss_classes=miss_classes,
            planning_ms={"v1": v1_ms, "v2": v2_ms})
    finally:
        if baseline_scratch is not None:
            remove_scratch(project, baseline_scratch)
        remove_scratch(project, scratch)
        if store_db is not None:
            restore_store(store_db, backup)


def run_campaign(args) -> int:
    """Seeded A1-A5 campaign: mutants in scratch worktrees, v1 vs v2, truth."""
    project = Path(args.project)
    out = Path(args.out)
    tree = read_tree_files(project)
    sites = [site for site in collect_sites(tree) if site.site_class in args.classes]
    mutants = sample_mutants(sites, args.mutants, args.seed)
    try:
        store_db = store_db_path(project)
    except RuntimeError:
        store_db = None
    backup = (backup_store(store_db, out / "scratch" / "store-backup")
              if store_db is not None else None)
    try:
        records = [_run_mutant(args, project, out, store_db, backup, mutant)
                   for mutant in mutants]
    finally:
        if store_db is not None:
            restore_store(store_db, backup)
            if backup is not None and backup.is_file():
                backup.unlink()
    overhead = (benchmark_overhead(project, out, args.ptest)
                if args.benchmark_overhead else {})
    _write_reports(out, EvalReport(seed=args.seed,
                                   mutants=tuple(records), overhead_ms=overhead))
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return _dry_run(args)
    return run_campaign(args)


if __name__ == "__main__":
    sys.exit(main())
