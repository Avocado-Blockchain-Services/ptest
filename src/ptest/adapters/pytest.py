"""Bounded, local-only preparation for the native pytest bridge."""
from __future__ import annotations

from pathlib import Path

from ptest import contracts as C


_REMOTE_OPTIONS = {"--tx", "--px", "--rsyncdir"}
_PARALLEL_OPTIONS = {
    "-n", "--numprocesses", "--maxprocesses", "--dist",
    "--max-worker-restart",
}


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _reject_unowned_controls(argv: tuple[str, ...]) -> None:
    """Reject controls that would bypass the admission grant before import."""
    for token in argv:
        option = token.split("=", 1)[0]
        if option in _REMOTE_OPTIONS or option in _PARALLEL_OPTIONS:
            raise _problem("native-config-invalid",
                           "pytest remote or parallel control is not ptest-owned")
        if token.startswith("-n") and token != "-q":
            raise _problem("native-config-invalid",
                           "pytest remote or parallel control is not ptest-owned")


def _project_root(config: C.Config) -> Path:
    if config.checkout is not None:
        return config.checkout.root
    if config.config_path is not None:
        return config.config_path.parent
    raise _problem("native-config-invalid", "pytest configuration has no project root")


def _bridge_path() -> Path:
    """The guard executes this trusted file with the project interpreter."""
    return Path(__file__).parents[1] / "runtime" / "pytest_bridge.py"


def _require_python_launcher(launcher: tuple[str, ...]) -> None:
    """A bridge file is meaningful only when the selected launcher is Python."""
    executable = Path(launcher[-1]).name.lower()
    if executable not in {"python", "python3"} and not executable.startswith("python3."):
        raise _problem("native-config-invalid",
                       "pytest bridge requires a CPython interpreter launcher")


def prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
            attempt: C.AttemptIdentity) -> C.PreparedRun:
    """Create literal pytest argv after the scheduler has granted capacity.

    This deliberately does not import pytest.  The guarded project-interpreter
    bridge is responsible for native configuration parsing and effective-profile
    checks when T11 executes it under the guard.
    """
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        raise _problem("native-config-invalid", "pytest adapter requires pytest config")
    if not isinstance(plan, C.Plan) or not isinstance(grant, C.Grant) or not isinstance(attempt, C.AttemptIdentity):
        raise TypeError("prepare requires Config, Plan, Grant and AttemptIdentity")
    if grant.run_id != attempt.run_id or grant.slots != attempt.worker_count:
        raise _problem("admission-invalid", "pytest attempt does not match its admission grant")
    if config.runner.workers != grant.slots:
        raise _problem("admission-invalid", "pytest configured workers do not match admission grant")
    if plan.execution == "none":
        raise _problem("native-config-invalid", "pytest bridge cannot execute an empty plan")

    _require_python_launcher(config.runner.launcher)
    native = config.runner.args
    _reject_unowned_controls(native + config.runner.full_args)
    if plan.execution == "full":
        native += config.runner.full_args
    else:
        if not plan.files:
            raise _problem("native-config-invalid", "pytest selected plan has no files")
        native += plan.files

    generated: tuple[str, ...] = ()
    capability = C.ExecutionTier.BASIC_SERIAL
    if grant.slots > 1:
        native += ("-n", str(grant.slots))
        generated = (f"xdist-workers={grant.slots}",)
        capability = C.ExecutionTier.ADVANCED
    if plan.execution == "full":
        native += config.runner.test_roots
    argv = config.runner.launcher + (str(_bridge_path()),) + native
    return C.PreparedRun(
        argv=argv,
        cwd=_project_root(config),
        env_updates=(
            ("PTEST_BRIDGE_PROTOCOL", str(Path(__file__).parents[1] / "runtime" / "protocol-v1.json")),
            ("PTEST_GRANT_WORKERS", str(grant.slots)),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_GRANT_NONCE", grant.nonce),
        ),
        capability=C.Capability(
            execution=capability, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message="native effective configuration is validated in the project interpreter",
            ),),
        ),
        summary=C.summarize_command(
            C.RunnerKind.PYTEST, plan.mode, argv, workers=grant.slots,
            generated_options=generated, provenance=("pytest-native-bridge",),
        ),
    )
