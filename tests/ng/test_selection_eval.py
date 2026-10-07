"""Pure-logic unit tests for scripts/selection_eval.py (spec 6.7).

Only deterministic logic is covered here: sampling, mutation operators,
ground-truth choice, miss classification, flake marking and report
rendering. The A1-A5 campaign itself is never executed in this task.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "selection_eval", SCRIPTS / "selection_eval.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


se = _load()


FUNCS_PY = '''\
VALUE = 41

CONSTANT = "hello"

import os
import sys as system

from pkg.mod import alpha, beta


def add(first, second=True, count=3):
    """Add things."""
    total = first + second
    if total == count and total != 0:
        return total
    return -1


class Widget:
    KIND = "gadget"

    def __init__(self, size=10):
        self.size = size

    @pytest.mark.parametrize("case", [1, 2])
    def render(self, mode="short"):
        return f"{self.KIND}:{mode}"
'''


def _tree():
    return {"pkg/core.py": FUNCS_PY, "data/config.json": '{"key": 1}\n'}


def test_mutation_classes_are_the_spec_eight():
    assert se.MUTATION_CLASSES == (
        "raise-entry",
        "comparison-flip",
        "constant-change",
        "attr-change",
        "default-change",
        "decorator-change",
        "import-change",
        "data-change",
    )


def test_collect_sites_covers_every_class():
    sites = se.collect_sites(_tree())
    found = {site.site_class for site in sites}
    assert found == set(se.MUTATION_CLASSES)


def test_collect_sites_is_sorted_and_deterministic():
    first = se.collect_sites(_tree())
    second = se.collect_sites(_tree())
    assert [(site.site_class, site.path, site.lineno) for site in first] == [
        (site.site_class, site.path, site.lineno) for site in second]
    keys = [(site.site_class, site.path, site.lineno) for site in first]
    assert keys == sorted(keys)


def test_sampling_is_deterministic_for_a_seed():
    sites = se.collect_sites(_tree())
    one = se.sample_mutants(sites, 6, seed=1234)
    two = se.sample_mutants(sites, 6, seed=1234)
    assert one == two


def test_sampling_covers_every_class_when_it_fits():
    sites = se.collect_sites(_tree())
    mutants = se.sample_mutants(sites, len(se.MUTATION_CLASSES), seed=7)
    assert {mutant.site.site_class for mutant in mutants} == set(se.MUTATION_CLASSES)


def test_sampling_caps_at_available_sites():
    sites = se.collect_sites(_tree())
    mutants = se.sample_mutants(sites, 10_000, seed=7)
    assert len(mutants) == len(sites)
    assert len({mutant.mutant_id for mutant in mutants}) == len(sites)


@pytest.mark.parametrize("site_class", [
    "raise-entry", "comparison-flip", "constant-change", "attr-change",
    "default-change", "decorator-change", "import-change",
])
def test_each_code_operator_parses_and_changes_exactly_one_site(site_class):
    tree = _tree()
    sites = [site for site in se.collect_sites(tree) if site.site_class == site_class]
    assert sites, site_class
    site = sites[0]
    mutated = se.apply_mutation(tree[site.path], site)
    assert mutated != tree[site.path]
    ast.parse(mutated)  # still valid Python
    # Deterministic: applying twice gives the same mutant.
    assert se.apply_mutation(tree[site.path], site) == mutated
    # Exactly one site changes: the diff is a single hunk.
    assert len(_hunks(tree[site.path], mutated)) == 1


def _hunks(before, after):
    import difflib
    return [line for line in difflib.unified_diff(
        before.splitlines(), after.splitlines()) if line.startswith("@@")]


def test_data_operator_appends_a_marker_line():
    tree = _tree()
    sites = [site for site in se.collect_sites(tree) if site.site_class == "data-change"]
    assert len(sites) == 1
    mutated = se.apply_mutation(tree[sites[0].path], sites[0])
    assert mutated != tree[sites[0].path]
    assert "ptest-eval-mutant" in mutated
    assert len(_hunks(tree[sites[0].path], mutated)) == 1


def test_raise_entry_operator_inserts_a_raise():
    tree = _tree()
    site = next(site for site in se.collect_sites(tree)
                if site.site_class == "raise-entry" and site.name == "add")
    mutated = se.apply_mutation(tree[site.path], site)
    func = next(node for node in ast.walk(ast.parse(mutated))
                if isinstance(node, ast.FunctionDef) and node.name == "add")
    assert isinstance(func.body[0], ast.Raise)


def test_union_or_full_prefers_full_only_when_affordable():
    files = ("tests/test_a.py", "tests/test_b.py")
    assert se.ground_truth_target(files, v1_is_full=True, affordable=True) == ("full", ())
    assert se.ground_truth_target(files, v1_is_full=True, affordable=False) == ("union", files)
    assert se.ground_truth_target(files, v1_is_full=False, affordable=True) == ("union", files)
    assert se.ground_truth_target((), v1_is_full=False, affordable=True) == ("union", ())


@pytest.mark.parametrize(("indicators", "expected"), [
    ({"nondeterministic": True}, "R1"),
    ({"order_dependent": True}, "R1"),
    ({"shared_state": True}, "R1"),
    ({"dynamic_name": True}, "R2"),
    ({"import_side_effect": True}, "R3"),
    ({"spawned_binary": True}, "R4"),
    ({"recorder_tamper": True}, "R5"),
    ({"py_as_data": True}, "R6"),
    ({"installed_package": True}, "R7"),
    ({"line_number_dependent": True}, "R8"),
    ({"background_thread": True}, "R9"),
    ({}, "unclassified"),
])
def test_miss_classification_mapping(indicators, expected):
    assert se.classify_miss(indicators) == expected


def test_miss_classification_priority_is_documented_order():
    assert se.classify_miss({"dynamic_name": True, "spawned_binary": True}) == "R2"


def test_baseline_failures_are_not_misses():
    misses = se.find_misses(
        mutant_failed=("tests/test_a.py::test_1", "tests/test_a.py::test_2"),
        baseline_failed=("tests/test_a.py::test_2",),
        selected=("tests/test_a.py::test_1", "tests/test_b.py::test_9"),
    )
    assert misses == ()


def test_unselected_new_failures_are_misses():
    misses = se.find_misses(
        mutant_failed=("tests/test_a.py::test_1", "tests/test_a.py::test_2"),
        baseline_failed=(),
        selected=("tests/test_a.py::test_1",),
    )
    assert misses == ("tests/test_a.py::test_2",)


def test_report_json_round_trips_and_markdown_names_every_miss():
    report = se.EvalReport(
        seed=11,
        mutants=(se.MutantRecord(
            mutant_id="m0001", site_class="constant-change",
            path="pkg/core.py", lineno=1,
            v1_files=("tests/test_a.py",), v2_files=("tests/test_a.py",),
            v1_is_full=False, failed=("tests/test_a.py::test_1",),
            baseline_failed=(), misses=(),
            miss_classes=(), planning_ms={"v1": 3.0, "v2": 5.0}),),
        overhead_ms={"dynamic-false": [1.0, 2.0, 3.0], "dynamic-true": [1.0, 1.0, 1.0]},
    )
    payload = json.loads(se.render_json(report))
    assert payload["seed"] == 11
    assert payload["mutants"][0]["mutant_id"] == "m0001"
    text = se.render_markdown(report)
    assert "m0001" in text
    assert "constant-change" in text
    assert "R1-R9" in text or "miss" in text


def test_median_of_three():
    assert se.median([3.0, 1.0, 2.0]) == 2.0


def test_arg_parsing_defaults():
    args = se.parse_args(["--project", "proj", "--mutants", "5",
                          "--seed", "9", "--out", "outdir"])
    assert args.project == "proj"
    assert args.mutants == 5
    assert args.seed == 9
    assert args.out == "outdir"
    assert args.dry_run is False
    assert args.benchmark_overhead is False


def test_dry_run_lists_mutants_without_touching_git_or_runs(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "core.py").write_text(FUNCS_PY)
    out = tmp_path / "out"
    calls = []

    def fail(*argv, **kwargs):
        calls.append(argv)
        raise AssertionError("must not touch git or run tests")

    monkeypatch.setattr(se.subprocess, "run", fail)
    code = se.main(["--project", str(project), "--mutants", "4",
                    "--seed", "3", "--out", str(out), "--dry-run"])
    assert code == 0
    assert calls == []
    listed = json.loads((out / "selection-eval.json").read_text(encoding="utf-8"))
    assert len(listed["mutants"]) == 4
    assert "m000" in capsys.readouterr().out
