"""0.3.3 command model twins: folder/file/node routing, nearest config,
SIGINT silence, real project names, stale-guidance warning.

Mode is CHANGED by default; ``--full`` switches to ALL; a path only
narrows WHERE. ``ptest.impact`` is stubbed through the ``cli._impact_api``
seam (same pattern as test_changed_default) so these tests pin ROUTING.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

from ptest import contracts as C
from ptest.cli import main
from support import write_ptest_toml


def _install_impact(monkeypatch, *, plans, repo_changed=(),
                    sha="abc123", label="origin/dev"):
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

    base = Base(sha, label)
    mod.Base = Base
    mod.Impact = Impact
    mod.git_top = lambda start: Path(start)
    mod.resolve_base = lambda top, explicit: base
    mod.changed_files = lambda top, resolved: repo_changed

    def plan(top, project_root, config, changed):
        return plans[Path(project_root).name]

    mod.plan = plan
    monkeypatch.setitem(sys.modules, "ptest.impact", mod)
    monkeypatch.setattr("ptest.cli._impact_api", lambda: mod)
    return mod


def _capture(monkeypatch):
    calls = []

    def fake_execute(domain, config, request):
        calls.append(request)
        return types.SimpleNamespace(reasons=(), exit_code=0,
                                     status=C.Status.PASSED, counts=None)

    monkeypatch.setattr("ptest.operations.execute", fake_execute)
    return calls


def _mono(tmp_path, monkeypatch, monorepo, children=None):
    monorepo(
        children if children is not None else {
            "api": {"kind": "pytest", "launcher": ("python",),
                    "test_roots": ["tests"], "project_id": "ab" * 16},
            "web": {"kind": "command", "launcher": ("true",), "args": (),
                    "full_args": (), "project_id": "cd" * 16}},
        parent=tmp_path, name=None)
    (tmp_path / "api" / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "api" / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def _selected(mod, changed=("pkg/a.py",), files=("tests/unit/test_a.py",
              "tests/test_b.py"), total=10):
    return mod.Impact(kind="selected", changed=tuple(changed),
                      files=tuple(files), direct=1, via=1, total=total)


def _none(mod, changed=()):
    return mod.Impact(kind="none", changed=tuple(changed))


# --- rule 1: folder narrows the changed set -----------------------------------

def test_folder_runs_changed_tests_only_under_it(tmp_path, monkeypatch,
                                                 capsys, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch,
                    plans={"api": _selected(mod), "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(("api/tests/unit",)) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/unit/test_a.py",)
    assert request.changed_note is not None
    assert "api/tests/unit" in request.changed_note
    capsys.readouterr()


def test_folder_with_nothing_affected_prints_no_changes_under(
        tmp_path, monkeypatch, capsys, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch, repo_changed=("web/src/a.ts",),
                    plans={"api": _none(mod, changed=("docs/x.md",)),
                           "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(("api/tests",)) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: api · no changes under api/tests — nothing to test · "
            "ptest --full api/tests runs all of them") in err


def test_file_always_runs_even_when_nothing_changed(
        tmp_path, monkeypatch, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch,
                    plans={"api": _none(mod), "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(("api/tests/test_x.py",)) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.SCOPED
    assert calls[0].argv == ("tests/test_x.py",)


def test_node_id_always_runs_even_when_nothing_changed(
        tmp_path, monkeypatch, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch,
                    plans={"api": _none(mod), "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(("api/tests/test_x.py::test_q",)) == 0

    assert len(calls) == 1
    assert calls[0].argv == ("tests/test_x.py::test_q",)


def test_folder_and_file_combine(tmp_path, monkeypatch, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch,
                    plans={"api": _selected(mod), "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(("api/tests/unit", "api/tests/test_b.py")) == 0

    assert len(calls) == 1
    assert sorted(calls[0].argv) == ["tests/test_b.py",
                                    "tests/unit/test_a.py"]


def test_full_folder_runs_all_tests_there_without_gate(
        tmp_path, monkeypatch, capsys, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    _install_impact(monkeypatch, plans={})
    calls = _capture(monkeypatch)

    assert main(("--full", "api/tests/unit")) == 0

    assert len(calls) == 1
    request = calls[0]
    assert request.mode is C.Mode.SCOPED
    assert request.argv == ("tests/unit",)
    assert request.again is False
    assert request.changed_note == "all tests under api/tests/unit"
    capsys.readouterr()


def test_full_file_is_rejected_with_next_step(tmp_path, monkeypatch, capsys,
                                              monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    _install_impact(monkeypatch, plans={})
    calls = _capture(monkeypatch)

    assert main(("--full", "api/tests/test_x.py")) == 2

    assert calls == []
    err = capsys.readouterr().err
    assert "ptest api/tests/test_x.py" in err
    assert "--full" in err


# --- rule 1: standalone mirrors the model -------------------------------------

def _standalone(tmp_path, monkeypatch):
    write_ptest_toml(tmp_path, kind="command", launcher=("true",),
                     args=(), full_args=(), project_id="ab" * 16)
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_standalone_folder_narrows_changed(tmp_path, monkeypatch):
    _standalone(tmp_path, monkeypatch)
    mod = _install_impact(monkeypatch, plans={})
    name = tmp_path.name
    _install_impact(monkeypatch, plans={
        name: mod.Impact(kind="selected", changed=("pkg/a.py",),
                         files=("tests/unit/test_a.py", "tests/test_b.py"),
                         direct=1, via=1, total=10)})
    calls = _capture(monkeypatch)

    assert main(("tests/unit",)) == 0

    assert len(calls) == 1
    assert calls[0].argv == ("tests/unit/test_a.py",)


def test_standalone_full_folder_runs_all_there(tmp_path, monkeypatch, capsys):
    _standalone(tmp_path, monkeypatch)
    _install_impact(monkeypatch, plans={})
    calls = _capture(monkeypatch)

    assert main(("--full", "tests/unit")) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.SCOPED
    assert calls[0].argv == ("tests/unit",)
    assert calls[0].changed_note == "all tests under tests/unit"
    capsys.readouterr()


# --- rule 2: nearest-config routing --------------------------------------------

def test_path_routes_to_nearest_config_without_root_manifest(
        tmp_path, monkeypatch):
    (tmp_path / "exp" / "tests").mkdir(parents=True)
    write_ptest_toml(tmp_path / "exp", kind="command", launcher=("true",),
                     args=(), full_args=(), project_id="ab" * 16)
    (tmp_path / "exp" / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = _capture(monkeypatch)

    assert main(("exp/tests/test_x.py",)) == 0

    assert len(calls) == 1
    assert calls[0].mode is C.Mode.SCOPED
    assert calls[0].argv == ("tests/test_x.py",)


def test_path_without_config_reports_no_project(tmp_path, monkeypatch,
                                                capsys):
    monkeypatch.chdir(tmp_path)

    assert main(("docs/tests",)) == 2

    err = capsys.readouterr().err
    assert ("ptest: no ptest project for docs/tests — run ptest init "
            "there") in err


# --- rule 3: SIGINT prints only the cancelled line -----------------------------

def _cancelled_result():
    return types.SimpleNamespace(
        reasons=(C.Reason(code="protocol-mismatch",
                          message="guard handoff was incomplete"),
                 C.Reason(code="state-unavailable",
                          message="guard execution did not complete "
                                  "normally")),
        exit_code=130, status=C.Status.CANCELLED, counts=None, signal=2)


def test_sigint_run_prints_only_cancelled_line(tmp_path, monkeypatch, capsys,
                                               monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    _install_impact(monkeypatch, plans={})
    monkeypatch.setattr("ptest.operations.execute",
                        lambda domain, config, request: _cancelled_result())

    assert main(("api/tests/test_x.py",)) == 130

    err = capsys.readouterr().err
    assert "protocol-mismatch" not in err
    assert "guard handoff was incomplete" not in err
    assert "state-unavailable" not in err
    assert "guard execution did not complete normally" not in err


def test_genuine_failure_keeps_handoff_lines(tmp_path, monkeypatch, capsys,
                                             monorepo):
    from ptest.cli import _emit_reasons

    _mono(tmp_path, monkeypatch, monorepo)
    failed = types.SimpleNamespace(
        reasons=(C.Reason(code="protocol-mismatch",
                          message="guard handoff was incomplete"),),
        exit_code=70, status=C.Status.INCOMPLETE, counts=None, signal=None)
    _emit_reasons(failed)

    err = capsys.readouterr().err
    assert "protocol-mismatch: guard handoff was incomplete" in err


# --- rule 4: real project names -------------------------------------------------

def test_errors_name_the_first_declared_child(tmp_path, monkeypatch, capsys,
                                              monorepo):
    _mono(tmp_path, monkeypatch, monorepo,
          children={
              "web": {"kind": "command", "launcher": ("true",), "args": (),
                      "full_args": (), "project_id": "ab" * 16},
              "srv": {"kind": "command", "launcher": ("true",), "args": (),
                      "full_args": (), "project_id": "cd" * 16}})
    _install_impact(monkeypatch, plans={})
    calls = _capture(monkeypatch)

    assert main(("oops/tests",)) == 2

    assert calls == []
    err = capsys.readouterr().err
    assert '"web/...\"' in err or '"web/"' in err or "web/..." in err
    assert "api/" not in err


# --- amendment (c): stale managed guidance warns once per run ------------------

def test_run_warns_once_on_older_managed_guide(tmp_path, monkeypatch, capsys,
                                               monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch, plans={"api": _none(mod), "web": _none(mod)})
    import hashlib

    import ptest.agent_rules as rules_module

    old = b"# stale managed guide\n"
    monkeypatch.setattr(rules_module, "_PREVIOUS_GUIDE_SHA256S",
                        frozenset({hashlib.sha256(old).hexdigest()}))
    guide_dir = tmp_path / "docs"
    guide_dir.mkdir()
    (guide_dir / "ptest-agent.md").write_bytes(old)
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert err.count("ptest: agent guidance is outdated — run ptest init "
                     "to update") == 1


def test_run_stays_silent_on_current_or_edited_guidance(
        tmp_path, monkeypatch, capsys, monorepo):
    _mono(tmp_path, monkeypatch, monorepo)
    mod = _install_impact(monkeypatch, plans={})
    _install_impact(monkeypatch, plans={"api": _none(mod), "web": _none(mod)})
    calls = _capture(monkeypatch)

    assert main(()) == 0

    assert calls == []
    assert "agent guidance is outdated" not in capsys.readouterr().err
