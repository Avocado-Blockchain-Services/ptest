"""Lease-fd inheritance: owner/guard hold it, runner and daemons never do.

The owner fd is non-inheritable and absent from a ``close_fds=False``
child. A real ``operations._launch_guard`` run (driven by an owner child
process, the production shape) whose runner double-forks a detached
sleeper shows the lease inode in the guard's fds only — never the
runner's or the daemon's. After the owner and guard die with the daemon
alive, the probe is ACQUIRABLE. ``_launch_guard`` refuses with no held
lease; ``run_guard`` with a mismatched lease fd exits registration
without registering.
"""
from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import leases
from ptest import operations
from ptest import platform
from ptest import scheduler

_WATCHDOG_S = 45.0

_RUNNER = textwrap.dedent("""\
    import os, sys, time
    barrier, pidfile = sys.argv[1], sys.argv[2]
    child = os.fork()
    if child == 0:
        os.setsid()
        grandchild = os.fork()
        if grandchild != 0:
            os._exit(0)
        with open(pidfile + ".daemon", "w") as stream:
            stream.write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    with open(pidfile + ".runner", "w") as stream:
        stream.write(str(os.getpid()))
    os.waitpid(child, 0)
    deadline = time.monotonic() + 60
    while not os.path.exists(barrier) and time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(0)
    """)

_OWNER = textwrap.dedent("""\
    import json, os, select, struct, sys, time
    from pathlib import Path
    from ptest import contracts as C, leases, operations, platform, scheduler

    def main():
        spec = json.loads(sys.argv[1])
        domain = C.DomainPaths(root=Path(spec["root"]),
                               machine_config=Path(spec["machine_config"]),
                               ledger=Path(spec["ledger"]), marker=Path(spec["marker"]),
                               fixture=spec["fixture"], domain_id=spec["domain_id"])
        owner = platform.process_identity(os.getpid())
        checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                      root=Path(spec["workdir"]))
        ticket = scheduler.enqueue(domain, C.AdmissionRequest(
            run_id=spec["run_id"], checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=spec["fixture"]))
        grant = scheduler.poll(domain, ticket).grant
        assert grant is not None
        held = leases.held_fd(Path(spec["root"]), spec["run_id"])
        assert held is not None
        lease_inode = os.fstat(held).st_ino
        prepared = C.PreparedRun(
            argv=(sys.executable, spec["runner"], spec["barrier"], spec["pidfile"]),
            cwd=Path(spec["workdir"]))
        process, controller, _frames = operations._launch_guard(domain, grant, prepared)
        print(json.dumps({"guard": process.pid, "lease_inode": lease_inode,
                          "run_id": spec["run_id"]}), flush=True)
        buf = b""
        decided = False
        deadline = time.monotonic() + 30
        runner_path, daemon_path = spec["pidfile"] + ".runner", spec["pidfile"] + ".daemon"
        while time.monotonic() < deadline:
            ready, _, _ = select.select([controller], [], [], 0.5)
            if ready:
                chunk = controller.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    (length,) = struct.unpack(">I", buf[:4])
                    if len(buf) < 4 + length:
                        break
                    frame = C.decode_control_frame(buf[:4 + length],
                                                   expected_nonce=grant.nonce)
                    buf = buf[4 + length:]
                    if frame.kind == "attempt-ready" and not decided:
                        payload = {"attempt_id": frame.payload["attempt_id"],
                                   "generation": frame.payload["generation"],
                                   "gate_token": frame.payload["gate_token"],
                                   "action": "continue", "reason": None}
                        controller.sendall(C.encode_control_frame(C.ControlFrame(
                            protocol=C.GUARD_PROTOCOL_VERSION, run_id=grant.run_id,
                            nonce=grant.nonce, kind="attempt-decision", payload=payload)))
                        decided = True
            if decided and os.path.exists(runner_path) and os.path.exists(daemon_path):
                break
        runner = int(Path(runner_path).read_text())
        daemon = int(Path(daemon_path).read_text())
        print(json.dumps({"ready": True, "runner": runner, "daemon": daemon}),
              flush=True)
        sys.stdin.readline()

    main()
    """)

_GUARD_PROBE = textwrap.dedent("""\
    import os, sys
    from ptest.guard import run_guard
    code = run_guard(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
    print(code, flush=True)
    """)


def _fd_inodes(pid: int) -> set[int]:
    inodes = set()
    try:
        entries = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return inodes
    for entry in entries:
        try:
            inodes.add(os.stat(f"/proc/{pid}/fd/{entry}").st_ino)
        except OSError:
            continue
    return inodes


def test_owner_fd_absent_from_close_fds_false_child(case):
    domain = case.domain()
    run_id = "a0" * 16
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / "owner")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=domain.fixture))
    assert scheduler.poll(domain, ticket).grant is not None
    fd = leases.held_fd(domain.root, run_id)
    assert fd is not None
    assert os.get_inheritable(fd) is False
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os, sys\ntry:\n    os.fstat(int(os.environ['LEASE_FD']))\n"
         "except OSError:\n    sys.exit(0)\nsys.exit(1)"],
        capture_output=True, timeout=30, close_fds=False,
        env={**os.environ, "LEASE_FD": str(fd)})
    assert probe.returncode == 0, "owner lease fd leaked into a close_fds=False child"


def test_launch_guard_refuses_without_held_lease(case):
    domain = case.domain()
    run_id = "b1" * 16
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / "unheld")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=domain.fixture))
    grant = scheduler.poll(domain, ticket).grant
    assert grant is not None
    leases.release_held(domain.root, run_id)
    prepared = C.PreparedRun(argv=(sys.executable, "-c", "pass"), cwd=domain.root)
    with pytest.raises(C.Problem) as caught:
        operations._launch_guard(domain, grant, prepared)
    assert caught.value.code == "ownership-uncertain"
    assert caught.value.message == "run lease is not held by this process"
    assert caught.value.phase == "execution"


def test_run_guard_mismatched_lease_fd_exits_without_registering(case, tmp_path):
    from ptest import guard as guard_api

    domain = case.domain()
    run_id = "c2" * 16
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / "mismatch")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=domain.fixture))
    grant = scheduler.poll(domain, ticket).grant
    assert grant is not None
    manifest = C.LaunchManifest(
        protocol=C.GUARD_PROTOCOL_VERSION, domain=domain, grant=grant, setup=None,
        attempts=(C.PreparedRun(argv=(sys.executable, "-c", "pass"), cwd=domain.root),),
        attempt_ids=("a001",), setup_timeout_s=5, attempt_timeout_s=10,
        compound_timeout_s=20)
    driver = tmp_path / "guard_probe.py"
    driver.write_text(_GUARD_PROBE)
    control_guard, control = socket.socketpair()
    manifest_read, manifest_write = os.pipe()
    decoy = os.open(tmp_path / "decoy", os.O_RDWR | os.O_CREAT, 0o600)
    proc = subprocess.Popen(
        (sys.executable, str(driver),
         str(control_guard.fileno()), str(manifest_read), str(decoy)),
        close_fds=True,
        pass_fds=(control_guard.fileno(), manifest_read, decoy),
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    control_guard.close()
    os.close(manifest_read)
    try:
        payload = C.encode_launch_manifest(manifest)
        view = memoryview(payload)
        while view:
            view = view[os.write(manifest_write, view):]
        os.close(manifest_write)
        ready, _, _ = select.select([proc.stdout], [], [], _WATCHDOG_S)
        assert ready, "guard probe produced no exit code"
        assert proc.stdout.readline().strip() == "75"
        assert proc.wait(timeout=_WATCHDOG_S) == 0
    finally:
        control.close()
        os.close(decoy)
        if proc.poll() is None:
            proc.kill()
    with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
        assert connection.execute(
            "SELECT state FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0] == "GRANTED"


def test_run_guard_rejects_lease_fd_equal_to_control():
    from ptest import guard as guard_api

    assert guard_api.run_guard(5, 6, 5) == 70
    assert guard_api.run_guard(5, 6, -2) == 70
    assert guard_api.run_guard(5, 6, True) == 70


def test_guard_holds_lease_while_runner_and_daemon_never_do(case, tmp_path):
    domain = case.domain()
    workdir = domain.root / "work"
    workdir.mkdir()
    runner = tmp_path / "runner_script.py"
    runner.write_text(_RUNNER)
    owner_script = tmp_path / "owner_driver.py"
    owner_script.write_text(_OWNER)
    run_id = "d3" * 16
    barrier = tmp_path / "barrier"
    pidfile = tmp_path / "pids"
    spec = {"root": str(domain.root), "machine_config": str(domain.machine_config),
            "ledger": str(domain.ledger), "marker": str(domain.marker),
            "fixture": domain.fixture, "domain_id": domain.domain_id,
            "run_id": run_id, "workdir": str(workdir),
            "runner": str(runner), "barrier": str(barrier), "pidfile": str(pidfile)}
    def read_pids_file(name):
        try:
            return int((tmp_path / name).read_text())
        except (OSError, ValueError):
            return None

    def kill_all():
        for name in ("pids.runner", "pids.daemon"):
            pid = read_pids_file(name)
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        barrier.touch()

    owner = subprocess.Popen(
        [sys.executable, str(owner_script), json.dumps(spec)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        start_new_session=True)
    guard_pid = runner_pid = daemon_pid = None
    lease_inode = None
    try:
        ready, _, _ = select.select([owner.stdout], [], [], _WATCHDOG_S)
        assert ready, "owner driver produced no guard line"
        first = json.loads(owner.stdout.readline())
        ready, _, _ = select.select([owner.stdout], [], [], _WATCHDOG_S)
        assert ready, "owner driver produced no ready line"
        second = json.loads(owner.stdout.readline())
        assert second.get("ready") is True
        guard_pid, runner_pid, daemon_pid = first["guard"], second["runner"], second["daemon"]
        lease_inode = first["lease_inode"]
        # The guard references the lease inode; the runner and its detached
        # daemon never do.
        assert lease_inode in _fd_inodes(guard_pid)
        assert lease_inode not in _fd_inodes(runner_pid)
        assert lease_inode not in _fd_inodes(daemon_pid)
        if guard_pid is not None:
            try:
                os.kill(guard_pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            owner.stdin.write("exit\n")
            owner.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            owner.wait(timeout=_WATCHDOG_S)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=_WATCHDOG_S)
        # Owner and guard are gone; the detached daemon is still alive: the
        # lock must be acquirable, proving the daemon never held it.
        assert os.path.exists(f"/proc/{daemon_pid}")
        sidecar = leases.read_sidecar(domain.root, run_id)
        assert sidecar.status == "valid"
        assert leases.probe(domain.root, run_id, sidecar) == leases.ACQUIRABLE
    finally:
        if owner.poll() is None:
            try:
                owner.kill()
            except OSError:
                pass
            owner.wait(timeout=_WATCHDOG_S)
        kill_all()
