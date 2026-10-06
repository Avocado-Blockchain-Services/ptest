"""Stall-detection workloads: simulate the pytest bridge arm + dump contract.

Modes (argv: mode ready_path report_path):
  armed-idle        register dump file, create the marker, then block
  unarmed-idle      register dump file, block WITHOUT creating the marker
  armed-busy        register dump file, create the marker, spin CPU till release
  armed-idle-child  armed-idle plus one same-group child with its own dump file

Derived paths: marker = report_path + ".done",
dump = report_path + ".stack-<pid>", term note = report_path + ".term".
The dump header mirrors the bridge contract
("ptest stack dump: role=<role> ... pid=<pid>"). SIGTERM is recorded in
the term note; the release socket also releases the workload during
failed-test cleanup so failures cannot leak it.
"""
from __future__ import annotations

import faulthandler
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

_HEADER_PREFIX = "ptest stack dump: "


def _register_dump(dump_path: Path, role: str) -> None:
    fd = os.open(dump_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | os.O_NOFOLLOW | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        if role == "controller":
            header = f"{_HEADER_PREFIX}role=controller pid={os.getpid()}\n"
        else:
            header = (f"{_HEADER_PREFIX}role=worker id=gw0 "
                      f"pid={os.getpid()}\n")
        os.write(fd, header.encode("ascii"))
    except Exception:
        os.close(fd)
        raise
    faulthandler.register(signal.SIGWINCH, file=fd, all_threads=True,
                          chain=True)
    globals().setdefault("_dump_fds", []).append(fd)


def _try_create_marker(marker: Path) -> None:
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return
    os.close(fd)


def stall_blocked_teardown(peer: socket.socket) -> None:
    """Block until released or killed; names the stall in stack dumps."""
    peer.recv(1)


def stall_cpu_spin_until_released(peer: socket.socket) -> None:
    """Burn CPU until the release socket delivers a byte or times out."""
    peer.settimeout(0.05)
    while True:
        stop = time.monotonic() + 0.05
        while time.monotonic() < stop:
            sum(range(500))
        try:
            if peer.recv(1):
                return
        except socket.timeout:
            continue


def _child_main(ready_path: str, dump_path: str) -> None:
    _register_dump(Path(dump_path), "worker")

    def child_stopped(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, child_stopped)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(ready_path)
        peer.sendall(json.dumps({"pid": os.getpid(),
                                 "pgid": os.getpgrp(),
                                 "mode": "stall-child"}).encode() + b"\n")
        stall_blocked_teardown(peer)


def main() -> None:
    mode, ready_path, report_name = sys.argv[1:4]
    report_path = Path(report_name)
    marker = Path(str(report_path) + ".done")
    dump_path = Path(f"{report_path}.stack-{os.getpid()}")
    term_path = Path(f"{report_path}.term")
    _register_dump(dump_path, "controller")
    if mode in {"armed-idle", "armed-busy", "armed-idle-child"}:
        _try_create_marker(marker)

    child = None
    if mode == "armed-idle-child":
        child = subprocess.Popen(
            (sys.executable, __file__, "child", ready_path,
             str(report_path)),
            close_fds=True)

    def stopped(signum, _frame):
        term_path.write_text(str(signum))
        if child is not None:
            try:
                child.wait(timeout=10)
            except Exception:
                pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(ready_path)
        info = {"pid": os.getpid(), "pgid": os.getpgrp(), "mode": mode,
                "child": child.pid if child else None,
                "dump": str(dump_path), "marker": str(marker)}
        peer.sendall(json.dumps(info).encode() + b"\n")
        if mode == "armed-busy":
            stall_cpu_spin_until_released(peer)
        else:
            stall_blocked_teardown(peer)
    if child is not None:
        child.wait(timeout=45)
    term_path.write_text("released")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        _child_main(sys.argv[2], f"{sys.argv[3]}.stack-{os.getpid()}")
    else:
        main()
