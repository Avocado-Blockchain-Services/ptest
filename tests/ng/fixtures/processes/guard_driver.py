"""Test-only isolated entrypoint for exercising the real guard process group."""
from __future__ import annotations

import os

from ptest import guard, platform


def main() -> int:
    guard.scheduler.register_guard = lambda *_args: True
    guard.scheduler.mark_draining = lambda *_args: os.environ.get("GUARD_DRAIN", "1") == "1"
    return guard.run_guard(int(os.environ["GUARD_CONTROL_FD"]),
                           int(os.environ["GUARD_MANIFEST_FD"]))


if __name__ == "__main__":
    raise SystemExit(main())
