"""Evaluation harness for dependency-recorded test selection (spec 6.7).

Samples seeded deterministic mutants across the eight mutation classes,
plans v1 (static) and v2 (dynamic) for each mutant in a scratch git
worktree, runs the ground truth through the branch's ptest, and reports
misses classified by the spec section 9 residual risks (R1-R9).

Only the orchestration tail needs the merged T1-T5 tree (impact,
selection_engine, selection_store); every pure-logic unit in this module
is stdlib-only and covered by tests/ng/test_selection_eval.py. The A1-A5
campaign itself is never executed in tests.
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


def _node_span(lines, node):
    offsets = _offsets(lines)
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    return start, end


def _bump(segment):
    try:
        value = ast.literal_eval(segment)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return "None  # ptest-eval-mutant"
    if isinstance(value, bool):
        return "False" if value else "True"
    if isinstance(value, int):
        return str(value + 1)
    if isinstance(value, float):
        return repr(value + 1.0)
    if isinstance(value, str):
        return repr(value + "-mutant")
    return "None  # ptest-eval-mutant"


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
    lines.append("| mutant | class | path | v1 files | v2 files | failed | miss |")
    lines.append("|---|---|---|---|---|---|---|")
    for record in report.mutants:
        miss = ",".join(record.misses) if record.misses else "none"
        lines.append(f"| {record.mutant_id} | {record.site_class} | "
                     f"{record.path}:{record.lineno} | {len(record.v1_files)} | "
                     f"{len(record.v2_files)} | {len(record.failed)} | {miss} |")
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

def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


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


def plan_v1(project_root: Path, changed: tuple):
    """Static plan: impact.plan with the 0.4.10 rules (conftest_edges=False)."""
    from ptest import impact  # noqa: imported lazily; needs the merged tree
    import inspect
    if "conftest_edges" not in inspect.signature(impact.plan).parameters:
        raise RuntimeError("v1 planning needs the merged T1 tree (conftest_edges)")
    return impact.plan(None, project_root, _planning_config(project_root),
                       changed, key=None, cache=None, conftest_edges=False)


def plan_v2(project_root: Path, changed: tuple):
    """Dynamic plan: selection_engine.preview."""
    from ptest import selection_engine  # noqa: needs the merged tree
    domain, config = _planning_domain_config(project_root)
    return selection_engine.preview(domain, None, project_root, config, changed)


def _planning_config(project_root: Path):
    raise RuntimeError("planning needs the merged T1-T5 tree "
                       "(config/domain loading seam)")


def _planning_domain_config(project_root: Path):
    raise RuntimeError("planning needs the merged T1-T5 tree "
                       "(config/domain loading seam)")


def store_db_path(project_root: Path) -> Path:
    """Locate <state>/projects/<id>/selection.db (merged-tree seam)."""
    raise RuntimeError("store lookup needs the merged T1-T5 tree "
                       "(state dir and project id seam)")


def failing_nodeids_for_run(store_db: Path, run_id: str) -> tuple:
    """Failing node ids recorded under run_id (merged-tree seam)."""
    from ptest import selection_store  # noqa: needs the merged tree
    del selection_store, store_db, run_id
    raise RuntimeError("ground truth needs the merged T1-T5 tree")


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


def indicators_for(mutated_text: str) -> dict:
    """Cheap static signals mapping a mutant file to R1-R9 indicators."""
    return {
        "dynamic_name": any(token in mutated_text
                            for token in ("getattr(", "importlib", "eval(")),
        "spawned_binary": "subprocess" in mutated_text,
        "background_thread": "threading" in mutated_text,
        "import_side_effect": False,
        "nondeterministic": False,
        "order_dependent": False,
        "shared_state": False,
        "recorder_tamper": False,
        "py_as_data": False,
        "installed_package": False,
        "line_number_dependent": False,
    }


def run_campaign(args) -> int:
    """Seeded A1-A5 campaign: mutants in scratch worktrees, v1 vs v2, truth."""
    project = Path(args.project)
    out = Path(args.out)
    tree = read_tree_files(project)
    sites = [site for site in collect_sites(tree) if site.site_class in args.classes]
    mutants = sample_mutants(sites, args.mutants, args.seed)
    records = []
    for mutant in mutants:
        scratch = create_scratch(project, out, mutant.mutant_id)
        try:
            target = scratch / mutant.site.path
            original = target.read_text(encoding="utf-8")
            target.write_text(apply_mutation(original, mutant.site),
                              encoding="utf-8")
            changed = (mutant.site.path,)
            before = time.perf_counter()
            v1 = plan_v1(scratch, changed)
            v1_ms = (time.perf_counter() - before) * 1000.0
            before = time.perf_counter()
            v2 = plan_v2(scratch, changed)
            v2_ms = (time.perf_counter() - before) * 1000.0
            union = sorted(set(v1.files) | set(v2.files))
            kind, files = ground_truth_target(
                union, v1_is_full=(v1.kind == "full"),
                affordable=len(union) <= 200)
            records.append(MutantRecord(
                mutant_id=mutant.mutant_id,
                site_class=mutant.site.site_class,
                path=mutant.site.path, lineno=mutant.site.lineno,
                v1_files=tuple(v1.files), v2_files=tuple(v2.files),
                v1_is_full=(v1.kind == "full"),
                planning_ms={"v1": v1_ms, "v2": v2_ms}))
            # Ground-truth ptest run (union files, or full when v1 is
            # full and affordable), per-file-set baseline, store backup
            # and miss classification happen here once the T1-T5 tree
            # is merged: run `ptest <files> --result-json`, read the
            # failing node ids from the store snapshot for that run_id,
            # find_misses() them against the baseline, classify_miss().
            _ = (kind, files)
            raise RuntimeError("campaign needs the merged T1-T5 tree")
        finally:
            remove_scratch(project, scratch)
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
