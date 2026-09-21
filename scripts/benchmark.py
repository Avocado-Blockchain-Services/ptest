#!/usr/bin/env python3
"""Bounded, replayable local benchmark helpers for ptest acceptance work.

This module is deliberately not imported by the ptest runtime.  It is a root
tool for recording measurements and never invokes a shell or a remote service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 100
WORKLOAD_NAME = "local-miniature-v1"
_VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lexical_no_symlinks(path: Path) -> Path:
    """Lstat every lexical component through the filesystem root."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            stamp = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(stamp.st_mode):
            raise ValueError(f"path component is a symlink: {current}")
    return absolute


@dataclass(frozen=True)
class Sample:
    """One attempt, including failed and capped attempts."""

    seconds: float
    exit_code: int
    capped: bool = False
    label: str | None = None
    artifact: str | None = None


@dataclass(frozen=True)
class BenchmarkSummary:
    sample_count: int
    failed_count: int
    promotable: bool
    median: float
    p95: float
    capped_count: int = 0


def _number(value: object, name: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def summarize_samples(samples: Iterable[Sample]) -> BenchmarkSummary:
    """Summarize every supplied attempt without dropping failures.

    p95 uses nearest-rank (ceil(0.95*n)); this stays deterministic for small
    acceptance batches and avoids inventing interpolation between attempts.
    """
    values = list(samples)
    if not values:
        return BenchmarkSummary(0, 0, False, 0.0, 0.0, 0)
    if len(values) > MAX_SAMPLES:
        raise ValueError(f"at most {MAX_SAMPLES} samples are supported")
    for sample in values:
        if not isinstance(sample, Sample):
            raise TypeError("samples must contain Sample values")
        _number(sample.seconds, "sample.seconds")
        if isinstance(sample.exit_code, bool) or not isinstance(sample.exit_code, int):
            raise ValueError("sample.exit_code must be an integer")
    ordered = sorted(float(sample.seconds) for sample in values)
    rank = max(1, math.ceil(len(ordered) * 0.95))
    failed = sum(sample.exit_code != 0 for sample in values)
    capped = sum(sample.capped for sample in values)
    return BenchmarkSummary(
        sample_count=len(values),
        failed_count=failed,
        promotable=failed == 0 and capped == 0,
        median=float(median(ordered)),
        p95=ordered[rank - 1],
        capped_count=capped,
    )


def _bounded_bytes(data: bytes) -> tuple[bytes, bool]:
    return data[:MAX_OUTPUT_BYTES], len(data) > MAX_OUTPUT_BYTES


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Write one retained artifact exactly once with restrictive permissions."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _private_artifact_dir(path: Path) -> Path:
    """Create an absent 0700 directory, rejecting all pre-existing targets."""
    path = _lexical_no_symlinks(path)
    if path.exists() or path.is_symlink():
        raise ValueError("benchmark artifact directory must be newly created")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    stamp = os.stat(path)
    if path.is_symlink() or stamp.st_uid != os.getuid() or (stamp.st_mode & 0o777) != 0o700:
        raise ValueError("benchmark artifact directory has unsafe ownership or mode")
    return path


def _stream_process(proc: subprocess.Popen[bytes], timeout: float) -> tuple[bytes, bytes, bool]:
    """Stream both pipes, terminating on timeout or per-stream output cap."""
    selector = selectors.DefaultSelector()
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    streams = dict(buffers)
    for stream in streams:
        assert stream is not None
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    capped = False
    terminated = False
    while streams:
        remaining = deadline - time.monotonic()
        if not terminated and remaining <= 0:
            capped = True
            _terminate(proc)
            terminated = True
        events = selector.select(0.05 if terminated else min(0.05, remaining))
        if not events and terminated and proc.poll() is not None:
            # Pipes can remain registered briefly after the child exits; keep
            # polling until EOF so retained output is complete up to the cap.
            events = selector.select(0)
        for key, _ in events:
            stream = key.fileobj
            try:
                piece = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                continue
            if not piece:
                selector.unregister(stream)
                streams.pop(stream, None)
                continue
            buffer = buffers[stream]
            room = MAX_OUTPUT_BYTES - len(buffer)
            if room <= 0:
                capped = True
                if not terminated:
                    _terminate(proc)
                    terminated = True
                continue
            buffer.extend(piece[:room])
            if len(piece) > room:
                capped = True
                if not terminated:
                    _terminate(proc)
                    terminated = True
        if terminated and proc.poll() is not None and not events:
            # A final zero-time poll above is enough to avoid waiting forever
            # on a broken descriptor; unregister all remaining streams.
            for stream in list(streams):
                try:
                    selector.unregister(stream)
                except KeyError:
                    pass
                streams.pop(stream, None)
        elif terminated and time.monotonic() > deadline + 6:
            for stream in list(streams):
                try:
                    selector.unregister(stream)
                except KeyError:
                    pass
                streams.pop(stream, None)
    selector.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    return bytes(buffers.get(proc.stdout, b"")), bytes(buffers.get(proc.stderr, b"")), capped


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    artifact_dir: Path,
    label: str,
    env: dict[str, str] | None = None,
) -> Sample:
    """Run one finite literal-argv command and retain stdout/stderr."""
    if not argv or any(not isinstance(token, str) or not token for token in argv):
        raise ValueError("benchmark commands require non-empty string argv")
    timeout = _number(timeout, "timeout")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ValueError("benchmark cwd must be a directory")
    _private_artifact_dir(artifact_dir)
    safe_label = "".join(char if char.isalnum() or char in "-_" else "_" for char in label)
    stdout_path = artifact_dir / f"{safe_label}.stdout"
    stderr_path = artifact_dir / f"{safe_label}.stderr"
    started = time.monotonic()
    capped = False
    exit_code = 127
    try:
        child_env = {"PATH": os.environ.get("PATH", "")}
        if env:
            child_env.update(env)
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr, capped = _stream_process(proc, timeout)
        exit_code = 124 if capped else int(proc.returncode)
    except (OSError, ValueError) as exc:
        stdout = b""
        stderr = str(exc).encode("utf-8", "replace")
    elapsed = time.monotonic() - started
    stdout, stdout_capped = _bounded_bytes(stdout)
    stderr, stderr_capped = _bounded_bytes(stderr)
    capped = capped or stdout_capped or stderr_capped
    _write_private(stdout_path, stdout)
    _write_private(stderr_path, stderr)
    return Sample(
        seconds=elapsed,
        exit_code=exit_code,
        capped=capped,
        label=label,
        artifact=str(artifact_dir),
    )


def environment_metadata(root: Path) -> dict[str, object]:
    """Return non-secret reproducibility metadata for an evidence record."""
    commit = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": os.environ.get("PATH", "")},
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(aliased=True),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "ptest": shutil.which("ptest"),
        "commit": commit,
    }


def _validate_output(output: Path, root: Path) -> Path:
    """Keep benchmark artifacts outside the source checkout."""
    output = _lexical_no_symlinks(output)
    resolved = output.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("benchmark output must be outside the checkout")
    if resolved.exists():
        raise ValueError("benchmark evidence output must be a new directory")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.mkdir(mode=0o700)
    stamp = os.stat(resolved)
    if stamp.st_uid != os.getuid() or (stamp.st_mode & 0o777) != 0o700:
        raise ValueError("benchmark evidence output has unsafe mode")
    return resolved


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--label", default="sample")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="candidate ptest executable")
    parser.add_argument("--profile", required=True,
                        help=f"typed workload profile (must be {WORKLOAD_NAME!r})")
    parser.add_argument("--", dest="separator", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(list(argv))
    if parsed.command and parsed.command[0] == "--":
        parsed.command = parsed.command[1:]
    if not parsed.command:
        parser.error("provide a literal command after --")
    if parsed.samples < 1 or parsed.samples > MAX_SAMPLES:
        parser.error(f"--samples must be between 1 and {MAX_SAMPLES}")
    return parsed


def _validate_candidate_invocation(args: argparse.Namespace) -> tuple[Path, str]:
    """Validate the only currently executable workload before creating output."""
    candidate = _lexical_no_symlinks(args.candidate)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError("candidate must be an executable regular file")
    resolved = candidate.resolve()
    if resolved.name != "ptest":
        raise ValueError("candidate must be the ptest executable, not a raw runner")
    if args.profile != WORKLOAD_NAME:
        raise ValueError(f"unsupported benchmark profile: {args.profile}")
    command = tuple(args.command)
    if (len(command) != 2 or Path(command[0]).absolute().resolve() != resolved
            or command[1] != "--version"):
        raise ValueError("only the literal candidate ptest --version workload is supported")
    try:
        source = resolved.read_bytes()[:65536]
    except OSError as exc:
        raise ValueError("candidate cannot be inspected") from exc
    if b"ptest.cli" not in source or b"main" not in source:
        raise ValueError("candidate is not an identifiable ptest console script")
    return resolved, hashlib.sha256(resolved.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"benchmark root is not a directory: {root}", file=sys.stderr)
        return 2
    try:
        candidate_path, candidate_digest = _validate_candidate_invocation(args)
    except ValueError as exc:
        print(f"benchmark refused: {exc}", file=sys.stderr)
        return 2
    owned_tmp = args.output is None
    if owned_tmp:
        output = Path(tempfile.mkdtemp(prefix="ptest-benchmark-", dir="/tmp"))
    else:
        output = _validate_output(args.output, root)
    samples = [
        run_command(
            args.command,
            cwd=root,
            timeout=args.timeout,
            artifact_dir=output / "attempts" / f"sample-{index + 1}",
            label=f"{args.label}-{index + 1}",
        )
        for index in range(args.samples)
    ]
    summary = summarize_samples(samples)
    candidate_bound = True
    first_output = output / "attempts" / "sample-1" / "sample-1.stdout"
    try:
        candidate_bound = bool(_VERSION_RE.fullmatch(
            first_output.read_text(encoding="utf-8", errors="replace").strip()
        ))
    except OSError:
        candidate_bound = False
    # The currently implemented subprocess is a candidate/version smoke, not
    # the declared performance workload.  Keep its measurements, but never
    # promote a speed claim until the typed setup/queue/RSS/coverage fields are
    # actually collected.
    promotable = False
    summary = replace(summary, promotable=promotable)
    payload = {
        "schema_version": 1,
        "kind": "benchmark",
        "root": str(root),
        "command": list(args.command),
        "candidate": str(candidate_path),
        "candidate_sha256": candidate_digest,
        "candidate_bound": candidate_bound,
        "profile": args.profile or None,
        "promotion_blocked_reason": (
            "version-only diagnostic; typed performance workload is not implemented"
        ),
        "timeout_seconds": args.timeout,
        "environment": environment_metadata(root),
        "samples": [asdict(sample) for sample in samples],
        "summary": asdict(summary),
        "artifacts": str(output),
        "temporary_output": owned_tmp,
    }
    _write_private(output / "benchmark.json", json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if summary.promotable else 1


if __name__ == "__main__":
    raise SystemExit(main())
