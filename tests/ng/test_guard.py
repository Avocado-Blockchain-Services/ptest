"""Lifecycle contracts for the local admitted process-group guard."""
from __future__ import annotations

import importlib
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from ptest import contracts as C


_RECOVERY_WATCHDOG_S = 45
_FIXTURES = Path(__file__).parent / "fixtures" / "processes"


def _manifest(case, marker: Path) -> C.LaunchManifest:
    domain = case.domain()
    grant = C.Grant(run_id="a" * 32, nonce="b" * 64, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=0, domain_id=domain.domain_id)
    prepared = C.PreparedRun(
        argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"),
        cwd=domain.root)
    return C.LaunchManifest(protocol=1, domain=domain, grant=grant, setup=None,
                            attempts=(prepared,), attempt_ids=("a001",),
                            setup_timeout_s=1.0, attempt_timeout_s=1.0,
                            compound_timeout_s=1.0)


def test_unregistered_guard_never_executes(case, monkeypatch, tmp_path):
    """A lost registration race must leave repository work untouched."""
    guard = importlib.import_module("ptest.guard")
    marker = tmp_path / "must-not-exist"
    manifest = _manifest(case, marker)
    monkeypatch.setattr(guard.scheduler, "register_guard", lambda *_: False)
    control_guard, control_parent = socket.socketpair()
    manifest_read, manifest_write = os.pipe()
    try:
        os.write(manifest_write, C.encode_launch_manifest(manifest))
        os.close(manifest_write)
        manifest_write = -1
        code = guard.run_guard(control_guard.detach(), manifest_read)
    finally:
        if manifest_write >= 0:
            os.close(manifest_write)
        control_parent.close()
    assert code != 0
    assert marker.exists() is False


def _frames(sock: socket.socket, manifest: C.LaunchManifest) -> list[C.ControlFrame]:
    raw = bytearray()
    frames = []
    sock.settimeout(1)
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return frames
            raw.extend(chunk)
            while len(raw) >= 4 and len(raw) >= 4 + struct.unpack(">I", raw[:4])[0]:
                length = struct.unpack(">I", raw[:4])[0]
                frames.append(C.decode_control_frame(raw[:4 + length],
                                                     expected_nonce=manifest.grant.nonce))
                del raw[:4 + length]
    except (TimeoutError, OSError):
        return frames


def _registered_identity(monkeypatch, guard):
    identity = C.ProcessIdentity(pid=os.getpid(), birth=1.0, uid=os.getuid(), pgid=os.getpid())
    monkeypatch.setattr(guard.os, "setsid", lambda: None)
    monkeypatch.setattr(guard.platform, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(guard.scheduler, "register_guard", lambda *_: True)
    return identity


def _invoke(guard, manifest: C.LaunchManifest, *, control_parent: socket.socket | None = None):
    control_guard, local_parent = socket.socketpair()
    parent = local_parent if control_parent is None else control_parent
    manifest_read, manifest_write = os.pipe()
    try:
        os.write(manifest_write, C.encode_launch_manifest(manifest))
        os.close(manifest_write)
        manifest_write = -1
        code = guard.run_guard(control_guard.detach(), manifest_read)
        return code, _frames(parent, manifest)
    finally:
        if manifest_write >= 0:
            os.close(manifest_write)
        if control_parent is None:
            local_parent.close()


def test_malformed_private_manifest_never_registers_or_executes(monkeypatch, tmp_path):
    guard = importlib.import_module("ptest.guard")
    registered = []
    monkeypatch.setattr(guard.scheduler, "register_guard", lambda *_: registered.append(True))
    control_guard, control_parent = socket.socketpair()
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x00\x00\x00\x02{")
        os.close(write_fd)
        write_fd = -1
        assert guard.run_guard(control_guard.detach(), read_fd) == 70
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        control_parent.close()
    assert registered == []
    assert list(tmp_path.iterdir()) == []


def test_false_draining_handoff_never_emits_success_fact(case, monkeypatch, tmp_path):
    guard = importlib.import_module("ptest.guard")
    marker = tmp_path / "ran"
    manifest = _manifest(case, marker)
    _registered_identity(monkeypatch, guard)
    monkeypatch.setattr(guard.scheduler, "mark_draining", lambda *_: False)
    code, frames = _invoke(guard, manifest)
    assert code != 0
    assert marker.read_text() == "ran"
    assert "draining" not in [frame.kind for frame in frames]


def test_true_draining_handoff_is_the_only_success_like_fact(case, monkeypatch, tmp_path):
    guard = importlib.import_module("ptest.guard")
    marker = tmp_path / "ran"
    manifest = _manifest(case, marker)
    _registered_identity(monkeypatch, guard)
    monkeypatch.setattr(guard.scheduler, "mark_draining", lambda *_: True)
    code, frames = _invoke(guard, manifest)
    assert code == 0
    assert marker.read_text() == "ran"
    assert [frame.kind for frame in frames][-1] == "draining"
    assert frames[-1].payload == {"provisional_artifact_id": None}


def test_parent_eof_before_execution_forbids_first_attempt(case, monkeypatch, tmp_path):
    guard = importlib.import_module("ptest.guard")
    marker = tmp_path / "must-not-exist"
    manifest = _manifest(case, marker)
    _registered_identity(monkeypatch, guard)
    monkeypatch.setattr(guard.scheduler, "mark_draining", lambda *_: True)
    control_guard, control_parent = socket.socketpair()
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, C.encode_launch_manifest(manifest))
        os.close(write_fd)
        write_fd = -1
        control_parent.close()
        assert guard.run_guard(control_guard.detach(), read_fd) != 0
    finally:
        if write_fd >= 0:
            os.close(write_fd)
    assert marker.exists() is False


def _read_frame(sock: socket.socket, manifest: C.LaunchManifest) -> C.ControlFrame:
    sock.settimeout(_RECOVERY_WATCHDOG_S)
    prefix = bytearray()
    while len(prefix) < 4:
        prefix.extend(sock.recv(4 - len(prefix)))
    length = struct.unpack(">I", prefix)[0]
    body = bytearray()
    while len(body) < length:
        body.extend(sock.recv(length - len(body)))
    return C.decode_control_frame(bytes(prefix + body), expected_nonce=manifest.grant.nonce)


def _isolated_guard(manifest: C.LaunchManifest, *, drain: bool = True):
    guard_sock, parent_sock = socket.socketpair()
    manifest_read, manifest_write = os.pipe()
    env = dict(os.environ, GUARD_CONTROL_FD=str(guard_sock.fileno()),
               GUARD_MANIFEST_FD=str(manifest_read), GUARD_DRAIN="1" if drain else "0")
    child = subprocess.Popen(
        (sys.executable, str(_FIXTURES / "guard_driver.py")), cwd=Path.cwd(), env=env,
        pass_fds=(guard_sock.fileno(), manifest_read), close_fds=True,
    )
    guard_sock.close()
    os.close(manifest_read)
    os.write(manifest_write, C.encode_launch_manifest(manifest))
    os.close(manifest_write)
    return child, parent_sock


@pytest.mark.parametrize("cancel_signal", [2, 15])
def test_active_cancel_signals_only_owned_guard_group_and_never_drains(
        case, tmp_path, cancel_signal):
    """The isolated guard's real session receives repeated controller cancellation."""
    marker = tmp_path / "child-state"
    manifest = _manifest(case, marker)
    ready_path = tmp_path / "ready.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(ready_path))
    listener.listen(1)
    listener.settimeout(_RECOVERY_WATCHDOG_S)
    prepared = C.PreparedRun(argv=(sys.executable, str(_FIXTURES / "wait_for_signal.py"), str(marker), str(ready_path)),
                             cwd=case.domain().root)
    manifest = C.LaunchManifest(protocol=1, domain=manifest.domain, grant=manifest.grant,
                                setup=None, attempts=(prepared,), attempt_ids=("a001",),
                                setup_timeout_s=1, attempt_timeout_s=30, compound_timeout_s=30)
    process, control = _isolated_guard(manifest)
    try:
        assert _read_frame(control, manifest).kind == "registered"
        assert _read_frame(control, manifest).kind == "phase"
        ready, _ = listener.accept()
        ready.close()
        cancel = C.encode_control_frame(C.ControlFrame(
            protocol=1, run_id=manifest.grant.run_id, nonce=manifest.grant.nonce,
            kind="cancel", payload={"signal": cancel_signal}))
        control.sendall(cancel)
        control.sendall(cancel)
        assert process.wait(timeout=_RECOVERY_WATCHDOG_S) != 0
        assert marker.read_text() == "terminated"
        assert all(frame.kind != "draining" for frame in _frames(control, manifest))
    finally:
        listener.close()
        control.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=_RECOVERY_WATCHDOG_S)
