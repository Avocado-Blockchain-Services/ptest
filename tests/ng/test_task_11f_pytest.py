"""Task 11F full-mode admission contracts."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace

import pytest

from ptest import config as config_api
from ptest import contracts as C, operations, platform, scheduler
from ptest.adapters.pytest import prepare


def test_pytest_full_rejects_base_before_queue(case, monkeypatch):
    domain = case.domain()
    config = case.config(runner_kind="pytest")
    monkeypatch.setattr(operations.scheduler, "enqueue", lambda *a, **k: pytest.fail("queued"))
    with pytest.raises(C.Problem, match="invalid-config"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL, base="HEAD"))


@pytest.mark.parametrize("prefix", [("--full", "--base", "HEAD"),
                                     ("--base", "HEAD", "--full")])
def test_pytest_full_cli_rejects_base_before_admission(case, prefix):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    completed = case.invoke(domain, root, *prefix, timeout=5)
    assert completed.code == 2
    assert b"invalid-config" in completed.stderr
    assert completed.result is None
    assert not domain.ledger.exists()


def test_pytest_full_cli_parsing_rejects_base_in_both_orders():
    from ptest.cli import parse_argv

    with pytest.raises(C.Problem, match="invalid-config"):
        parse_argv(("--full", "--base", "HEAD"))
    with pytest.raises(C.Problem, match="invalid-config"):
        parse_argv(("--base", "HEAD", "--full"))


@pytest.mark.parametrize("suffix", [
    ("--full", "--", "-k", "expr"),
    ("--full", "--", "-m", "mark"),
    ("--full", "tests/a.py"),
    ("--full", "--", "tests/a.py::test_x"),
])
def test_pytest_full_cli_rejects_runner_suffix_before_enqueue(case, suffix):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    completed = case.invoke(domain, root, *suffix, timeout=5)
    assert completed.code == 2
    assert b"invalid-config" in completed.stderr
    assert completed.result is None
    assert not domain.ledger.exists()


def test_pytest_full_empty_delimiter_carries_no_narrowing_token():
    from ptest.cli import parse_argv

    parsed = parse_argv(("--full", "--"))
    assert parsed.mode is C.Mode.FULL
    assert parsed.runner_argv == ()
    assert parsed.base is None


def test_trailing_full_flag_after_scope_stays_literal_scope():
    from ptest.cli import parse_argv

    parsed = parse_argv(("-k", "--full"))
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.runner_argv == ("-k", "--full")


def test_pytest_full_is_single_slot_and_has_no_scope(case):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    config = replace(case.config(runner_kind="pytest"), config_path=root / ".ptest.toml")
    grant = C.Grant(run_id="12" * 16, nonce="34" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="56" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="pt_test", worker_count=1)
    plan = C.Plan(mode=C.Mode.FULL, execution="full")
    prepared = prepare(config, plan, grant, attempt)
    assert prepared.summary.workers == 1
    assert "-n" not in prepared.argv
    assert prepared.argv[-1] == "tests"


def test_pytest_full_dot_root_refuses_before_queue(case, monkeypatch):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    config = replace(case.config(runner_kind="pytest"),
                     checkout=case.checkout(domain),
                     config_path=root / ".ptest.toml")
    object.__setattr__(config.checkout, "root", root)
    config = replace(config, runner=replace(config.runner, test_roots=(".",)))
    monkeypatch.setattr(operations.scheduler, "enqueue", lambda *a, **k: pytest.fail("queued"))
    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))


def test_pytest_full_refuses_scoped_files_in_direct_plan(case):
    config = case.config(runner_kind="pytest")
    grant = C.Grant(run_id="12" * 16, nonce="34" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None, generation=1,
                    domain_id="56" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001", resource_prefix="pt_test", worker_count=1)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(config, C.Plan(mode=C.Mode.FULL, execution="full", files=("tests/a.py",)), grant, attempt)


@pytest.mark.parametrize(
    "mode,execution",
    [(C.Mode.SCOPED, "full"), (C.Mode.FULL, "scoped"),
     (C.Mode.AUTOMATIC, "full"), (C.Mode.FULL, "selected")],
)
def test_pytest_full_adapter_rejects_mismatched_mode_execution(mode, execution, case):
    config = case.config(runner_kind="pytest")
    grant = C.Grant(run_id="12" * 16, nonce="34" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None, generation=1,
                    domain_id="56" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="pt_test", worker_count=1)
    with pytest.raises(C.Problem, match="(native-config-invalid|unsupported-capability)"):
        prepare(config, C.Plan(mode=mode, execution=execution), grant, attempt)


@pytest.mark.parametrize("roots", [("tests", "tests"), ("tests::test_a",), ("../tests",)])
def test_pytest_full_adapter_rejects_ambiguous_or_narrowing_roots(case, roots):
    config = case.config(runner_kind="pytest")
    config = replace(config, runner=replace(config.runner, test_roots=roots))
    grant = C.Grant(run_id="12" * 16, nonce="34" * 32, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None, generation=1,
                    domain_id="56" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="pt_test", worker_count=1)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(config, C.Plan(mode=C.Mode.FULL, execution="full"), grant, attempt)


def test_pytest_full_queued_full_args_mutation_cancels_without_launch(case, monkeypatch):
    """A queued full run must not launch the command resolved before admission."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = " + json.dumps(project_id) + "\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\n"
        "workers = 1\nlifecycle = \"cooperative-process-group\"\n"
    )
    config = config_api.resolve_config(root).config
    assert config is not None
    owner = platform.process_identity(os.getpid())
    assert owner is not None
    blocker = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="ab" * 16, checkout=operations._checkout(config), owner=owner,
        slots=1, exclusive=True, deadline=time.monotonic() + 30, fixture=True,
    ))
    assert scheduler.poll(domain, blocker).state is C.LeaseState.GRANTED
    original_poll = scheduler.poll
    changed = False

    def observe_poll(current_domain, ticket):
        nonlocal changed
        state = original_poll(current_domain, ticket)
        if (not changed and ticket.run_id != blocker.run_id
                and state.state is C.LeaseState.QUEUED):
            (root / ".ptest.toml").write_text(
                (root / ".ptest.toml").read_text().replace(
                    "full_args = []", "full_args = [\"--maxfail=1\"]"))
            changed = True
            assert scheduler.cancel_pending(domain, blocker, owner)
        return state

    monkeypatch.setattr(scheduler, "poll", observe_poll)
    monkeypatch.setattr(operations, "_run_guard", lambda *args: pytest.fail("stale full command launched"))
    with pytest.raises(C.Problem, match="changed-during-run"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert changed
    states = scheduler.reconcile(domain)
    assert all(item.state not in {C.LeaseState.GRANTED, C.LeaseState.RUNNING,
                                  C.LeaseState.DRAINING, C.LeaseState.FINALIZING}
               for item in states)
