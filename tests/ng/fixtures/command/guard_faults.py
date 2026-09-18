"""Fault injection at real guard boundaries; never imported by production."""
import os
import select
import signal
import sys
from dataclasses import replace

from ptest import contracts as C, guard


mode = os.environ["TEST_GUARD_FAULT"]
original_emit = guard._Control.emit


def emit(self, kind, payload):
    if mode in {"wrong-nonce", "wrong-run"} and kind == "draining":
        frame = C.ControlFrame(protocol=1, run_id=("c" * 32 if mode == "wrong-run" else self.manifest.grant.run_id),
                               nonce=("c" * 64 if mode == "wrong-nonce" else self.manifest.grant.nonce),
                               kind=kind, payload=payload)
        os.write(self.fd, C.encode_control_frame(frame))
        return
    if kind == "runner-facts":
        if mode.startswith("problem:"):
            _, code, raw = mode.split(":")
            payload = dict(payload, raw_exit_code=None if raw == "none" else int(raw),
                           problem={"code": code, "message": "injected guard failure",
                                    "phase": "execution", "retryable": False})
        elif mode == "wrong-attempt":
            payload = dict(payload, attempt_id="a002")
        elif mode == "missing-raw":
            payload = dict(payload, raw_exit_code=None)
    if kind == "draining":
        if mode == "missing-draining":
            return
        if mode == "killed-before-draining":
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "bad-draining":
            payload = {"provisional_artifact_id": "unexpected.json"}
        if mode == "truncated-draining":
            os.write(self.fd, b"\x00\x00\x00\x05{")
            return
    original_emit(self, kind, payload)
    if mode == "early-draining" and kind == "registered":
        original_emit(self, "draining", {"provisional_artifact_id": None})
    if mode == "duplicate-facts" and kind == "runner-facts":
        original_emit(self, kind, payload)


guard._Control.emit = emit
if mode == "bounded-attempt":
    original_run_one = guard._run_one

    def run_one(control, prepared, attempt_id, phase, timeout_s, *args):
        # Compress only a supplied attempt timeout; None remains unbounded by
        # the probe budget. The real guard still enforces the compound limit.
        return original_run_one(control, prepared, attempt_id, phase,
                                0 if timeout_s is not None else None, *args)

    guard._run_one = run_one
elif mode == "startup-cancel":
    original_read = guard._read_manifest

    def read_manifest(fd, state):
        manifest = original_read(fd, state)
        assert select.select([int(sys.argv[1])], [], [], 5)[0]
        return manifest

    guard._read_manifest = read_manifest

raise SystemExit(guard.run_guard(int(sys.argv[1]), int(sys.argv[2])))
