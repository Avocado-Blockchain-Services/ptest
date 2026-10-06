"""Bounded, escaped stack-dump collection/printing and per-attempt cleanup.

Covers the T4 ``stack_dumps`` unit contract: controller-first ordering,
header-only skips, per-file and total caps with truncation notes, escaping
of control bytes, refusal (never read, never delete through links) of
symlinked/foreign-owned/hard-linked/FIFO dumps, ignoring of other attempts'
files, and marker+dump cleanup on every outcome.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ptest import stack_dumps

_REPORT_NAME = "native-a001-" + "ab" * 16 + ".json"
_OTHER_REPORT_NAME = "native-a001-" + "cd" * 16 + ".json"


def _report(tmp_path: Path, name: str = _REPORT_NAME) -> Path:
    reports = tmp_path / "reports"
    reports.mkdir(mode=0o700, exist_ok=True)
    return reports / name


def _write_dump(path: Path, *, role: str = "worker", worker_id: str = "gw0",
                pid: int = 4242, body: tuple[str, ...] = ("frame line",)) -> Path:
    if role == "controller":
        header = f"ptest stack dump: role=controller pid={pid}\n"
    else:
        header = f"ptest stack dump: role=worker id={worker_id} pid={pid}\n"
    path.write_bytes((header + "".join(line + "\n" for line in body)).encode("utf-8"))
    os.chmod(path, 0o600)
    return path


def _write_marker(report_path: Path) -> Path:
    marker = Path(str(report_path) + ".done")
    marker.write_bytes(b"")
    os.chmod(marker, 0o600)
    return marker


def test_collect_orders_controller_first_then_pid(tmp_path):
    report = _report(tmp_path)
    _write_dump(Path(f"{report}.stack-900"), role="worker", worker_id="gw1", pid=900,
                body=("worker one",))
    _write_dump(Path(f"{report}.stack-100"), role="worker", worker_id="gw0", pid=100,
                body=("worker zero",))
    _write_dump(Path(f"{report}.stack-500"), role="controller", pid=500,
                body=("controller frames",))
    dumps = stack_dumps.collect(report)
    assert [dump.pid for dump in dumps] == [500, 100, 900]
    assert dumps[0].controller is True
    assert all(dump.controller is False for dump in dumps[1:])


def test_emit_skips_header_only_and_unrelated_names(tmp_path, capsys):
    report = _report(tmp_path)
    _write_dump(Path(f"{report}.stack-700"), role="controller", pid=700,
                body=("teardown frames",))
    _write_dump(Path(f"{report}.stack-701"), role="controller", pid=701, body=())
    other = _report(tmp_path, _OTHER_REPORT_NAME)
    _write_dump(Path(f"{other}.stack-702"), role="controller", pid=702,
                body=("other attempt",))
    stray = report.parent / (_REPORT_NAME + ".stack-xyz")
    stray.write_bytes(b"ptest stack dump: role=controller pid=1\njunk\n")
    printed = stack_dumps.emit(report, "post-test-stall")
    err = capsys.readouterr().err
    assert printed == 1
    assert "ptest: stack dumps (1 processes) — post-test-stall" in err
    assert "teardown frames" in err
    assert "other attempt" not in err
    assert "junk" not in err


def test_emit_rejects_unknown_reason(tmp_path):
    report = _report(tmp_path)
    with pytest.raises(ValueError):
        stack_dumps.emit(report, "no-such-reason")


def test_emit_escapes_control_bytes(tmp_path, capsys):
    report = _report(tmp_path)
    evil = "\x1b[31mred\x1b[0m \x9bX\x07bell\x85sep fixture_\u2028_fn"
    _write_dump(Path(f"{report}.stack-800"), role="controller", pid=800,
                body=(evil,))
    assert stack_dumps.emit(report, "execution-timeout") == 1
    err = capsys.readouterr().err
    assert "\x1b[31m" not in err
    assert "\x9b" not in err
    assert "\\x1b[31mred\\x1b[0m" in err


def test_emit_caps_per_file_lines_and_bytes(tmp_path, capsys):
    report = _report(tmp_path)
    many_lines = tuple(f"frame {index}" for index in range(410))
    _write_dump(Path(f"{report}.stack-810"), role="controller", pid=810,
                body=many_lines)
    big = "y" * (40 * 1024)
    _write_dump(Path(f"{report}.stack-811"), role="worker", pid=811, body=(big,))
    assert stack_dumps.emit(report, "post-test-stall") == 2
    err = capsys.readouterr().err
    assert "frame 399" in err
    assert "frame 400" not in err
    notes = [line for line in err.splitlines()
             if line.startswith("ptest: stack dump truncated")]
    assert len(notes) == 2
    assert any("pid=810" in line for line in notes)
    assert any("pid=811" in line for line in notes)


def test_emit_caps_file_count_and_total_bytes(tmp_path, capsys):
    report = _report(tmp_path)
    for index in range(33):
        _write_dump(Path(f"{report}.stack-{1000 + index}"), role="worker",
                    worker_id=f"gw{index}", pid=1000 + index,
                    body=(f"worker {index}",))
    assert stack_dumps.emit(report, "post-test-stall") == 32
    err = capsys.readouterr().err
    assert "ptest: stack dumps (32 processes) — post-test-stall" in err
    assert "worker 32" not in err
    assert any(line.startswith("ptest: stack dumps truncated")
               for line in err.splitlines())


def test_emit_stops_at_total_byte_cap(tmp_path, capsys):
    report = _report(tmp_path)
    # Many short lines: each stays under render.terminal_text's per-line
    # bound, so every file really contributes ~30 KiB toward the total cap.
    body = tuple(f"frame {index:04d} " + "z" * 80 for index in range(300))
    for index in range(5):
        _write_dump(Path(f"{report}.stack-{2000 + index}"), role="worker",
                    worker_id=f"gw{index}", pid=2000 + index, body=body)
    printed = stack_dumps.emit(report, "post-test-stall")
    err = capsys.readouterr().err
    assert printed == 4
    assert any(line.startswith("ptest: stack dumps truncated")
               for line in err.splitlines())
    assert len(err.encode("utf-8")) <= 128 * 1024 + 8 * 1024


def test_emit_refuses_symlink_fifo_hardlink_and_foreign(tmp_path, capsys, monkeypatch):
    report = _report(tmp_path)
    target = tmp_path / "secret.txt"
    target.write_bytes(b"ptest stack dump: role=controller pid=1\nsensitive\n")
    link = Path(f"{report}.stack-3001")
    os.symlink(target, link)
    fifo = Path(f"{report}.stack-3002")
    os.mkfifo(fifo)
    hard = Path(f"{report}.stack-3003")
    hard.write_bytes(b"ptest stack dump: role=controller pid=1\nlinked\n")
    os.chmod(hard, 0o600)
    os.link(hard, tmp_path / "alias")
    foreign = Path(f"{report}.stack-3004")
    _write_dump(foreign, role="controller", pid=3004, body=("foreign frames",))
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1 if real_uid < 60000 else real_uid - 1)
    assert stack_dumps.emit(report, "post-test-stall") == 0
    err = capsys.readouterr().err
    assert err == ""
    assert "sensitive" not in err
    assert link.is_symlink()
    assert target.read_bytes().endswith(b"sensitive\n")
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert hard.exists()
    monkeypatch.undo()
    assert stack_dumps.emit(report, "post-test-stall") == 1
    assert "foreign frames" in capsys.readouterr().err


def test_emit_with_no_dumps_prints_nothing(tmp_path, capsys):
    report = _report(tmp_path)
    assert stack_dumps.emit(report, "post-test-stall") == 0
    assert capsys.readouterr().err == ""


def test_collect_ignores_missing_report_dir(tmp_path):
    assert stack_dumps.collect(tmp_path / "nope" / "native-a001.json") == ()


def test_candidate_scan_examines_at_most_4096_names(tmp_path, monkeypatch):
    report = _report(tmp_path)
    seen = []

    class _Entry:
        def __init__(self, name):
            self.name = name

    real_scandir = os.scandir

    def fake_scandir(path):
        seen.append(path)
        return iter(_Entry(f"file-{index}") for index in range(5000))

    monkeypatch.setattr(os, "scandir", fake_scandir)
    assert stack_dumps.collect(report) == ()
    assert real_scandir is not None
    assert len(seen) == 1


def test_scan_cap_yields_at_most_4096_names(tmp_path, monkeypatch):
    from ptest.stack_dumps import _candidate_names

    class _Entry:
        def __init__(self, name):
            self.name = name

    def fake_scandir(path):
        for index in range(10 ** 6):
            yield _Entry(f"file-{index}")

    monkeypatch.setattr(os, "scandir", fake_scandir)
    assert list(_candidate_names(tmp_path)) == [f"file-{index}" for index in range(4096)]


def test_cleanup_removes_marker_and_own_dumps(tmp_path):
    report = _report(tmp_path)
    marker = _write_marker(report)
    first = _write_dump(Path(f"{report}.stack-4001"), role="controller", pid=4001,
                        body=("frames",))
    second = _write_dump(Path(f"{report}.stack-4002"), role="worker", pid=4002,
                         body=("frames",))
    stack_dumps.cleanup(report)
    assert not marker.exists()
    assert not first.exists()
    assert not second.exists()


def test_cleanup_leaves_symlinks_foreign_other_reports_and_missing(tmp_path):
    report = _report(tmp_path)
    target = tmp_path / "keep.txt"
    target.write_bytes(b"keep")
    link = Path(f"{report}.stack-5001")
    os.symlink(target, link)
    other = _report(tmp_path, _OTHER_REPORT_NAME)
    other_dump = _write_dump(Path(f"{other}.stack-5002"), role="controller",
                             pid=5002, body=("other",))
    stack_dumps.cleanup(report)
    assert link.is_symlink()
    assert target.read_bytes() == b"keep"
    assert other_dump.exists()
    stack_dumps.cleanup(tmp_path / "reports" / "native-a999.json")
