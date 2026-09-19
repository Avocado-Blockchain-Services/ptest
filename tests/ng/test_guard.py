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
              advance=0, decision_timeout=None):
        env = dict(os.environ, GUARD_CONTROL_FD=str(self.control_guard.fileno()),
                   GUARD_MANIFEST_FD=str(self.manifest_read),
                   GUARD_FAILURE=failure,
                   GUARD_ADVANCE_AFTER_FACTS=str(advance),
                   GUARD_DECISION_TIMEOUT=("" if decision_timeout is None
                                           else str(decision_timeout)),
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
    h.manifest = replace(h.manifest, attempt_timeout_s=0.5)
    h.start()
    h.running()
    h.control.sendall(b"\x00\x00")
    start = time.monotonic()
    assert h.finish(timeout=_CANCEL_WATCHDOG_S)[0] == 143
    assert time.monotonic() - start < 1.5
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
    h.manifest = replace(h.manifest, attempt_timeout_s=0.5 if scope == "attempt" else None,
                         compound_timeout_s=0.5 if scope == "compound" else 20)
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
    assert queued.owner.wait(timeout=_CANCEL_WATCHDOG_S) == 128 + signum
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


def test_compound_deadline_spans_setup_and_attempts_with_truthful_unstarted_fact(harness):
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
    assert len(facts) == 3
    assert facts[-1]["attempt_id"] == "a002"
    assert facts[-1]["raw_exit_code"] is None
    assert facts[-1]["problem"]["code"] == "execution-timeout"
    assert "compound" in facts[-1]["problem"]["message"]
    assert h.frames[-1].kind == "draining"


def test_manifest_trailing_bytes_cannot_authorize_repository_work(harness):
    h = harness()
    h.start(raw=C.encode_launch_manifest(h.manifest) + b"untrusted trailing bytes")
    assert h.finish()[0] == 70
    assert not h.marker.exists()
    assert h.row()["guard_pid"] is None
