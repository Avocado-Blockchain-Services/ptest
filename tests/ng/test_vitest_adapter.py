"""Unit contracts for the local, exact-version Vitest preparation bridge.

Native CLI fixtures deliberately remain unexecuted until the candidate ptest
executor exists (Task 11).  These tests exercise only preparation and static
bridge safety properties.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.adapters.vitest import prepare


def full_plan(case):
    return C.Plan(mode=C.Mode.FULL, execution="full", files=())


def scoped_plan(case):
    return C.Plan(
        mode=C.Mode.SCOPED,
        execution="scoped",
        files=("tests/alpha.test.ts", "tests/alphabet.test.ts"),
    )


def grant(case, workers: int):
    return C.Grant(
        run_id="12" * 16,
        nonce="34" * 32,
        slots=workers,
        memory_estimate_mb=None,
        reserved_memory_mb=None,
        generation=0,
        domain_id="56" * 16,
    )


def attempt(case, workers: int):
    return C.AttemptIdentity(
        run_id="12" * 16,
        attempt_id="a001",
        resource_prefix="pt_checkout_run_a001_w000",
        worker_count=workers,
    )


def test_vitest_worker_env_is_owned(case):
    prepared = prepare(case.config(runner_kind="vitest", workers=2),
                       full_plan(case), grant(case, 2), attempt(case, 2))
    updates = dict(prepared.env_updates)
    assert updates["VITEST_MAX_FORKS"] == "2"
    assert updates["VITEST_MIN_FORKS"] == "2"
    assert updates["VITEST_MAX_THREADS"] == "2"
    assert updates["VITEST_MIN_THREADS"] == "2"
    assert prepared.summary.workers == 2


def test_vitest_preparation_preserves_literal_native_arguments(case):
    literal = "tests/[a-z] $not-a-shell;*.test.ts"
    config = case.config(
        runner_kind="vitest",
        workers=2,
        runner=C.RunnerConfig(
            kind=C.RunnerKind.VITEST,
            launcher=("node",),
            args=("--reporter", "verbose", literal),
            full_args=("--coverage",),
            test_roots=("tests",),
            workers=2,
        ),
    )
    prepared = prepare(config, full_plan(case), grant(case, 2), attempt(case, 2))
    assert literal in prepared.argv
    assert " ".join(prepared.argv).count(literal) == 1
    assert prepared.argv[0] == "node"
    assert prepared.argv[1].endswith("vitest_bridge.mjs")


def test_vitest_selected_files_remain_distinct_literal_arguments(case):
    prepared = prepare(case.config(runner_kind="vitest", workers=2),
                       scoped_plan(case), grant(case, 2), attempt(case, 2))
    assert prepared.argv[-2:] == ("tests/alpha.test.ts", "tests/alphabet.test.ts")
    assert prepared.capability.execution is C.ExecutionTier.ADVANCED
    assert prepared.capability.selection is True


@pytest.mark.parametrize("configured,granted,attempt_workers", [
    (1, 2, 2), (2, 1, 1), (2, 2, 1),
])
def test_vitest_rejects_any_ungranted_worker_count(case, configured, granted, attempt_workers):
    with pytest.raises(ValueError, match="grant"):
        prepare(case.config(runner_kind="vitest", workers=configured),
                full_plan(case), grant(case, granted), attempt(case, attempt_workers))


def test_vitest_rejects_non_vitest_and_empty_execution(case):
    with pytest.raises(ValueError, match="vitest"):
        prepare(case.config(runner_kind="pytest"), full_plan(case), grant(case, 1), attempt(case, 1))
    with pytest.raises(ValueError, match="execution"):
        prepare(case.config(runner_kind="vitest"),
                C.Plan(mode=C.Mode.FULL, execution="none"), grant(case, 1), attempt(case, 1))


def test_vitest_bridge_is_public_api_only_and_no_shell_execution():
    bridge = Path(__file__).parents[2] / "src" / "ptest" / "runtime" / "vitest_bridge.mjs"
    source = bridge.read_text(encoding="utf-8")
    assert "parseCLI(nativeArgv)" in source
    assert "createVitest('test'" in source
    assert "configureVitest({ vitest })" in source
    assert "runTestSpecifications" in source
    assert "child_process" not in source
    assert "exec(" not in source
    assert ".start(" in source


def test_vitest_bridge_closes_exactly_once_on_failure_and_keeps_tiers_separate():
    bridge = Path(__file__).parents[2] / "src" / "ptest" / "runtime" / "vitest_bridge.mjs"
    source = bridge.read_text(encoding="utf-8")
    assert "finally {\n    await ctx.close()" in source
    assert source.count("createVitest('test'") == 1
    assert "await ctx.init()" in source
    assert "await ctx.runTestSpecifications" in source
    assert "await ctx.start(requestedFiles)" in source
    assert source.index("await ctx.start(requestedFiles)") < source.index("await ctx.init()")
    assert "await ctx.init()\n    await ctx.start" not in source


def test_vitest_bridge_resolves_only_the_project_module_and_rejects_user_worker_controls():
    bridge = Path(__file__).parents[2] / "src" / "ptest" / "runtime" / "vitest_bridge.mjs"
    source = bridge.read_text(encoding="utf-8")
    assert "createRequire(resolve(process.cwd(), 'package.json'))" in source
    assert "projectRequire.resolve('vitest/node')" in source
    assert "loadProjectVitest()" in source
    assert "await loadProjectVitest()" in source
    assert "const requestedFiles = parsed.filter ?? []" in source
    assert "assertNoUserWorkerControls(nativeArgv)" in source
    assert "unsupported-capability" in source


@pytest.mark.parametrize("fixture,version", [
    ("advanced-3.2.7", "3.2.7"),
    ("basic-3.1.4", "3.1.4"),
    ("basic-3.2.6", "3.2.6"),
    ("basic-3.2.7", "3.2.7"),
])
def test_vitest_native_cli_fixture_metadata_is_exact_and_uninstalled(fixture, version):
    root = Path(__file__).parent / "fixtures" / "vitest" / fixture
    package = root / "package.json"
    lock = root / "package-lock.json"
    assert package.is_file() and lock.is_file()
    assert f'"vitest": "{version}"' in package.read_text(encoding="utf-8")
    assert not (root / "node_modules").exists()
