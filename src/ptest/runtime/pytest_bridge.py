"""Dependency-light entry point executed by the selected project interpreter.

The module imports only the standard library until :func:`run` has validated
the generated protocol descriptor.  It intentionally has no ptest imports so
the target interpreter need only provide pytest itself.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _fail(message: str) -> None:
    raise RuntimeError(f"native-config-invalid: {message}")


def _protocol() -> dict[str, Any]:
    name = os.environ.get("PTEST_BRIDGE_PROTOCOL")
    if not name:
        _fail("missing protocol descriptor")
    try:
        raw = Path(name).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _fail("unreadable protocol descriptor")
    if not isinstance(data, dict) or data.get("protocol") != 1:
        _fail("unsupported protocol descriptor")
    return data


def _workers() -> int:
    value = os.environ.get("PTEST_GRANT_WORKERS")
    try:
        workers = int(value or "")
    except ValueError:
        _fail("invalid granted worker count")
    if workers < 1 or workers > 64:
        _fail("invalid granted worker count")
    return workers


def _python_version() -> None:
    """Keep the tested interpreter matrix explicit before importing pytest."""
    version = sys.version_info
    if getattr(version, "major", version[0]) != 3 or not 11 <= getattr(version, "minor", version[1]) <= 14:
        _fail("unsupported CPython version")


class OwnedPlugin:
    """Additive profile gate; it neither replaces reporters nor parses addopts."""

    def __init__(self, workers: int) -> None:
        self.workers = workers

    def pytest_configure(self, config: Any) -> None:
        option = config.option
        remote = any(getattr(option, name, None) for name in ("tx", "px", "rsyncdir"))
        if remote:
            _fail("remote/proxy pytest execution is unsupported")
        configured = getattr(option, "numprocesses", None)
        if self.workers == 1 and configured not in (None, 0, "0"):
            _fail("serial grant cannot use xdist")
        if self.workers > 1 and str(configured) != str(self.workers):
            _fail("xdist worker count differs from admission grant")


def run(argv: list[str] | tuple[str, ...] | None = None) -> int:
    """Run pytest natively after validating the immutable bridge descriptor."""
    _protocol()
    workers = _workers()
    _python_version()
    if argv is None:
        argv = tuple(sys.argv[1:])
    if not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) for item in argv):
        _fail("pytest argv must be a string array")
    try:
        import pytest
    except ImportError:
        _fail("pytest is unavailable in the selected interpreter")
    return int(pytest.main(list(argv), plugins=[OwnedPlugin(workers)]))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except RuntimeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(4)
