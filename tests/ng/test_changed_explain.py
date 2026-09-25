"""Section B twins: --changed explains its plan and baseline outcome.

Pure twins target ``ptest.progress`` helpers; wiring twins target
``ptest.operations._emit_start`` / ``_emit_end`` / ``_baseline_note``
through capsys (no subprocess). Strict TDD: these fail before the fix.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ptest import contracts as C
from ptest import operations
from ptest import progress


def _reason(code: str, message: str) -> C.Reason:
    return C.Reason(code=code, message=message)


def _checkout() -> C.CheckoutIdentity:
    # Payload-only path: formatting input, never created on disk.
    return C.CheckoutIdentity(
        project_id="ab" * 16, checkout_id="cd" * 16, root=Path("/tmp/proj"))


def _result(case, **overrides) -> C.RunResult:
    request = case.request()
    plan = overrides.pop("plan", C.Plan(mode=C.Mode.AUTOMATIC, execution="full"))
    command = C.summarize_command(C.RunnerKind.PYTEST, C.Mode.FULL, ("pytest",))
    result = operations._result(
        run_id="ef" * 16, checkout=_checkout(), request=request, plan=plan,
        command=command, status=C.Status.PASSED, phase="complete",
        started="2026-09-25T00:00:00+00:00", runner_code=0, exit_code=0,
        origin="runner")
    return replace(result, **overrides)


# ---- B.1: selected start line ------------------------------------------------

def test_changed_selected_names_plan_counts():
    assert progress.format_changed_selected(
        selected=12, total=812, changed_files=3,
    ) == "changed: 12 of 812 test files (3 files changed)"


def test_changed_selected_singular_file():
    assert progress.format_changed_selected(
        selected=1, total=812, changed_files=1,
    ) == "changed: 1 of 812 test files (1 file changed)"


def test_emit_start_changed_selected(case, capsys):
    config = case.config()
    request = case.request()
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/test_a.py",), static_preview=True)
    snapshot = case.snapshot(changes=(
        C.Change(old=None, new="src/a.py", kind="added"),))
    history_view = case.history(with_baseline=True)
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1, snapshot=snapshot, history_view=history_view)
    err = capsys.readouterr().err
    assert err.splitlines()[0] == (
        "ptest: proj · pytest · changed: 1 of 1 test files (1 file changed)")


def test_changed_start_styles_prefix_and_project_on_tty():
    line = progress.format_changed_start(
        project="api", runner="pytest",
        segment="changed: 1 of 1 test files (1 file changed)", color=True)
    assert "\x1b[2mptest:\x1b[0m" in line
    assert "\x1b[1mapi\x1b[0m" in line
    assert line.endswith("changed: 1 of 1 test files (1 file changed)")


def test_changed_start_plain_without_tty(monkeypatch):
    segment = "changed: 1 of 1 test files (1 file changed)"
    assert progress.format_changed_start(
        project="api", runner="pytest", segment=segment) == (
        f"ptest: api · pytest · {segment}")
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in progress.format_changed_start(
        project="api", runner="pytest", segment=segment, color=True)


def test_baseline_note_styles_prefix_on_tty(case):
    result = _result(case, baseline_published=True)
    assert progress.format_baseline_note(
        result, color=True) == "\x1b[2mptest:\x1b[0m baseline recorded"


def test_baseline_note_plain_without_tty(case, monkeypatch):
    result = _result(case, baseline_published=True)
    assert progress.format_baseline_note(result) == "ptest: baseline recorded"
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in progress.format_baseline_note(result, color=True)


def test_emit_start_counts_test_files_not_test_items(case, capsys):
    config = case.config()
    request = case.request()
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/test_a.py", "tests/test_b.py"),
                  static_preview=True)
    snapshot = case.snapshot(changes=(
        C.Change(old=None, new="src/a.py", kind="added"),))
    records = (
        C.TestRecord(id="tests/test_a.py::t1", file="tests/test_a.py",
                     outcome=C.Outcome("passed"), setup_s=None, call_s=0.01,
                     teardown_s=None),
        C.TestRecord(id="tests/test_a.py::t2", file="tests/test_a.py",
                     outcome=C.Outcome("passed"), setup_s=None, call_s=0.01,
                     teardown_s=None),
        C.TestRecord(id="tests/test_a.py::t3", file="tests/test_a.py",
                     outcome=C.Outcome("passed"), setup_s=None, call_s=0.01,
                     teardown_s=None),
        C.TestRecord(id="tests/test_b.py::t1", file="tests/test_b.py",
                     outcome=C.Outcome("passed"), setup_s=None, call_s=0.01,
                     teardown_s=None),
        C.TestRecord(id="tests/test_b.py::t2", file="tests/test_b.py",
                     outcome=C.Outcome("passed"), setup_s=None, call_s=0.01,
                     teardown_s=None),
    )
    inventory = C.Inventory(adapter="pytest", version="9.1.1", complete=True,
                            tests=records, digest="99" * 32)
    baseline = C.Baseline(
        run_id="ab" * 16, head="a" * 40, input_digest="11" * 32,
        compatibility="test-compat-v1", inventory=inventory,
        policy_digest="22" * 32, created_at="2026-09-25T00:00:00+00:00")
    history_view = case.history(baseline=baseline)
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1, snapshot=snapshot, history_view=history_view)
    err = capsys.readouterr().err
    assert err.splitlines()[0] == (
        "ptest: proj · pytest · changed: 2 of 2 test files (1 file changed)")


# ---- B.1: changed -> full reason words ---------------------------------------

def test_explain_no_baseline():
    assert progress.explain_changed_full_reason(
        _reason("no-baseline", "no compatible baseline exists"),
    ) == "no baseline yet (this run records one if it passes on a clean tree)"


def test_explain_selection_disabled_names_config():
    assert progress.explain_changed_full_reason(
        _reason("selection-disabled", "selection is not explicitly closed"),
        config_name=".ptest.toml",
    ) == "selection is off in .ptest.toml (ptest doctor --fix)"


def test_explain_full_trigger_names_file():
    assert progress.explain_changed_full_reason(
        _reason("policy-changed", "full-trigger input changed"),
        changed_path="pyproject.toml",
    ) == "pyproject.toml is a full trigger"


def test_explain_outside_map_names_file():
    assert progress.explain_changed_full_reason(
        _reason("unknown-input", "changed path has no declared group"),
        changed_path="new_runtime/x.py",
    ) == "new_runtime/x.py is outside the selection map"


def test_explain_not_ancestor():
    assert progress.explain_changed_full_reason(
        _reason("incompatible-baseline",
                "snapshot does not cover this baseline's committed delta"),
    ) == "baseline is not an ancestor of HEAD"


def test_explain_policy_changed():
    assert progress.explain_changed_full_reason(
        _reason("incompatible-baseline",
                "baseline compatibility or policy changed"),
    ) == "policy changed"


def test_explain_remaining_full_codes():
    assert progress.explain_changed_full_reason(
        _reason("policy-invalid", "exclusion overlaps an input contract")) == "policy changed"
    assert progress.explain_changed_full_reason(
        _reason("selection-shadow-quarantine", "selection is quarantined")
    ) == "selection is quarantined (full suite required)"
    assert progress.explain_changed_full_reason(
        _reason("prior-failure", "failed test is absent from inventory")
    ) == "a failed test requires a full run"
    assert progress.explain_changed_full_reason(
        _reason("incomplete-inventory", "baseline inventory is incomplete")
    ) == "test inventory is incomplete"
    assert progress.explain_changed_full_reason(
        _reason("full-gate-obligation", "selection reaches full ratio")
    ) == "a full run is required"


def test_explain_full_reason_without_path_falls_back():
    assert progress.explain_changed_full_reason(
        _reason("policy-changed", "full-trigger input changed")) == "a full trigger changed"
    assert progress.explain_changed_full_reason(
        _reason("unknown-input", "static input evidence is incomplete")
    ) == "changed inputs could not be classified"
    assert progress.explain_changed_full_reason(None) == "a full run is required"


def test_emit_start_changed_full_no_baseline(case, capsys):
    config = case.config()
    request = case.request()
    plan = C.Plan(
        mode=C.Mode.AUTOMATIC, execution="full", static_preview=True,
        reasons=(_reason("no-baseline", "no compatible baseline exists"),))
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1, snapshot=case.snapshot(), history_view=case.history())
    err = capsys.readouterr().err
    assert err.splitlines()[0] == (
        "ptest: proj · pytest · changed → full suite: "
        "no baseline yet (this run records one if it passes on a clean tree)")


def test_emit_start_changed_full_trigger_names_file(case, capsys):
    config = case.config()
    request = case.request()
    plan = C.Plan(
        mode=C.Mode.AUTOMATIC, execution="full", static_preview=True,
        reasons=(_reason("policy-changed", "full-trigger input changed"),))
    snapshot = case.snapshot(changes=(
        C.Change(old=None, new="pyproject.toml", kind="added"),))
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1, snapshot=snapshot, history_view=case.history())
    err = capsys.readouterr().err
    assert "changed → full suite: pyproject.toml is a full trigger" in err


def test_emit_start_explicit_full_keeps_legacy_line(case, capsys):
    config = case.config()
    request = replace(case.request(), mode=C.Mode.FULL)
    plan = C.Plan(mode=C.Mode.FULL, execution="full")
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1)
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "ptest: proj · pytest · full suite"


def test_quiet_suppresses_changed_start_line(case, capsys):
    config = case.config()
    request = replace(case.request(), quiet=True)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/test_a.py",), static_preview=True)
    operations._emit_start(
        checkout=_checkout(), config=config, request=request, plan=plan,
        workers=1, snapshot=case.snapshot(), history_view=case.history())
    assert capsys.readouterr().err == ""


# ---- B.2: baseline end-line notes --------------------------------------------

def test_baseline_recorded_note(case):
    result = _result(case, baseline_published=True)
    assert progress.format_baseline_note(result) == "ptest: baseline recorded"


def test_no_baseline_recorded_names_failures(case):
    result = _result(
        case, status=C.Status.FAILED, exit_code=1,
        counts=C.Counts(failed=2, passed=8, executed=10, collected=10))
    assert progress.format_baseline_note(result) == (
        "ptest: no baseline recorded: 2 failures")


def test_no_baseline_recorded_singular_failure(case):
    result = _result(
        case, status=C.Status.FAILED, exit_code=1,
        counts=C.Counts(failed=1, passed=8, executed=9, collected=9))
    assert progress.format_baseline_note(result) == (
        "ptest: no baseline recorded: 1 failure")


def test_no_baseline_recorded_uncommitted_changes(case):
    dirty = case.snapshot(clean=False)
    result = _result(case, input_before=dirty, input_after=dirty)
    assert progress.format_baseline_note(result) == (
        "ptest: no baseline recorded: uncommitted changes")


def test_no_baseline_recorded_changed_during_run(case):
    before = case.snapshot(digest="aa" * 32)
    after = case.snapshot(digest="bb" * 32)
    result = _result(case, input_before=before, input_after=after)
    assert progress.format_baseline_note(result) == (
        "ptest: no baseline recorded: files changed during the run")


def test_no_baseline_recorded_incomplete_results(case):
    snap = case.snapshot()
    result = _result(
        case, input_before=snap, input_after=snap,
        reasons=(_reason("incomplete-inventory",
                         "the full test inventory is not conclusive"),))
    assert progress.format_baseline_note(result) == (
        "ptest: no baseline recorded: incomplete results")


def test_emit_end_prints_baseline_note_after_end_line(case, capsys):
    request = case.request()
    result = _result(case, baseline_published=True)
    operations._emit_end(request, result, 0.0,
                         baseline_note=progress.format_baseline_note(result))
    lines = capsys.readouterr().err.splitlines()
    assert lines[-2].startswith("ptest: passed")
    assert lines[-1] == "ptest: baseline recorded"


def test_emit_end_without_note_prints_only_end_line(case, capsys):
    request = case.request()
    result = _result(case, baseline_published=True)
    operations._emit_end(request, result, 0.0)
    lines = capsys.readouterr().err.splitlines()
    assert lines[-1].startswith("ptest: passed")
    assert not any("baseline" in line for line in lines)


def test_quiet_suppresses_baseline_note(case, capsys):
    request = replace(case.request(), quiet=True)
    result = _result(case, baseline_published=True)
    operations._emit_end(request, result, 0.0,
                         baseline_note=progress.format_baseline_note(result))
    assert capsys.readouterr().err == ""


# ---- B.2: note gating (full runs only) ---------------------------------------

def test_baseline_note_gating_selected_is_none(case):
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                  files=("tests/test_a.py",), static_preview=True)
    result = _result(case, plan=plan, baseline_published=False)
    assert operations._baseline_note(plan=plan, advanced=True, result=result) is None


def test_baseline_note_gating_non_advanced_is_none(case):
    plan = C.Plan(mode=C.Mode.FULL, execution="full")
    result = _result(case, plan=plan, baseline_published=False)
    assert operations._baseline_note(plan=plan, advanced=False, result=result) is None


def test_baseline_note_gating_full_advanced_recorded(case):
    plan = C.Plan(mode=C.Mode.FULL, execution="full")
    result = _result(case, plan=plan, baseline_published=True)
    assert operations._baseline_note(
        plan=plan, advanced=True, result=result) == "ptest: baseline recorded"


def test_baseline_note_gating_full_advanced_failures(case):
    plan = C.Plan(mode=C.Mode.FULL, execution="full")
    result = _result(
        case, plan=plan, status=C.Status.FAILED, exit_code=1,
        counts=C.Counts(failed=2, passed=8, executed=10, collected=10))
    assert operations._baseline_note(
        plan=plan, advanced=True, result=result) == (
        "ptest: no baseline recorded: 2 failures")
