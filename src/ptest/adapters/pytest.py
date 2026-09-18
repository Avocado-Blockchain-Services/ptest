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
_FULL_REDIRECT_OPTIONS = {
    "-c", "--config-file", "--rootdir", "--confcutdir", "--noconftest",
    "--pyargs", "-o", "--override-ini", "--basetemp",
}


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _short_redirect_cluster(token: str) -> bool:
    """Recognise value-taking ``-c``/``-o`` inside a short-option cluster."""
    if not token.startswith("-") or token.startswith("--"):
        return False
    short_options = token[1:]
    return len(short_options) > 1 and ("c" in short_options or "o" in short_options)


def _node_id_token(argv: tuple[str, ...], index: int) -> bool:
    """Only positional ``::`` tokens are native node selectors."""
    token = argv[index]
    if "::" not in token or token.startswith("-"):
        return False
    return index == 0 or argv[index - 1] not in {"-W", "--pythonwarnings"}


def _reject_unowned_controls(argv: tuple[str, ...], *, full: bool = False) -> None:
    """Reject controls that would bypass the admission grant before import."""
    for index, token in enumerate(argv):
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
        redirect_cluster = _short_redirect_cluster(token)
        maxfail_zero = (full and option == "--maxfail" and (
            token.partition("=")[2] == "0"
            or ("=" not in token and index + 1 < len(argv) and argv[index + 1] == "0")
        ))
        if full and (option in _NARROWING_OPTIONS or option in _FULL_REDIRECT_OPTIONS
                     or redirect_cluster or _node_id_token(argv, index)
                     or (short and short[1] in {"k", "m"})
                     or re.fullmatch(r"-[qvs]*x[qvs]*", token)) and not maxfail_zero:
            raise _problem("native-config-invalid", "full pytest plans cannot narrow the inventory")


def _validate_full_roots(roots: tuple[str, ...]) -> None:
    """Validate the literal roots before a native parser/project is involved."""
    if not roots or len(set(roots)) != len(roots):
        raise _problem("native-config-invalid", "full pytest roots must be nonempty and unique")
    for root in roots:
        if (not isinstance(root, str) or not root or root == "."
                or root.startswith(("-", "@", "/")) or "\\" in root
                or "::" in root or any(part in {"", ".", ".."} for part in root.split("/"))):
            raise _problem("native-config-invalid", "full pytest roots must be literal project-relative paths")


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


def inspect_capability(config: C.Config) -> C.Capability:
    """Describe Pytest's static conditional capability without native I/O."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        raise _problem("native-config-invalid", "pytest adapter requires pytest config")
    if config.setup is not None:
        return C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(code="unsupported-capability",
                                  message="pytest setup declarations are unsupported in this tier"),),
        )
    try:
        _require_python_launcher(config.runner.launcher)
    except C.Problem as problem:
        return C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(code=problem.code, message=problem.message),),
        )
    try:
        _reject_unowned_controls(config.runner.args + config.runner.full_args
                                 + config.runner.test_roots, full=False)
    except C.Problem:
        return C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=("pytest basic-serial is unavailable for shared native controls; "
                         "remove parallel, remote or argfile configuration"),
            ),),
        )
    limitations = [C.Reason(
        code="unsupported-capability",
        message=("pytest scoped/full basic-serial is conditional on the provisioned native tuple, "
                 "exact hook policy, comparable bounded Git content and exact checkout-root native "
                 "cache placement; non-Git, nested/custom-cache or over-budget evidence makes full "
                 "incomplete/70; inventory/counts/full gates remain unavailable"),
    ), C.Reason(
        code="unsupported-capability",
        message=("allowed plain/wrapper pytest_collection_finish code may mutate the effective item list, "
                 "and setup/fixtures may skip; automatic/setup/shadow/probe, selection, history, "
                 "baseline and whole-gate obligations remain unavailable"),
    )]
    if "." in config.runner.test_roots:
        limitations.insert(0, C.Reason(
            code="unsupported-capability",
            message=("pytest full execution is unavailable for a dot test root in this slice; "
                     "the basic-serial capability describes scoped execution only"),
        ))
    try:
        _validate_full_roots(config.runner.test_roots)
        _reject_unowned_controls(config.runner.args + config.runner.full_args
                                 + config.runner.test_roots, full=True)
    except C.Problem:
        limitations.insert(0, C.Reason(
            code="unsupported-capability",
            message=("pytest full execution is unavailable for the configured native controls; "
                     "scoped execution remains literal basic-serial"),
        ))
    return C.Capability(
        execution=C.ExecutionTier.BASIC_SERIAL, selection=False,
        lifecycle="cooperative-process-group", limitations=tuple(limitations),
    )


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
    if plan.execution not in {"scoped", "full"}:
        raise _problem("unsupported-capability", "pytest execution mode is unavailable")
    expected_mode = C.Mode.FULL if plan.execution == "full" else C.Mode.SCOPED
    if plan.mode is not expected_mode:
        raise _problem("native-config-invalid", "pytest plan mode does not match execution mode")
    if grant.slots != 1:
        raise _problem("admission-invalid", "pytest basic-serial execution requires one slot")
    if plan.execution == "full" and plan.files:
        raise _problem("native-config-invalid", "pytest full plans cannot carry scoped files")
    if plan.execution == "full":
        _validate_full_roots(config.runner.test_roots)

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
    if plan.execution == "full":
        native += config.runner.test_roots
    argv = config.runner.launcher + (str(_bridge_path()),) + native
    execution = C.ExecutionTier.BASIC_SERIAL
    limitations = (() if plan.execution == "scoped" else (C.Reason(
        code="unsupported-capability",
        message="pytest full observes native outcomes only; inventory, source validity and full gates remain unavailable",
    ),))
    return C.PreparedRun(
        argv=argv,
        cwd=_project_root(config),
        env_updates=(
            ("PTEST_BRIDGE_PROTOCOL", str(Path(__file__).parents[1] / "runtime" / "protocol-v1.json")),
            ("PTEST_GRANT_WORKERS", str(grant.slots)),
            ("PTEST_RUN_ID", grant.run_id),
            ("PTEST_EXECUTION", plan.execution),
            ("PTEST_TEST_ROOTS", json.dumps(config.runner.test_roots)),
            ("PTEST_PYTEST_CHECKOUT_ROOT", str(_project_root(config).resolve())),
            ("PTEST_PYTEST_CONFIG_PATH", "" if config.config_path is None
             else str(config.config_path.resolve())),
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
