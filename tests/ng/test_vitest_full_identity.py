"""Native full-command proofs fail closed instead of inventing inventory."""
from dataclasses import replace
import importlib
import json
import os

import pytest

from ptest import contracts as C


def module():
    # Missing implementation is an explicit behavior failure during RED.
    spec = importlib.util.find_spec("ptest.vitest_full")
    assert spec is not None, "Vitest full runtime evidence is not implemented"
    return importlib.import_module("ptest.vitest_full")


def installation(case):
    root = case.base
    node = root / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    package = root / "node_modules" / "vitest"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "vitest", "version": "3.2.6"}))
    (package / "vitest.mjs").write_text("// native entry\n")
    dependency = root / "node_modules" / "dependency"
    dependency.mkdir()
    (dependency / "index.js").write_text("export const value = 1\n")
    (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {"node_modules/vitest": {"version": "3.2.6"}}}))
    config = case.config(runner_kind="vitest", config_path=root / ".ptest.toml")
    return replace(config, runner=replace(config.runner, launcher=(str(node),), args=("--maxWorkers=100%", "--fileParallelism")))


def test_native_maximum_workers_command_can_earn_full_evidence(case):
    config = installation(case)
    assert module().full_command_qualified(config, C.RunRequest(mode=C.Mode.FULL))
    assert module().runtime_identity(config) is not None


@pytest.mark.parametrize("args", [("--passWithNoTests",), ("--pass-with-no-tests=false",), ("--testNamePattern", "hello"), ("--test-name-pattern=x",), ("--project=x",), ("--changed",), ("--shard", "1/2"), ("--watch=false",), ("--help",), ("-v",), ("tests/example.test.ts",), ("--unknown-option",), ("--config", "external.ts"), ("--",)])
def test_narrowed_or_unknown_command_cannot_earn_full_evidence(case, args):
    config = installation(case)
    config = replace(config, runner=replace(config.runner, args=args))
    assert not module().full_command_qualified(config, C.RunRequest(mode=C.Mode.FULL))


@pytest.mark.parametrize("run_request", [C.RunRequest(mode=C.Mode.SCOPED), C.RunRequest(mode=C.Mode.FULL, argv=("tests/a.ts",)), C.RunRequest(mode=C.Mode.FULL, setup_only=True), C.RunRequest(mode=C.Mode.FULL, shadow=True)])
def test_non_execution_requests_cannot_earn_full_evidence(case, run_request):
    assert not module().full_command_qualified(installation(case), run_request)


@pytest.mark.parametrize("filename", ["vitest.config.ts", "vite.config.mjs", "vitest.workspace.json"])
def test_unknown_native_config_declines_full_evidence(case, filename):
    config = installation(case)
    (case.base / filename).write_text("export default { test: { passWithNoTests: true } }")
    assert not module().full_command_qualified(config, C.RunRequest(mode=C.Mode.FULL))


@pytest.mark.parametrize("path", ["bin/node", "node_modules/vitest/vitest.mjs", "node_modules/dependency/index.js", "package-lock.json"])
def test_actual_runtime_bytes_invalidate_identity_even_with_same_stat(case, path):
    config = installation(case)
    before = module().runtime_identity(config)
    target = case.base / path
    stamp = target.stat()
    data = target.read_bytes()
    target.write_bytes(data.replace(b"1", b"2", 1) if b"1" in data else data.replace(data[-2:-1], b"X", 1))
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert target.stat().st_size == stamp.st_size
    assert target.stat().st_mtime_ns == stamp.st_mtime_ns
    assert before is not None
    assert module().runtime_identity(config) != before


def test_unsafe_dependency_symlink_declines_identity(case, tmp_path):
    config = installation(case)
    outside = tmp_path / "outside.js"
    outside.write_text("private external bytes")
    (case.base / "node_modules" / "dependency" / "escape.js").symlink_to(outside)
    assert module().runtime_identity(config) is None


def test_runtime_environment_is_bound_without_exposing_values(case, monkeypatch):
    config = installation(case)
    before = module().runtime_identity(config)
    monkeypatch.setenv("NODE_ENV", "private-test-value")
    after = module().runtime_identity(config)
    assert before is not None and after is not None and before != after
    assert "private-test-value" not in after


def test_external_node_injection_declines_identity(case, monkeypatch):
    config = installation(case)
    monkeypatch.setenv("NODE_OPTIONS", "--require /outside/private.js")
    assert module().runtime_identity(config) is None


def full_result():
    snapshot = C.InputSnapshot(digest="11" * 32, compatibility="22" * 32, head="abc", clean=True)
    return C.RunResult(
        run_id="33" * 16, project_id="44" * 16, checkout_id="55" * 16,
        mode=C.Mode.FULL, status=C.Status.PASSED, phase="complete", started_at="2026-10-03T00:00:00Z", finished_at="2026-10-03T00:00:01Z",
        plan=C.Plan(mode=C.Mode.FULL, execution="full", input_digest=snapshot.digest, compatibility=snapshot.compatibility),
        command=C.CommandSummary(kind=C.RunnerKind.VITEST, mode=C.Mode.FULL, argument_count=3),
        runner_exit_code=0, exit_code=0, source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot, runtime_identity="66" * 32, policy_digest="77" * 32,
        attempts=(C.AttemptResult(attempt_id="a001", phase="execution", status=C.Status.PASSED, raw_exit_code=0, final_exit_code=0),),
    )


def test_controller_complete_green_with_no_inventory_can_be_reused():
    assert module().reusable_full_result(full_result())


@pytest.mark.parametrize("updates", [
    {"full_gate_eligible": False}, {"phase": "execution"}, {"runtime_identity": None},
    {"source_valid": False}, {"exit_code": 1}, {"signal": 15}, {"exit_origin": "ptest"},
    {"input_after": None}, {"attempts": ()},
    {"status": C.Status.INCOMPLETE}, {"runner_exit_code": None},
])
def test_incomplete_controller_proof_never_reuses(updates):
    assert not module().reusable_full_result(replace(full_result(), **updates))


def test_failed_earlier_attempt_cannot_be_hidden_by_final_green():
    result = full_result()
    failed = C.AttemptResult(attempt_id="a002", phase="complete", status=C.Status.FAILED, raw_exit_code=1, final_exit_code=1)
    assert not module().reusable_full_result(replace(result, attempts=(failed, *result.attempts)))


def test_changed_input_cannot_be_hidden_by_controller_eligibility():
    result = full_result()
    assert not module().reusable_full_result(replace(result, input_after=replace(result.input_after, digest="88" * 32)))


def test_symlinked_installation_parent_declines_identity(case, tmp_path):
    config = installation(case)
    modules = case.base / "node_modules"
    relocated = tmp_path / "relocated-modules"
    modules.rename(relocated)
    modules.symlink_to(relocated, target_is_directory=True)
    assert module().runtime_identity(config) is None
