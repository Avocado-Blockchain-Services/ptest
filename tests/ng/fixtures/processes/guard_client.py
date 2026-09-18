"""Independent admission owner controlled through private stdin/stdout pipes."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ptest import contracts as C, platform, scheduler


domain = platform.domain_paths(Path(sys.argv[1]))
root = Path(sys.argv[2])
ticket = scheduler.enqueue(domain, C.AdmissionRequest(
    run_id=os.urandom(16).hex(),
    checkout=C.CheckoutIdentity(project_id="a" * 32,
                                checkout_id=os.urandom(16).hex(), root=root),
    owner=platform.process_identity(os.getpid()), slots=1, exclusive=False,
    fixture=True, deadline=time.monotonic() + 60))
grant = None


def cancel(signum, _frame):
    if grant is None:
        raise SystemExit(128 + signum)
    os.write(int(sys.argv[3]), C.encode_control_frame(C.ControlFrame(
        protocol=1, run_id=grant.run_id, nonce=grant.nonce, kind="cancel",
        payload={"signal": signum})))


signal.signal(signal.SIGINT, cancel)
signal.signal(signal.SIGTERM, cancel)
for command in sys.stdin:
    assert command == "poll\n"
    state = scheduler.poll(domain, ticket)
    grant = state.grant
    print(json.dumps({"ticket": asdict(ticket), "state": state.state.value,
                      "grant": None if state.grant is None else asdict(state.grant)}), flush=True)
