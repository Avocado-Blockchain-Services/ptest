"""Preparation for the finite, local Vitest bridge profile.

This module deliberately prepares literal argv and generated environment values
only.  Resolving or executing a project's Node/Vitest installation belongs to
the admitted bridge process, never to configuration preview or preparation.
"""
from __future__ import annotations

from pathlib import Path

from ptest import contracts as C


def _bridge_path() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / "vitest_bridge.mjs"


def _validate_inputs(config: C.Config, plan: C.Plan, grant: C.Grant,
                     attempt: C.AttemptIdentity) -> None:
    if config.runner.kind is not C.RunnerKind.VITEST:
        raise ValueError("Vitest adapter requires runner.kind=vitest")
    if plan.execution not in ("full", "selected", "scoped"):
        raise ValueError("Vitest adapter cannot prepare an empty execution plan")
    if config.runner.workers != grant.slots or attempt.worker_count != grant.slots:
        raise ValueError("Vitest worker count must equal the admitted grant")
    if grant.run_id != attempt.run_id:
        raise ValueError("Vitest attempt must belong to the admitted grant run")


def _native_args(config: C.Config, plan: C.Plan) -> tuple[str, ...]:
    """Keep repository arguments literal; only ptest-owned paths are appended."""
    args = tuple(config.runner.args)
    if plan.execution == "full":
        return args + tuple(config.runner.full_args)
    return args + tuple(plan.files)


def prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
            attempt: C.AttemptIdentity) -> C.PreparedRun:
    """Create the bridge invocation for one already-admitted Vitest attempt."""
    _validate_inputs(config, plan, grant, attempt)
    workers = str(grant.slots)
    native_args = _native_args(config, plan)
    cwd = (config.checkout.root if config.checkout is not None
           else (config.config_path.parent if config.config_path is not None else Path.cwd()))
    argv = tuple(config.runner.launcher) + (str(_bridge_path()), "--") + native_args
    return C.PreparedRun(
        argv=argv,
        cwd=cwd,
        env_updates=(
            ("PTEST_VITEST_WORKERS", workers),
            ("PTEST_VITEST_ATTEMPT", attempt.attempt_id),
            ("PTEST_VITEST_EXECUTION", plan.execution),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_GRANT_NONCE", grant.nonce),
            ("VITEST_MAX_FORKS", workers),
            ("VITEST_MIN_FORKS", workers),
            ("VITEST_MAX_THREADS", workers),
            ("VITEST_MIN_THREADS", workers),
        ),
        capability=C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE,
            selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message="Native profile acceptance and private report binding require the executor; "
                        "selection and baseline evidence are unavailable.",
            ),),
        ),
        summary=C.summarize_command(
            C.RunnerKind.VITEST,
            plan.mode,
            argv,
            generated_options=(
                f"vitest.workers={workers}",
                f"vitest.maxConcurrency={workers}",
            ),
            workers=grant.slots,
            provenance=("native-vitest-bridge",),
        ),
    )
