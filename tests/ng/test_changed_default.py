"""Changed-by-default routing: bare `ptest` / `--changed` through impact.

Bare `ptest` and `ptest --changed` (no --shadow, no --probe) route through
``ptest.impact`` (``resolve_base`` + ``changed_files`` + ``plan``) instead of
the history/baseline engine, for single projects and monorepo children.
``ptest.impact`` is stubbed here through the ``cli._impact_api`` seam (plus a
``sys.modules`` entry) so these tests pin the ROUTING (request mapping,
start lines, end-line hint) rather than the graph.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import operations
from ptest import progress
from ptest.cli import main
from support import init_git_repo, write_ptest_toml


# --- stub ptest.impact -------------------------------------------------------

def _install_impact(monkeypatch, *, sha="abc123", label="origin/dev",
                    repo_changed=(), plans):
    """Install a fake ``ptest.impact`` module; return (module, seen)."""
    mod = types.ModuleType("ptest.impact")

    @dataclass(frozen=True, slots=True)
    class Base:
        sha: str | None
        label: str

    @dataclass(frozen=True, slots=True)
    class Impact:
        kind: str
        changed: tuple = ()
        files: tuple = ()
        direct: int = 0
        via: int = 0
        total: int = 0
        reason: str = ""
        ignored: int = 0

    seen: dict = {}
    base = Base(sha, label)
    mod.Base = Base
    mod.Impact = Impact
    mod.git_top = lambda start: Path(start)

    def resolve_base(top, explicit):
        seen["explicit"] = explicit
        return base

    def changed_files(top, resolved):
        seen["base"] = resolved
        return repo_changed

    def plan(top, project_root, config, changed):
        seen.setdefault("projects", []).append(Path(project_root).name)
        return plans[Path(project_root).name]

    mod.resolve_base = resolve_base
    mod.changed_files = changed_files
    mod.plan = plan
    monkeypatch.setitem(sys.modules, "ptest.impact", mod)
    # `cli._impact_api()` does `from . import impact`, which reads the
    # attribute on the `ptest` package before consulting `sys.modules`, so
    # the sys.modules entry alone is ignored once the real module is
    # imported (e.g. T1's test_impact.py does `from ptest import impact`).
    # This setattr is the binding that matters; the sys.modules entry is
    # kept for any direct sys.modules lookup.
    monkeypatch.setattr("ptest.cli._impact_api", lambda: mod)
    return mod, seen


def _selected(mod, changed=("services/credits.py", "a.py", "b.py"),
              files=("tests/test_a.py", "tests/test_b.py"),
              direct=1, via=1, total=10):
    return mod.Impact(kind="selected", changed=tuple(changed),
                      files=tuple(files), direct=direct, via=via, total=total)


def _capture(monkeypatch):
    calls = []

    def fake_execute(domain, config, request):
        calls.append(request)
        return types.SimpleNamespace(reasons=(), exit_code=0,
                                     status=C.Status.PASSED, counts=None)

    monkeypatch.setattr("ptest.operations.execute", fake_execute)
    return calls


def _standalone(tmp_path, monkeypatch, **toml):
    write_ptest_toml(tmp_path, kind="command", launcher=("true",),
                     args=(), full_args=(), project_id="ab" * 16, **toml)
    monkeypatch.chdir(tmp_path)


def _route_standalone(monkeypatch, project_name, make, *, repo_changed=(),
                      sha="abc123", label="origin/dev"):
    """Install the stub with plans built from the stub module itself."""
    mod, _ = _install_impact(monkeypatch, plans={})
    _, seen = _install_impact(monkeypatch, sha=sha, label=label,
                              repo_changed=repo_changed,
                              plans={project_name: make(mod)})
    return seen


# --- standalone routing ------------------------------------------------------

def test_bare_selected_routes_scoped_with_note(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name, _selected,
        repo_changed=("services/credits.py", "a.py", "b.py"))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/test_a.py", "tests/test_b.py")
    assert request.base is None
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): services/credits.py (+2 files) → "
        "2 of 10 test files (1 direct · 1 via importers)")
    assert request.next_hint is True


def test_changed_flag_matches_bare_request(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: _selected(mod, changed=("only.py",),
                              files=("tests/test_only.py",),
                              direct=1, via=0, total=4),
        repo_changed=("only.py",))
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.SCOPED
    assert calls[0].changed_note == (
        "changed vs origin/dev (no green run yet): only.py → 1 of 4 test files (1 direct · 0 via importers)")


def test_bare_full_routes_full_with_reason(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="full", changed=("uv.lock",),
                               reason="uv.lock is a full trigger"),
        repo_changed=("uv.lock",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.FULL
    assert request.argv == ()
    assert request.base is None
    assert request.changed_note == "changed vs origin/dev (no green run yet) → full suite: uv.lock is a full trigger"
    assert request.next_hint is True


def test_bare_vitest_delegates_changed_argv(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="vitest", changed=("src/a.ts", "src/b.ts")),
        repo_changed=("src/a.ts", "src/b.ts"))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("--changed", "abc123")
    assert request.base is None
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): src/a.ts (+1 file) → vitest --changed origin/dev")
    assert request.next_hint is True


def test_bare_vitest_without_sha_falls_back_to_head(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="vitest", changed=("src/a.ts",)),
        repo_changed=("src/a.ts",), sha=None, label="HEAD")
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls[0].argv == ("--changed", "HEAD")
    assert calls[0].changed_note == "changed vs HEAD (no green run yet): src/a.ts → vitest --changed HEAD"


def test_bare_none_with_changes_prints_no_tests_line(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="none", changed=("README.md",)),
        repo_changed=("README.md",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert (f"ptest: {tmp_path.name} · changed vs origin/dev (no green run yet): README.md → "
            "no tests affected\n") in err
    # R31: the "ptest --full runs everything" suffix is gone, and a verdict
    # without a selection report owes no gate hint (D11).
    assert "runs everything" not in err
    assert "gate at spec end" not in err


def test_bare_nothing_changed_prints_nothing_line(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="none", changed=()))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to test"
            "\n") in err
    assert "runs everything" not in err


def test_bare_verbose_reports_ignored_count(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="none", changed=(), ignored=61),
        repo_changed=())
    calls = _capture(monkeypatch)

    assert main(("-v",)) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert "ptest: -v ignored 61 non-code/output files" in err
    assert "no changes" in err


def test_bare_without_verbose_hides_ignored_count(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="none", changed=(), ignored=61),
        repo_changed=())
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert "ignored" not in err
    assert "no changes" in err


def test_base_ref_is_forwarded_to_resolve(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    seen = _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: _selected(mod, changed=("a.py",),
                              files=("tests/test_a.py",),
                              direct=0, via=1, total=5),
        repo_changed=("a.py",))
    calls = _capture(monkeypatch)

    assert main(("--changed", "--base", "myref")) == 0

    assert seen["explicit"] == "myref"
    assert len(calls) == 1


def test_shadow_bypasses_impact(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _, seen = _install_impact(monkeypatch, repo_changed=("a.py",), plans={})
    calls = _capture(monkeypatch)

    assert main(("--changed", "--shadow")) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.AUTOMATIC
    assert "projects" not in seen


def test_full_flag_ignores_impact(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _, seen = _install_impact(monkeypatch, repo_changed=("a.py",), plans={})
    calls = _capture(monkeypatch)

    assert main(("--full",)) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.FULL
    assert calls[0].changed_note is None
    assert calls[0].next_hint is False
    assert "projects" not in seen


def test_bad_base_ref_reports_config_error(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    mod, _ = _install_impact(monkeypatch, plans={})

    def bad_base(top, explicit):
        raise C.Problem(code="invalid-config",
                        message="--base is not a commit in this repository",
                        phase="config", retryable=False)

    mod.resolve_base = bad_base
    calls = _capture(monkeypatch)

    assert main(("--changed", "--base", "nope")) == 2
    assert calls == []
    assert "--base is not a commit" in capsys.readouterr().err


# --- monorepo routing --------------------------------------------------------

def _monorepo_root(tmp_path, monkeypatch, monorepo, plans):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    mod, _ = _install_impact(monkeypatch, plans={})
    by_child = dict(plans(mod))
    _, seen = _install_impact(monkeypatch, repo_changed=("api/a.py",),
                              plans=by_child)
    return seen


def test_monorepo_selected_child_runs_scoped_without_hint(
        tmp_path, monkeypatch, capsys, monorepo):
    seen = _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": _selected(mod), "web": mod.Impact(kind="none")})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/test_a.py", "tests/test_b.py")
    assert request.next_hint is False
    assert request.changed_note.startswith("changed vs origin/dev (no green run yet): services/credits.py")
    err = capsys.readouterr().err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err
    # D11 / R29: command runners carry no selection report, so the total
    # line owes no hint (the old `next:` hint is gone with NEXT_FULL).
    assert "next: ptest --full" not in err
    assert "gate at spec end" not in err
    assert seen["explicit"] is None


def test_monorepo_full_child_runs_full_gate(tmp_path, monkeypatch, monorepo):
    _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": mod.Impact(kind="full", changed=("uv.lock",),
                                       reason="uv.lock is a full trigger"),
                     "web": mod.Impact(kind="none")})
    calls = _capture(monkeypatch)

    assert main(("--changed",)) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.FULL
    assert calls[0].changed_note == "changed vs origin/dev (no green run yet) → full suite: uv.lock is a full trigger"
    assert calls[0].next_hint is False


def test_monorepo_vitest_child_delegates(tmp_path, monkeypatch, monorepo):
    _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": mod.Impact(kind="none"),
                     "web": mod.Impact(kind="vitest", changed=("src/a.ts",))})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.SCOPED
    assert calls[0].argv == ("--changed", "abc123")


def test_monorepo_nothing_changed_has_no_total(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    mod, _ = _install_impact(monkeypatch, plans={})
    none = mod.Impact(kind="none", changed=())
    _install_impact(monkeypatch, repo_changed=(),
                    plans={"api": none, "web": none})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to test"
            "\n") in err
    assert "ptest: total" not in err
    assert "ptest: api ·" not in err
    assert "ptest: web ·" not in err


def test_monorepo_failure_total_carries_fix_hint(
        tmp_path, monkeypatch, capsys, monorepo):
    _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": _selected(mod), "web": mod.Impact(kind="none")})

    def failing(domain, config, request):
        return types.SimpleNamespace(reasons=(), exit_code=1,
                                     status=C.Status.FAILED,
                                     counts=C.Counts(failed=1, passed=0))

    monkeypatch.setattr("ptest.operations.execute", failing)

    assert main(()) == 1

    err = capsys.readouterr().err
    assert "fix the code under test, then rerun ptest" in err


def test_monorepo_refinement_uses_child_project_id(
        tmp_path, monkeypatch, monorepo):
    _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": _selected(mod), "web": mod.Impact(kind="none")})
    _, seen = _install_selection(monkeypatch)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    child_ids = set()
    for name in ("api", "web"):
        text = (tmp_path / name / ".ptest.toml").read_text(encoding="utf-8")
        child_ids.add(text.split('project_id = "', 1)[1].split('"', 1)[0])
    assert len(child_ids) == 2
    planned_ids = {config.project_id for _, config in seen["plannings"]}
    assert planned_ids == child_ids
    refined_ids = {config.project_id for config, _ in seen["refines"]}
    assert refined_ids == child_ids


# --- progress + operations hint wiring ---------------------------------------

def _gate_report(**fields):
    base = dict(engine="dynamic", iteration=True, run_now=2, deferred=4,
                deferred_by_reason=(("b-static", "pkg/core.py", 4),),
                deferred_by_file=(("tests/test_a.py", 4),),
                gate_recorded=True)
    base.update(fields)
    return C.SelectionReport(**base)


def test_next_step_matrix():
    deferred = _gate_report()
    assert progress.next_step(C.Status.FAILED, None) == progress.NEXT_FIX
    assert progress.next_step(C.Status.FAILED, deferred) == progress.NEXT_FIX
    assert progress.next_step(C.Status.PASSED, deferred) == progress.GATE_HINT
    assert progress.next_step(C.Status.NO_TESTS_NEEDED, deferred) == progress.GATE_HINT
    assert progress.next_step(C.Status.PASSED, _gate_report(
        gate_recorded=False)) == progress.GATE_HINT_NO_GATE
    assert progress.next_step(C.Status.PASSED, None) is None
    assert progress.next_step(C.Status.CANCELLED, deferred) is None
    assert progress.next_step(C.Status.INCOMPLETE, deferred) is None
    assert progress.next_step(C.Status.NOT_RUN, deferred) is None


def test_format_impact_and_nothing_changed():
    assert progress.format_impact("api", "changed: a.py → 1 of 2 test files") == (
        "ptest: api · changed: a.py → 1 of 2 test files")
    assert progress.format_nothing_changed("origin/dev") == (
        "ptest: no changes vs origin/dev — nothing to test")
    assert progress.format_nothing_changed(
        "origin/dev", no_green_run=True) == (
        "ptest: no changes vs origin/dev (no green run yet) — nothing to test")
    assert progress.format_no_green_changes() == (
        "ptest: no changes since last green run — nothing to test")


def test_format_impact_styles_on_tty():
    line = progress.format_impact("api", "note", color=True)
    assert "\x1b[2mptest:\x1b[0m" in line
    assert "\x1b[1mapi\x1b[0m" in line


def test_format_end_next_step_suppresses_verbosity_hint():
    line = progress.format_end(
        C.Status.PASSED, counts=C.Counts(passed=12), duration_s=1.2,
        exit_code=0, hint=True, next_step=progress.GATE_HINT)
    assert progress.GATE_HINT in line
    assert progress.HINT not in line


def test_format_end_without_next_step_keeps_hint():
    line = progress.format_end(
        C.Status.PASSED, counts=C.Counts(passed=12), duration_s=1.2,
        exit_code=0, hint=True)
    assert progress.HINT in line


def test_emit_end_changed_pass_carries_gate_hint(case, capsys):
    from dataclasses import replace

    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                           changed_note="changed: a.py → 1 of 2 test files",
                           next_hint=True)
    result = replace(case.result(status=C.Status.PASSED, exit_code=0,
                                 counts=C.Counts(passed=12)),
                     selection=_gate_report())
    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "gate at spec end" in err
    assert "next: ptest --full" not in err
    assert progress.HINT not in err


def test_emit_end_changed_failure_carries_fix_hint(case, capsys):
    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                           changed_note="changed: a.py → 1 of 2 test files",
                           next_hint=True)
    result = case.result(status=C.Status.FAILED, exit_code=1,
                         counts=C.Counts(failed=1, passed=11))
    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "fix the code under test, then rerun ptest" in err


def test_emit_end_changed_full_pass_has_no_hint(case, capsys):
    request = C.RunRequest(mode=C.Mode.FULL,
                           changed_note="changed vs origin/dev (no green run yet) → full suite: uv.lock is a full trigger",
                           next_hint=True)
    result = case.result(status=C.Status.PASSED, exit_code=0,
                         counts=C.Counts(passed=12))
    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "next:" not in err
    assert "gate at spec end" not in err


def test_emit_end_without_next_hint_has_no_next_step(case, capsys):
    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",))
    result = case.result(status=C.Status.PASSED, exit_code=0,
                         counts=C.Counts(passed=12))
    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "next:" not in err


def test_emit_start_changed_note_overrides_scope(case, capsys):
    from pathlib import Path as _Path
    checkout = C.CheckoutIdentity(
        project_id="ab" * 16, checkout_id="cd" * 16, root=_Path("/tmp/api"))
    config = case.config()
    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                           changed_note="changed: a.py → 1 of 2 test files")
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped")
    operations._emit_start(checkout=checkout, config=config, request=request,
                           plan=plan, workers=2)
    err = capsys.readouterr().err
    assert err.splitlines()[0] == (
        "ptest: api · changed: a.py → 1 of 2 test files")


def test_run_request_validates_new_fields():
    with pytest.raises(TypeError):
        C.RunRequest(mode=C.Mode.SCOPED, changed_note=123)
    with pytest.raises(TypeError):
        C.RunRequest(mode=C.Mode.SCOPED, next_hint="yes")
    request = C.RunRequest(mode=C.Mode.SCOPED, changed_note="note", next_hint=True)
    assert request.changed_note == "note"
    assert request.next_hint is True
    assert C.RunRequest(mode=C.Mode.SCOPED).changed_note is None
    assert C.RunRequest(mode=C.Mode.SCOPED).next_hint is False


# --- doctor --fix next-step line ----------------------------------------------

def test_doctor_fix_prints_graph_next_step(tmp_path, monkeypatch, capsys):
    from ptest import cli as cli_api
    from ptest import doctor_fix

    write_ptest_toml(tmp_path, kind="pytest", launcher=("pytest",),
                     args=(), full_args=(), project_id="ab" * 16)
    monkeypatch.chdir(tmp_path)
    plan = doctor_fix.FixPlan(files=(doctor_fix.FileFix(
        rel=".ptest.toml", previous=b"", updated=b"",
        changes=(doctor_fix.FieldChange("selection", "enabled", True),)),))
    monkeypatch.setattr(doctor_fix, "plan_all", lambda root, resolution: plan)
    monkeypatch.setattr(doctor_fix, "render_diff", lambda plan: "")
    monkeypatch.setattr(doctor_fix, "apply_plan",
                        lambda root, plan: (".ptest.toml",))

    assert main(("doctor", "--fix")) == 0

    out = capsys.readouterr().out
    assert ("selection enabled: bare ptest now runs the tests your change reaches "
            "(no baseline needed); run ptest --full once at the end of the spec "
            "or chain") in out
    assert "record a baseline" not in out


# --- dynamic selection routing (T5) -------------------------------------------
#
# The static graph is stubbed through ``cli._impact_api`` as above; the
# dynamic refinement is stubbed through ``cli._selection_api`` so these
# tests pin the ROUTING (request mapping, start lines, -v escaping)
# rather than the planner.

def _install_selection(monkeypatch, *, refine=None):
    """Install a fake ``cli._selection_api``; return (module, seen)."""
    import contextlib

    mod = types.SimpleNamespace()
    seen: dict = {}

    @contextlib.contextmanager
    def planning(domain, config):
        seen["planning"] = (domain, config)
        seen.setdefault("plannings", []).append((domain, config))
        yield None

    def refine_fn(domain, top, root, config, changed, impact):
        seen.setdefault("refines", []).append((config, impact))
        return refine(impact) if refine is not None else impact

    mod.planning = planning
    mod.refine = refine_fn
    monkeypatch.setattr("ptest.cli._selection_api", lambda: mod)
    return mod, seen


def _dynamic(changed=("pkg/core.py",), files=("tests/test_a.py",),
             deselect=("tests/test_a.py::test_other",), tests=3, total=8,
             reached=0,
             units=(("function", "pkg/core.py:boot", 2),),
             details=("engine dynamic · 3 tests recorded · coverage 38% of "
                      "test files · store 4.0 KB · newest 5.0s ago",)):
    return types.SimpleNamespace(
        kind="selected", changed=tuple(changed), files=tuple(files),
        direct=1, via=1, total=total, reason="", ignored=0,
        engine="dynamic", dynamic_ok=True, relevant=tuple(changed),
        static_reason="", deselect=tuple(deselect), tests=tests,
        reached=reached,
        units=tuple(types.SimpleNamespace(kind=kind, label=label,
                                          tests=count)
                    for kind, label, count in units),
        details=tuple(details),
        project_index=None)


def _pytest_project(tmp_path, monkeypatch):
    write_ptest_toml(tmp_path, kind="pytest", launcher=("pytest",),
                     args=(), full_args=(), project_id="ab" * 16)
    monkeypatch.chdir(tmp_path)


def _route_dynamic(monkeypatch, project_name, dynamic, *,
                   repo_changed=("pkg/core.py",)):
    _install_impact(monkeypatch, plans={})
    mod, _ = _install_impact(monkeypatch, repo_changed=repo_changed,
                             plans={project_name: types.SimpleNamespace(
                                 kind="selected", changed=tuple(repo_changed),
                                 files=("tests/test_a.py",),
                                 direct=1, via=1, total=10, reason="",
                                 ignored=0)})
    _install_selection(monkeypatch, refine=lambda static: dynamic)
    return mod


def test_dynamic_selected_routes_scoped_with_deselect(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    _route_dynamic(monkeypatch, tmp_path.name, _dynamic())
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/test_a.py",)
    assert request.deselect == ("tests/test_a.py::test_other",)
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): pkg/core.py → "
        "3 tests in 1 of 8 files (dynamic · 1 function changed)")


def test_dynamic_selected_singular_counts(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    single = _dynamic(files=("tests/test_a.py",),
                      deselect=("tests/test_a.py::test_other",), tests=1,
                      total=1,
                      units=(("function", "pkg/core.py:boot", 1),))
    _route_dynamic(monkeypatch, tmp_path.name, single)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    assert calls[0].changed_note == (
        "changed vs origin/dev (no green run yet): pkg/core.py → "
        "1 test in 1 of 1 file (dynamic · 1 function changed)")


def test_dynamic_none_with_reach_prints_reached_line(tmp_path, monkeypatch,
                                                      capsys):
    _pytest_project(tmp_path, monkeypatch)
    none = _dynamic(files=(), deselect=(), tests=0, reached=5)
    none.kind = "none"
    _route_dynamic(monkeypatch, tmp_path.name, none)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("changed vs origin/dev (no green run yet): pkg/core.py → "
            "no tests affected: 5 tests reach these changes and already "
            "passed on this code") in err


def test_dynamic_none_without_reach_keeps_static_text(tmp_path, monkeypatch,
                                                      capsys):
    _pytest_project(tmp_path, monkeypatch)
    none = _dynamic(files=(), deselect=(), tests=0, reached=0)
    none.kind = "none"
    _route_dynamic(monkeypatch, tmp_path.name, none)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert "no tests affected\n" in err
    assert "runs everything" not in err


def test_dynamic_none_singular_reach(tmp_path, monkeypatch, capsys):
    _pytest_project(tmp_path, monkeypatch)
    none = _dynamic(files=(), deselect=(), tests=0, reached=1)
    none.kind = "none"
    _route_dynamic(monkeypatch, tmp_path.name, none)
    _capture(monkeypatch)

    assert main(()) == 0

    err = capsys.readouterr().err
    assert ("no tests affected: 1 test reaches these changes and already "
            "passed on this code") in err


def test_static_fallback_note_names_reason(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    static = _dynamic()
    static.engine = "static"
    static.static_reason = "no dependency records yet — any run records them"
    static.deselect = ()
    static.tests = 0
    _route_dynamic(monkeypatch, tmp_path.name, static)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.deselect == ()
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): pkg/core.py → "
        "1 of 8 test files (static: no dependency records yet — any run "
        "records them · 1 direct · 1 via importers)")


def test_dynamic_full_routes_full_with_dynamic_reason(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    full = _dynamic()
    full.kind = "full"
    full.files = ()
    full.deselect = ()
    full.reason = "3 of 4 test files reach full_ratio 0.7 (dynamic)"
    _route_dynamic(monkeypatch, tmp_path.name, full)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.FULL
    assert request.argv == ()
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet) → full suite: "
        "3 of 4 test files reach full_ratio 0.7 (dynamic)")


def test_dynamic_verbose_details_are_escaped(tmp_path, monkeypatch, capsys):
    _pytest_project(tmp_path, monkeypatch)
    evil = _dynamic(details=("changed function evil\x1b[31m.py:boot → 1 test",))
    _route_dynamic(monkeypatch, tmp_path.name, evil)
    calls = _capture(monkeypatch)

    assert main(("-v",)) == 0

    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "ptest: -v selection: changed function evil\\x1b[31m.py:boot → 1 test" in err
    assert "\x1b[31m" not in err


def test_dynamic_quiet_hides_verbose_details(tmp_path, monkeypatch, capsys):
    _pytest_project(tmp_path, monkeypatch)
    _route_dynamic(monkeypatch, tmp_path.name, _dynamic())
    calls = _capture(monkeypatch)

    assert main(("-q",)) == 0

    assert len(calls) == 1
    assert "-v selection:" not in capsys.readouterr().err


def test_planning_receives_project_config(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    _, seen = _install_selection(monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: _selected(mod, changed=("services/credits.py",),
                              files=("tests/test_a.py", "tests/test_b.py"),
                              direct=1, via=1, total=10),
        repo_changed=("services/credits.py",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    assert seen["planning"][1].project_id == "ab" * 16
    assert seen["refines"][0][1].kind == "selected"
    # A stubbed static verdict passes through with a byte-identical note.
    assert calls[0].changed_note == (
        "changed vs origin/dev (no green run yet): services/credits.py → "
        "2 of 10 test files (1 direct · 1 via importers)")
    assert calls[0].deselect == ()


def test_folder_run_filters_deselect_to_folder(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("def test_a(): pass\n")
    (tests_dir / "test_named.py").write_text("def test_1(): pass\n")
    dynamic = _dynamic(files=("tests/test_a.py", "tests/test_named.py"),
                       deselect=("tests/test_a.py::test_skip",
                                 "tests/test_named.py::test_skip_too"),
                       tests=4, total=8)
    _route_dynamic(monkeypatch, tmp_path.name, dynamic)
    calls = _capture(monkeypatch)

    assert main(("tests", "tests/test_named.py::test_1")) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert "tests/test_a.py" in request.argv
    # Deselected ids survive only under the folder files, never inside
    # explicitly named files.
    assert request.deselect == ("tests/test_a.py::test_skip",)


# --- 0.7.0 tiered selection: CLI lines, routing, totals (T6) ------------------
#
# Oracle: docs/specs/2026-10-19-tiered-iteration-selection.md R10, R12, R29,
# R31, R39, R40, R44 and Decision 10. The refinement is stubbed through
# ``cli._selection_api`` so these pin the CLI, not the planner.

_CONDITIONAL_ID = "tests/test_a.py::test_observer"
_FP = "c" * 64


def _tier_report(*, deferred=7, run_now=0, gate_recorded=True,
                 code="b-static", path="pkg/models.py", conditional=(),
                 engine="dynamic"):
    rows = (((code, path, deferred),) if deferred else ())
    files = ((("tests/test_a.py", deferred),) if deferred else ())
    return C.SelectionReport(
        engine=engine, iteration=True, run_now=run_now, deferred=deferred,
        deferred_by_reason=rows, deferred_by_file=files,
        gate_recorded=gate_recorded, conditional=tuple(conditional),
        observers_deferred=len(conditional))


def _tiered(kind="none", *, changed=("pkg/core.py",), files=(), deselect=(),
            tests=0, reached=0, selection=None, conditional=(), total=8,
            engine="dynamic"):
    """A refined verdict as ``selection_engine.refine`` returns it."""
    return types.SimpleNamespace(
        kind=kind, changed=tuple(changed), files=tuple(files), direct=1,
        via=1, total=total, reason="", ignored=0, engine=engine,
        dynamic_ok=True, relevant=tuple(changed), static_reason="",
        deselect=tuple(deselect), tests=tests, reached=reached,
        units=(types.SimpleNamespace(kind="unrecorded", label="x", tests=1),),
        details=(), project_index=None, selection=selection,
        conditional=tuple(conditional))


def _empty_static(mod, changed=()):
    return mod.Impact(kind="none", changed=tuple(changed))


def _route_tiered(monkeypatch, project_name, refined, *, repo_changed=(),
                  static_changed=None):
    """Stub the static plan (kind none) and the refinement result."""
    _install_impact(monkeypatch, plans={})
    mod, _ = _install_impact(
        monkeypatch, repo_changed=repo_changed,
        plans={project_name: types.SimpleNamespace(
            kind="none", changed=tuple(
                repo_changed if static_changed is None else static_changed),
            files=(), direct=0, via=0, total=0, reason="", ignored=0)})
    _install_selection(monkeypatch, refine=lambda static: refined)
    return mod


def test_no_run_line_never_calls_deferred_unaffected(tmp_path, monkeypatch, capsys):
    # R31: with T2 tests the line says "nothing to run now"; "no tests
    # affected" is only for a plan with nothing deferred.
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=14300, code="g-shared",
                          path="api/models.py")
    _route_tiered(monkeypatch, tmp_path.name,
                  _tiered(selection=report, reached=5))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("changed vs origin/dev (no green run yet): pkg/core.py → "
            "nothing to run now · deferred 14300 (shared import-time state "
            "via api/models.py) · gate at spec end\n") in err
    assert "no tests affected" not in err
    assert "already passed" not in err
    assert "runs everything" not in err


@pytest.mark.parametrize("reached,gate,tail", [
    (5, True, "no tests affected: 5 tests reach these changes and already "
              "passed on this code"),
    (0, True, "no tests affected"),
    (0, False, "no tests affected · gate at spec end (no gate recorded)"),
])
def test_no_run_line_without_t2_keeps_no_tests_affected(
        tmp_path, monkeypatch, capsys, reached, gate, tail):
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=0, gate_recorded=gate)
    _route_tiered(monkeypatch, tmp_path.name,
                  _tiered(selection=report, reached=reached))
    _capture(monkeypatch)

    assert main(()) == 0

    err = capsys.readouterr().err
    assert f"pkg/core.py → {tail}\n" in err
    assert "nothing to run now" not in err


def test_empty_diff_runs_t1_nodes(tmp_path, monkeypatch, capsys):
    # R10: an empty changed set still plans; T1 nodes run with the
    # `{reference}: no changes → …` start line.
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=0, run_now=3)
    refined = _tiered("selected", changed=(), files=("tests/test_a.py",),
                      deselect=("tests/test_a.py::test_other",), tests=3,
                      selection=report)
    _route_tiered(monkeypatch, tmp_path.name, refined, repo_changed=())
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/test_a.py",)
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): no changes → "
        "3 tests in 1 of 8 files (dynamic · 1 unrecorded test file)")
    assert request.selection is report
    assert "nothing to test" not in capsys.readouterr().err


def test_empty_diff_still_reports_deferred(tmp_path, monkeypatch, capsys):
    # R31: an empty diff with T2 tests says so instead of "nothing to test".
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=9, code="u-static", path="tests/test_new.py")
    _route_tiered(monkeypatch, tmp_path.name,
                  _tiered(changed=(), selection=report), repo_changed=())
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to "
            "run now · deferred 9 (unrecorded since gate via "
            "tests/test_new.py) · gate at spec end\n") in err
    assert "nothing to test" not in err


def test_empty_diff_without_deferred_keeps_nothing_to_test_with_hint(
        tmp_path, monkeypatch, capsys):
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=0, gate_recorded=False)
    _route_tiered(monkeypatch, tmp_path.name,
                  _tiered(changed=(), selection=report), repo_changed=())
    _capture(monkeypatch)

    assert main(()) == 0

    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to "
            "test · gate at spec end (no gate recorded)\n"
            ) in capsys.readouterr().err


def test_conditional_only_plan_routes_through_execute(tmp_path, monkeypatch):
    # R44 / D8: a plan with conditional entries and no T1 node executes.
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(
        deferred=2, code="g-shared", path="pkg/models.py",
        conditional=((_CONDITIONAL_ID, "g-shared", "pkg/models.py"),))
    refined = _tiered(
        "none", changed=("pkg/models.py",), files=("tests/test_a.py",),
        deselect=(_CONDITIONAL_ID, "tests/test_a.py::test_other"),
        selection=report, conditional=((_CONDITIONAL_ID, _FP),))
    _route_tiered(monkeypatch, tmp_path.name, refined,
                  repo_changed=("pkg/models.py",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/test_a.py",)
    assert request.deselect == (_CONDITIONAL_ID, "tests/test_a.py::test_other")
    assert request.conditional == ((_CONDITIONAL_ID, _FP),)
    assert request.selection is report
    assert request.changed_note == (
        "changed vs origin/dev (no green run yet): pkg/models.py → "
        "checking 1 observer · deferred 2 (shared import-time state via "
        "pkg/models.py)")
    assert "gate at spec end" not in request.changed_note


def test_selected_plan_carries_selection_and_conditional(tmp_path, monkeypatch):
    _pytest_project(tmp_path, monkeypatch)
    report = _tier_report(deferred=3, run_now=2)
    refined = _tiered(
        "selected", files=("tests/test_a.py",), tests=2, selection=report,
        deselect=(_CONDITIONAL_ID,), conditional=((_CONDITIONAL_ID, None),))
    _route_tiered(monkeypatch, tmp_path.name, refined,
                  repo_changed=("pkg/core.py",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls[0].selection is report
    assert calls[0].conditional == ((_CONDITIONAL_ID, None),)


def test_forced_full_run_carries_its_reason_report(tmp_path, monkeypatch):
    # R8a: the verdict's full_reason report rides to the end line / JSON.
    _pytest_project(tmp_path, monkeypatch)
    report = C.SelectionReport(engine=None, iteration=True, run_now=None,
                               deferred=0,
                               full_reason="uv.lock is a full trigger")
    refined = _tiered("full", changed=("uv.lock",), selection=report)
    refined.reason = "uv.lock is a full trigger"
    _route_tiered(monkeypatch, tmp_path.name, refined,
                  repo_changed=("uv.lock",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls[0].mode is C.Mode.FULL
    assert calls[0].selection is report
    assert calls[0].selection.full_reason == "uv.lock is a full trigger"


def test_vitest_changed_reports_engine_and_bare_gate_hint(
        tmp_path, monkeypatch, capsys, case):
    # R12: a Vitest changed run reports engine vitest-changed with no counts
    # and its end line ends with the bare hint, never "(no gate recorded)".
    from dataclasses import replace

    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="vitest", changed=("src/a.ts",)),
        repo_changed=("src/a.ts",))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    report = calls[0].selection
    assert report == C.SelectionReport(engine="vitest-changed", iteration=True,
                                       run_now=None, deferred=None)
    assert report.gate_recorded is False
    result = replace(case.result(status=C.Status.PASSED, exit_code=0,
                                 counts=C.Counts(passed=4)), selection=report)
    operations._emit_end(calls[0], result, 0.0)
    err = capsys.readouterr().err.rstrip()
    assert err.endswith("· gate at spec end")
    assert "no gate recorded" not in err
    assert "deferred" not in err


def test_vitest_child_in_a_monorepo_reports_vitest_changed(
        tmp_path, monkeypatch, monorepo):
    _monorepo_root(
        tmp_path, monkeypatch, monorepo,
        lambda mod: {"api": mod.Impact(kind="none"),
                     "web": mod.Impact(kind="vitest", changed=("src/a.ts",))})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls[0].selection.engine == "vitest-changed"
    assert calls[0].selection.iteration is True
    assert (calls[0].selection.run_now, calls[0].selection.deferred) == (None, None)


def test_vitest_folder_run_reports_vitest_changed(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    (tmp_path / "src").mkdir()
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="vitest", changed=("src/a.ts",)),
        repo_changed=("src/a.ts",))
    calls = _capture(monkeypatch)

    assert main(("src",)) == 0

    assert calls[0].selection.engine == "vitest-changed"


# --- monorepo totals (R39, Decision 10) ---------------------------------------

def _tiered_monorepo(tmp_path, monkeypatch, monorepo, by_child, *,
                     repo_changed=("api/a.py",)):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    _install_impact(monkeypatch, plans={})
    plans = {name: types.SimpleNamespace(
        kind="none", changed=(f"{name}/x.py",), files=(), direct=0, via=0,
        total=0, reason="", ignored=0) for name in by_child}
    _install_impact(monkeypatch, repo_changed=repo_changed, plans=plans)
    refined = {f"{name}/x.py": value for name, value in by_child.items()}
    _install_selection(
        monkeypatch, refine=lambda static: refined[static.changed[0]])


def test_monorepo_total_sums_deferred_across_children(
        tmp_path, monkeypatch, capsys, monorepo):
    api = _tiered(changed=("api/x.py",),
                  selection=_tier_report(deferred=3, path="api/m.py"))
    web = _tiered(changed=("web/x.py",),
                  selection=_tier_report(deferred=4, path="web/m.py",
                                         gate_recorded=False))
    _tiered_monorepo(tmp_path, monkeypatch, monorepo,
                     {"api": api, "web": web})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    lines = err.splitlines()
    # Oracle: R31 bullet 1 / R39: a non-empty changed set prints the
    # "nothing to run now" form; "no changes" is for an empty changed set.
    assert not any("no changes" in line for line in lines)
    assert any(line.startswith("ptest: api · ")
               and "nothing to run now · deferred 3 (static reach via "
                   "api/m.py)" in line and "gate at spec end" not in line
               for line in lines)
    assert any(line.startswith("ptest: web · ")
               and "nothing to run now · deferred 4 (static reach via "
                   "web/m.py)" in line and "gate at spec end" not in line
               for line in lines)
    total = [line for line in lines if line.startswith("ptest: total")]
    assert len(total) == 1
    assert "deferred 7 across 2 children" in total[0]
    assert total[0].endswith("gate at spec end (no gate recorded)")
    assert total[0].count("gate at spec end") == 1
    assert "nothing to test" not in err


def test_monorepo_nothing_anywhere_only_when_no_child_defers(
        tmp_path, monkeypatch, capsys, monorepo):
    api = _tiered(changed=("api/x.py",), selection=_tier_report(deferred=0))
    web = _tiered(changed=("web/x.py",),
                  selection=_tier_report(deferred=5, path="web/m.py"))
    _tiered_monorepo(tmp_path, monkeypatch, monorepo,
                     {"api": api, "web": web})
    _capture(monkeypatch)

    assert main(()) == 0

    err = capsys.readouterr().err
    assert "ptest: no changes —" not in err
    assert "nothing to run now · deferred 5 (static reach via web/m.py)" in err
    assert "ptest: web · no changes" not in err


# --- R40: folder scopes ---------------------------------------------------------

def test_folder_run_counts_only_deferred_under_the_folder(
        tmp_path, monkeypatch, capsys):
    _pytest_project(tmp_path, monkeypatch)
    (tmp_path / "tests").mkdir()
    whole = _tier_report(deferred=9, path="pkg/models.py")
    refined = _tiered(changed=("pkg/core.py",), selection=whole)
    _route_tiered(monkeypatch, tmp_path.name, refined,
                  repo_changed=("pkg/core.py",))
    seen = {}

    def narrow(impact, folders, whole=()):
        seen["folders"] = tuple(folders)
        seen["whole"] = tuple(whole)
        return types.SimpleNamespace(**{
            **vars(impact),
            "selection": _tier_report(deferred=2, path="pkg/models.py")})

    import ptest.cli as cli_module
    api = cli_module._selection_api()
    api.narrow = narrow
    calls = _capture(monkeypatch)

    assert main(("tests",)) == 0

    assert seen["folders"] == ("tests",)
    assert seen["whole"] == ()
    assert calls == []
    err = capsys.readouterr().err
    assert ("no changes under tests — nothing to run now · deferred 2 "
            "(static reach via pkg/models.py) · gate at spec end") in err
    assert "deferred 9" not in err


def test_folder_run_hands_its_explicit_files_to_narrow_as_whole(
        tmp_path, monkeypatch, capsys):
    """R30: in ``ptest <folder> <file>`` the named file runs whole, so the
    narrowing must know it to keep its nodes out of the deferred count."""
    _pytest_project(tmp_path, monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    refined = _tiered(changed=("pkg/core.py",),
                      selection=_tier_report(deferred=9, path="pkg/models.py"))
    _route_tiered(monkeypatch, tmp_path.name, refined,
                  repo_changed=("pkg/core.py",))
    seen = {}

    def narrow(impact, folders, whole=()):
        seen["folders"] = tuple(folders)
        seen["whole"] = tuple(whole)
        return impact

    import ptest.cli as cli_module
    api = cli_module._selection_api()
    api.narrow = narrow
    _capture(monkeypatch)

    main(("tests", "tests/test_x.py"))

    assert seen == {"folders": ("tests",), "whole": ("tests/test_x.py",)}


def test_monorepo_conditional_child_executes_and_total_uses_its_result(
        tmp_path, monkeypatch, capsys, monorepo):
    # R44 in a monorepo: a child with only conditional observers executes
    # (children carry no hint), and the total line sums the counts the child's
    # RESULT reports after promotion, not the planned ones.
    report = _tier_report(
        deferred=4, code="g-shared", path="api/models.py",
        conditional=((_CONDITIONAL_ID, "g-shared", "api/models.py"),))
    api = _tiered("none", changed=("api/x.py",), files=("tests/test_a.py",),
                  deselect=(_CONDITIONAL_ID,), selection=report,
                  conditional=((_CONDITIONAL_ID, _FP),))
    web = _tiered(changed=("web/x.py",),
                  selection=_tier_report(deferred=0))
    _tiered_monorepo(tmp_path, monkeypatch, monorepo, {"api": api, "web": web})
    calls = []
    promoted = _tier_report(deferred=3, run_now=1)

    def fake_execute(domain, config, request):
        calls.append(request)
        return types.SimpleNamespace(
            reasons=(), exit_code=0, status=C.Status.PASSED,
            counts=C.Counts(passed=1), selection=promoted)

    monkeypatch.setattr("ptest.operations.execute", fake_execute)

    assert main(()) == 0

    assert len(calls) == 1
    assert calls[0].conditional == ((_CONDITIONAL_ID, _FP),)
    assert calls[0].next_hint is False
    assert calls[0].selection is report
    lines = capsys.readouterr().err.splitlines()
    (total,) = [line for line in lines if line.startswith("ptest: total")]
    assert "deferred 3 across 1 children" in total
    assert total.endswith("gate at spec end")
