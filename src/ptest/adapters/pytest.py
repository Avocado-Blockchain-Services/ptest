"""Bounded, local-only preparation for the native pytest bridge."""
from __future__ import annotations

from pathlib import Path
import json
import re

from ptest import contracts as C


_REMOTE_OPTIONS = {"--tx", "--px", "--rsyncdir"}
_PARALLEL_OPTIONS = {
    "-n", "--numprocesses", "--maxprocesses", "--dist",
    "--max-worker-restart", "-f", "--looponfail", "-d", "--distload",
}
_NARROWING_OPTIONS = {
    "--deselect", "--lf", "--last-failed", "--ff", "--failed-first",
    "--sw", "--stepwise", "--sw-skip", "--stepwise-skip", "--testmon",
    "--ignore", "--ignore-glob", "--collect-only", "--co", "--maxfail",
    "--setup-only", "--setup-plan", "--fixtures", "--funcargs",
    "--fixtures-per-test", "--markers", "--cache-show",
    "-h", "--help", "-V", "--version",
}


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _reject_unowned_controls(argv: tuple[str, ...], *, full: bool = False) -> None:
    """Reject controls that would bypass the admission grant before import."""
    for token in argv:
        option = token.split("=", 1)[0]
        if option in _REMOTE_OPTIONS or option in _PARALLEL_OPTIONS:
            raise _problem("native-config-invalid",
                           "pytest remote or parallel control is not ptest-owned")
        # pytest 9 expands @files; do not read a second source of hidden controls.
        # Flag-only short options can precede a value-taking -n/-k/-m in a cluster.
        short = re.match(r"^-[qvxslhVfd]*([nkm])", token)
        if token.startswith("@") or (short and short[1] == "n"):
            raise _problem("native-config-invalid",
                           "pytest remote or parallel control is not ptest-owned")
        if full and (option in _NARROWING_OPTIONS or "::" in token
                     or (short and short[1] in {"k", "m"})
                     or re.fullmatch(r"-[qvs]*x[qvs]*", token)):
            raise _problem("native-config-invalid", "full pytest plans cannot narrow the inventory")


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
    interpreter = Path(launcher[-1])
    valid_python = interpreter.name in {
        "python", "python3", "python3.11", "python3.12", "python3.13", "python3.14",
    }
    direct = len(launcher) == 1 and (interpreter.is_absolute() or launcher[0] == interpreter.name)
    locked_uv = launcher == ("uv", "run", "--locked", "--no-sync", "python")
    if not valid_python or not (direct or locked_uv):
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
    if plan.execution == "none":
        raise _problem("native-config-invalid", "pytest bridge cannot execute an empty plan")
    if plan.execution == "selected":
        raise _problem("unsupported-capability", "pytest selection requires qualified native inventory evidence")

    _require_python_launcher(config.runner.launcher)
    native = config.runner.args
    literal_controls = native + config.runner.full_args + plan.files
    if plan.execution == "full":
        literal_controls += config.runner.test_roots
    _reject_unowned_controls(literal_controls,
                            full=plan.execution == "full")
    if plan.execution == "full":
        native += config.runner.full_args
    else:
        native += plan.files

    generated: tuple[str, ...] = ()
    if grant.slots > 1:
        native += ("-n", str(grant.slots))
        generated = (f"xdist-workers={grant.slots}",)
    if plan.execution == "full":
        native += config.runner.test_roots
    argv = config.runner.launcher + (str(_bridge_path()),) + native
    execution = C.ExecutionTier.BASIC_SERIAL if plan.execution == "scoped" else C.ExecutionTier.UNAVAILABLE
    limitations = () if execution is C.ExecutionTier.BASIC_SERIAL else (C.Reason(
        code="unsupported-capability",
        message=("unqualified pytest foundation: native_cli checks, terminal/coverage "
                 "and inventory evidence pending; cannot launch as a supported profile "
                 "or publish full gates"),
    ),)
    return C.PreparedRun(
        argv=argv,
        cwd=_project_root(config),
        env_updates=(
            ("PTEST_BRIDGE_PROTOCOL", str(Path(__file__).parents[1] / "runtime" / "protocol-v1.json")),
            ("PTEST_GRANT_WORKERS", str(grant.slots)),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_EXECUTION", plan.execution),
            ("PTEST_TEST_ROOTS", json.dumps(config.runner.test_roots)),
        ),
        capability=C.Capability(
            execution=execution, selection=False,
            lifecycle="cooperative-process-group",
            limitations=limitations,
        ),
        summary=C.summarize_command(
            C.RunnerKind.PYTEST, plan.mode, argv, workers=grant.slots,
            generated_options=generated, provenance=("pytest-native-bridge",),
        ),
    )
