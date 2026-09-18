from ptest import contracts as C
import hashlib


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
    changed = case.snapshot(changes=(C.Change(old=None, new="new_runtime/x.py", kind="added"),))
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
    changed = case.snapshot(changes=(C.Change(old="src/core/a.py", new="src/core/a.py", kind="modified"),))
    plan = choose_plan(config, changed, _compatible_history(case, config, ("tests/test_core.py", "tests/test_other.py")), case.request())
    assert plan.execution == "selected"
    assert plan.files == ("tests/test_core.py",)


def test_overlap_always_and_ratio_promote_to_actual_full_gate(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"), always=("src/always",), full_ratio=.7,
        groups=(C.Group(name="one", sources=("src",), tests=("tests/a.py", "tests/b.py")),
                C.Group(name="two", sources=("src/core",), tests=("tests/c.py",))))
    config = case.config(selection=policy)
    changed = case.snapshot(changes=(C.Change(old=None, new="src/core/x.py", kind="added"),))
    plan = choose_plan(config, changed, _compatible_history(case, config, ("tests/a.py", "tests/b.py", "tests/c.py")), case.request())
    assert plan.execution == "full"


def test_ignored_undeclared_and_no_test_overlap_fail_closed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    ignored = case.snapshot(changes=(C.Change(old=None, new="generated/x.py", kind="ignored"),))
    assert choose_plan(config, ignored, _compatible_history(case, config), case.request()).execution == "full"
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src",), no_tests=("src/docs",))
    assert choose_plan(case.config(selection=policy), case.snapshot(), _compatible_history(case, case.config(selection=policy)), case.request()).execution == "full"


def test_explicit_full_and_scoped_never_auto_narrow(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = case.snapshot()
    history = _compatible_history(case, config)
    assert choose_plan(config, snapshot, history, case.request(mode=C.Mode.FULL)).execution == "full"
    assert choose_plan(config, snapshot, history, case.request(mode=C.Mode.SCOPED)).execution == "scoped"


def test_true_no_change_returns_no_tests_needed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = case.snapshot(changes=())
    plan = choose_plan(config, snapshot, _compatible_history(case, config, input_digest=snapshot.digest), case.request())
    assert plan.execution == "none"
    assert any(reason.code == "no-tests-needed" for reason in plan.reasons)


def test_unexplained_fingerprint_change_cannot_be_no_tests_needed(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    history = _compatible_history(case, config)
    changed = case.snapshot(digest="d" * 64, changes=())
    assert choose_plan(config, changed, history, case.request()).execution == "full"


def test_config_lock_plugin_and_global_helper_changes_are_full_triggers(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"),
                               full_triggers=("pyproject.toml", "uv.lock", "conftest.py", "plugins"))
    config = case.config(selection=policy)
    history = _compatible_history(case, config)
    for path in ("pyproject.toml", "uv.lock", "conftest.py", "plugins/x.py"):
        changed = case.snapshot(changes=(C.Change(old=path, new=path, kind="modified"),))
        assert choose_plan(config, changed, history, case.request()).execution == "full"


def test_output_overlap_and_failure_obligations_force_full(case):
    from ptest.selection import choose_plan

    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src",), non_input_outputs=("src/report",))
    config = case.config(selection=policy)
    assert choose_plan(config, case.snapshot(), _compatible_history(case, config), case.request()).execution == "full"
    obligation = C.Obligation(file=None, test_id=None, sequence=1, source_digest=None, compatibility=None, reason="coverage")
    config = case.config(selection_enabled=True, closed_inputs=True)
    assert choose_plan(config, case.snapshot(), C.HistoryView(baseline=_compatible_history(case, config).baseline, obligations=(obligation,)), case.request()).execution == "full"


def test_incompatible_baseline_cannot_select(case):
    from ptest.selection import choose_plan

    config = case.config(selection_enabled=True, closed_inputs=True)
    snapshot = case.snapshot()
    baseline = _compatible_history(case, config, input_digest=snapshot.digest).baseline
    baseline = C.Baseline(run_id=baseline.run_id, head=baseline.head, input_digest=baseline.input_digest,
                          compatibility="different", inventory=baseline.inventory,
                          policy_digest=baseline.policy_digest, created_at=baseline.created_at)
    assert choose_plan(config, snapshot, C.HistoryView(baseline=baseline), case.request()).execution == "full"
