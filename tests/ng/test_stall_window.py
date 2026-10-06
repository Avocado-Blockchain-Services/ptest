"""Unit tests for stall.IdleWindow and platform.group_cpu_times."""
from __future__ import annotations

import os
import subprocess
import sys

import psutil
import pytest

from ptest import platform
from ptest.stall import IdleWindow, idle_threshold_s


def test_threshold_floor_half_second():
    assert idle_threshold_s(1.0) == 0.5
    assert idle_threshold_s(10.0) == 0.5
    assert idle_threshold_s(25.0) == 0.5


def test_threshold_two_percent_above_floor():
    assert idle_threshold_s(26.0) == pytest.approx(0.52)
    assert idle_threshold_s(100.0) == pytest.approx(2.0)
    assert idle_threshold_s(120.0) == pytest.approx(2.4)


def test_idle_window_needs_full_span_before_firing():
    window = IdleWindow(10.0)
    assert window.observe(0.0, {(1, 100.0): 5.0}) is False
    # Span is 9.9s with zero progress: not yet a full window.
    assert window.observe(9.9, {(1, 100.0): 5.0}) is False
    # Span reaches the window with zero progress: idle.
    assert window.observe(10.0, {(1, 100.0): 5.0}) is True


def test_idle_window_busy_progress_never_fires():
    window = IdleWindow(2.0)
    cpu = 0.0
    now = 0.0
    for _ in range(10):
        cpu += 1.0
        assert window.observe(now, {(1, 100.0): cpu}) is False
        now += 0.5
    assert window.observe(now, {(1, 100.0): cpu + 1.0}) is False


def test_idle_window_slides_forward():
    window = IdleWindow(2.0)
    assert window.observe(0.0, {(1, 100.0): 0.0}) is False
    # Burst of progress, then silence: the burst leaves the window.
    assert window.observe(0.5, {(1, 100.0): 5.0}) is False
    assert window.observe(1.0, {(1, 100.0): 5.0}) is False
    assert window.observe(2.0, {(1, 100.0): 5.0}) is False
    # The burst's baseline sample still anchors the window: not idle yet.
    assert window.observe(2.5, {(1, 100.0): 5.0}) is False
    # Once the burst sample leaves the window, the silence shows: idle.
    assert window.observe(3.0, {(1, 100.0): 5.0}) is True


def test_idle_window_none_sample_resets_and_reports_busy():
    window = IdleWindow(1.0)
    assert window.observe(0.0, {(1, 100.0): 0.0}) is False
    # An unreadable member counts as busy and restarts the window.
    assert window.observe(5.0, None) is False
    assert window.observe(5.5, {(1, 100.0): 0.0}) is False
    assert window.observe(6.5, {(1, 100.0): 0.0}) is True


def test_idle_window_new_member_counts_full_value_as_progress():
    window = IdleWindow(2.0)
    assert window.observe(0.0, {(1, 100.0): 0.0}) is False
    assert window.observe(1.0, {(1, 100.0): 0.0}) is False
    # A forked child arriving with 50s of CPU is progress, not idleness.
    assert window.observe(1.5, {(1, 100.0): 0.0, (2, 101.0): 50.0}) is False
    # The arrival still anchors the window's baseline: not idle yet.
    assert window.observe(3.5, {(1, 100.0): 0.0, (2, 101.0): 50.0}) is False
    assert window.observe(4.0, {(1, 100.0): 0.0, (2, 101.0): 50.0}) is True


def test_idle_window_vanished_member_adds_nothing():
    window = IdleWindow(1.0)
    assert window.observe(0.0, {(1, 100.0): 3.0, (2, 101.0): 7.0}) is False
    # One member exits: no phantom progress is credited.
    assert window.observe(0.5, {(1, 100.0): 3.0}) is False
    assert window.observe(1.0, {(1, 100.0): 3.0}) is True


def test_idle_window_reused_pid_with_new_birth_is_new_member():
    window = IdleWindow(1.0)
    assert window.observe(0.0, {(1, 100.0): 4.0}) is False
    # Same pid, different start stamp: a new process, full value counts.
    assert window.observe(0.5, {(1, 200.0): 4.0}) is False
    # The arrival sample still anchors the window: not idle yet.
    assert window.observe(1.5, {(1, 200.0): 4.0}) is False
    assert window.observe(2.0, {(1, 200.0): 4.0}) is True


def test_idle_window_small_threshold_boundary():
    # Window 1.0s -> threshold 0.5s. Progress of exactly the threshold
    # is not "below" it, so the window is not idle.
    window = IdleWindow(1.0)
    assert window.observe(0.0, {(1, 100.0): 0.0}) is False
    assert window.observe(1.0, {(1, 100.0): 0.5}) is False
    # The 0.5s of progress still anchors the window through its baseline.
    assert window.observe(2.0, {(1, 100.0): 0.5}) is False
    assert window.observe(3.0, {(1, 100.0): 0.5}) is True


def test_group_cpu_times_observes_live_child_and_excludes_guard(monkeypatch):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        times = platform.group_cpu_times(
            os.getpgrp(), exclude_pid=os.getpid())
        assert times is not None
        assert os.getpid() not in {pid for pid, _ in times}
        child_keys = [key for key in times if key[0] == proc.pid]
        assert len(child_keys) == 1
        assert times[child_keys[0]] >= 0.0
    finally:
        proc.kill()
        proc.wait()


def test_group_cpu_times_none_when_member_unreadable(monkeypatch):
    import ptest.platform as platform_module

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:

        class _Unreadable:
            pid = proc.pid

            def status(self):
                raise psutil.AccessDenied(pid=proc.pid)

        monkeypatch.setattr(platform_module.psutil, "pids", lambda: [proc.pid])
        monkeypatch.setattr(platform_module.psutil, "Process",
                            lambda pid: _Unreadable())
        assert platform.group_cpu_times(
            os.getpgrp(), exclude_pid=-1) is None
    finally:
        proc.kill()
        proc.wait()


def test_group_cpu_times_none_past_limit():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert platform.group_cpu_times(
            os.getpgrp(), exclude_pid=-1, limit=0) is None
    finally:
        proc.kill()
        proc.wait()


def test_group_cpu_times_limit_counts_members_not_host_processes():
    """A busy host (many unrelated processes) must not disable sampling."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        members = platform.group_cpu_times(
            os.getpgrp(), exclude_pid=os.getpid(), limit=8192)
        assert members is not None
        assert platform.group_cpu_times(
            os.getpgrp(), exclude_pid=os.getpid(),
            limit=len(members)) is not None
    finally:
        proc.kill()
        proc.wait()


def test_group_cpu_times_none_when_scan_outlives_budget(monkeypatch):
    import ptest.platform as platform_module

    ticks = iter(range(1000))
    monkeypatch.setattr(platform_module.time, "monotonic",
                        lambda: float(next(ticks)))
    monkeypatch.setattr(platform_module.psutil, "pids", lambda: list(range(1, 50)))
    assert platform.group_cpu_times(
        os.getpgrp(), exclude_pid=-1, budget_s=3.0) is None
