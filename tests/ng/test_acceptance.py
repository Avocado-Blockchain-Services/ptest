"""Task14 acceptance/benchmark harness contracts.

These tests exercise only bounded harness logic and miniature temporary
fixtures.  They are not adoption runs and never collect a child test suite.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load("ptest_task14_benchmark", ROOT / "scripts" / "benchmark.py")
acceptance = _load("ptest_task14_acceptance", ROOT / "scripts" / "acceptance.py")


def test_benchmark_keeps_failed_samples():
    summary = benchmark.summarize_samples([
        benchmark.Sample(seconds=1.0, exit_code=0),
        benchmark.Sample(seconds=9.0, exit_code=1),
    ])
    assert summary.sample_count == 2
    assert summary.failed_count == 1
    assert summary.promotable is False
    assert summary.p95 == 9.0


def test_capped_sample_is_not_promotable():
    summary = benchmark.summarize_samples([benchmark.Sample(seconds=0.2, exit_code=0, capped=True)])
    assert summary.capped_count == 1
    assert summary.promotable is False


def test_empty_and_invalid_sample_accounting_is_conservative():
    assert benchmark.summarize_samples([]).promotable is False
    with pytest.raises(ValueError, match="finite"):
        benchmark.summarize_samples([benchmark.Sample(seconds=float("nan"), exit_code=0)])
    with pytest.raises(ValueError, match="integer"):
        benchmark.summarize_samples([benchmark.Sample(seconds=1.0, exit_code=True)])


def test_bounded_command_preserves_failure_and_artifacts(tmp_path):
    sample = benchmark.run_command(
        (sys.executable, "-c", "import sys; print('out', end=''); print('err', file=sys.stderr, end=''); sys.exit(7)"),
        cwd=tmp_path,
        timeout=2,
        artifact_dir=tmp_path / "artifacts",
        label="failure",
    )
    assert sample.exit_code == 7
    assert sample.capped is False
    assert (tmp_path / "artifacts" / "failure.stdout").read_bytes() == b"out"
    assert (tmp_path / "artifacts" / "failure.stderr").read_bytes() == b"err"


def test_bounded_command_rejects_symlinked_artifact_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "artifacts"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        benchmark.run_command(
            (sys.executable, "-c", "pass"),
            cwd=tmp_path,
            timeout=2,
            artifact_dir=link,
            label="unsafe",
        )


def test_command_timeout_is_capped_and_owned_child_is_terminated(tmp_path):
    sample = benchmark.run_command(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        timeout=0.05,
        artifact_dir=tmp_path / "artifacts",
        label="timeout",
    )
    assert sample.exit_code == 124
    assert sample.capped is True


def test_fixture_domain_is_explicit_and_nested(tmp_path):
    domain = acceptance._fixture_domain(tmp_path)
    marker = json.loads((domain / "fixture-domain.json").read_text())
    assert marker == {"schema_version": 1, "fixture": True, "children": ["child-a", "child-b"]}
    assert (domain / "child-a" / "tests" / "nested" / "test_smoke.py").is_file()
    assert (domain / "child-b" / "tests" / "nested" / "test_smoke.py").is_file()
    assert stat.S_IMODE(domain.stat().st_mode) & stat.S_IWOTH == 0


def test_plan_only_acceptance_does_not_execute_or_claim_support(tmp_path):
    candidate = tmp_path / "ptest"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    evidence = acceptance.run_acceptance(
        root=tmp_path,
        ptest=candidate,
        output=tmp_path.parent / f"{tmp_path.name}-evidence",
        execute=False,
    )
    assert evidence["status"] == "inventory-only"
    assert evidence["promotable"] is False
    assert evidence["attempts"][0]["status"] == "not-run"
    assert evidence["adoption"]["pytest"]["status"] == "blocked-unverified"
