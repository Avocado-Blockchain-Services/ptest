"""Pure-logic unit tests for scripts/selection_eval.py (spec 6.7).

Only deterministic logic is covered here: sampling, mutation operators,
ground-truth choice, miss classification, flake marking and report
rendering. The A1-A5 campaign itself is never executed in this task.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
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


def test_test_modules_are_never_mutated_but_support_is():
    tree = dict(_tree())
    tree["tests/test_core.py"] = FUNCS_PY
    tree["tests/core_test.py"] = FUNCS_PY
    tree["tests/conftest.py"] = FUNCS_PY
    paths = {site.path for site in se.collect_sites(tree)}
    assert "tests/test_core.py" not in paths
    assert "tests/core_test.py" not in paths
    assert "tests/conftest.py" in paths


def test_sampling_is_stratified_over_classes():
    """Plenty of comparison sites must not crowd out the rare classes."""
    many = [se.Site("comparison-flip", f"pkg/m{i}.py", 1, "compare")
            for i in range(100)]
    rare = [se.Site("data-change", f"data/d{i}.json", 1, f"d{i}.json")
            for i in range(3)]
    mutants = se.sample_mutants(many + rare, 8, seed=3)
    classes = [mutant.site.site_class for mutant in mutants]
    assert classes.count("data-change") == 3
    assert classes.count("comparison-flip") == 5


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


@pytest.mark.parametrize(("indicators", "site_class", "expected"), [
    ({"nondeterministic": True}, "comparison-flip", "R1"),
    ({"order_dependent": True}, "comparison-flip", "R1"),
    ({"shared_state": True}, "comparison-flip", "R1"),
    ({"dynamic_name": True}, "constant-change", "R2"),
    ({"dynamic_name": True}, "attr-change", "R2"),
    ({"import_side_effect": True}, "import-change", "R3"),
    ({"spawned_binary": True}, "comparison-flip", "R4"),
    ({"recorder_tamper": True}, "comparison-flip", "R5"),
    ({"py_as_data": True}, "comparison-flip", "R6"),
    ({"installed_package": True}, "comparison-flip", "R7"),
    ({"line_number_dependent": True}, "comparison-flip", "R8"),
    ({"background_thread": True}, "comparison-flip", "R9"),
    ({}, "comparison-flip", "unclassified"),
    # Spec preconditions gate R2/R3: a dynamic-name signal on a
    # function-body change, or an import-side-effect signal on a
    # non-import change, must not classify.
    ({"dynamic_name": True}, "comparison-flip", "unclassified"),
    ({"dynamic_name": True}, None, "unclassified"),
    ({"import_side_effect": True}, "comparison-flip", "unclassified"),
    ({"import_side_effect": True}, None, "unclassified"),
])
def test_miss_classification_mapping(indicators, site_class, expected):
    assert se.classify_miss(indicators, site_class=site_class) == expected


def test_miss_classification_priority_is_documented_order():
    assert se.classify_miss({"dynamic_name": True, "spawned_binary": True},
                            site_class="constant-change") == "R2"
    # The same signals on a function-body change fall through to R4:
    # the R2 gate blocks, the R4 signal still counts.
    assert se.classify_miss({"dynamic_name": True, "spawned_binary": True},
                            site_class="comparison-flip") == "R4"


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
            v1_files=("tests/test_a.py",), v2_files=(),
            v1_is_full=False, full_files=4,
            failed=("tests/test_a.py::test_1",),
            baseline_failed=(), misses=("tests/test_a.py::test_1",),
            miss_classes=("R2",), planning_ms={"v1": 3.0, "v2": 5.0}),),
        overhead_ms={"dynamic-false": [1.0, 2.0, 3.0], "dynamic-true": [1.0, 1.0, 1.0]},
    )
    payload = json.loads(se.render_json(report))
    assert payload["seed"] == 11
    assert payload["mutants"][0]["mutant_id"] == "m0001"
    assert payload["mutants"][0]["miss_classes"] == ["R2"]
    assert payload["mutants"][0]["planning_ms"] == {"v1": 3.0, "v2": 5.0}
    assert payload["mutants"][0]["full_files"] == 4
    text = se.render_markdown(report)
    assert "m0001" in text
    assert "constant-change" in text
    # The record carries a real miss: it must name the failing node id,
    # its R-class, the selection sizes and both planning times.
    assert "tests/test_a.py::test_1" in text
    assert "R2" in text
    assert "3.0" in text and "5.0" in text
    assert "| 1 | 0 | 4 |" in text


def test_tuple_default_mutant_parses_without_comment_splice():
    source = "def g(a=(1, 2), b=3):\n    return a\n"
    sites = [site for site in se.collect_sites({"m.py": source})
             if site.site_class == "default-change"]
    assert len(sites) == 2
    for site in sites:
        mutated = se.apply_mutation(source, site)
        ast.parse(mutated)
        assert "#" not in mutated.splitlines()[site.lineno - 1]
        assert se.diff_edits(source, mutated) == 1


def test_none_default_mutant_is_not_a_noop():
    source = "def f(x=None, y=1):\n    return x\n"
    site = next(site for site in se.collect_sites({"m.py": source})
                if site.site_class == "default-change"
                and source.splitlines()[site.lineno - 1].startswith("def f(x=None"))
    mutated = se.apply_mutation(source, site)
    ast.parse(mutated)
    assert mutated != source
    assert "x=None" not in mutated
    assert se.diff_edits(source, mutated) == 1


def test_multi_statement_line_keeps_the_second_statement():
    source = "X = (1, 2); Y = 2\n"
    site = next(site for site in se.collect_sites({"m.py": source})
                if site.site_class == "constant-change" and site.name == "X")
    mutated = se.apply_mutation(source, site)
    ast.parse(mutated)
    assert "Y = 2" in mutated
    assert se.diff_edits(source, mutated) == 1


def test_non_ascii_default_splice_lands_on_the_node():
    source = 'def g(a="é", b=2):\n    return a\n'
    sites = [site for site in se.collect_sites({"m.py": source})
             if site.site_class == "default-change"]
    assert len(sites) == 2
    by_detail = {site.detail: site for site in sites}
    assert se.apply_mutation(source, by_detail["0"]) == \
        'def g(a=\'é-mutant\', b=2):\n    return a\n'
    assert se.apply_mutation(source, by_detail["1"]) == \
        'def g(a="é", b=3):\n    return a\n'


def test_check_mutant_rejects_noop_unparseable_and_multi_site():
    with pytest.raises(RuntimeError):
        se.check_mutant("X = 1\n", "X = 1\n")
    with pytest.raises(RuntimeError):
        se.check_mutant("X = 1\n", "X = \n")
    with pytest.raises(RuntimeError):
        se.check_mutant("X = 1\n", "X = 2\nY = 3\n")
    assert se.check_mutant("X = 1\n", "X = 2\n") == "X = 2\n"


@pytest.mark.parametrize(("snippet", "indicator", "site_class", "expected"), [
    ("def f(a):\n    return getattr(a, 'b')\n", "dynamic_name",
     "constant-change", "R2"),
    ("configure()\n", "import_side_effect", "import-change", "R3"),
    ("def run():\n    import subprocess\n    subprocess.run(['x'])\n",
     "spawned_binary", "comparison-flip", "R4"),
    ("DB = 'selection.db'\n", "recorder_tamper", "comparison-flip", "R5"),
    ("import ast\ntree = ast.parse('x=1')\n", "py_as_data",
     "comparison-flip", "R6"),
    ("import importlib.metadata\n", "installed_package", "comparison-flip",
     "R7"),
    ("n = node.lineno\n", "line_number_dependent", "comparison-flip", "R8"),
    ("def start():\n    import threading\n    threading.Thread().start()\n",
     "background_thread", "comparison-flip", "R9"),
    ("import random\nx = random.random()\n", "nondeterministic",
     "comparison-flip", "R1"),
    ("def bump():\n    global STATE\n    STATE = 1\n", "order_dependent",
     "comparison-flip", "R1"),
    ("import multiprocessing\n", "shared_state", "comparison-flip", "R1"),
])
def test_every_indicator_can_fire_and_classifies(snippet, indicator,
                                                site_class, expected):
    signals = se.indicators_for(snippet)
    assert signals[indicator] is True
    assert se.classify_miss({indicator: True},
                            site_class=site_class) == expected
    assert se.classify_miss(signals, site_class=site_class) == expected


def test_ground_truth_argv_uses_ptest_with_result_json():
    union = se.ground_truth_argv("uv run ptest", ("a.py", "b.py"),
                                 "run-1.json", full=False)
    assert union == ["uv", "run", "ptest", "--result-json", "run-1.json",
                     "a.py", "b.py"]
    full = se.ground_truth_argv("uv run ptest", ("a.py",), "run-1.json",
                                full=True)
    assert full == ["uv", "run", "ptest", "--full", "--again",
                    "--result-json", "run-1.json"]
    child = se.ground_truth_argv("uv run ptest", (), "run-1.json",
                                 full=True, scope=("services/cp",))
    assert child == ["uv", "run", "ptest", "--full",
                     "--result-json", "run-1.json", "services/cp"]


def test_ground_truth_argv_parses_through_ptest_cli():
    """The harness argv must survive ptest's own option parsing.

    Options precede paths, so ``--result-json`` lands in ``result_path``
    (not in the pytest tail) for both the union and the full branch.
    """
    from ptest import cli as cli_mod
    union = se.ground_truth_argv(
        "uv run ptest",
        ("tests/test_a.py", "tests/test_b.py"),
        "selection-eval-run-x.json", full=False)
    parsed = cli_mod._parse_execution(union[3:])
    assert parsed.result_path == "selection-eval-run-x.json"
    assert tuple(parsed.runner_argv) == ("tests/test_a.py",
                                         "tests/test_b.py")
    assert parsed.mode == cli_mod.C.Mode.SCOPED
    full = se.ground_truth_argv(
        "uv run ptest", (), "selection-eval-run-x.json", full=True)
    parsed_full = cli_mod._parse_execution(full[3:])
    assert parsed_full.result_path == "selection-eval-run-x.json"
    assert tuple(parsed_full.runner_argv) == ()
    assert parsed_full.mode == cli_mod.C.Mode.FULL
    assert parsed_full.again is True


def test_read_result_export_rejects_missing_and_scalar(tmp_path):
    with pytest.raises(RuntimeError):
        se.read_result_export(tmp_path / "absent.json")
    scalar = tmp_path / "scalar.json"
    scalar.write_text("[1, 2]\n")
    with pytest.raises(RuntimeError):
        se.read_result_export(scalar)
    ok = tmp_path / "ok.json"
    ok.write_text('{"run_id": "abc"}\n')
    assert se.read_result_export(ok) == {"run_id": "abc"}


def test_pytest_lastfailed_merges_caches_and_skips_garbage(tmp_path):
    first = tmp_path / ".pytest_cache" / "v" / "cache"
    first.mkdir(parents=True)
    (first / "lastfailed").write_text(
        json.dumps({"tests/test_a.py::test_1": True}))
    nested = tmp_path / "tests" / ".pytest_cache" / "v" / "cache"
    nested.mkdir(parents=True)
    (nested / "lastfailed").write_text(
        json.dumps({"tests/test_b.py::test_9": True}))
    broken = tmp_path / "other" / ".pytest_cache" / "v" / "cache"
    broken.mkdir(parents=True)
    (broken / "lastfailed").write_text("not json{")
    assert se.pytest_lastfailed(tmp_path) == (
        "tests/test_a.py::test_1", "tests/test_b.py::test_9")


def test_failing_nodeids_raise_on_unidentified_failure(tmp_path):
    export = tmp_path / "run.json"
    export.write_text("{}\n")
    with pytest.raises(RuntimeError):
        se.failing_nodeids(export, tmp_path, run_failed=True)
    assert se.failing_nodeids(export, tmp_path, run_failed=False) == ()


def test_selected_covering_maps_files_to_failing_nodes():
    failed = ("tests/test_a.py::test_1", "tests/test_b.py::test_2")
    assert se.selected_covering(failed, ("tests/test_a.py",)) == (
        "tests/test_a.py::test_1",)
    assert se.find_misses(failed, (), se.selected_covering(
        failed, ("tests/test_a.py",))) == ("tests/test_b.py::test_2",)


def test_store_backup_restore_round_trip(tmp_path):
    assert se.backup_store(tmp_path / "absent.db", tmp_path / "bak") is None
    store = tmp_path / "selection.db"
    store.write_bytes(b"store-bytes")
    backup = se.backup_store(store, tmp_path / "bak")
    assert backup is not None and backup.read_bytes() == b"store-bytes"
    store.write_bytes(b"polluted")
    se.restore_store(store, backup)
    assert store.read_bytes() == b"store-bytes"
    se.restore_store(tmp_path / "other.db", None)


def test_restore_drops_stale_journal_and_none_removes(tmp_path):
    """A leftover journal would be replayed into the restored database."""
    store = tmp_path / "selection.db"
    store.write_bytes(b"store-bytes")
    backup = se.backup_store(store, tmp_path / "bak")
    journal = tmp_path / "selection.db-journal"
    journal.write_bytes(b"hot")
    se.restore_store(store, backup)
    assert not journal.exists()
    assert store.read_bytes() == b"store-bytes"
    se.restore_store(store, None)
    assert not store.exists()


def test_backup_refuses_live_journal(tmp_path):
    store = tmp_path / "selection.db"
    store.write_bytes(b"store-bytes")
    (tmp_path / "selection.db-journal").write_bytes(b"hot")
    with pytest.raises(RuntimeError):
        se.backup_store(store, tmp_path / "bak")


def test_campaign_project_id_is_private_and_stable(tmp_path):
    first = se.campaign_project_id(tmp_path, 7)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert se.campaign_project_id(tmp_path, 7) == first
    assert se.campaign_project_id(tmp_path, 8) != first
    assert se.campaign_project_id(tmp_path, 7, "n1") != first


def test_overlay_project_id_rewrites_only_the_id(tmp_path):
    config = tmp_path / ".ptest.toml"
    config.write_text('project_id = "' + "ab" * 16 + '"\n[runner]\nkind = "pytest"\n')
    assert se.overlay_project_id(config, "cd" * 16) is True
    assert config.read_text() == (
        'project_id = "' + "cd" * 16 + '"\n[runner]\nkind = "pytest"\n')
    assert se.overlay_project_id(tmp_path / "absent.toml", "cd" * 16) is False
    config.write_text('[runner]\nkind = "pytest"\n')
    with pytest.raises(RuntimeError):
        se.overlay_project_id(config, "cd" * 16)
    with pytest.raises(ValueError):
        se.overlay_project_id(config, "not-hex")


def test_isolate_scratch_links_env_never_copies(tmp_path):
    top = tmp_path / "repo"
    child = top / "services" / "cp"
    child.mkdir(parents=True)
    (top / ".env").write_text("SECRET=1\n")
    scratch = tmp_path / "scratch"
    (scratch / "services" / "cp").mkdir(parents=True)
    workdir = se.isolate_scratch(scratch, "services/cp", "ab" * 16, child)
    assert workdir == scratch / "services" / "cp"
    assert (scratch / ".env").is_symlink()
    assert (scratch / ".env").resolve() == (top / ".env").resolve()
    assert not (workdir / ".env").exists()


def test_selection_tests_counts_files_nodes_and_full():
    counts = {"tests/test_a.py": 5, "tests/test_b.py": 3}

    class Plan:
        def __init__(self, kind, files=(), engine="static", tests=0):
            self.kind, self.files, self.engine, self.tests = (
                kind, files, engine, tests)

    assert se.selection_tests(Plan("full"), counts, dynamic=False) == 8
    assert se.selection_tests(Plan("none"), counts, dynamic=True) == 0
    assert se.selection_tests(Plan("selected", ("tests/test_a.py",)),
                              counts, dynamic=False) == 5
    assert se.selection_tests(
        Plan("selected", ("tests/test_a.py",), "dynamic", 2),
        counts, dynamic=True) == 2
    # A static fallback in v2 counts whole files like v1.
    assert se.selection_tests(Plan("selected", ("tests/test_b.py",)),
                              counts, dynamic=True) == 3


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


def test_plan_v1_call_uses_0410_rules_and_fails_without_the_knob():
    seen = {}

    class FakeImpact:
        def plan(self, top, root, config, changed, *, conftest_edges=True):
            seen.update(top=top, root=root, changed=changed,
                        conftest_edges=conftest_edges)
            return "v1-plan"

    assert se._plan_v1_call(FakeImpact(), "top", "root", "config",
                            ("a.py",)) == "v1-plan"
    assert seen["conftest_edges"] is False
    assert seen["changed"] == ("a.py",)

    class OldImpact:
        def plan(self, top, root, config, changed):
            return "v1-plan"

    with pytest.raises(RuntimeError):
        se._plan_v1_call(OldImpact(), "top", "root", "config", ("a.py",))


def test_open_without_py_path_is_not_py_as_data():
    assert se.indicators_for('open("fixture.py")\n')["py_as_data"] is True
    assert se.indicators_for(
        'handle = open("data.json")\n# parsed from docs/schema.py\n'
    )["py_as_data"] is False


def test_function_body_miss_with_incidental_tokens_stays_unclassified(
        tmp_path):
    module = ("import random\n\n\n"
              "STATE = {}\n\n\n"
              "def fetch(key):\n"
              "    return getattr(STATE, key)\n\n\n"
              "def touch():\n"
              "    global STATE\n"
              "    STATE = {}\n\n\n"
              "def compare(first, second):\n"
              "    if first == second:\n"
              "        return True\n"
              "    return False\n")
    site = next(site for site in se.collect_sites({"pkg/core.py": module})
                if site.site_class == "comparison-flip")
    mutated = se.apply_mutation(module, site)
    # Whole-module keyword hits would claim R1 (random/global) or R2
    # (getattr) for this function-body change; they are suggestions only.
    whole = se.indicators_for(mutated)
    assert whole["nondeterministic"] is True
    assert whole["dynamic_name"] is True
    assert whole["order_dependent"] is True
    assert se.classify_miss(whole, site_class="comparison-flip") == "R1"
    # The per-miss signals come from the changed line plus the missed
    # test only, so the incidental module tokens cannot classify.
    scratch = tmp_path / "scratch"
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests" / "test_core.py").write_text(
        "from pkg.core import compare\n\n\n"
        "def test_compare():\n"
        "    assert compare(1, 1) is True\n")
    signals = se.signals_for_miss(scratch, "tests/test_core.py::test_compare",
                                  module, mutated)
    assert signals["dynamic_name"] is False
    assert signals["nondeterministic"] is False
    assert signals["order_dependent"] is False
    assert se.classify_miss(signals,
                            site_class="comparison-flip") == "unclassified"


def test_test_function_source_ignores_sibling_tests(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests" / "test_two.py").write_text(
        "import threading\n\n\n"
        "def test_noisy():\n"
        "    threading.Thread().start()\n\n\n"
        "def test_quiet():\n"
        "    assert 1 + 1 == 2\n")
    noisy = se.signals_for_miss(scratch, "tests/test_two.py::test_noisy",
                                "X = 1\n", "X = 2\n")
    assert noisy["background_thread"] is True
    assert se.classify_miss(noisy, site_class="comparison-flip") == "R9"
    quiet = se.signals_for_miss(scratch, "tests/test_two.py::test_quiet",
                                "X = 1\n", "X = 2\n")
    assert quiet["background_thread"] is False
    assert se.classify_miss(quiet,
                            site_class="comparison-flip") == "unclassified"
    missing = se.signals_for_miss(scratch, "tests/test_two.py::test_absent",
                                  "X = 1\n", "X = 2\n")
    assert se.classify_miss(missing,
                            site_class="comparison-flip") == "unclassified"


def test_project_prefix_is_empty_for_standalone_project(tmp_path):
    assert se._project_prefix(tmp_path / "proj") == ""


def test_scope_node_prefixes_child_relative_ids_idempotently():
    assert se._scope_node("", "tests/test_a.py::test_1") == (
        "tests/test_a.py::test_1")
    assert se._scope_node("services/cp", "tests/test_a.py::test_1") == (
        "services/cp/tests/test_a.py::test_1")
    assert se._scope_node(
        "services/cp",
        "services/cp/tests/test_a.py::test_1") == (
        "services/cp/tests/test_a.py::test_1")
    assert se._scope_node("services/cp", "tests/test_a.py") == (
        "services/cp/tests/test_a.py")
    # A selected child-relative failure matches the prefixed v2 plan,
    # so it is covered rather than a miss.
    assert se.selected_covering(
        (se._scope_node("services/cp", "tests/test_a.py::test_1"),),
        ("services/cp/tests/test_a.py",)) == (
        "services/cp/tests/test_a.py::test_1",)


def test_benchmark_overhead_on_nested_child_uses_child_config_and_cwd(
        tmp_path, monkeypatch):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    child = tmp_path / "services" / "cp"
    child.mkdir(parents=True)
    (tmp_path / ".ptest.toml").write_text(
        '[monorepo]\nversion = 1\n[[monorepo.child]]\n'
        'path = "services/cp"\n')
    created = {}

    def fake_create(repo, outdir, name):
        scratch = outdir / "scratch" / name
        target = scratch / "services" / "cp" / ".ptest.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('[project]\nname = "cp"\n')
        created[name] = scratch
        return scratch

    runs = []

    def fake_run(cmd, cwd, timeout=600):
        runs.append(Path(cwd))
        assert list(cmd)[:4] == ["uv", "run", "ptest", "--full"]
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(se, "create_scratch", fake_create)
    monkeypatch.setattr(se, "remove_scratch", lambda repo, scratch: None)
    monkeypatch.setattr(se, "_run", fake_run)
    samples = se.benchmark_overhead(child, tmp_path / "out", "uv run ptest")
    assert set(samples) == {"dynamic-false", "dynamic-true"}
    assert all(len(values) == 3 for values in samples.values())
    # Every run happens in the child dir, never at the scratch root
    # where the [monorepo] manifest lives.
    assert runs and all(
        cwd == created["overhead"] / "services" / "cp" for cwd in runs)
    # The overlay is reverted after each run.
    assert (created["overhead"] / "services" / "cp" / ".ptest.toml"
            ).read_text() == '[project]\nname = "cp"\n'


# --- campaign wiring (stubs for git, setup, ptest and planning) -----------

_CORE = ("import random\n\nVALUE = 1\n\n\n"
         "def add(first, second=True):\n"
         "    total = first + second\n"
         "    if total == VALUE:\n"
         "        return total\n"
         "    return -1\n")
_REAL_ID = "ab" * 16


class _Plan:
    def __init__(self, kind="selected", files=(), deselect=(), total=10,
                 engine="static", tests=0):
        self.kind, self.files, self.deselect = kind, tuple(files), deselect
        self.total, self.engine, self.tests = total, engine, tests
        self.static_reason, self.reason, self.details = "", "", ()


def _campaign(tmp_path, monkeypatch, *, v1, v2, failing, prefix="",
              benchmark=False, counts=None):
    """Wire run_campaign to stubs. ``failing(label, cwd)`` returns the
    child-relative failing ids of one ptest run."""
    import subprocess
    if prefix:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    project = tmp_path / prefix if prefix else tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "core.py").write_text(_CORE)
    config_text = f'project_id = "{_REAL_ID}"\n'
    (project / ".ptest.toml").write_text(config_text)
    out = tmp_path / "out"
    seen = {"created": {}, "removed": [], "setup": [], "runs": [],
            "configs": []}
    store_root = tmp_path / "state" / "projects"

    def fake_create(repo, outdir, name):
        scratch = outdir / "scratch" / name
        workdir = scratch / prefix if prefix else scratch
        (workdir / "pkg").mkdir(parents=True, exist_ok=True)
        (workdir / "pkg" / "core.py").write_text(_CORE)
        (workdir / ".ptest.toml").write_text(config_text)
        seen["created"][name] = scratch
        return scratch

    def fake_run(cmd, cwd, timeout=600):
        export = cmd[cmd.index("--result-json") + 1]
        label = export[len(se.RESULT_EXPORT_PREFIX):].split("-")[0]
        workdir = Path(cwd) / prefix if prefix else Path(cwd)
        seen["runs"].append((label, list(cmd), Path(cwd)))
        seen["configs"].append((workdir / ".ptest.toml").read_text())
        (workdir / export).write_text("{}\n")
        cache = workdir / ".pytest_cache" / "v" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        ids = failing(label, Path(cwd))
        (cache / "lastfailed").write_text(
            json.dumps({node: True for node in ids}))
        store = store_root / campaign_id[0] / "selection.db"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"records-" + label.encode())
        return type("Done", (), {"returncode": 1 if ids else 0,
                                 "stderr": ""})()

    campaign_id = []
    real_id_of = se.campaign_project_id

    def capture_id(project_path, seed, nonce=""):
        value = real_id_of(project_path, seed, nonce)
        campaign_id[:] = [value]
        return value

    monkeypatch.setattr(se, "campaign_project_id", capture_id)
    monkeypatch.setattr(se, "create_scratch", fake_create)
    monkeypatch.setattr(se, "remove_scratch",
                        lambda repo, scratch: seen["removed"].append(scratch))
    monkeypatch.setattr(se, "run_setup",
                        lambda workdir: seen["setup"].append(workdir))
    monkeypatch.setattr(se, "store_db_path",
                        lambda pid: store_root / pid / "selection.db")
    monkeypatch.setattr(se, "tests_per_file",
                        lambda pid: dict(counts or {"tests/test_a.py": 4,
                                                    "tests/test_b.py": 6}))
    monkeypatch.setattr(se, "plan_v1", lambda top, root, changed: v1)
    monkeypatch.setattr(se, "plan_v2", lambda top, root, changed: v2)
    monkeypatch.setattr(se, "_run", fake_run)
    if benchmark:
        monkeypatch.setattr(se, "benchmark_overhead",
                            lambda *a: (_ for _ in ()).throw(
                                RuntimeError("benchmark run failed")))
    args = type("Args", (), {
        "project": str(project), "mutants": 1, "seed": 5,
        "classes": ["raise-entry"], "out": str(out),
        "ptest": "uv run ptest", "benchmark_overhead": benchmark,
        "truth": "full"})()
    seen["project"], seen["out"], seen["args"] = project, out, args
    seen["campaign_id"], seen["store_root"] = campaign_id, store_root
    return seen


def _report(seen):
    return json.loads((seen["out"] / "selection-eval.json").read_text())


def test_deselected_failing_test_in_a_selected_file_is_a_miss(
        tmp_path, monkeypatch):
    """v2 is node-level: a file match is not coverage. Before the fix a
    failing test v2 deselected inside a selected file counted as run."""
    seen = _campaign(
        tmp_path, monkeypatch,
        v1=_Plan(files=("tests/test_a.py",)),
        v2=_Plan(files=("tests/test_a.py",),
                 deselect=("tests/test_a.py::test_1",), engine="dynamic",
                 tests=1),
        failing=lambda label, cwd: (
            ("tests/test_a.py::test_1",) if label == "ground" else ()))
    assert se.run_campaign(seen["args"]) == 0
    record = _report(seen)["mutants"][0]
    assert record["misses"] == ["tests/test_a.py::test_1"]
    assert record["miss_classes"] == ["unclassified"]
    assert record["v1_tests"] == 4 and record["v2_tests"] == 1
    assert record["total_tests"] == 10
    labels = [(label, cwd) for label, _, cwd in seen["runs"]]
    assert labels == [("seed", seen["created"]["base"]),
                      ("ground", seen["created"]["truth"]),
                      ("baseline", seen["created"]["base"])]
    seed_cmd = seen["runs"][0][1]
    assert seed_cmd[3:6] == ["--full", "--again", "--result-json"]
    assert seen["runs"][1][1][3:6] == ["--full", "--again", "--result-json"]
    assert seen["runs"][2][1][-1] == "tests/test_a.py"
    # The mutant is reverted in the persistent scratch afterwards.
    truth = seen["created"]["truth"]
    assert (truth / "pkg" / "core.py").read_text() == _CORE
    assert set(seen["setup"]) == {truth, seen["created"]["base"]}
    assert set(seen["removed"]) == {truth, seen["created"]["base"]}


def test_candidate_miss_failing_on_pristine_rerun_is_not_a_miss(
        tmp_path, monkeypatch):
    seen = _campaign(
        tmp_path, monkeypatch, v1=_Plan(), v2=_Plan(kind="none"),
        failing=lambda label, cwd: (
            () if label == "seed" else ("tests/test_a.py::test_1",)))
    assert se.run_campaign(seen["args"]) == 0
    record = _report(seen)["mutants"][0]
    assert record["misses"] == []
    assert record["baseline_failed"] == ["tests/test_a.py::test_1"]


def test_campaign_never_touches_the_real_project_store(tmp_path,
                                                        monkeypatch):
    seen = _campaign(tmp_path, monkeypatch, v1=_Plan(),
                     v2=_Plan(kind="full"), failing=lambda label, cwd: ())
    real_store = seen["store_root"] / _REAL_ID / "selection.db"
    real_store.parent.mkdir(parents=True)
    real_store.write_bytes(b"shared-records")
    assert se.run_campaign(seen["args"]) == 0
    campaign = seen["campaign_id"][0]
    assert campaign != _REAL_ID
    assert seen["configs"] and all(
        text == f'project_id = "{campaign}"\n' for text in seen["configs"])
    assert (seen["project"] / ".ptest.toml").read_text() == (
        f'project_id = "{_REAL_ID}"\n')
    assert real_store.read_bytes() == b"shared-records"
    assert not (seen["store_root"] / campaign / "selection.db").exists()


def test_nested_child_runs_at_repo_root_with_child_scope(tmp_path,
                                                         monkeypatch):
    seen = _campaign(
        tmp_path, monkeypatch, prefix="services/cp",
        v1=_Plan(files=("tests/test_a.py",)),
        v2=_Plan(files=("tests/test_b.py",), engine="dynamic", tests=2),
        failing=lambda label, cwd: (
            ("tests/test_a.py::test_1",) if label == "ground" else ()))
    assert se.run_campaign(seen["args"]) == 0
    for label, cmd, cwd in seen["runs"]:
        assert cwd in (seen["created"]["truth"], seen["created"]["base"])
        if label in ("seed", "ground"):
            assert cmd[3:5] == ["--full", "--result-json"]
            assert cmd[6:] == ["services/cp"]
    assert seen["runs"][-1][1][-1] == "services/cp/tests/test_a.py"
    record = _report(seen)["mutants"][0]
    assert record["misses"] == ["services/cp/tests/test_a.py::test_1"]


def test_campaign_writes_reports_when_benchmark_fails(tmp_path, monkeypatch):
    seen = _campaign(tmp_path, monkeypatch, v1=_Plan(), v2=_Plan(),
                     failing=lambda label, cwd: (), benchmark=True)
    with pytest.raises(RuntimeError):
        se.run_campaign(seen["args"])
    payload = _report(seen)
    assert len(payload["mutants"]) == 1
    assert payload["overhead_ms"] == {}


def test_a_broken_mutant_is_reported_and_the_campaign_continues(
        tmp_path, monkeypatch):
    seen = _campaign(tmp_path, monkeypatch, v1=_Plan(), v2=_Plan(),
                     failing=lambda label, cwd: ())
    monkeypatch.setattr(se, "plan_v2", lambda top, root, changed: (
        _ for _ in ()).throw(RuntimeError("planner exploded")))
    assert se.run_campaign(seen["args"]) == 0
    record = _report(seen)["mutants"][0]
    assert record["error"] == "planner exploded"
    assert (seen["created"]["truth"] / "pkg" / "core.py").read_text() == _CORE


def test_v2_covering_is_node_level():
    failed = ("tests/test_a.py::t1", "tests/test_a.py::t2",
              "tests/test_b.py::t3")
    plan = _Plan(files=("tests/test_a.py",), deselect=("tests/test_a.py::t2",))
    assert se.v2_covering(failed, plan, "") == ("tests/test_a.py::t1",)
    assert se.v2_covering(failed, _Plan(kind="full"), "") == failed
    assert se.v2_covering(failed, _Plan(kind="none"), "") == ()
    nested = tuple(f"svc/{node}" for node in failed)
    assert se.v2_covering(nested, plan, "svc") == ("svc/tests/test_a.py::t1",)


def test_an_incomplete_ground_truth_is_not_evaluated(tmp_path, monkeypatch):
    """Exit 70/124 runs report a partial failure list: never ground truth."""
    seen = _campaign(tmp_path, monkeypatch, v1=_Plan(), v2=_Plan(),
                     failing=lambda label, cwd: (
                         ("tests/test_a.py::test_1",) if label == "ground"
                         else ()))
    real_run = se._run

    def incomplete(cmd, cwd, timeout=600):
        done = real_run(cmd, cwd, timeout)
        if "--full" in cmd and "ground" in cmd[cmd.index("--result-json") + 1]:
            return type("Done", (), {"returncode": 70,
                                     "stderr": "ptest: incomplete (exit 70)"})()
        return done

    monkeypatch.setattr(se, "_run", incomplete)
    assert se.run_campaign(seen["args"]) == 0
    record = _report(seen)["mutants"][0]
    assert "ptest could not complete the run" in record["error"]
    assert "ptest: incomplete (exit 70)" in record["error"]
    assert record["misses"] == []


def test_each_run_exports_to_a_fresh_name_and_cleans_up(tmp_path,
                                                        monkeypatch):
    seen = _campaign(tmp_path, monkeypatch, v1=_Plan(), v2=_Plan(),
                     failing=lambda label, cwd: ())
    assert se.run_campaign(seen["args"]) == 0
    exports = [cmd[cmd.index("--result-json") + 1]
               for _, cmd, _ in seen["runs"]]
    assert len(set(exports)) == len(exports) == 2
    for scratch in seen["created"].values():
        assert not list(scratch.rglob(se.RESULT_EXPORT_PREFIX + "*"))
