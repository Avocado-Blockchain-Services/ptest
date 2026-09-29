"""Vitest execution through the project-local Vitest CLI.

``kind = "vitest"`` stays the config kind so existing ``web/.ptest.toml``
files keep working. Scope arrives through the effective runner args (the
caller scope is already appended there by the orchestrator), so
``plan.files`` must stay empty. ptest claims no per-test results; the vitest
exit code is the outcome.

When the installed Vitest version is known and the project does not pin its
own workers or pool in runner args, ptest caps Vitest at the granted slots
so the run shares the machine like any bounded runner. Otherwise it runs as
one exclusive command that reserves the whole machine. Measured on real
installs: Vitest 3 ignores ``--maxWorkers`` when the project config sets
``poolOptions.<pool>`` limits but honours the CLI poolOptions caps; Vitest 4
and later dropped poolOptions (the flags are unknown options) and honour
``--maxWorkers``.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from ptest import contracts as C

VITEST_ENTRY = "node_modules/vitest/vitest.mjs"
VITEST_EXCLUSIVE_NOTE = ("Vitest runs as one exclusive command (node node_modules/vitest/vitest.mjs run); "
                         "ptest does not own Vitest workers, selection or per-test results")
VITEST_CAPPED_NOTE = ("ptest caps Vitest at the granted workers; "
                      "ptest does not own Vitest selection or per-test results")
_VERSION_FILE = "node_modules/vitest/package.json"
_VERSION_MAX_BYTES = 65536
_VERSION_RE = re.compile(r"^(\d+)\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]*)?$")
_MIN_CAPPED_MAJOR = 3
# Runner-arg controls through which a project owns Vitest's parallelism.
_WORKER_CONTROLS = ("--maxWorkers", "--minWorkers", "--pool", "--poolOptions",
                    "--fileParallelism", "--no-file-parallelism")


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


def _installed_major(root: Path) -> int | None:
    """Major version of the project-local Vitest, or None when unknown.

    The package manifest itself must be a regular file (never a symlink) of
    bounded size; a symlinked package directory (pnpm) is fine.
    """
    path = root / _VERSION_FILE
    try:
        stamp = os.lstat(path)
        if not stat.S_ISREG(stamp.st_mode) or stamp.st_size > _VERSION_MAX_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            data = json.loads(stream.read(_VERSION_MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    match = _VERSION_RE.match(version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def _owns_workers(args: tuple[str, ...]) -> bool:
    return any(token == control or token.startswith(control + "=")
               or token.startswith(control + ".")
               for token in args for control in _WORKER_CONTROLS)


def capped_major(config: C.Config) -> int | None:
    """The Vitest major ptest can cap, or None when the run stays exclusive."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.VITEST:
        return None
    if _owns_workers(tuple(config.runner.args) + tuple(config.runner.full_args)):
        return None
    try:
        root = _project_root(config)
    except C.Problem:
        return None
    major = _installed_major(root)
    return major if major is not None and major >= _MIN_CAPPED_MAJOR else None


def requires_exclusive(config: C.Config) -> bool:
    """Exclusive admission unless ptest can cap this Vitest install."""
    return capped_major(config) is None


def worker_caps(major: int, workers: int) -> tuple[str, ...]:
    """CLI flags that bound Vitest to ``workers`` for the given major."""
    if major >= 4:
        return (f"--maxWorkers={workers}",)
    return (f"--maxWorkers={workers}", "--minWorkers=1",
            f"--poolOptions.threads.maxThreads={workers}", "--poolOptions.threads.minThreads=1",
            f"--poolOptions.forks.maxForks={workers}", "--poolOptions.forks.minForks=1",
            f"--poolOptions.vmThreads.maxThreads={workers}", "--poolOptions.vmThreads.minThreads=1",
            f"--poolOptions.vmForks.maxForks={workers}", "--poolOptions.vmForks.minForks=1")


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
    root = (config.checkout.root if config.checkout is not None
            else config.config_path.parent if config.config_path is not None else None)
    if config.setup is None and root is not None and not (root / VITEST_ENTRY).exists():
        # Launching node here only prints a MODULE_NOT_FOUND stack trace that
        # reads like a test failure; say what is missing instead.
        from ptest.config import node_install_argv
        install = " ".join(node_install_argv(root) or ("npm", "install"))
        raise _problem(
            "native-config-invalid",
            f"vitest is not installed ({VITEST_ENTRY} is missing): run {install}, "
            "or declare it as [setup] in .ptest.toml")

    tail = (tuple(config.runner.args) if plan.execution == "scoped"
            else tuple(config.runner.args) + tuple(config.runner.full_args))
    major = capped_major(config)
    caps = () if major is None else worker_caps(major, grant.slots)
    argv = tuple(config.runner.launcher) + (VITEST_ENTRY, "run") + caps + tail
    try:
        summary = C.summarize_command(
            C.RunnerKind.VITEST, plan.mode, argv,
            workers=grant.slots,
            provenance=(("vitest-exclusive-command",) if major is None
                        else ("vitest-capped-command",)),
        )
    except (TypeError, ValueError) as exc:
        raise _problem(
            "native-config-invalid",
            "literal Vitest argv violates the configured bounds",
        ) from exc
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=(),
        capability=C.Capability(
            execution=(C.ExecutionTier.EXCLUSIVE_COMMAND if major is None
                       else C.ExecutionTier.BOUNDED_NATIVE),
            selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=VITEST_EXCLUSIVE_NOTE if major is None else VITEST_CAPPED_NOTE,
            ),),
        ),
        summary=summary,
    )
