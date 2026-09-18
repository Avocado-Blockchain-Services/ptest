"""Prepared-but-unavailable Vitest ``basic_serial`` foundation contracts.

The bridge cases use ``stub-node.mjs`` as a local API double. They test ptest
boundaries; they do not qualify a real Vitest version or subprocess tuple.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from ptest import contracts as C
from ptest.adapters.vitest import prepare

RUN_ID = "12" * 16
NONCE = "34" * 32


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(run_id=RUN_ID, nonce=NONCE, slots=slots, memory_estimate_mb=None,
                   reserved_memory_mb=None, generation=0, domain_id="56" * 16)


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(run_id=RUN_ID, attempt_id="a001",
                             resource_prefix="pt_checkout_run_a001_w000", worker_count=workers)


def _scoped() -> C.Plan:
    return C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("tests/one.test.ts",))


def test_prepare_only_admits_scoped_vitest_and_ignores_requested_workers(case):
    config = case.config(runner_kind="vitest", workers=64, config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=("node",)))
    prepared = prepare(config, _scoped(), _grant(), _attempt())
    assert prepared.capability.execution is C.ExecutionTier.UNAVAILABLE
    assert prepared.summary.workers == 1
    updates = dict(prepared.env_updates)
    assert {updates[n] for n in ("VITEST_MIN_THREADS", "VITEST_MAX_THREADS", "VITEST_MIN_FORKS", "VITEST_MAX_FORKS")} == {"1"}
    assert updates["PTEST_VITEST_EXECUTION"] == "scoped"
    assert updates["PTEST_VITEST_PROFILE"] == "basic_serial"


@pytest.mark.parametrize("mode,execution", [(C.Mode.FULL, "full"), (C.Mode.AUTOMATIC, "scoped"), (C.Mode.FULL, "selected"), (C.Mode.FULL, "none"), (C.Mode.SHADOW, "scoped"), (C.Mode.PROBE, "scoped")])
def test_prepare_refuses_every_non_scoped_mode_before_node_evaluation(case, mode, execution):
    with pytest.raises(C.Problem, match="scoped"):
        prepare(case.config(runner_kind="vitest"), C.Plan(mode=mode, execution=execution, files=()), _grant(), _attempt())


def test_prepare_refuses_non_vitest_and_grant_attempt_mismatch(case):
    with pytest.raises(C.Problem, match="vitest"):
        prepare(case.config(runner_kind="pytest"), _scoped(), _grant(), _attempt())
    with pytest.raises(C.Problem, match="admission"):
        prepare(case.config(runner_kind="vitest"), _scoped(), _grant(2), _attempt())


def test_prepare_refuses_empty_scoped_suffix(case):
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=("node",)))
    with pytest.raises(C.Problem, match="file"):
        prepare(config,
                C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=()), _grant(), _attempt())


@pytest.mark.parametrize("launcher", [("npm",), ("npx", "vitest"), ("yarn", "vitest"), ("bun",), ("node", "--inspect"), ("/tmp/not-node",), ("/usr/bin/node", "--trace-warnings")])
def test_prepare_refuses_non_node_or_flagged_launcher(case, launcher):
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=launcher))
    with pytest.raises(C.Problem, match="Node"):
        prepare(config, _scoped(), _grant(), _attempt())


def test_prepare_keeps_literal_configured_args_and_scoped_suffix_once(case):
    literal = "tests/[a-z] $not-a-shell;*.test.ts"
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=("node",), args=("--reporter", "verbose", literal), full_args=("--coverage",)))
    prepared = prepare(config, _scoped(), _grant(), _attempt())
    assert prepared.argv[:3] == ("node", str(Path(prepared.argv[1])), "--")
    assert prepared.argv[3:] == ("--reporter", "verbose", literal, "tests/one.test.ts")
    assert "--coverage" not in prepared.argv


@pytest.fixture
def bridge_run(case, tmp_path):
    project = tmp_path / "project"
    package = project / "node_modules" / "vitest"
    package.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parent / "fixtures" / "vitest" / "stub-node.mjs", package / "node.mjs")
    (package / "package.json").write_text(json.dumps({"name": "vitest", "version": "3.2.7", "type": "module", "exports": {"./node": "./node.mjs", "./package.json": "./package.json"}}))
    reports = tmp_path / "reports"
    reports.mkdir(mode=0o700)

    def run(*, args=(), scenario=None, report=True, env_updates=None, report_name=None, parent_mode=None):
        config = case.config(runner_kind="vitest", config_path=project / "ptest.toml")
        config = replace(config, runner=replace(config.runner, launcher=("node",), args=tuple(args)))
        prepared = prepare(config, _scoped(), _grant(), _attempt())
        path = reports / (report_name or "native-a001-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json")
        if parent_mode is not None:
            reports.chmod(parent_mode)
        env = {key: value for key, value in os.environ.items() if key not in {"NODE_OPTIONS", "NODE_PATH"}}
        env.update(prepared.env_updates)
        env.update(PTEST_STUB_CASE=json.dumps(scenario or {}), PTEST_STUB_TRACE=str(tmp_path / "trace.jsonl"))
        if report:
            env["PTEST_VITEST_REPORT_PATH"] = str(path)
        if env_updates:
            env.update(env_updates)
        result = subprocess.run(prepared.argv, cwd=project, env=env, capture_output=True, text=True, timeout=3)
        trace = tmp_path / "trace.jsonl"
        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        terminal = json.loads(path.read_text()) if path.exists() else None
        return result, events, terminal, path
    return run


def test_bridge_writes_only_native_terminal_shape_for_scoped_serial(bridge_run):
    result, events, terminal, path = bridge_run(args=("--reporter", "verbose", "tests/[a-z] $literal"))
    assert result.returncode == 0, result.stderr
    assert events[0] == {"event": "parse", "argv": ["vitest", "run", "--reporter", "verbose", "tests/[a-z] $literal", "tests/one.test.ts"]}
    assert set(terminal) == {"protocol", "run_id", "nonce", "attempt_id", "runner", "observed_runtime_version", "execution_mode", "effective_profile", "terminal_complete", "native_exit_code", "bridge_exit_code", "problem"}
    assert terminal["runner"] == "vitest" and terminal["execution_mode"] == "scoped"
    assert terminal["effective_profile"] == "basic_serial" and terminal["terminal_complete"] is True
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("args,scenario", [(("--max-workers=9",), {}), (("--pool=threads",), {}), (("--watch",), {}), (("--browser",), {}), (("--project", "other"), {}), (("--typecheck",), {}), ((), {"root": {"workspace": "workspace.mjs"}}), ((), {"root": {"pool": "threads"}}), ((), {"root": {"browser": {"enabled": True}}}), ((), {"root": {"api": {"middlewareMode": True, "port": 51204}}}), ((), {"root": {"runner": "custom-runner"}}), ((), {"root": {"sequencer": "custom-sequencer"}}), ((), {"foreignHook": "foreign"})])
def test_bridge_refuses_unowned_execution_controls_before_test_execution(bridge_run, args, scenario):
    result, events, terminal, _ = bridge_run(args=args, scenario=scenario)
    assert result.returncode != 0
    assert not any(event["event"] in {"run", "start"} for event in events)
    assert terminal["terminal_complete"] is False and terminal["problem"] == "bridge-refused"


@pytest.mark.parametrize("name", ["NODE_OPTIONS", "NODE_PATH"])
def test_bridge_refuses_unowned_node_environment_without_loading_project(bridge_run, name):
    result, events, terminal, _ = bridge_run(env_updates={name: "--require=evil"})
    assert result.returncode != 0 and not events
    # NODE_OPTIONS is acted on by Node before bridge code can allocate evidence.
    if name == "NODE_OPTIONS":
        assert terminal is None
    else:
        assert terminal["problem"] == "bridge-refused"


def test_bridge_fails_closed_when_executor_did_not_allocate_report(bridge_run):
    result, events, terminal, _ = bridge_run(report=False)
    assert result.returncode != 0 and not events and terminal is None


@pytest.mark.parametrize("report_name,parent_mode", [
    ("terminal.json", None), ("native-a001-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json", 0o755),
])
def test_bridge_refuses_nonbinding_or_nonprivate_report_target(bridge_run, report_name, parent_mode):
    result, events, terminal, _ = bridge_run(report_name=report_name, parent_mode=parent_mode)
    assert result.returncode != 0 and not events and terminal is None


def test_bridge_preserves_native_failure_as_complete_nonzero_terminal(bridge_run):
    result, _, terminal, _ = bridge_run(scenario={"failed": True})
    assert result.returncode == 1
    assert terminal["terminal_complete"] is True
    assert terminal["native_exit_code"] == terminal["bridge_exit_code"] == 1
    assert terminal["problem"] == "native-failure"
