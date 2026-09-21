"""Local ownership checks reserved for a future qualified probe subprocess."""

import os
from pathlib import Path


def local_resource_name(prefix: str, worker: str) -> str:
    """Derive a bounded local name; never contact or mutate a remote target."""
    if not prefix or not worker or "/" in prefix or "/" in worker:
        raise ValueError("unsafe local ownership identity")
    return f"{prefix}-{worker}"


def test_probe_fixture_is_local_only():
    assert Path(__file__).is_file()
    assert not os.environ.get("PTEST_REMOTE_URL")
    assert local_resource_name("run-local", "w000") == "run-local-w000"
