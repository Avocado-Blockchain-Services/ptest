"""Run-output status lines: ptest says what it is doing on stderr.

Strict TDD for the 2026-09-25 run-output requirements: start/end lines,
waiting lines, setup lines, -v/--verbose and -q/--quiet. Integration
through ``case.invoke`` (a real subprocess); parse-level checks through
``cli.parse_argv``. Runner stdout/stderr stay untouched.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import platform, scheduler
from ptest.cli import parse_argv

_COMMAND_FIXTURE = Path(__file__).parent / "fixtures" / "command" / "command.py"


def _command_project(case, domain, *, args=(), full_args=(), setup=None):
    root = case.project(domain, kind="command")
    shutil.copy2(_COMMAND_FIXTURE, root / "command.py")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    lines = [
        "version = 1",
        f'project_id = "{project_id}"',
        "[runner]",
        f"launcher = {json.dumps([sys.executable, 'command.py'])}",
        f"args = {json.dumps(list(args))}",
        f"full_args = {json.dumps(list(full_args))}",
        'kind = "command"',
        'lifecycle = "cooperative-process-group"',
    ]
    if setup is not None:
        lines += [
            "[setup]",
            f"argv = {json.dumps(list(setup['argv']))}",
            f"required_paths = {json.dumps(list(setup['required_paths']))}",
        ]
    (root / ".ptest.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _stderr_lines(completed):
    return completed.stderr.decode("utf-8", "replace").splitlines()


def _ptest_lines(completed):
    return [line for line in _stderr_lines(completed) if line.startswith("ptest:")]


# ---- R1: start/end lines ----------------------------------------------------

def test_scoped_run_prints_start_and_end_lines(case):
    domain = case.domain()
    root = _command_project(case, domain)

    completed = case.invoke(domain, root, "--", "literal", "scope-token", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == ["scope-token"]
    lines = _ptest_lines(completed)
    assert lines[0] == f"ptest: {root.name} · command · 1 worker · literal scope-token"
    assert re.fullmatch(r"ptest: passed · \d+(\.\d+)?s", lines[-1]), lines


def test_full_run_names_full_suite(case):
    domain = case.domain()
    root = _command_project(case, domain, full_args=("literal",))

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0
    lines = _ptest_lines(completed)
    assert lines[0] == f"ptest: {root.name} · command · full suite"
    assert re.fullmatch(r"ptest: passed · \d+(\.\d+)?s", lines[-1]), lines


def test_failing_run_end_line_carries_hint(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("exit", "3"))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 3
    lines = _ptest_lines(completed)
    assert re.fullmatch(
        r"ptest: failed · \d+(\.\d+)?s \(exit 3\) · run with ptest -v for scheduling and setup details",
        lines[-1],
    ), lines


def test_clean_pass_end_line_has_no_hint(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    assert all("ptest -v" not in line for line in _ptest_lines(completed))


def test_monorepo_full_prints_child_end_lines_and_total(case):
    domain = case.domain()
    root = domain.root / "mono"
    root.mkdir()
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n', encoding="utf-8")
    for child in ("api", "web"):
        child_root = root / child
        child_root.mkdir()
        project_id = "ab" * 16 if child == "api" else "cd" * 16
        shutil.copy2(_COMMAND_FIXTURE, child_root / "command.py")
        (child_root / ".ptest.toml").write_text(
            "version = 1\n"
            f'project_id = "{project_id}"\n'
            "[runner]\n"
            f"launcher = {json.dumps([sys.executable, 'command.py'])}\n"
            'args = ["literal"]\n'
            'full_args = ["literal"]\n'
            'kind = "command"\n'
            'lifecycle = "cooperative-process-group"\n',
            encoding="utf-8")

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 0
    lines = _ptest_lines(completed)
    assert any(line.startswith("ptest: api · command · full suite") for line in lines)
    assert any(line.startswith("ptest: web · command · full suite") for line in lines)
    assert sum(1 for line in lines if re.fullmatch(r"ptest: passed · \S+", line)) >= 2
    assert re.fullmatch(r"ptest: total · passed · \d+(\.\d+)?s", lines[-1]), lines


# ---- R2: waiting line --------------------------------------------------------

def _hold_slot(domain, root):
    """Occupy the domain's only slot with a never-polled lease; return a release."""
    owner = platform.process_identity(os.getpid())
    assert owner is not None
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    checkout = C.CheckoutIdentity(
        project_id=project_id, checkout_id="cd" * 16, root=root)
    admission = C.AdmissionRequest(
        run_id=secrets.token_hex(16), checkout=checkout, owner=owner,
        slots=1, exclusive=True, locks=(), memory_mb=None,
        deadline=time.monotonic() + 300, fixture=True)
    ticket = scheduler.enqueue(domain, admission)
    return lambda: scheduler.cancel_pending(domain, ticket, owner)


def test_queued_run_prints_waiting_line_then_repeats_with_elapsed(case):
    domain = case.domain(slots=1, jobs=1)
    root = _command_project(case, domain, args=("literal",))
    release = _hold_slot(domain, root)
    timer = threading.Timer(25.0, release)
    timer.start()
    try:
        completed = case.invoke(domain, root, timeout=60)
    finally:
        timer.cancel()
        try:
            release()
        except Exception:
            pass

    assert completed.code == 0
    lines = _ptest_lines(completed)
    waiting = [line for line in lines if "waiting for 1 slot" in line]
    assert len(waiting) == 2, lines
    first, second = waiting
    assert first.startswith(
        "ptest: waiting for 1 slot (0 of 1 free) — in use by ")
    assert "queue timeout 30m" in first
    assert "(pid " in first  # the holder is named, never with argv/secrets
    assert "(position" not in first  # queue position is -v detail only
    assert first.endswith("run with ptest -v for scheduling and setup details")
    assert re.fullmatch(
        r"ptest: still waiting for 1 slot \([01] of 1 free\) · \d+(\.\d+)?s",
        second), lines
    assert "ptest -v" not in second
    end = lines[-1]
    assert re.fullmatch(r"ptest: passed · \d+(\.\d+)?s", end), lines


# ---- R3: setup lines ---------------------------------------------------------

def _write_setup_config(root, argv, required_paths):
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([sys.executable, 'command.py'])}\n"
        'args = ["literal"]\n'
        "full_args = []\n"
        'kind = "command"\n'
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\n"
        f"argv = {json.dumps(list(argv))}\n"
        f"required_paths = {json.dumps(list(required_paths))}\n"
        "network = false\n"
        "lifecycle_scripts = false\n",
        encoding="utf-8")


def test_setup_first_run_prints_start_and_done_lines(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    marker = "setup-out.txt"
    argv = [sys.executable, "command.py", "marker", marker]
    _write_setup_config(root, argv, [marker])

    completed = case.invoke(domain, root, timeout=30)

    assert completed.code == 0
    assert (root / marker).is_file()
    lines = _ptest_lines(completed)
    start = next(i for i, line in enumerate(lines) if line.startswith("ptest: setup:"))
    done = next(i for i, line in enumerate(lines) if line.startswith("ptest: setup done"))
    assert start < done < len(lines) - 1  # setup lines precede the end line
    assert lines[start] == f"ptest: setup: {' '.join(argv)} (first run)"
    assert re.fullmatch(r"ptest: setup done \(\d+(\.\d+)?s\)", lines[done]), lines


def test_setup_current_second_run_prints_no_setup_lines(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    marker = "setup-out.txt"
    argv = [sys.executable, "command.py", "marker", marker]
    _write_setup_config(root, argv, [marker])
    first = case.invoke(domain, root, timeout=30)
    assert first.code == 0

    second = case.invoke(domain, root, timeout=30)

    assert second.code == 0
    assert not [line for line in _ptest_lines(second) if line.startswith("ptest: setup")]


def test_setup_inputs_changed_prints_changed_reason(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    marker = "setup-out.txt"
    argv = [sys.executable, "command.py", "marker", marker]
    _write_setup_config(root, argv, [marker])
    assert case.invoke(domain, root, timeout=30).code == 0
    (root / "uv.lock").write_text("inputs changed\n", encoding="utf-8")

    completed = case.invoke(domain, root, timeout=30)

    assert completed.code == 0
    lines = _ptest_lines(completed)
    assert any(
        line == f"ptest: setup: {' '.join(argv)} (inputs changed)" for line in lines), lines


# ---- R4: -v/--verbose and -q/--quiet ------------------------------------------

def test_verbose_flag_in_option_position_is_a_ptest_option():
    parsed = parse_argv(("-v", "tests/a.py"))
    assert parsed.verbose is True
    assert parsed.mode is C.Mode.SCOPED
    assert parsed.runner_argv == ("tests/a.py",)

    parsed = parse_argv(("--verbose", "--full"))
    assert parsed.verbose is True
    assert parsed.mode is C.Mode.FULL
    assert parsed.runner_argv == ()


def test_quiet_flag_in_option_position_is_a_ptest_option():
    parsed = parse_argv(("-q", "--full"))
    assert parsed.quiet is True
    assert parsed.verbose is False

    parsed = parse_argv(("--quiet", "--workers", "2", "tests/a.py"))
    assert parsed.quiet is True
    assert parsed.workers == 2
    assert parsed.runner_argv == ("tests/a.py",)


def test_verbose_and_quiet_combine_without_error():
    parsed = parse_argv(("-v", "-q", "--full"))
    assert parsed.verbose is True
    assert parsed.quiet is True


def test_tail_verbose_after_scope_stays_runner_data():
    parsed = parse_argv(("tests/a.py", "-v"))
    assert parsed.verbose is False
    assert parsed.runner_argv == ("tests/a.py", "-v")

    parsed = parse_argv(("--", "-v"))
    assert parsed.verbose is False
    assert parsed.runner_argv == ("-v",)

    parsed = parse_argv(("-k", "-v"))
    assert parsed.verbose is False
    assert parsed.runner_argv == ("-k", "-v")


def test_verbose_run_prints_detail_lines(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "-v", timeout=20)

    assert completed.code == 0
    lines = _ptest_lines(completed)
    assert any(line.startswith("ptest: -v plan: full · mode automatic") for line in lines), lines
    assert any(line.startswith("ptest: -v admission: requested 1 slot") for line in lines), lines
    assert any(line.startswith("ptest: -v grant: 1 slot after ") for line in lines), lines
    assert "ptest: -v setup: skipped (none declared)" in lines, lines
    assert any(line.startswith("ptest: -v runner: ") for line in lines), lines
    assert any(line.startswith("ptest: -v timing: ") for line in lines), lines


def test_command_runner_never_gets_forwarded_verbose(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "-v", env={"COLUMNS": "200"}, timeout=20)

    assert completed.code == 0
    runner_lines = [line for line in _ptest_lines(completed)
                    if line.startswith("ptest: -v runner: ")]
    assert len(runner_lines) == 1
    argv_part = runner_lines[0].removeprefix("ptest: -v runner: ")
    assert argv_part == f"{sys.executable} command.py literal"


def test_tail_verbose_is_runner_data_not_ptest_option(case):
    domain = case.domain()
    root = _command_project(case, domain, args=())

    completed = case.invoke(domain, root, "--", "literal", "-v", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == ["-v"]
    assert not [line for line in _ptest_lines(completed)
                if line.startswith("ptest: -v")]


def test_quiet_suppresses_status_lines_but_keeps_runner_output(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "-q", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == []
    assert _ptest_lines(completed) == []


def test_quiet_combined_with_verbose_forwards_but_stays_quiet(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "-v", "-q", timeout=20)

    assert completed.code == 0
    assert _ptest_lines(completed) == []


def test_quiet_refusal_still_prints_with_hint(case):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8")
        + 'test_roots = ["tests"]\n',
        encoding="utf-8")

    completed = case.invoke(domain, root, "-q", timeout=20)

    assert completed.code == 2
    err = completed.stderr.decode("utf-8", "replace")
    assert "unsupported-capability" in err
    assert "run with ptest -v for scheduling and setup details" in err
    assert [line for line in err.splitlines() if line.startswith("ptest: -v")] == []


def test_forwarded_verbose_reaches_pytest(case):
    if pytest.__version__ != "9.1.1":
        pytest.skip("needs the preprovisioned pytest 9.1.1 interpreter")
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    config_path = root / ".ptest.toml"
    project_id = config_path.read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    config_path.write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-p", "no:xdist"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8")

    completed = case.invoke(domain, root, "-v", "tests/test_native.py",
                              env={"COLUMNS": "200"}, timeout=60)

    assert completed.code == 0
    # Verbose pytest prints per-test PASSED lines: the forwarded -v arrived.
    assert "PASSED" in completed.stdout.decode("utf-8", "replace")
    runner_lines = [line for line in _ptest_lines(completed)
                    if line.startswith("ptest: -v runner: ")]
    assert len(runner_lines) == 1
    argv_part = runner_lines[0].removeprefix("ptest: -v runner: ")
    assert argv_part.startswith(sys.executable)


# ---- Unit: end-line shapes with bridge counts --------------------------------

def test_end_line_with_counts_shapes():
    from ptest import progress

    counts = C.Counts(collected=812, executed=812, passed=812, failed=0,
                      skipped=0, unknown=0)
    assert progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=192.3,
        exit_code=0) == "ptest: passed · 812 tests · 3m12s"

    failed = C.Counts(collected=812, executed=809, passed=806, failed=3,
                      skipped=3, unknown=0)
    assert progress.format_end(
        C.Status.FAILED, counts=failed, duration_s=195.0, exit_code=1,
        hint=True) == ("ptest: failed · 3 failed, 806 passed · 3m15s (exit 1) "
                       "· run with ptest -v for scheduling and setup details")

    assert progress.format_end(
        C.Status.PASSED, counts=None, duration_s=4.2,
        exit_code=0) == "ptest: passed · 4.2s"

    total = C.Counts(collected=10, executed=10, passed=10, failed=0,
                     skipped=0, unknown=0)
    assert progress.format_end(
        C.Status.PASSED, counts=total, duration_s=5.1, exit_code=0,
        lead="total") == "ptest: total · passed · 10 tests · 5.1s"

    assert progress.format_end(
        C.Status.CANCELLED, counts=None, duration_s=1.2,
        exit_code=130) == "ptest: cancelled · 1.2s (exit 130)"
    assert progress.format_end(
        C.Status.INCOMPLETE, counts=None, duration_s=0.4, exit_code=70,
        hint=True) == ("ptest: incomplete · 0.4s (exit 70) · "
                       "run with ptest -v for scheduling and setup details")


def test_duration_and_timeout_shapes():
    from ptest import progress

    assert progress.format_duration(4.2) == "4.2s"
    assert progress.format_duration(192.3) == "3m12s"
    assert progress.format_duration(3723.0) == "1h2m3s"
    assert progress.format_timeout(1800.0) == "30m"
    assert progress.format_timeout(600.0) == "10m"


# ---- Parallel xdist end-line counts (persea-shaped) -----------------------------

def _xdist_project(case, domain, *, test_count=5):
    root = case.project(domain, kind="pytest")
    config_path = root / ".ptest.toml"
    project_id = config_path.read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    config_path.write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-p", "no:cacheprovider"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 8\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-n", "4"]\n', encoding="utf-8")
    (root / "tests").mkdir()
    body = "".join(
        f"def test_case_{index:02d}():\n    assert True\n" for index in range(test_count))
    (root / "tests" / "test_logger.py").write_text(body, encoding="utf-8")
    return root


def test_parallel_xdist_run_end_line_carries_counts(case):
    domain = case.domain(slots=4, jobs=4)
    root = _xdist_project(case, domain)

    completed = case.invoke(domain, root, "tests/test_logger.py", timeout=120)

    assert completed.code == 0, completed.stderr.decode()
    assert "5 passed" in completed.stdout.decode("utf-8", "replace")
    lines = _ptest_lines(completed)
    assert lines[0] == f"ptest: {root.name} · pytest · 4 workers · tests/test_logger.py"
    assert re.fullmatch(
        r"ptest: passed · 5 tests · \d+(\.\d+)?s", lines[-1]), lines


def test_parallel_xdist_failure_end_line_carries_counts(case):
    domain = case.domain(slots=4, jobs=4)
    root = _xdist_project(case, domain)
    path = root / "tests" / "test_logger.py"
    path.write_text(
        path.read_text().replace("def test_case_04():\n    assert True\n",
                                 "def test_case_04():\n    assert False\n"))

    completed = case.invoke(domain, root, "tests/test_logger.py", timeout=120)

    assert completed.code == 1, completed.stderr.decode()
    lines = _ptest_lines(completed)
    assert re.fullmatch(
        r"ptest: failed · 1 failed, 4 passed · \d+(\.\d+)?s \(exit 1\)"
        r" · run with ptest -v for scheduling and setup details",
        lines[-1]), lines


# ---- R6: json modes and hostile names ----------------------------------------

def test_result_json_mode_leaves_stdout_to_the_runner(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "--result-json", "mine.json", timeout=20)

    assert completed.code == 0
    assert completed.stdout == b"[]\n"
    assert completed.result is not None
    assert completed.result["kind"] == "run"
    assert completed.result["error"] is None
    assert _ptest_lines(completed)


def test_hostile_project_name_is_escaped(case):
    domain = case.domain()
    root = domain.root / "bad\nname\x01x"
    root.mkdir()
    project_id = "ee" * 16
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "command"\n'
        'launcher = ["true"]\n'
        "args = []\n"
        "full_args = []\n"
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")

    completed = case.invoke(domain, root, timeout=20)

    assert completed.code == 0
    raw = completed.stderr.decode("utf-8", "replace")
    assert "bad\\nname\\x01x" in raw
    assert "bad\nname" not in raw


def test_setup_failure_prints_failed_line_with_exit(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    argv = [sys.executable, "command.py", "exit", "3"]
    _write_setup_config(root, argv, ["never-created.txt"])

    completed = case.invoke(domain, root, timeout=30)

    assert completed.code != 0
    lines = _ptest_lines(completed)
    assert "ptest: setup failed (exit 3)" in lines, lines


def test_queue_timeout_refusal_keeps_code_message_and_hint_once(case):
    domain = case.domain(slots=1, jobs=1)
    root = _command_project(case, domain, args=("literal",))
    release = _hold_slot(domain, root)
    try:
        completed = case.invoke(domain, root, "--queue-timeout", "3", timeout=30)
    finally:
        try:
            release()
        except Exception:
            pass

    assert completed.code == 75
    lines = _ptest_lines(completed) + [
        line for line in completed.stderr.decode("utf-8", "replace").splitlines()
        if line.startswith("queue-timeout:")]
    waiting = [line for line in lines if "waiting for 1 slot" in line]
    assert len(waiting) == 1, lines
    assert "queue timeout 3s" in waiting[0]
    assert waiting[0].endswith("run with ptest -v for scheduling and setup details")
    refusal = [line for line in lines if line.startswith("queue-timeout:")]
    assert len(refusal) == 1, lines
    assert refusal[0] == "queue-timeout: admission queue deadline expired"
    assert [line for line in lines if line.startswith("ptest: passed")] == []


def _monorepo_root(domain, specs):
    root = domain.root / "mono-fail"
    root.mkdir()
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n', encoding="utf-8")
    for child, args in specs.items():
        child_root = root / child
        child_root.mkdir()
        shutil.copy2(_COMMAND_FIXTURE, child_root / "command.py")
        (child_root / ".ptest.toml").write_text(
            "version = 1\n"
            f'project_id = "{child.encode().hex()[:16].ljust(32, "0")}"\n'
            "[runner]\n"
            f"launcher = {json.dumps([sys.executable, 'command.py'])}\n"
            f"args = {json.dumps(list(args))}\n"
            f"full_args = {json.dumps(list(args))}\n"
            'kind = "command"\n'
            'lifecycle = "cooperative-process-group"\n',
            encoding="utf-8")
    return root


def test_monorepo_full_failure_total_carries_exit_and_hint_once(case):
    domain = case.domain()
    root = _monorepo_root(domain, {"api": ("literal",), "web": ("exit", "3")})

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 3
    lines = _ptest_lines(completed)
    web_end = [line for line in lines if line.startswith("ptest: failed")]
    assert len(web_end) == 1, lines
    assert "(exit 3)" in web_end[0]
    assert web_end[0].endswith("run with ptest -v for scheduling and setup details")
    assert re.fullmatch(
        r"ptest: total · failed · \d+(\.\d+)?s \(exit 3\)", lines[-1]), lines


def test_monorepo_scoped_start_line_shows_user_scope_from_root(case):
    domain = case.domain()
    root = _monorepo_root(domain, {"api": ("literal",), "web": ("literal",)})

    completed = case.invoke(domain, root, "api/greet", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == ["greet"]
    lines = _ptest_lines(completed)
    assert lines[0] == "ptest: api · command · 1 worker · api/greet"


def test_monorepo_full_two_failures_total_names_first_exit(case):
    domain = case.domain()
    root = _monorepo_root(domain, {"api": ("exit", "3"), "web": ("exit", "5")})

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 3
    lines = _ptest_lines(completed)
    assert sum(1 for line in lines if line.startswith("ptest: failed")) == 2
    assert re.fullmatch(
        r"ptest: total · failed · \d+(\.\d+)?s \(exit 3\)", lines[-1]), lines


def test_status_lines_are_identical_on_tty_and_non_tty():
    from ptest import progress

    class FakeStream(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self._tty = tty

        def isatty(self):
            return self._tty

    streams = [FakeStream(True), FakeStream(False)]
    for stream in streams:
        progress.emit(progress.format_start(
            project="api", runner="pytest", workers=4,
            scope="api/tests", full=False), stream=stream)
        progress.emit(progress.format_waiting(
            needed=4, free=2, limit=4, timeout_s=600.0), stream=stream)
        progress.emit(progress.format_end(
            C.Status.PASSED, counts=None, duration_s=192.3,
            exit_code=0), stream=stream)
    tty_text, non_tty_text = (stream.getvalue() for stream in streams)
    assert tty_text == non_tty_text
    assert "\r" not in tty_text  # never a spinner stream
    assert tty_text.count("\n") == 3
