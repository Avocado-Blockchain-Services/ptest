"""Guard wrapper that exposes a child-active barrier for shadow regressions."""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from ptest import guard


def main() -> int:
    control_fd = int(os.environ.pop("GUARD_CONTROL_FD"))
    manifest_fd = int(os.environ.pop("GUARD_MANIFEST_FD"))
    active_path = Path(os.environ.pop("SHADOW_ACTIVE_MARKER"))
    release_path = Path(os.environ.pop("SHADOW_ACTIVE_RELEASE"))
    active_attempt = os.environ.pop("SHADOW_ACTIVE_ATTEMPT", "a002")
    advance = float(os.environ.pop("SHADOW_ACTIVE_ADVANCE", "0"))
    clock_offset = [0.0]
    if advance:
        guard.time = SimpleNamespace(
            monotonic=lambda: time.monotonic() + clock_offset[0])
    launches = 0
    original_popen = guard.subprocess.Popen

    def launch(*args, **kwargs):
        nonlocal launches
        process = original_popen(*args, **kwargs)
        launches += 1
        active_path.write_text(str(launches), encoding="ascii")
        if launches == int(active_attempt[1:]):
            clock_offset[0] += advance
            deadline = time.monotonic() + 45
            while not release_path.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("active-child barrier controller disappeared")
                time.sleep(0.01)
        return process

    guard.subprocess.Popen = launch
    return guard.run_guard(control_fd, manifest_fd)


if __name__ == "__main__":
    raise SystemExit(main())
