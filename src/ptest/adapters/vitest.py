"""Vitest execution as one literal exclusive command.

``kind = "vitest"`` stays the config kind so existing ``web/.ptest.toml``
files keep working. The adapter builds a literal exclusive command through
the project-local Vitest CLI; scope arrives through the effective runner
args (the caller scope is already appended there by the orchestrator), so
``plan.files`` must stay empty. Vitest keeps its own worker pool: ptest
claims no worker ownership and no per-test results; the vitest exit code is
the outcome.
"""
from __future__ import annotations

from pathlib import Path

from ptest import contracts as C

VITEST_ENTRY = "node_modules/vitest/vitest.mjs"
VITEST_EXCLUSIVE_NOTE = ("Vitest runs as one exclusive command (node node_modules/vitest/vitest.mjs run); "
                         "ptest does not own Vitest workers, selection or per-test results")


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _project_root(config: C.Config) -> Path:
    if config.checkout is not None:
        return config.checkout.root
    if config.config_path is not None:
        return config.config_path.parent
    raise _problem("native-config-invalid", "Vitest configuration has no project root")


def _require_node_launcher(launcher: tuple[str, ...]) -> None:
    """Accept a direct ``node`` PATH lookup or exactly one absolute node path."""
    if len(launcher) != 1:
        raise _problem("native-config-invalid", "Vitest execution requires one direct Node launcher")
    executable = launcher[0]
    candidate = Path(executable)
    if executable != "node" and not (candidate.is_absolute() and candidate.name in {"node", "node.exe"}):
        raise _problem("native-config-invalid", "Vitest execution requires a direct Node launcher")


def prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
            attempt: C.AttemptIdentity) -> C.PreparedRun:
    """Prepare the literal exclusive Vitest command for one admitted run."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.VITEST:
        raise _problem("native-config-invalid", "Vitest adapter requires runner.kind=vitest")
    if not isinstance(plan, C.Plan) or not isinstance(grant, C.Grant) or not isinstance(attempt, C.AttemptIdentity):
        raise TypeError("prepare requires Config, Plan, Grant and AttemptIdentity")
    if plan.execution == "selected":
        raise _problem("unsupported-capability",
                       "literal Vitest profiles do not support selected execution")
    if plan.execution not in ("full", "scoped"):
        raise _problem("native-config-invalid", "Vitest plan is not executable")
    if plan.files:
        # Scope arrives through the effective runner args, as for command
        # profiles. Plan.files is a public selection field, not an argv side
        # channel.
        raise _problem(
            "unsupported-capability",
            "literal Vitest profiles do not accept public plan files",
        )
    _require_node_launcher(config.runner.launcher)

    tail = (tuple(config.runner.args) if plan.execution == "scoped"
            else tuple(config.runner.args) + tuple(config.runner.full_args))
    argv = tuple(config.runner.launcher) + (VITEST_ENTRY, "run") + tail
    try:
        summary = C.summarize_command(
            C.RunnerKind.VITEST, plan.mode, argv,
            workers=grant.slots,
            provenance=("vitest-exclusive-command",),
        )
    except (TypeError, ValueError) as exc:
        raise _problem(
            "native-config-invalid",
            "literal Vitest argv violates the configured bounds",
        ) from exc
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=(),
        capability=C.Capability(
            execution=C.ExecutionTier.EXCLUSIVE_COMMAND, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=VITEST_EXCLUSIVE_NOTE,
            ),),
        ),
        summary=summary,
    )
