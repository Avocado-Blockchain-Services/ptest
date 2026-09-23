"""Vitest exclusive-command preparation contracts.

``kind = "vitest"`` executes as one literal exclusive command through the
project-local Vitest CLI. Scope arrives through the effective runner args
(the caller scope is already appended there); ``plan.files`` must stay empty.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.adapters import vitest as vitest_adapter
from ptest.runners import adapter_for

RUN_ID = "12" * 16
NONCE = "34" * 32


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(run_id=RUN_ID, nonce=NONCE, slots=slots, memory_estimate_mb=None,
                   reserved_memory_mb=None, generation=0, domain_id="56" * 16)


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(run_id=RUN_ID, attempt_id="a001",
                             resource_prefix="pt_checkout_run_a001_w000", worker_count=workers)


def _config(case, **overrides):
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    runner = replace(config.runner, launcher=("node",))
    if overrides:
        runner = replace(runner, **overrides)
    return replace(config, runner=runner)


def test_scoped_builds_literal_exclusive_argv_from_effective_args(case):
    config = _config(case, args=("src/a.test.ts",))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run", "src/a.test.ts")
    assert prepared.cwd == case.base
    assert prepared.env_updates == ()
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert prepared.capability.selection is False
    assert [reason.code for reason in prepared.capability.limitations] == ["unsupported-capability"]
    assert prepared.capability.limitations[0].message == vitest_adapter.VITEST_EXCLUSIVE_NOTE


def test_full_appends_full_args_after_runner_args(case):
    config = _config(case, args=("--reporter", "verbose"), full_args=("--coverage",))
    plan = C.Plan(mode=C.Mode.FULL, execution="full", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run",
                             "--reporter", "verbose", "--coverage")


def test_absolute_node_launcher_is_accepted(case):
    config = _config(case)
    config = replace(config, runner=replace(config.runner, launcher=("/usr/bin/node",)))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv[:3] == ("/usr/bin/node", vitest_adapter.VITEST_ENTRY, "run")


def test_selected_execution_is_rejected(case):
    config = _config(case)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected", files=("tests/a.test.ts",))

    with pytest.raises(C.Problem, match="unsupported-capability"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_nonempty_plan_files_are_rejected(case):
    config = _config(case)
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("tests/a.test.ts",))

    with pytest.raises(C.Problem, match="plan.files"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


@pytest.mark.parametrize("launcher", [("npm",), ("npx", "vitest"), ("node", "--inspect"),
                                      ("/tmp/not-node",), ("node", "node")])
def test_non_node_launcher_is_rejected(case, launcher):
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=launcher))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    with pytest.raises(C.Problem, match="Node"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_non_vitest_config_is_rejected(case):
    config = case.config(runner_kind="pytest", config_path=case.base / "ptest.toml")
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    with pytest.raises(C.Problem, match="vitest"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_registry_marks_vitest_exclusive_and_automatic_full(case):
    adapter = adapter_for(C.RunnerKind.VITEST)

    assert adapter.requires_exclusive(_config(case)) is True
    assert adapter.mode_for_automatic() == "full"


def test_registry_prepare_advanced_raises_unsupported_capability(case):
    adapter = adapter_for(C.RunnerKind.VITEST)
    config = _config(case)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected", files=("tests/a.test.ts",))
    grant = C.Grant(run_id=RUN_ID, nonce=NONCE, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)

    with pytest.raises(C.Problem, match="unsupported-capability"):
        adapter.prepare_advanced(config, plan, grant, attempt)


def test_vitest_entry_literal_is_stable():
    assert vitest_adapter.VITEST_ENTRY == "node_modules/vitest/vitest.mjs"
    assert Path(vitest_adapter.VITEST_ENTRY).name == "vitest.mjs"
