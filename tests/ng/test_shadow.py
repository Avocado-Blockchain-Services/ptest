"""Task 12c shadow contracts."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import tomllib
import time

import pytest

from ptest import config as config_api
from ptest import contracts as C, history, operations, platform, reports, scheduler
from ptest import selection
from ptest.cli import parse_argv


_FIXTURE = Path(__file__).parent / "fixtures" / "shadow"
_GUARD_DRIVER = Path(__file__).parent / "fixtures" / "processes" / "guard_driver.py"
_GUARD_FAULTS = Path(__file__).parent / "fixtures" / "command" / "guard_faults.py"
_ACTIVE_GUARD = _FIXTURE / "active_guard.py"


def _coverage_launcher():
    value = os.environ.get("PTEST_TEST_PYTHON_9_1_1_COV")
    if not value:
        pytest.skip("unqualified: frozen pytest-cov tuple is not provisioned")
    checked = subprocess.run(
        [value, "-c", "import pytest,pytest_cov,coverage; print(pytest.__version__, pytest_cov.__version__, coverage.__version__)"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    if checked.returncode != 0 or checked.stdout.strip() != "9.1.1 7.1.0 7.15.0":
        pytest.skip("unqualified: frozen pytest-cov tuple is unavailable")
    return value


def test_shadow_fixture_sources_are_non_discoverable_templates():
    template_names = sorted(path.name for path in (_FIXTURE / "tests").glob("*.py"))
    assert template_names == ["alpha.py", "beta.py", "delta.py", "gamma.py"]
    assert not any(name.startswith("test_") for name in template_names)


def _shadow_project(case, domain, *, ratio=0.75):
    root = case.project(domain, kind="pytest")
    shutil.copytree(_FIXTURE / "src", root / "src")
    (root / "tests").mkdir()
    for template in sorted((_FIXTURE / "tests").glob("*.py")):
        shutil.copy2(template, root / "tests" / f"test_{template.name}")
    (root / ".gitignore").write_text(
        ".coverage\n.pytest_cache/\n__pycache__/\n.ptest-result.json\n",
        encoding="utf-8",
    )
    project_id = tomllib.loads((root / ".ptest.toml").read_text())["project_id"]
    groups = ('[{ name = "shared", sources = ["src/shared.py"], '
              'tests = ["tests/test_alpha.py"] }]')
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\nkind = \"pytest\"\n"
        f"launcher = [{json.dumps(_coverage_launcher())}]\n"
        'args = ["-p", "no:xdist", "--cov=src", "--cov-report=term"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\nworkers = 8\n'
        "[selection]\nenabled = true\nclosed_inputs = true\n"
        f"full_ratio = {ratio}\ninput_roots = [\"src\", \"tests\"]\n"
        'ignored_inputs = ["src/__pycache__", "tests/__pycache__"]\n'
        'non_input_outputs = [".coverage", ".pytest_cache", "__pycache__", ".ptest-result.json"]\n'
        f"groups = {groups}\n",
        encoding="utf-8",
    )
    return root


def _data(completed):
    assert completed.result is not None, completed.stderr.decode()
    assert completed.result["error"] is None
    return completed.result["data"]


def _published_attempt_ids(domain, checkout, run_id):
    store = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    with sqlite3.connect(store) as connection:
        return tuple(row[0] for row in connection.execute(
            "SELECT attempt_id FROM attempt_evidence WHERE run_id = ? "
            "ORDER BY attempt_id", (run_id,)))


def _commit(root):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
               GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test")
    for args in (("init",), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(("git", "-c", "core.hooksPath=" + os.devnull,
                        "-c", "commit.gpgsign=false", "-C", str(root), *args),
                       env=env, check=True, capture_output=True, timeout=10)


def _invoke(case, domain, root, *args):
    result_path = root / ".ptest-result.json"
    result_path.unlink(missing_ok=True)
    return case.invoke(domain, root, "--result-json", result_path.name,
                       *args,
                       env={"PYTHONPYCACHEPREFIX": str(domain.root / "python-cache")},
                       timeout=60)


def _quarantined_recovery_case(case, domain):
    """Build a real quarantined history and a corrected policy for S3."""
    root = _shadow_project(case, domain)
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    miss = _invoke(case, domain, root, "--shadow")
    assert miss.code == 1, miss.stderr.decode()
    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'tests = ["tests/test_alpha.py"]',
        'tests = ["tests/test_alpha.py", "tests/test_beta.py"]',
    ), encoding="utf-8")
    (root / "src" / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(root)
    corrected = _invoke(case, domain, root, "--full")
    assert corrected.code == 0, corrected.stderr.decode()
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# stable recovery\n", encoding="utf-8")
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert history.read_history(domain, checkout).selection_quarantine is not None
    return root


def _use_guard_driver(monkeypatch, **values):
    script = _GUARD_DRIVER.as_posix()
    monkeypatch.setattr(
        operations, "_GUARD_SCRIPT",
        "import os, sys; "
        "os.environ['GUARD_CONTROL_FD'] = sys.argv[1]; "
        "os.environ['GUARD_MANIFEST_FD'] = sys.argv[2]; "
        f"exec(compile(open({script!r}).read(), 'guard_driver', 'exec'))",
    )
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))


def _use_guard_fault(monkeypatch, value):
    script = _GUARD_FAULTS.as_posix()
    monkeypatch.setattr(
        operations, "_GUARD_SCRIPT",
        f"exec(compile(open({script!r}).read(), 'guard_faults', 'exec'))",
    )
    monkeypatch.setenv("TEST_GUARD_FAULT", value)


def _use_active_guard(monkeypatch, active_path, release_path):
    script = _ACTIVE_GUARD.as_posix()
    monkeypatch.setattr(
        operations, "_GUARD_SCRIPT",
        "import os, sys; "
        "os.environ['GUARD_CONTROL_FD'] = sys.argv[1]; "
        "os.environ['GUARD_MANIFEST_FD'] = sys.argv[2]; "
        f"exec(compile(open({script!r}).read(), 'active_guard', 'exec'))",
    )
    monkeypatch.setenv("SHADOW_ACTIVE_MARKER", str(active_path))
    monkeypatch.setenv("SHADOW_ACTIVE_RELEASE", str(release_path))


def _shadow_direct(case, domain, root):
    config = config_api.resolve_config(root).config
    return operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC, shadow=True))


@pytest.mark.parametrize("argv", [
    ("--shadow",), ("--shadow", "--changed"),
    ("--changed", "--shadow"), ("changed", "--shadow"),
])
def test_all_automatic_shadow_spellings_parse(argv):
    parsed = parse_argv(argv)
    assert parsed.shadow is True
    assert parsed.mode is C.Mode.AUTOMATIC


@pytest.mark.parametrize("argv", [
    ("--shadow", "--full"), ("--shadow", "tests/test_a.py"),
])
def test_shadow_cannot_be_combined_with_full_or_scoped_tail(argv):
    with pytest.raises(C.Problem) as error:
        parse_argv(argv)
    assert error.value.code == "invalid-config"


def _reason(code):
    return C.Reason(code=code, message="synthetic S3 boundary")


def _history_deadline_evidence(case):
    attempt = C.AttemptResult(
        attempt_id="a001", phase="execution", status=C.Status.PASSED,
        raw_exit_code=0, final_exit_code=0, source_valid=True,
        inventory_complete=True,
    )
    return C.AttemptEvidence(
        attempt_id="a001", result=attempt,
        inventory=case.inventory(("tests/test_a.py",), outcome="passed"),
        terminal_complete=True, parallel_identity=False,
        runtime_identity="66" * 32,
    )


def _history_deadline_attempt(*, raw=-15, final=143):
    return C.AttemptResult(
        attempt_id="a002", phase="execution", status=C.Status.INCOMPLETE,
        raw_exit_code=raw, final_exit_code=final, source_valid=True,
        inventory_complete=False,
    )


def _history_shadow_result(case, checkout, attempts, *, reasons=()):
    snapshot = case.snapshot(digest="11" * 32, compatibility="compat-v1")
    plan = C.Plan(
        mode=C.Mode.SHADOW, execution="selected",
        files=("tests/test_a.py",), input_digest=snapshot.digest,
        compatibility=snapshot.compatibility,
    )
    return replace(case.result(
        sequence=1, run_id="7" * 32, mode=C.Mode.SHADOW,
        status="incomplete", plan=plan,
        input_before=snapshot, input_after=snapshot,
        policy_digest="22" * 32, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, runner_exit_code=0, exit_code=70,
        full_gate_eligible=False, attempts=attempts, reasons=reasons,
    ), plan=plan)


def _history_probe_result(case, checkout, attempts):
    snapshot = case.snapshot(digest="11" * 32, compatibility="compat-v1")
    plan = C.Plan(
        mode=C.Mode.PROBE, execution="scoped", files=(),
        input_digest=snapshot.digest, compatibility=snapshot.compatibility,
    )
    return replace(case.result(
        sequence=1, run_id="8" * 32, mode=C.Mode.PROBE,
        status="incomplete", plan=plan,
        input_before=snapshot, input_after=snapshot,
        policy_digest="22" * 32, project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, runner_exit_code=0, exit_code=70,
        full_gate_eligible=False, attempts=attempts,
    ), plan=plan)


def test_history_allows_only_authenticated_shadow_deadline_no_report(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    selected = _history_deadline_evidence(case)
    result = _history_shadow_result(
        case, checkout, (selected.result, _history_deadline_attempt()),
        reasons=(C.Reason(code="execution-timeout", message="deadline"),),
    )
    comparison = C.ShadowComparison(
        selected=selected, full=None, verdict="incomplete",
        expected_quarantine=None,
    )

    published = history.publish_shadow_outcome(
        domain, checkout, result, comparison)

    assert published.committed is True


@pytest.mark.parametrize("case_builder", [
    "missing-predecessor", "positive-signal", "foreign-phase", "probe-mode",
])
def test_history_rejects_unauthenticated_no_report_attempts(case, case_builder):
    domain = case.domain()
    checkout = case.checkout(domain)
    selected = _history_deadline_evidence(case)
    execution = _history_deadline_attempt()
    if case_builder == "missing-predecessor":
        result = _history_shadow_result(
            case, checkout, (execution,),
            reasons=(C.Reason(code="execution-timeout", message="deadline"),),
        )
        publish = history.publish_shadow_outcome
        args = (domain, checkout, result, C.ShadowComparison(
            selected=None, full=None, verdict="incomplete",
            expected_quarantine=None))
    elif case_builder == "positive-signal":
        result = _history_shadow_result(
            case, checkout, (selected.result,
                             replace(execution, raw_exit_code=1,
                                     final_exit_code=1)),
            reasons=(C.Reason(code="execution-timeout", message="deadline"),),
        )
        publish = history.publish_shadow_outcome
        args = (domain, checkout, result, C.ShadowComparison(
            selected=selected, full=None, verdict="incomplete",
            expected_quarantine=None))
    elif case_builder == "foreign-phase":
        foreign = replace(selected.result, phase="discovery")
        result = _history_shadow_result(
            case, checkout, (foreign, execution),
            reasons=(C.Reason(code="execution-timeout", message="deadline"),),
        )
        publish = history.publish_shadow_outcome
        args = (domain, checkout, result, C.ShadowComparison(
            selected=None, full=None, verdict="incomplete",
            expected_quarantine=None))
    else:
        result = _history_probe_result(case, checkout,
                                       (selected.result, execution))
        publish = history.publish_probe_outcome
        args = (domain, checkout, result, ())

    with pytest.raises(ValueError):
        publish(*args)


def test_s3_first_gate_invalidation_is_not_run_and_starts_no_attempts():
    outcome = operations._shadow_outcome(
        raw_codes=(), gate_count=1, decision_reason=_reason("changed-during-run"))
    assert outcome == (C.Status.NOT_RUN, 2, "ptest", None, None)
    assert history.derive_shadow_verdict(None, None) == "incomplete"


def test_s3_post_a001_invalidation_is_incomplete_70_and_never_a_miss():
    outcome = operations._shadow_outcome(
        raw_codes=(0,), gate_count=2,
        decision_reason=_reason("changed-during-run"))
    assert outcome == (C.Status.INCOMPLETE, 70, "ptest", None, 0)
    assert history.derive_shadow_verdict(None, None) == "incomplete"


@pytest.mark.parametrize("reason_code", [
    "execution-timeout", "attempt-decision-timeout", "report-invalid",
    "state-unavailable",
])
def test_s3_timeout_report_and_handoff_are_incomplete_and_not_a_miss(reason_code):
    outcome = operations._shadow_outcome(
        raw_codes=(0,), gate_count=2, decision_reason=_reason(reason_code),
        handoff_complete=False if reason_code == "state-unavailable" else True)
    assert outcome[0:3] == (C.Status.INCOMPLETE, 70, "ptest")
    assert history.derive_shadow_verdict(None, None) == "incomplete"


def test_s3_cancellation_uses_signal_exit_and_never_launches_after_stop():
    outcome = operations._shadow_outcome(
        raw_codes=(0,), gate_count=2, cancellation=signal.SIGINT)
    assert outcome[0:3] == (C.Status.CANCELLED, 128 + signal.SIGINT, "signal")
    assert operations._shadow_outcome(
        raw_codes=(), gate_count=1, cancellation=signal.SIGINT)[0:3] == (
            C.Status.CANCELLED, 128 + signal.SIGINT, "signal")


def test_shadow_queued_cancellation_returns_cancelled_and_releases_ticket(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    blocker_root = case.project(domain)
    blocker_config = config_api.resolve_config(blocker_root).config
    blocker_checkout = operations._checkout(blocker_config)
    blocker = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=os.urandom(16).hex(), checkout=blocker_checkout,
        owner=platform.process_identity(os.getpid()), slots=1,
        exclusive=True, fixture=True, deadline=time.monotonic() + 30))
    assert scheduler.poll(domain, blocker).state is C.LeaseState.GRANTED
    original_poll = scheduler.poll
    signalled = False

    def signal_while_queued(*args):
        nonlocal signalled
        state = original_poll(*args)
        if not signalled and state.state is C.LeaseState.QUEUED:
            signalled = True
            signal.raise_signal(signal.SIGINT)
        return state

    monkeypatch.setattr(scheduler, "poll", signal_while_queued)
    config = config_api.resolve_config(root).config
    try:
        result = operations.execute(
            domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC, shadow=True))
    finally:
        scheduler.cancel_pending(
            domain, blocker, platform.process_identity(os.getpid()))
    assert (result.status, result.exit_code, result.exit_origin) == (
        C.Status.CANCELLED, 130, "signal")


def test_shadow_failed_setup_is_first_attempt_with_setup_timing_and_exit(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    python = json.dumps(os.sys.executable)
    config = root / ".ptest.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "[setup]\n"
        + f"argv = [{python}, \"-c\", \"import sys; sys.exit(0)\"]\n"
        + 'required_paths = ["src/shared.py"]\n'
        + "network = false\n"
        + "lifecycle_scripts = false\n",
        encoding="utf-8",
    )
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()
    (root / "src" / "shared.py").write_text("VALUE = 1\n# setup\n", encoding="utf-8")
    resolved = config_api.resolve_config(root).config
    raw_elapsed = []
    original_run_guard = operations._run_guard

    def measured_run_guard(*args, **kwargs):
        observed = original_run_guard(*args, **kwargs)
        raw_elapsed.append(observed[2])
        return observed

    monkeypatch.setattr(operations, "_run_guard", measured_run_guard)
    def failing_setup(config, checkout, request, prepared, domain, grant, attempt):
        return replace(
            prepared,
            argv=(os.sys.executable, "-c", "import sys; sys.exit(7)"),
            report_path=None,
        )
    monkeypatch.setattr(operations, "_setup_prepared", failing_setup)
    result = operations.execute(
        domain, resolved, C.RunRequest(mode=C.Mode.AUTOMATIC, shadow=True))
    assert (result.status, result.exit_code, result.exit_origin,
            result.runner_exit_code) == (C.Status.FAILED, 7, "setup", 7), result
    assert [(item.phase, item.attempt_id) for item in result.attempts] == [
        ("setup", "a001"), ("execution", "a001"), ("execution", "a002")]
    assert [item.status for item in result.attempts] == [
        C.Status.FAILED, C.Status.NOT_RUN, C.Status.NOT_RUN]
    assert result.attempts[0].phase == "setup"
    assert result.attempts[0].raw_exit_code == 7
    assert result.attempts[0].final_exit_code == 7
    assert result.attempts[0].timings is not None
    assert result.attempts[0].timings.setup_s is not None
    assert result.timings is not None
    assert result.timings.setup_s == result.attempts[0].timings.setup_s
    assert result.timings.execution_s == pytest.approx(
        max(0.0, raw_elapsed[0] - result.timings.setup_s))


def test_shadow_successful_setup_preserves_phase_identity_and_history(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    python = json.dumps(os.sys.executable)
    config = root / ".ptest.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "[setup]\n"
        + f"argv = [{python}, \"-c\", \"import sys; sys.exit(0)\"]\n"
        + 'required_paths = ["src/shared.py"]\n'
        + "network = false\n"
        + "lifecycle_scripts = false\n",
        encoding="utf-8",
    )
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()
    monkeypatch.setattr(operations, "_stored_setup_fingerprint",
                        lambda *_args, **_kwargs: "<invalid>")
    original_setup_prepared = operations._setup_prepared
    def require_setup(*args, **kwargs):
        prepared = original_setup_prepared(*args, **kwargs)
        assert prepared is not None
        return prepared
    monkeypatch.setattr(operations, "_setup_prepared", require_setup)
    original_run_guard = operations._run_guard
    captured_frames = []
    def capture_guard(*args, **kwargs):
        observed = original_run_guard(*args, **kwargs)
        captured_frames.append(observed[1])
        return observed
    monkeypatch.setattr(operations, "_run_guard", capture_guard)
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# setup-success\n", encoding="utf-8")
    result = _shadow_direct(case, domain, root)
    assert result.status is C.Status.PASSED, result
    assert captured_frames[0].setup_facts is not None
    assert [(item.phase, item.attempt_id) for item in result.attempts] == [
        ("setup", "a001"), ("execution", "a001"), ("execution", "a002")]
    assert [item.status for item in result.attempts] == [
        C.Status.PASSED, C.Status.PASSED, C.Status.PASSED]
    checkout = operations._checkout(config_api.resolve_config(root).config)
    store = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    with sqlite3.connect(store) as connection:
        rows = connection.execute(
            "SELECT attempt_id FROM attempt_evidence "
            "WHERE run_id = (SELECT run_id FROM runs ORDER BY sequence DESC LIMIT 1) "
            "ORDER BY attempt_id"
        ).fetchall()
    assert rows == [("a001",), ("a002",)]


def test_s3_negative_runner_code_maps_to_shell_exit_without_losing_raw_code():
    outcome = operations._shadow_outcome(raw_codes=(-signal.SIGTERM,), gate_count=2)
    assert outcome == (C.Status.FAILED, 128 + signal.SIGTERM, "signal",
                       signal.SIGTERM, -signal.SIGTERM)


def test_s1_selected_native_failure_still_runs_full_and_preserves_first_nonzero(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()

    (root / "tests" / "test_alpha.py").write_text(
        "from src.shared import VALUE\n\n\ndef test_alpha():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    shadow = _invoke(case, domain, root, "--shadow")
    data = _data(shadow)
    assert shadow.code == 1
    assert data["plan"]["execution"] == "selected"
    assert [item["attempt_id"] for item in data["attempts"]] == ["a001", "a002"]
    assert [item["status"] for item in data["attempts"]] == ["failed", "failed"]
    assert [item["raw_exit_code"] for item in data["attempts"]] == [1, 1]
    assert data["runner_exit_code"] == 1
    assert data["exit_code"] == 1


def test_s2_four_file_bad_policy_shadow_quarantines_and_recovers(case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    _commit(root)
    baseline = _invoke(case, domain, root, "--full")
    assert baseline.code == 0, baseline.stderr.decode()
    assert _data(baseline)["baseline_published"] is True

    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    shadow = _invoke(case, domain, root, "--shadow")
    shadow_data = _data(shadow)
    assert shadow_data["mode"] == "shadow"
    assert shadow_data["plan"]["execution"] == "selected"
    assert shadow_data["plan"]["files"] == ["tests/test_alpha.py"]
    assert shadow_data["attempts"][0]["status"] == "passed"
    assert shadow_data["attempts"][1]["status"] == "failed"
    assert shadow_data["runner_exit_code"] == 1
    assert any(item["code"] == "selection-shadow-quarantine"
               for item in shadow_data["reasons"])
    checkout = operations._checkout(config_api.resolve_config(root).config)
    shadow_history = history.read_history(domain, checkout)
    assert shadow_history.baseline is not None
    assert {item.file for item in shadow_history.baseline.inventory.tests} == {
        "tests/test_alpha.py", "tests/test_beta.py",
        "tests/test_gamma.py", "tests/test_delta.py",
    }
    assert shadow_history.selection_quarantine is not None
    assert shadow_history.selection_quarantine.verdict == "suspected-miss"

    automatic = _invoke(case, domain, root, "--changed")
    automatic_data = _data(automatic)
    assert automatic_data["plan"]["execution"] == "full"
    assert automatic_data["runner_exit_code"] == 1

    focused = _invoke(case, domain, root, "--", "tests/test_beta.py")
    assert focused.code == 1

    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'tests = ["tests/test_alpha.py"]',
        'tests = ["tests/test_alpha.py", "tests/test_beta.py"]',
    ), encoding="utf-8")
    (root / "src" / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(root)
    corrected = _invoke(case, domain, root, "--full")
    assert corrected.code == 0
    (root / "src" / "shared.py").write_text("VALUE = 1\n# benign\n", encoding="utf-8")
    recovered = _invoke(case, domain, root, "--shadow")
    recovered_data = _data(recovered)
    assert recovered.code == 0
    assert recovered_data["runner_exit_code"] == 0
    assert recovered_data["plan"]["execution"] == "selected"
    assert recovered_data["plan"]["files"] == [
        "tests/test_alpha.py", "tests/test_beta.py"]
    assert all(item["status"] == "passed" for item in recovered_data["attempts"])
    recovered_history = history.read_history(domain, checkout)
    assert recovered_history.selection_quarantine is None


def test_s2_source_fix_without_policy_correction_does_not_clear_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _invoke(case, domain, root, "--shadow").code == 1
    (root / "src" / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# unrelated source edit\n", encoding="utf-8")
    recovery = _invoke(case, domain, root, "--shadow")
    assert recovery.code == 0
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s2_unrelated_history_health_blocks_quarantine_clear(case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _invoke(case, domain, root, "--shadow").code == 1
    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'tests = ["tests/test_alpha.py"]',
        'tests = ["tests/test_alpha.py", "tests/test_beta.py"]',
    ), encoding="utf-8")
    (root / "src" / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# benign\n", encoding="utf-8")
    checkout = operations._checkout(config_api.resolve_config(root).config)
    marker = domain.root / "checkouts" / checkout.checkout_id / "history-disabled.json"
    marker.write_text('{"version": 1, "code": "state-unavailable"}',
                      encoding="utf-8")
    blocked = _invoke(case, domain, root, "--shadow")
    assert blocked.code == 2
    assert "unsupported-capability" in blocked.stderr.decode()
    marker.unlink()
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s3_source_edit_at_second_gate_stops_full_and_retains_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    original_snapshot = operations.source.snapshot
    calls = 0

    def edit_when_second_gate_is_validated(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            (root / "src" / "shared.py").write_text(
                "VALUE = 1\n# edited at gate\n", encoding="utf-8")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(
        operations.source, "snapshot", edit_when_second_gate_is_validated)
    result = _shadow_direct(case, domain, root)
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70), result
    assert result.attempts[0].status is C.Status.PASSED
    assert result.attempts[1].status is C.Status.NOT_RUN
    assert any(reason.code == "changed-during-run" for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s3_source_edit_while_full_child_is_active_keeps_raw_evidence_and_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    release = domain.root / "shadow-child-release"
    active_test = (
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from src.shared import VALUE\n\n"
        "def test_beta_requires_original_shared_contract():\n"
        "    if os.environ.get('PTEST_ATTEMPT_ID') == 'a002':\n"
        "        release = Path(os.environ['SHADOW_CHILD_RELEASE'])\n"
        "        while not release.exists():\n"
        "            time.sleep(0.01)\n"
        "    assert VALUE == 1\n"
    )
    (root / "tests" / "test_beta.py").write_text(active_test, encoding="utf-8")
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _invoke(case, domain, root, "--shadow").code == 1
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# stable recovery\n", encoding="utf-8")
    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "full_ratio = 0.75", "full_ratio = 0.80"), encoding="utf-8")
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    checkout = operations._checkout(config_api.resolve_config(root).config)
    recovered_history = history.read_history(domain, checkout)
    assert recovered_history.selection_quarantine is not None
    active_marker = domain.root / "shadow-child-active"
    _use_active_guard(monkeypatch, active_marker, release)
    monkeypatch.setenv("SHADOW_CHILD_RELEASE", str(release))
    errors = []
    active = threading.Event()
    finished = threading.Event()

    def mutate_active_child():
        try:
            deadline = time.monotonic() + 45
            while (not active_marker.exists()
                   or active_marker.read_text(encoding="ascii") != "2"):
                if finished.is_set():
                    return
                assert time.monotonic() < deadline
                time.sleep(0.01)
            (root / "src" / "shared.py").write_text(
                "VALUE = 1\n# changed-while-full-active\n", encoding="utf-8")
            active.set()
            release.touch()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=mutate_active_child)
    worker.start()
    try:
        result = _shadow_direct(case, domain, root)
    finally:
        finished.set()
        worker.join(timeout=45)
    assert not errors
    assert [(item.attempt_id, item.status.value) for item in result.attempts] == [
        ("a001", "passed"), ("a002", "passed")], (
            result.reasons, result.attempts, result.plan)
    assert active.is_set()
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert (result.status, result.exit_code) == (C.Status.INCOMPLETE, 70), result
    assert [item.raw_exit_code for item in result.attempts] == [0, 0]
    assert any(reason.code == "changed-during-run" for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None
    assert _published_attempt_ids(domain, checkout, result.run_id) == (
        "a001", "a002")


def test_s3_compound_deadline_expires_while_full_child_is_active(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _shadow_project(case, domain)
    release = domain.root / "deadline-child-release"
    active_test = (
        "import os\n"
        "import time\n"
        "from src.shared import VALUE\n\n"
        "def test_beta_requires_original_shared_contract():\n"
        "    if os.environ.get('PTEST_ATTEMPT_ID') == 'a002':\n"
        "        time.sleep(5)\n"
        "    assert VALUE == 1\n"
    )
    (root / "tests" / "test_beta.py").write_text(active_test, encoding="utf-8")
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert _invoke(case, domain, root, "--shadow").code == 1
    (root / "src" / "shared.py").write_text(
        "VALUE = 1\n# stable recovery\n", encoding="utf-8")
    config = root / ".ptest.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "full_ratio = 0.75", "full_ratio = 0.80"), encoding="utf-8")
    _commit(root)
    assert _invoke(case, domain, root, "--full").code == 0
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert history.read_history(domain, checkout).selection_quarantine is not None
    active_marker = domain.root / "deadline-child-active"
    _use_active_guard(monkeypatch, active_marker, release)
    monkeypatch.setenv("SHADOW_ACTIVE_ADVANCE", "601")
    errors = []
    active = threading.Event()
    finished = threading.Event()

    def release_full_child():
        try:
            deadline = time.monotonic() + 45
            while (not active_marker.exists()
                   or active_marker.read_text(encoding="ascii") != "2"):
                if finished.is_set():
                    return
                assert time.monotonic() < deadline
                time.sleep(0.01)
            active.set()
            release.touch()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=release_full_child)
    worker.start()
    try:
        result = _shadow_direct(case, domain, root)
    finally:
        finished.set()
        worker.join(timeout=45)
    assert not errors
    assert active.is_set()
    assert result.status is C.Status.INCOMPLETE, result
    assert result.exit_code == 70
    assert result.attempts[0].raw_exit_code == 0
    assert (result.attempts[1].phase, result.attempts[1].status,
            result.attempts[1].raw_exit_code,
            result.attempts[1].final_exit_code) == (
                "execution", C.Status.INCOMPLETE, -signal.SIGTERM,
                128 + signal.SIGTERM)
    assert any(reason.code in {"execution-timeout", "state-unavailable"}
               for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None
    assert _published_attempt_ids(domain, checkout, result.run_id) == ("a001",), (
        result.reasons, result.attempts)
    store = domain.root / "checkouts" / checkout.checkout_id / "history.sqlite3"
    with sqlite3.connect(store) as connection:
        summary_text = connection.execute(
            "SELECT summary FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
    summary = json.loads(summary_text)
    assert (summary["status"], summary["exit_code"]) == ("incomplete", 70)
    assert [(item["attempt_id"], item["phase"], item["status"],
             item["raw_exit_code"], item["final_exit_code"])
            for item in summary["attempts"]] == [
                ("a001", "execution", "passed", 0, 0),
                ("a002", "execution", "incomplete", -15, 143),
            ]


def test_shadow_publication_not_committed_forces_incomplete_without_throwing(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)

    def rejected_publication(*_args, **_kwargs):
        return C.PublishResult(
            committed=False, baseline_published=False,
            selection_disabled=True,
            reasons=(C.Reason(
                code="coordinator-unavailable",
                message="history publication was rejected"),),
        )

    monkeypatch.setattr(history, "publish_shadow_outcome", rejected_publication)
    result = _shadow_direct(case, domain, root)

    assert (result.status, result.exit_code, result.exit_origin) == (
        C.Status.INCOMPLETE, 70, "ptest")
    assert any(reason.code == "coordinator-unavailable"
               for reason in result.reasons)

    (root / "src" / "shared.py").write_text("VALUE = 2\n", encoding="utf-8")
    failed = _shadow_direct(case, domain, root)
    assert (failed.status, failed.exit_code, failed.exit_origin,
            failed.runner_exit_code) == (
                C.Status.INCOMPLETE, 1, "runner", 1)


def test_s3_missing_handoff_is_incomplete_and_retains_raw_outcomes_and_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _use_guard_driver(monkeypatch, GUARD_FAILURE="drain")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    _commit(root)
    result = _shadow_direct(case, domain, root)
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert result.runner_exit_code == 0
    assert [item.raw_exit_code for item in result.attempts] == [0, 0]
    assert any(reason.code in {"state-unavailable", "ownership-uncertain"}
               for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s3_guard_fact_problem_is_incomplete_and_preserves_problem_reason(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _use_guard_fault(monkeypatch, "problem:ownership-uncertain:0")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    result = _shadow_direct(case, domain, root)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert all(item.status is C.Status.INCOMPLETE for item in result.attempts)
    assert any(reason.code == "ownership-uncertain"
               for reason in result.reasons)


def test_s3_invalid_report_is_incomplete_and_retains_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    original_consume = reports.consume_attempt_report
    consumed = 0

    def missing_second_report(binding):
        nonlocal consumed
        consumed += 1
        if consumed == 2:
            raise C.Problem(
                code="report-invalid", message="missing full report",
                phase="execution")
        return original_consume(binding)

    monkeypatch.setattr(reports, "consume_attempt_report", missing_second_report)
    result = _shadow_direct(case, domain, root)
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert [item.raw_exit_code for item in result.attempts] == [0, 0]
    assert any(reason.code == "report-invalid" for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s3_compound_deadline_invalidation_is_incomplete_and_keeps_first_raw_outcome(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _use_guard_driver(monkeypatch, GUARD_ADVANCE_AFTER_FACTS="601")
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    result = _shadow_direct(case, domain, root)
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code == 70
    assert result.runner_exit_code == 0
    assert result.attempts[0].raw_exit_code == 0
    assert result.attempts[1].status is C.Status.NOT_RUN
    assert any(reason.code in {"execution-timeout", "state-unavailable"}
               for reason in result.reasons)
    assert history.read_history(domain, checkout).selection_quarantine is not None


def test_s3_stable_two_pass_shadow_with_fake_elapsed_time_clears_quarantine(
        case, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _use_guard_driver(monkeypatch, GUARD_ADVANCE_AFTER_FACTS="31")
    monkeypatch.setattr(operations, "_FRAME_TIMEOUT_S", 40.0)
    domain = case.domain(slots=1, jobs=1)
    root = _quarantined_recovery_case(case, domain)
    _commit(root)
    result = _shadow_direct(case, domain, root)
    checkout = operations._checkout(config_api.resolve_config(root).config)
    assert (result.status, result.exit_code, result.runner_exit_code) == (
        C.Status.PASSED, 0, 0), result
    assert [item.raw_exit_code for item in result.attempts] == [0, 0]
    assert history.read_history(domain, checkout).selection_quarantine is None


def test_selection_shadow_equality_ratio_widens_to_full(case):
    config = case.config(selection_enabled=True, closed_inputs=True)
    config = config.__class__(
        runner=config.runner, setup=config.setup, resources=config.resources,
        selection=C.SelectionPolicy(
            enabled=True, closed_inputs=True, full_ratio=0.50,
            groups=(C.Group(name="g", sources=("src/shared.py",),
                            tests=("tests/alpha.py", "tests/beta.py")),),
        ), project_id=config.project_id, config_path=case.base / ".ptest.toml",
    )
    snapshot = C.InputSnapshot(
        digest="aa" * 32, compatibility="compat", head="b" * 40,
        baseline_head="b" * 40,
        clean=True,
        changes=(C.Change(old="src/shared.py", new="src/shared.py", kind="modified"),),
    )
    inventory = C.Inventory(
        adapter="pytest", version="9.1.1", complete=True,
        tests=tuple(C.TestRecord(id=f"tests/{name}.py::test", file=f"tests/{name}.py",
                                 outcome=C.Outcome.PASSED)
                    for name in ("alpha", "beta", "gamma", "delta")),
        digest="cc" * 32,
    )
    baseline = C.Baseline(
        run_id="dd" * 16, head="b" * 40, input_digest="ee" * 32,
        compatibility="compat", inventory=inventory,
        policy_digest=hashlib.sha256(repr(config.selection).encode()).hexdigest(),
        created_at="2026-01-01T00:00:00Z",
    )
    plan = selection.choose_plan(config, snapshot,
                                 C.HistoryView(baseline=baseline),
                                 C.RunRequest(mode=C.Mode.AUTOMATIC))
    assert plan.execution == "full"
    assert plan.files == ()
    assert plan.reasons[0].code == "full-gate-obligation"
