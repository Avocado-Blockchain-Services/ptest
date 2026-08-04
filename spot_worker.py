"""One-suite-at-a-time Spot worker primitives."""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from spot_queue import SpotResult


def run_claimed_request(request, workspace_root: Path, adapter) -> SpotResult:
    """Run one leased request; publish before acknowledging its Pub/Sub message."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="ptest-spot-", dir=workspace_root))
    archive = workspace / "source.tar.gz"
    source = workspace / "source"
    source.mkdir()
    try:
        adapter.download(request.source_uri, archive)
        adapter.unpack(archive, source)
        code, output = adapter.run(request.command, source)
        result = SpotResult(
            request.request_key, "passed" if code == 0 else "failed", code,
            output, datetime.now(timezone.utc),
        )
        if not adapter.publish_result(result):
            return SpotResult(request.request_key, "infrastructure", None,
                              "result publication lost", datetime.now(timezone.utc))
        if not getattr(adapter, "preempted", lambda: False)():
            adapter.acknowledge()
        return result
    except Exception as exc:
        result = SpotResult(request.request_key, "infrastructure", None, str(exc),
                            datetime.now(timezone.utc))
        try:
            adapter.publish_result(result)
        except Exception:
            pass
        return result
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
