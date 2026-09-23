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

# This is the closed, in-product admission catalog.  It deliberately names a
# configuration class rather than trusting a repository-provided profile name:
# a pytest tuple is admissible only when the declared command carries the
# coverage/reporter policy that the advanced bridge will verify after launch.
# The catalog does not discover pytest or certify an installed environment.
_QUALIFIED_PROFILE = "pytest-advanced-v1"


def _qualified_profile_catalog(config: C.Config) -> dict[str, str] | None:
    """Return the static catalog declaration for an advanced pytest tuple."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        return None
    try:
        require_python_launcher(config.runner.launcher)
        controls = tuple(config.runner.args) + tuple(config.runner.full_args)
        reject_unowned_controls(controls, full=False)
    except C.Problem:
        return None
    # Coverage and a named terminal reporter are part of the frozen SELECT
    # tuple.  Their actual plugin/version and measured files remain bridge
    # evidence; these strings only select the closed declaration.
    has_coverage = any(token == "--cov" or token.startswith("--cov=")
                       for token in controls)
    has_reporter = any(token == "--cov-report" or token.startswith("--cov-report=")
                       for token in controls)
    if not (has_coverage and has_reporter):
        return None
    return {"source": "catalog", "runner": C.RunnerKind.PYTEST.value,
            "profile": _QUALIFIED_PROFILE, "coverage_policy": "pytest-cov",
            "reporter_policy": "terminal"}


def qualified_profile(config: C.Config) -> dict[str, str] | None:
    """Expose the closed catalog lookup to the adapter registry."""
    return _qualified_profile_catalog(config)


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _unsupported(message: str) -> C.CompoundSupport:
    return C.CompoundSupport(
        selection=False, parallel_identity=False, profile=None,
        limitations=(C.Reason(code="unsupported-capability", message=message),),
    )


def compound_support(config: C.Config, *, qualified_profile: dict[str, str] | None = None) -> C.CompoundSupport:
    """Return static compound capability without certifying native evidence.

    Native imports, version discovery, collection, coverage and worker probes
    are deliberately absent here.  A profile can only be promoted by a
    caller after consuming a complete authenticated advanced report.
    """
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        return _unsupported("pytest compound support requires a pytest configuration")
    try:
        require_python_launcher(config.runner.launcher)
        reject_unowned_controls(config.runner.args + config.runner.full_args, full=False)
    except C.Problem:
        return _unsupported("pytest native controls are not owned by the compound profile")
    if qualified_profile is None:
        qualified_profile = _qualified_profile_catalog(config)
    if (not isinstance(qualified_profile, dict)
            or qualified_profile.get("runner") != C.RunnerKind.PYTEST.value
            or qualified_profile.get("profile") != _QUALIFIED_PROFILE
            or (qualified_profile.get("source") == "catalog" and
                (qualified_profile.get("coverage_policy") != "pytest-cov"
                 or qualified_profile.get("reporter_policy") != "terminal"))
            or (qualified_profile.get("source") != "catalog" and
                (not re.fullmatch(r"[0-9a-f]{64}", qualified_profile.get("runtime_identity", ""))
                 or not re.fullmatch(r"[0-9a-f]{64}", qualified_profile.get("inventory_digest", ""))
                 or not re.fullmatch(r"[0-9a-f]{64}", qualified_profile.get("evidence_digest", ""))))):
        return _unsupported(
            "pytest advanced selection and worker identity require consumed native qualification")
    return C.CompoundSupport(
        selection=True, parallel_identity=False, profile="pytest-advanced-v1",
        limitations=(),
    )


def _short_redirect_cluster(token: str) -> bool:
    """Recognise value-taking ``-c``/``-o`` inside a short-option cluster."""
    if (not token.startswith("-") or token.startswith("--")
            or token.startswith("-W")):
        return False
    short_options = token[1:]
    return len(short_options) > 1 and ("c" in short_options or "o" in short_options)


def _node_id_token(argv: tuple[str, ...], index: int) -> bool:
    """Only positional ``::`` tokens are native node selectors."""
    token = argv[index]
    if "::" not in token or token.startswith("-"):
        return False
    return index == 0 or argv[index - 1] not in {"-W", "--pythonwarnings"}


def reject_unowned_controls(argv: tuple[str, ...], *, full: bool = False) -> None:
    """Reject controls that would bypass the admission grant before import."""
    index = 0
    while index < len(argv):
        token = argv[index]
        # Generated serial spellings neutralize xdist without removing it
        # (blocking xdist breaks conftests that implement xdist hooks).
        # These four spellings are the only accepted parallel-family controls.
        if token in ("-n", "--numprocesses") \
                and index + 1 < len(argv) and argv[index + 1] == "0":
            index += 2
            continue
        if token in ("-n0", "--numprocesses=0"):
            index += 1
            continue
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
        index += 1


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


def require_python_launcher(launcher: tuple[str, ...]) -> None:
    """A bridge file is meaningful only when the selected launcher is Python."""
    interpreter = Path(launcher[-1])
    valid_python = interpreter.name in {
        "python", "python3", "python3.11", "python3.12", "python3.13", "python3.14",
    }
    direct = len(launcher) == 1 and (interpreter.is_absolute() or launcher[0] == interpreter.name)
    locked_uv = launcher == ("uv", "run", "--locked", "--no-sync", "python")
    locked_project = (
        len(launcher) == 7
        and launcher[:4] == ("uv", "run", "--locked", "--no-sync")
        and launcher[4] == "--project"
        and Path(launcher[5]).is_absolute()
        and launcher[6] == "python"
    )
    if not valid_python or not (direct or locked_uv or locked_project):
        raise _problem("native-config-invalid",
                       "pytest bridge requires a CPython interpreter launcher")


def inspect_capability(config: C.Config) -> C.Capability:
    """Describe Pytest's static conditional capability without native I/O."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        raise _problem("native-config-invalid", "pytest adapter requires pytest config")
    try:
        require_python_launcher(config.runner.launcher)
    except C.Problem as problem:
        return C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(code=problem.code, message=problem.message),),
        )
    try:
        reject_unowned_controls(config.runner.args + config.runner.full_args
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
        message=("declared setup executes under the guard with its configured network and lifecycle-script "
                 "implications; automatic, shadow and probe remain unavailable; setup/fixtures may skip "
                 "and allowed plain/wrapper pytest_collection_finish code may mutate the effective item "
                 "list; selection, history, baseline and whole-gate obligations remain unavailable"),
    )]
    if "." in config.runner.test_roots:
        limitations.insert(0, C.Reason(
            code="unsupported-capability",
            message=("pytest full execution is unavailable for a dot test root in this slice; "
                     "the basic-serial capability describes scoped execution only"),
        ))
    try:
        _validate_full_roots(config.runner.test_roots)
        reject_unowned_controls(config.runner.args + config.runner.full_args
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

    require_python_launcher(config.runner.launcher)
    native = tuple(config.runner.args)
    literal_controls = native + config.runner.full_args + plan.files
    if plan.execution == "full":
        literal_controls += config.runner.test_roots
    reject_unowned_controls(literal_controls,
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


def prepare_advanced(config: C.Config, plan: C.Plan, grant: C.Grant,
                     attempt: C.AttemptIdentity,
                     *, expected_runtime_identity: str | None = None) -> C.PreparedRun:
    """Prepare a native advanced attempt after qualification has been admitted.

    The caller must have a qualified profile from real prior evidence.  This
    function only creates literal argv/env bindings; the bridge revalidates
    every native fact after admission and refuses promotion on drift.
    """
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.PYTEST:
        raise _problem("native-config-invalid", "pytest adapter requires pytest config")
    if not isinstance(plan, C.Plan) or not isinstance(grant, C.Grant) or not isinstance(attempt, C.AttemptIdentity):
        raise TypeError("prepare_advanced requires Config, Plan, Grant and AttemptIdentity")
    if grant.run_id != attempt.run_id or grant.slots != attempt.worker_count:
        raise _problem("admission-invalid", "pytest advanced attempt does not match its grant")
    if plan.execution not in {"scoped", "selected", "full"}:
        raise _problem("unsupported-capability", "pytest advanced execution mode is unavailable")
    if plan.execution == "selected" and not plan.files:
        raise _problem("unsupported-capability", "pytest advanced selection requires exact files")
    if plan.execution == "selected" and any(
            not isinstance(path, str) or not path or path.startswith(("-", "@", "/", "\\"))
            or "\x00" in path or "::" in path
            or any(part in {"", ".", ".."} for part in path.replace("\\", "/").split("/"))
            for path in plan.files):
        raise _problem("native-config-invalid", "pytest advanced selection files are unsafe")
    if plan.execution == "selected":
        if len(set(plan.files)) != len(plan.files):
            raise _problem("native-config-invalid", "pytest advanced selection files are duplicated")
    if plan.execution == "full":
        _validate_full_roots(config.runner.test_roots)
    require_python_launcher(config.runner.launcher)
    controls = tuple(config.runner.args) + tuple(config.runner.full_args)
    controls += tuple(config.runner.test_roots if plan.execution == "full" else plan.files)
    reject_unowned_controls(controls, full=plan.execution == "full")
    native = tuple(config.runner.args)
    if plan.execution == "full":
        native += tuple(config.runner.full_args) + tuple(config.runner.test_roots)
    else:
        native += tuple(plan.files)
    generated = ("pytest-xdist.workers=%d" % grant.slots,) if grant.slots > 1 else ()
    if grant.slots > 1:
        native += ("-n", str(grant.slots))
    argv = tuple(config.runner.launcher) + (str(_bridge_path()),) + native
    env = [
        ("PTEST_BRIDGE_PROTOCOL", str(Path(__file__).parents[1] / "runtime" / "protocol-v1.json")),
        ("PTEST_GRANT_WORKERS", str(grant.slots)), ("PTEST_RUN_ID", grant.run_id),
        ("PTEST_GRANT_NONCE", grant.nonce), ("PTEST_EXECUTION", plan.execution),
        ("PTEST_PYTEST_EXECUTION", plan.execution), ("PTEST_PYTEST_PROFILE", "advanced"),
        ("PTEST_PYTEST_ATTEMPT", attempt.attempt_id),
        ("PTEST_WORKER_ID", "w000"), ("PTEST_RESOURCE_PREFIX", attempt.resource_prefix),
        ("PTEST_PYTEST_SELECTED_FILES", json.dumps(plan.files)
         if plan.execution == "selected" else ""),
        ("PTEST_TEST_ROOTS", json.dumps(config.runner.test_roots)),
        ("PTEST_PYTEST_CHECKOUT_ROOT", str(_project_root(config).resolve())),
        ("PTEST_PYTEST_CONFIG_PATH", "" if config.config_path is None else str(config.config_path.resolve())),
        ("PTEST_PYTEST_COMMAND_VARIANTS", json.dumps([
            list(config.runner.launcher) + list(config.runner.args),
            list(config.runner.launcher) + list(config.runner.full_args)
            + list(config.runner.test_roots)])),
    ]
    if expected_runtime_identity is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_runtime_identity):
            raise _problem("native-config-invalid", "expected pytest runtime identity is malformed")
        env.append(("PTEST_EXPECTED_RUNTIME_IDENTITY", expected_runtime_identity))
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=tuple(env),
        capability=C.Capability(
            execution=C.ExecutionTier.ADVANCED, selection=False,
            lifecycle="cooperative-process-group", limitations=()),
        summary=C.summarize_command(
            C.RunnerKind.PYTEST, plan.mode, argv, workers=grant.slots,
            generated_options=generated,
            provenance=("pytest-native-advanced-bridge",)),
    )
