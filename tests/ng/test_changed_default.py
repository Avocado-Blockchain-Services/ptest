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
        "changed: services/credits.py (+2 files) → "
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
        "changed: only.py → 1 of 4 test files (1 direct · 0 via importers)")


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
    assert request.changed_note == "changed → full suite: uv.lock is a full trigger"
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
        "changed: src/a.ts (+1 file) → vitest --changed origin/dev")
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
    assert calls[0].changed_note == "changed: src/a.ts → vitest --changed HEAD"


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
    assert (f"ptest: {tmp_path.name} · changed: README.md → "
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
    assert ("ptest: no changes vs origin/dev — nothing to test · "
            "ptest --full runs everything") in err


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
    assert request.changed_note.startswith("changed: services/credits.py")
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
    assert calls[0].changed_note == "changed → full suite: uv.lock is a full trigger"
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
    assert ("ptest: no changes vs origin/dev — nothing to test · "
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
                           changed_note="changed → full suite: uv.lock is a full trigger",
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
