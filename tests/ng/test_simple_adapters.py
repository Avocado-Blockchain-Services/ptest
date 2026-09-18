"""Contract tests for preparation-only Go, Cargo and literal command profiles."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.adapters import simple
from ptest.adapters.simple import prepare


RUN_ID = "a" * 32
NONCE = "b" * 64
DOMAIN_ID = "c" * 32


@pytest.fixture(autouse=True)
def clean_native_project(tmp_path, monkeypatch):
    # Isolate environment/config discovery without borrowing the account's home.
    for name in tuple(os.environ):
        if name.startswith(("CARGO_", "RUST", "GO")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = 'fixture'\nversion = '0.1.0'\n", encoding="utf-8",
    )


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
        "go", "test", "-count=1", "-p=1", "-parallel=2", "-cpu=2", ".",
    )
    assert prepared.summary.generated_options == (
        "go.count=1", "go.packages=1", "go.parallel=2", "go.cpu=2",
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


def test_command_preserves_literal_tokens_and_requires_exclusive_admission(tmp_path):
    tokens = ("--flag", "secret with space", 'quote"token', "$(metacharacter)", ";")
    prepared = prepare(_config(tmp_path, C.RunnerKind.COMMAND, args=tokens), _plan(),
                       _grant(), _attempt())
    assert prepared.argv == ("literal-tool",) + tokens
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert prepared.capability.selection is False
    warning = " ".join(reason.message for reason in prepared.capability.limitations)
    assert "AdmissionRequest.exclusive=True" in warning
    assert "does not prove or contain inner command parallelism" in warning


def test_command_scoped_uses_only_common_args_and_full_adds_full_args(tmp_path):
    common = (
        "literal value with spaces",
        'quote"and\'mark',
        "ümlaut-値",
        "$(not-shell-expanded)",
        "--looks-like-a-flag",
        "; && |",
    )
    full_only = ("full-only value with spaces", "--full-flag", "Ω")
    config = _config(tmp_path, C.RunnerKind.COMMAND, args=common, full_args=full_only)

    scoped = prepare(
        config,
        C.Plan(mode=C.Mode.SCOPED, execution="scoped"),
        _grant(),
        _attempt(),
    )
    full = prepare(config, C.Plan(mode=C.Mode.FULL, execution="full"), _grant(), _attempt())

    assert scoped.argv == config.runner.launcher + common
    assert full.argv == config.runner.launcher + common + full_only
    assert full.argv != scoped.argv
    assert full_only[0] not in scoped.argv


def test_command_rejects_public_file_payloads(tmp_path):
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("literal argv",))
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.COMMAND), plan, _grant(), _attempt())


@pytest.mark.parametrize("execution", ["selected", "none", "malformed"])
def test_command_rejects_non_command_plan_shapes(tmp_path, execution):
    plan = object.__new__(C.Plan)
    object.__setattr__(plan, "mode", C.Mode.SCOPED)
    object.__setattr__(plan, "execution", execution)
    object.__setattr__(plan, "files", ())
    object.__setattr__(plan, "reasons", ())
    object.__setattr__(plan, "input_digest", None)
    object.__setattr__(plan, "compatibility", None)
    object.__setattr__(plan, "baseline_run_id", None)
    object.__setattr__(plan, "static_preview", False)
    with pytest.raises(C.Problem):
        prepare(_config(tmp_path, C.RunnerKind.COMMAND), plan, _grant(), _attempt())


@pytest.mark.parametrize("mode", [C.Mode.FULL, C.Mode.AUTOMATIC])
def test_command_scoped_execution_requires_explicit_scoped_mode(tmp_path, mode):
    plan = C.Plan(mode=mode, execution="scoped")
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.COMMAND), plan, _grant(), _attempt())


def test_command_plan_full_does_not_claim_selection_or_inventory(tmp_path):
    config = _config(
        tmp_path,
        C.RunnerKind.COMMAND,
        args=("--input", "all"),
        full_args=("--coverage",),
    )
    prepared = prepare(
        config,
        C.Plan(mode=C.Mode.AUTOMATIC, execution="full"),
        _grant(),
        _attempt(),
    )
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert prepared.capability.selection is False


def test_command_rejects_oversized_combined_argv_without_echoing_tokens(tmp_path):
    token = "x" * 16384
    config = _config(
        tmp_path,
        C.RunnerKind.COMMAND,
        args=tuple(token for _ in range(8)),
        full_args=(token,),
    )
    with pytest.raises(C.Problem, match="native-config-invalid") as caught:
        prepare(config, C.Plan(mode=C.Mode.FULL, execution="full"), _grant(), _attempt())
    assert token not in str(caught.value)


def test_command_rejects_malformed_runner_argv_as_typed_problem(tmp_path):
    config = _config(tmp_path, C.RunnerKind.COMMAND)
    runner = object.__new__(C.RunnerConfig)
    object.__setattr__(runner, "kind", C.RunnerKind.COMMAND)
    object.__setattr__(runner, "launcher", ("literal-tool",))
    object.__setattr__(runner, "args", ("ok", 7))
    object.__setattr__(runner, "full_args", ())
    object.__setattr__(runner, "test_roots", ())
    object.__setattr__(runner, "workers", 2)
    object.__setattr__(runner, "lifecycle", "cooperative-process-group")
    config = replace(config, runner=runner)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(config, C.Plan(mode=C.Mode.SCOPED, execution="scoped"), _grant(), _attempt())


def test_command_rejects_implicit_scoped_selection(tmp_path):
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.COMMAND),
                C.Plan(mode=C.Mode.SCOPED, execution="selected"),
                _grant(), _attempt())


def test_native_profiles_remain_unqualified_until_task11_execution(tmp_path):
    prepared = prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--tests",)),
                       _plan(), _grant(), _attempt())
    assert prepared.capability.execution is C.ExecutionTier.UNAVAILABLE
    assert "Task11" in prepared.capability.limitations[0].message


def test_attempt_must_match_grant(tmp_path):
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.GO), _plan(), _grant(), _attempt(1))


@pytest.mark.parametrize("prefix", ["-", "--", "-test.", "--test."])
@pytest.mark.parametrize("control", ["p", "parallel", "cpu", "count"])
@pytest.mark.parametrize("joined", [False, True])
@pytest.mark.parametrize("field", ["args", "full_args"])
def test_go_owned_control_aliases_cannot_override_grant(tmp_path, prefix, control, joined, field):
    option = prefix + control
    tokens = (option + "=64",) if joined else (option, "64")
    # A preceding boolean must not swallow the next option or its value.
    config = _config(tmp_path, C.RunnerKind.GO, **{field: ("-v",) + tokens})
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(config, _plan(), _grant(), _attempt())


@pytest.mark.parametrize("tokens", [
    ("-args", "-test.parallel=64"), ("--args", "--test.count=1"),
    ("-test.args", "-test.cpu=64"), ("--", "-test.parallel=64"),
    ("-fuzz=Fuzz",), ("--fuzztime=1s",), ("-test.fuzzminimizetime=1s",),
    ("--exec=wrapper",), ("-c",), ("--c",), ("-o", "binary"),
    ("--outputdir=outside",), ("-list", ".*"), ("--test.list=.*",),
    ("-run=^$",), ("--test.run=^$",), ("-skip", ".*"),
    ("-test.short",), ("--short=false",), ("-bench=.",),
    ("-n",), ("-help",), ("--unknown-control=64",),
    ("build",), ("./extra-package",), ("-v", "true"),
    ("-timeout",), ("-timeout", "-parallel=64"),
])
@pytest.mark.parametrize("field", ["args", "full_args"])
def test_go_ambiguous_forwarded_or_nonfull_modes_are_refused(tmp_path, tokens, field):
    with pytest.raises(C.Problem):
        prepare(_config(tmp_path, C.RunnerKind.GO, **{field: tokens}),
                _plan(), _grant(), _attempt())


def test_go_known_reporting_options_keep_literal_values(tmp_path):
    tokens = ("test", "-v", "--json", "-coverprofile", "coverage with spaces.out")
    prepared = prepare(_config(tmp_path, C.RunnerKind.GO, args=tokens),
                       _plan(), _grant(), _attempt())
    assert prepared.argv[6:] == tokens[1:] + (".",)


@pytest.mark.parametrize("root", ["-p=64", "--", "-args", "../other", "/tmp", "test.go"])
def test_go_test_roots_cannot_be_flags_or_unsafe_file_scopes(tmp_path, root):
    config = _config(tmp_path, C.RunnerKind.GO)
    config = replace(config, runner=replace(config.runner, test_roots=(root,)))
    with pytest.raises(C.Problem):
        prepare(config, _plan(), _grant(), _attempt())


@pytest.mark.parametrize("kind", [C.RunnerKind.GO, C.RunnerKind.CARGO])
@pytest.mark.parametrize("execution", ["scoped", "selected"])
@pytest.mark.parametrize("files", [("tests/foo.rs",), ("foo_test.go",), ("-p=64",), ("--",)])
def test_native_file_plans_are_refused_until_mapping_is_defined(tmp_path, kind, execution, files):
    config = _config(tmp_path, kind, args=("--lib",) if kind is C.RunnerKind.CARGO else ())
    plan = C.Plan(mode=C.Mode.SCOPED, execution=execution, files=files)
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(config, plan, _grant(), _attempt())


@pytest.mark.parametrize("args,full_args", [
    (("--lib", "--", "--nocapture"), ()),
    (("--lib",), ("--", "--show-output")),
    (("--lib", "--", "--nocapture"), ("--show-output",)),
])
def test_cargo_places_owned_threads_before_user_libtest_options(tmp_path, args, full_args):
    prepared = prepare(_config(tmp_path, C.RunnerKind.CARGO, args=args, full_args=full_args),
                       _plan(), _grant(), _attempt())
    index = prepared.argv.index("--")
    assert prepared.argv.count("--") == 1
    assert prepared.argv[index + 1] == "--test-threads=2"
    assert prepared.argv[index + 2:] == (args + full_args)[(args + full_args).index("--") + 1:]


@pytest.mark.parametrize("tokens", [
    ("-j64",), ("-j=64",), ("--jobs=64",), ("--jobs", "64"),
    ("-qj64",), ("--jobs64",), ("--config", "build.jobs=64"),
    ("--config=build.jobs=64",), ("--manifest-path=other/Cargo.toml",),
    ("--workspace",), ("--all",), ("-p", "other"), ("-pother",),
    ("--package=other",), ("--exclude=other",), ("--target=custom",),
    ("--target-dir=other",), ("-C", "other"), ("-Zunstable-options",),
    ("--no-run",), ("--no-run=false",), ("--benches",), ("--all-targets",),
    ("--example=custom",), ("--examples",), ("--doc",), ("--help",),
    ("--", "--test-threads", "64"), ("--", "--test-threads=64"),
    ("--", "--skip", "fixture"), ("--", "--list"), ("--", "--ignored"),
    ("--", "--exact"), ("--", "--bench"), ("--", "--", "--nocapture"),
    ("--", "name-filter"), ("name-filter",), ("tests/foo.rs",),
    ("--", "--format=terse"), ("--", "-Zunstable-options"),
    ("--bin", "--"), ("--bin=",), ("--lib=true",),
])
@pytest.mark.parametrize("field", ["args", "full_args"])
def test_cargo_unowned_modes_filters_and_control_forms_are_refused(tmp_path, tokens, field):
    settings = {"args": ("--lib",), "full_args": ()}
    settings[field] += tokens
    with pytest.raises(C.Problem):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, **settings),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("source", ["project", "ancestor", "cargo-home"])
@pytest.mark.parametrize("filename", ["config", "config.toml"])
@pytest.mark.parametrize("body", [
    "[build]\njobs = 64\n", "[target.any]\nrunner = 'wrapper'\n",
    "[build]\nrustc-wrapper = 'wrapper'\n", "[env]\nRUST_TEST_THREADS = '64'\n",
    "include = ['other.toml']\n", "# Empty configs still require native qualification.\n",
])
def test_cargo_discovered_config_sources_fail_closed(tmp_path, source, filename, body):
    config_dir = {"project": tmp_path / ".cargo", "ancestor": tmp_path.parent / ".cargo",
                  "cargo-home": tmp_path / "cargo-home"}[source]
    config_dir.mkdir(exist_ok=True)
    control = config_dir / filename
    control.write_text(body, encoding="utf-8")
    try:
        with pytest.raises(C.Problem, match="native-config-invalid"):
            prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                    _plan(), _grant(), _attempt())
    finally:
        control.unlink()


@pytest.mark.parametrize("name", [
    "CARGO_BUILD_JOBS", "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
    "CARGO_BUILD_TARGET", "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUNNER", "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_FUTURE_CONTROL", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTC",
    "RUSTDOC", "RUSTFLAGS", "RUSTDOCFLAGS", "RUST_TEST_THREADS", "RUST_TEST_NOCAPTURE",
])
def test_cargo_inherited_execution_controls_cannot_bypass_grant(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "hostile-wrapper-or-control")
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("placement", ["root", "ancestor", "explicit-parent"])
def test_cargo_workspaces_are_refused_without_member_harness_discovery(tmp_path, placement):
    if placement == "root":
        path = tmp_path / "Cargo.toml"
        body = "[workspace]\nmembers = ['member']\n"
    elif placement == "ancestor":
        path = tmp_path.parent / "Cargo.toml"
        body = "[workspace]\nmembers = ['*']\n"
    else:
        path = tmp_path / "Cargo.toml"
        body = "[package]\nname = 'fixture'\nversion = '0.1.0'\nworkspace = '../external'\n"
    path.write_text(body, encoding="utf-8")
    try:
        with pytest.raises(C.Problem, match="unsupported-capability"):
            prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--tests",)),
                    _plan(), _grant(), _attempt())
    finally:
        path.unlink()


@pytest.mark.parametrize("kind", ["lib", "bin", "test"])
def test_cargo_custom_harness_any_standard_target_is_refused(tmp_path, kind):
    header = "[lib]" if kind == "lib" else f"[[{kind}]]"
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = 'fixture'\nversion = '0.1.0'\n" + header +
        "\nname = 'custom'\nharness = false\n", encoding="utf-8",
    )
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--tests",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("kind", ["example", "bench"])
def test_cargo_nonstandard_test_enabled_targets_are_refused(tmp_path, kind):
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = 'fixture'\nversion = '0.1.0'\n" + f"[[{kind}]]\n" +
        "name = 'custom'\ntest = true\nharness = false\n", encoding="utf-8",
    )
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--tests",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("source", ["project", "cargo-home"])
@pytest.mark.parametrize("shape", ["symlink", "file", "inaccessible"])
def test_cargo_uncertain_config_directories_are_refused(tmp_path, source, shape):
    directory = tmp_path / (".cargo" if source == "project" else "cargo-home")
    if shape == "symlink":
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "config").write_text("[build]\njobs = 64\n", encoding="utf-8")
        directory.symlink_to(other, target_is_directory=True)
    elif shape == "file":
        directory.write_text("not a directory", encoding="utf-8")
    else:
        directory.mkdir(mode=0o000)
    try:
        with pytest.raises(C.Problem, match="native-config-invalid"):
            prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                    _plan(), _grant(), _attempt())
    finally:
        if shape == "inaccessible":
            directory.chmod(0o700)


def test_cargo_default_home_config_is_also_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CARGO_HOME")
    account_home = tmp_path / "account"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: account_home))
    directory = account_home / ".cargo"
    directory.mkdir(parents=True)
    (directory / "config").write_text("[target.any]\nrunner = 'wrapper'\n", encoding="utf-8")
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("cargo_home", ["", "relative", "../external"])
def test_cargo_ambiguous_home_is_refused(tmp_path, monkeypatch, cargo_home):
    monkeypatch.setenv("CARGO_HOME", cargo_home)
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("shape", ["absent", "directory", "fifo", "symlink", "malformed", "oversized"])
def test_cargo_uncertain_manifest_discovery_fails_closed(tmp_path, shape):
    path = tmp_path / "Cargo.toml"
    path.unlink()
    if shape == "directory":
        path.mkdir()
    elif shape == "fifo":
        os.mkfifo(path)
    elif shape == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif shape == "malformed":
        path.write_text("[package", encoding="utf-8")
    elif shape == "oversized":
        path.write_text("#" * (C.INPUT_FILE_MAX_BYTES + 1), encoding="utf-8")
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(tmp_path, C.RunnerKind.CARGO, args=("--lib",)),
                _plan(), _grant(), _attempt())


@pytest.mark.parametrize("kind", [C.RunnerKind.GO, C.RunnerKind.CARGO])
def test_native_launcher_prefixes_cannot_inject_wrappers(tmp_path, kind):
    config = _config(tmp_path, kind, args=("--lib",) if kind is C.RunnerKind.CARGO else ())
    config = replace(config, runner=replace(config.runner, launcher=("wrapper", "runner")))
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(config, _plan(), _grant(), _attempt())


@pytest.mark.parametrize("kind,required", [
    (C.RunnerKind.COMMAND, True), (C.RunnerKind.GO, False), (C.RunnerKind.CARGO, False),
])
def test_admission_hook_requires_exclusive_only_for_literal_commands(tmp_path, kind, required):
    assert simple.requires_exclusive(_config(tmp_path, kind)) is required


@pytest.mark.parametrize("runner", ["go", "cargo", "command"])
def test_fixture_matrix_prepares_real_projects_without_resolution_or_execution(tmp_path, monkeypatch, runner):
    def forbidden(*args, **kwargs):
        raise AssertionError("Task10 must not resolve or execute a native runner")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    fixture = Path(__file__).parent / "fixtures" / runner
    matrix = json.loads((fixture / "matrix.json").read_text(encoding="utf-8"))
    (tmp_path / "Cargo.toml").unlink()
    for case in matrix["cases"]:
        project = tmp_path / case["id"]
        shutil.copytree(fixture / case["project"], project)
        settings = matrix["runner"] | case

        def tokens(name):
            return tuple(token.replace("{python}", sys.executable)
                         .replace("{project}", str(project))
                         .replace("{run_output}", str(tmp_path / "run-output"))
                         for token in settings[name])

        config = _config(project, C.RunnerKind(runner))
        config = replace(config, runner=replace(
            config.runner, launcher=tokens("launcher"), args=tokens("args"),
            test_roots=tuple(settings["test_roots"]), workers=settings["workers"],
        ))
        if "prepare_error" in case:
            with pytest.raises(C.Problem, match=case["prepare_error"]):
                prepare(config, _plan(), _grant(), _attempt())
        else:
            prepared = prepare(config, _plan(), _grant(), _attempt())
            assert prepared.capability.execution.value == case["prepare_tier"]
            assert prepared.argv[:len(config.runner.launcher)] == config.runner.launcher
            if "expected_stdout_json" in case:
                assert prepared.argv[-4:] == tuple(case["expected_stdout_json"])
