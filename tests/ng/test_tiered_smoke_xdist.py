"""Tiered iteration selection smoke rows S1, S3, S4 and S8 under xdist.

Same fixture, same real CLI subprocess and same expectations as
``test_tiered_smoke`` (serial), with ``args = ["-q", "-n", "2"]`` against
the locked pytest-xdist 3.8.0 (the one the parallel twins use,
``test_pytest_parallel_subprocess``). The deselect binding (S1, S4), its
R44 rewrite (S8) and the recording must hold under two workers, and
``ran.txt`` is the union of what both workers wrote. Under two workers
the end line carries the controller-reconciled counts.
"""
from __future__ import annotations

import importlib.util
import re
import sys

import pytest

from test_tiered_smoke import (
    ALL_TESTS, CHANGED_TIMEOUT, GATE_HINT, MODELS, NEIGHBOUR, OBSERVER,
    ORDERS_TESTS,
    SHARED_MODELS, USERS_TESTS, Project,
)

# The spec writes the variant as ``args = ["-q", "-n", "2"]``. ptest owns
# the worker flag (``reject_unowned_controls``: a literal ``-n 2`` in the
# runner args is refused, pinned by test_x0), so two xdist workers are
# requested the way a project does: the pytest config activates xdist and
# ptest grants the workers (``workers = 2`` and a two-slot domain).
XDIST_ARGS = ("-q",)
XDIST_FILES = {"pyproject.toml": (
    "[tool.pytest.ini_options]\naddopts = [\"-n\", \"2\"]\n")}

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("xdist") is None,
    reason="pytest-xdist is not installed")


@pytest.fixture
def recorded(case):
    return Project.recorded(case, XDIST_ARGS, workers=2, files=XDIST_FILES)


def test_x0_literal_worker_flag_in_the_args_is_refused(case):
    """The spec's literal ``args = ["-q", "-n", "2"]`` never reaches pytest."""
    domain = case.domain(slots=2, jobs=2)
    project = Project.create(case, domain, ("-q", "-n", "2"), workers=2)

    run = project.run("--full")

    assert run.code == 2, run.err[-2000:]
    assert ("native-config-invalid: pytest remote or parallel control is "
            "not ptest-owned") in run.err
    assert project.ran() == set()


def test_x2_the_recording_run_is_parallel(case):
    domain = case.domain(slots=2, jobs=2)
    project = Project.create(case, domain, XDIST_ARGS, 2, XDIST_FILES)

    run = project.run("-v", "--full")

    assert run.code == 0, run.err[-2000:]
    assert "-v grant: 2 slots" in run.err
    assert "recorded 20 tests from 2 processes" in run.err
    assert re.fullmatch(r"ptest: passed · 20 tests · [\d.]+m?s", run.end), (
        run.end)
    assert project.ran() == ALL_TESTS  # the union of both workers


def test_x1_body_edit_in_a_partial_file_under_two_workers(recorded):
    recorded.edit_users_body()

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert re.fullmatch(r"ptest: passed · 3 tests · [\d.]+m?s", run.end), (
        run.end)
    assert recorded.ran() == USERS_TESTS  # test_mixed_hub deselected
    selection = run.selection
    assert selection["run_now"] == 3
    assert selection["deferred"] == 0
    assert selection["gate"] is None


def test_x3_gate_learns_the_observer_under_two_workers(recorded):
    recorded.after_s2()
    assert recorded.ran() == set()

    gate = recorded.run("--full")

    assert gate.code == 1, gate.err[-2000:]
    assert ("ptest: selection gate: ran 20 previously deferred tests · "
            "1 failed · 1 observer learned") in gate.lines
    assert re.fullmatch(
        r"ptest: failed · 1 failed, 19 passed · [\d.]+m?s \(exit 1\) · .*",
        gate.end), gate.end
    assert recorded.ran() == ALL_TESTS
    assert recorded.sql("SELECT nodeid, path FROM observers") == [
        (OBSERVER, MODELS)]
    learned = gate.selection["gate"]
    assert learned["observers_learned"] == 1
    assert learned["unlearned"] == 0
    assert learned["demoted"] == 0


def test_x4_observer_runs_and_neighbour_stays_deferred_under_two_workers(
        recorded):
    recorded.learned()
    recorded.reseed(["age", "email", "name"], None)
    recorded.add_field("phone: str")

    run = recorded.run("--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 1, run.err[-2000:]
    assert re.fullmatch(
        rf"ptest: failed · 1 failed, 0 passed · deferred 19 "
        rf"\({SHARED_MODELS}\) · [\d.]+m?s \(exit 1\) · .*", run.end), (
        run.end)
    assert recorded.ran() == {OBSERVER}
    assert NEIGHBOUR not in recorded.ran()
    selection = run.selection
    assert selection["observers"]["promoted"] == 1
    assert selection["run_now"] == 1
    assert selection["deferred"] == 19


def test_x8_changed_fingerprint_promotes_the_observer_under_two_workers(
        recorded):
    recorded.learned()
    recorded.reseed(["age", "email", "name"],
                    [sys.executable, "-m", "app.manifest"])
    recorded.add_field("phone: str")

    run = recorded.run("-v", "--changed", timeout=CHANGED_TIMEOUT)

    assert run.code == 1, run.err[-2000:]
    assert "fingerprint: changed · 1 observer promoted" in run.err
    assert f"deferred 19 ({SHARED_MODELS})" in run.end, run.end
    assert recorded.ran() == {OBSERVER}  # binding rewritten for this id
    assert run.selection["observers"]["promoted"] == 1
    assert GATE_HINT not in run.end  # a failing run keeps the fix hint


def test_deferred_tests_never_counted_as_passed(recorded):
    """18 deferred tests are not run: the counted end line, the result
    JSON and the run counts report the 2 that ran, never 20."""
    recorded.edit_total_body()
    recorded.add_field("age: int")

    run = recorded.run("--changed", "--result-json", "r.json",
                       timeout=CHANGED_TIMEOUT)

    assert run.code == 0, run.err[-2000:]
    assert re.fullmatch(
        rf"ptest: passed · 2 tests · deferred 18 \({SHARED_MODELS}\) · "
        rf"[\d.]+m?s · {GATE_HINT}", run.end), run.end
    assert not re.search(r"\b(20|19|18) (tests|passed)\b",
                         run.end.split(" · deferred")[0])
    assert recorded.ran() == ORDERS_TESTS
    selection = run.selection
    assert selection["run_now"] == 2
    assert selection["deferred"] == 18
    counted = [value for key, value in run.data.items()
               if re.search(r"passed|tests|count", key)
               and isinstance(value, int)]
    assert all(value <= 2 for value in counted), run.data
