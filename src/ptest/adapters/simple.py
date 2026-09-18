"""Preparation-only profiles for Go, Cargo, and explicit literal commands.

This module does not resolve executables, read native runner configuration, or
execute a subprocess.  Task11 owns those native boundaries.  Until then native
profiles remain unavailable for launch even though their ptest-owned bounds are
prepared here.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from ptest import contracts as C


_GO_OWNED = frozenset({"-count", "-p", "-parallel", "-cpu"})
_GO_UNSUPPORTED = frozenset({"-fuzz", "-fuzztime", "-exec"})
_CARGO_OWNED = frozenset({"-j", "--jobs", "--test-threads"})
_CARGO_TARGETS = frozenset({"--lib", "--bin", "--bins", "--test", "--tests"})


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


def _split_option(tokens: tuple[str, ...], index: int) -> tuple[str, str | None, int]:
    """Return option spelling/value/next index without interpreting shell syntax."""
    token = tokens[index]
    name, separator, value = token.partition("=")
    if separator:
        return name, value, index + 1
    if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
        return name, tokens[index + 1], index + 2
    return name, None, index + 1


def _reject_go_controls(tokens: tuple[str, ...]) -> None:
    index = 0
    while index < len(tokens):
        name, _value, next_index = _split_option(tokens, index)
        if name in _GO_OWNED:
            raise _problem("native-config-invalid", "Go concurrency/cache controls are ptest-owned")
        if name in _GO_UNSUPPORTED:
            raise _problem("unsupported-capability", "Go fuzzing or custom executors require an explicit command profile")
        index = next_index


def _go(config: C.Config, plan: C.Plan, grant: C.Grant) -> C.PreparedRun:
    if config.runner.kind is not C.RunnerKind.GO:
        raise _problem("native-config-invalid", "Go preparation requires runner.kind=go")
    if os.environ.get("GOFLAGS") or os.environ.get("GOMAXPROCS"):
        raise _problem("native-config-invalid", "inherited Go flags or GOMAXPROCS are not ptest-owned")
    native = config.runner.args
    if native[:1] == ("test",):
        native = native[1:]
    _reject_go_controls(native + config.runner.full_args)
    if plan.execution == "full":
        native += config.runner.full_args + config.runner.test_roots
    else:
        native += plan.files
    workers = str(grant.slots)
    argv = config.runner.launcher + (
        "test", "-count=1", f"-p={workers}", f"-parallel={workers}", f"-cpu={workers}",
    ) + native
    return _native_prepared(config, plan, grant, argv, ("GOMAXPROCS", workers),
                            (f"go.count=1", f"go.packages={workers}",
                             f"go.parallel={workers}", f"go.cpu={workers}"),
                            "go-native")


def _cargo_target(tokens: tuple[str, ...]) -> None:
    """Require a target that excludes doctests and avoid custom harness modes."""
    index = 0
    target = False
    after_separator = False
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            after_separator = True
            index += 1
            continue
        name, _value, next_index = _split_option(tokens, index)
        if name in _CARGO_OWNED or (after_separator and name == "--test-threads"):
            raise _problem("native-config-invalid", "Cargo concurrency controls are ptest-owned")
        if name in ("--doc", "--bench", "--no-run"):
            raise _problem("unsupported-capability", "Cargo doctest, benchmark, or custom harness modes require an explicit command profile")
        if not after_separator and name in _CARGO_TARGETS:
            target = True
        index = next_index
    if not target:
        raise _problem("unsupported-capability", "Cargo requires an explicit lib, bin, or integration target to exclude doctests")


def _read_toml(path: Path) -> dict:
    """Read one declared Cargo control file; malformed controls fail closed."""
    if path.is_symlink():
        raise _problem("native-config-invalid", "Cargo control files must not be symlinks")
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as source:
            document = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _problem("native-config-invalid", "Cargo control file cannot be safely interpreted") from exc
    if not isinstance(document, dict):
        raise _problem("native-config-invalid", "Cargo control file has an invalid top-level shape")
    return document


def _reject_cargo_project_controls(root: Path) -> None:
    cargo_config = _read_toml(root / ".cargo" / "config.toml")
    build = cargo_config.get("build")
    if isinstance(build, dict) and "jobs" in build:
        raise _problem("native-config-invalid", "Cargo build.jobs is not ptest-owned")
    manifest = _read_toml(root / "Cargo.toml")
    target_tables = [manifest.get(kind) for kind in ("lib", "bin", "test")]
    for table in target_tables:
        entries = table if isinstance(table, list) else [table]
        if any(isinstance(entry, dict) and entry.get("harness") is False for entry in entries):
            raise _problem("unsupported-capability", "Cargo custom harnesses require an explicit command profile")


def _cargo(config: C.Config, plan: C.Plan, grant: C.Grant) -> C.PreparedRun:
    if config.runner.kind is not C.RunnerKind.CARGO:
        raise _problem("native-config-invalid", "Cargo preparation requires runner.kind=cargo")
    if os.environ.get("CARGO_BUILD_JOBS") or os.environ.get("RUST_TEST_THREADS"):
        raise _problem("native-config-invalid", "inherited Cargo concurrency controls are not ptest-owned")
    _reject_cargo_project_controls(_project_root(config))
    native = config.runner.args
    if native[:1] == ("test",):
        native = native[1:]
    if plan.execution == "full":
        native += config.runner.full_args
    else:
        native += plan.files
    _cargo_target(native)
    workers = str(grant.slots)
    argv = config.runner.launcher + ("test", f"-j={workers}") + native + ("--", f"--test-threads={workers}")
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
    if plan.execution != "full":
        raise _problem("unsupported-capability", "literal command profiles cannot claim scoped or selected execution")
    argv = config.runner.launcher + config.runner.args + config.runner.full_args
    return C.PreparedRun(
        argv=argv, cwd=_project_root(config), env_updates=(),
        capability=C.Capability(
            execution=C.ExecutionTier.EXCLUSIVE_COMMAND, selection=False,
            lifecycle="cooperative-process-group",
            limitations=(C.Reason(
                code="unsupported-capability",
                message="exclusive admission does not prove or contain inner command parallelism; Task11 preserves native outcomes",
            ),),
        ),
        summary=C.summarize_command(C.RunnerKind.COMMAND, plan.mode, argv,
                                    workers=grant.slots,
                                    provenance=("literal-exclusive-command",)),
    )


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
