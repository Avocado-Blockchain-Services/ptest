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
            "no tests affected · ptest --full runs everything") in err


def test_bare_nothing_changed_prints_nothing_line(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _route_standalone(
        monkeypatch, tmp_path.name,
        lambda mod: mod.Impact(kind="none", changed=()))
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to test · "
            "ptest --full runs everything") in err


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
    assert "next: ptest --full before handoff" in err
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
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to test · "
            "ptest --full runs everything") in err
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

def test_next_step_matrix():
    assert progress.next_step(C.Status.FAILED, False) == progress.NEXT_FIX
    assert progress.next_step(C.Status.FAILED, True) == progress.NEXT_FIX
    assert progress.next_step(C.Status.PASSED, True) == progress.NEXT_FULL
    assert progress.next_step(C.Status.NO_TESTS_NEEDED, True) == progress.NEXT_FULL
    assert progress.next_step(C.Status.PASSED, False) is None
    assert progress.next_step(C.Status.CANCELLED, True) is None
    assert progress.next_step(C.Status.INCOMPLETE, True) is None
    assert progress.next_step(C.Status.NOT_RUN, True) is None


def test_format_impact_and_nothing_changed():
    assert progress.format_impact("api", "changed: a.py → 1 of 2 test files") == (
        "ptest: api · changed: a.py → 1 of 2 test files")
    assert progress.format_nothing_changed("origin/dev") == (
        "ptest: no changes vs origin/dev — nothing to test · "
        "ptest --full runs everything")
    assert progress.format_nothing_changed(
        "origin/dev", no_green_run=True) == (
        "ptest: no changes vs origin/dev (no green run yet) — nothing to test · "
        "ptest --full runs everything")
    assert progress.format_no_green_changes() == (
        "ptest: no changes since last green run — nothing to test · "
        "ptest --full runs everything")


def test_format_impact_styles_on_tty():
    line = progress.format_impact("api", "note", color=True)
    assert "\x1b[2mptest:\x1b[0m" in line
    assert "\x1b[1mapi\x1b[0m" in line


def test_format_end_next_step_suppresses_verbosity_hint():
    line = progress.format_end(
        C.Status.PASSED, counts=C.Counts(passed=12), duration_s=1.2,
        exit_code=0, hint=True, next_step="next: ptest --full before handoff")
    assert "next: ptest --full before handoff" in line
    assert progress.HINT not in line


def test_format_end_without_next_step_keeps_hint():
    line = progress.format_end(
        C.Status.PASSED, counts=C.Counts(passed=12), duration_s=1.2,
        exit_code=0, hint=True)
    assert progress.HINT in line


def test_emit_end_changed_pass_carries_next_full(case, capsys):
    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                           changed_note="changed: a.py → 1 of 2 test files",
                           next_hint=True)
    result = case.result(status=C.Status.PASSED, exit_code=0,
                         counts=C.Counts(passed=12))
    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "next: ptest --full before handoff" in err
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
            "(no baseline needed); run ptest --full once before handoff") in out
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
    assert ("no tests affected · ptest --full runs everything") in err


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
    evil = _dynamic(details=("changed function evil\x1b[31m.py:boot → 1 tests",))
    _route_dynamic(monkeypatch, tmp_path.name, evil)
    calls = _capture(monkeypatch)

    assert main(("-v",)) == 0

    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "ptest: -v selection: changed function evil\\x1b[31m.py:boot → 1 tests" in err
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
