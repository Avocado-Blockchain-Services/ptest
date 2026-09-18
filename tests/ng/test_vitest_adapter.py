"""Unit contracts for the local, exact-version Vitest preparation bridge.

Native CLI fixtures remain unexecuted until Task 11. The Node subprocesses
below execute our bridge against a hand-written local API double, never Vitest.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import os
import shutil
import subprocess

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
    assert prepared.capability.execution is C.ExecutionTier.UNAVAILABLE
    assert prepared.capability.selection is False
    assert prepared.capability.limitations
    assert dict(prepared.env_updates)["PTEST_VITEST_EXECUTION"] == "scoped"


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


@pytest.fixture
def bridge_run(case, tmp_path):
    """Bind the executor-owned report seam in a private temporary directory."""
    project = tmp_path / "project"
    package = project / "node_modules" / "vitest"
    package.mkdir(parents=True)
    stub = Path(__file__).parent / "fixtures" / "vitest" / "stub-node.mjs"
    shutil.copyfile(stub, package / "node.mjs")
    reports = tmp_path / "reports"
    reports.mkdir(mode=0o700)
    counter = 0

    def run(*, args=(), execution="full", workers=1, version="3.2.7",
            scenario=None, report_kind="valid", coverage_version=None, env_updates=None):
        nonlocal counter
        counter += 1
        (package / "package.json").write_text(json.dumps({
            "name": "vitest", "version": version, "type": "module",
            "exports": {"./node": "./node.mjs", "./package.json": "./package.json"},
        }))
        if coverage_version is not None:
            coverage = project / "node_modules" / "@vitest" / "coverage-v8"
            coverage.mkdir(parents=True, exist_ok=True)
            (coverage / "package.json").write_text(json.dumps({"version": coverage_version}))
        config = case.config(runner_kind="vitest", workers=workers)
        config = replace(config, runner=replace(config.runner, launcher=("node",), args=tuple(args)))
        plan = C.Plan(mode=C.Mode.FULL if execution == "full" else C.Mode.SCOPED,
                      execution=execution, files=())
        prepared = prepare(config, plan, grant(case, workers), attempt(case, workers))
        report = reports / f"{counter}.jsonl"
        trace = tmp_path / f"trace-{counter}.jsonl"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PTEST_", "VITEST_"))}
        env.update(prepared.env_updates)
        # This is the Task 11 binding seam: private allocation is not prepare's job.
        prepared = replace(prepared, report_path=report)
        env.update(PTEST_VITEST_REPORT_PATH=str(prepared.report_path),
                   PTEST_STUB_TRACE=str(trace), PTEST_STUB_CASE=json.dumps(scenario or {}))
        if report_kind == "missing":
            env.pop("PTEST_VITEST_REPORT_PATH")
        elif report_kind == "existing":
            report.write_text("user file")
        elif report_kind == "symlink":
            report.symlink_to(tmp_path / "target")
        elif report_kind == "public_parent":
            reports.chmod(0o755)
        if env_updates:
            env.update(env_updates)
        result = subprocess.run(prepared.argv, cwd=project, env=env,
                                capture_output=True, text=True, timeout=3)
        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        terminal = None
        if report.is_file() and report_kind not in {"existing", "symlink"}:
            terminal = json.loads(report.read_text())
            assert report.stat().st_mode & 0o777 == 0o600
            assert report.stat().st_size < 65536
        return result, events, terminal, report
    return run


def test_bridge_parse_cli_preserves_every_literal_token(bridge_run):
    tokens = ("--reporter", "verbose", "tests/[a-z] $literal;*.test.mjs")
    result, events, _, _ = bridge_run(args=tokens, execution="scoped")
    assert result.returncode == 0, result.stderr
    assert events[0] == {"event": "parse", "argv": ["vitest", "run", *tokens]}


def test_bridge_terminal_is_private_protocol_bound_and_after_close(bridge_run):
    result, events, terminal, _ = bridge_run()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "native reporter output\n"
    assert result.stderr == "native reporter stderr\n"
    assert terminal == {
        "protocol": 1, "run_id": "12" * 16, "nonce": "34" * 32,
        "event": "terminal", "attempt_id": "a001", "execution": "full",
        "profile": "advanced", "selection": False, "baseline_eligible": False,
        "complete": True, "status": "passed", "exit_code": 0, "files": 1,
        "all_tests_run": True,
    }
    names = [e["event"] for e in events]
    assert names.count("create") == names.count("close") == 1
    assert names.index("glob") < names.index("init") < names.index("run") < names.index("close")
    assert next(e for e in events if e["event"] == "reporters")["userRetained"] is True


@pytest.mark.parametrize("version", ["3.1.4", "3.2.6"])
def test_bridge_basic_serial_never_accepts_selected_execution(bridge_run, version):
    result, events, terminal, _ = bridge_run(version=version, execution="selected")
    assert result.returncode != 0
    assert not any(e["event"] in {"init", "run", "start"} for e in events)
    assert terminal["status"] == "incomplete"


def test_bridge_basic_serial_uses_native_filters_without_prior_init(bridge_run):
    result, events, terminal, _ = bridge_run(version="3.2.6", execution="scoped",
                                            scenario={"filters": ["alpha"]})
    assert result.returncode == 0, result.stderr
    names = [e["event"] for e in events]
    assert names.index("start") < names.index("init")
    assert "glob" not in names and "run" not in names
    assert terminal["all_tests_run"] is False
    assert terminal["profile"] == "basic_serial"
    assert terminal["baseline_eligible"] is False


@pytest.mark.parametrize("target", ["root", "project"])
@pytest.mark.parametrize("unsafe", [
    {"pool": "threads"}, {"poolMatchGlobs": [["**", "./custom-pool.mjs"]]},
    {"typecheck": {"enabled": True}}, {"browser": {"enabled": True}},
    {"watch": True}, {"api": {"port": 1234}}, {"workspace": "workspace.mjs"},
    {"projects": ["other"]}, {"maxWorkers": 8}, {"minWorkers": 8},
    {"maxConcurrency": 8}, {"poolOptions": {"forks": {"maxForks": 8, "minForks": 8}}},
])
def test_bridge_rejects_unsafe_effective_root_and_project_before_tests(bridge_run, target, unsafe):
    result, events, terminal, _ = bridge_run(scenario={target: unsafe})
    assert result.returncode != 0
    assert not any(e["event"] in {"init", "run", "start", "glob"} for e in events)
    assert terminal["status"] == "incomplete"


@pytest.mark.parametrize("name", ["other-plugin", "ptest-vitest-reporter"])
def test_bridge_rejects_foreign_configure_hook_even_if_name_spoofed(bridge_run, name):
    result, events, _, _ = bridge_run(scenario={"foreignHook": name})
    assert result.returncode != 0
    assert not any(e["event"] in {"foreign-hook", "run", "start"} for e in events)


@pytest.mark.parametrize("removed", ["removeReporter", "removeInstantiated", "removeAfterRun"])
def test_bridge_rejects_lost_owned_reporter(bridge_run, removed):
    result, _, terminal, _ = bridge_run(scenario={removed: True})
    assert result.returncode != 0
    assert terminal["complete"] is False


@pytest.mark.parametrize("options", [
    {"testNamePattern": "one"}, {"shard": "1/2"}, {"changed": True},
    {"related": ["src/a.js"]}, {"project": ["one"]}, {"bail": 1},
    {"fileParallelism": False, "maxWorkers": 7},
])
@pytest.mark.parametrize("source", ["options", "root"])
def test_bridge_rejects_full_narrowing_from_cli_or_config(bridge_run, options, source):
    result, events, _, _ = bridge_run(scenario={source: options})
    assert result.returncode != 0
    assert not any(e["event"] in {"run", "start"} for e in events)


def test_bridge_rejects_full_positional_filter(bridge_run):
    result, events, _, _ = bridge_run(scenario={"filters": ["alpha"]})
    assert result.returncode != 0
    assert not any(e["event"] == "run" for e in events)


def test_bridge_scoped_without_filters_does_not_claim_full_inventory(bridge_run):
    result, events, terminal, _ = bridge_run(execution="scoped")
    assert result.returncode == 0, result.stderr
    assert next(e for e in events if e["event"] == "run")["allTestsRun"] is False
    assert terminal["all_tests_run"] is False


@pytest.mark.parametrize("token", ["--max-workers=9", "--maxConcurrency=9",
                                    "--pool-options.threads.max-threads=8", "--no-file-parallelism"])
def test_bridge_rejects_normalized_owned_control_spellings(bridge_run, token):
    result, events, _, _ = bridge_run(args=(token,))
    assert result.returncode != 0
    assert not any(e["event"] == "create" for e in events)


@pytest.mark.parametrize("scenario,code,status,complete", [
    ({"failed": True}, 1, "failed", True),
    ({"unhandled": True}, 1, "failed", True),
    ({"exitCode": 9}, 9, "failed", True),
    ({"runError": True}, 70, "incomplete", False),
    ({"closeError": True}, 70, "incomplete", False),
    ({"closeHang": True}, 70, "incomplete", False),
    ({"keepAlive": True}, 70, "incomplete", False),
    ({"exitCode": 9, "closeError": True}, 9, "failed", False),
])
def test_bridge_terminal_preserves_failure_and_close_outcomes(bridge_run, scenario, code, status, complete):
    result, events, terminal, _ = bridge_run(scenario=scenario)
    assert result.returncode == code, result.stderr
    assert terminal["status"] == status
    assert terminal["complete"] is complete
    assert terminal["exit_code"] == code
    assert [e["event"] for e in events].count("close") == 1


@pytest.mark.parametrize("kind", ["missing", "existing", "symlink", "public_parent"])
def test_bridge_requires_private_exclusive_report_without_overwrite(bridge_run, kind):
    result, events, _, report = bridge_run(report_kind=kind)
    assert result.returncode != 0
    assert not events
    if kind == "existing":
        assert report.read_text() == "user file"


@pytest.mark.parametrize("version", [None, "3.2.6"])
def test_bridge_rejects_missing_or_mismatched_coverage_v8(bridge_run, version):
    result, events, _, _ = bridge_run(coverage_version=version,
        scenario={"root": {"coverage": {"enabled": True, "provider": "v8"}}})
    assert result.returncode != 0
    assert not any(e["event"] in {"init", "run", "start"} for e in events)


def test_bridge_preserves_matching_coverage_and_late_threshold_failure(bridge_run):
    result, _, terminal, _ = bridge_run(coverage_version="3.2.7", scenario={
        "root": {"coverage": {"enabled": True, "provider": "v8"}}, "exitCode": 1,
    })
    assert result.returncode == 1
    assert terminal["status"] == "failed"


def test_bridge_unfinished_runner_cannot_publish_success_on_event_loop_drain(bridge_run):
    result, _, terminal, _ = bridge_run(scenario={"runHang": True})
    assert result.returncode == 70
    assert terminal["status"] == "incomplete"
    assert terminal["complete"] is False


def test_bridge_empty_full_inventory_never_claims_a_passing_gate(bridge_run):
    result, events, terminal, _ = bridge_run(scenario={"specs": []})
    assert result.returncode != 0
    assert terminal["status"] == "incomplete"
    assert not any(e["event"] in {"init", "run"} for e in events)


def test_bridge_native_exit_70_remains_failed_when_close_also_fails(bridge_run):
    result, _, terminal, _ = bridge_run(scenario={"exitCode": 70, "closeError": True})
    assert result.returncode == 70
    assert terminal["status"] == "failed"
    assert terminal["complete"] is False


def test_bridge_selected_modules_match_exactly_and_never_publish_baseline(bridge_run):
    result, events, terminal, report = bridge_run(execution="selected", workers=2,
                                               scenario={"filters": ["tests/alpha.test.mjs"]})
    assert result.returncode == 0, result.stderr
    run = next(e for e in events if e["event"] == "run")
    assert run["specs"] == [str(report.parent.parent / "project" / "tests" / "alpha.test.mjs")]
    assert run["allTestsRun"] is False
    assert terminal["baseline_eligible"] is False
    assert terminal["selection"] is False


@pytest.mark.parametrize("scenario", [
    {"filters": ["alpha"]},
    {"filters": ["tests/missing.test.mjs"]},
    {"filters": ["tests/alpha.test.mjs"], "specs": ["tests/alpha.test.mjs", "tests/alpha.test.mjs"]},
    {"filters": ["tests/alpha.test.mjs"], "specPool": "threads"},
])
def test_bridge_selected_module_uncertainty_never_runs_tests(bridge_run, scenario):
    result, events, _, _ = bridge_run(execution="selected", workers=2, scenario=scenario)
    assert result.returncode != 0
    assert not any(e["event"] in {"init", "run"} for e in events)


@pytest.mark.parametrize("version,workers", [("3.2.5", 1), ("3.2.8", 1), ("3.2.6", 2)])
def test_bridge_rejects_unqualified_version_and_worker_tuples(bridge_run, version, workers):
    result, events, _, _ = bridge_run(version=version, workers=workers)
    assert result.returncode != 0
    assert not any(e["event"] == "create" for e in events)


@pytest.mark.parametrize("variable,value", [
    ("PTEST_RUN_ID", "bad"), ("PTEST_GRANT_NONCE", "bad"),
    ("PTEST_VITEST_ATTEMPT", "a011"), ("PTEST_VITEST_EXECUTION", "none"),
    ("PTEST_VITEST_WORKERS", "65"), ("VITEST_MAX_FORKS", "8"),
])
def test_bridge_invalid_identity_or_worker_environment_fails_before_module_loading(bridge_run, variable, value):
    result, events, _, _ = bridge_run(env_updates={variable: value})
    assert result.returncode != 0
    assert not events


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
