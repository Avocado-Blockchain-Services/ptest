"""Conservative evidence for a literal native Vitest full command.

Unknown native semantics remain executable; they simply cannot certify reuse.
No repository JavaScript executes while establishing this identity.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from . import contracts as C
from .adapters import vitest

_MAX_FILES = 16384
_MAX_TOTAL = 512 * 1024 * 1024
_MAX_FILE = 192 * 1024 * 1024
_ENV = ("NODE_ENV", "TZ", "CI", "VITEST_MAX_WORKERS", "VITEST_MAX_THREADS",
        "VITEST_MAX_FORKS", "VITEST_MIN_THREADS", "VITEST_MIN_FORKS")
# These native options cannot narrow collection or allow a no-tests success.
_BOOLEAN = {"fileparallelism", "nofileparallelism", "coverage", "silent", "nocolor", "color"}
_VALUE = {"maxworkers", "minworkers", "reporter", "pool", "testtimeout", "hooktimeout"}


class _Unknown(Exception):
    pass


def _name(token: str) -> str:
    return token.replace("-", "").lower()


def full_command_qualified(config: C.Config, request: C.RunRequest) -> bool:
    """Only certify a known whole-suite invocation with native no-tests failure."""
    if (config.runner.kind is not C.RunnerKind.VITEST or request.mode is not C.Mode.FULL
            or request.argv or request.setup_only or request.shadow or request.probe is not None):
        return False
    try:
        vitest._require_node_launcher(config.runner.launcher)
        root = vitest._project_root(config)
        # Configs are executable code; textual guesses cannot prove no-tests
        # or collection semantics. Native discovery also searches ancestors.
        for directory in (root, *root.parents):
            for filename in (*vitest._VITEST_CONFIGS, *vitest._VITE_CONFIGS, *vitest._WORKSPACE_FILES):
                try:
                    os.lstat(directory / filename)
                except FileNotFoundError:
                    continue
                return False
        args = config.runner.args + config.runner.full_args
        index = 0
        while index < len(args):
            token = args[index]
            if not token.startswith("--") or token == "--":
                return False
            option, separator, value = token[2:].partition("=")
            name = _name(option)
            if name in _BOOLEAN:
                if separator and value not in {"true", "false"}:
                    return False
            elif name in _VALUE:
                if not separator:
                    index += 1
                    if index >= len(args):
                        return False
                    value = args[index]
                if not value or value.startswith("-"):
                    return False
                if name in {"maxworkers", "minworkers"} and not re.fullmatch(r"[1-9][0-9]*(?:%)?", value):
                    return False
                if name == "pool" and value not in {"threads", "forks", "vmThreads", "vmForks"}:
                    return False
                # Arbitrary reporter modules execute unobserved repository code.
                if name == "reporter" and value not in {"default", "verbose", "dot", "basic", "json", "junit", "tap", "tap-flat", "hanging-process"}:
                    return False
            else:
                return False
            index += 1
        return True
    except (C.Problem, OSError, ValueError):
        return False


def _stamp(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _read(fd: int, limit: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise _Unknown()
    blocks = []
    size = 0
    while chunk := os.read(fd, min(1024 * 1024, limit + 1 - size)):
        blocks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise _Unknown()
    if _stamp(before) != _stamp(os.fstat(fd)):
        raise _Unknown()
    return b"".join(blocks)


def _file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _read(fd, limit)
    finally:
        os.close(fd)


def _local_file(root: Path, parts: tuple[str, ...], limit: int) -> bytes:
    """Read installation metadata without following a dependency-directory link."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            return _read(fd, limit)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def runtime_identity(config: C.Config) -> str | None:
    """Hash current installed bytes, bounded and anchored against link escapes.

    The first implementation qualifies standard npm installations. Unknown
    package-manager layouts decline reuse rather than trusting lockfile text.
    Runtime-controlled caches and npm's launcher symlinks are not loaded by
    the direct entry command and are excluded at node_modules' top level.
    """
    try:
        vitest._require_node_launcher(config.runner.launcher)
        root = vitest._project_root(config)
        if os.environ.get("NODE_OPTIONS") or os.environ.get("NODE_PATH"):
            return None  # Can inject code from outside the declared installation.
        if any(name.startswith("VITEST_") and name not in _ENV for name in os.environ):
            return None
        executable = shutil.which(config.runner.launcher[0])
        if executable is None:
            return None
        resolved = Path(executable).resolve(strict=True)
        node = _file(resolved, _MAX_FILE)
        lock = _file(root / "package-lock.json", 8 * 1024 * 1024)
        parsed = json.loads(lock)
        if parsed.get("lockfileVersion") not in {2, 3}:
            return None
        package = json.loads(_local_file(root, ("node_modules", "vitest", "package.json"), 65536))
        version = package.get("version")
        if (package.get("name") != "vitest" or not isinstance(version, str)
                or not re.fullmatch(r"[34]\.\d+\.\d+(?:[-+][\w.+-]+)?", version)
                or parsed.get("packages", {}).get("node_modules/vitest", {}).get("version") != version):
            return None
        hasher = hashlib.sha256(b"ptest-native-vitest-full-v1\0")
        for value in (str(resolved).encode(), node, lock,
                      json.dumps([(name, os.environ.get(name)) for name in _ENV], separators=(",", ":")).encode()):
            hasher.update(len(value).to_bytes(8, "big"))
            hasher.update(value)
        count, total, entries = 0, len(node) + len(lock), 0
        entry_seen = False

        def visit(fd: int, prefix: str, depth: int) -> None:
            nonlocal count, total, entries, entry_seen
            if depth > 32:
                raise _Unknown()
            before = os.fstat(fd)
            names = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > _MAX_FILES * 2:
                        raise _Unknown()
                    names.append(entry.name)
            names.sort()
            for name in names:
                if not prefix and name in {".bin", ".vite", ".vite-temp", ".cache"}:
                    continue
                relative = prefix + name
                stamp = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(stamp.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, relative + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(stamp.st_mode):
                    count += 1
                    if count > _MAX_FILES:
                        raise _Unknown()
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        data = _read(child, _MAX_FILE)
                    finally:
                        os.close(child)
                    total += len(data)
                    if total > _MAX_TOTAL:
                        raise _Unknown()
                    encoded = relative.encode()
                    hasher.update(len(encoded).to_bytes(8, "big"))
                    hasher.update(encoded)
                    hasher.update(len(data).to_bytes(8, "big"))
                    hasher.update(data)
                    entry_seen |= relative == "vitest/vitest.mjs"
                else:
                    raise _Unknown()  # Symlinks, devices and sockets are not evidence.
            if _stamp(before) != _stamp(os.fstat(fd)):
                raise _Unknown()

        fd = os.open(root / "node_modules", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(fd, "", 0)
        finally:
            os.close(fd)
        return hasher.hexdigest() if entry_seen else None
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, C.Problem, _Unknown):
        return None


def reusable_full_result(result: C.RunResult) -> bool:
    """Controller-only native success with stable clean inputs, without inventory."""
    before, after = result.input_before, result.input_after
    return bool(
        result.command.kind is C.RunnerKind.VITEST
        and result.mode is C.Mode.FULL and result.plan.execution == "full"
        and result.status is C.Status.PASSED and result.phase == "complete"
        and result.source_valid and result.full_gate_eligible
        and result.exit_code == 0 and result.runner_exit_code == 0
        and result.signal is None and result.exit_origin == "runner"
        and result.runtime_identity is not None and result.policy_digest is not None
        and before is not None and after is not None and before.clean and after.clean
        and not before.limitations and not after.limitations
        and before.digest is not None and before.digest == after.digest
        and before.compatibility is not None and before.compatibility == after.compatibility
        and before.head is not None and before.head == after.head
        and result.plan.input_digest == after.digest
        and result.plan.compatibility == after.compatibility
        and any(item.phase == "execution" for item in result.attempts)
        and all(item.status is C.Status.PASSED
                and item.raw_exit_code == 0 and item.final_exit_code == 0
                and item.source_valid for item in result.attempts)
    )
