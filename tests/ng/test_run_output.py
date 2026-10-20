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

import support

from ptest import contracts as C
from ptest import platform, progress, scheduler
from ptest.cli import parse_argv

_COMMAND_FIXTURE = Path(__file__).parent / "fixtures" / "command" / "command.py"


def _command_project(case, domain, *, args=(), full_args=(), setup=None,
                     launcher=None):
    root = case.project(domain, kind="command")
    shutil.copy2(_COMMAND_FIXTURE, root / "command.py")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1)[1].split('"', 1)[0]
    lines = [
        "version = 1",
        f'project_id = "{project_id}"',
        "[runner]",
        f"launcher = {json.dumps(list(launcher) if launcher is not None else [sys.executable, 'command.py'])}",
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

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 3
    lines = _ptest_lines(completed)
    assert re.fullmatch(
        r"ptest: failed · \d+(\.\d+)?s \(exit 3\) · run with ptest -v for scheduling and setup details",
        lines[-1],
    ), lines


def test_clean_pass_end_line_has_no_hint(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "--full", timeout=20)

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
        completed = case.invoke(domain, root, "--full", timeout=60)
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
    assert "queue timeout 4h" in first
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

    completed = case.invoke(domain, root, "--full", timeout=30)

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
    first = case.invoke(domain, root, "--full", timeout=30)
    assert first.code == 0

    second = case.invoke(domain, root, "--full", timeout=30)

    assert second.code == 0
    assert not [line for line in _ptest_lines(second) if line.startswith("ptest: setup")]


def test_setup_inputs_changed_prints_changed_reason(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    marker = "setup-out.txt"
    argv = [sys.executable, "command.py", "marker", marker]
    _write_setup_config(root, argv, [marker])
    assert case.invoke(domain, root, "--full", timeout=30).code == 0
    (root / "uv.lock").write_text("inputs changed\n", encoding="utf-8")

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 0
    lines = _ptest_lines(completed)
    assert any(
        line == f"ptest: setup: {' '.join(argv)} (inputs changed)" for line in lines), lines


def test_setup_writing_ignored_outputs_is_not_changed_during_run(case):
    """Setup may write gitignored outputs (husky's prepare writes .husky/_).

    fullon2 web: the first `pnpm install` ran husky, which created the
    ignored .husky/_ directory between the pre-launch and gate snapshots,
    and the run ended "changed-during-run: path classes: ignored" (exit 70).
    """
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    argv = [sys.executable, "-c",
            "import pathlib; pathlib.Path('setup-out.txt').write_text('x'); "
            "hooks = pathlib.Path('.husky/_'); hooks.mkdir(parents=True); "
            "(hooks / 'pre-commit').write_text('hook')"]
    _write_setup_config(root, argv, ["setup-out.txt"])
    support.init_git_repo(root, files={".gitignore": "setup-out.txt\n.husky/_/\n"})

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 0, completed.stderr.decode()
    assert (root / ".husky" / "_" / "pre-commit").exists()
    assert b"changed-during-run" not in completed.stderr


def test_setup_rewriting_tracked_source_is_still_changed_during_run(case):
    """Only ignored outputs are setup's own; a rewritten tracked file is not."""
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    argv = [sys.executable, "-c",
            "import pathlib; pathlib.Path('setup-out.txt').write_text('x'); "
            "pathlib.Path('src.txt').write_text('rewritten')"]
    _write_setup_config(root, argv, ["setup-out.txt"])
    support.init_git_repo(root, files={".gitignore": "setup-out.txt\n",
                                       "src.txt": "original"})

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code != 0
    assert b"changed-during-run" in completed.stderr


def test_tests_writing_gitignored_output_still_pass(case):
    """fastapi: tests write coverage/ data and log.txt, both gitignored."""
    domain = case.domain()
    root = _command_project(case, domain, args=("marker", "log.txt"))
    support.init_git_repo(root, files={".gitignore": "log.txt\n"})

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 0, completed.stderr.decode()
    assert (root / "log.txt").exists()
    assert b"changed-during-run" not in completed.stderr


def test_tests_writing_untracked_source_still_invalidate_the_run(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("marker", "generated.py"))
    support.init_git_repo(root)

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code == 70
    assert b"changed-during-run" in completed.stderr


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


def test_verbose_run_prints_detail_lines(case, capsys):
    # The automatic engine's -v lines are pinned in-process: bare `ptest`
    # now routes through the import graph, so the CLI no longer drives
    # AUTOMATIC for it (see test_changed_default.py for the new route).
    from ptest import config as config_api
    from ptest import operations

    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    config = config_api.resolve_config(root).config
    assert config is not None

    result = operations.execute(
        domain, config,
        C.RunRequest(mode=C.Mode.AUTOMATIC, verbose=True))

    assert result.exit_code == 0
    lines = capsys.readouterr().err.splitlines()
    assert any(line.startswith("ptest: -v plan: full · mode automatic") for line in lines), lines
    assert any(line.startswith("ptest: -v admission: requested 1 slot") for line in lines), lines
    assert any(line.startswith("ptest: -v grant: 1 slot after ") for line in lines), lines
    assert "ptest: -v setup: skipped (none declared)" in lines, lines
    assert any(line.startswith("ptest: -v runner: ") for line in lines), lines
    assert any(line.startswith("ptest: -v timing: ") for line in lines), lines


def test_command_runner_never_gets_forwarded_verbose(case):
    domain = case.domain()
    # Short stable interpreter: the 110-column display clamp would trim a
    # worktree-qualified venv path, hiding the literal tail under test.
    launcher = ("/usr/bin/python3", "command.py")
    root = _command_project(case, domain, args=("literal",), launcher=launcher)

    completed = case.invoke(domain, root, "-v", "--full", env={"COLUMNS": "200"}, timeout=20)

    assert completed.code == 0
    runner_lines = [line for line in _ptest_lines(completed)
                    if line.startswith("ptest: -v runner: ")]
    assert len(runner_lines) == 1
    argv_part = runner_lines[0].removeprefix("ptest: -v runner: ")
    assert argv_part == f"{launcher[0]} command.py literal"


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

    completed = case.invoke(domain, root, "-q", "--full", timeout=20)

    assert completed.code == 0
    assert json.loads(completed.stdout) == []
    assert _ptest_lines(completed) == []


def test_quiet_combined_with_verbose_forwards_but_stays_quiet(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))

    completed = case.invoke(domain, root, "-v", "-q", "--full", timeout=20)

    assert completed.code == 0
    assert _ptest_lines(completed) == []


def test_quiet_refusal_still_prints_with_hint(case):
    domain = case.domain()
    root = case.project(domain, kind="pytest")
    (root / ".ptest.toml").write_text(
        (root / ".ptest.toml").read_text(encoding="utf-8")
        + 'test_roots = ["tests"]\n',
        encoding="utf-8")

    completed = case.invoke(domain, root, "-q", "--full", timeout=20)

    assert completed.code == 2
    err = completed.stderr.decode("utf-8", "replace")
    # A full gate reaches bridge validation (bare `ptest` plans this
    # fixture to full first: selection is off with no [selection] table),
    # so the refusal names the bridge, not the old automatic capability.
    assert "native-config-invalid" in err
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


def test_end_line_uses_singular_test_count():
    from ptest import progress

    counts = C.Counts(collected=1, executed=1, passed=1, failed=0,
                      skipped=0, unknown=0)
    assert progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=3.6,
        exit_code=0) == "ptest: passed · 1 test · 3.6s"


def test_end_line_colors_verdict_on_tty_only(monkeypatch):
    from ptest import progress

    counts = C.Counts(collected=2, executed=2, passed=2, failed=0,
                      skipped=0, unknown=0)
    plain = progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=3.6, exit_code=0)
    assert "\x1b" not in plain

    tty = progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=3.6, exit_code=0,
        color=True)
    assert "\x1b[32mpassed\x1b[0m" in tty
    assert "\x1b[2mptest:\x1b[0m" in tty

    failed_line = progress.format_end(
        C.Status.FAILED, counts=counts, duration_s=1.0, exit_code=1,
        color=True)
    assert "\x1b[31mfailed\x1b[0m" in failed_line

    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=3.6, exit_code=0,
        color=True)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert "\x1b" not in progress.format_end(
        C.Status.PASSED, counts=counts, duration_s=3.6, exit_code=0,
        color=True)


def test_setup_only_run_reads_as_setup():
    from ptest import progress

    assert (progress.format_setup_run_start("web", ("npm", "ci"))
            == "ptest: web · setup: npm ci")
    assert (progress.format_setup_run_done(5.9)
            == "ptest: setup done · 5.9s")


def test_duration_and_timeout_shapes():
    from ptest import progress

    assert progress.format_duration(4.2) == "4.2s"
    assert progress.format_duration(192.3) == "3m12s"
    assert progress.format_duration(3723.0) == "1h2m3s"
    assert progress.format_timeout(1800.0) == "30m"
    assert progress.format_timeout(14400.0) == "4h"
    assert progress.format_timeout(5400.0) == "90m"
    assert progress.format_timeout(600.0) == "10m"


def test_waiting_line_repeats_back_off_to_a_few_dozen_over_4h():
    from ptest import progress

    gaps, gap = [], progress.WAIT_REPEAT_S
    elapsed = progress.WAIT_FIRST_S
    while elapsed < 14400.0:
        gaps.append(gap)
        elapsed += gap
        gap = progress.next_wait_gap(gap)
    assert gaps[:5] == [15.0, 30.0, 60.0, 120.0, 240.0]
    assert max(gaps) == progress.WAIT_REPEAT_MAX_S == 300.0
    assert len(gaps) < 60


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

    completed = case.invoke(domain, root, "--full", "--result-json", "mine.json", timeout=20)

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

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0
    raw = completed.stderr.decode("utf-8", "replace")
    assert "bad\\nname\\x01x" in raw
    assert "bad\nname" not in raw


def test_setup_failure_prints_failed_line_with_exit(case):
    domain = case.domain()
    root = _command_project(case, domain, args=("literal",))
    argv = [sys.executable, "command.py", "exit", "3"]
    _write_setup_config(root, argv, ["never-created.txt"])

    completed = case.invoke(domain, root, "--full", timeout=30)

    assert completed.code != 0
    lines = _ptest_lines(completed)
    assert "ptest: setup failed (exit 3)" in lines, lines


def test_setup_failed_line_names_problem_for_zero_exit():
    from ptest import operations

    problem = operations._problem("state-unavailable", "guard trouble")
    assert operations._setup_failed_line(
        setup_raw=0, setup_problem=problem) == (
        "ptest: setup failed (state-unavailable)")


def test_setup_failed_line_keeps_exit_for_nonzero_exit():
    from ptest import operations

    assert operations._setup_failed_line(
        setup_raw=3, setup_problem=None) == "ptest: setup failed (exit 3)"


def test_setup_failed_line_uses_problem_code_without_exit():
    from ptest import operations

    problem = operations._problem("state-unavailable", "guard trouble")
    assert operations._setup_failed_line(
        setup_raw=None, setup_problem=problem) == (
        "ptest: setup failed (state-unavailable)")


def test_setup_failed_line_clean_setup_reports_done():
    from ptest import operations

    assert operations._setup_failed_line(
        setup_raw=0, setup_problem=None) is None


def test_queue_timeout_refusal_keeps_code_message_and_hint_once(case):
    domain = case.domain(slots=1, jobs=1)
    root = _command_project(case, domain, args=("literal",))
    release = _hold_slot(domain, root)
    try:
        completed = case.invoke(domain, root, "--full", "--queue-timeout", "3", timeout=30)
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


def test_waiting_line_names_the_real_blocker_when_slots_are_free():
    # "waiting for 1 slot (2 of 4 free)" was a lie: slots were free and an
    # exclusive run held the machine. With a blocker the line says so.
    first = progress.format_waiting(
        needed=1, free=2, limit=4, timeout_s=60,
        holders="repo-api (pid 3763337)",
        blocker="an exclusive run holds the whole machine")
    assert first == ("ptest: waiting: an exclusive run holds the whole machine"
                     " — in use by repo-api (pid 3763337) · queue timeout 1m")
    again = progress.format_waiting(
        needed=1, free=2, limit=4, timeout_s=60, elapsed_s=16.5,
        blocker="an exclusive run holds the whole machine")
    assert again == ("ptest: still waiting: an exclusive run holds the whole"
                     " machine · 16.5s")


def test_waiting_line_keeps_the_slot_wording_without_a_blocker():
    assert progress.format_waiting(needed=2, free=1, limit=4, timeout_s=60) == (
        "ptest: waiting for 2 slots (1 of 4 free) · queue timeout 1m")


def test_queued_run_behind_an_exclusive_holder_names_it(case):
    domain = case.domain(slots=2, jobs=2)
    root = _command_project(case, domain, args=("literal",))
    # An exclusive holder on a 2-slot machine leaves a slot free: the old
    # line would have claimed "1 of 2 free" as the reason for waiting.
    owner = platform.process_identity(os.getpid())
    holder = C.AdmissionRequest(
        run_id=secrets.token_hex(16),
        checkout=C.CheckoutIdentity(project_id="ab" * 16, checkout_id="cd" * 16, root=root),
        owner=owner, slots=1, exclusive=True, locks=(), memory_mb=None,
        deadline=time.monotonic() + 300, fixture=True)
    ticket = scheduler.enqueue(domain, holder)
    assert scheduler.poll(domain, ticket).state is C.LeaseState.GRANTED
    try:
        completed = case.invoke(domain, root, "--full", "--queue-timeout", "3", timeout=30)
    finally:
        scheduler.cancel_pending(domain, ticket, owner)
    lines = _ptest_lines(completed)
    waiting = [line for line in lines if "waiting" in line]
    # The queued command run is exclusive itself, so the truthful reason is
    # that it needs the whole machine while the holder is active.
    assert waiting and waiting[0].startswith(
        "ptest: waiting: this run needs the whole machine; 1 run is still active"
        " — in use by "), lines
    assert "free)" not in waiting[0]


# --- 0.7.0 tiered selection: hints, deferred segment, no-run lines (T6) -------

def _report(**fields):
    base = dict(engine="dynamic", iteration=True, run_now=3, deferred=0,
                gate_recorded=True)
    base.update(fields)
    deferred = base["deferred"]
    if isinstance(deferred, int) and deferred > 0:
        if "deferred_by_reason" not in base:
            base["deferred_by_reason"] = (("b-static", "api/x.py", deferred),)
        if "deferred_by_file" not in base:
            base["deferred_by_file"] = (("tests/test_x.py", deferred),)
    return C.SelectionReport(**base)


def test_gate_hint_strings_replace_next_full():
    # Oracle: spec R29 (the hint strings are exact).
    assert progress.GATE_HINT == "gate at spec end"
    assert progress.GATE_HINT_NO_GATE == "gate at spec end (no gate recorded)"
    assert progress.COVERAGE_OWED == "coverage gate still owed: ptest --full --cov"
    assert not hasattr(progress, "NEXT_FULL")
    assert not hasattr(progress, "NEXT_FULL_COV")
    assert "NEXT_FULL" not in progress.__all__


@pytest.mark.parametrize("status,selection,expected", [
    (C.Status.FAILED, None, progress.NEXT_FIX),
    (C.Status.FAILED, _report(deferred=9), progress.NEXT_FIX),
    (C.Status.PASSED, None, None),
    (C.Status.PASSED, _report(iteration=False, engine=None), None),
    (C.Status.PASSED, _report(full_reason="uv.lock is a full trigger",
                              engine=None, deferred=0), None),
    (C.Status.PASSED, _report(engine="vitest-changed", run_now=None,
                              deferred=None, gate_recorded=False),
     progress.GATE_HINT),
    (C.Status.PASSED, _report(gate_recorded=False), progress.GATE_HINT_NO_GATE),
    (C.Status.PASSED, _report(engine="static", run_now=None, deferred=None),
     progress.GATE_HINT),
    (C.Status.PASSED, _report(engine="static", run_now=None, deferred=None,
                              gate_recorded=False),
     progress.GATE_HINT_NO_GATE),
    (C.Status.PASSED, _report(deferred=4), progress.GATE_HINT),
    (C.Status.PASSED, _report(deferred=0), None),
    (C.Status.NO_TESTS_NEEDED, _report(deferred=2), progress.GATE_HINT),
    (C.Status.CANCELLED, _report(deferred=2), None),
    (C.Status.INCOMPLETE, _report(deferred=2), None),
    (C.Status.NOT_RUN, _report(deferred=2), None),
])
def test_next_step_matrix_follows_the_selection_report(status, selection,
                                                       expected):
    # Oracle: spec R29 (next_step rules) and D11 (no report, no hint).
    assert progress.next_step(status, selection) == expected


def _deferred(*rows):
    tests = sum(item[2] for item in rows)
    return _report(deferred=tests, deferred_by_reason=tuple(rows),
                   deferred_by_file=(("tests/test_x.py", tests),))


@pytest.mark.parametrize("code,phrase", [
    ("g-shared", "shared import-time state via api/models.py"),
    ("g-reach", "import-time reach via api/models.py"),
    ("g-data", "ambient data api/models.py"),
    ("b-static", "static reach via api/models.py"),
    ("u-static", "unrecorded since gate via api/models.py"),
])
def test_format_deferred_phrases(code, phrase):
    # Oracle: spec R30 (one phrase per T2 code).
    report = _deferred((code, "api/models.py", 14300))
    assert progress.format_deferred(report) == f"deferred 14300 ({phrase})"


def test_format_deferred_names_other_codes_and_picks_top_path():
    # Oracle: R30 example plus "+{k} more" for other codes; ties by path.
    report = _deferred(("g-shared", "api/b.py", 5), ("g-shared", "api/a.py", 5),
                       ("b-static", "api/c.py", 2), ("u-static", "api/d.py", 1))
    assert progress.format_deferred(report) == (
        "deferred 13 (shared import-time state via api/a.py, +2 more)")
    one_more = _deferred(("g-shared", "api/a.py", 5), ("b-static", "api/c.py", 2))
    assert progress.format_deferred(one_more) == (
        "deferred 7 (shared import-time state via api/a.py, +1 more)")


@pytest.mark.parametrize("report", [
    None, _report(deferred=0), _report(deferred=None),
    _report(engine="vitest-changed", run_now=None, deferred=None)])
def test_format_deferred_is_none_without_deferred_tests(report):
    assert progress.format_deferred(report) is None


def test_format_deferred_escapes_hostile_paths():
    # Oracle: R30 "escaped through render.terminal_text".
    report = _deferred(("b-static", "api/\x1b[31mevil.py", 3))
    line = progress.format_deferred(report)
    assert "\x1b" not in line
    assert line.startswith("deferred 3 (static reach via api/")


def test_no_run_lines_name_deferred_and_drop_full_suffixes():
    # Oracle: spec R31 (nothing to run now; suffixes dropped; hint last).
    deferred = "deferred 7 (static reach via api/c.py)"
    assert progress.format_nothing_changed(
        "origin/dev", no_green_run=True, deferred=deferred,
        hint=progress.GATE_HINT) == (
        "ptest: no changes vs origin/dev (no green run yet) — nothing to run "
        "now · deferred 7 (static reach via api/c.py) · gate at spec end")
    assert progress.format_nothing_changed("origin/dev") == (
        "ptest: no changes vs origin/dev — nothing to test")
    assert progress.format_nothing_changed(
        "origin/dev", hint=progress.GATE_HINT_NO_GATE) == (
        "ptest: no changes vs origin/dev — nothing to test · "
        "gate at spec end (no gate recorded)")
    assert progress.format_no_green_changes(
        deferred=deferred, hint=progress.GATE_HINT) == (
        "ptest: no changes since last green run — nothing to run now · "
        "deferred 7 (static reach via api/c.py) · gate at spec end")
    assert progress.format_no_changes_anywhere() == (
        "ptest: no changes — nothing to test")
    assert progress.format_no_changes_under(
        "api", "api/tests", deferred=deferred, hint=progress.GATE_HINT) == (
        "ptest: api · no changes under api/tests — nothing to run now · "
        "deferred 7 (static reach via api/c.py) · gate at spec end")
    assert progress.format_no_changes_under("api", "api/tests") == (
        "ptest: api · no changes under api/tests — nothing to test")
    assert progress.format_no_changes(
        "api", deferred=deferred) == (
        "ptest: api · no changes · deferred 7 (static reach via api/c.py)")
    assert progress.format_no_changes("api") == "ptest: api · no changes"


def test_total_note_and_step_sum_children():
    # Oracle: spec Decision 10 / R31 (one summed count and one hint).
    api = _report(deferred=3)
    web = _report(deferred=4, gate_recorded=False)
    assert progress.format_total_deferred([api, web, None]) == (
        "deferred 7 across 2 children")
    assert progress.format_total_deferred([api, _report(deferred=0)]) == (
        "deferred 3 across 1 children")
    assert progress.format_total_deferred([_report(deferred=0), None]) is None
    assert progress.total_next_step(C.Status.PASSED, [api, web]) == (
        progress.GATE_HINT_NO_GATE)
    assert progress.total_next_step(C.Status.PASSED, [api]) == (
        progress.GATE_HINT)
    assert progress.total_next_step(C.Status.PASSED, [_report(deferred=0)]) is None
    assert progress.total_next_step(C.Status.FAILED, [api]) == progress.NEXT_FIX
    assert progress.total_next_step(C.Status.PASSED, [None, None]) is None


# --- 0.7.0 end to end through the CLI (T6) ------------------------------------
#
# A real pytest project through the bridge, built by the T4a e2e helpers, to
# pin what the user reads: the end-line note and hint of a deferral, the gate
# that clears it, and a forced full suite (spec R8a, R29, R30, R33).

from test_selection_e2e import (  # noqa: E402
    _ALL, _CHANGED_TIMEOUT, _CORE, _FULL_TIMEOUT, _make_project, _markers)


@pytest.fixture
def recorded_project(case):
    """A git project with a green `--full` run (dependencies recorded)."""
    domain = case.domain()
    root = _make_project(case, domain)
    full = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)
    assert full.code == 0, full.stderr.decode()[-2000:]
    (root / "ran.txt").unlink()
    return domain, root


def _end_lines(completed):
    return [line for line in completed.stderr.decode().splitlines()
            if re.match(r"ptest: (passed|failed|incomplete|no tests needed)",
                        line)]


def test_gate_runs_every_deferred_test(case, recorded_project):
    # Oracle: spec R15/R30/R33 - the gate that follows a deferral executes the
    # deferred tests, clears the deferral, and the next iteration line is clean.
    domain, root = recorded_project
    core = root / "pkg" / "core.py"
    core.write_text(_CORE + "\nEFFECT_MARKER = 1\n")

    iteration = case.invoke(domain, root, "--changed",
                            timeout=_CHANGED_TIMEOUT)

    assert iteration.code == 0, iteration.stderr.decode()[-2000:]
    assert _markers(root) == set()
    err = iteration.stderr.decode()
    # Nothing ran, so the line says so and names what waits for the gate.
    assert re.search(r"nothing to run now · deferred \d+ \(", err), err
    assert "no tests affected" not in err
    assert err.rstrip().endswith("· gate at spec end")

    gate = case.invoke(domain, root, "--full", "--result-json", "gate.json",
                       timeout=_FULL_TIMEOUT)

    gate_err = gate.stderr.decode()
    assert gate.code == 0, gate_err[-2000:]
    assert _markers(root) == _ALL
    assert re.search(r"ptest: selection gate: ran \d+ previously deferred "
                     r"tests? · 0 failed · 0 observers learned", gate_err)
    block = json.loads((root / "gate.json").read_text())["data"]["selection"]
    assert block["iteration"] is False and block["engine"] is None
    assert block["deferred"] == 0 and block["run_now"] is None
    assert block["gate"]["previously_deferred"] >= 1
    assert block["gate"]["failed"] == 0

    after = case.invoke(domain, root, "--changed", timeout=_CHANGED_TIMEOUT)

    after_err = after.stderr.decode()
    assert after.code == 0, after_err[-2000:]
    assert "deferred" not in after_err
    assert "gate at spec end" not in after_err


def test_forced_full_suite_names_reason_on_end_line(case, recorded_project):
    # Oracle: spec R8a - a trigger in the diff forces a full suite; its end
    # line names the start-line reason, its JSON carries full_reason, it is a
    # gate (report filled) and it owes no hint and no coverage line.
    domain, root = recorded_project
    (root / "pyproject.toml").write_text("[tool.fixture]\nname = 'x'\n")

    forced = case.invoke(domain, root, "--changed", "--result-json",
                         "forced.json", timeout=_FULL_TIMEOUT)

    err = forced.stderr.decode()
    assert forced.code == 0, err[-2000:]
    assert _markers(root) == _ALL
    assert "→ full suite: pyproject.toml is a full trigger" in err
    (end,) = _end_lines(forced)
    assert "full suite (pyproject.toml is a full trigger)" in end
    assert "gate at spec end" not in end
    assert "coverage gate" not in err
    block = json.loads((root / "forced.json").read_text())["data"]["selection"]
    assert block["full_reason"] == "pyproject.toml is a full trigger"
    assert block["iteration"] is True
    assert block["deferred"] == 0 and block["run_now"] is None
    assert block["gate"] is not None


def test_iteration_green_publishes_no_full_proof(case, recorded_project):
    # Oracle: spec R38/Vocabulary - an iteration pass is not a gate: it may
    # not make the tree "already verified" for a later `--full`.
    domain, root = recorded_project
    (root / "pkg" / "core.py").write_text(
        _CORE.replace("return n + 1", "return 1 + n"))

    iteration = case.invoke(domain, root, "--changed", "--result-json",
                            "iteration.json", timeout=_CHANGED_TIMEOUT)

    assert iteration.code == 0, iteration.stderr.decode()[-2000:]
    data = json.loads((root / "iteration.json").read_text())["data"]
    assert data["full_gate_eligible"] is False
    assert data["baseline_published"] is False
    assert data["selection"]["iteration"] is True
    (root / "ran.txt").unlink(missing_ok=True)

    gate = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)

    gate_err = gate.stderr.decode()
    assert gate.code == 0, gate_err[-2000:]
    assert "already verified" not in gate_err
    assert _markers(root) == _ALL


def test_json_caps_and_terminal_escaping(case, capsys):
    # Oracle: spec R34 (at most 5 reasons x 3 paths, 50 files, scalars only)
    # and R30 (paths reach the terminal escaped).
    from dataclasses import replace

    codes = ("g-shared", "g-reach", "g-data", "b-static", "u-static")
    by_reason = tuple((code, f"pkg/mod_{index}.py", 10 + index)
                      for code in codes for index in range(7))
    total = sum(item[2] for item in by_reason)
    by_file = tuple((f"tests/test_{index:03d}.py", 1) for index in range(120))
    # The file aggregate must sum to the same total as the reason aggregate.
    by_file += (("tests/test_big.py", total - 120),)
    report = C.SelectionReport(
        engine="dynamic", iteration=True, run_now=3, deferred=total,
        deferred_by_reason=by_reason, deferred_by_file=by_file,
        gate_recorded=True)
    result = replace(case.result(status=C.Status.PASSED, exit_code=0,
                                 counts=C.Counts(passed=3)), selection=report)

    block = C.serialize_run_result(result)["selection"]

    assert len(block["deferred_reasons"]) == 5
    assert all(len(item["paths"]) == 3 for item in block["deferred_reasons"])
    assert len(block["deferred_files"]) == 50
    assert block["deferred_files_truncated"] is True
    assert block["deferred_files"] == sorted(
        block["deferred_files"], key=lambda item: item["file"])
    assert block["observers"] == {"promoted": 0, "deferred": 0,
                                  "fingerprint": None}
    assert set(block) == {
        "engine", "iteration", "run_now", "deferred", "full_reason",
        "deferred_reasons", "deferred_files", "deferred_files_truncated",
        "observers", "gate"}

    from ptest import operations

    hostile = C.SelectionReport(
        engine="dynamic", iteration=True, run_now=1, deferred=2,
        deferred_by_reason=(("b-static", "pkg/\x1b[31mred\x1b[0m.py", 2),),
        deferred_by_file=(("tests/test_a.py", 2),), gate_recorded=True)
    operations._emit_end(
        C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                     next_hint=True),
        replace(case.result(status=C.Status.PASSED, exit_code=0,
                            counts=C.Counts(passed=1)), selection=hostile),
        0.0)
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "deferred 2 (static reach via pkg/" in err


@pytest.mark.parametrize("status,exit_code", [
    (C.Status.PASSED, 0), (C.Status.FAILED, 1), (C.Status.INCOMPLETE, 70)])
def test_deferred_always_reported_on_end_line(case, capsys, status, exit_code):
    # Oracle: spec R30 - an iteration run that deferred anything says so on
    # its end line whatever the verdict; -q silences it like every line.
    from dataclasses import replace

    from ptest import operations

    report = C.SelectionReport(
        engine="dynamic", iteration=True, run_now=1, deferred=5,
        deferred_by_reason=(("g-data", "data/seed.json", 5),),
        deferred_by_file=(("tests/test_a.py", 5),), gate_recorded=True)
    result = replace(case.result(status=status, exit_code=exit_code,
                                 counts=C.Counts(passed=1)), selection=report)
    request = C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_a.py",),
                           next_hint=True)

    operations._emit_end(request, result, 0.0)
    err = capsys.readouterr().err
    assert "deferred 5 (ambient data data/seed.json)" in err

    operations._emit_end(replace(request, quiet=True), result, 0.0)
    assert capsys.readouterr().err == ""


def _normalised(completed, root):
    """ptest's own stderr lines with durations and the project name masked."""
    out = []
    for line in completed.stderr.decode().splitlines():
        if not line.startswith("ptest:"):
            continue
        line = line.replace(root.name, "<project>")
        line = re.sub(r"\d+(\.\d+)?(ms|s|min|h)\b", "<t>", line)
        out.append(line)
    return out


def test_full_run_output_byte_identical_when_nothing_deferred(case):
    # Oracle: spec "Rollout and compatibility" - a plain `--full` run on a
    # project with nothing deferred prints exactly what 0.6.8 printed (ptest's
    # own lines, durations masked). Basis for the expected lines: they are
    # produced by code this task did not touch (`progress.format_start`,
    # `progress.format_end` without a note or hint, and the `full gate runs:`
    # emit in `operations.execute`), checked with
    # `git diff e005a18 -- src/ptest/` showing no hunk in any of them.
    domain = case.domain()
    root = _make_project(case, domain)

    full = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)

    assert full.code == 0, full.stderr.decode()[-2000:]
    assert _normalised(full, root) == [
        "ptest: <project> · pytest · full suite",
        "ptest: full gate runs: source evidence is limited: fingerprint key "
        "is absent or unsafe",
        "ptest: passed · <t>",
    ]


def _coverage_project(case, domain):
    """A small git pytest project that declares ``coverage_args`` and whose
    tests write nothing (a writing test would invalidate the advanced tier)."""
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split(
        'project_id = "', 1)[1].split('"', 1)[0]
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("VALUE = 1\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_pkg.py").write_text(
        "import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == 1\n")
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.coverage*\nptest-result-*.json\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["-q", "-p", "no:xdist"]\n'
        "full_args = []\n"
        'coverage_args = ["--cov", "pkg", "--cov-report", "term"]\n'
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    support.init_git_repo(root, message="base")
    return root


def test_plain_full_run_on_a_coverage_project_only_adds_the_owed_line(case):
    # Oracle: spec R29 - the only change to a plain green `--full` on a project
    # that declares coverage is the wording of the coverage line. This also
    # proves the CLI's `--full` request carries no changed_note (a note would
    # silence the line, as it does for a forced full suite).
    domain = case.domain()
    root = _coverage_project(case, domain)

    full = case.invoke(domain, root, "--full", timeout=_FULL_TIMEOUT)

    err = full.stderr.decode()
    assert full.code == 0, err[-2000:]
    lines = _normalised(full, root)
    assert "ptest: <project> · pytest · full suite" in lines
    (end,) = [line for line in lines if line.startswith("ptest: passed")]
    assert end.endswith(" · coverage gate still owed: ptest --full --cov"), lines
    assert "before handoff" not in err
