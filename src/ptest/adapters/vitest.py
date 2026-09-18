"""Preparation for the intentionally unavailable Vitest basic-serial bridge."""
from __future__ import annotations

import json
from pathlib import Path

from ptest import contracts as C


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _bridge_path() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / "vitest_bridge.mjs"


def _project_root(config: C.Config) -> Path:
    if config.checkout is not None:
        return config.checkout.root
    if config.config_path is not None:
        return config.config_path.parent
    raise _problem("native-config-invalid", "Vitest configuration has no project root")


def _require_node_launcher(launcher: tuple[str, ...]) -> None:
    """Accept a direct ``node`` PATH lookup or exactly one absolute node path."""
    if len(launcher) != 1:
        raise _problem("native-config-invalid", "Vitest bridge requires one direct Node launcher")
    executable = launcher[0]
    candidate = Path(executable)
    if executable != "node" and not (candidate.is_absolute() and candidate.name in {"node", "node.exe"}):
        raise _problem("native-config-invalid", "Vitest bridge requires a direct Node launcher")


def _scoped_files_binding(files: tuple[str, ...]) -> str:
    """Bind scope separately from native options, within the control-frame bound."""
    if not files or len(files) > 256 or any(
        not isinstance(file, str) or not file or file.startswith("-") or "\x00" in file
        for file in files
    ):
        raise _problem("native-config-invalid", "Vitest scoped files must be nonempty literal paths")
    binding = json.dumps(files, ensure_ascii=False, separators=(",", ":"))
    try:
        size = len(binding.encode("utf-8"))
    except UnicodeEncodeError:
        raise _problem("native-config-invalid", "Vitest scoped files require valid Unicode") from None
    if size > C.CONTROL_FRAME_MAX_BYTES:
        raise _problem("native-config-invalid", "Vitest scoped files exceed the binding size bound")
    return binding


def prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
            attempt: C.AttemptIdentity) -> C.PreparedRun:
    """Prepare a literal, one-slot scoped request; execution remains unavailable.

    Report allocation is executor-owned, so ``report_path`` deliberately remains
    unset here. The bridge refuses to load a project when that binding is absent.
    """
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.VITEST:
        raise _problem("native-config-invalid", "Vitest adapter requires runner.kind=vitest")
    if not isinstance(plan, C.Plan) or not isinstance(grant, C.Grant) or not isinstance(attempt, C.AttemptIdentity):
        raise TypeError("prepare requires Config, Plan, Grant and AttemptIdentity")
    if plan.mode is not C.Mode.SCOPED or plan.execution != "scoped":
        raise _problem("unsupported-capability", "Vitest foundation only prepares explicit scoped execution")
    scoped_files = _scoped_files_binding(plan.files)
    if grant.run_id != attempt.run_id or grant.slots != 1 or attempt.worker_count != 1:
        raise _problem("admission-invalid", "Vitest basic-serial attempt requires one admitted slot")
    _require_node_launcher(config.runner.launcher)

    native_args = tuple(config.runner.args) + tuple(plan.files)
    argv = tuple(config.runner.launcher) + (str(_bridge_path()), "--") + native_args
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config),
        env_updates=(
            ("PTEST_RUN_ID", grant.run_id), ("PTEST_GRANT_NONCE", grant.nonce),
            ("PTEST_VITEST_ATTEMPT", attempt.attempt_id),
            ("PTEST_VITEST_EXECUTION", "scoped"), ("PTEST_VITEST_PROFILE", "basic_serial"),
            ("PTEST_VITEST_SCOPED_FILES", scoped_files),
            ("PTEST_VITEST_WORKERS", "1"), ("VITEST_MAX_FORKS", "1"),
            ("VITEST_MIN_FORKS", "1"), ("VITEST_MAX_THREADS", "1"), ("VITEST_MIN_THREADS", "1"),
        ),
        capability=C.Capability(execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group", limitations=(C.Reason(
                code="unsupported-capability", message=("Vitest basic_serial is prepared only; "
                "executor-owned report allocation and real native tuple qualification are unavailable.")),)),
        summary=C.summarize_command(C.RunnerKind.VITEST, plan.mode, argv, workers=1,
            generated_options=("vitest.workers=1",), provenance=("vitest-basic-serial-prepared",)),
    )
