#!/usr/bin/env python3
"""Bounded, replayable local benchmark helpers for ptest acceptance work.

This module is deliberately not imported by the ptest runtime.  It is a root
tool for recording measurements and never invokes a shell or a remote service.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 100


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
    if artifact_dir.exists() and artifact_dir.is_symlink():
        raise ValueError("benchmark artifact directory may not be a symlink")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise ValueError("benchmark artifact directory is not an owned directory")
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
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = int(proc.returncode)
        except subprocess.TimeoutExpired as exc:
            capped = True
            _terminate(proc)
            stdout = exc.output or b""
            stderr = exc.stderr or b""
            exit_code = 124
    except (OSError, ValueError) as exc:
        stdout = b""
        stderr = str(exc).encode("utf-8", "replace")
    elapsed = time.monotonic() - started
    stdout, stdout_capped = _bounded_bytes(stdout)
    stderr, stderr_capped = _bounded_bytes(stderr)
    capped = capped or stdout_capped or stderr_capped
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
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
    if output.exists() and output.is_symlink():
        raise ValueError("benchmark output may not be a symlink")
    resolved = output.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("benchmark output must be outside the checkout")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--label", default="sample")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"benchmark root is not a directory: {root}", file=sys.stderr)
        return 2
    owned_tmp = args.output is None
    output = Path(tempfile.mkdtemp(prefix="ptest-benchmark-")) if owned_tmp else args.output
    output = _validate_output(output, root)
    samples = [
        run_command(
            args.command,
            cwd=root,
            timeout=args.timeout,
            artifact_dir=output / "attempts",
            label=f"{args.label}-{index + 1}",
        )
        for index in range(args.samples)
    ]
    summary = summarize_samples(samples)
    payload = {
        "schema_version": 1,
        "kind": "benchmark",
        "root": str(root),
        "command": list(args.command),
        "timeout_seconds": args.timeout,
        "environment": environment_metadata(root),
        "samples": [asdict(sample) for sample in samples],
        "summary": asdict(summary),
        "artifacts": str(output),
        "temporary_output": owned_tmp,
    }
    (output / "benchmark.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if summary.promotable else 1


if __name__ == "__main__":
    raise SystemExit(main())
