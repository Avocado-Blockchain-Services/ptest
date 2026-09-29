"""Vitest execution through the project-local Vitest CLI.

``kind = "vitest"`` stays the config kind so existing ``web/.ptest.toml``
files keep working. Scope arrives through the effective runner args (the
caller scope is already appended there by the orchestrator), so
``plan.files`` must stay empty. ptest claims no per-test results; the vitest
exit code is the outcome.

When the installed Vitest (3 or later) and the project's worker settings are
known, ptest bounds Vitest to the granted slots through its environment
(``VITEST_MAX_WORKERS``/``_THREADS``/``_FORKS`` and ``VITEST_MIN_THREADS``/
``_FORKS`` = 1), and the run shares the machine like any bounded runner. The
environment caps every pool on Vitest 3, 4 and 5, including 4.x projects,
without version-specific flags, so a setup that changes the version cannot
break the command. A project's own lower literal ceiling (``maxWorkers``,
``maxThreads``, ``maxForks``, ``fileParallelism: false``, ``singleThread``,
``singleFork``) is kept, never raised. Anything ptest cannot read statically
keeps the old exclusive command that reserves the whole machine.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import stat
from dataclasses import dataclass
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
# Runner-arg controls through which a project owns Vitest's parallelism or
# points at a config ptest cannot read.
_WORKER_CONTROLS = ("--maxWorkers", "--minWorkers", "--pool", "--poolOptions",
                    "--fileParallelism", "--no-file-parallelism", "--config",
                    "--workspace", "--project", "--root", "--browser")
_SHORT_CONTROLS = ("-c", "-r")
_CONFIG_MAX_BYTES = 256 * 1024
_VITEST_CONFIGS = tuple(f"vitest.config.{ext}" for ext in ("ts", "mts", "cts", "js", "mjs", "cjs"))
_VITE_CONFIGS = tuple(f"vite.config.{ext}" for ext in ("ts", "mts", "cts", "js", "mjs", "cjs"))
_WORKSPACE_FILES = tuple(f"vitest.workspace.{ext}"
                         for ext in ("ts", "mts", "cts", "js", "mjs", "cjs", "json"))
_MAX_KEY = re.compile(r"\b(maxWorkers|maxThreads|maxForks)\s*:\s*([^,}\n]*)")
_SERIAL_KEY = re.compile(r"\b(singleThread|singleFork|fileParallelism)\s*:\s*([^,}\n]*)")
_WORKER_WORD = re.compile(r"\b(maxWorkers|maxThreads|maxForks|singleThread|singleFork|fileParallelism)\b")
# Settings that can move worker limits out of this file's literal text: a
# merged or extended config, per-project configs, or browser mode.
_INDIRECT = re.compile(
    r"\bmergeConfig\s*\(|\bdefineWorkspace\s*\(|\bextends\s*:\s*['\"`]"
    r"|\b(?:projects|workspace|browser)\s*:")
_IMPORT_SOURCE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+)['"`]([^'"`]+)['"`]""",
    re.MULTILINE)
_IMPORT_BINDING = re.compile(
    r"""^\s*import\s+([^'"`;]+?)\s+from\s*['"`]([^'"`]+)['"`]""", re.MULTILINE)
_SOURCE_SUFFIXES = ("", ".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".json")
_ANCESTOR_LIMIT = 32
_ENV_LIMITS = ("VITEST_MAX_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS")
# The environment ptest sets on a bounded run; each Vitest major reads the
# names it knows and ignores the rest.
_CAP_ENV = ("VITEST_MAX_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS")
_FLOOR_ENV = ("VITEST_MIN_THREADS", "VITEST_MIN_FORKS")


@dataclass(frozen=True, slots=True)
class VitestBound:
    """ptest may bound this Vitest run; ``limit`` is the project's own ceiling."""

    limit: int | None


# The admission decision, handed to prepare so both see the same answer.
_UNSET = object()
DECISION: contextvars.ContextVar = contextvars.ContextVar(
    "ptest_vitest_decision", default=_UNSET)


class _Unknown(Exception):
    """A worker setting ptest cannot read statically."""


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
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    match = _VERSION_RE.match(version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def _owns_workers(args: tuple[str, ...]) -> bool:
    return any(token == control or token.startswith(control + "=")
               or token.startswith(control + ".")
               for token in args for control in _WORKER_CONTROLS) or any(
        token == short or (token.startswith(short) and len(token) > len(short)
                           and not token.startswith("--"))
        for token in args for short in _SHORT_CONTROLS)


def _read_config(root: Path, name: str) -> str | None:
    path = root / name
    try:
        stamp = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unknown(name) from exc
    if not stat.S_ISREG(stamp.st_mode) or stamp.st_size > _CONFIG_MAX_BYTES:
        raise _Unknown(name)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            return stream.read(_CONFIG_MAX_BYTES + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Unknown(name) from exc


def _strip_comments(text: str) -> str:
    """Drop JS comments outside string and template literals.

    Globs such as ``'tests/*.test.js'`` hold ``/*`` and ``*/`` inside
    strings; a comment regex would pair them and delete real settings in
    between. Unterminated strings or comments (e.g. a regex literal holding
    a quote) are unknown, never guessed.
    """
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char in "'\"`":
            end = index + 1
            while end < length and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            if end >= length:
                raise _Unknown("unterminated string")
            out.append(text[index:end + 1])
            index = end + 1
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = length if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise _Unknown("unterminated comment")
            out.append(" ")
            index = end + 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _imported_text(root: Path, source: str) -> str | None:
    """One level of a relative import, or raise _Unknown when unreadable."""
    base = (root / source) if not source.startswith("/") else Path(source)
    candidates = [Path(str(base) + suffix) for suffix in _SOURCE_SUFFIXES]
    candidates += [base / f"index{suffix}" for suffix in _SOURCE_SUFFIXES[1:]]
    for candidate in candidates:
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            raise _Unknown(source)
        text = _read_config(root, str(relative))
        if text is not None:
            return text
    raise _Unknown(source)


def _bare_binding_used_as_config(code: str) -> bool:
    """A package import whose binding is spread or used as the config value."""
    for names, source in _IMPORT_BINDING.findall(code):
        if source.startswith((".", "/")) or source in ("vitest/config", "vite", "vitest"):
            continue
        for name in re.findall(r"[A-Za-z_$][\w$]*", names.replace(" as ", " ")):
            if name in ("type", "default"):
                continue
            used = re.compile(
                r"\.\.\.\s*" + re.escape(name) + r"\b"
                r"|\btest\s*:\s*" + re.escape(name) + r"\b"
                r"|\bdefine(?:Config|Project)\s*\(\s*" + re.escape(name) + r"\b"
                r"|\bexport\s+default\s+" + re.escape(name) + r"\b")
            if used.search(code):
                return True
    return False


def _config_like(source: str) -> bool:
    """An import that may carry Vitest settings ptest cannot see."""
    if source.startswith((".", "/")):
        return re.search(r"vite|vitest|config", source, re.IGNORECASE) is not None
    return (source not in ("vitest/config", "vite", "vitest")
            and re.search(r"(?:vite|vitest)[-_./]?config", source, re.IGNORECASE) is not None)


def _existing_configs(directory: Path) -> list[str]:
    return [name for name in _VITEST_CONFIGS + _VITE_CONFIGS + _WORKSPACE_FILES
            if os.path.lexists(directory / name)]


def _config_limit(root: Path, major: int) -> int | None:
    """The project's own literal worker ceiling, None when it sets none.

    Fails closed (raises _Unknown) whenever a limit could live outside the
    literal text ptest reads: merged, extended or imported configs,
    per-project configs, workspace files, browser mode, any worker key not
    in plain ``key: literal`` form, or (below Vitest 5, which search
    upwards) a config found only in a parent directory.
    """
    if any(_read_config(root, name) is not None for name in _WORKSPACE_FILES):
        raise _Unknown("workspace")
    texts = [text for text in (_read_config(root, name)
                               for name in _VITEST_CONFIGS + _VITE_CONFIGS)
             if text is not None]
    if not texts:
        if major < 5:
            parent = root.parent
            for _ in range(_ANCESTOR_LIMIT):
                if _existing_configs(parent):
                    raise _Unknown("ancestor config")
                if parent.parent == parent:
                    break
                parent = parent.parent
        return None
    limits: list[int] = []
    for text in texts:
        code = _strip_comments(text)
        if _INDIRECT.search(code):
            raise _Unknown("indirect")
        sources = _IMPORT_SOURCE.findall(code)
        if any(_config_like(source) for source in sources):
            raise _Unknown("imported config")
        if _bare_binding_used_as_config(code):
            raise _Unknown("package config")
        for source in sources:
            if not source.startswith((".", "/")):
                continue
            imported = _strip_comments(_imported_text(root, source))
            if _WORKER_WORD.search(imported) or _INDIRECT.search(imported):
                raise _Unknown(source)
        literal = _MAX_KEY.findall(code) + _SERIAL_KEY.findall(code)
        if len(_WORKER_WORD.findall(code)) != len(literal):
            raise _Unknown("worker key form")
        for _key, value in _MAX_KEY.findall(code):
            value = value.strip()
            if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
                raise _Unknown(value)
            limits.append(int(value))
        for key, value in _SERIAL_KEY.findall(code):
            value = value.strip()
            serial = "false" if key == "fileParallelism" else "true"
            if value == serial:
                limits.append(1)
            elif value not in ("true", "false"):
                raise _Unknown(value)
    return min(limits) if limits else None


def _inherited_limit() -> int | None:
    """An exported VITEST_MAX_* the caller set; Vitest would honour it."""
    limits = []
    for name in _ENV_LIMITS:
        value = os.environ.get(name)
        if value is None:
            continue
        if not re.fullmatch(r"[1-9][0-9]{0,3}", value.strip()):
            raise _Unknown(name)
        limits.append(int(value.strip()))
    return min(limits) if limits else None


def bound(config: C.Config) -> VitestBound | None:
    """How ptest may bound this Vitest run, or None to stay exclusive."""
    if not isinstance(config, C.Config) or config.runner.kind is not C.RunnerKind.VITEST:
        return None
    if _owns_workers(tuple(config.runner.args) + tuple(config.runner.full_args)):
        return None
    try:
        root = _project_root(config)
    except C.Problem:
        return None
    major = _installed_major(root)
    if major is None or major < _MIN_CAPPED_MAJOR:
        return None
    try:
        found = [limit for limit in (_config_limit(root, major), _inherited_limit())
                 if limit is not None]
    except (_Unknown, RecursionError):
        return None
    return VitestBound(limit=min(found) if found else None)


def requires_exclusive(config: C.Config) -> bool:
    """Exclusive admission unless ptest can bound this Vitest run."""
    return bound(config) is None


def worker_env(workers: int) -> tuple[tuple[str, str], ...]:
    """Environment that bounds Vitest 3+ to ``workers`` across every pool."""
    return (tuple((name, str(workers)) for name in _CAP_ENV)
            + tuple((name, "1") for name in _FLOOR_ENV))


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
    decided = DECISION.get()
    bounded = bound(config) if decided is _UNSET else decided
    argv = tuple(config.runner.launcher) + (VITEST_ENTRY, "run") + tail
    workers = grant.slots
    if bounded is not None and bounded.limit is not None:
        workers = min(workers, bounded.limit)
    env = () if bounded is None else worker_env(workers)
    try:
        summary = C.summarize_command(
            C.RunnerKind.VITEST, plan.mode, argv,
            workers=grant.slots,
            provenance=(("vitest-exclusive-command",) if bounded is None
                        else ("vitest-capped-command",)),
        )
    except (TypeError, ValueError) as exc:
        raise _problem(
            "native-config-invalid",
            "literal Vitest argv violates the configured bounds",
        ) from exc
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=env,
        capability=C.Capability(
            execution=(C.ExecutionTier.EXCLUSIVE_COMMAND if bounded is None
                       else C.ExecutionTier.BOUNDED_NATIVE),
            selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message=VITEST_EXCLUSIVE_NOTE if bounded is None else VITEST_CAPPED_NOTE,
            ),),
        ),
        summary=summary,
    )
