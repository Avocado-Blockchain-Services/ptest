"""Preparation-only profiles for Go, Cargo, and explicit literal commands.

This module inspects Cargo control paths, but never resolves executables or
executes a subprocess. Task11 owns launch qualification, same-snapshot config /
environment revalidation, and outcome truth. Native profiles stay unavailable.
"""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from ptest import contracts as C
from ptest.files import read_regular


_GO_OWNED = frozenset({"count", "p", "parallel", "cpu"})
_GO_BOOLEAN = frozenset({"v", "json", "race", "cover", "failfast"})
_GO_VALUE = frozenset({"timeout", "shuffle", "covermode", "coverpkg", "coverprofile"})
_CARGO_OWNED = frozenset({"-j", "--jobs", "--test-threads"})
_CARGO_TARGETS = frozenset({"--lib", "--bin", "--bins", "--test", "--tests"})
_CARGO_BOOLEAN = frozenset({
    "--lib", "--bins", "--tests", "-v", "-vv", "--verbose", "-q", "--quiet",
    "--locked", "--offline", "--frozen", "--no-fail-fast", "--all-features",
    "--no-default-features",
})
_CARGO_VALUE = frozenset({"--bin", "--test", "--features", "--color"})
_LIBTEST_OPTIONS = frozenset({"--nocapture", "--show-output"})


def _problem(code: str, message: str) -> C.Problem:
    return C.Problem(code=code, message=message, phase="execution")


def _project_root(config: C.Config) -> Path:
    if config.checkout is not None:
        return config.checkout.root
    if config.config_path is not None:
        return config.config_path.parent
    raise _problem("native-config-invalid", "runner configuration has no project root")


def _validate_inputs(config: C.Config, plan: C.Plan, grant: C.Grant,
                     attempt: C.AttemptIdentity) -> None:
    if not isinstance(config, C.Config) or not isinstance(plan, C.Plan):
        raise TypeError("prepare requires Config and Plan")
    if not isinstance(grant, C.Grant) or not isinstance(attempt, C.AttemptIdentity):
        raise TypeError("prepare requires Grant and AttemptIdentity")
    if grant.run_id != attempt.run_id or grant.slots != attempt.worker_count:
        raise _problem("admission-invalid", "attempt does not match its admission grant")
    if config.runner.workers != grant.slots:
        raise _problem("admission-invalid", "configured workers must equal the admission grant")
    if plan.execution not in ("full", "scoped", "selected"):
        raise _problem("native-config-invalid", "adapter cannot prepare an empty execution plan")


def requires_exclusive(config: C.Config) -> bool:
    """Task11 must set AdmissionRequest.exclusive to this BEFORE admission.

    Grant has no exclusivity field: prepare cannot prove the scheduler made an
    exclusive reservation. This is a required caller precondition, not evidence
    of enforcement or containment of the command's own child processes.
    """
    if not isinstance(config, C.Config):
        raise TypeError("requires_exclusive requires Config")
    return config.runner.kind is C.RunnerKind.COMMAND


def _native_context(config: C.Config, plan: C.Plan) -> None:
    if plan.execution != "full" or plan.files:
        raise _problem("unsupported-capability", "native file-to-package/target mapping is not qualified; use a full plan")
    if len(config.runner.launcher) != 1:
        raise _problem("unsupported-capability", "native launcher prefixes/wrappers require an explicit command profile")


def _value_end(tokens: tuple[str, ...], index: int) -> int:
    """Consume one known value option, never guess the arity of other flags."""
    _name, separator, value = tokens[index].partition("=")
    if separator:
        end = index + 1
    else:
        value = tokens[index + 1] if index + 1 < len(tokens) else ""
        end = index + 2
    if not value or value.startswith("-"):
        raise _problem("native-config-invalid", "native value option requires a non-option value")
    return end


def _go_options(tokens: tuple[str, ...]) -> None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        spelling, separator, value = token.partition("=")
        # Go accepts one/two dashes and forwards test.* aliases. An allowlist
        # also closes unknown/future aliases, -args and option terminators.
        name = spelling.removeprefix("--") if spelling.startswith("--") else spelling.removeprefix("-")
        name = name.removeprefix("test.")
        if name in _GO_OWNED:
            raise _problem("native-config-invalid", "Go concurrency/cache controls are ptest-owned")
        if not token.startswith("-"):
            raise _problem("unsupported-capability", "Go packages must be declared in test_roots")
        if name in _GO_BOOLEAN:
            if separator and value not in ("true", "false"):
                raise _problem("native-config-invalid", "Go boolean option requires true or false")
            index += 1
        elif name in _GO_VALUE:
            index = _value_end(tokens, index)
        else:
            raise _problem("unsupported-capability", "Go option is not qualified for a full test gate; use an explicit command profile")


def _go_roots(roots: tuple[str, ...]) -> None:
    for root in roots:
        # Accept only local package directories/patterns. Source-file lists,
        # imports, arbitrary positional modes and flag-looking roots need a
        # separately qualified mapping; none may become forwarded options.
        if root in (".", "./..."):
            continue
        directory = root[2:] if root.startswith("./") else ""
        if directory.endswith("/..."):
            directory = directory[:-4]
        if (not directory or any(part in ("", ".", "..") for part in directory.split("/"))
                or root.endswith(".go") or any(char in root for char in "\\\x00\n\r\t")):
            raise _problem("unsupported-capability", "Go test_roots require local package directories or ./... patterns")


def _go(config: C.Config, plan: C.Plan, grant: C.Grant) -> C.PreparedRun:
    if config.runner.kind is not C.RunnerKind.GO:
        raise _problem("native-config-invalid", "Go preparation requires runner.kind=go")
    _native_context(config, plan)
    if os.environ.get("GOFLAGS") or os.environ.get("GOMAXPROCS"):
        raise _problem("native-config-invalid", "inherited Go flags or GOMAXPROCS are not ptest-owned")
    native = config.runner.args
    if native[:1] == ("test",):
        native = native[1:]
    native += config.runner.full_args
    _go_options(native)
    _go_roots(config.runner.test_roots)
    native += config.runner.test_roots
    workers = str(grant.slots)
    argv = config.runner.launcher + (
        "test", "-count=1", "-p=1", f"-parallel={workers}", f"-cpu={workers}",
    ) + native
    return _native_prepared(config, plan, grant, argv, ("GOMAXPROCS", workers),
                            ("go.count=1", "go.packages=1",
                             f"go.parallel={workers}", f"go.cpu={workers}"),
                            "go-native")


def _cargo_options(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Keep Cargo and libtest parsing separate and allow only full-test modes."""
    split = tokens.index("--") if "--" in tokens else len(tokens)
    cargo, libtest = tokens[:split], tokens[split + 1:]
    for token in tokens:
        if token.partition("=")[0] in _CARGO_OWNED or token.startswith(("-j", "--jobs", "--test-threads")):
            raise _problem("native-config-invalid", "Cargo concurrency controls are ptest-owned")
    if any(token not in _LIBTEST_OPTIONS for token in libtest):
        raise _problem("unsupported-capability", "libtest passthrough permits only qualified display flags, not filters or controls")
    index = 0
    target = False
    while index < len(cargo):
        token = cargo[index]
        name = token.partition("=")[0]
        if token in _CARGO_BOOLEAN:
            index += 1
        elif name in _CARGO_VALUE:
            index = _value_end(cargo, index)
        else:
            raise _problem("unsupported-capability", "Cargo option or positional is not qualified; use an explicit command profile")
        if name in _CARGO_TARGETS:
            target = True
    if not target:
        raise _problem("unsupported-capability", "Cargo requires an explicit lib, bin, or integration target to exclude doctests")
    return cargo, libtest


def _existing(path: Path) -> os.stat_result | None:
    """Only ENOENT establishes absence; permissions and unknown types fail shut."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _problem("native-config-invalid", "Cargo control path discovery is uncertain") from exc


def _check_directory_path(path: Path, *, required: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise _problem("native-config-invalid", "Cargo discovery requires absolute, unambiguous directories")
    for component in reversed((path, *path.parents)):
        stamp = _existing(component)
        if stamp is None and not required:
            return
        if stamp is None or not stat.S_ISDIR(stamp.st_mode):
            raise _problem("native-config-invalid", "Cargo control directories must exist without symlinks or special files")


def _cargo_manifest(root: Path) -> dict:
    stamp = _existing(root / "Cargo.toml")
    if stamp is None or not stat.S_ISREG(stamp.st_mode) or stamp.st_size > C.INPUT_FILE_MAX_BYTES:
        raise _problem("native-config-invalid", "Cargo requires a bounded regular root manifest")
    try:
        raw = read_regular(root, "Cargo.toml", C.INPUT_FILE_MAX_BYTES + 1)
        if len(raw) > C.INPUT_FILE_MAX_BYTES:
            raise ValueError("oversized manifest")
        return tomllib.loads(raw.decode("utf-8"))
    except (C.Problem, OSError, ValueError, RecursionError) as exc:
        raise _problem("native-config-invalid", "Cargo manifest cannot be safely interpreted") from exc


def _reject_cargo_project_controls(root: Path) -> None:
    _check_directory_path(root, required=True)
    try:
        cargo_home = Path(os.environ["CARGO_HOME"]) if "CARGO_HOME" in os.environ else Path.home() / ".cargo"
    except (KeyError, RuntimeError) as exc:
        raise _problem("native-config-invalid", "Cargo home discovery is uncertain") from exc
    # Cargo merges both historical/current names in cwd/ancestors and CARGO_HOME.
    # Refuse the entire config surface (including include/env/runner/wrapper
    # indirection) until native discovery is qualified. Never follow symlinks or
    # open these potentially secret-bearing files merely to reject them.
    directories = {cargo_home, *(parent / ".cargo" for parent in (root, *root.parents))}
    for directory in directories:
        _check_directory_path(directory)
        if any(_existing(directory / name) is not None for name in ("config", "config.toml")):
            raise _problem("native-config-invalid", "Cargo discovered configuration is unqualified; use an explicit command profile")
    manifest = _cargo_manifest(root)
    package = manifest.get("package")
    if "workspace" in manifest or (isinstance(package, dict) and "workspace" in package):
        raise _problem("unsupported-capability", "Cargo workspace/member discovery requires an explicit command profile")
    if not isinstance(package, dict):
        raise _problem("native-config-invalid", "Cargo requires a standalone package manifest")
    if any(_existing(parent / "Cargo.toml") is not None for parent in root.parents):
        raise _problem("unsupported-capability", "Cargo ancestor manifests require workspace/member discovery")
    for kind in ("lib", "bin", "test", "example", "bench"):
        table = manifest.get(kind)
        if table is None:
            continue
        if not isinstance(table, dict if kind == "lib" else list):
            raise _problem("native-config-invalid", "Cargo target declarations have an unqualified shape")
        entries = table if isinstance(table, list) else [table]
        for entry in entries:
            if not isinstance(entry, dict):
                raise _problem("native-config-invalid", "Cargo target declarations have an unqualified shape")
            if entry.get("harness", True) is not True:
                raise _problem("unsupported-capability", "Cargo custom harnesses require an explicit command profile")
            if kind in ("example", "bench") and entry.get("test", False) is not False:
                raise _problem("unsupported-capability", "Cargo examples/benchmarks opted into tests require an explicit command profile")


def _cargo(config: C.Config, plan: C.Plan, grant: C.Grant) -> C.PreparedRun:
    if config.runner.kind is not C.RunnerKind.CARGO:
        raise _problem("native-config-invalid", "Cargo preparation requires runner.kind=cargo")
    _native_context(config, plan)
    if any(name != "CARGO_HOME" and name.startswith(("CARGO_", "RUST")) for name in os.environ):
        raise _problem("native-config-invalid", "inherited Cargo/Rust execution controls are unqualified")
    native = config.runner.args
    if native[:1] == ("test",):
        native = native[1:]
    cargo, libtest = _cargo_options(native + config.runner.full_args)
    _reject_cargo_project_controls(_project_root(config))
    workers = str(grant.slots)
    argv = config.runner.launcher + ("test", f"-j={workers}") + cargo + ("--", f"--test-threads={workers}") + libtest
    return _native_prepared(config, plan, grant, argv, (),
                            (f"cargo.jobs={workers}", f"libtest.threads={workers}"),
                            "cargo-native")


def _native_prepared(config: C.Config, plan: C.Plan, grant: C.Grant,
                     argv: tuple[str, ...], update: tuple[str, str], generated: tuple[str, ...],
                     provenance: str) -> C.PreparedRun:
    updates = (update,) if update else ()
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=updates,
        capability=C.Capability(
            execution=C.ExecutionTier.UNAVAILABLE, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message="native profile execution, exit/signal truth, and fixture acceptance are deferred to Task11",
            ),),
        ),
        summary=C.summarize_command(config.runner.kind, plan.mode, argv,
                                    generated_options=generated, workers=grant.slots,
                                    provenance=(provenance,)),
    )


def _command(config: C.Config, plan: C.Plan, grant: C.Grant) -> C.PreparedRun:
    if config.runner.kind is not C.RunnerKind.COMMAND:
        raise _problem("native-config-invalid", "command preparation requires runner.kind=command")
    if plan.execution not in ("full", "scoped"):
        if plan.execution == "selected":
            raise _problem(
                "unsupported-capability",
                "literal command profiles do not support selected execution",
            )
        raise _problem("native-config-invalid", "literal command plan is not executable")
    if plan.files:
        # A generic command has no inventory or file selector contract.  In
        # particular, caller argv must stay in the ephemeral runner config;
        # Plan.files is a public selection field, not an argv side channel.
        raise _problem(
            "unsupported-capability",
            "literal command profiles do not accept public plan files",
        )
    if plan.execution == "scoped" and plan.mode is not C.Mode.SCOPED:
        raise _problem(
            "native-config-invalid",
            "scoped command execution requires an explicit scoped plan",
        )

    try:
        # RunnerConfig validates each configured argv sequence.  PreparedRun
        # is the shared final bound for the combined child argv (including
        # token count, per-token UTF-8 size, aggregate UTF-8 size and NULs).
        # Keep these tuples untouched: no shell string is ever formed or
        # reparsed, and scoped execution deliberately excludes full_args.
        common = tuple(config.runner.launcher) + tuple(config.runner.args)
        argv = (common if plan.execution == "scoped"
                else common + tuple(config.runner.full_args))
        summary = C.summarize_command(
            C.RunnerKind.COMMAND, plan.mode, argv,
            workers=grant.slots,
            provenance=("literal-exclusive-command",),
        )
        return C.PreparedRun(
            argv=argv, cwd=_project_root(config), env_updates=(),
            capability=C.Capability(
                execution=C.ExecutionTier.EXCLUSIVE_COMMAND, selection=False,
                lifecycle="cooperative-process-group",
                limitations=(C.Reason(
                    code="unsupported-capability",
                    message="Task11 must set AdmissionRequest.exclusive=True using requires_exclusive(config) before admission; exclusive admission does not prove or contain inner command parallelism; Task11 preserves native outcomes",
                ),),
            ),
            summary=summary,
        )
    except (TypeError, ValueError) as exc:
        # Do not leak malformed token values through adapter errors.  The
        # contracts own the exact limits; this layer maps their construction
        # failures to the stable adapter problem code.
        raise _problem(
            "native-config-invalid",
            "literal command argv violates the configured bounds",
        ) from exc


def prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
            attempt: C.AttemptIdentity) -> C.PreparedRun:
    """Prepare a non-executing bounded native or literal exclusive profile."""
    _validate_inputs(config, plan, grant, attempt)
    if config.runner.kind is C.RunnerKind.GO:
        return _go(config, plan, grant)
    if config.runner.kind is C.RunnerKind.CARGO:
        return _cargo(config, plan, grant)
    if config.runner.kind is C.RunnerKind.COMMAND:
        return _command(config, plan, grant)
    raise _problem("native-config-invalid", "simple adapter supports only Go, Cargo, or command profiles")
