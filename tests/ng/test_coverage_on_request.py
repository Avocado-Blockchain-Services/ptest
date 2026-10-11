"""Coverage on request (spec docs/specs/2026-10-11-optional-coverage.md).

Every test names the spec clause it pins. Coverage options live in
``[runner] coverage_args`` and reach argv only when a run asks for them with
``--cov``.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.config import _serialize_fresh, resolve_config


def _write_pytest_config(root: Path, runner_extra: str = "",
                         kind: str = "pytest") -> Path:
    (root / "tests").mkdir(exist_ok=True)
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        'project_id = "ab1234567890ab1234567890ab123456"\n'
        "\n[runner]\n"
        f'kind = "{kind}"\n'
        'launcher = ["python"]\n'
        "args = []\n"
        'test_roots = ["tests"]\n'
        + runner_extra +
        "\n[resources]\n"
        "locks = []\n"
        "memory_mb_per_worker = 0\n"
        'probe_isolation = "undeclared"\n',
        encoding="utf-8",
    )
    return path


def _pytest_config(**runner_overrides) -> C.Config:
    runner_fields = {
        "kind": C.RunnerKind.PYTEST,
        "launcher": ("python",),
        "args": (),
        "full_args": (),
        "test_roots": ("tests",),
    }
    runner_fields.update(runner_overrides)
    return C.Config(
        runner=C.RunnerConfig(**runner_fields),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
    )


def _toml_array(tokens) -> str:
    return "[" + ", ".join('"' + token + '"' for token in tokens) + "]"


# --- C1: [runner] coverage_args ------------------------------------------------

def test_config_without_coverage_args_has_none(tmp_path):
    """C1: the key is optional and defaults to empty."""
    _write_pytest_config(tmp_path)
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config.runner.coverage_args == ()


@pytest.mark.parametrize("tokens", [
    ["--cov"],
    ["--cov", "--cov-report", "term"],
    ["--cov=ptest", "--cov-report=term-missing"],
    ["--cov", "src", "--cov-report", "xml:cov.xml"],
    ["--cov", "--cov-config", ".coveragerc", "--cov-branch"],
    ["--cov", "--cov-fail-under", "85"],
    ["--cov", "--cov-fail-under=85", "--cov-context=test"],
    ["--cov", "--cov-context", "test"],
])
def test_config_accepts_coverage_options(tmp_path, tokens):
    """C1: the closed allowlist of pytest-cov options parses literally."""
    _write_pytest_config(tmp_path, f"coverage_args = {_toml_array(tokens)}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None, resolution.problem
    assert resolution.config.runner.coverage_args == tuple(tokens)


@pytest.mark.parametrize("tokens", [
    # Hostile input: anything that could narrow, redirect or replace the
    # suite must never ride in on the coverage key (negative contract).
    ["--cov", "-k", "slow"],
    ["--cov", "-m", "not slow"],
    ["--cov", "--deselect", "tests/test_a.py::test_x"],
    ["--cov", "-p", "evil_plugin"],
    ["--cov", "-n", "4"],
    ["--cov", "@more-args.txt"],
    ["--cov", "--no-cov"],
    ["--cov", "tests/test_a.py::test_x"],
    ["--cov", "--cov-report"],
    ["--cov", "--cov-report", "-k"],
    ["--cov", "--cov-config"],
    ["--cov", "--cov-fail-under", "--cov-branch"],
    ["--cov", ""],
    ["--cov-report", "term"],
    ["--cov-branch"],
    ["--cov", "--cov-append"],
    ["--cov", "--maxfail=1"],
])
def test_config_rejects_non_coverage_tokens(tmp_path, tokens):
    """C1 negative contract: coverage_args holds coverage options only."""
    _write_pytest_config(tmp_path, f"coverage_args = {_toml_array(tokens)}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


@pytest.mark.parametrize("kind", ["vitest", "go", "cargo", "command"])
def test_config_rejects_coverage_args_for_other_runners(tmp_path, kind):
    """C1 negative contract: never silently ignored on a non-pytest runner."""
    _write_pytest_config(tmp_path, 'coverage_args = ["--cov"]\n', kind=kind)
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_config_renders_coverage_args_after_full_args_only_when_set():
    """C1: the config writer round-trips the key and omits it when empty."""
    without = _serialize_fresh(_pytest_config()).decode("utf-8")
    assert "coverage_args" not in without
    rendered = _serialize_fresh(_pytest_config(
        coverage_args=("--cov", "--cov-report", "term"))).decode("utf-8")
    assert ('full_args = []\ncoverage_args = ["--cov", "--cov-report", "term"]\n'
            in rendered)


def test_config_coverage_args_render_round_trips(tmp_path):
    """C1: what the writer renders, the parser reads back identically."""
    tokens = ("--cov=ptest", "--cov-report", "term")
    text = _serialize_fresh(_pytest_config(coverage_args=tokens)).decode("utf-8")
    line = next(item for item in text.splitlines()
                if item.startswith("coverage_args"))
    _write_pytest_config(tmp_path, line + "\n")
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config.runner.coverage_args == tokens


# --- C8: compatibility is independent of coverage -----------------------------

def test_runner_fingerprint_ignores_coverage_args_and_request():
    """C8: the runner repr feeds the compatibility MAC; coverage stays out."""
    plain = _pytest_config().runner
    declared = _pytest_config(coverage_args=("--cov",)).runner
    requested = replace(declared, coverage_requested=True)
    assert repr(plain) == repr(declared) == repr(requested)


# --- C5/C7: the bridge with and without coverage ------------------------------

from test_pytest_parallel_coverage_subprocess import (  # noqa: E402
    _identity_tests, _run_advanced_bridge)


@pytest.mark.parametrize("workers", [1, 2])
def test_runtime_identity_is_the_same_with_and_without_coverage(
        tmp_path, fake_pytest_project, workers):
    """C7: a baseline measured with --cov authorises a selected run without
    it (and the reverse), so coverage never enters the runtime identity."""
    root = fake_pytest_project(
        tests={"tests/test_ok.py": _identity_tests(4)}, git=False)
    common = ["-q", "-p", "no:cacheprovider"]
    if workers > 1:
        common = ["-n", str(workers), "--dist", "load", *common]
    with_cov = _run_advanced_bridge(
        root, [*common, "--cov=tests", "--cov-report=", "tests"],
        execution="full", workers=workers,
        extra_env={"PTEST_PYTEST_COVERAGE": "1"})
    without = _run_advanced_bridge(
        root, [*common, "tests"], execution="full", workers=workers,
        extra_env={"PTEST_PYTEST_COVERAGE": "0"})
    assert with_cov.code == 0, with_cov.stderr.decode()
    assert without.code == 0, without.stderr.decode()
    assert with_cov.report["coverage"] == {"complete": True}
    assert without.report["coverage"] == {"complete": False}
    assert with_cov.report["runtime_identity"] == without.report["runtime_identity"]


def test_bridge_without_coverage_request_needs_no_coverage_pair(
        tmp_path, fake_pytest_project):
    """C5: an advanced run that does not measure coverage never requires the
    pytest-cov/coverage pair, so it completes with the plugin disabled."""
    root = fake_pytest_project(
        tests={"tests/test_ok.py": _identity_tests(2)}, git=False)
    twin = _run_advanced_bridge(
        root, ["-q", "-p", "no:cacheprovider", "-p", "no:pytest_cov", "tests"],
        execution="full", workers=1, extra_env={"PTEST_PYTEST_COVERAGE": "0"})
    assert twin.code == 0, twin.stderr.decode()
    assert twin.report["terminal_complete"] is True
    assert twin.report["inventory"]["complete"] is True


# --- C9: reuse never lets a green without coverage satisfy --full --cov -------

import os  # noqa: E402

from ptest import history as history_api  # noqa: E402
from ptest import operations  # noqa: E402
from ptest import verified  # noqa: E402
from support import git, init_git_repo, write_ptest_toml  # noqa: E402

_DIGEST = "11" * 32
_COMPAT = "test-compat-v1"
_RUN_ID = "ef" * 16
_COV = 'coverage_args = ["--cov", "--cov-report", "term"]'


def _pytest_repo(domain, name: str = "repo", runner_extra: str = _COV) -> Path:
    root = domain.root / name
    (root / "tests").mkdir(parents=True, exist_ok=True)
    write_ptest_toml(root, kind="pytest", launcher=("python",), args=(),
                     full_args=(), project_id="ab" * 16,
                     runner_extra=runner_extra)
    init_git_repo(root, files={"tests/test_a.py": "def test_a():\n    pass\n"},
                  message="base")
    return root


def _resolved(root: Path) -> C.Config:
    config = resolve_config(root).config
    assert config is not None
    return config


def _snap(head: str) -> C.InputSnapshot:
    return C.InputSnapshot(digest=_DIGEST, compatibility=_COMPAT, head=head,
                           clean=True)


def _publish_green(domain, config, snapshot, *, coverage: bool,
                   run_id: str = _RUN_ID) -> None:
    """A committed green full baseline; ``coverage`` says whether that run's
    argv measured coverage (its publication token carries the proof)."""
    checkout = operations._checkout(config)
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.PYTEST, C.Mode.FULL, ("python",),
                                  workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=run_id, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=C.Mode.FULL,
        status=C.Status.PASSED, phase="complete",
        started_at="2026-10-11T00:00:00+00:00",
        finished_at="2026-10-11T00:00:01+00:00",
        plan=plan, command=command, exit_code=0,
        exit_origin="runner", runner_exit_code=0,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot,
        policy_digest=operations._policy_digest(config),
        sequence=history_api.next_sequence(domain, checkout),
    )
    inventory = C.Inventory(
        adapter="pytest", version="9.1.1", complete=True,
        tests=(C.TestRecord(id="tests/test_a.py::test_a", file="tests/test_a.py",
                            outcome=C.Outcome("passed"), setup_s=None,
                            call_s=0.01, teardown_s=None),),
        digest="44" * 32)
    measured = operations._coverage_config(
        config, C.RunRequest(mode=C.Mode.FULL, coverage=coverage))
    token = verified.begin_full(
        domain, checkout.project_id, snapshot.digest,
        coverage=operations.pytest_adapter.coverage_proof(measured))
    published = history_api.publish_outcome(domain, checkout, result, inventory,
                                            verification_token=token)
    assert published.baseline_published is True


def _gate(domain, config, *, coverage: bool):
    # A short queue limit: a regression that joins the wrong running run
    # must fail in seconds, not wait out the 4h default.
    request = C.RunRequest(mode=C.Mode.FULL, coverage=coverage,
                           queue_timeout_s=1)
    action, _result, claimed = operations._full_pre_admission(
        domain, config, operations._checkout(config), request, "cd" * 16,
        operations._Signals())
    if claimed:
        history_api.release_full_lease(domain, operations._checkout(config),
                                       "cd" * 16)
    return action


def test_plain_green_does_not_satisfy_full_cov_in_this_checkout(case, monkeypatch):
    """C9 negative: "already verified" (and the monorepo child skip, which
    shares this branch) needs coverage proof for a --cov request."""
    domain = case.domain()
    root = _pytest_repo(domain)
    config = _resolved(root)
    snapshot = _snap(git(root, "rev-parse", "HEAD"))
    _publish_green(domain, config, snapshot, coverage=False)
    monkeypatch.setattr(operations, "_capture_source", lambda *a, **k: snapshot)

    assert _gate(domain, config, coverage=False) == "skip"
    assert _gate(domain, config, coverage=True) == "run"


def test_coverage_green_satisfies_both_requests(case, monkeypatch):
    """C9: a green that measured coverage proves the plain gate too."""
    domain = case.domain()
    root = _pytest_repo(domain)
    config = _resolved(root)
    snapshot = _snap(git(root, "rev-parse", "HEAD"))
    _publish_green(domain, config, snapshot, coverage=True)
    monkeypatch.setattr(operations, "_capture_source", lambda *a, **k: snapshot)

    assert _gate(domain, config, coverage=True) == "skip"
    assert _gate(domain, config, coverage=False) == "skip"


def test_plain_green_after_coverage_green_keeps_the_coverage_proof(case, monkeypatch):
    """C9: re-running the plain gate on the same tree must not erase the
    earlier coverage proof for that tree."""
    domain = case.domain()
    root = _pytest_repo(domain)
    config = _resolved(root)
    snapshot = _snap(git(root, "rev-parse", "HEAD"))
    _publish_green(domain, config, snapshot, coverage=True)
    _publish_green(domain, config, snapshot, coverage=False, run_id="ee" * 16)
    monkeypatch.setattr(operations, "_capture_source", lambda *a, **k: snapshot)

    assert _gate(domain, config, coverage=True) == "skip"


def test_coverage_green_with_other_coverage_args_is_not_proof(case, monkeypatch):
    """C9 negative: the proof is bound to the coverage tokens that ran."""
    domain = case.domain()
    root = _pytest_repo(domain)
    config = _resolved(root)
    snapshot = _snap(git(root, "rev-parse", "HEAD"))
    _publish_green(domain, config, snapshot, coverage=True)
    changed = replace(config, runner=replace(
        config.runner, coverage_args=("--cov=src", "--cov-report", "xml")))
    monkeypatch.setattr(operations, "_capture_source", lambda *a, **k: snapshot)

    assert _gate(domain, changed, coverage=True) == "run"


def _worktree_pair(case):
    domain = case.domain()
    main_root = _pytest_repo(domain)
    linked = domain.root / "linked"
    git(main_root, "worktree", "add", "-q", str(linked), "-b", "feature")
    return domain, main_root, linked


@pytest.mark.parametrize("coverage_green", [False, True])
def test_verified_elsewhere_needs_coverage_proof_for_cov(case, coverage_green):
    """C9: "verified elsewhere" honours a --cov request only with proof."""
    domain, main_root, linked = _worktree_pair(case)
    snapshot = _snap(git(linked, "rev-parse", "HEAD"))
    _publish_green(domain, _resolved(linked), snapshot, coverage=coverage_green)
    config = _resolved(main_root)
    original = operations._capture_source
    try:
        operations._capture_source = lambda *a, **k: snapshot
        checkout = operations._checkout(config)
        plain = operations._full_verified_elsewhere(
            domain, config, checkout, C.RunRequest(mode=C.Mode.FULL))
        with_cov = operations._full_verified_elsewhere(
            domain, config, checkout,
            C.RunRequest(mode=C.Mode.FULL, coverage=True))
    finally:
        operations._capture_source = original
    assert plain is not None
    assert (with_cov is not None) is coverage_green


def _hold_lease(domain, config, *, coverage: bool):
    checkout = operations._checkout(config)
    measured = operations._coverage_config(
        config, C.RunRequest(mode=C.Mode.FULL, coverage=coverage))
    payload = {"version": 1, "digest": _DIGEST, "run_id": _RUN_ID,
               "pid": os.getpid(), "started_at": operations._iso_now(),
               "sequence": 1,
               "coverage": operations.pytest_adapter.coverage_proof(measured)}
    assert history_api.claim_full_lease(domain, checkout, payload) is True
    return checkout


@pytest.mark.parametrize("running,requested", [(False, True), (True, False)])
def test_full_joins_only_a_run_with_the_same_coverage_mode(
        case, monkeypatch, running, requested):
    """C9 negative: a --cov request never joins a plain running full run,
    and a plain request never inherits a coverage run's gate."""
    domain = case.domain()
    root = _pytest_repo(domain)
    config = _resolved(root)
    snapshot = _snap(git(root, "rev-parse", "HEAD"))
    checkout = _hold_lease(domain, config, coverage=running)
    monkeypatch.setattr(operations, "_capture_source", lambda *a, **k: snapshot)
    try:
        assert _gate(domain, config, coverage=requested) == "run"
    finally:
        history_api.release_full_lease(domain, checkout, _RUN_ID)


def test_old_lease_without_coverage_key_reads_as_plain(case):
    """Compatibility: a 0.5-shaped lease (no coverage key) is a plain run."""
    domain = case.domain()
    root = _pytest_repo(domain)
    checkout = operations._checkout(_resolved(root))
    payload = {"version": 1, "digest": _DIGEST, "run_id": _RUN_ID,
               "pid": os.getpid(), "started_at": "2026-10-11T00:00:00+00:00",
               "sequence": 1}
    assert history_api.claim_full_lease(domain, checkout, payload) is True
    try:
        lease = history_api.read_full_lease(domain, checkout)
        assert lease is not None and lease.get("coverage") is None
    finally:
        history_api.release_full_lease(domain, checkout, _RUN_ID)


def test_old_verified_record_without_coverage_key_is_plain_proof(case):
    """Compatibility: a 0.5-shaped ledger record still reads, as a green
    without coverage."""
    record = {"run_id": "ab" * 16, "head": "c" * 40, "input_digest": _DIGEST,
              "compatibility": _COMPAT, "policy_digest": "dd" * 32,
              "runtime_identity": None, "scope": ".", "checkout_id": "ef" * 16,
              "root": "/tmp/x", "created_at": "2026-10-11T00:00:00+00:00"}
    parsed = verified._valid(record)
    assert parsed is not None and parsed.coverage is None


# --- C11: doctor --fix moves --cov out of every-run args ----------------------

from ptest.cli import main  # noqa: E402
from test_doctor_fix import _no_review, _write_config, _write_pytest_project  # noqa: E402


def _fixed(root: Path) -> C.Config:
    resolution = resolve_config(root)
    assert resolution.problem is None, resolution.problem
    return resolution.config


def test_fix_moves_cov_from_args_to_coverage_args(tmp_path, monkeypatch, capsys):
    """C11: --cov in args measures every run; doctor --fix makes it opt-in
    and keeps every other literal token where it was."""
    root = _write_pytest_project(tmp_path / "cov-args")
    _write_config(root, args=("-x", "--cov", "pkg", "--cov-report", "term",
                              "--no-cov-on-fail"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    runner = _fixed(root).runner
    assert runner.args == ("-x", "--no-cov-on-fail")
    assert runner.coverage_args == ("--cov", "pkg", "--cov-report", "term")


def test_fix_moves_cov_from_full_args_too(tmp_path, monkeypatch, capsys):
    """C11: full_args coverage joins the same coverage_args."""
    root = _write_pytest_project(tmp_path / "cov-full")
    _write_config(root, args=("--cov=pkg",),
                  extra_lines=())
    text = (root / ".ptest.toml").read_text(encoding="utf-8").replace(
        "full_args = []", 'full_args = ["--cov-report=xml"]')
    (root / ".ptest.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    runner = _fixed(root).runner
    assert runner.args == ()
    assert runner.full_args == ()
    assert runner.coverage_args == ("--cov=pkg", "--cov-report=xml")


def test_fix_leaves_a_declared_coverage_split_alone(tmp_path, monkeypatch, capsys):
    """C11: a config that already declares coverage_args is the user's
    choice; doctor --fix does not move anything."""
    root = _write_pytest_project(tmp_path / "declared")
    _write_config(root, args=("--cov-report=term",))
    text = (root / ".ptest.toml").read_text(encoding="utf-8").replace(
        "full_args = []", 'full_args = []\ncoverage_args = ["--cov"]')
    (root / ".ptest.toml").write_text(text, encoding="utf-8")
    before = _fixed(root).runner
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    after = _fixed(root).runner
    assert (after.args, after.coverage_args) == (before.args, before.coverage_args)


def test_fix_without_cov_adds_no_coverage_args(tmp_path, monkeypatch, capsys):
    """C11 negative: doctor never invents coverage for a project without it."""
    root = _write_pytest_project(tmp_path / "plain")
    _write_config(root, args=("-x",))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert "coverage_args" not in (root / ".ptest.toml").read_text(encoding="utf-8")


# --- C2: the --cov flag ---------------------------------------------------------

from ptest.cli import parse_argv  # noqa: E402


@pytest.mark.parametrize("argv", [
    ("--cov",), ("--full", "--cov"), ("--cov", "--full"),
    ("--full", "--cov", "--again"), ("--changed", "--cov"),
    ("--cov", "api/tests"), ("--full", "--cov", "api/tests"),
])
def test_cov_flag_parses_with_every_execution_mode(argv):
    """C2: --cov combines with every execution mode."""
    assert parse_argv(argv).coverage is True


def test_cov_flag_defaults_off_and_rejects_shadow_and_repeats():
    """C2: off unless asked; never with --shadow; never repeated."""
    assert parse_argv(("--full",)).coverage is False
    with pytest.raises(C.Problem):
        parse_argv(("--shadow", "--cov"))
    with pytest.raises(C.Problem):
        parse_argv(("--full", "--cov", "--cov"))


# --- C3: argv composition -------------------------------------------------------

from ptest.adapters import pytest as pytest_adapter  # noqa: E402


def _argv_for(root: Path, config: C.Config, execution: str, *,
              requested: bool, files: tuple = ()) -> tuple:
    from test_pytest_adapter import _attempt, _grant

    (root / "tests").mkdir(exist_ok=True)
    runner = replace(config.runner, coverage_requested=requested)
    effective = replace(config, runner=runner,
                        config_path=root / ".ptest.toml")
    mode = {"full": C.Mode.FULL, "scoped": C.Mode.SCOPED,
            "selected": C.Mode.AUTOMATIC}[execution]
    plan = C.Plan(mode=mode, execution=execution, files=files)
    return pytest_adapter.prepare_advanced(
        effective, plan, _grant(1), _attempt(1)).argv


@pytest.mark.parametrize("execution,files", [
    ("full", ()), ("scoped", ("tests/test_a.py",)),
    ("selected", ("tests/test_a.py",)),
])
def test_coverage_args_reach_argv_only_on_request(tmp_path, execution, files):
    """C3: requested → coverage options right after args; not requested →
    no coverage token at all (negative contract)."""
    config = _pytest_config(args=("-q",),
                            coverage_args=("--cov", "--cov-report", "term"))
    plain = _argv_for(tmp_path, config, execution, requested=False, files=files)
    asked = _argv_for(tmp_path, config, execution, requested=True, files=files)
    assert not any(token.startswith("--cov") for token in plain)
    index = asked.index("-q")
    assert asked[index + 1:index + 4] == ("--cov", "--cov-report", "term")
    if execution in ("scoped", "selected"):
        # C4: a partial run never applies the whole-suite gate.
        assert "--cov-fail-under=0" in asked
    else:
        assert "--cov-fail-under=0" not in asked


# --- C7 upgrade: a 0.5 baseline fails compatibility at planning time ----------

def test_compatibility_moved_with_coverage_on_request(case, monkeypatch):
    """C7: stored 0.5 baselines carry a runtime identity that counted
    pytest-cov facts. Their compatibility must differ from any 0.6 planning
    snapshot, so the planner answers incompatible-baseline (one full run)
    instead of a selected run failing the identity check (exit 70)."""
    from ptest import source
    from support import init_git_repo as _init

    domain = case.domain()
    _init(domain.root / "repo", files={"tests/test_a.py": "pass\n"},
          message="initial")
    source.ensure_fingerprint_key(domain)
    checkout = case.checkout(domain)
    object.__setattr__(checkout, "root", domain.root / "repo")
    config = case.config(checkout=checkout)
    current = source.snapshot(domain, config, None, None,
                              runtime_identity="a" * 64)
    monkeypatch.setattr(source, "_COMPATIBILITY_PROTOCOL", None)
    legacy = source.snapshot(domain, config, None, None,
                             runtime_identity="a" * 64)
    assert current.digest == legacy.digest  # content identity is unchanged
    assert current.compatibility and legacy.compatibility
    assert current.compatibility != legacy.compatibility


# --- C10/C11: narration ---------------------------------------------------------

from ptest import progress  # noqa: E402


def test_next_step_names_the_coverage_gate_when_declared():
    """C11: the handoff hint follows the project's coverage declaration."""
    assert progress.next_step(C.Status.PASSED, True) == progress.NEXT_FULL
    assert (progress.next_step(C.Status.PASSED, True, coverage=True)
            == "next: ptest --full --cov before handoff")
    assert progress.next_step(C.Status.FAILED, True, coverage=True) == progress.NEXT_FIX


def test_start_line_marks_a_run_that_measures_coverage():
    """C11: a run that measures coverage says so in its start line."""
    line = progress.format_start(project="api", runner="pytest", workers=8,
                                 scope="", full=True, coverage=True)
    assert line == "ptest: api · pytest · full suite · coverage"
    assert "coverage" not in progress.format_start(
        project="api", runner="pytest", workers=8, scope="", full=True)


@pytest.mark.parametrize("child,runner,expected", [
    (None, "pytest", "ptest: coverage not configured — add [runner] "
                     "coverage_args to .ptest.toml"),
    ("api", "pytest", "ptest: api · coverage not configured — add [runner] "
                      "coverage_args to api/.ptest.toml"),
    ("web", "vitest", "ptest: web · coverage not supported for vitest"),
])
def test_coverage_unavailable_line(child, runner, expected):
    """C10: --cov with nothing to add names the fix and runs anyway."""
    path = ".ptest.toml" if child is None else f"{child}/.ptest.toml"
    assert progress.format_coverage_unavailable(
        child=child, runner=runner, config_path=path) == expected


# --- the advanced tier is still chosen by declaring coverage -------------------

@pytest.mark.parametrize("args,coverage_args,qualified", [
    ((), ("--cov", "--cov-report", "term"), True),
    (("--cov", "--cov-report", "term"), (), True),
    (("--cov-report", "term"), ("--cov",), True),
    ((), (), False),
    ((), ("--cov",), False),
])
def test_tier_follows_the_coverage_declaration(args, coverage_args, qualified):
    """Every existing project keeps its tier: a declaration in args,
    full_args or coverage_args selects the advanced profile; requesting
    --cov for one run never changes it."""
    config = _pytest_config(args=args, coverage_args=coverage_args)
    for requested in (False, True):
        runner = replace(config.runner,
                         coverage_requested=requested and bool(coverage_args))
        profile = pytest_adapter.qualified_profile(replace(config, runner=runner))
        assert (profile is not None) is qualified
