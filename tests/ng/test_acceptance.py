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
    assert stat.S_IMODE((tmp_path / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "artifacts" / "failure.stdout").stat().st_mode) == 0o600


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


def test_oversized_output_is_stream_capped_and_terminated(tmp_path):
    sample = benchmark.run_command(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000000); sys.stdout.flush(); __import__('time').sleep(10)"),
        cwd=tmp_path,
        timeout=5,
        artifact_dir=tmp_path / "oversized-artifacts",
        label="oversized",
    )
    retained = (tmp_path / "oversized-artifacts" / "oversized.stdout").stat().st_size
    assert sample.capped is True
    assert sample.exit_code == 124
    assert retained <= benchmark.MAX_OUTPUT_BYTES


def test_fixture_domain_is_explicit_and_nested(tmp_path):
    domain = acceptance._fixture_domain(tmp_path)
    marker = (domain / "fixture-domain.toml").read_text()
    assert "fixture = true" in marker
    assert "workload = \"synthetic-or-miniature\"" in marker
    assert (domain / "child-a" / "tests" / "nested" / "test_smoke.py").is_file()
    assert (domain / "child-b" / "tests" / "nested" / "test_smoke.py").is_file()
    assert (domain / "child-a" / ".ptest.toml").is_file()
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


def test_generic_exit_zero_candidate_cannot_promote_execute_mode(tmp_path):
    candidate = tmp_path / "generic"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    evidence = acceptance.run_acceptance(
        root=tmp_path,
        ptest=candidate,
        output=tmp_path.parent / f"{tmp_path.name}-generic-evidence",
        execute=True,
        timeout=2,
    )
    assert evidence["promotable"] is False
    assert evidence["status"] == "failed"
    assert evidence["attempts"][0]["name"] == "candidate-version"


@pytest.mark.skipif(not (ROOT / ".venv/bin/ptest").is_file(), reason="candidate development environment is unavailable")
def test_candidate_bound_execute_records_lifecycle_and_never_version_only_promotes(tmp_path):
    candidate = ROOT / ".venv/bin/ptest"
    evidence = acceptance.run_acceptance(
        root=ROOT,
        ptest=candidate,
        output=tmp_path.parent / f"{tmp_path.name}-candidate-evidence",
        execute=True,
        timeout=5,
    )
    names = [attempt["name"] for attempt in evidence["attempts"]]
    assert names[:2] == ["candidate-version", "init"]
    expected_success = {"candidate-version", "init", "where", "plan", "doctor", "status", "full", "scoped", "automatic"}
    observed = {attempt["name"]: attempt for attempt in evidence["attempts"]}
    assert expected_success | {"cancel"} <= set(observed)
    for name in expected_success:
        assert observed[name]["status"] == "passed"
        assert observed[name]["exit_code"] == 0
    assert observed["cancel"]["status"] == "blocked-unverified"
    assert observed["cancel"]["exit_code"] is None
    assert evidence["candidate_identity"]["version"] == "0.1.0"
    assert evidence["promotable"] is False


def test_benchmark_cli_rejects_raw_runner_before_launch(tmp_path, monkeypatch):
    candidate = tmp_path / "ptest"
    candidate.write_text("#!/usr/bin/python\nfrom ptest.cli import main\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    output = tmp_path.parent / f"{tmp_path.name}-raw-rejected"

    def launched(*args, **kwargs):
        pytest.fail("raw runner was launched before CLI validation")

    monkeypatch.setattr(benchmark, "run_command", launched)
    result = benchmark.main([
        "--root", str(tmp_path), "--candidate", str(candidate),
        "--profile", benchmark.WORKLOAD_NAME, "--output", str(output),
        "--", "/bin/sh", "--version",
    ])
    assert result == 2
    assert not output.exists()


def test_benchmark_cli_rejects_mismatched_candidate_and_profile_before_launch(tmp_path, monkeypatch):
    candidate = tmp_path / "ptest"
    candidate.write_text("#!/usr/bin/python\nfrom ptest.cli import main\n")
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    other = tmp_path / "other-ptest"
    other.write_bytes(candidate.read_bytes())
    other.chmod(other.stat().st_mode | stat.S_IXUSR)
    output = tmp_path.parent / f"{tmp_path.name}-mismatch-rejected"

    monkeypatch.setattr(benchmark, "run_command", lambda *a, **k: pytest.fail("launch before validation"))
    result = benchmark.main([
        "--root", str(tmp_path), "--candidate", str(candidate),
        "--profile", "unsupported-profile", "--output", str(output),
        "--", str(other), "--version",
    ])
    assert result == 2
    assert not output.exists()


@pytest.mark.skipif(not (ROOT / ".venv/bin/ptest").is_file(), reason="candidate development environment is unavailable")
def test_benchmark_cli_records_candidate_bound_nonpromotable_artifacts(tmp_path, capsys):
    candidate = ROOT / ".venv/bin/ptest"
    output = tmp_path.parent / f"{tmp_path.name}-benchmark-cli"
    result = benchmark.main([
        "--root", str(ROOT), "--candidate", str(candidate),
        "--profile", benchmark.WORKLOAD_NAME, "--samples", "1",
        "--output", str(output), "--", str(candidate), "--version",
    ])
    assert result == 1
    payload = json.loads((output / "benchmark.json").read_text())
    assert payload["candidate_bound"] is True
    assert payload["summary"]["promotable"] is False
    assert payload["profile"] == benchmark.WORKLOAD_NAME
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    attempts = output / "attempts" / "sample-1"
    assert stat.S_IMODE(attempts.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "benchmark.json").stat().st_mode) == 0o600
    assert capsys.readouterr().out


def test_evidence_root_rejects_existing_file_directory_and_parent_symlink(tmp_path):
    existing_file = tmp_path / "existing-file"
    existing_file.write_text("owned by test")
    with pytest.raises(ValueError, match="new directory"):
        acceptance.run_acceptance(root=tmp_path, ptest=existing_file, output=existing_file)

    existing_dir = tmp_path / "existing-dir"
    existing_dir.mkdir()
    with pytest.raises(ValueError, match="new directory"):
        acceptance.run_acceptance(root=tmp_path, ptest=existing_file, output=existing_dir)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="parent"):
        acceptance.run_acceptance(
            root=tmp_path, ptest=existing_file,
            output=linked_parent / "new-evidence",
        )


def test_benchmark_evidence_root_is_exclusive_and_not_reusable(tmp_path):
    existing = tmp_path.parent / f"{tmp_path.name}-benchmark-existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="new directory"):
        benchmark._validate_output(existing, tmp_path)

    fresh = tmp_path.parent / f"{tmp_path.name}-benchmark-fresh"
    benchmark._validate_output(fresh, tmp_path)
    with pytest.raises(ValueError, match="new directory"):
        benchmark._validate_output(fresh, tmp_path)

    outside = tmp_path.parent / f"{tmp_path.name}-benchmark-outside"
    outside.mkdir()
    linked = tmp_path.parent / f"{tmp_path.name}-benchmark-linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        benchmark._validate_output(linked, tmp_path)

    linked_parent = tmp_path.parent / f"{tmp_path.name}-benchmark-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="parent"):
        benchmark._validate_output(linked_parent / "new", tmp_path)
