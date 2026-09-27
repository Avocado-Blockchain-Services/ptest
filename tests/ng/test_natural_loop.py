"""Cheap-loop twins: bare ptest is --changed, --full skips verified work,
duplicate full runs coalesce.

Target behavior (strict TDD: every test fails before the fix):
1. Bare `ptest` behaves exactly like `ptest --changed` standalone and at a
   monorepo root; scoped paths and --full are unchanged.
2. `ptest --full` skips without admission when the latest full run passed
   and recorded a baseline on exactly the same inputs; `--again` reruns;
   dirty trees never skip; monorepo root applies this per child.
3. A second `ptest --full` with the same input fingerprint joins an
   admitted/queued full run instead of running again; different inputs
   queue normally; unreadable results run normally (fail-closed).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptest import config as config_api
from ptest import contracts as C
from ptest import history as history_api
from ptest import operations
from ptest import progress
from ptest.cli import main, parse_argv
from support import git, init_git_repo, write_ptest_toml

_DIGEST = "11" * 32
_OTHER_DIGEST = "22" * 32
_COMPAT = "test-compat-v1"
_RUN_ID = "ef" * 16


def _repo(domain) -> Path:
    # History requires a fixture checkout inside its domain; execution is
    # exempt from the state-outside-checkout rule for fixture domains.
    root = domain.root / "repo"
    root.mkdir(parents=True, exist_ok=True)
    write_ptest_toml(root, kind="command", launcher=("true",), args=(),
                     full_args=(), project_id="ab" * 16)
    init_git_repo(root, files={"input.txt": "v1\n"}, message="base")
    return root


def _config(root: Path):
    config = config_api.resolve_config(root).config
    assert config is not None
    return config


def _snapshot(head: str, *, digest: str = _DIGEST, clean: bool = True) -> C.InputSnapshot:
    return C.InputSnapshot(digest=digest, compatibility=_COMPAT, head=head,
                           clean=clean)


def _publish_full(domain, checkout, snapshot: C.InputSnapshot, policy: str,
                  *, run_id: str = _RUN_ID, sequence: int = 1,
                  status: str = "passed", exit_code: int = 0):
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL, ("true",),
                                  workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=run_id, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=C.Mode.FULL,
        status=C.Status(status), phase="complete",
        started_at="2026-09-25T00:00:00+00:00",
        finished_at="2026-09-25T00:00:01+00:00",
        plan=plan, command=command, exit_code=exit_code,
        exit_origin="runner", runner_exit_code=exit_code,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot, policy_digest=policy,
    )
    inventory = C.Inventory(
        adapter="command", version="1", complete=True,
        tests=(C.TestRecord(id="tests/test_a.py::test_x",
                            file="tests/test_a.py",
                            outcome=C.Outcome("passed"),
                            setup_s=None, call_s=0.01, teardown_s=None),),
        digest="44" * 32,
    )
    published = history_api.publish_outcome(domain, checkout, result, inventory)
    assert published.baseline_published is True
    return result


def _no_admission(monkeypatch):
    calls = []

    def _enqueue(domain, request):
        calls.append(request)
        raise AssertionError("full gate must not admit")

    monkeypatch.setattr("ptest.scheduler.enqueue", _enqueue)
    return calls


# --- 1. bare ptest is --changed -----------------------------------------------
#
# Bare `ptest` and `ptest --changed` route through the import graph
# (``ptest.impact``, stubbed here through the ``cli._impact_api`` seam);
# the mapping itself is pinned in test_changed_default.py, these twins pin
# loop parity and skip lines.

def _stub_impact(monkeypatch, plans, *, sha="abc123", label="origin/dev",
                 repo_changed=()):
    """Stub ``ptest.impact``; plans maps project dir name -> Impact."""
    import sys
    import types
    from dataclasses import dataclass

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

    mod.Base = Base
    mod.Impact = Impact
    mod.git_top = lambda start: Path(start)
    mod.resolve_base = lambda top, explicit: Base(sha, label)
    mod.changed_files = lambda top, resolved: repo_changed
    mod.plan = lambda top, project_root, config, changed: plans(mod)[
        Path(project_root).name]
    monkeypatch.setitem(sys.modules, "ptest.impact", mod)
    # Stub through the `cli._impact_api` seam: `from . import impact` reads
    # the attribute on the `ptest` package before `sys.modules`, so the
    # sys.modules entry alone is ignored once the real module is imported.
    monkeypatch.setattr("ptest.cli._impact_api", lambda: mod)
    return mod


def test_bare_standalone_matches_changed_request(tmp_path, monkeypatch):
    write_ptest_toml(tmp_path, kind="command", launcher=("true",), args=(),
                     full_args=(), project_id="ab" * 16)
    monkeypatch.chdir(tmp_path)
    _stub_impact(
        monkeypatch,
        lambda mod: {tmp_path.name: mod.Impact(
            kind="selected", changed=("a.py",), files=("tests/test_a.py",),
            direct=0, via=1, total=5)},
        repo_changed=("a.py",))
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append(request)
        or type("R", (), {"reasons": (), "exit_code": 0})(),
    )

    assert main(()) == 0
    assert main(("--changed",)) == 0
    assert [request.mode for request in calls] == [C.Mode.SCOPED] * 2
    assert calls[0].argv == calls[1].argv == ("tests/test_a.py",)
    assert calls[0].changed_note == calls[1].changed_note
    assert all(request.next_hint for request in calls)


def test_monorepo_root_bare_runs_changed_loop(tmp_path, monkeypatch, capsys, monorepo):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    monkeypatch.chdir(tmp_path)
    _stub_impact(monkeypatch,
                 lambda mod: {"api": mod.Impact(kind="none"),
                              "web": mod.Impact(kind="none")})
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or type("R", (), {"reasons": (), "exit_code": 0,
                          "status": C.Status.PASSED, "counts": None})(),
    )

    assert main(()) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert ("ptest: no changes vs origin/dev (no green run yet) — nothing to test · "
            "ptest --full runs everything") in err
    assert "ptest: total" not in err


def test_monorepo_root_bare_runs_only_the_touched_child(
        tmp_path, monkeypatch, capsys, monorepo):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    init_git_repo(tmp_path, message="base")
    (tmp_path / "api" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _stub_impact(
        monkeypatch,
        lambda mod: {"api": mod.Impact(
            kind="selected", changed=("api/extra.py",),
            files=("tests/test_a.py",), direct=1, via=0, total=4),
            "web": mod.Impact(kind="none")},
        repo_changed=("api/extra.py",))
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append((config, request))
        or type("R", (), {"reasons": (), "exit_code": 0,
                          "status": C.Status.PASSED, "counts": None})(),
    )

    assert main(()) == 0
    assert [call[1].mode for call in calls] == [C.Mode.SCOPED]
    assert calls[0][0].config_path.parent.name == "api"
    err = capsys.readouterr().err
    assert "ptest: web · no changes" in err
    assert "ptest: total" in err


# --- 2. --full skips verified work --------------------------------------------

def _verified_setup(case, tmp_path, monkeypatch):
    domain = case.domain()
    root = _repo(domain)
    config = _config(root)
    checkout = operations._checkout(config)
    head = git(root, "rev-parse", "HEAD")
    snapshot = _snapshot(head)
    _publish_full(domain, checkout, snapshot,
                  operations._policy_digest(config))
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    return domain, config, checkout, head


def test_full_skips_without_admission_when_inputs_match(case, tmp_path,
                                                        monkeypatch, capsys):
    domain, config, _, head = _verified_setup(case, tmp_path, monkeypatch)
    calls = _no_admission(monkeypatch)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))

    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    err = capsys.readouterr().err
    assert f"ptest: already verified at {head[:7]}" in err
    assert "ago) — ptest --full --again to rerun" in err


def test_full_never_skips_on_a_dirty_tree(case, tmp_path, monkeypatch, capsys):
    domain, config, _, head = _verified_setup(case, tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: _snapshot(head, clean=False))
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert len(calls) == 1
    assert "already verified" not in capsys.readouterr().err


def test_full_again_forces_a_run(case, tmp_path, monkeypatch, capsys):
    domain, config, _, _ = _verified_setup(case, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(
            domain, config, C.RunRequest(mode=C.Mode.FULL, again=True))
    assert len(calls) == 1
    assert "already verified" not in capsys.readouterr().err


def test_full_runs_when_inputs_differ(case, tmp_path, monkeypatch, capsys):
    domain, config, _, head = _verified_setup(case, tmp_path, monkeypatch)
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: _snapshot(head, digest=_OTHER_DIGEST))
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert len(calls) == 1
    assert "already verified" not in capsys.readouterr().err


def test_full_skip_is_per_child_checkout(case, tmp_path, monkeypatch, capsys):
    domain = case.domain()
    roots = {}
    for name in ("api", "web"):
        root = domain.root / name
        root.mkdir(parents=True, exist_ok=True)
        write_ptest_toml(root, kind="command", launcher=("true",), args=(),
                         full_args=(), project_id="ab" * 16)
        init_git_repo(root, files={"input.txt": "v1\n"}, message="base")
        roots[name] = root
    configs = {name: _config(root) for name, root in roots.items()}
    checkouts = {name: operations._checkout(config)
                 for name, config in configs.items()}
    assert checkouts["api"].checkout_id != checkouts["web"].checkout_id
    head = git(roots["api"], "rev-parse", "HEAD")
    snapshot = _snapshot(head)
    _publish_full(domain, checkouts["api"], snapshot,
                  operations._policy_digest(configs["api"]))
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    api_calls = _no_admission(monkeypatch)

    api_result = operations.execute(
        domain, configs["api"], C.RunRequest(mode=C.Mode.FULL))

    assert api_calls == []
    assert (api_result.status, api_result.exit_code) == (C.Status.PASSED, 0)
    assert "already verified" in capsys.readouterr().err


def test_again_reaches_every_monorepo_child(tmp_path, monkeypatch, monorepo):
    monorepo({"api": {"kind": "command", "launcher": ("true",)},
              "web": {"kind": "command", "launcher": ("true",)}},
             parent=tmp_path, name=None)
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda domain, config, request: calls.append(request)
        or type("R", (), {"reasons": (), "exit_code": 0,
                          "status": C.Status.PASSED, "counts": None})(),
    )

    assert main(("--full", "--again")) == 0
    assert len(calls) == 2
    assert all(request.mode is C.Mode.FULL and request.again for request in calls)


def test_again_requires_full():
    assert parse_argv(("--full", "--again")).again is True
    assert parse_argv(("--full",)).again is False
    with pytest.raises(C.Problem):
        parse_argv(("--again",))
    with pytest.raises(C.Problem):
        parse_argv(("--changed", "--again"))


def test_already_verified_line_format():
    line = progress.format_already_verified("a1b2c3d", 90.0)
    assert line == ("ptest: already verified at a1b2c3d "
                    "(1m30s ago) — ptest --full --again to rerun")


# --- 3. duplicate full runs coalesce ------------------------------------------

def _lease(domain, checkout, digest: str, run_id: str, pid: int):
    payload = {"version": 1, "digest": digest, "run_id": run_id, "pid": pid,
               "started_at": operations._iso_now(), "sequence": 1}
    assert history_api.claim_full_lease(domain, checkout, payload) is True
    return payload


def _publish_summary_only(domain, checkout, snapshot, policy, *, run_id,
                          status="passed", exit_code=0):
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL, ("true",),
                                  workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=run_id, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=C.Mode.FULL,
        status=C.Status(status), phase="complete",
        started_at="2026-09-25T00:00:00+00:00",
        finished_at="2026-09-25T00:00:05+00:00",
        plan=plan, command=command, exit_code=exit_code,
        exit_origin="runner", runner_exit_code=exit_code,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot, policy_digest=policy,
    )
    published = history_api.publish_outcome(domain, checkout, result, None)
    assert published.committed is True
    return result


def _full_setup(case, tmp_path):
    domain = case.domain()
    root = _repo(domain)
    config = _config(root)
    checkout = operations._checkout(config)
    head = git(root, "rev-parse", "HEAD")
    return domain, config, checkout, _snapshot(head)


def test_full_joins_running_lease_with_same_digest(case, tmp_path, monkeypatch,
                                                   capsys):
    domain, config, checkout, snapshot = _full_setup(case, tmp_path)
    policy = operations._policy_digest(config)
    _publish_summary_only(domain, checkout, snapshot, policy, run_id=_RUN_ID)
    _lease(domain, checkout, _DIGEST, _RUN_ID, os.getpid())
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    calls = _no_admission(monkeypatch)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))

    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    err = capsys.readouterr().err
    assert f"ptest: joined the running full run (pid {os.getpid()})" in err
    assert "ptest: passed" in err


def test_full_join_reports_the_other_runs_failure(case, tmp_path, monkeypatch,
                                                  capsys):
    domain, config, checkout, snapshot = _full_setup(case, tmp_path)
    policy = operations._policy_digest(config)
    _publish_summary_only(domain, checkout, snapshot, policy, run_id=_RUN_ID,
                          status="failed", exit_code=3)
    _lease(domain, checkout, _DIGEST, _RUN_ID, os.getpid())
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    calls = _no_admission(monkeypatch)

    result = operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))

    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.FAILED, 3)
    assert "ptest: failed" in capsys.readouterr().err


def test_full_stale_lease_runs_normally(case, tmp_path, monkeypatch, capsys):
    domain, config, checkout, snapshot = _full_setup(case, tmp_path)
    dead = 1 << 22
    try:
        while True:
            os.kill(dead, 0)
            dead += 1
    except OSError:
        pass
    _lease(domain, checkout, _DIGEST, _RUN_ID, dead)
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert len(calls) == 1
    assert "joined the running full run" not in capsys.readouterr().err


def test_full_different_digest_ignores_live_lease(case, tmp_path, monkeypatch,
                                                  capsys):
    domain, config, checkout, snapshot = _full_setup(case, tmp_path)
    _lease(domain, checkout, _OTHER_DIGEST, _RUN_ID, os.getpid())
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert len(calls) == 1
    assert "joined the running full run" not in capsys.readouterr().err


def test_full_unreadable_result_runs_normally(case, tmp_path, monkeypatch,
                                              capsys):
    domain, config, checkout, snapshot = _full_setup(case, tmp_path)
    _lease(domain, checkout, _DIGEST, _RUN_ID, os.getpid())
    monkeypatch.setattr(operations, "_capture_source",
                        lambda *args, **kwargs: snapshot)
    calls = []
    monkeypatch.setattr("ptest.scheduler.enqueue",
                        lambda domain, request: calls.append(request) or (_ for _ in ()).throw(
                            C.Problem(code="queue-timeout",
                                      message="admission reached",
                                      phase="scheduler", retryable=True)))

    with pytest.raises(C.Problem, match="admission reached"):
        operations.execute(domain, config,
                           C.RunRequest(mode=C.Mode.FULL, queue_timeout_s=1))
    assert len(calls) == 1
    assert "joined the running full run" in capsys.readouterr().err
