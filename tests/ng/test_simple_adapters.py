"""Contract tests for preparation-only Go, Cargo and literal command profiles."""
from __future__ import annotations

from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.adapters.simple import prepare


RUN_ID = "a" * 32
NONCE = "b" * 64
DOMAIN_ID = "c" * 32


def _config(tmp_path: Path, kind: C.RunnerKind, *, args: tuple[str, ...] = (),
            full_args: tuple[str, ...] = (), workers: int = 2) -> C.Config:
    return C.Config(
        runner=C.RunnerConfig(
            kind=kind,
            launcher=("go",) if kind is C.RunnerKind.GO else
            (("cargo",) if kind is C.RunnerKind.CARGO else ("literal-tool",)),
            args=args, full_args=full_args,
            test_roots=(".",) if kind is not C.RunnerKind.COMMAND else (),
            workers=workers,
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="d" * 32,
        config_path=tmp_path / ".ptest.toml",
    )


def _plan(*, execution: str = "full") -> C.Plan:
    return C.Plan(mode=C.Mode.FULL, execution=execution)


def _grant(slots: int = 2) -> C.Grant:
    return C.Grant(run_id=RUN_ID, nonce=NONCE, slots=slots,
                   memory_estimate_mb=None, reserved_memory_mb=None,
                   generation=1, domain_id=DOMAIN_ID)


def _attempt(slots: int = 2) -> C.AttemptIdentity:
    return C.AttemptIdentity(run_id=RUN_ID, attempt_id="a001",
                             resource_prefix="ptest_run", worker_count=slots)


def test_go_disables_cached_success(tmp_path):
    prepared = prepare(_config(tmp_path, C.RunnerKind.GO), _plan(),
                       _grant(), _attempt())
    assert prepared.argv == (
        "go", "test", "-count=1", "-p=2", "-parallel=2", "-cpu=2", ".",
    )
    assert dict(prepared.env_updates)["GOMAXPROCS"] == "2"
    assert prepared.capability.execution is C.ExecutionTier.UNAVAILABLE


@pytest.mark.parametrize(("kind", "args"), [
    (C.RunnerKind.GO, ("test", "-p=3")),
    (C.RunnerKind.GO, ("test", "-parallel", "3")),
    (C.RunnerKind.GO, ("test", "-cpu=1,2")),
    (C.RunnerKind.CARGO, ("test", "-j", "3")),
    (C.RunnerKind.CARGO, ("test", "--", "--test-threads=3")),
])
def test_native_owned_concurrency_controls_are_rejected(tmp_path, kind, args):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, kind, args=args), _plan(), _grant(), _attempt())


def test_cargo_adds_build_and_libtest_grant_bounds(tmp_path):
    prepared = prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)), _plan(),
                       _grant(), _attempt())
    assert prepared.argv == ("cargo", "test", "-j=2", "--lib", "--", "--test-threads=2")
    assert prepared.summary.generated_options == ("cargo.jobs=2", "libtest.threads=2")


@pytest.mark.parametrize("target", ["--doc", "--bench", "--no-run"])
def test_cargo_unsupported_or_ambiguous_target_is_rejected(tmp_path, target):
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("test", target)),
                _plan(), _grant(), _attempt())


def test_cargo_default_target_refuses_silent_doctest_omission(tmp_path):
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO), _plan(), _grant(), _attempt())


def test_cargo_project_build_jobs_are_rejected(tmp_path):
    cargo_config = tmp_path / ".cargo" / "config.toml"
    cargo_config.parent.mkdir()
    cargo_config.write_text("[build]\njobs = 8\n", encoding="utf-8")
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


def test_cargo_control_symlink_is_rejected(tmp_path):
    external = tmp_path / "external.toml"
    external.write_text("[build]\ntarget-dir = 'target'\n", encoding="utf-8")
    cargo_config = tmp_path / ".cargo" / "config.toml"
    cargo_config.parent.mkdir()
    cargo_config.symlink_to(external)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


def test_cargo_custom_harness_is_rejected(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = 'fixture'\nversion = '0.1.0'\n"
        "[[test]]\nname = 'custom'\nharness = false\n",
        encoding="utf-8",
    )
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--test", "custom")),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize(("name", "value", "kind", "args"), [
    ("GOFLAGS", "-p=8", C.RunnerKind.GO, ()),
    ("GOMAXPROCS", "8", C.RunnerKind.GO, ()),
    ("CARGO_BUILD_JOBS", "8", C.RunnerKind.CARGO, ("--lib",)),
    ("RUST_TEST_THREADS", "8", C.RunnerKind.CARGO, ("--lib",)),
])
def test_inherited_native_concurrency_controls_are_rejected(
    tmp_path, monkeypatch, name, value, kind, args,
):
    monkeypatch.setenv(name, value)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, kind, args=args), _plan(), _grant(), _attempt())


def test_command_preserves_literal_tokens_and_reserves_exclusive_capacity(tmp_path):
    tokens = ("--flag", "secret with space", 'quote"token', "$(metacharacter)", ";")
    prepared = prepare(_config(tmp_path, C.RunnerKind.COMMAND, args=tokens), _plan(),
                       _grant(), _attempt())
    assert prepared.argv == ("literal-tool",) + tokens
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert prepared.capability.selection is False
    assert any(reason.code == "unsupported-capability"
               for reason in prepared.capability.limitations)


def test_command_rejects_implicit_scoped_selection(tmp_path):
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.COMMAND), _plan(execution="scoped"),
                _grant(), _attempt())


def test_native_profiles_remain_unqualified_until_task11_execution(tmp_path):
    prepared = prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--tests",)),
                       _plan(), _grant(), _attempt())
    assert prepared.capability.execution is C.ExecutionTier.UNAVAILABLE
    assert "Task11" in prepared.capability.limitations[0].message


def test_attempt_must_match_grant(tmp_path):
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.GO), _plan(), _grant(), _attempt(1))
