"""Real-process guard contracts. Only owned fixture processes receive signals."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from ptest import contracts as C, platform, scheduler

_RECOVERY_WATCHDOG_S = 45
_CANCEL_WATCHDOG_S = 10
_FIXTURES = Path(__file__).parent / "fixtures" / "processes"


def _exact(peer, size):
    result = bytearray()
    while len(result) < size:
        chunk = peer.recv(size - len(result))
        assert chunk, "unexpected EOF in fixture protocol"
        result.extend(chunk)
    return bytes(result)


def _frame(peer, manifest):
    prefix = _exact(peer, 4)
    length = struct.unpack(">I", prefix)[0]
    assert length <= C.CONTROL_FRAME_MAX_BYTES
    return C.decode_control_frame(prefix + _exact(peer, length),
                                  expected_nonce=manifest.grant.nonce)


def _cancel(manifest, signum=15):
    return C.encode_control_frame(C.ControlFrame(
        protocol=C.GUARD_PROTOCOL_VERSION,
        run_id=manifest.grant.run_id, nonce=manifest.grant.nonce,
        kind="cancel", payload={"signal": signum}))


def _wire(obj):
    body = json.dumps(obj).encode()
    return struct.pack(">I", len(body)) + body


def _bad_control(manifest, case):
    obj = {"protocol": C.GUARD_PROTOCOL_VERSION,
           "run_id": manifest.grant.run_id,
           "nonce": manifest.grant.nonce, "kind": "cancel", "payload": {"signal": 15}}
    if case == "nonce":
        obj["nonce"] = "c" * 64
    elif case == "run":
        obj["run_id"] = "c" * 32
    elif case == "kind":
        obj["kind"] = "unknown"
    elif case == "payload":
        obj["payload"] = {"signal": 9}
    elif case == "unknown-field":
        obj["unexpected"] = "not-authority"
    elif case == "deep":
        nested = "leaf"
        for _ in range(20):
            nested = [nested]
        obj["payload"]["pad"] = nested
    elif case == "oversize":
        return struct.pack(">I", C.CONTROL_FRAME_MAX_BYTES + 1)
    elif case == "truncated":
        return b"\x00\x00\x00\x05{"
    elif case == "json":
        return b"\x00\x00\x00\x01{"
    return _wire(obj)


class Harness:
    def __init__(self, case, *, domain=None, owner=False, label="a"):
        self.domain = domain or case.domain()
        self.root = self.domain.root / label
        self.root.mkdir()
        self.marker = self.root / "marker"
        self.later = self.root / "later"
        self.control_guard, self.control = socket.socketpair()
        self.control.settimeout(_RECOVERY_WATCHDOG_S)
        self.manifest_read, self.manifest_write = os.pipe()
        self.owner = None
        if owner:
            # The logical invoking controller owns the only other copy of the
            # guard peer after the test closes its observation copy.
            self.owner = subprocess.Popen(
                (sys.executable, str(_FIXTURES / "guard_client.py"),
                 str(self.domain.root), str(self.root), str(self.control.fileno())),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                pass_fds=(self.control.fileno(),),
                close_fds=True, start_new_session=True)
            state = self.owner_poll()
            self.ticket = C.Ticket(**state["ticket"])
            grant = None if state["grant"] is None else C.Grant(**state["grant"])
        else:
            checkout = C.CheckoutIdentity(
                project_id="a" * 32, checkout_id=os.urandom(16).hex(), root=self.root)
            self.ticket = scheduler.enqueue(self.domain, C.AdmissionRequest(
                run_id=os.urandom(16).hex(), checkout=checkout,
                owner=platform.process_identity(os.getpid()), slots=1,
                exclusive=False, fixture=True, deadline=time.monotonic() + 60))
            grant = scheduler.poll(self.domain, self.ticket).grant
        self.grant = grant
        self.manifest = None if grant is None else C.LaunchManifest(
            protocol=C.GUARD_PROTOCOL_VERSION,
            domain=self.domain, grant=grant, setup=None,
            attempts=(self.write(self.marker),), attempt_ids=("a001",),
            setup_timeout_s=5, attempt_timeout_s=10, compound_timeout_s=20)
        self.process = None
        self.listeners = []
        self.peers = []
        self.owned = []
        self.frames = []
        self.auto_decide = True

    def owner_poll(self):
        self.owner.stdin.write(b"poll\n")
        self.owner.stdin.flush()
        assert select.select([self.owner.stdout], [], [], _RECOVERY_WATCHDOG_S)[0]
        return json.loads(self.owner.stdout.readline())

    def write(self, path):
        return C.PreparedRun(argv=(sys.executable, "-c",
            f"from pathlib import Path; Path({str(path)!r}).write_text('ran')"),
            cwd=self.root)

    def write_exit(self, path, code):
        return C.PreparedRun(argv=(
            sys.executable, "-c",
            f"from pathlib import Path; Path({str(path)!r}).write_text('ran'); "
            f"raise SystemExit({code})",
        ), cwd=self.root)

    def listener(self, name):
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.bind(str(self.domain.root / (self.root.name + name)))
        peer.listen(8)
        peer.settimeout(_RECOVERY_WATCHDOG_S)
        self.listeners.append(peer)
        return peer

    def workload(self, mode="signal", *, later=True):
        self.ready = self.listener("r")
        prepared = C.PreparedRun(
            argv=(sys.executable, str(_FIXTURES / "guard_workload.py"),
                  mode, str(self.domain.root / (self.root.name + "r")), str(self.marker)),
            cwd=self.root)
        attempts = (prepared, self.write(self.later)) if later else (prepared,)
        self.manifest = replace(self.manifest, attempts=attempts,
                                attempt_ids=("a001", "a002") if later else ("a001",))
        return prepared

    def start(self, *, stage="", raw=None, queued=None, failure="", stats=False,
              advance=0, decision_timeout=None,
              scan_limit_after_phase=None, stall_poll=None, dump_wait=None,
              stall_timeout=None):
        env = dict(os.environ, GUARD_CONTROL_FD=str(self.control_guard.fileno()),
                   GUARD_MANIFEST_FD=str(self.manifest_read),
                   GUARD_FAILURE=failure,
                   GUARD_ADVANCE_AFTER_FACTS=str(advance),
                   GUARD_DECISION_TIMEOUT=("" if decision_timeout is None
                                           else str(decision_timeout)),
                   GUARD_STALL_POLL=("" if stall_poll is None
                                     else str(stall_poll)),
                   GUARD_DUMP_WAIT=("" if dump_wait is None
                                    else str(dump_wait)),
                   GUARD_STALL_TIMEOUT=("" if stall_timeout is None
                                        else str(stall_timeout)),
                   GUARD_SCAN_LIMIT_AFTER_PHASE=(
                       "" if scan_limit_after_phase is None
                       else str(scan_limit_after_phase)),
                   GUARD_STATS=str(self.root / "stats") if stats else "")
        if stage:
            self.barrier = self.listener("b")
            env.update(GUARD_BARRIER_STAGE=stage,
                       GUARD_BARRIER_PATH=str(self.domain.root / (self.root.name + "b")))
        self.process = subprocess.Popen(
            (sys.executable, str(_FIXTURES / "guard_driver.py")),
            cwd=self.root, env=env, start_new_session=True, close_fds=True,
            pass_fds=(self.control_guard.fileno(), self.manifest_read),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.control_guard.close()
        os.close(self.manifest_read)
        self.manifest_read = -1
        if queued:
            self.control.sendall(queued)
            if queued == b"\x00\x00\x00\x05{":
                self.control.shutdown(socket.SHUT_WR)
        data = C.encode_launch_manifest(self.manifest) if raw is None else raw
        os.write(self.manifest_write, data)
        os.close(self.manifest_write)
        self.manifest_write = -1

    def at_barrier(self):
        deadline = time.monotonic() + _RECOVERY_WATCHDOG_S
        while not select.select([self.barrier], [], [], 0.05)[0]:
            if select.select([self.control], [], [], 0)[0]:
                self.read()
            assert self.process.poll() is None, "guard exited before lifecycle barrier"
            assert time.monotonic() < deadline, "lifecycle barrier watchdog expired"
        peer, _ = self.barrier.accept()
        peer.settimeout(_RECOVERY_WATCHDOG_S)
        assert peer.recv(64)
        self.peers.append(peer)
        return peer

    def running(self, count=1):
        assert self.read().kind == "registered"
        assert self.read().kind == "attempt-ready"
        assert self.read().kind == "phase"
        result = []
        for _ in range(count):
            peer, _ = self.ready.accept()
            peer.settimeout(_RECOVERY_WATCHDOG_S)
            raw = bytearray()
            while not raw.endswith(b"\n"):
                raw.extend(_exact(peer, 1))
            info = json.loads(raw)
            self.peers.append(peer)
            try:
                self.owned.append(psutil.Process(info["pid"]))
            except psutil.NoSuchProcess:
                pass
            result.append((peer, info))
        return result

    def read(self):
        frame = _frame(self.control, self.manifest)
        self.frames.append(frame)
        if self.auto_decide and frame.kind == "attempt-ready":
            self.decision(frame)
        return frame

    def decision(self, ready, **overrides):
        payload = {
            "attempt_id": ready.payload["attempt_id"],
            "generation": ready.payload["generation"],
            "gate_token": ready.payload["gate_token"],
            "action": "continue",
            "reason": None,
        }
        payload.update(overrides)
        self.control.sendall(C.encode_control_frame(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION,
            run_id=self.manifest.grant.run_id,
            nonce=self.manifest.grant.nonce,
            kind="attempt-decision",
            payload=payload,
        )))

    def finish(self, timeout=_RECOVERY_WATCHDOG_S):
        # Read while alive; an implementation may not rely on a large socket
        # send buffer or let its caller wait before consuming frames.
        while self.control.fileno() >= 0:
            ready, _, _ = select.select([self.control], [], [], timeout)
            assert ready, "guard control watchdog expired"
            if not self.control.recv(1, socket.MSG_PEEK):
                break
            self.read()
        self.process.wait(timeout=timeout)
        # An escaped fixture can retain stdio after guard exit. These tiny
        # fixtures have bounded output: read what's present without confusing
        # pipe EOF with guard completion or waiting for an escaped writer.
        output = []
        for stream in (self.process.stdout, self.process.stderr):
            chunks = bytearray()
            while select.select([stream], [], [], 0)[0]:
                data = os.read(stream.fileno(), 65536)
                if not data:
                    break
                chunks.extend(data)
            output.append(bytes(chunks))
        stdout, stderr = output
        assert not stderr, stderr.decode(errors="replace")
        return self.process.returncode, stdout

    def row(self):
        with sqlite3.connect(self.domain.ledger) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM jobs WHERE run_id=?",
                                     (self.ticket.run_id,)).fetchone())

    def kill_owner(self):
        self.control.close()
        self.owner.kill()
        self.owner.wait(timeout=_RECOVERY_WATCHDOG_S)

    def close(self):
        # These socket peers are capabilities to our tiny fixture workloads.
        for peer in self.peers:
            peer.close()
        self.control.close()
        self.control_guard.close()
        for fd in (self.manifest_read, self.manifest_write):
            if fd >= 0:
                os.close(fd)
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=_RECOVERY_WATCHDOG_S)
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()
        if self.owner is not None:
            if self.owner.poll() is None:
                self.owner.kill()
            self.owner.wait(timeout=_RECOVERY_WATCHDOG_S)
            self.owner.stdin.close()
            self.owner.stdout.close()
        for proc in self.owned:
            try:
                if proc.is_running():
                    proc.kill()
                proc.wait(timeout=_RECOVERY_WATCHDOG_S)
            except psutil.NoSuchProcess:
                pass
        for listener in self.listeners:
            listener.close()


@pytest.fixture
def harness(case):
    # Linux permits reaping grandchildren after the deliberately killed guard.
    # Each wait below names an observed fixture PID; never waitpid(-1).
    libc = None
    previous = ctypes.c_int()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
        assert libc.prctl(36, 1, 0, 0, 0) == 0
    created = []

    def create(**kwargs):
        h = Harness(case, **kwargs)
        created.append(h)
        return h

    yield create
    for h in reversed(created):
        h.close()
    if libc:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def test_guard_accepts_launcher_created_session_and_real_draining(harness):
    h = harness()
    h.start(stage="drained")
    gate = h.at_barrier()
    assert h.row()["state"] == "DRAINING"
    assert platform.probe_group(h.process.pid).exists is True
    assert h.row()["final_status"] is None
    gate.sendall(b"g")
    assert h.finish()[0] == 0
    assert h.marker.read_text() == "ran"
    assert h.frames[0].payload["guard"]["pgid"] == h.process.pid
    assert h.frames[-1].kind == "draining"
    assert platform.probe_group(h.process.pid).exists is False
    assert scheduler.poll(h.domain, h.ticket).state is C.LeaseState.DRAINING
    proof = scheduler.begin_finalization(h.domain, h.grant)
    assert proof.group_absent and proof.pgid == h.process.pid
    assert scheduler.poll(h.domain, h.ticket).state is C.LeaseState.FINALIZING


def test_wrong_authenticated_attempt_decision_never_launches_child(harness):
    h = harness()
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    ready = h.read()
    assert ready.kind == "attempt-ready"
    h.decision(ready, gate_token="f" * 32)
    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["state"] == "DRAINING"


def test_replayed_attempt_decision_cannot_launch_later_attempt(harness):
    h = harness()
    h.auto_decide = False
    h.manifest = replace(
        h.manifest,
        attempts=(h.write(h.marker), h.write(h.later)),
        attempt_ids=("a001", "a002"),
    )
    h.start()
    assert h.read().kind == "registered"
    first_ready = h.read()
    assert first_ready.kind == "attempt-ready"
    h.decision(first_ready)
    assert h.read().kind == "phase"
    assert h.read().kind == "runner-facts"
    second_ready = h.read()
    assert second_ready.kind == "attempt-ready"
    h.decision(first_ready)
    assert h.finish()[0] == 70
    assert h.marker.read_text() == "ran"
    assert not h.later.exists()


@pytest.mark.parametrize("later_code", [0, 1])
def test_ordered_attempt_gates_preserve_earlier_exit_after_setup(
        harness, later_code):
    h = harness()
    h.manifest = replace(
        h.manifest,
        setup=h.write(h.root / "setup"),
        attempts=(h.write_exit(h.marker, 23),
                  h.write_exit(h.later, later_code)),
        attempt_ids=("a001", "a002"),
    )

    h.start()
    assert h.finish()[0] == 0

    assert (h.root / "setup").read_text() == "ran"
    assert h.marker.read_text() == "ran"
    assert h.later.read_text() == "ran"
    ready = [frame.payload for frame in h.frames
             if frame.kind == "attempt-ready"]
    assert [(item["attempt_id"], item["previous_attempt_id"])
            for item in ready] == [("a001", None), ("a002", "a001")]
    assert ready[0]["gate_token"] != ready[1]["gate_token"]
    facts = [frame.payload for frame in h.frames
             if frame.kind == "runner-facts"]
    assert [(item["phase"], item["attempt_id"], item["raw_exit_code"])
            for item in facts] == [
                ("setup", "a001", 0),
                ("execution", "a001", 23),
                ("execution", "a002", later_code),
            ]


def test_early_authenticated_decision_is_not_future_authority(harness):
    h = harness()
    h.auto_decide = False
    early = C.ControlFrame(
        protocol=C.GUARD_PROTOCOL_VERSION,
        run_id=h.manifest.grant.run_id,
        nonce=h.manifest.grant.nonce,
        kind="attempt-decision",
        payload={
            "attempt_id": "a001",
            "generation": h.manifest.grant.generation,
            "gate_token": "d" * 32,
            "action": "continue",
            "reason": None,
        },
    )

    h.start(queued=C.encode_control_frame(early))

    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["guard_pid"] is None


@pytest.mark.parametrize("fault", ["generation", "wrong-direction"])
def test_misdirected_or_wrong_generation_decision_never_launches(
        harness, fault):
    h = harness()
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    ready = h.read()
    assert ready.kind == "attempt-ready"
    if fault == "generation":
        h.decision(ready, generation=ready.payload["generation"] + 1)
    else:
        h.control.sendall(C.encode_control_frame(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION,
            run_id=h.manifest.grant.run_id,
            nonce=h.manifest.grant.nonce,
            kind="phase",
            payload={"phase": "execution", "attempt_id": "a001"},
        )))

    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["state"] == "DRAINING"


def test_authenticated_stop_closes_spawn_without_synthetic_facts(harness):
    h = harness()
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    ready = h.read()
    assert ready.kind == "attempt-ready"
    h.decision(ready, action="stop", reason="unknown-input")

    assert h.finish()[0] == 0
    assert not h.marker.exists()
    assert not any(frame.kind in {"phase", "runner-facts"}
                   for frame in h.frames)
    assert h.frames[-1].kind == "draining"


def test_attempt_gate_eof_cancellation_and_watchdog_never_launch(harness):
    eof = harness(label="eof")
    eof.auto_decide = False
    eof.start()
    assert eof.read().kind == "registered"
    assert eof.read().kind == "attempt-ready"
    eof.control.shutdown(socket.SHUT_WR)
    assert eof.finish()[0] == 0
    assert not eof.marker.exists()
    assert eof.row()["state"] == "DRAINING"

    cancelled = harness(label="cancelled")
    cancelled.auto_decide = False
    cancelled.start()
    assert cancelled.read().kind == "registered"
    assert cancelled.read().kind == "attempt-ready"
    cancelled.control.sendall(_cancel(cancelled.manifest))
    assert cancelled.finish()[0] == 143
    assert not cancelled.marker.exists()
    assert cancelled.row()["state"] == "DRAINING"

    timed = harness(label="timed")
    timed.auto_decide = False
    timed.start(decision_timeout=0.2)
    assert timed.read().kind == "registered"
    ready = timed.read()
    assert ready.kind == "attempt-ready"
    assert ready.payload["deadline_monotonic"] > 0
    assert timed.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 70
    assert not timed.marker.exists()
    assert timed.row()["state"] == "DRAINING"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("delivery", ["pid", "group"])
def test_signal_while_waiting_for_attempt_decision_never_launches(
        harness, signum, delivery):
    h = harness()
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"

    if delivery == "pid":
        os.kill(h.process.pid, signum)
    else:
        os.killpg(h.process.pid, signum)

    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 128 + signum
    assert not h.marker.exists()
    assert not any(frame.kind in {"phase", "runner-facts"}
                   for frame in h.frames)
    assert h.frames[-1].kind == "draining"


def test_parent_sigkill_while_attempt_gate_is_pending_never_launches(
        harness):
    h = harness(owner=True)
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"

    h.kill_owner()

    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    assert not h.marker.exists()
    assert not any(frame.kind in {"phase", "runner-facts"}
                   for frame in h.frames)
    assert h.row()["state"] == "DRAINING"


@pytest.mark.parametrize("bad", ["unknown-field", "deep"])
def test_adverse_frame_at_pending_gate_preserves_neighbor_sentinel(
        harness, bad):
    h = harness(owner=True)
    h.auto_decide = False
    h.start()
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"

    h.control.sendall(_bad_control(h.manifest, bad))

    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 70
    assert not h.marker.exists()
    assert not any(frame.kind in {"phase", "runner-facts"}
                   for frame in h.frames)
    assert h.owner.poll() is None
    assert h.row()["state"] == "DRAINING"


@pytest.mark.parametrize("signum", [2, 15])
@pytest.mark.parametrize("delivery", ["frame", "pid", "group"])
def test_cooperative_cancel_forwards_signal_reaps_then_drains(harness, signum, delivery):
    h = harness()
    h.workload()
    h.start(stage="drain")
    _, info = h.running()[0]
    start = time.monotonic()
    if delivery == "frame":
        h.control.sendall(_cancel(h.manifest, signum) * 2)
    elif delivery == "pid":
        os.kill(h.process.pid, signum)
    else:
        os.killpg(h.process.pid, signum)
    gate = h.at_barrier()
    assert h.marker.read_text() == str(signum)
    assert not psutil.pid_exists(info["pid"]), "direct child was not reaped before handoff"
    assert not h.later.exists()
    gate.sendall(b"g")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 128 + signum
    assert time.monotonic() - start < _CANCEL_WATCHDOG_S
    assert h.frames[-1].kind == "draining"
    assert h.row()["state"] == "DRAINING"


@pytest.mark.parametrize("signum", [2, 15])
@pytest.mark.parametrize("stage", ["manifest", "register", "registered"])
def test_signal_before_manifest_or_registration_stops_work(harness, signum, stage):
    h = harness()
    h.start(stage=stage)
    gate = h.at_barrier()
    os.kill(h.process.pid, signum)
    gate.sendall(b"g")
    assert h.finish()[0] == 128 + signum
    if stage == "manifest":
        assert h.row()["guard_pid"] is None
    else:
        assert h.row()["state"] == "DRAINING"
        assert h.frames[-1].kind == "draining"
    assert not h.marker.exists()


def test_unread_notifications_hit_bounded_send_deadline_and_close_spawn(harness):
    h = harness()
    h.control_guard.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    h.manifest = replace(h.manifest,
        attempts=tuple(h.write(h.root / f"attempt-{i}") for i in range(10)),
        attempt_ids=tuple(f"a{i + 1:03}" for i in range(10)))
    h.start()
    # Intentionally do not drain the tiny control buffer. This is a deadline
    # test; the watchdog is a failure bound, not synchronization by sleeping.
    assert h.process.wait(timeout=5) == 70
    assert h.finish()[0] == 70
    assert h.row()["state"] == "DRAINING"
    assert not any(f.kind == "draining" for f in h.frames)
    assert not (h.root / "attempt-9").exists()


def test_partial_active_frame_does_not_block_execution_deadline(harness):
    h = harness()
    h.workload()
    # Below the guard's 2.0 s frame deadline, so a deadline blocked behind
    # the partial frame would end as a protocol failure (70), never 143;
    # long enough that a slow workload start cannot outrun it.
    timeout_s = 1.5
    h.manifest = replace(h.manifest, attempt_timeout_s=timeout_s)
    h.start()
    h.running()
    spawned_at = h.owned[0].create_time()
    h.control.sendall(b"\x00\x00")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    # The attempt deadline fired on time, measured from the workload's own
    # start rather than from a fixed point that slow startup can consume;
    # 1.5 s is the lateness the original send-relative bound allowed.
    assert time.time() - (spawned_at + timeout_s) < 1.5
    facts = [f.payload for f in h.frames if f.kind == "runner-facts"]
    assert facts[0]["problem"]["code"] == "execution-timeout"
    assert not h.later.exists()


@pytest.mark.parametrize("bad", ["nonce", "run", "kind", "payload", "oversize", "truncated", "json"])
def test_bad_control_before_registration_cannot_launch(harness, bad):
    h = harness()
    h.start(queued=_bad_control(h.manifest, bad))
    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["guard_pid"] is None


@pytest.mark.parametrize("bad", ["nonce", "run", "oversize", "truncated"])
def test_bad_active_control_cancels_and_reaps_owned_child(harness, bad):
    h = harness()
    h.workload()
    h.start()
    _, info = h.running()[0]
    h.control.sendall(_bad_control(h.manifest, bad))
    if bad == "truncated":
        h.control.shutdown(socket.SHUT_WR)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 70
    assert not psutil.pid_exists(info["pid"])
    assert h.marker.read_text() == "15"
    assert not h.later.exists()
    assert h.row()["state"] == "DRAINING"


@pytest.mark.parametrize("raw", [
    struct.pack(">I", C.MANIFEST_MAX_BYTES + 1),
    b"\x00\x00\x00\x05{",
    b"\x00\x00\x00\x01{",
])
def test_invalid_manifest_never_registers(harness, raw):
    h = harness()
    h.start(raw=raw)
    assert h.finish()[0] == 70
    assert h.row()["guard_pid"] is None
    assert not h.marker.exists()


@pytest.mark.parametrize("signum", [2, 15])
def test_queued_cancel_forbids_first_spawn(harness, signum):
    h = harness()
    h.start(queued=_cancel(h.manifest, signum))
    assert h.finish()[0] == 128 + signum
    assert not h.marker.exists()
    assert not any(f.kind == "phase" for f in h.frames)


def test_missing_executable_closes_all_later_attempts(harness):
    h = harness()
    missing = C.PreparedRun(argv=(str(h.root / "missing"),), cwd=h.root)
    h.manifest = replace(h.manifest, attempts=(missing, h.write(h.later)),
                         attempt_ids=("a001", "a002"))
    h.start()
    assert h.finish()[0] == 0  # guard success is only a provisional handoff
    facts = [f.payload for f in h.frames if f.kind == "runner-facts"]
    assert len(facts) == 1
    assert facts[0]["raw_exit_code"] is None
    assert facts[0]["problem"]["code"] == "missing-executable"
    assert not h.later.exists()


@pytest.mark.parametrize("scope", ["attempt", "compound"])
def test_timeout_fact_preserves_raw_status_and_closes_spawn(harness, scope):
    h = harness()
    h.workload()
    # Long enough that a slow workload start cannot outrun the deadline
    # before the harness sees it running; nothing here measures latency.
    h.manifest = replace(h.manifest, attempt_timeout_s=2.0 if scope == "attempt" else None,
                         compound_timeout_s=2.0 if scope == "compound" else 20)
    h.start()
    h.running()
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    facts = [f.payload for f in h.frames if f.kind == "runner-facts"]
    assert facts[0]["raw_exit_code"] == 0
    assert facts[0]["problem"]["code"] == "execution-timeout"
    assert scope in facts[0]["problem"]["message"]
    assert not h.later.exists()
    assert h.frames[-1].kind == "draining"


def test_active_eof_reaps_without_repolling_closed_peer_and_drains(harness):
    h = harness(owner=True)
    h.workload("wait")
    h.start(stats=True)
    peer, info = h.running()[0]
    h.kill_owner()
    # Child completion is a barrier, not a timer. Driver counts actual EOF reads.
    peer.sendall(b"g")
    assert h.finish()[0] == 0
    assert h.marker.read_text() == "finished"
    assert not h.later.exists()
    assert not psutil.pid_exists(info["pid"])
    assert json.loads((h.root / "stats").read_text())["eof_reads"] <= 1
    assert h.row()["state"] == "DRAINING"
    start = time.monotonic()
    assert scheduler.poll(h.domain, h.ticket).state is C.LeaseState.RELEASED
    assert time.monotonic() - start < 30
    assert h.row()["final_status"] is None


@pytest.mark.parametrize("stage", ["manifest", "register", "registered", "fork", "drain", "drained"])
def test_controller_sigkill_at_lifecycle_barriers(harness, stage):
    h = harness(owner=True)
    h.manifest = replace(h.manifest, attempts=(h.write(h.marker), h.write(h.later)),
                         attempt_ids=("a001", "a002"))
    h.start(stage=stage)
    gate = h.at_barrier()
    h.kill_owner()
    gate.sendall(b"g")
    code, _ = h.finish()
    if stage in {"manifest", "register"}:
        assert code == (70 if stage == "manifest" else 75)
        assert h.row()["guard_pid"] is None
    else:
        assert code == 0
        assert h.row()["state"] == "DRAINING"
    if stage in {"manifest", "register", "registered", "fork"}:
        assert not h.marker.exists()
        assert not h.later.exists()
    assert h.row()["final_status"] is None


@pytest.mark.parametrize("phase", ["register", "drain"])
def test_scheduler_failure_is_contained_and_has_no_draining_notification(harness, phase):
    h = harness()
    h.start(failure=phase)
    assert h.finish()[0] == 70
    assert not any(f.kind == "draining" for f in h.frames)
    if phase == "register":
        assert not h.marker.exists()


def test_revoked_real_grant_refuses_late_guard(harness):
    h = harness(owner=True)
    h.owner.kill()
    h.owner.wait(timeout=_RECOVERY_WATCHDOG_S)
    assert scheduler.poll(h.domain, h.ticket).state is C.LeaseState.CANCELLED
    h.start()
    assert h.finish()[0] == 75
    assert not h.marker.exists()
    assert h.row()["guard_pid"] is None


@pytest.mark.parametrize("mode", ["ignore", "grandchild"])
def test_forced_kill_retains_charge_and_never_notifies_draining(harness, mode):
    h = harness(owner=True)
    h.workload(mode)
    h.start()
    owned = h.running(count=2 if mode == "grandchild" else 1)
    start = time.monotonic()
    h.control.sendall(_cancel(h.manifest))
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == -signal.SIGKILL
    assert time.monotonic() - start < _CANCEL_WATCHDOG_S
    assert not any(f.kind == "draining" for f in h.frames)
    assert not h.later.exists()
    assert h.row()["state"] == "RUNNING"
    follower = harness(domain=h.domain, owner=True, label="q")
    assert follower.grant is None
    # After guard reap, the harness reaps its adopted owned descendants. The
    # kernel group cannot be declared absent until those zombies disappear.
    for _, info in owned:
        try:
            os.waitpid(info["pid"], 0)
        except ChildProcessError:
            pass
    assert platform.probe_group(h.process.pid).exists is False
    # Forced kill left no DRAINING. A live caller still owns finalization;
    # orphan recovery may release only after all independent absence checks.
    assert follower.owner_poll()["grant"] is None
    h.kill_owner()
    start = time.monotonic()
    assert follower.owner_poll()["grant"] is not None
    assert time.monotonic() - start < 30
    assert h.row()["state"] == "RELEASED"
    assert h.row()["final_status"] is None


def test_exited_direct_child_with_live_descendant_never_reaches_next_gate(
        harness):
    h = harness(owner=True)
    h.workload("orphan")
    h.start()
    owned = h.running(count=2)

    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == -signal.SIGKILL
    assert any(info["mode"] == "ignore" for _, info in owned)
    assert not h.later.exists()
    assert not any(
        frame.kind == "attempt-ready"
        and frame.payload["attempt_id"] == "a002"
        for frame in h.frames)
    assert h.row()["state"] == "RUNNING"
    follower = harness(domain=h.domain, owner=True, label="q")
    assert follower.grant is None


def test_observation_limit_exhaustion_never_reaches_next_gate_or_releases(
        harness):
    h = harness(owner=True)
    h.manifest = replace(
        h.manifest,
        attempts=(h.write(h.marker), h.write(h.later)),
        attempt_ids=("a001", "a002"),
    )
    # The test driver lowers the real guard's bounded group scan only after
    # the first phase is authorized, so this exercises predecessor evidence
    # rather than blocking admission or the first decision gate.
    h.start(scan_limit_after_phase=0)

    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == -signal.SIGKILL
    assert h.marker.read_text() == "ran"
    assert not h.later.exists()
    assert not any(
        frame.kind == "attempt-ready"
        and frame.payload["attempt_id"] == "a002"
        for frame in h.frames)
    assert h.row()["state"] == "RUNNING"
    follower = harness(domain=h.domain, owner=True, label="q")
    assert follower.grant is None


def test_observed_escape_retains_charge_and_unrelated_sentinel_survives(harness):
    h = harness(owner=True)
    h.workload("escaped")
    h.start()
    owned = h.running(count=2)
    escaped = next(info for _, info in owned if info["mode"] == "ignore")
    assert escaped["pgid"] != h.process.pid
    # The poll persists actual scheduler ancestry observations before detachment
    # loses its parent; the guard never signals stored escaped identities.
    assert scheduler.poll(h.domain, h.ticket).state is C.LeaseState.UNCERTAIN
    h.control.sendall(_cancel(h.manifest))
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 75
    assert psutil.pid_exists(escaped["pid"])
    assert h.owner.poll() is None  # unrelated session sentinel
    assert not any(f.kind == "draining" for f in h.frames)
    h.kill_owner()
    state = scheduler.poll(h.domain, h.ticket)
    assert state.state is C.LeaseState.UNCERTAIN
    assert h.row()["reason_code"] == "unsupported-detached-descendant"


def test_private_fds_literal_argv_env_and_stdio(harness):
    h = harness()
    prepared = h.workload("fdcheck", later=False)
    literals = ("; echo injected", "$HOME", "space value", "*.py")
    prepared = replace(prepared,
        argv=prepared.argv + (str(h.control_guard.fileno()), str(h.manifest_read)) + literals,
        env_updates=(("LITERAL_TEST", "$HOME; literal"),))
    h.manifest = replace(h.manifest, attempts=(prepared,))
    h.start()
    h.running()
    # This fixture deliberately emits stderr; consume without finish's quiet
    # assertion to prove inherited descriptors were unchanged.
    while h.control.recv(1, socket.MSG_PEEK):
        h.read()
    out, err = h.process.communicate(timeout=_RECOVERY_WATCHDOG_S)
    assert h.process.returncode == 0
    facts = json.loads(h.marker.read_text())
    assert facts == {"fds": ["closed", "closed"], "argv": list(literals),
                     "env": "$HOME; literal", "private_env": []}
    assert out == b"literal stdout\n"
    assert err == b"literal stderr\n"


@pytest.mark.parametrize("budget", [1, 4])
def test_six_independent_clients_obey_grants_and_recover_only_after_owner_death(case, harness, budget):
    domain = case.domain(slots=budget, jobs=budget)
    clients = [harness(domain=domain, owner=True, label=f"c{i}") for i in range(6)]
    assert len({h.owner.pid for h in clients}) == 6
    assert [h.ticket.sequence for h in clients] == list(range(1, 7))
    assert sum(h.grant is not None for h in clients) == budget
    running = []
    for h in clients[:budget]:
        h.workload("wait", later=False)
        h.start()
        peer, _ = h.running()[0]
        running.append((h, peer))
    for h in clients[budget:]:
        assert h.owner_poll()["state"] == "QUEUED"
        assert not h.marker.exists()
    # Completing a guard alone retains the checkout/capacity for its live
    # finalizer. Only proof of this owner's death permits incomplete recovery.
    first, peer = running[0]
    peer.sendall(b"g")
    assert first.finish()[0] == 0
    follower = clients[budget]
    assert follower.owner_poll()["state"] == "QUEUED"
    assert first.row()["state"] == "DRAINING"
    assert platform.probe_group(first.process.pid).exists is False
    proof = scheduler.begin_finalization(first.domain, first.grant)
    assert proof.group_absent and proof.pgid == first.process.pid
    assert follower.owner_poll()["state"] == "QUEUED"
    assert first.row()["state"] == "FINALIZING"
    start = time.monotonic()
    first.kill_owner()
    admitted = follower.owner_poll()
    assert admitted["grant"] is not None
    assert time.monotonic() - start < 30
    assert first.row()["state"] == "RELEASED"
    assert first.row()["final_status"] is None
    # Every actual guard publishes exactly the admitted one-slot identity.
    for h, _ in running:
        assert h.frames[0].payload["guard"]["pid"] == h.process.pid
        assert h.row()["slots"] == 1


@pytest.mark.parametrize("signum", [2, 15])
def test_queued_controller_group_signal_cancels_without_guard(harness, signum):
    held = harness(owner=True)
    queued = harness(domain=held.domain, owner=True, label="q")
    assert queued.grant is None
    os.killpg(queued.owner.pid, signum)
    try:
        rc = queued.owner.wait(timeout=_CANCEL_WATCHDOG_S)
    except subprocess.TimeoutExpired:
        # An idle fixture can wait whole seconds for a timeslice/page-in
        # while hot paths sail through under box memory pressure; the group
        # signal is pending, never lost, so grant the watchdog once more
        # rather than failing a healthy controller. A controller that truly
        # ignores the signal still times out and fails below.
        if queued.owner.poll() is None:
            rc = queued.owner.wait(timeout=_CANCEL_WATCHDOG_S)
        else:
            rc = queued.owner.returncode
    assert rc == 128 + signum
    assert scheduler.poll(queued.domain, queued.ticket).state is C.LeaseState.CANCELLED
    assert queued.row()["guard_pid"] is None
    assert not queued.marker.exists()


@pytest.mark.parametrize("signum", [2, 15])
def test_active_controller_group_signal_forwards_private_cancel(harness, signum):
    h = harness(owner=True)
    h.workload()
    h.start()
    h.running()
    os.killpg(h.owner.pid, signum)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 128 + signum
    assert h.marker.read_text() == str(signum)
    assert h.frames[-1].kind == "draining"
    assert not h.later.exists()


def test_eof_channel_never_reads_or_selects_its_closed_peer_again(harness, monkeypatch):
    from ptest import guard
    h = harness()
    guard_peer, parent_peer = socket.socketpair()
    try:
        os.set_blocking(guard_peer.fileno(), False)
        state = guard._State()
        control = guard._Control(guard_peer.fileno(), h.manifest, state)
        parent_peer.close()
        control.poll()
        assert state.spawn_closed

        def forbidden_read(*_):
            raise AssertionError("EOF fd was read again")

        original_select = select.select

        def checked_select(read, write, error, timeout):
            assert guard_peer.fileno() not in read
            return original_select(read, write, error, timeout)

        monkeypatch.setattr(guard.os, "read", forbidden_read)
        monkeypatch.setattr(guard.select, "select", checked_select)
        for _ in range(3):
            control.poll(0.001)
    finally:
        parent_peer.close()
        guard_peer.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux zombie group evidence")
def test_group_presence_until_guard_is_reaped(harness):
    h = harness()
    h.start()
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"
    deadline = time.monotonic() + _RECOVERY_WATCHDOG_S
    owned = psutil.Process(h.process.pid)
    while owned.status() != psutil.STATUS_ZOMBIE:
        assert time.monotonic() < deadline, "guard exit watchdog expired"
        select.select([], [], [], 0.01)
    # No Popen.poll/wait has reaped it yet. A completed process still keeps
    # the kernel group present until its parent performs the wait.
    assert platform.probe_group(h.process.pid).exists is True
    assert h.row()["state"] == "DRAINING"
    assert h.row()["final_status"] is None
    assert h.finish()[0] == 0
    assert platform.probe_group(h.process.pid).exists is False


def test_compound_deadline_emits_no_facts_for_unstarted_attempt(harness):
    h = harness()
    h.manifest = replace(h.manifest, setup=h.write(h.root / "setup"),
                         attempts=(h.write(h.marker), h.write(h.later)),
                         attempt_ids=("a001", "a002"), setup_timeout_s=5,
                         attempt_timeout_s=5, compound_timeout_s=5)
    # Advance only the guard clock after each completed fact: each phase takes
    # less than its own bound, but setup + first attempt exceed the compound.
    h.start(advance=3)
    assert h.finish()[0] == 143
    assert (h.root / "setup").read_text() == "ran"
    assert h.marker.read_text() == "ran"
    assert not h.later.exists()
    facts = [f.payload for f in h.frames if f.kind == "runner-facts"]
    assert [(fact["phase"], fact["attempt_id"]) for fact in facts] == [
        ("setup", "a001"), ("execution", "a001")]
    assert not any(
        frame.kind == "runner-facts"
        and frame.payload["attempt_id"] == "a002"
        for frame in h.frames)
    assert h.frames[-1].kind == "draining"


def test_manifest_trailing_bytes_cannot_authorize_repository_work(harness):
    h = harness()
    h.start(raw=C.encode_launch_manifest(h.manifest) + b"untrusted trailing bytes")
    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["guard_pid"] is None


# --- Parallel tier (T2): xdist-style fanout leaves no live survivors ---

_FANOUT_SCRIPT = ";".join([
    "import json, os, socket, subprocess, sys",
    "ready, marker = sys.argv[1:3]",
    "kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
    " for _ in range(4)]",
    "open(marker + '.pids', 'w').write('\\n'.join(str(kid.pid) for kid in kids))",
    "peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
    "peer.connect(ready)",
    "peer.sendall(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(),"
    " 'mode': 'fanout'}).encode() + b'\\n')",
    "peer.recv(1)",
])


def _fanout(harness, **manifest_overrides):
    h = harness()
    h.ready = h.listener("r")
    ready_path = str(h.domain.root / (h.root.name + "r"))
    prepared = C.PreparedRun(
        argv=(sys.executable, "-c", _FANOUT_SCRIPT, ready_path, str(h.marker)),
        cwd=h.root)
    h.manifest = replace(h.manifest, attempts=(prepared,),
                         attempt_ids=("a001",), **manifest_overrides)
    return h


def _no_live_process(pid):
    """True once pid is gone or an un-reaped zombie (dead either way).

    The harness makes the test process a subreaper, so group-killed
    grandchildren reparent here as zombies until reaped. A zombie holds no
    resources and can never run again; reap ours so the group probe below
    observes the empty group.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        status = proc.status()
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False
    if status != psutil.STATUS_ZOMBIE:
        return False
    try:
        ours = proc.ppid() == os.getpid()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True
    if ours:
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError, PermissionError, OSError):
            pass
    return True


def _fanout_pids(h):
    pids = [int(line) for line in
            (h.marker.parent / (h.marker.name + ".pids")).read_text().split()]
    assert len(pids) == 4
    return pids


def _assert_fanout_dead(h, info):
    watched = _fanout_pids(h) + [info["pid"]]
    deadline = time.monotonic() + 10
    while True:
        alive = [pid for pid in watched if not _no_live_process(pid)]
        if not alive:
            return
        assert time.monotonic() < deadline, f"fanout survivors: {alive}"
        time.sleep(0.05)


def _assert_fanout_group_gone(info):
    # After the guard driver itself has exited and every fanout pid is
    # reaped, no process group member may remain.
    with pytest.raises(ProcessLookupError):
        os.killpg(info["pgid"], 0)


def test_fanout_children_leave_no_survivors_on_timeout(harness):
    h = _fanout(harness, attempt_timeout_s=1)
    h.start()
    _, info = h.running()[0]
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    facts = [f.payload for f in h.frames if f.kind == "runner-facts"]
    assert facts[0]["problem"]["code"] == "execution-timeout"
    _assert_fanout_dead(h, info)
    _assert_fanout_group_gone(info)


def test_fanout_children_leave_no_survivors_on_cancel(harness):
    h = _fanout(harness)
    h.start(stage="drain")
    _, info = h.running()[0]
    h.control.sendall(_cancel(h.manifest))
    gate = h.at_barrier()
    _assert_fanout_dead(h, info)
    gate.sendall(b"g")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert h.frames[-1].kind == "draining"
    _assert_fanout_group_gone(info)


# --- Group-quiescence scan under parallel load --------------------------------
#
# The quiescence scan reads the machine-global process table on a bounded
# budget. Under a parallel suite a transient scheduling stall can exhaust
# that budget while the group is already quiescent; the scan must retry a
# bounded number of times before failing closed, or verdict lines flip
# intermittently (e.g. a second "ptest: failed" line reads "incomplete").

def _quiescent_identity():
    from types import SimpleNamespace

    return SimpleNamespace(pid=os.getpid(), pgid=-1)


def test_transient_scan_stall_retries_before_reporting_cleanup(monkeypatch):
    import ptest.guard as guard_module

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise psutil.Error("transient observation stall")
        return iter(())

    monkeypatch.setattr(guard_module.psutil, "process_iter", flaky)

    assert guard_module._group_needs_cleanup(_quiescent_identity()) is False
    assert len(calls) == 2


def test_persistent_scan_outage_still_fails_closed(monkeypatch):
    import ptest.guard as guard_module

    # The retry budget is a deadline, not a pass count: pin the verdict with
    # a small deadline so a permanently blind table still fails closed fast.
    monkeypatch.setattr(guard_module, "_GROUP_SCAN_DEADLINE_S", 0.4)
    calls = []

    def blind():
        calls.append(1)
        raise psutil.Error("process table unavailable")

    monkeypatch.setattr(guard_module.psutil, "process_iter", blind)

    started = time.monotonic()
    assert guard_module._group_needs_cleanup(_quiescent_identity()) is True
    assert time.monotonic() - started < 10
    assert len(calls) > 1


def test_observed_group_member_fails_fast_without_retry(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        calls = []

        def counting():
            calls.append(1)
            # One real same-group member, first in iteration order, so
            # the verdict cannot depend on table size or scan budget.
            return iter([psutil.Process(proc.pid)])

        monkeypatch.setattr(guard_module.psutil, "process_iter", counting)
        identity = SimpleNamespace(pid=os.getpid(), pgid=os.getpgrp())

        assert guard_module._group_needs_cleanup(identity) is True
        assert len(calls) == 1
    finally:
        proc.kill()
        proc.wait()


# --- Slow-scan and contended-reconcile twins ----------------------------------
#
# Under load the per-pass scan budget is exhausted on every pass while the
# group is already quiescent, and the guard's read-only reconcile collides
# with the parent's write transactions. Both are transient: the guard must
# wait for a complete clean pass / a readable snapshot instead of reporting
# "a prior phase still has live descendants" or a failed handoff.

def test_slow_but_complete_scan_waits_for_a_clean_pass(monkeypatch):
    import ptest.guard as guard_module

    calls = []

    def sluggish():
        calls.append(1)
        if len(calls) <= 3:
            # Each of the first passes exceeds the per-pass budget
            # mid-iteration (a foreign pid is yielded only after the stall),
            # so every one of those passes is incomplete, not clean.
            time.sleep(0.3)
        return iter([psutil.Process(1)])

    monkeypatch.setattr(guard_module.psutil, "process_iter", sluggish)

    assert guard_module._group_needs_cleanup(_quiescent_identity()) is False
    assert len(calls) == 4


def _monotonic_only_clock(monkeypatch):
    """Mirror the guard driver's fake clock: monotonic exists, sleep does not."""
    import time as realtime
    import ptest.guard as guard_module
    from types import SimpleNamespace

    monkeypatch.setattr(
        guard_module, "time", SimpleNamespace(monotonic=realtime.monotonic))
    return guard_module


def test_scan_retry_survives_monotonic_only_clock(monkeypatch):
    import psutil as psutil_module

    guard_module = _monotonic_only_clock(monkeypatch)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise psutil_module.Error("transient observation stall")
        return iter(())

    monkeypatch.setattr(guard_module.psutil, "process_iter", flaky)

    # The retry pacing must not use time.sleep: under the driver's fake
    # clock that attribute does not exist and the guard would exit 70 with
    # no facts instead of waiting out the stall.
    assert guard_module._group_needs_cleanup(_quiescent_identity()) is False
    assert len(calls) == 2


def test_reconcile_retry_survives_monotonic_only_clock(monkeypatch):
    from ptest.storage import TransientContention

    guard_module = _monotonic_only_clock(monkeypatch)
    reads = []

    def flaky(domain):
        reads.append(1)
        if len(reads) == 1:
            raise TransientContention(message="coordinator is busy",
                                      phase="scheduler")
        return ()

    monkeypatch.setattr(guard_module.scheduler, "reconcile", flaky)

    assert guard_module._reconcile_for_spawn(object()) == ()
    assert len(reads) == 2


def test_lease_view_shares_its_deadline_with_reconcile_retries(monkeypatch):
    import ptest.guard as guard_module
    from ptest.storage import TransientContention

    # Retry budgets compose, never stack: even with a generous reconcile
    # budget, the shorter lease-view deadline owns the wait. Without
    # sharing, one spawn decision burns scan + reconcile + view end to end
    # (~50s) and outlives the harness watchdog although nothing is wrong.
    monkeypatch.setattr(guard_module, "_LEASE_VIEW_RETRY_DEADLINE_S", 0.5)
    monkeypatch.setattr(guard_module, "_RECONCILE_RETRY_DEADLINE_S", 30.0)

    def busy(domain):
        raise TransientContention(message="coordinator is busy",
                                  phase="scheduler")

    monkeypatch.setattr(guard_module.scheduler, "reconcile", busy)
    started = time.monotonic()
    with pytest.raises(TransientContention):
        guard_module._running_lease(object(), "r1")
    assert time.monotonic() - started < 10


def test_truncated_scan_fails_closed_without_waiting(monkeypatch):
    import ptest.guard as guard_module

    calls = []

    def truncated():
        calls.append(1)
        return iter([psutil.Process(1), psutil.Process(1)])

    monkeypatch.setattr(guard_module, "_MAX_GROUP_SCAN", 1)
    monkeypatch.setattr(guard_module.psutil, "process_iter", truncated)

    # The count bound is identical on every pass: retrying cannot complete
    # it, so the verdict must come from the first pass, not the deadline.
    started = time.monotonic()
    assert guard_module._group_needs_cleanup(_quiescent_identity()) is True
    assert time.monotonic() - started < 5
    assert len(calls) == 1


def test_chronically_slow_scan_still_fails_closed(monkeypatch):
    import ptest.guard as guard_module

    monkeypatch.setattr(guard_module, "_GROUP_SCAN_DEADLINE_S", 0.4)
    calls = []

    def sluggish():
        calls.append(1)
        time.sleep(0.3)
        return iter([psutil.Process(1)])

    monkeypatch.setattr(guard_module.psutil, "process_iter", sluggish)

    started = time.monotonic()
    assert guard_module._group_needs_cleanup(_quiescent_identity()) is True
    assert time.monotonic() - started < 10
    assert len(calls) > 1


def _quiescent_manifest(run_id="r1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        domain=SimpleNamespace(root="/nonexistent"),
        grant=SimpleNamespace(run_id=run_id))


def test_predecessor_quiescent_retries_transient_reconcile(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    from ptest.storage import TransientContention

    scans, reads = [], []

    def clean(identity):
        scans.append(1)
        return False

    lease = SimpleNamespace(
        run_id="r1", state=C.LeaseState.RUNNING)

    def flaky(domain):
        reads.append(1)
        if len(reads) <= 2:
            raise TransientContention(message="coordinator read failed",
                                      phase="scheduler")
        return (lease,)

    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda identity, deadline_s=None: clean(identity))
    monkeypatch.setattr(guard_module.scheduler, "reconcile", flaky)

    state = guard_module._State()
    assert guard_module._predecessor_quiescent(
        _quiescent_manifest(), _quiescent_identity(), state) is True
    assert state.problem is None
    assert len(reads) == 3


def test_predecessor_quiescent_tolerates_transient_uncertain_view(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda identity, deadline_s=None: False)
    running = SimpleNamespace(run_id="r1", state=C.LeaseState.RUNNING)
    uncertain = SimpleNamespace(run_id="r1", state=C.LeaseState.UNCERTAIN)
    reads = []

    def flickering(domain):
        reads.append(1)
        # An exiting descendant caught mid-reap reads as escaped for one
        # pass; the next pass sees the reaped truth. Only a stable verdict
        # may refuse the spawn.
        return (uncertain,) if len(reads) == 1 else (running,)

    monkeypatch.setattr(guard_module.scheduler, "reconcile", flickering)

    state = guard_module._State()
    assert guard_module._predecessor_quiescent(
        _quiescent_manifest(), _quiescent_identity(), state) is True
    assert state.problem is None
    assert len(reads) == 2


def test_predecessor_quiescent_fails_closed_on_stable_uncertain_view(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda identity, deadline_s=None: False)
    monkeypatch.setattr(guard_module, "_LEASE_VIEW_RETRY_DEADLINE_S", 0.3)
    uncertain = SimpleNamespace(run_id="r1", state=C.LeaseState.UNCERTAIN)
    reads = []

    def stuck(domain):
        reads.append(1)
        return (uncertain,)

    monkeypatch.setattr(guard_module.scheduler, "reconcile", stuck)

    state = guard_module._State()
    assert guard_module._predecessor_quiescent(
        _quiescent_manifest(), _quiescent_identity(), state) is False
    assert state.problem is not None
    assert state.problem.code == "ownership-uncertain"
    assert len(reads) > 1


def test_predecessor_quiescent_stays_fast_while_cancelling(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    uncertain = SimpleNamespace(run_id="r1", state=C.LeaseState.UNCERTAIN)
    reads = []

    def stuck(domain):
        reads.append(1)
        return (uncertain,)

    monkeypatch.setattr(guard_module.scheduler, "reconcile", stuck)

    state = guard_module._State()
    state.cancel(signal.SIGTERM)
    started = time.monotonic()
    assert guard_module._predecessor_quiescent(
        _quiescent_manifest(), _quiescent_identity(), state) is False
    # A pending cancel owns the timeline: the kill path keeps its budget
    # instead of burning the patient spawn-gate budgets.
    assert time.monotonic() - started < 5
    assert state.problem is not None
    assert len(reads) >= 1


def test_predecessor_quiescent_tolerates_slow_reap_view_while_cancelling(
        monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda identity, deadline_s=None: False)
    running = SimpleNamespace(run_id="r1", state=C.LeaseState.RUNNING)
    uncertain = SimpleNamespace(run_id="r1", state=C.LeaseState.UNCERTAIN)
    started = time.monotonic()

    def settling(domain):
        # Under load, descendants exiting from the cancel signal can read as
        # indeterminate for longer than one pass. A healthy cancelled run
        # must not turn into an incomplete (exit 70) result.
        return ((uncertain,) if time.monotonic() - started < 0.8
                else (running,))

    monkeypatch.setattr(guard_module.scheduler, "reconcile", settling)

    state = guard_module._State()
    state.cancel(signal.SIGINT)
    assert guard_module._predecessor_quiescent(
        _quiescent_manifest(), _quiescent_identity(), state) is True
    assert state.problem is None


@pytest.mark.parametrize("code", ["ownership-uncertain", "coordinator-unavailable"])
def test_predecessor_quiescent_does_not_retry_genuine_failure(
        monkeypatch, code):
    import ptest.guard as guard_module

    def clean(identity):
        return False

    reads = []

    def failing(domain):
        reads.append(1)
        # A plain coordinator-unavailable Problem carries genuine failures
        # too (disk errors, failed commits): only cause-verified contention
        # authorizes a retry, so both cases must propagate on the first pass.
        raise C.Problem(code=code,
                        message="process-group ownership could not be proven",
                        phase="scheduler", retryable=(code != "ownership-uncertain"))

    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda identity, deadline_s=None: clean(identity))
    monkeypatch.setattr(guard_module.scheduler, "reconcile", failing)

    state = guard_module._State()
    with pytest.raises(C.Problem) as caught:
        guard_module._predecessor_quiescent(
            _quiescent_manifest(), _quiescent_identity(), state)
    assert caught.value.code == code
    assert len(reads) == 1


# --- Post-test stall detection and SIGWINCH dump signal -----------------------
#
# Real guard plus real workload processes. The workload simulates the pytest
# bridge: it registers a faulthandler dump file, creates the ".done" arm
# marker, then blocks (idle), spins (busy), or forks a same-group child.
# The stall window travels via GUARD_STALL_TIMEOUT because the
# shared-contracts barrier has not landed: the manifest cannot carry
# stall_timeout_s through encode/decode yet (see guard_driver.py).

_STALL_WORKLOAD = _FIXTURES / "stall_workload.py"


def _stall_harness(harness, mode, *, attempt_timeout_s=10, report=True,
                   later=True):
    h = harness()
    h.ready = h.listener("s")
    ready_path = str(h.domain.root / (h.root.name + "s"))
    reports = h.root / "reports"
    reports.mkdir(exist_ok=True)
    h.report_path = reports / ("native-a001-" + "f" * 32 + ".json")
    argv = (sys.executable, str(_STALL_WORKLOAD), mode, ready_path,
            str(h.report_path))
    if report:
        prepared = C.PreparedRun(argv=argv, cwd=h.root,
                                 report_path=h.report_path)
    else:
        prepared = C.PreparedRun(argv=argv, cwd=h.root)
    attempts = (prepared, h.write(h.later)) if later else (prepared,)
    ids = ("a001", "a002") if later else ("a001",)
    h.manifest = replace(h.manifest, attempts=attempts, attempt_ids=ids,
                         attempt_timeout_s=attempt_timeout_s)
    return h


def _stall_running(h, count=1):
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"
    assert h.read().kind == "phase"
    infos = []
    for _ in range(count):
        peer, _ = h.ready.accept()
        peer.settimeout(_RECOVERY_WATCHDOG_S)
        raw = bytearray()
        while not raw.endswith(b"\n"):
            raw.extend(_exact(peer, 1))
        infos.append((peer, json.loads(raw)))
        h.peers.append(peer)
    return infos


def _stall_facts(h):
    return [f.payload for f in h.frames if f.kind == "runner-facts"]


def _assert_alive(h, info, until):
    while time.monotonic() < until:
        assert h.process.poll() is None, "guard exited early"
        assert psutil.Process(info["pid"]).is_running()
        time.sleep(0.05)


def test_stall_armed_idle_killed_with_dump_before_sigterm(harness):
    # Integration boundary: real guard plus workload startup, 1s stall window.
    h = _stall_harness(harness, "armed-idle")
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    facts = _stall_facts(h)
    assert facts[0]["phase"] == "execution"
    assert facts[0]["problem"]["code"] == "post-test-stall"
    assert facts[0]["problem"]["message"] == (
        "tests finished but runner processes stayed idle for "
        "1s without exiting")
    dump = Path(info["dump"])
    text = dump.read_text()
    assert text.startswith("ptest stack dump: role=controller")
    assert "stall_blocked_teardown" in text
    term = Path(str(h.report_path) + ".term")
    assert term.read_text() == "15"
    assert dump.stat().st_mtime_ns <= term.stat().st_mtime_ns
    assert not h.later.exists()


def test_stall_armed_idle_child_dumps_whole_group(harness):
    # Integration boundary: real guard plus two workload processes.
    h = _stall_harness(harness, "armed-idle-child", later=False)
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    infos = _stall_running(h, count=2)
    by_mode = {info["mode"]: info for _, info in infos}
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert _stall_facts(h)[0]["problem"]["code"] == "post-test-stall"
    parent = by_mode["armed-idle-child"]
    child = by_mode["stall-child"]
    parent_dump = Path(str(h.report_path) + f".stack-{parent['pid']}")
    child_dump = Path(str(h.report_path) + f".stack-{child['pid']}")
    parent_text = parent_dump.read_text()
    child_text = child_dump.read_text()
    assert "role=controller" in parent_text.splitlines()[0]
    assert "role=worker" in child_text.splitlines()[0]
    assert "stall_blocked_teardown" in parent_text
    assert "stall_blocked_teardown" in child_text


def test_stall_unarmed_idle_survives_window_and_exits_zero(harness):
    # Integration boundary: must outlive the 1s stall window plus a margin.
    h = _stall_harness(harness, "unarmed-idle", later=False)
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    _assert_alive(h, info, time.monotonic() + 2.5)
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    facts = _stall_facts(h)
    assert facts[0]["raw_exit_code"] == 0
    assert facts[0]["problem"] is None
    assert Path(str(h.report_path) + ".term").read_text() == "released"


def test_stall_armed_busy_survives_window(harness):
    # Integration boundary: a spinning teardown must outlive the window.
    # The design mandates the 1.0s window here, like every other
    # real-process stall test; the spinner polls the release socket
    # without blocking, so it reads as busy even on a contended core.
    h = _stall_harness(harness, "armed-busy", later=False)
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    assert Path(str(h.report_path) + ".done").exists()
    _assert_alive(h, info, time.monotonic() + 2.5)
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    assert _stall_facts(h)[0]["problem"] is None


@pytest.mark.parametrize("shape", ["symlink", "fifo", "dir"])
def test_stall_foreign_marker_never_arms(harness, shape):
    # Integration boundary: a non-regular marker must outlive the window.
    h = _stall_harness(harness, "unarmed-idle", later=False)
    marker = Path(str(h.report_path) + ".done")
    if shape == "symlink":
        target = h.root / "target"
        target.write_text("x")
        os.symlink(target, marker)
    elif shape == "fifo":
        os.mkfifo(marker)
    else:
        marker.mkdir()
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    _assert_alive(h, info, time.monotonic() + 2.0)
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    assert _stall_facts(h)[0]["problem"] is None


def test_stall_symlink_marker_not_written_through(harness):
    # Integration boundary: the bridge-style create must fail on a symlink.
    h = _stall_harness(harness, "armed-idle", later=False)
    target = h.root / "target"
    target.write_text("x")
    os.symlink(target, Path(str(h.report_path) + ".done"))
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    _assert_alive(h, info, time.monotonic() + 2.0)
    assert target.read_text() == "x"
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    assert _stall_facts(h)[0]["problem"] is None


def test_stall_other_report_marker_never_arms(harness):
    # Integration boundary: a stale marker for another report must not arm.
    h = _stall_harness(harness, "unarmed-idle", later=False)
    other = h.report_path.parent / ("native-a001-" + "e" * 32 + ".json")
    Path(str(other) + ".done").write_text("")
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    _assert_alive(h, info, time.monotonic() + 2.0)
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    assert _stall_facts(h)[0]["problem"] is None


def test_stall_first_attempt_marker_does_not_arm_second(harness):
    # Integration boundary: a001 arms its own marker path and exits
    # cleanly, then an idle a002 with no marker of its own must survive
    # the window. A watcher or arm state carried across attempts would see
    # a001's stale marker and kill a002.
    h = harness()
    h.ready = h.listener("s")
    ready_path = str(h.domain.root / (h.root.name + "s"))
    reports = h.root / "reports"
    reports.mkdir(exist_ok=True)
    report_first = reports / ("native-a001-" + "f" * 32 + ".json")
    report_second = reports / ("native-a002-" + "f" * 32 + ".json")
    first = C.PreparedRun(
        argv=(sys.executable, str(_STALL_WORKLOAD), "armed-idle",
              ready_path, str(report_first)),
        cwd=h.root, report_path=report_first)
    second = C.PreparedRun(
        argv=(sys.executable, str(_STALL_WORKLOAD), "unarmed-idle",
              ready_path, str(report_second)),
        cwd=h.root, report_path=report_second)
    h.manifest = replace(h.manifest, attempts=(first, second),
                         attempt_ids=("a001", "a002"),
                         attempt_timeout_s=10)
    h.start(stall_poll=0.2, dump_wait=0.3, stall_timeout=1.0)
    assert h.read().kind == "registered"
    assert h.read().kind == "attempt-ready"
    assert h.read().kind == "phase"

    def _accept():
        peer, _ = h.ready.accept()
        peer.settimeout(_RECOVERY_WATCHDOG_S)
        raw = bytearray()
        while not raw.endswith(b"\n"):
            raw.extend(_exact(peer, 1))
        h.peers.append(peer)
        return peer, json.loads(raw)

    peer_first, _ = _accept()
    peer_first.sendall(b"x")
    while True:
        frame = h.read()
        if frame.kind == "phase":
            break
    peer, info = _accept()
    assert Path(str(report_first) + ".done").exists()
    assert not Path(str(report_second) + ".done").exists()
    _assert_alive(h, info, time.monotonic() + 2.0)
    peer.sendall(b"x")
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 0
    facts = _stall_facts(h)
    assert len(facts) == 2
    assert [frame["attempt_id"] for frame in facts] == ["a001", "a002"]
    assert [frame["raw_exit_code"] for frame in facts] == [0, 0]
    assert [frame["problem"] for frame in facts] == [None, None]
    assert Path(str(report_second) + ".term").read_text() == "released"


def test_deadline_kill_sends_sigwinch_first_without_stall(harness):
    # Integration boundary: attempt deadline with no stall configured.
    h = _stall_harness(harness, "armed-idle", later=False,
                       attempt_timeout_s=2)
    h.start(stall_poll=0.2, dump_wait=0.3)
    [(peer, info)] = _stall_running(h)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    facts = _stall_facts(h)
    assert facts[0]["problem"]["code"] == "execution-timeout"
    dump = Path(info["dump"])
    assert "stall_blocked_teardown" in dump.read_text()
    term = Path(str(h.report_path) + ".term")
    assert term.read_text() == "15"
    assert dump.stat().st_mtime_ns <= term.stat().st_mtime_ns


def test_deadline_kill_without_report_path_sends_no_sigwinch(harness):
    # Integration boundary: no report binding means no dump signal or wait.
    h = _stall_harness(harness, "armed-idle", later=False, report=False,
                       attempt_timeout_s=2)
    started = time.monotonic()
    h.start(stall_poll=0.2, dump_wait=5, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert _stall_facts(h)[0]["problem"]["code"] == "execution-timeout"
    dump = Path(info["dump"])
    assert dump.read_text().startswith("ptest stack dump: ")
    assert len(dump.read_text().splitlines()) == 1
    # A 5s dump wait must leave no trace when there is no report binding.
    assert time.monotonic() - started < 6.0


def test_external_cancel_sends_no_sigwinch_and_no_delay(harness):
    # Integration boundary: a control-frame cancel must skip the dump path.
    h = _stall_harness(harness, "unarmed-idle", later=False)
    h.start(stall_poll=0.2, dump_wait=5, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    started = time.monotonic()
    h.control.sendall(_cancel(h.manifest))
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert time.monotonic() - started < 4.0
    dump = Path(info["dump"])
    assert len(dump.read_text().splitlines()) == 1


def test_sigint_cancel_sends_no_sigwinch(harness):
    # Integration boundary: SIGINT to the guard must skip the dump path.
    h = _stall_harness(harness, "unarmed-idle", later=False)
    h.start(stall_poll=0.2, dump_wait=5, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    started = time.monotonic()
    os.kill(h.process.pid, signal.SIGINT)
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 128 + signal.SIGINT
    assert time.monotonic() - started < 4.0
    dump = Path(info["dump"])
    assert len(dump.read_text().splitlines()) == 1


def test_cancel_during_dump_wait_ends_wait_promptly(harness):
    # Integration boundary: cancel interrupts a 5s dump wait mid-flight.
    h = _stall_harness(harness, "armed-idle", later=False)
    h.start(stall_poll=0.2, dump_wait=5, stall_timeout=1.0)
    [(peer, info)] = _stall_running(h)
    dump = Path(info["dump"])
    header_len = len(dump.read_text())
    deadline = time.monotonic() + _RECOVERY_WATCHDOG_S
    while len(dump.read_text()) <= header_len:
        assert time.monotonic() < deadline, "SIGWINCH dump never landed"
        time.sleep(0.02)
    started = time.monotonic()
    h.control.sendall(_cancel(h.manifest))
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert time.monotonic() - started < 4.0


def test_deadline_wins_same_poll_race(monkeypatch):
    # Unit boundary: the attempt deadline AND the stall verdict are both
    # due in the same _run_one pass, so the poll-loop order decides. The
    # deadline fail() runs first and the stall check is skipped once
    # cancel_signal is set: exactly one problem, one dump signal, one kill.
    # (An integration timing test cannot force both due in one pass, so a
    # 1s-deadline/5s-window pairing would prove nothing about precedence.)
    import ptest.guard as guard_module
    from types import SimpleNamespace

    now = [1000.0]
    monkeypatch.setattr(guard_module, "time",
                        SimpleNamespace(monotonic=lambda: now[0]))

    class _Child:
        calls = 0

        def poll(self):
            # Alive for the verdict pass, reaped by the kill path after.
            type(self).calls += 1
            return None if type(self).calls == 1 else 0

        def wait(self):
            return 0

    monkeypatch.setattr(guard_module.subprocess, "Popen",
                        lambda *args, **kwargs: _Child())

    watcher = SimpleNamespace(stall_s=5.0, checks=0)

    def _due():
        watcher.checks += 1
        return True  # the stall verdict is due in this same pass

    watcher.check = _due
    monkeypatch.setattr(guard_module, "_stall_watcher_for",
                        lambda *args: watcher)

    sent = []
    monkeypatch.setattr(guard_module, "_signal_group",
                        lambda identity, signum: sent.append(signum))
    monkeypatch.setattr(guard_module, "_group_needs_cleanup",
                        lambda *args, **kwargs: False)

    class _Control:
        manifest = SimpleNamespace()
        pending = bytearray()

        def poll(self, timeout=0):
            now[0] += 0.05

        def emit(self, kind, payload):
            emitted.append((kind, payload))

    emitted = []
    state = guard_module._State()
    fail_calls = []
    original_fail = state.fail

    def _counting_fail(problem, *, dump=False):
        fail_calls.append((problem.code, dump))
        return original_fail(problem, dump=dump)

    state.fail = _counting_fail
    prepared = SimpleNamespace(report_path=Path("report.json"),
                               argv=("runner",), cwd=".", env_updates={})
    result = guard_module._run_one(
        _Control(), SimpleNamespace(), prepared, "a001", "execution",
        0, now[0] + 100, SimpleNamespace(), state)

    # The stall check is skipped once the deadline owns the timeline.
    assert watcher.checks == 0
    assert state.problem is not None
    assert state.problem.code == "execution-timeout"
    # Exactly one fail call: dropping the cancel guard on the stall check
    # would record a second (losing) problem here.
    assert fail_calls == [("execution-timeout", True)]
    # Exactly one dump signal and one kill signal, in that order.
    assert sent == [signal.SIGWINCH, signal.SIGTERM]
    assert result == 0
    facts = [payload for kind, payload in emitted if kind == "runner-facts"]
    assert len(facts) == 1
    assert facts[0]["problem"]["code"] == "execution-timeout"


def test_signal_group_sigwinch_forged_identity_raises(monkeypatch):
    import ptest.guard as guard_module
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(os, "killpg",
                        lambda pgid, sig: calls.append((pgid, sig)))
    forged = SimpleNamespace(pid=123456789, pgid=123456789)
    with pytest.raises(C.Problem) as caught:
        guard_module._signal_group(forged, signal.SIGWINCH)
    assert caught.value.code == "ownership-uncertain"
    assert calls == []


def test_fail_records_first_problem_and_dump_request():
    import ptest.guard as guard_module

    state = guard_module._State()
    assert state.dump_requested is False
    assert state.external_cancel is False
    state.fail(guard_module._problem("execution-timeout", "x"), dump=True)
    assert state.problem.code == "execution-timeout"
    assert state.dump_requested is True
    assert state.cancel_signal == signal.SIGTERM
    assert state.external_cancel is False
    state.fail(guard_module._problem("post-test-stall", "y"), dump=True)
    assert state.problem.code == "execution-timeout"
    assert state.dump_requested is True


def test_fail_after_external_cancel_arms_no_dump():
    import ptest.guard as guard_module

    state = guard_module._State()
    state.cancel(signal.SIGINT)
    assert state.external_cancel is True
    state.fail(guard_module._problem("execution-timeout", "x"), dump=True)
    assert state.problem.code == "execution-timeout"
    assert state.dump_requested is False

