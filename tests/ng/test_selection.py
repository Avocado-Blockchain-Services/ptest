from ptest import contracts as C
import hashlib
from dataclasses import replace

import pytest


def _snapshot(case, **overrides):
    baseline_head = overrides.pop("baseline_head", "a" * 40)
    return replace(case.snapshot(**overrides), baseline_head=baseline_head)


def _compatible_history(case, config, files=("tests/test_a.py",), input_digest=None):
    old = case.history(with_baseline=True).baseline
    inventory = case.inventory(files, outcome="passed")
    return C.HistoryView(baseline=C.Baseline(
        run_id=old.run_id, head=old.head, input_digest=input_digest or old.input_digest,
        compatibility="test-compat-v1", inventory=inventory,
        policy_digest=hashlib.sha256(repr(config.selection).encode()).hexdigest(),
        created_at=old.created_at,
    ))


def test_unmapped_runtime_change_forces_full(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    changed = _snapshot(case, changes=(C.Change(old=None, new="new_runtime/x.py", kind="added"),))
    plan = choose_plan(config, changed, _compatible_history(case, config), case.request())
    assert plan.execution == "full"
    assert any(reason.code == "unknown-input" for reason in plan.reasons)


def test_known_group_change_selects_its_declared_test(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(
        enabled=True, closed_inputs=True, input_roots=("src", "tests"),
        groups=(C.Group(name="core", sources=("src/core",), tests=("tests/test_core.py",)),),
    )
    config = case.config(selection=policy)
    changed = _snapshot(case, changes=(C.Change(old="src/core/a.py", new="src/core/a.py", kind="modified"),))
    plan = choose_plan(config, changed, _compatible_history(case, config, ("tests/test_core.py", "tests/test_other.py")), case.request())
    assert plan.execution == "selected"
    assert plan.files == ("tests/test_core.py",)


def test_overlapping_groups_promote_to_actual_full_gate_by_ratio(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"), full_ratio=.7,
        groups=(C.Group(name="one", sources=("src",), tests=("tests/a.py", "tests/b.py")),
                C.Group(name="two", sources=("src/core",), tests=("tests/c.py",))))
    config = case.config(selection=policy)
    changed = _snapshot(case, changes=(C.Change(old=None, new="src/core/x.py", kind="added"),))
    plan = choose_plan(config, changed, _compatible_history(case, config, ("tests/a.py", "tests/b.py", "tests/c.py")), case.request())
    assert plan.execution == "full"
    assert plan.reasons[0].message == "selection reaches full ratio"


def test_ignored_undeclared_and_no_test_overlap_fail_closed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    ignored = _snapshot(case, changes=(C.Change(old=None, new="generated/x.py", kind="ignored"),))
    assert choose_plan(config, ignored, _compatible_history(case, config), case.request()).execution == "full"
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src",), no_tests=("src/docs",))
    assert choose_plan(case.config(selection=policy), _snapshot(case, ), _compatible_history(case, case.config(selection=policy)), case.request()).execution == "full"


def test_explicit_full_and_scoped_never_auto_narrow(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = _snapshot(case, )
    history = _compatible_history(case, config)
    assert choose_plan(config, snapshot, history, case.request(mode=C.Mode.FULL)).execution == "full"
    assert choose_plan(config, snapshot, history, case.request(mode=C.Mode.SCOPED)).execution == "scoped"


def test_true_no_change_returns_no_tests_needed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = _snapshot(case, changes=())
    plan = choose_plan(config, snapshot, _compatible_history(case, config, input_digest=snapshot.digest), case.request())
    assert plan.execution == "none"
    assert any(reason.code == "no-tests-needed" for reason in plan.reasons)


def test_unexplained_fingerprint_change_cannot_be_no_tests_needed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    history = _compatible_history(case, config)
    changed = _snapshot(case, digest="d" * 64, changes=())
    assert choose_plan(config, changed, history, case.request()).execution == "full"


def test_config_lock_plugin_and_global_helper_changes_are_full_triggers(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"),
                               full_triggers=("pyproject.toml", "uv.lock", "conftest.py", "plugins"))
    config = case.config(selection=policy)
    history = _compatible_history(case, config)
    for path in ("pyproject.toml", "uv.lock", "conftest.py", "plugins/x.py"):
        changed = _snapshot(case, changes=(C.Change(old=path, new=path, kind="modified"),))
        assert choose_plan(config, changed, history, case.request()).execution == "full"


def test_output_overlap_and_failure_obligations_force_full(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src",), non_input_outputs=("src/report",))
    config = case.config(selection=policy)
    assert choose_plan(config, _snapshot(case, ), _compatible_history(case, config), case.request()).execution == "full"
    obligation = C.Obligation(file=None, test_id=None, sequence=1, source_digest=None, compatibility=None, reason="coverage")
    config = case.config(selection_enabled=True, closed_inputs=True)
    assert choose_plan(config, _snapshot(case, ), C.HistoryView(baseline=_compatible_history(case, config).baseline, obligations=(obligation,)), case.request()).execution == "full"


def test_incompatible_baseline_cannot_select(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = _snapshot(case, )
    baseline = _compatible_history(case, config, input_digest=snapshot.digest).baseline
    baseline = C.Baseline(run_id=baseline.run_id, head=baseline.head, input_digest=baseline.input_digest,
                          compatibility="different", inventory=baseline.inventory,
                          policy_digest=baseline.policy_digest, created_at=baseline.created_at)
    assert choose_plan(config, snapshot, C.HistoryView(baseline=baseline), case.request()).execution == "full"


@pytest.mark.parametrize("excluded", ["docs", "src", "generated"])
def test_no_tests_cannot_contain_an_input_contract(case, excluded):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True,
        input_roots=("src/core", "docs/api"), ignored_inputs=("generated/runtime",),
        no_tests=(excluded,),
        groups=(C.Group(name="core", sources=("src/core", "docs/api", "generated/runtime"),
                        tests=("tests/core",)),))
    config = case.config(selection=policy)
    plan = choose_plan(config, _snapshot(case, ), _compatible_history(case, config), case.request())
    assert plan.execution == "full"
    assert plan.reasons[0].code == "policy-invalid"


@pytest.mark.parametrize("pattern,path", [("*", "src/x.py"), ("src/?ore", "src/core"), ("src/[c]ore", "src/core")])
def test_no_tests_metacharacters_are_literal_prefixes(case, pattern, path):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True,
        no_tests=(pattern,))
    config = case.config(selection=policy)
    snap = _snapshot(case, changes=(C.Change(old=None, new=path, kind="added"),))
    assert choose_plan(config, snap, _compatible_history(case, config), case.request()).execution == "full"


def test_group_prefixes_and_always_files_are_expanded_and_unioned(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, full_ratio=1.0,
        always=("tests/smoke",), groups=(
            C.Group(name="core", sources=("src",), tests=("tests/core",)),
            C.Group(name="helper", sources=("src/helpers",), tests=("tests/dependent.py",))))
    config = case.config(selection=policy)
    files = ("tests/core/a.py", "tests/core/b.py", "tests/dependent.py", "tests/smoke/check.py", "tests/other.py")
    changed = _snapshot(case, changes=(C.Change(old=None, new="src/helpers/a.py", kind="added"),))
    plan = choose_plan(config, changed, _compatible_history(case, config, files), case.request())
    assert plan.execution == "selected"
    assert plan.files == tuple(sorted(files[:-1]))


def test_missing_always_prefix_is_incomplete_inventory(case):
    from ptest.selection import choose_plan

    config = case.config(selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        always=("tests/missing",), groups=(C.Group(name="core", sources=("src",), tests=("tests/core.py",)),)))
    snap = _snapshot(case, changes=(C.Change(old=None, new="src/a.py", kind="added"),))
    plan = choose_plan(config, snap, _compatible_history(case, config, ("tests/core.py", "tests/other.py")), case.request())
    assert plan.execution == "full"
    assert plan.reasons[0].code == "incomplete-inventory"


@pytest.mark.parametrize("kind,new", [("deleted", None), ("renamed", "tests/replacement.py")])
def test_removed_inventory_test_is_never_selected(case, kind, new):
    from ptest.selection import choose_plan

    config = case.config(selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        groups=(C.Group(name="core", sources=("src",), tests=("tests/core.py",)),)))
    snap = _snapshot(case, changes=(C.Change(old="tests/core.py", new=new, kind=kind),))
    plan = choose_plan(config, snap, _compatible_history(case, config, ("tests/core.py", "tests/other.py")), case.request())
    assert plan.execution == "full"
    assert plan.reasons[0].code == "incomplete-inventory"


def test_snapshot_without_baseline_delta_cannot_hide_committed_changes(case):
    from ptest.selection import choose_plan

    config = case.config(selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        groups=(C.Group(name="core", sources=("src",), tests=("tests/core.py",)),)))
    snap = _snapshot(case, baseline_head=None, head="b" * 40, changes=(C.Change(old=None, new="src/a.py", kind="added"),))
    assert choose_plan(config, snap, _compatible_history(case, config, ("tests/core.py", "tests/other.py")), case.request()).execution == "full"


def test_choose_plan_is_pure_without_subprocess_or_test_imports(case, monkeypatch):
    import builtins
    import subprocess
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snap = _snapshot(case, )
    history = _compatible_history(case, config, input_digest=snap.digest)
    real_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name in {"pytest", "conftest"} or name.startswith("test_"):
            raise AssertionError("static planning imported runner/test code")
        return real_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("static planning executed a subprocess")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert choose_plan(config, snap, history, case.request()).execution == "none"


def test_changed_test_dependency_unions_all_dependent_groups(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, full_ratio=1.0,
        groups=(C.Group(name="own", sources=("src/a",), tests=("tests/shared.py",)),
                C.Group(name="consumer", sources=("tests/shared.py",), tests=("tests/consumer.py",))))
    config = case.config(selection=policy)
    snap = _snapshot(case, changes=(C.Change(old="tests/shared.py", new="tests/shared.py", kind="modified"),))
    plan = choose_plan(config, snap, _compatible_history(case, config,
        ("tests/shared.py", "tests/consumer.py", "tests/unrelated.py")), case.request())
    assert plan.execution == "selected"
    assert plan.files == ("tests/consumer.py", "tests/shared.py")


def test_new_test_under_known_prefix_cannot_be_omitted(case):
    from ptest.selection import choose_plan

    config = case.config(selection=C.SelectionPolicy(enabled=True, closed_inputs=True,
        groups=(C.Group(name="core", sources=("src",), tests=("tests/core",)),)))
    snap = _snapshot(case, changes=(C.Change(old=None, new="tests/core/new.py", kind="untracked"),))
    plan = choose_plan(config, snap, _compatible_history(case, config,
        ("tests/core/existing.py", "tests/other.py")), case.request())
    assert plan.execution == "full"
    assert plan.reasons[0].code == "incomplete-inventory"


def test_mandatory_failure_is_not_hidden_by_docs_change(case):
    from ptest.selection import choose_plan

    config = case.config(selection=C.SelectionPolicy(enabled=True, closed_inputs=True, no_tests=("docs",)))
    history = _compatible_history(case, config, ("tests/failed.py", "tests/other.py"))
    history = replace(history, obligations=(C.Obligation(file="tests/failed.py", test_id="failed::id",
        sequence=1, source_digest=None, compatibility=None, reason="failure"),))
    snap = _snapshot(case, changes=(C.Change(old=None, new="docs/readme.md", kind="untracked"),))
    plan = choose_plan(config, snap, history, case.request())
    assert plan.execution == "selected" and plan.files == ("tests/failed.py",)
