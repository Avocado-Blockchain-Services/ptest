"""Ambient fingerprint command runner (R41-R43, R48).

Runs the project's declared ``[selection] ambient_fingerprint`` argv once and
reduces its stdout to a keyed digest. The command is untrusted input: literal
argv with no shell, stdin ``/dev/null``, its own session so a timeout or a
cancellation can SIGKILL the whole group, and bounded stdout/stderr. Stdout is
hashed as it arrives and never kept, printed or stored. Every failure is an
absent digest with a short reason; ``compute`` never raises, so a broken
command can only make the caller treat the fingerprint as changed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import contracts as C

_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class Outcome:
    digest: str | None
    reason: str | None
    stderr_tail: tuple[str, ...] = ()


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _exited(proc: subprocess.Popen) -> bool:
    """True once the leader has exited, without reaping it.

    Leaving the zombie in place keeps its process-group id reserved, so the
    final SIGKILL of the group can never hit a recycled id.
    """
    if hasattr(os, "waitid"):
        try:
            return os.waitid(
                os.P_PID, proc.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _tail_lines(data: bytes) -> tuple[str, ...]:
    text = data.decode("utf-8", errors="replace")
    lines = [line.rstrip("\r") for line in text.split("\n")]
    lines = [line for line in lines if line.strip()]
    return tuple(lines[-C.AMBIENT_FINGERPRINT_STDERR_LINES:])


def _failure(reason: str, stderr: bytearray = bytearray()) -> Outcome:
    return Outcome(None, reason, _tail_lines(bytes(stderr)))


def compute(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
            key: bytes, timeout_s: float = C.AMBIENT_FINGERPRINT_TIMEOUT_S,
            cancelled: Callable[[], bool] = lambda: False,
            poll_s: float = 0.05) -> Outcome:
    """Run ``argv`` once and return its keyed stdout digest; never raises."""
    try:
        return _compute(argv, cwd, env, key, timeout_s, cancelled, poll_s)
    except Exception:  # noqa: BLE001 - contract: failures are data, never fatal
        return _failure("could not start")


def _compute(argv, cwd, env, key, timeout_s, cancelled, poll_s) -> Outcome:
    if not argv:
        return _failure("could not start")
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), env=dict(env), shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    except FileNotFoundError:
        return _failure("not found")
    except (OSError, ValueError):
        return _failure("could not start")
    pgid = proc.pid
    stdout_cap = C.AMBIENT_FINGERPRINT_STDOUT_MAX_BYTES
    stderr_cap = C.AMBIENT_FINGERPRINT_STDERR_MAX_BYTES
    mac = hmac.new(key, digestmod=hashlib.sha256)
    stdout_size = 0
    stderr = bytearray()
    reason: str | None = None
    selector = selectors.DefaultSelector()
    try:
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        def pump(wait_s: float) -> bool:
            """Drain ready pipe data; True when stdout went over its cap."""
            nonlocal stdout_size
            for event, _ in selector.select(wait_s):
                stream = event.fileobj
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    continue
                if stream is proc.stdout:
                    stdout_size += len(chunk)
                    if stdout_size > stdout_cap:
                        return True
                    mac.update(chunk)
                else:
                    stderr.extend(chunk)
                    if len(stderr) > stderr_cap:
                        del stderr[:len(stderr) - stderr_cap]
            return False

        deadline = time.monotonic() + timeout_s
        while reason is None:
            if cancelled():
                reason = "cancelled"
            elif pump(poll_s):
                reason = "output over 16 MiB"
            elif time.monotonic() >= deadline:
                reason = f"timed out after {timeout_s:g} s"
            elif _exited(proc):
                # Leader gone: take what is already buffered, then stop.
                while selector.get_map() and selector.select(0):
                    if pump(0):
                        reason = "output over 16 MiB"
                        break
                break
        # Nothing of the command's session may outlive compute().
        _kill_group(pgid)
        returncode = proc.wait()
        if reason is None and returncode != 0:
            reason = f"exit {returncode}"
        if reason is not None:
            return _failure(reason, stderr)
        return Outcome(mac.hexdigest(), None, ())
    finally:
        _kill_group(pgid)
        selector.close()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
