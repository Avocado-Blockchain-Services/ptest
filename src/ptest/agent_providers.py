"""Owned provider subprocess boundary (task 1A owned).

Single release gate for the ``claude`` / ``codex`` / ``opencode`` reviewer
adapters. Every profile below is a CANDIDATE: adversarial qualification is
a separate required gate, so all adapters resolve as unqualified and
:func:`launch_review` fails closed until each exact profile is proved.
Synthetic tests exercise the machinery with fake executables only and
never qualify a real provider.
"""
from __future__ import annotations

import json
import logging
import math
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .contracts import Problem

log = logging.getLogger(__name__)

SUPPORTED_REVIEWERS = ("claude", "codex", "opencode")

PROMPT_INPUT_MAX_BYTES = 1048576
OUTPUT_MAX_BYTES = 524288
TIMEOUT_MIN_S = 1
TIMEOUT_MAX_S = 900
_TERM_GRACE_S = 2.0
_HEARTBEAT_S = 15.0

# Process-group ownership is verified on POSIX only; on other platforms the
# implementation falls back to direct-child terminate/kill (unverified).
PROCESS_GROUP_SCOPE = "posix-only; non-POSIX group control unverified"

_PHASES = frozenset({"collecting", "reviewing", "validating", "publishing"})

_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TZ", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
})
_CRED_DENY = ("token", "secret", "key", "credential", "password", "auth",
              "session", "cookie")

# Candidate fixed argv tails (executable prepended at resolution). Unproven:
# the qualification gate must freeze these exactly before any real use.
# Deliberately no "--bare" Claude fallback (it disables normal auth).
_CANDIDATE_TAILS = {
    "claude": ("--print", "--output-format", "json", "--input-format",
               "text", "--safe-mode", "--allowedTools", "",
               "--mcp-config", "", "--no-slash-commands",
               "--no-session-persistence"),
    "codex": ("exec", "--sandbox", "read-only", "--skip-git-repo-check",
              "--no-browser", "--no-shell", "--ephemeral"),
    "opencode": ("run", "--pure", "--format", "json",
                 "--agent", "ptest-locked-denied"),
}

_UNPROVEN_NOTE = (
    "candidate profile only; adversarial qualification pending, "
    "real provider use blocked until the exact argv/environment is proved"
)


def _problem(code: str, message: str) -> Problem:
    return Problem(code=code, message=message, phase="provider")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_str(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be nonempty")
    return value


def _check_argv(name: str, value: object) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} must be tuple, got {type(value).__name__}")
    tokens = tuple(value)
    if not tokens:
        raise ValueError(f"{name} must be nonempty")
    total = 0
    for token in tokens:
        _check_str(f"{name}[]", token, allow_empty=True)
        if "\x00" in token:
            raise ValueError(f"{name}[] must not contain NUL")
        total += len(token.encode("utf-8"))
    if total > 131072:
        raise ValueError(f"{name} exceeds 128 KiB total")
    return tokens


@dataclass(frozen=True, slots=True)
class ReviewerAdapter:
    name: str
    executable: str
    argv: tuple = ()
    qualified: bool = False
    qualification_note: str = ""

    def __post_init__(self) -> None:
        _check_str("adapter.name", self.name)
        if self.name not in SUPPORTED_REVIEWERS:
            raise ValueError(f"adapter.name {self.name!r} is not supported")
        _check_str("adapter.executable", self.executable)
        tokens = _check_argv("adapter.argv", self.argv)
        if tokens[0] != self.executable:
            raise ValueError("adapter.argv[0] must be the resolved executable")
        object.__setattr__(self, "argv", tokens)
        if not isinstance(self.qualified, bool):
            raise TypeError("adapter.qualified must be bool")
        _check_str("adapter.qualification_note", self.qualification_note)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: str
    provider: str
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        _check_str("progress.phase", self.phase)
        if self.phase not in _PHASES:
            raise ValueError(f"progress.phase {self.phase!r} is unknown")
        _check_str("progress.provider", self.provider)
        if self.provider not in SUPPORTED_REVIEWERS:
            raise ValueError("progress.provider is not supported")
        if isinstance(self.elapsed_s, bool) or not isinstance(
                self.elapsed_s, (int, float)):
            raise TypeError("progress.elapsed_s must be numeric")
        elapsed = float(self.elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("progress.elapsed_s must be finite and >= 0")
        object.__setattr__(self, "elapsed_s", elapsed)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    ok: bool
    assessment: bytes
    error: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    truncated: bool
    pid: int
    argv: tuple = ()
    scratch: str = ""

    def __post_init__(self) -> None:
        _check_str("result.provider", self.provider)
        if self.provider not in SUPPORTED_REVIEWERS:
            raise ValueError("result.provider is not supported")
        if not isinstance(self.ok, bool):
            raise TypeError("result.ok must be bool")
        if not isinstance(self.assessment, bytes):
            raise TypeError("result.assessment must be bytes")
        _check_str("result.error", self.error, allow_empty=True)
        if self.ok and self.error:
            raise ValueError("successful result must carry empty error")
        if not self.ok and not self.error:
            raise ValueError("failed result must carry an error code")
        if self.exit_code is not None and not _is_int(self.exit_code):
            raise TypeError("result.exit_code must be int or None")
        for field in ("timed_out", "cancelled", "truncated"):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"result.{field} must be bool")
        if not _is_int(self.pid) or self.pid < 1:
            raise ValueError("result.pid must be a positive int")
        object.__setattr__(self, "argv", _check_argv("result.argv", self.argv))
        _check_str("result.scratch", self.scratch)


@dataclass(frozen=True, slots=True)
class QualificationStatus:
    name: str
    qualified: bool
    argv: tuple = ()
    note: str = ""

    def __post_init__(self) -> None:
        _check_str("qualification.name", self.name)
        if self.name not in SUPPORTED_REVIEWERS:
            raise ValueError("qualification.name is not supported")
        if not isinstance(self.qualified, bool):
            raise TypeError("qualification.qualified must be bool")
        object.__setattr__(self, "argv", _check_argv("qualification.argv",
                                                     self.argv))
        _check_str("qualification.note", self.note, allow_empty=True)


def qualification_status(name: str) -> QualificationStatus:
    """Expose the (currently unproven) qualification state of a profile."""
    _check_str("name", name)
    if name not in SUPPORTED_REVIEWERS:
        raise _problem("provider-unavailable", f"unsupported reviewer {name}")
    return QualificationStatus(name=name, qualified=False,
                               argv=(name,) + _CANDIDATE_TAILS[name],
                               note=_UNPROVEN_NOTE)


def _find_executable(name: str, env: Mapping) -> str:
    path = env.get("PATH", "")
    if not isinstance(path, str):
        raise TypeError("env PATH must be str")
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    raise _problem("provider-unavailable", f"reviewer {name} not installed")


def resolve_reviewer(name: str, env: Mapping[str, str]) -> ReviewerAdapter:
    """Resolve a supported reviewer to a fixed, unqualified adapter."""
    _check_str("name", name)
    if not isinstance(env, Mapping):
        raise TypeError("env must be a mapping")
    if name not in SUPPORTED_REVIEWERS:
        raise _problem("provider-unavailable", f"unsupported reviewer {name}")
    executable = _find_executable(name, env)
    return ReviewerAdapter(name=name, executable=executable,
                           argv=(executable,) + _CANDIDATE_TAILS[name],
                           qualified=False,
                           qualification_note=_UNPROVEN_NOTE)


def sanitized_child_env(source: Mapping) -> dict[str, str]:
    """Project a minimal allowlisted child environment.

    Credential-bearing keys never cross, even if allowlisted. Values are
    never logged by this module.
    """
    if not isinstance(source, Mapping):
        raise TypeError("source must be a mapping")
    projected: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        try:
            value = source[key]
        except KeyError:
            continue
        if not isinstance(value, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in _CRED_DENY):
            continue
        projected[key] = value
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in _CRED_DENY):
            projected.pop(key, None)
    return projected


def _result(adapter: ReviewerAdapter, *, ok: bool, assessment: bytes,
            error: str, exit_code: int | None, timed_out: bool,
            cancelled: bool, truncated: bool, pid: int,
            scratch: str) -> ProviderResult:
    return ProviderResult(provider=adapter.name, ok=ok,
                          assessment=assessment, error=error,
                          exit_code=exit_code, timed_out=timed_out,
                          cancelled=cancelled, truncated=truncated, pid=pid,
                          argv=adapter.argv, scratch=scratch)


def _still_owned(pid: int, pgid: int | None) -> bool:
    if pgid is None or os.name != "posix":
        return True
    try:
        return os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _stop_owned(proc: subprocess.Popen, pgid: int | None) -> None:
    if proc.poll() is None and _still_owned(proc.pid, pgid):
        try:
            if pgid is not None and os.name == "posix":
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    deadline = time.monotonic() + _TERM_GRACE_S
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None and _still_owned(proc.pid, pgid):
        try:
            if pgid is not None and os.name == "posix":
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _tool_key_present(mapping: Mapping) -> bool:
    for key in mapping:
        if isinstance(key, str) and "tool" in key.lower():
            return True
    kind = mapping.get("type")
    if isinstance(kind, str) and kind.lower() in ("tool_use", "tool_call",
                                                  "function_call"):
        return True
    return False


def _normalize(name: str, stdout: bytes) -> tuple[bool, bytes, str]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False, b"", "invalid-assessment"
    if name == "claude":
        try:
            envelope = json.loads(text.strip() or "null")
        except (json.JSONDecodeError, ValueError):
            return False, b"", "invalid-assessment"
        if not isinstance(envelope, dict):
            return False, b"", "invalid-assessment"
        if _tool_key_present(envelope):
            return False, b"", "tool-attempt"
        has_result = "result" in envelope
        has_results = "results" in envelope
        candidate = None
        if has_result and not has_results:
            candidate = envelope["result"]
        elif has_results and not has_result:
            items = envelope["results"]
            if not isinstance(items, list) or len(items) != 1:
                return False, b"", "invalid-assessment"
            candidate = items[0]
        else:
            return False, b"", "invalid-assessment"
        if not isinstance(candidate, str) or not candidate:
            return False, b"", "invalid-assessment"
        return True, candidate.encode("utf-8"), ""
    lines = [line for line in text.splitlines() if line.strip()]
    events: list = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            return False, b"", "invalid-assessment"
    if name == "codex":
        for event in events:
            if not isinstance(event, dict) or not isinstance(
                    event.get("type"), str):
                return False, b"", "invalid-assessment"
            kind = event["type"].lower()
            if kind in ("tool_call", "tool_use",
                        "function_call") or kind.startswith("tool"):
                return False, b"", "tool-attempt"
        messages = [e["content"] for e in events
                    if isinstance(e.get("type"), str)
                    and e["type"].lower() == "message" and isinstance(
                        e.get("content"), str)]
        if not messages or not messages[-1]:
            return False, b"", "invalid-assessment"
        return True, messages[-1].encode("utf-8"), ""
    if name == "opencode":
        results = []
        for event in events:
            if not isinstance(event, dict) or not isinstance(
                    event.get("event"), str):
                return False, b"", "invalid-assessment"
            kind = event["event"].lower()
            if kind == "tool" or kind.startswith("tool"):
                return False, b"", "tool-attempt"
            if kind == "result":
                results.append(event.get("data"))
        if len(results) != 1 or not isinstance(results[0], str) \
                or not results[0]:
            return False, b"", "invalid-assessment"
        return True, results[0].encode("utf-8"), ""
    return False, b"", "invalid-assessment"


def launch_review(adapter: ReviewerAdapter, packet: bytes, schema: bytes,
                  timeout_s: int,
                  progress: Callable[[ProgressEvent], None]) -> ProviderResult:
    """Run one owned provider child and normalize exactly one assessment."""
    if not isinstance(adapter, ReviewerAdapter):
        raise TypeError("adapter must be ReviewerAdapter")
    if not isinstance(packet, bytes) or not isinstance(schema, bytes):
        raise TypeError("packet and schema must be bytes")
    if not _is_int(timeout_s):
        raise TypeError("timeout_s must be int")
    if not callable(progress):
        raise TypeError("progress must be callable")
    if not packet or not schema:
        raise _problem("invalid-bound", "packet and schema must be nonempty")
    if len(packet) + len(schema) > PROMPT_INPUT_MAX_BYTES:
        raise _problem("invalid-bound", "prompt input exceeds 1 MiB")
    if timeout_s < TIMEOUT_MIN_S or timeout_s > TIMEOUT_MAX_S:
        raise _problem("invalid-bound", "timeout_s outside 1..900 seconds")
    if not adapter.qualified:
        raise _problem("provider-unqualified",
                       f"reviewer {adapter.name} profile unproven")
    if not os.path.isfile(adapter.executable) or not os.access(
            adapter.executable, os.X_OK):
        raise _problem("provider-unavailable",
                       f"reviewer {adapter.name} executable missing")

    scratch = tempfile.mkdtemp(prefix="ptest-review-")
    with open(os.path.join(scratch, "schema.json"), "wb") as handle:
        handle.write(schema)

    start_new = os.name == "posix"
    try:
        proc = subprocess.Popen(
            list(adapter.argv), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=scratch,
            env=sanitized_child_env(os.environ), shell=False,
            start_new_session=start_new)
    except OSError as exc:
        raise _problem("provider-unavailable",
                       f"reviewer {adapter.name} failed to start") from exc
    pgid: int | None = None
    if start_new:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None

    def _emit(phase: str) -> None:
        try:
            progress(ProgressEvent(phase=phase, provider=adapter.name,
                                   elapsed_s=time.monotonic() - start))
        except KeyboardInterrupt:
            raise
        except Exception:
            pass  # progress reporting must never fail or leak the review

    def _feed() -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(packet)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    start = time.monotonic()
    feeder = threading.Thread(target=_feed, daemon=True)
    timed_out = False
    exhausted = False
    out_chunks: list[bytes] = []
    out_len = 0
    log.info("review start provider=%s timeout_s=%d", adapter.name, timeout_s)
    try:
        _emit("reviewing")
        feeder.start()
        if proc.stdout is None or proc.stderr is None:
            _stop_owned(proc, pgid)
            raise _problem("provider-unavailable",
                           f"reviewer {adapter.name} pipes unavailable")
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        last_beat = start
        try:
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= timeout_s:
                    timed_out = True
                    break
                now = time.monotonic()
                if now - last_beat >= _HEARTBEAT_S:
                    _emit("reviewing")
                    last_beat = now
                remaining = timeout_s - elapsed
                for key, _ in selector.select(min(0.2, remaining)):
                    try:
                        chunk = key.fileobj.read(65536)
                    except (BlockingIOError, OSError):
                        chunk = None
                    if not chunk:
                        try:
                            selector.unregister(key.fileobj)
                        except (KeyError, ValueError):
                            pass
                        continue
                    if key.fileobj is proc.stdout:
                        out_chunks.append(chunk)
                    out_len += len(chunk)
                    if out_len > OUTPUT_MAX_BYTES:
                        exhausted = True
                        break
                if exhausted:
                    break
                if not selector.get_map():
                    if proc.poll() is not None:
                        break
                    if time.monotonic() - start >= timeout_s:
                        timed_out = True
                        break
                    time.sleep(0.05)
        finally:
            selector.close()
        feeder.join(timeout=5)
        if timed_out:
            _stop_owned(proc, pgid)
            log.info("review timeout provider=%s", adapter.name)
            return _result(adapter, ok=False, assessment=b"",
                           error="timeout", exit_code=proc.poll(),
                           timed_out=True, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        if exhausted:
            _stop_owned(proc, pgid)
            log.info("review output-exhausted provider=%s", adapter.name)
            return _result(adapter, ok=False, assessment=b"",
                           error="output-exhausted", exit_code=proc.poll(),
                           timed_out=False, cancelled=False, truncated=True,
                           pid=proc.pid, scratch=scratch)
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, timeout_s - (time.monotonic()
                                                       - start)))
            except subprocess.TimeoutExpired:
                _stop_owned(proc, pgid)
                return _result(adapter, ok=False, assessment=b"",
                               error="timeout", exit_code=proc.poll(),
                               timed_out=True, cancelled=False,
                               truncated=False, pid=proc.pid, scratch=scratch)
        exit_code = proc.poll()
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if exit_code != 0:
            log.info("review provider-failed provider=%s exit=%s",
                     adapter.name, exit_code)
            return _result(adapter, ok=False, assessment=b"",
                           error="provider-failed", exit_code=exit_code,
                           timed_out=False, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        _emit("validating")
        stdout = b"".join(out_chunks)
        ok, assessment, error = _normalize(adapter.name, stdout)
        if not ok:
            log.info("review %s provider=%s stdout_bytes=%d", error,
                     adapter.name, len(stdout))
            return _result(adapter, ok=False, assessment=b"", error=error,
                           exit_code=exit_code, timed_out=False,
                           cancelled=False, truncated=False, pid=proc.pid,
                           scratch=scratch)
        if len(assessment) > OUTPUT_MAX_BYTES:
            return _result(adapter, ok=False, assessment=b"",
                           error="invalid-assessment", exit_code=exit_code,
                           timed_out=False, cancelled=False, truncated=False,
                           pid=proc.pid, scratch=scratch)
        log.info("review ok provider=%s assessment_bytes=%d", adapter.name,
                 len(assessment))
        return _result(adapter, ok=True, assessment=assessment, error="",
                       exit_code=exit_code, timed_out=False, cancelled=False,
                       truncated=False, pid=proc.pid, scratch=scratch)
    except KeyboardInterrupt:
        _stop_owned(proc, pgid)
        log.info("review cancelled provider=%s", adapter.name)
        return _result(adapter, ok=False, assessment=b"", error="cancelled",
                       exit_code=proc.poll(), timed_out=False, cancelled=True,
                       truncated=False, pid=proc.pid, scratch=scratch)
    except BaseException:
        _stop_owned(proc, pgid)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
